from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Iterable, Mapping
from types import MappingProxyType
from typing import ClassVar

from crocodile.core.ingest.transport import AiohttpWsTransport, Transport
from crocodile.core.schema.enums import AssetClass, SecurityType, Side
from crocodile.core.schema.records import Record, Trade
from crocodile.core.sink.base import Sink
from crocodile.core.util.time import ms_to_ns
from crocodile.equity.providers.base import US_EQUITY_MICS, FatalProviderError, Provider
from crocodile.equity.reference.identity import InstrumentIdentity
from crocodile.equity.reference.registry import InstrumentRegistry

log = logging.getLogger(__name__)

# Re-export for callers that import from this module
__all__ = ["FatalProviderError", "FinnhubProvider"]


class RedactedString(str):
    def __repr__(self) -> str:
        import re

        return re.sub(r"token=[a-zA-Z0-9_\-]+", "token=REDACTED", super().__repr__())

    def __str__(self) -> str:
        import re

        return re.sub(r"token=[a-zA-Z0-9_\-]+", "token=REDACTED", super().__str__())


class FinnhubWsTransport(AiohttpWsTransport):
    def _redact_exception(self, exc: Exception) -> Exception:
        import re

        if hasattr(exc, "args") and exc.args:
            new_args = tuple(
                re.sub(r"token=[a-zA-Z0-9_\-]+", "token=REDACTED", str(arg))
                if isinstance(arg, str)
                else arg
                for arg in exc.args
            )
            try:
                exc.args = new_args
            except Exception:
                pass
        return exc

    async def connect(self) -> None:
        try:
            await super().connect()
        except Exception as exc:
            raise self._redact_exception(exc) from None

    async def _iter(self) -> AsyncIterator[bytes]:
        try:
            async for frame in super()._iter():
                yield frame
        except Exception as exc:
            raise self._redact_exception(exc) from None

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iter()

    async def send(self, data: bytes) -> None:
        try:
            await super().send(data)
        except Exception as exc:
            raise self._redact_exception(exc) from None

    async def close(self) -> None:
        try:
            await super().close()
        except Exception as exc:
            raise self._redact_exception(exc) from None


