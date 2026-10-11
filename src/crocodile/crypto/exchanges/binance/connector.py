"""Binance WebSocket connector — spot and USD-M futures.

Appendix §3.2 + §7:
- Spot WS: ``wss://stream.binance.com:9443/stream``
- USD-M futures WS: ``wss://fstream.binance.com/stream``
- Combined-stream URL format: ``<base>?streams=<topic1>/<topic2>/...``
- Subscribe frame: ``{"method": "SUBSCRIBE", "params": [...topics], "id": <int>}``
- Topics use lowercase symbol + ``@<streamName>``, e.g. ``btcusdt@aggTrade``.

Book path (sequence-gap resync)
--------------------------------
When ``book_delta`` / ``book_snapshot`` channels are subscribed, depth diffs
are gated through :class:`~crocodile.crypto.exchanges.binance.book.OrderBookSync`
and :class:`~crocodile.core.ingest.gap_bridge.BookResyncBridge`:

1. First depth for a symbol → REST ``/depth`` snapshot seeds the sync machine.
2. Continuous ``depthUpdate`` events → APPLY / DROP via ``OrderBookSync``.
3. Sequence gap (``SyncResult.RESYNC``) → buffer live deltas, re-fetch REST
   snapshot, emit snapshot + post-snapshot buffered deltas.

Override :meth:`~crocodile.core.connector.Connector._handle_message` is the
integration point; non-depth messages still use the pure normalizer path.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any, Literal

from crocodile.core.capture.classification import FrameClass
from crocodile.core.capture.schema import RawFrame
from crocodile.core.capture.snapshots import HttpSnapshotProvider, SnapshotProvider
from crocodile.core.connector import Connector
from crocodile.core.errors import ConfigError, TransientConnectorError
from crocodile.core.ingest.gap_bridge import BookResyncBridge
from crocodile.core.ingest.transport import AiohttpWsTransport, Transport
from crocodile.core.schema.records import BookDelta, BookSnapshot, Record
from crocodile.core.sink.base import Sink
from crocodile.core.util.time import now_ns
from crocodile.crypto.exchanges.binance.book import (
    OrderBookSync,
    SyncResult,
    normalize_depth,
    parse_rest_depth_snapshot,
)
from crocodile.crypto.instruments.registry import Instrument, InstrumentRegistry, Kind

from .book_session import BinanceBookSession, json_depth
from .normalize import normalize_message
from .normalize_sbe import normalize_sbe_message, to_depth_event
from .sbe import (
    SbeBestBidAsk,
    SbeDepthDiff,
    SbeDepthSnapshot,
    SbeProtocolError,
    SbeTrades,
    decode_sbe_frame,
)

log = logging.getLogger(__name__)

EXCHANGE = "binance"

# WS base URLs per market
_MARKET_WS_URL: dict[str, str] = {
    "spot": "wss://stream.binance.com:9443/stream",
    "usdm": "wss://fstream.binance.com/stream",
    "coinm": "wss://dstream.binance.com/stream",
}

# REST base URLs per market
_MARKET_REST_URL: dict[str, str] = {
    "spot": "https://api.binance.com",
    "usdm": "https://fapi.binance.com",
    "coinm": "https://dapi.binance.com",
}

# Mapping from canonical channel names to Binance stream topic patterns.
# ``{sym}`` is substituted with the lowercase symbol.
# Deduplication is applied in build_channels: topics that share the same wire
# stream (e.g. derivative_ticker + funding both use @markPrice) are collapsed.
_CHANNEL_MAP: dict[str, str] = {
    "trade": "{sym}@aggTrade",
    "book_ticker": "{sym}@bookTicker",
    "book_delta": "{sym}@depth",
    "book_snapshot": "{sym}@depth",
    "derivative_ticker": "{sym}@markPrice",
    "funding": "{sym}@markPrice",  # markPrice carries funding rate
    "liquidation": "{sym}@forceOrder",
    "options_chain": "{sym}@optionMarkPrice",
}


def build_channels(
    symbols: list[str],
    channels: list[str],
    market: str = "spot",
) -> list[str]:
    """Return Binance combined-stream topic strings for the given symbols and channel kinds.

    Topics use lowercase symbol names (Binance convention), e.g.
    ``btcusdt@aggTrade``, ``btcusdt@bookTicker``.

    Deduplicates: multiple canonical channels that map to the same Binance topic
    (e.g. ``derivative_ticker`` + ``funding`` → ``@markPrice``) are collapsed to
    a single entry per symbol.

    The ``market`` parameter selects the stream-name convention; for spot and
    USD-M futures the topic patterns are identical (the URL determines the
    market), so this parameter is accepted but not used to alter topic names.
    It is kept for symmetry with Bybit's ``category`` kwarg.
    """
    seen: set[str] = set()
    result: list[str] = []
    for sym in symbols:
        sym_lower = sym.lower()
        for ch in channels:
            pattern = _CHANNEL_MAP.get(ch)
            if pattern is None:
                continue
            topic = pattern.format(sym=sym_lower)
            if topic not in seen:
                seen.add(topic)
                result.append(topic)
    return result


def build_sbe_channels(symbols: list[str], channels: list[str]) -> list[str]:
    patterns = {
        "trade": "trade",
        "book_ticker": "bestBidAsk",
        "book_delta": "depth",
        "book_snapshot": "depth",
    }
    if not symbols or not channels or any(ch not in patterns for ch in channels):
        raise ConfigError("SBE supports trade, book_ticker, book_delta and book_snapshot channels")
    if any(not s.isalnum() for s in symbols):
        raise ConfigError("SBE requires native Spot symbols such as BTCUSDT")
    return list(dict.fromkeys(f"{s.lower()}@{patterns[ch]}" for s in symbols for ch in channels))


class BinanceConnector(Connector):
    """Binance combined-stream WebSocket connector.

    Supports ``spot`` (api.binance.com), ``usdm`` (fapi.binance.com, USD-M
    futures), and ``coinm`` (dapi.binance.com, COIN-M futures).  Defaults to
    ``spot``.

    Subscribe frame format::

        {"method": "SUBSCRIBE", "params": ["btcusdt@aggTrade", ...], "id": 1}

    The connector reuses :func:`~.normalize.normalize_message` from the existing
    binance normalizer, passing the ``venue`` as ``"binance-<market>"`` so that
    downstream code can distinguish spot from futures records.

    That market-tagged venue is the connector's whole instrument identity, not
    just a label on the records: it is the ``source`` every record carries, the
    prefix of the fallback canonical the normalizer builds when the registry
    misses (``f"{venue}:{raw}"``), and the key
    :meth:`~crocodile.crypto.instruments.registry.InstrumentRegistry.get_raw`
    is called with on every message.  So :meth:`list_instruments` must register
    under it too.  It registered under the bare ``"binance"`` instead, and since
    the registry keys on ``(Instrument.exchange, Instrument.symbol_raw)`` every
    live lookup missed: prices still flowed on the fallback canonical, but no
    tick size, base/quote or :class:`~...registry.Kind` ever reached a consumer,
    and the same stream's canonical flipped between ``binance:BTCUSDT`` and
    ``binance-spot:BTCUSDT`` depending on whether the registry had loaded.
    ``"binance"`` is also not a unique key here — spot ``BTCUSDT`` and USD-M
    ``BTCUSDT`` are different instruments with different tick sizes, and one
    shared registry would have collapsed them onto each other.
    """

    name = EXCHANGE
    rest_url = _MARKET_REST_URL["spot"]  # overridden per-instance in __init__

    def __init__(
        self,
        symbols: list[str],
        channels: list[str],
        out: Sink,
        registry: InstrumentRegistry,
        market: str = "spot",
        *,
        wire_format: Literal["json", "sbe"] = "json",
        api_key: str | None = None,
        snapshot_provider: SnapshotProvider | None = None,
    ) -> None:
        if market not in _MARKET_WS_URL or wire_format not in {"json", "sbe"}:
            raise ConfigError("Invalid Binance market or wire format")
        if wire_format == "sbe" and (market != "spot" or not api_key or not api_key.strip()):
            raise ConfigError("Spot SBE requires spot market and CROCODILE_BINANCE_SBE_API_KEY")
        super().__init__(symbols=symbols, channels=channels, out=out, registry=registry)
        self.wire_format, self._api_key = wire_format, api_key
        self.decoder_id = "binance-sbe" if wire_format == "sbe" else "binance-json"
        self.capture_schema_id = 1 if wire_format == "sbe" else None
        self.capture_schema_version = 0 if wire_format == "sbe" else None
        self.capture_endpoint_id = f"binance-{market}-{wire_format}-ws"
        self.market = market
        self.ws_url = _MARKET_WS_URL.get(market, _MARKET_WS_URL["spot"])
        self.rest_url = _MARKET_REST_URL.get(market, _MARKET_REST_URL["spot"])
        self._sub_topics = build_channels(symbols, channels, market=market)
        if wire_format == "sbe":
            self.ws_url = "wss://stream-sbe.binance.com/ws"
            self._sub_topics = build_sbe_channels(symbols, channels)
        # venue tag forwarded to normalize_message so records carry the right exchange label
        self._venue = f"binance-{market}"
        # Per-symbol book sync + resync bridge (lazy, only for book channels).
        self._book_syncs: dict[str, OrderBookSync] = {}
        self._book_bridges: dict[str, BookResyncBridge] = {}
        # limit for REST depth snapshots (Binance max 5000; 1000 is standard).
        self.book_depth_limit: int = 1000
        self.snapshot_provider = snapshot_provider
        self._book_session: BinanceBookSession | None = None

    async def _prepare_connection(self) -> None:
        if self.capture is not None or self.wire_format == "sbe":
            self._book_session = BinanceBookSession(
                self._sync_venue(),
                self.snapshot_provider
                or HttpSnapshotProvider(self.http_session, self.capture, base_url=self.rest_url),
                self.out,
                self.registry,
                self.capture,
                source=self._venue,
                stream_id=self._stream_id() if self.capture is not None else "s1",
                path=self._depth_rest_path(),
                depth_limit=self.book_depth_limit,
            )

    async def _drain_market(self) -> None:
        if self._book_session is not None:
            await self._book_session.drain()

    async def _watch_market_failure(self) -> None:
        if self._book_session is not None:
            await self._book_session.wait_failure()
        else:
            await super()._watch_market_failure()

    async def _on_connection_close(self) -> None:
        if self._book_session is not None:
            await self._book_session.reset("disconnect")
            self._book_session = None

    def normalize(self, msg: object, local_ts: int) -> Iterable[Record]:
        if isinstance(msg, (SbeTrades, SbeBestBidAsk, SbeDepthSnapshot, SbeDepthDiff)):
            yield from normalize_sbe_message(msg, local_ts=local_ts, registry=self.registry)
            return
        if isinstance(msg, dict):
            yield from normalize_message(
                msg,
                local_ts=local_ts,
                venue=self._venue,
                registry=self.registry,
            )

    def subscribe_channels(self) -> list[str]:
        """Return the list of Binance stream topic strings."""
        return self._sub_topics

    def make_transport(self) -> Transport:
        if self.wire_format == "sbe":
            assert self._api_key is not None
            return AiohttpWsTransport(self.ws_url, headers={"X-MBX-APIKEY": self._api_key})
        return super().make_transport()

    def _decode_frame(self, frame: RawFrame) -> object:
        if self.wire_format == "sbe":
            return decode_sbe_frame(frame.payload)
        return super()._decode_frame(frame)

    def _classify_frame(self, frame: RawFrame, msg: object | None) -> FrameClass:
        if self.wire_format == "sbe":
            if frame.frame_kind == "binary" or (frame.frame_kind == "unknown" and msg is None):
                return "market"
            return "control" if isinstance(msg, dict) else "unknown"
        return super()._classify_frame(frame, msg)

    async def _handle_control_message(self, msg: object, frame: RawFrame) -> bool:
        if isinstance(msg, dict) and msg.get("e") == "serverShutdown":
            raise TransientConnectorError("Binance requested connection renewal")
        if self.wire_format == "sbe":
            if isinstance(msg, dict) and (
                msg.get("error") is not None or ("code" in msg and "msg" in msg)
            ):
                raise SbeProtocolError("Binance SBE subscription/control rejected")
            return True
        return await super()._handle_control_message(msg, frame)

    # ------------------------------------------------------------------
    # Book resync integration (BookResyncBridge)
    # ------------------------------------------------------------------

    def _wants_book_sync(self) -> bool:
        """True when book channels are requested (enable gap → REST resync)."""
        return any(c in ("book_delta", "book_snapshot") for c in self.channels)

    @staticmethod
    def _is_depth_message(msg: dict[str, Any]) -> bool:
        stream = msg.get("stream", "") or ""
        if "@depth" in stream:
            return True
        data = msg.get("data", msg)
        return isinstance(data, dict) and data.get("e") == "depthUpdate"

    def _sync_venue(self) -> Literal["spot", "futures"]:
        """Map connector market → OrderBookSync venue ('spot' | 'futures')."""
        return "futures" if self.market in ("usdm", "coinm") else "spot"

    def _depth_rest_path(self) -> str:
        if self.market == "usdm":
            return "/fapi/v1/depth"
        if self.market == "coinm":
            return "/dapi/v1/depth"
        return "/api/v3/depth"

    async def fetch_book_snapshot(self, symbol: str) -> BookSnapshot:
        """Fetch a REST order-book snapshot for *symbol* (raw exchange symbol).

        Used as the ``fetch_snapshot`` callback for
        :class:`~crocodile.core.ingest.gap_bridge.BookResyncBridge`.  Tests may
        monkeypatch this method to avoid network I/O.
        """
        url = f"{self.rest_url}{self._depth_rest_path()}"
        data = await self.http_get(
            url,
            params={"symbol": symbol.upper(), "limit": self.book_depth_limit},
        )
        return parse_rest_depth_snapshot(
            data,
            symbol_raw=symbol.upper(),
            venue=self._venue,
            local_ts=now_ns(),
            registry=self.registry,
        )

    async def _ensure_book_bridge(self, symbol_raw: str) -> tuple[OrderBookSync, BookResyncBridge]:
        """Return (create if needed) OrderBookSync + BookResyncBridge for *symbol_raw*.

        On first use: REST-fetch a snapshot, emit it, and seed the sync machine.
        Bridges are registered only after a successful bootstrap with a usable
        ``sequence_id`` so a failed fetch does not permanently DROP the symbol.
        """
        key = symbol_raw.upper()
        if key not in self._book_bridges:
            sync = OrderBookSync(venue=self._sync_venue())
            bridge = BookResyncBridge(
                sync=sync,
                fetch_snapshot=self.fetch_book_snapshot,
                symbol=key,
            )

            # Bootstrap anchor so the first depth diffs can APPLY.
            # Register only after success — a failed fetch must leave the key
            # absent so the next depth message retries bootstrap.
            snap = await self.fetch_book_snapshot(key)
            if snap.sequence_id is None:
                raise ValueError(f"Binance book bootstrap [{key}]: snapshot missing sequence_id")
            sync.set_snapshot(last_update_id=snap.sequence_id)
            await self.out.put(snap)
            self._book_syncs[key] = sync
            self._book_bridges[key] = bridge
            log.info(
                "Binance book bootstrap [%s]: snapshot seq=%s",
                key,
                snap.sequence_id,
            )
        return self._book_syncs[key], self._book_bridges[key]

    async def _handle_depth_message(self, msg: dict[str, Any], local_ts: int) -> None:
        """Normalize one depthUpdate, gate via OrderBookSync + BookResyncBridge."""
        data: dict[str, Any] = msg.get("data", msg)
        symbol_raw: str = data["s"]
        sync, bridge = await self._ensure_book_bridge(symbol_raw)

        U = int(data["U"])
        u = int(data["u"])
        pu = data.get("pu")
        pu_int: int | None = int(pu) if pu is not None else None

        for rec in normalize_depth(
            msg, local_ts=local_ts, venue=self._venue, registry=self.registry
        ):
            if not isinstance(rec, BookDelta):
                await self.out.put(rec)
                continue

            result = sync.feed(U=U, u=u, pu=pu_int)
            emit = bridge.feed_sync_result(result, rec)
            if emit is not None:
                await self.out.put(emit)

            if bridge.is_resyncing:
                # This compatibility path keeps fetch_book_snapshot injection;
                # range validation uses the original U/u, never only seq_id.
                snap = await self.fetch_book_snapshot(symbol_raw)
                if snap.sequence_id is None:
                    raise ValueError("Binance resync snapshot missing sequence_id")
                sync.set_snapshot(snap.sequence_id)
                await self.out.put(snap)
                self._book_bridges[symbol_raw.upper()] = BookResyncBridge(
                    sync=sync, fetch_snapshot=self.fetch_book_snapshot, symbol=symbol_raw.upper()
                )
                if sync.feed(U=U, u=u, pu=pu_int) == SyncResult.APPLY:
                    await self.out.put(rec)

    async def _handle_message(self, msg: object, local_ts: int) -> None:
        """Route depth updates through BookResyncBridge; all else default path.

        Integration point documented on
        :meth:`crocodile.core.connector.Connector._handle_message`.
        """
        if isinstance(msg, SbeDepthSnapshot):
            raise SbeProtocolError("Unexpected partial depth snapshot on full-depth session")
        if isinstance(msg, SbeDepthDiff):
            if self._book_session is None:
                raise SbeProtocolError("SBE depth session has no snapshot provider")
            await self._book_session.accept_depth(
                to_depth_event(msg), origin_ordinal=self._origin_ordinal, local_ts=local_ts
            )
            return
        if isinstance(msg, dict) and self._wants_book_sync() and self._is_depth_message(msg):
            if self._book_session is not None:
                await self._book_session.accept_depth(
                    json_depth(msg), origin_ordinal=self._origin_ordinal, local_ts=local_ts
                )
                return
            await self._handle_depth_message(msg, local_ts)
            return
        await super()._handle_message(msg, local_ts)

    async def _subscribe(self, transport: Transport) -> None:  # pragma: no cover
        """Send a Binance SUBSCRIBE frame over the transport."""
        # Drop book state on (re)subscribe so a reconnect re-bootstraps REST.
        self._book_syncs.clear()
        self._book_bridges.clear()
        topics = self.subscribe_channels()
        if topics:
            frame = json.dumps({"method": "SUBSCRIBE", "params": topics, "id": 1}).encode()
            await transport.send(frame)

    async def list_instruments(self) -> list[Instrument]:  # pragma: no cover
        """Fetch exchange info from Binance REST and return instruments.

        Uses ``GET /api/v3/exchangeInfo`` for spot or
        ``GET /fapi/v1/exchangeInfo`` for USD-M futures.

        ``exchange`` and the canonical prefix are :attr:`_venue`, not the bare
        ``EXCHANGE`` constant, so that what is registered is what the live path
        looks up — see the class docstring.
        """
        if self.market == "spot":
            url = f"{self.rest_url}/api/v3/exchangeInfo"
        elif self.market == "usdm":
            url = f"{self.rest_url}/fapi/v1/exchangeInfo"
        else:
            url = f"{self.rest_url}/dapi/v1/exchangeInfo"

        data = await self.http_get(url)

        instruments: list[Instrument] = []
        for item in data.get("symbols", []):
            sym: str = item.get("symbol", "")
            base: str = item.get("baseAsset", "")
            quote: str = item.get("quoteAsset", "")
            status: str = item.get("status", "")
            if status not in ("TRADING", "PRE_DELIVERING", ""):
                continue

            contract_type: str = item.get("contractType", "")
            if self.market == "spot":
                kind = Kind.SPOT
            elif contract_type == "PERPETUAL":
                kind = Kind.PERPETUAL
            elif contract_type in (
                "CURRENT_QUARTER",
                "NEXT_QUARTER",
                "CURRENT_MONTH",
                "NEXT_MONTH",
            ):
                kind = Kind.FUTURE
            else:
                kind = Kind.SPOT

            # tick_size from filters
            tick_size: float | None = None
            for f in item.get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    ts_raw = f.get("tickSize")
                    if ts_raw:
                        try:
                            tick_size = float(ts_raw)
                        except (ValueError, TypeError):
                            pass
                    break

            instruments.append(
                Instrument(
                    canonical=f"{self._venue}:{sym}",
                    exchange=self._venue,
                    symbol_raw=sym,
                    kind=kind,
                    base=base,
                    quote=quote,
                    tick_size=tick_size,
                )
            )
        return instruments
