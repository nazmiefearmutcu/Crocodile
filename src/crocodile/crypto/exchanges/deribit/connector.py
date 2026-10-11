"""Deribit connector — wiring (REST instruments + WS subscribe build)."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterable
from typing import Any, Literal

from crocodile.core.capture.schema import RawFrame
from crocodile.core.connector import Connector
from crocodile.core.errors import ConfigError, TransientConnectorError
from crocodile.core.ingest.transport import Transport
from crocodile.core.schema.records import Record
from crocodile.core.sink.base import Sink
from crocodile.core.util.time import ms_to_ns
from crocodile.crypto.instruments.registry import Instrument, InstrumentRegistry, Kind

from .book import DeribitBookGate, DeribitProtocolError, validate_book
from .normalize import normalize_message
from .session import DeribitSession, SessionState

log = logging.getLogger(__name__)

EXCHANGE = "deribit"
REST_BASE = "https://www.deribit.com/api/v2"

# Unauthorized-safe throttled interval; .raw channels require authentication and
# ticker.{sym} without an interval is silently accepted but streams nothing.
_WS_INTERVAL = "100ms"

# Mapping from canonical channel names used by callers to Deribit WS channel patterns.
_CHANNEL_MAP: dict[str, str] = {
    "trade": "trades.{sym}." + _WS_INTERVAL,
    "book_delta": "book.{sym}." + _WS_INTERVAL,
    "book_snapshot": "book.{sym}." + _WS_INTERVAL,
    "derivative_ticker": "ticker.{sym}." + _WS_INTERVAL,
    "options_chain": "ticker.{sym}." + _WS_INTERVAL,
    "funding": "ticker.{sym}." + _WS_INTERVAL,
}


def build_channels(
    symbols: list[str], channels: list[str], *, interval: str = "100ms"
) -> list[str]:
    """Return the list of Deribit WS channel strings for the given symbols and channel kinds.

    Deduplicates: if both 'book_delta' and 'book_snapshot' appear they map to the same
    wire channel and are collapsed.
    """
    result: set[str] = set()
    for sym in symbols:
        for ch in channels:
            pattern = _CHANNEL_MAP.get(ch)
            if pattern is not None:
                result.add(pattern.format(sym=sym).removesuffix(_WS_INTERVAL) + interval)
    return sorted(result)


def parse_instruments(raw: dict[str, Any]) -> list[Instrument]:
    """Parse the JSON response from Deribit public/get_instruments."""
    out: list[Instrument] = []
    for item in raw.get("result", []):
        name: str = item["instrument_name"]
        kind_str: str = item.get("kind", "future")
        base: str = item.get("base_currency", "")
        quote: str = item.get("quote_currency", "USD")

        if kind_str == "spot":
            kind = Kind.SPOT
        elif kind_str == "future" and "PERPETUAL" in name.upper():
            kind = Kind.PERPETUAL
        elif kind_str == "future":
            kind = Kind.FUTURE
        elif kind_str == "option":
            kind = Kind.OPTION
        else:
            kind = Kind.FUTURE

        # Option-specific fields
        strike: float | None = item.get("strike")
        expiration_timestamp: int | None = item.get("expiration_timestamp")
        expiry_ns: int | None = (
            ms_to_ns(expiration_timestamp) if expiration_timestamp is not None else None
        )

        option_type_raw: str | None = item.get("option_type")
        opt_type: str | None = None
        if option_type_raw is not None:
            lowered = option_type_raw.lower()
            if lowered == "call":
                opt_type = "C"
            elif lowered == "put":
                opt_type = "P"
            else:
                log.warning(
                    "parse_instruments: unexpected option_type %r for %s; skipping instrument",
                    option_type_raw,
                    name,
                )
                continue

        tick_size: float | None = item.get("tick_size")
        contract_size: float | None = item.get("contract_size")
        settlement_currency: str | None = item.get("settlement_currency")

        out.append(
            Instrument(
                canonical=f"{EXCHANGE}:{name}",
                exchange=EXCHANGE,
                symbol_raw=name,
                kind=kind,
                base=base,
                quote=quote,
                strike=strike,
                expiry=expiry_ns,
                opt_type=opt_type,
                tick_size=tick_size,
                contract_size=contract_size,
                settlement_currency=settlement_currency,
            )
        )
    return out


class DeribitConnector(Connector):
    """Deribit WS connector."""

    name = EXCHANGE
    ws_url = "wss://www.deribit.com/ws/api/v2"
    rest_url = REST_BASE

    def __init__(
        self,
        symbols: list[str],
        channels: list[str],
        out: Sink,
        registry: InstrumentRegistry,
        *,
        interval: Literal["100ms", "raw"] = "100ms",
        client_id: str | None = None,
        client_secret: str | None = None,
        request_timeout_seconds: float = 10.0,
        heartbeat_interval_seconds: int = 30,
        refresh_margin_seconds: float = 30.0,
    ) -> None:
        if interval not in ("100ms", "raw"):
            raise ConfigError("Deribit interval expects 100ms or raw")
        if interval == "raw" and not (
            client_id and client_id.strip() and client_secret and client_secret.strip()
        ):
            raise ConfigError("Deribit raw requires CROCODILE_DERIBIT_CLIENT_ID and CLIENT_SECRET")
        if heartbeat_interval_seconds < 10:
            raise ConfigError("Deribit heartbeat interval must be at least 10 seconds")
        if not math.isfinite(request_timeout_seconds) or request_timeout_seconds <= 0:
            raise ConfigError("Deribit request timeout must be finite and positive")
        if not math.isfinite(refresh_margin_seconds) or refresh_margin_seconds < 0:
            raise ConfigError("Deribit refresh margin must be finite and nonnegative")
        super().__init__(symbols=symbols, channels=channels, out=out, registry=registry)
        self.interval = interval
        self._client_id, self._client_secret = client_id, client_secret
        self._request_timeout_seconds = request_timeout_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._refresh_margin_seconds = refresh_margin_seconds
        self._sub_channels = build_channels(symbols, channels, interval=interval)
        self.capture_endpoint_id = f"deribit-{interval}-ws"
        self.capture_decoder_id = "deribit-json"
        self.capture_decoder_version = 1
        self._control = DeribitSession(
            interval=interval,
            client_id=client_id,
            client_secret=client_secret,
            channels=tuple(self._sub_channels),
            request_timeout_seconds=request_timeout_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            refresh_margin_seconds=refresh_margin_seconds,
        )
        self._session_enabled = self._mixed_controls = interval == "raw"
        self._book = DeribitBookGate()

    def normalize(self, msg: object, local_ts: int) -> Iterable[Record]:
        if isinstance(msg, dict):
            yield from normalize_message(msg, local_ts=local_ts, registry=self.registry)

    async def list_instruments(self) -> list[Instrument]:  # pragma: no cover
        """Fetch instruments from Deribit REST API and parse them."""
        # Fetch all currencies x kinds (§3.1: BTC|ETH|SOL|USDC)
        instruments: list[Instrument] = []
        for currency in ("BTC", "ETH", "SOL", "USDC"):
            for kind in ("future", "option", "spot", "future_combo", "option_combo"):
                url = f"{REST_BASE}/public/get_instruments"
                params = {"currency": currency, "kind": kind, "expired": "false"}
                data = await self.http_get(url, params=params)
                instruments.extend(parse_instruments(data))
        return instruments

    def subscribe_channels(self) -> list[str]:
        """Return the list of Deribit channel strings this connector will subscribe to."""
        return self._sub_channels

    async def _subscribe(self, transport: Transport) -> None:  # pragma: no cover
        """Send a Deribit JSON-RPC 2.0 ``public/subscribe`` frame."""
        # Byte-only public adapters retain their existing fixture contract. Live
        # transports and captures always correlate subscription acknowledgements.
        self._session_enabled = (
            self.interval == "raw" or self.capture is not None or hasattr(transport, "iter_frames")
        )
        self._mixed_controls = self._session_enabled
        if self._session_enabled:
            await self._control.begin(transport)
            return
        channels = self.subscribe_channels()
        if channels:
            frame = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "public/subscribe",
                    "params": {"channels": channels},
                }
            ).encode()
            await transport.send(frame)

    async def _handle_control_message(self, msg: object, frame: RawFrame) -> bool:
        if self._session_enabled and isinstance(msg, dict):
            return await self._control.handle_control(msg)
        return False

    async def _prepare_connection(self) -> None:
        await self._control.close()
        self._book.reset()

    async def _on_connection_close(self) -> None:
        await self._control.close()

    def _market_ready(self, frame: RawFrame, msg: object | None) -> bool:
        if not self._session_enabled:
            return True
        if isinstance(msg, dict):
            params = msg.get("params")
            if isinstance(params, dict) and str(params.get("channel", "")).startswith("book."):
                data = params.get("data")
                if isinstance(data, dict) and isinstance(data.get("instrument_name"), str):
                    return self._control.ready_for_symbol(data["instrument_name"])
        return self._control.ready_for_market

    async def _handle_message(self, msg: object, local_ts: int) -> None:
        if self._session_enabled and isinstance(msg, dict):
            params = msg.get("params")
            if not isinstance(params, dict) or params.get("channel") not in self._sub_channels:
                raise DeribitProtocolError("Deribit notification channel was not subscribed")
            if not self._control.ready_for_market:
                await self._emit_records([], action="drop", reason="not_ready")
                return
            if isinstance(params, dict) and str(params.get("channel", "")).startswith("book."):
                data = params.get("data")
                if not isinstance(data, dict):
                    raise DeribitProtocolError("Deribit book payload is invalid")
                validate_book(data)
                if params["channel"] != f"book.{data['instrument_name']}.{self.interval}":
                    raise DeribitProtocolError("Deribit book instrument/channel mismatch")
                symbol = data.get("instrument_name")
                if not self._control.ready_for_symbol(symbol) or (
                    self._received_generation != self._control.generation_for_symbol(symbol)
                ):
                    await self._emit_records([], action="drop", reason="not_ready")
                    return
                normalized = list(self.normalize(msg, local_ts))
                action = self._book.evaluate(data)
                records = normalized if action in ("snapshot", "apply") else []
                await self._emit_records(records, action=action, reason="deribit_book")
                if action == "gap":
                    await self._control.resubscribe_symbol(data["instrument_name"])
                elif action == "snapshot":
                    self._control.state = SessionState.LIVE
                return
        await super()._handle_message(msg, local_ts)

    def _market_generation(self, msg: object | None) -> int | None:
        symbol = None
        if isinstance(msg, dict) and isinstance(params := msg.get("params"), dict):
            data = params.get("data")
            if str(params.get("channel", "")).startswith("book.") and isinstance(data, dict):
                symbol = data.get("instrument_name")
        return self._control.generation_for_symbol(symbol)

    async def _watch_market_failure(self) -> None:
        if self._session_enabled:
            await self._control._watchdog()
        else:
            await super()._watch_market_failure()

    async def _drain_market(self) -> None:
        if self._session_enabled and not self._control.ready_for_market:
            raise TransientConnectorError("Deribit connection ended before subscription was ready")
