from __future__ import annotations

import json
from typing import ClassVar

from crocodile.core.capture.schema import RawFrame
from crocodile.core.errors import ConfigError
from crocodile.core.ingest.transport import Transport
from crocodile.core.sink.base import Sink
from crocodile.crypto.exchanges.public_books import GatedConnector, PublicBookError
from crocodile.crypto.instruments.registry import Instrument, InstrumentRegistry, Kind

from .book import DydxBookGate


class DydxConnector(GatedConnector):
    name = "dydx"
    market = "perpetual"
    ws_url = "wss://indexer.dydx.trade/v4/ws"
    rest_url = "https://indexer.dydx.trade/v4"
    capture_endpoint_id = "dydx-v4-unbatched"
    decoder_id = "dydx-v4"
    quantity_unit = "base_asset"
    coverage: ClassVar[dict[str, object]] = {
        "kind": "indexer_l2",
        "cadence": None,
        "batched": False,
    }

    def __init__(
        self, symbols: list[str], channels: list[str], out: Sink, registry: InstrumentRegistry
    ) -> None:
        if any(c not in {"book_delta", "book_snapshot"} for c in channels):
            raise ConfigError("dYdX native unbatched connector supports book channels")
        super().__init__(symbols, channels, out, registry)
        self.gate = DydxBookGate()

    def subscribe_channels(self) -> list[str]:
        return [f"v4_orderbook.{s}" for s in self.symbols]

    async def _subscribe(self, transport: Transport) -> None:
        for symbol in self.symbols:
            await transport.send(
                json.dumps(
                    {"type": "subscribe", "channel": "v4_orderbook", "id": symbol, "batched": False}
                ).encode()
            )

    async def _handle_control_message(self, msg: object, frame: RawFrame) -> bool:
        if isinstance(msg, dict) and (msg.get("type") == "error" or msg.get("error")):
            raise PublicBookError("dYdX public subscription rejected")
        return True

    async def list_instruments(self) -> list[Instrument]:
        raw = await self.http_get(self.rest_url + "/perpetualMarkets")
        result = []
        for item in raw.get("markets", {}).values():
            symbol = item["ticker"]
            base, quote = symbol.rsplit("-", 1)
            result.append(
                Instrument(
                    canonical=f"dydx:{symbol}",
                    exchange="dydx",
                    symbol_raw=symbol,
                    kind=Kind.PERPETUAL,
                    base=base,
                    quote=quote,
                    tick_size=float(item["tickSize"]),
                )
            )
        return result
