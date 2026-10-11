"""Bybit V5 connector — wiring (REST instruments + WS subscribe build).

Appendix §7:
- WS public endpoints differ by category:
  ``wss://stream.bybit.com/v5/public/{spot|linear|inverse|option}``
- Subscribe format: ``{"op": "subscribe", "args": ["publicTrade.BTCUSDT"]}``
  (dot-delimited topic.symbol)
- REST: ``https://api.bybit.com/v5``
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from crocodile.core.capture.schema import RawFrame, SnapshotRequest
from crocodile.core.capture.snapshots import HttpSnapshotProvider, SnapshotProvider
from crocodile.core.errors import ConfigError, TransientConnectorError
from crocodile.core.ingest.transport import Transport
from crocodile.core.schema.records import Record
from crocodile.core.sink.base import Sink
from crocodile.core.util.time import ms_to_ns
from crocodile.crypto.exchanges.public_books import GatedConnector, PublicBookError
from crocodile.crypto.instruments.registry import Instrument, InstrumentRegistry, Kind

from .book import BybitBookGate
from .normalize import normalize_message

log = logging.getLogger(__name__)

EXCHANGE = "bybit"
REST_BASE = "https://api.bybit.com/v5"

# Mapping from canonical channel names to Bybit topic patterns.
# ``{sym}`` is substituted with the symbol; ``{depth}`` with the orderbook depth.
_CHANNEL_MAP: dict[str, str] = {
    "trade": "publicTrade.{sym}",
    "book_delta": "orderbook.50.{sym}",
    "book_snapshot": "orderbook.50.{sym}",
    "derivative_ticker": "tickers.{sym}",
    "options_chain": "tickers.{sym}",
    "funding": "tickers.{sym}",  # live funding derived from ticker stream
    "book_ticker": "tickers.{sym}",
    "liquidation": "liquidation.{sym}",
}

# Categories that require separate WS endpoints
_CATEGORY_WS_URL: dict[str, str] = {
    "spot": "wss://stream.bybit.com/v5/public/spot",
    "linear": "wss://stream.bybit.com/v5/public/linear",
    "inverse": "wss://stream.bybit.com/v5/public/inverse",
    "option": "wss://stream.bybit.com/v5/public/option",
}


def build_channels(
    symbols: list[str],
    channels: list[str],
    category: str = "linear",
    *,
    book_mode: str = "fast",
    book_depth: int | None = None,
) -> list[str]:
    """Return Bybit topic strings for the given symbols and channel kinds.

    Deduplicates: multiple canonical channels that map to the same Bybit topic
    (e.g. ``derivative_ticker``/``funding``/``book_ticker`` all map to
    ``tickers.{sym}``) are collapsed.
    """
    result: set[str] = set()
    for sym in symbols:
        for ch in channels:
            pattern = _CHANNEL_MAP.get(ch)
            if pattern is not None:
                if ch in {"book_delta", "book_snapshot"}:
                    depth = (
                        book_depth
                        if book_depth is not None
                        else (25 if category == "option" else 50)
                    )
                    pattern = f"orderbook.{'full' if book_mode == 'full' else depth}.{{sym}}"
                result.add(pattern.format(sym=sym))
    return sorted(result)


def parse_instruments(raw: dict[str, Any], category: str = "linear") -> list[Instrument]:
    """Parse the JSON response from Bybit ``/v5/market/instruments-info``.

    Supports ``linear`` (perp + dated futures), ``inverse``, ``option``,
    and ``spot`` categories. The response shape is:
    ``{"result": {"list": [...instrument dicts...]}}``.
    """
    out: list[Instrument] = []
    result: dict[str, Any] = raw.get("result") or {}
    items: list[dict[str, Any]] = result.get("list") or []

    for item in items:
        sym: str = item["symbol"]
        base: str = item.get("baseCoin", "")
        quote: str = item.get("quoteCoin", "USD")
        settle: str | None = item.get("settleCoin")
        contract_type: str = item.get("contractType", "")
        options_type: str | None = item.get("optionsType")

        # Determine kind
        if category == "option" or options_type:
            kind = Kind.OPTION
        elif "Perpetual" in contract_type:
            kind = Kind.PERPETUAL
        elif "Future" in contract_type or category in ("linear", "inverse"):
            kind = Kind.FUTURE
        elif category == "spot":
            kind = Kind.SPOT
        else:
            kind = Kind.PERPETUAL

        # Option-specific fields
        strike: float | None = None
        expiry: int | None = None
        opt_type: str | None = None

        if kind == Kind.OPTION:
            strike_raw = item.get("strikePrice")
            if strike_raw:
                try:
                    strike = float(strike_raw)
                except ValueError:
                    pass

            delivery_raw = item.get("deliveryTime")
            if delivery_raw:
                try:
                    expiry = ms_to_ns(int(delivery_raw))
                except (ValueError, TypeError):
                    pass

            if options_type:
                lowered = options_type.lower()
                opt_type = "C" if lowered == "call" else "P" if lowered == "put" else None

        # tick_size from priceFilter or top-level tickSize
        tick_size: float | None = None
        price_filter: dict[str, Any] | None = item.get("priceFilter")
        if price_filter:
            ts_raw = price_filter.get("tickSize")
        else:
            ts_raw = item.get("tickSize")
        if ts_raw:
            try:
                tick_size = float(ts_raw)
            except (ValueError, TypeError):
                pass

        out.append(
            Instrument(
                canonical=f"{EXCHANGE}:{sym}",
                exchange=EXCHANGE,
                symbol_raw=sym,
                kind=kind,
                base=base,
                quote=quote,
                strike=strike,
                expiry=expiry,
                opt_type=opt_type,
                tick_size=tick_size,
                settlement_currency=settle,
            )
        )
    return out


class BybitConnector(GatedConnector):
    """Bybit V5 WebSocket connector.

    ``category`` selects the public WS endpoint:
    ``spot`` | ``linear`` | ``inverse`` | ``option``.
    Defaults to ``linear`` (USDT-margined perpetuals).

    Fast mode uses fresh WebSocket snapshots. Full mode independently buffers
    deltas until a recorded REST full snapshot matches both ``seq`` and ``u``.
    A full counter gap or reset reconnects and obtains a fresh REST anchor.
    """

    name = EXCHANGE
    rest_url = REST_BASE
    gate: BybitBookGate

    def __init__(
        self,
        symbols: list[str],
        channels: list[str],
        out: Sink,
        registry: InstrumentRegistry,
        category: str = "linear",
        *,
        book_mode: str = "fast",
        book_depth: int | None = None,
        snapshot_provider: SnapshotProvider | None = None,
        bootstrap_timeout: float = 10.0,
    ) -> None:
        if category not in _CATEGORY_WS_URL or book_mode not in {"fast", "full"}:
            raise ConfigError("Bybit category/book_mode is not supported")
        depth = book_depth if book_depth is not None else (25 if category == "option" else 50)
        if depth not in ({25, 100} if category == "option" else {1, 50, 200, 1000}):
            raise ConfigError("Bybit depth is not supported for category")
        if book_mode == "full" and category == "option":
            raise ConfigError("Bybit full does not support options")
        if not 0 < bootstrap_timeout <= 30:
            raise ConfigError("Bybit bootstrap timeout expects 0 < seconds <= 30")
        super().__init__(symbols=symbols, channels=channels, out=out, registry=registry)
        self.category = category
        self.quantity_unit = (
            "quote_contracts"
            if category == "inverse"
            else "native_contracts"
            if category == "option"
            else "base_asset"
        )
        self.market = category
        self.book_mode, self.book_depth = book_mode, depth
        self.required_feed = book_mode == "full"
        self.capture_endpoint_id = f"bybit-{category}-{book_mode}-{depth}"
        self.decoder_id = "bybit-json"
        self.gate = BybitBookGate(mode=book_mode, depth=depth)
        self.coverage = {
            "price_levels_per_side": None if book_mode == "full" else depth,
            "documented_initial_snapshot_limit_per_side": 10000 if book_mode == "full" else None,
            "cadence_ms": 200
            if book_mode == "full"
            else {1: 10, 25: 20, 50: 20, 100: 100, 200: 100, 1000: 200}[depth],
            "far_unchanged_levels_unknown": book_mode == "full",
        }
        self._snapshot_provider = snapshot_provider
        self._bootstrap_timeout = bootstrap_timeout
        self._book_lock = asyncio.Lock()
        self._pending: list[tuple[dict[str, Any], int, int | None, int]] = []
        self._pending_bytes = 0
        self._pending_event = asyncio.Event()
        self.ws_url = _CATEGORY_WS_URL.get(category, _CATEGORY_WS_URL["linear"])
        self._sub_topics = build_channels(
            symbols, channels, category=category, book_mode=book_mode, book_depth=depth
        )

    def normalize(self, msg: object, local_ts: int) -> Iterable[Record]:
        if isinstance(msg, dict):
            yield from normalize_message(
                msg, local_ts=local_ts, venue=self.name, registry=self.registry
            )

    async def list_instruments(self) -> list[Instrument]:  # pragma: no cover
        """Fetch instruments from Bybit REST API for this connector's category."""
        instruments: list[Instrument] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"category": self.category, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            url = f"{REST_BASE}/market/instruments-info"
            data = await self.http_get(url, params=params)
            if data.get("retCode") != 0:
                raise PublicBookError("Bybit instrument query rejected")
            instruments.extend(parse_instruments(data, category=self.category))
            next_cursor: str = (data.get("result") or {}).get("nextPageCursor", "")
            if not next_cursor:
                break
            cursor = next_cursor
        return instruments

    def subscribe_channels(self) -> list[str]:
        return self._sub_topics

    async def _subscribe(self, transport: Transport) -> None:  # pragma: no cover
        """Send a Bybit V5 subscribe frame."""
        topics = self.subscribe_channels()
        if topics:
            frame = json.dumps({"op": "subscribe", "args": topics}).encode()
            await transport.send(frame)
        if self.book_mode == "full" and any(
            c in {"book_delta", "book_snapshot"} for c in self.channels
        ):
            await self._bootstrap_full()

    async def _prepare_connection(self) -> None:
        await super()._prepare_connection()
        self._pending.clear()
        self._pending_bytes = 0
        self._pending_event.clear()

    async def _handle_control_message(self, msg: object, frame: RawFrame) -> bool:
        if isinstance(msg, dict) and (msg.get("success") is False or msg.get("retCode", 0) != 0):
            raise PublicBookError("Bybit public subscription rejected")
        return True

    async def _handle_message(self, msg: object, local_ts: int) -> None:
        async with self._book_lock:
            if (
                self.book_mode == "full"
                and isinstance(msg, dict)
                and str(msg.get("topic", "")).startswith("orderbook.full.")
                and (msg.get("data") or {}).get("s") not in self.gate.ready
            ):
                self._apply_public(msg, local_ts)
                if msg["data"]["s"] not in self.symbols:
                    raise PublicBookError("Bybit full symbol is outside the subscription")
                size = len(repr(msg).encode()) + 256
                if self._pending_bytes + size > 8 << 20 or len(self._pending) >= 4096:
                    raise PublicBookError("Bybit full bootstrap buffer overflow")
                self._pending.append((msg, local_ts, self._origin_ordinal, size))
                self._pending_bytes += size
                self._pending_event.set()
                return
            await super()._handle_message(msg, local_ts)

    def telemetry(self) -> dict[str, object]:
        return {
            **super().telemetry(),
            "known_book_levels": {
                symbol: {
                    "bids": len(self.gate.bids.get(symbol, {})),
                    "asks": len(self.gate.asks.get(symbol, {})),
                }
                for symbol in self.gate.ready
            },
            "book_states": {
                symbol: "available"
                if self.gate.bids.get(symbol) or self.gate.asks.get(symbol)
                else "no_book"
                for symbol in self.gate.ready
            },
        }

    async def _bootstrap_full(self) -> None:
        provider = self._snapshot_provider or HttpSnapshotProvider(
            self.http_session, self.capture, base_url="https://api.bybit.com"
        )
        for symbol in self.symbols:
            async with asyncio.timeout(self._bootstrap_timeout):
                while True:
                    async with self._book_lock:
                        buffered = [x for x in self._pending if x[0]["data"]["s"] == symbol]
                        self._pending_event.clear()
                    if buffered:
                        break
                    await self._pending_event.wait()
                for _ in range(8):
                    request = SnapshotRequest(
                        request_id=uuid.uuid4().hex,
                        stream_id=self._stream_id() if self.capture else "live",
                        source="bybit",
                        symbol_raw=symbol,
                        path="/v5/market/full_orderbook",
                        params={"category": self.category, "symbol": symbol},
                    )
                    response = await provider.fetch(request)
                    raw = json.loads(response.response.payload, parse_float=Decimal)
                    if raw.get("retCode") != 0 or not isinstance(raw.get("result"), dict):
                        raise PublicBookError("Bybit full snapshot API rejected")
                    snapshot = raw["result"]
                    if snapshot.get("s") != symbol:
                        raise PublicBookError("Bybit full snapshot symbol mismatch")
                    while True:
                        async with self._book_lock:
                            buffered = [x for x in self._pending if x[0]["data"]["s"] == symbol]
                            matching = [
                                x
                                for x in buffered
                                if x[0]["data"].get("seq") == snapshot.get("seq")
                                and x[0]["data"].get("u") == snapshot.get("u")
                            ]
                            if matching:
                                result = self.gate.anchor(
                                    snapshot, response.response.received_utc_ns, self.registry
                                )
                                self._count(result, raw_frame=False)
                                await self._emit_records(
                                    list(result.records),
                                    action="snapshot",
                                    reason="bybit_full_anchor",
                                    origin_ordinal=response.origin_ordinal,
                                )
                                for msg, ts, origin, _size in buffered:
                                    result = self._apply_public(msg, ts)
                                    self._count(result)
                                    await self._emit_records(
                                        list(result.records),
                                        action=result.action,
                                        reason="public_book",
                                        origin_ordinal=origin,
                                    )
                                    if result.action == "gap":
                                        raise TransientConnectorError(
                                            "Bybit full buffered delta gap"
                                        )
                                self._pending = [
                                    x for x in self._pending if x[0]["data"]["s"] != symbol
                                ]
                                self._pending_bytes = sum(x[3] for x in self._pending)
                                break
                            # If WS passed this anchor it cannot subsequently match.
                            # Keep a future REST anchor while waiting for WS to catch up.
                            passed = any(x[0]["data"]["seq"] >= snapshot["seq"] for x in buffered)
                            self._pending_event.clear()
                        if passed:
                            break
                        await self._pending_event.wait()
                    if matching:
                        break
                else:
                    raise TransientConnectorError("Bybit full anchor did not match stream")

    async def _drain_market(self) -> None:
        if any(c in {"book_delta", "book_snapshot"} for c in self.channels):
            await super()._drain_market()
