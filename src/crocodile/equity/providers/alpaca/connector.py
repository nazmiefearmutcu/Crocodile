from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterable
from typing import Any, ClassVar

from crocodile.core.ingest.transport import AiohttpWsTransport, Transport
from crocodile.core.schema.enums import AssetClass, SecurityType, Side, Tape
from crocodile.core.schema.records import OHLCV, Quote, Record, Trade
from crocodile.core.sink.base import Sink
from crocodile.core.util.time import rfc3339_to_ns
from crocodile.equity.providers.base import US_EQUITY_MICS, FatalProviderError, Provider
from crocodile.equity.reference.identity import InstrumentIdentity
from crocodile.equity.reference.registry import InstrumentRegistry

log = logging.getLogger(__name__)

# Re-export for callers that import from this module
__all__ = ["AlpacaProvider", "FatalProviderError"]


class AlpacaProvider(Provider):
    name = "alpaca"
    ws_url = "wss://stream.data.alpaca.markets/v2/iex"
    rest_url = "https://data.alpaca.markets"

    supported_channels: ClassVar[frozenset[str]] = frozenset({"trade", "quote", "ohlcv"})
    """The three record types :meth:`normalize` builds, and nothing else.

    Declared — rather than left at ``None`` — because ``None`` used to be safe and no longer
    is. It meant "this connector has not been through the exercise", and
    :func:`~crocodile.equity.providers.factory.channels_for_provider` answered it with the
    whole vocabulary; that was harmless while the vocabulary was four market-data channels
    every provider plausibly served. The vocabulary is now the union of what the registered
    providers declare, and it includes ``options_chain``, ``macro_series``, ``insider`` and
    ``holding_13f`` — so an undeclared connector would be offered a menu containing channels
    it has never heard of. Every registered provider declares; the gate is in
    ``tests/conformance/test_provider_channels.py``.

    ``bar`` is not listed and is still accepted: it is the retired spelling of ``ohlcv`` and
    :func:`~crocodile.equity.providers.factory.channels_for_provider` widens for it. Listing
    both would put two names for one channel into the declaration that decides the menu.
    """

    supported_mics: ClassVar[frozenset[str]] = US_EQUITY_MICS
    """The IEX/SIP US consolidated feed."""

    def __init__(
        self,
        symbols: list[str],
        channels: list[str],
        out: Sink,
        registry: InstrumentRegistry,
        key: str | None = None,
        secret: str | None = None,
        feed: str | None = None,
    ) -> None:
        self.feed = feed or os.environ.get("ALPACA_FEED") or "iex"
        if self.feed not in ("iex", "sip"):
            raise ValueError("Alpaca feed must be 'iex' or 'sip'")

        # Respect symbol limit: Alpaca free tier permits max 30 symbols for trades + quotes
        has_capped_channels = any(ch in ["trade", "quote"] for ch in channels)
        if self.feed == "iex" and has_capped_channels and len(symbols) > 30:
            raise ValueError(
                "Alpaca basic plan WebSocket limits trade/quote subscriptions to 30 symbols."
            )

        super().__init__(symbols, channels, out, registry)
        self.key = key or os.environ.get("ALPACA_API_KEY")
        self.secret = secret or os.environ.get("ALPACA_API_SECRET")
        if not self.key or not self.secret:
            raise ValueError(
                "Alpaca API credentials missing. Set ALPACA_API_KEY and ALPACA_API_SECRET."
            )
        self.ws_url = f"wss://stream.data.alpaca.markets/v2/{self.feed}"
        self.transport = AiohttpWsTransport(self.ws_url)
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
        # Read the initial greeting message: [{"T":"success","msg":"connected"}]
        iterator = transport.__aiter__()
        try:
            raw = await asyncio.wait_for(iterator.__anext__(), timeout=10.0)
            msg = json.loads(raw)
            if isinstance(msg, list) and len(msg) > 0:
                first = msg[0]
                if not (first.get("T") == "success" and first.get("msg") == "connected"):
                    raise RuntimeError(f"Unexpected greeting from Alpaca: {raw.decode()}")
            else:
                raise RuntimeError(f"Unexpected greeting structure from Alpaca: {raw.decode()}")
        except TimeoutError as err:
            raise RuntimeError("Timeout waiting for Alpaca greeting handshake") from err
        except StopAsyncIteration as err:
            raise RuntimeError("Transport closed during connection handshake") from err

        # Authenticate
        auth_msg = {
            "action": "auth",
            "key": self.key,
            "secret": self.secret,
        }
        await transport.send(json.dumps(auth_msg).encode())

        # Await auth confirmation: [{"T":"success","msg":"authenticated"}]
        try:
            raw = await asyncio.wait_for(iterator.__anext__(), timeout=10.0)
            msg = json.loads(raw)
            if isinstance(msg, list) and len(msg) > 0:
                first = msg[0]
                if first.get("T") == "success" and first.get("msg") == "authenticated":
                    pass
                elif first.get("T") == "error":
                    err_msg = first.get("msg")
                    err_code = first.get("code")
                    err_l = str(err_msg).lower()
                    # 401/402 = auth; 406 = connection limit (not auth)
                    if err_code in (401, 402) or any(
                        w in err_l for w in ("auth", "credential", "key", "secret")
                    ):
                        raise FatalProviderError(
                            f"Alpaca authentication failed: {err_msg} (code {err_code})"
                        )
                    if err_code == 406 or "connection limit" in err_l or "limit" in err_l:
                        raise FatalProviderError(
                            f"Alpaca connection limit exceeded: {err_msg} (code {err_code})"
                        )
                    raise FatalProviderError(
                        f"Alpaca handshake error: {err_msg} (code {err_code})"
                    )
                else:
                    raise RuntimeError(f"Unexpected auth response: {raw.decode()}")
            else:
                raise RuntimeError(f"Unexpected auth response structure: {raw.decode()}")
        except TimeoutError as err:
            raise RuntimeError("Timeout waiting for Alpaca auth handshake") from err
        except StopAsyncIteration as err:
            raise RuntimeError("Transport closed during auth handshake") from err

        # Subscribe
        sub_msg: dict[str, Any] = {"action": "subscribe"}
        for ch in self.channels:
            if ch == "trade":
                sub_msg["trades"] = self.symbols
            elif ch == "quote":
                sub_msg["quotes"] = self.symbols
            # Both spellings map to Alpaca's `bars` stream. `bar` and `ohlcv` were two
            # tags for one struct and collapsed into `ohlcv`; subscribing with `ohlcv`
            # used to match no arm here and send no `bars` subscription at all — a
            # connector that connected, stayed connected and delivered nothing.
            elif ch in ("bar", "ohlcv"):
                sub_msg["bars"] = self.symbols

        await transport.send(json.dumps(sub_msg).encode())

    def normalize(self, msg: object, local_ts: int) -> Iterable[Record]:
        if not isinstance(msg, list):
            return
        for item in msg:
            if not isinstance(item, dict):
                continue
            try:
                t_type = item.get("T")
                if t_type == "error":
                    code = item.get("code")
                    err_msg = item.get("msg")
                    err_l = str(err_msg).lower()
                    if code in (401, 402) or any(
                        w in err_l for w in ("auth", "credential", "key", "secret")
                    ):
                        raise FatalProviderError(
                            f"Alpaca authentication failed: {err_msg} (code {code})"
                        )
                    if code == 406 or "connection limit" in err_l:
                        raise FatalProviderError(
                            f"Alpaca connection limit exceeded: {err_msg} (code {code})"
                        )
                    # Mid-session protocol errors are fatal (not DLQ noise)
                    raise FatalProviderError(f"Alpaca WebSocket error: {err_msg} (code {code})")

                elif t_type == "t":
                    tape_val = item.get("z")
                    try:
                        tape = Tape(tape_val) if tape_val else Tape.UNKNOWN
                    except ValueError:
                        tape = Tape.UNKNOWN

                    yield Trade(
                        source=self.name,
                        symbol=item["S"],
                        symbol_raw=item["S"],
                        source_ts=rfc3339_to_ns(item["t"]),
                        local_ts=local_ts,
                        id=str(item["i"]) if "i" in item else "",
                        price=float(item["p"]),
                        amount=float(item["s"]),
                        venue=item.get("x"),
                        conditions=item.get("c"),
                        tape=tape,
                        asset_class=AssetClass.EQUITY,
                        side=Side.UNKNOWN,
                    )
                elif t_type == "q":
                    tape_val = item.get("z")
                    try:
                        tape = Tape(tape_val) if tape_val else Tape.UNKNOWN
                    except ValueError:
                        tape = Tape.UNKNOWN

                    is_sip = getattr(self, "feed", "iex") == "sip"
                    yield Quote(
                        source=self.name,
                        symbol=item["S"],
                        symbol_raw=item["S"],
                        source_ts=rfc3339_to_ns(item["t"]),
                        local_ts=local_ts,
                        bid_px=float(item["bp"]),
                        bid_sz=float(item["bs"]),
                        ask_px=float(item["ap"]),
                        ask_sz=float(item["as"]),
                        is_nbbo=False,
                        is_consolidated=is_sip,
                        conditions=item.get("c"),
                        tape=tape,
                        asset_class=AssetClass.EQUITY,
                    )
                elif t_type == "b":
                    yield OHLCV(
                        source=self.name,
                        symbol=item["S"],
                        symbol_raw=item["S"],
                        source_ts=rfc3339_to_ns(item["t"]),
                        local_ts=local_ts,
                        interval="1m",
                        open=float(item["o"]),
                        high=float(item["h"]),
                        low=float(item["l"]),
                        close=float(item["c"]),
                        volume=float(item["v"]),
                        vwap=float(item["vw"]) if item.get("vw") is not None else None,
                        num_trades=int(item["n"]) if item.get("n") is not None else None,
                        asset_class=AssetClass.EQUITY,
                    )
            except (FatalProviderError, ValueError):
                raise
            except Exception as exc:
                log.error("Alpaca normalize item error: %s (item: %s)", exc, item)

    async def run(self, max_reconnects: int = -1) -> None:
        if self._running:
            raise RuntimeError("AlpacaProvider is already running (connection limit: 1).")
        self._running = True
        try:
            if isinstance(self.transport, AiohttpWsTransport):
                self.transport = AiohttpWsTransport(self.ws_url)
            await super().run(max_reconnects)
        finally:
            self._running = False