class FinnhubProvider(Provider):
    name = "finnhub"
    rest_url = "https://finnhub.io/api/v1"

    supported_channels: ClassVar[frozenset[str]] = frozenset({"trade"})
    """Trade prints, and on every tier.

    ``_subscribe`` widens its own local set to ``{"quote", "forex", "crypto"}`` off the free
    tier, and that is a statement about which *subscriptions* the endpoint accepts rather
    than about which records arrive here. The subscribe frame this connector sends is
    ``{"type": "subscribe", "symbol": ...}`` on every tier — there is no separate BBO stream
    to ask for — and :meth:`normalize` logs and drops ``quote`` frames rather than mapping
    them onto a ``Trade``. So a paid deployment that asked for ``quote`` got a connection
    that stayed up and wrote nothing, which is the dead channel this declaration exists to
    make visible before the socket opens.
    """

    supported_mics: ClassVar[frozenset[str]] = US_EQUITY_MICS
    """The trade stream as configured here, US symbols."""

    unservable_channels: ClassVar[Mapping[str, str]] = MappingProxyType(
        {
            "quote": (
                "the paid tier accepts a quote subscription, but this connector's subscribe "
                "frame carries no stream selector and `normalize` drops quote frames rather "
                "than inventing a Trade from one. Use alpaca for an equity top of book."
            )
        }
    )

    def __init__(
        self,
        symbols: list[str],
        channels: list[str],
        out: Sink,
        registry: InstrumentRegistry,
        token: str | None = None,
        is_free_tier: bool | None = None,
    ) -> None:
        if is_free_tier is None:
            env_val = os.environ.get("FINNHUB_FREE_TIER", "true").lower()
            is_free_tier = env_val in ("true", "1", "yes")
        self.is_free_tier = is_free_tier

        if self.is_free_tier and len(symbols) > 50:
            raise ValueError("Finnhub free tier WebSocket limits subscriptions to 50 symbols.")

        super().__init__(symbols, channels, out, registry)
        self.token = token or os.environ.get("FINNHUB_API_KEY")
        if not self.token:
            raise ValueError("Finnhub API key missing. Set FINNHUB_API_KEY.")
        self.ws_url = RedactedString(f"wss://ws.finnhub.io?token={self.token}")
        self.transport = FinnhubWsTransport(self.ws_url)
        self._running = False

    async def list_instruments(self) -> list[InstrumentIdentity]:
        insts = []
        for sym in self.symbols:
            sec_type = SecurityType.CS
            if ":" in sym or "/" in sym:
                sec_type = SecurityType.UNKNOWN
            elif len(sym) >= 15 and sym[-8:].isdigit() and any(c in sym for c in ("C", "P")):
                sec_type = SecurityType.UNKNOWN
            elif self.registry is not None:
                try:
                    inst = self.registry.get_raw(self.name, sym)
                    if inst is not None:
                        sec_type = inst.security_type
                    elif self.registry.security_master is not None:
                        sec = self.registry.security_master.get_by_symbol(sym)
                        if sec is None:
                            resolved = self.registry.security_master.resolve_ticker(sym)
                            if resolved is not None:
                                sec = self.registry.security_master.get_by_symbol(resolved)
                        if sec is not None and sec.security_type is not None:
                            sec_type = sec.security_type
                except Exception:
                    pass

            insts.append(
                InstrumentIdentity(
                    symbol=sym,
                    source=self.name,
                    symbol_raw=sym,
                    security_type=sec_type,
                )
            )
        return insts

    async def _subscribe(self, transport: Transport) -> None:
        supported_channels = {"trade"}
        if not self.is_free_tier:
            supported_channels.update({"quote", "forex", "crypto"})

        unsupported_channels = [ch for ch in self.channels if ch not in supported_channels]
        if unsupported_channels:
            tier_name = "free" if self.is_free_tier else "paid"
            log.warning(
                "Finnhub %s WebSocket does not support channels: %s. Ignoring.",
                tier_name,
                unsupported_channels,
            )

        active_channels = [ch for ch in self.channels if ch in supported_channels]
        if not active_channels:
            raise ValueError(
                f"Finnhub has no supported channels among {self.channels!r} "
                f"(supported: {sorted(supported_channels)})"
            )
        # Free Finnhub WS is trade-only; paid "quote" is not a separate BBO stream
        # in the free subscribe protocol — only trade subscriptions are sent.
        for sym in self.symbols:
            sub_msg = {"type": "subscribe", "symbol": sym}
            await transport.send(json.dumps(sub_msg).encode())

    def normalize(self, msg: object, local_ts: int) -> Iterable[Record]:
        if not isinstance(msg, dict):
            return
        msg_type = msg.get("type")
        if msg_type == "error":
            err_msg = msg.get("msg")
            log.error("Finnhub WebSocket error: %s", err_msg)
            # Protocol-level failures are fatal (no silent DLQ-continue)
            if "token" in str(err_msg).lower() or "auth" in str(err_msg).lower():
                raise FatalProviderError(f"Finnhub authentication failed: {err_msg}")
            raise FatalProviderError(f"Finnhub WebSocket error: {err_msg}")

        # Free WS trade prints only. Do not map "quote" frames to Trade.
        if msg_type == "trade":
            data = msg.get("data")
            if not isinstance(data, list):
                return
            for item in data:
                if not isinstance(item, dict):
                    continue
                try:
                    source_ts = ms_to_ns(item["t"]) if "t" in item else None
                    yield Trade(
                        source=self.name,
                        symbol=item["s"],
                        symbol_raw=item["s"],
                        source_ts=source_ts,
                        local_ts=local_ts,
                        id="",
                        price=float(item["p"]),
                        amount=float(item["v"]),
                        venue=None,
                        conditions=item.get("c"),
                        tape=None,
                        asset_class=AssetClass.EQUITY,
                        side=Side.UNKNOWN,
                    )
                except Exception as exc:
                    log.error("Finnhub normalize item error: %s (item: %s)", exc, item)
        elif msg_type == "quote":
            log.debug("Ignoring Finnhub quote frame (trade stream only): %s", msg)

    async def run(self, max_reconnects: int = -1) -> None:
        if self._running:
            raise RuntimeError("FinnhubProvider is already running (connection limit: 1).")
        self._running = True
        try:
            if isinstance(self.transport, AiohttpWsTransport):
                self.transport = FinnhubWsTransport(self.ws_url)
            await super().run(max_reconnects)
        finally:
            self._running = False
