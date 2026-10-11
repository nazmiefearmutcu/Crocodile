"""Unkeyed Bitfinex public R0/F0/250; native discovery and raw replay."""

from __future__ import annotations

import json
from typing import ClassVar

from crocodile.core.capture.classification import public_control_data
from crocodile.core.capture.schema import RawFrame
from crocodile.core.ingest.transport import Transport
from crocodile.core.sink.base import Sink
from crocodile.crypto.exchanges.public_books import GatedConnector, PublicBookError
from crocodile.crypto.instruments.registry import Instrument, InstrumentRegistry, Kind

from .book import BitfinexBookGate


class BitfinexConnector(GatedConnector):
    gate: BitfinexBookGate
    name = "bitfinex"
    market = "spot"
    ws_url = "wss://api-pub.bitfinex.com/ws/2"
    rest_url = "https://api-pub.bitfinex.com/v2"
    capture_endpoint_id = "bitfinex-r0-f0-250"
    decoder_id = "bitfinex-r0"
    quantity_unit = "base_asset"
    coverage: ClassVar[dict[str, object]] = {
        "kind": "visible_orders",
        "limit_per_side": 250,
        "checksum_orders_per_side": 25,
        "cadence": "realtime",
    }

    def __init__(
        self, symbols: list[str], channels: list[str], out: Sink, registry: InstrumentRegistry
    ) -> None:
        super().__init__(symbols, channels, out, registry)
        if any(not s.startswith("t") or not s[1:].replace(":", "").isalnum() for s in symbols):
            raise PublicBookError("Bitfinex requires exchange-native t-prefixed trading symbols")
        if any(c not in {"book_delta", "book_snapshot"} for c in channels):
            raise PublicBookError("Bitfinex native raw connector supports book channels")
        self.gate = BitfinexBookGate()

    def subscribe_channels(self) -> list[str]:
        return [f"book.R0.F0.250.{symbol}" for symbol in self.symbols]

    async def _subscribe(self, transport: Transport) -> None:
        await transport.send(json.dumps({"event": "conf", "flags": 229376}).encode())
        for symbol in self.symbols:
            await transport.send(
                json.dumps(
                    {
                        "event": "subscribe",
                        "channel": "book",
                        "symbol": symbol,
                        "prec": "R0",
                        "freq": "F0",
                        "len": "250",
                    }
                ).encode()
            )

    async def _handle_control_message(self, msg: object, frame: RawFrame) -> bool:
        if isinstance(msg, dict):
            if msg.get("event") == "error" or msg.get("status") == "ERROR":
                raise PublicBookError("Bitfinex subscription rejected")
            if msg.get("event") == "subscribed":
                mapping = public_control_data(self.name, msg)
                if mapping is None or mapping["symbol"] not in self.symbols:
                    raise PublicBookError("Bitfinex unexpected subscription acknowledgement")
                self.gate.map_channel(mapping)
            if msg.get("event") == "info" and msg.get("code") in {20051, 20060, 20061}:
                from crocodile.core.errors import TransientConnectorError

                raise TransientConnectorError("Bitfinex server requires fresh subscription")
        return True

    async def list_instruments(self) -> list[Instrument]:
        pairs = await self.http_get(self.rest_url + "/conf/pub:list:pair:exchange")
        currencies = await self.http_get(self.rest_url + "/conf/pub:list:currency")
        coins = sorted(currencies[0], key=len, reverse=True)
        result = []
        for pair in pairs[0]:
            if ":" in pair:
                base, quote = pair.split(":", 1)
            else:
                base = next(
                    (
                        coin
                        for coin in coins
                        if pair.startswith(coin) and pair[len(coin) :] in coins
                    ),
                    "",
                )
                quote = pair[len(base) :]
            if base and quote:
                symbol = "t" + pair
                result.append(
                    Instrument(
                        canonical=f"bitfinex:{symbol}",
                        exchange="bitfinex",
                        symbol_raw=symbol,
                        kind=Kind.SPOT,
                        base=base,
                        quote=quote,
                    )
                )
        return result
