"""Coinbase public Exchange (default) and Advanced Trade (explicit mode).

Appendix §7:
- WS public: ``wss://ws-feed.exchange.coinbase.com``
- Subscribe format: ``{"type":"subscribe","product_ids":[...],"channels":["matches"]}``
- ``product_id`` is canonical (e.g. ``BTC-USD``); always cache ``/products``.
- Spot only: no funding/OI/liquidation channels.
- Rate limits: ~5 req/s public REST; ~100 subscriptions per connection.

Channels:
- ``trade`` / ``book_ticker`` → ``ticker`` (Coinbase ticker stream)
- ``book_delta`` / ``book_snapshot`` → ``level2`` (snapshot + incremental updates)
- ``trade`` → ``matches``

Subscribe message triggers immediate snapshot for ``level2`` channel.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from crocodile.core.capture.schema import RawFrame
from crocodile.core.errors import ConfigError
from crocodile.core.ingest.transport import Transport
from crocodile.core.schema.records import Record
from crocodile.core.sink.base import Sink
from crocodile.crypto.exchanges.public_books import GatedConnector, PublicBookError
from crocodile.crypto.instruments.registry import Instrument, InstrumentRegistry, Kind

from .advanced import CoinbaseAdvancedGate
from .normalize import normalize_message
from .replay import CoinbaseExchangeGate

log = logging.getLogger(__name__)

EXCHANGE = "coinbase"
WS_URL = "wss://ws-feed.exchange.coinbase.com"
REST_BASE = "https://api.exchange.coinbase.com"

# Mapping from canonical channel names to Coinbase WS channel strings.
# Multiple canonical channels can map to the same Coinbase channel; deduplication
# is applied in build_channels.
_CHANNEL_MAP: dict[str, str] = {
    "trade": "matches",
    "book_delta": "level2",
    "book_snapshot": "level2",
    "book_ticker": "ticker",
    "derivative_ticker": "ticker",  # spot-only fallback (no derivative data emitted)
}


def build_channels(symbols: list[str], channels: list[str]) -> list[str]:
    """Return the set of Coinbase WS channel strings for the given canonical channels.

    Deduplicates: ``book_delta`` and ``book_snapshot`` both map to ``level2``, so only
    one ``level2`` subscription is created regardless of how many canonical channels
    reference it.  The ``symbols`` argument is accepted for interface parity but
    Coinbase uses ``product_ids`` in the subscribe frame, not channel-per-symbol names.

    The ``ticker`` channel is always included whenever any channel is requested;
    it provides top-of-book BookTicker data and also fires on last trade so it
    naturally accompanies ``matches`` and ``level2`` subscriptions.

    Returns a sorted deduplicated list of Coinbase channel name strings.
    """
    result: set[str] = set()
    for ch in channels:
        mapped = _CHANNEL_MAP.get(ch)
        if mapped is not None:
            result.add(mapped)
    # Always include ticker: it provides BookTicker and last-trade data alongside
    # matches and level2.
    if result:
        result.add("ticker")
    return sorted(result)


def parse_products(raw: dict[str, Any]) -> list[Instrument]:
    """Parse the JSON response from Coinbase ``GET /products``.

    Response shape: ``{"products": [{...}]}``.
    All products are spot instruments (Coinbase Advanced Trade is spot only).

    Fields used:
    - ``product_id``       → symbol_raw (canonical Coinbase ID, e.g. ``BTC-USD``)
    - ``base_currency``    → base
    - ``quote_currency``   → quote
    - ``quote_increment``  → tick_size
    """
    out: list[Instrument] = []
    products: list[dict[str, Any]] = raw.get("products") or []
    for item in products:
        product_id: str = item["product_id"]
        base: str = item.get("base_currency_id", item.get("base_currency", ""))
        quote: str = item.get("quote_currency_id", item.get("quote_currency", ""))

        tick_size: float | None = None
        qi_raw = item.get("price_increment", item.get("quote_increment"))
        if qi_raw:
            try:
                tick_size = float(qi_raw)
            except (ValueError, TypeError):
                pass

        out.append(
            Instrument(
                canonical=f"{EXCHANGE}:{product_id}",
                exchange=EXCHANGE,
                symbol_raw=product_id,
                kind=Kind.SPOT,  # Coinbase Advanced Trade is spot only
                base=base,
                quote=quote,
                tick_size=tick_size,
            )
        )
    return out


class CoinbaseConnector(GatedConnector):
    """Public spot feeds with distinct protocol and replay identities.

    Exchange preserves its unsequenced L2 protocol. Advanced tracks the public
    envelope counter across every subscribed channel and reconnects on gaps.
    Neither protocol's counter is fabricated into a canonical per-book ID.
    """

    name = EXCHANGE
    ws_url = WS_URL
    rest_url = REST_BASE
    quantity_unit = "base_asset"

    def __init__(
        self,
        symbols: list[str],
        channels: list[str],
        out: Sink,
        registry: InstrumentRegistry,
        *,
        mode: str = "exchange",
    ) -> None:
        if mode not in {"exchange", "advanced"}:
            raise ConfigError("Coinbase mode expects exchange or advanced")
        super().__init__(symbols=symbols, channels=channels, out=out, registry=registry)
        self.mode = mode
        self.market = "spot"
        self.required_feed = mode == "advanced"
        self.capture_endpoint_id = f"coinbase-{mode}"
        self.decoder_id = "coinbase-json"
        self.gate = CoinbaseAdvancedGate() if mode == "advanced" else CoinbaseExchangeGate()
        self.ws_url = "wss://advanced-trade-ws.coinbase.com" if mode == "advanced" else WS_URL
        self.coverage = {
            "kind": "visible_l2",
            "cadence": None,
            "trade_batch_ms": 250 if mode == "advanced" else None,
        }
        self._sub_channels = build_channels(symbols, channels)

    def normalize(self, msg: object, local_ts: int) -> Iterable[Record]:
        if self.mode == "advanced":
            yield from super().normalize(msg, local_ts)
            return
        if isinstance(msg, dict):
            yield from normalize_message(msg, local_ts=local_ts, registry=self.registry)

    async def list_instruments(self) -> list[Instrument]:
        """Fetch the selected protocol's public spot product catalogue."""
        if self.mode == "exchange":
            items = await self.http_get(f"{REST_BASE}/products")
            if not isinstance(items, list):
                raise PublicBookError("Coinbase Exchange product query rejected")
            return parse_products(
                {"products": [{**item, "product_id": item["id"]} for item in items]}
            )
        instruments: list[Instrument] = []
        offset = 0
        for _ in range(100):
            data = await self.http_get(
                "https://api.coinbase.com/api/v3/brokerage/market/products",
                params={"product_type": "SPOT", "limit": 1000, "offset": offset},
            )
            if not isinstance(data, dict) or not isinstance(data.get("products"), list):
                raise PublicBookError("Coinbase Advanced product query rejected")
            instruments.extend(parse_products(data))
            count = len(data["products"])
            offset += count
            if count < 1000 or offset >= data.get("num_products", offset + 1):
                return instruments
        raise PublicBookError("Coinbase Advanced product pagination exceeds budget")

    def make_transport(self) -> Transport:
        from crocodile.core.ingest.transport import AiohttpWsTransport

        # BTC's Advanced snapshot currently exceeds aiohttp's 4 MiB default.
        # Keep it inside the connector reader's bounded 8 MiB queue.
        return AiohttpWsTransport(self.ws_url, max_msg_size=8 << 20)

    def subscribe_channels(self) -> list[str]:
        """Return the list of Coinbase channel strings this connector subscribes to."""
        if self.mode == "advanced":
            mapping = {"matches": "market_trades", "level2": "level2", "ticker": "ticker"}
            return sorted({mapping[c] for c in self._sub_channels} | {"heartbeats"})
        return self._sub_channels

    async def _subscribe(self, transport: Transport) -> None:  # pragma: no cover
        """Send a Coinbase subscribe frame.

        Coinbase subscribe format::

            {
                "type": "subscribe",
                "product_ids": ["BTC-USD", "ETH-USD"],
                "channels": ["matches", "level2", "ticker"]
            }
        """
        channels = self.subscribe_channels()
        if self.mode == "advanced":
            for channel in channels:
                request: dict[str, Any] = {"type": "subscribe", "channel": channel}
                if channel != "heartbeats":
                    request["product_ids"] = self.symbols
                await transport.send(json.dumps(request).encode())
            return
        if channels and self.symbols:
            frame = json.dumps(
                {
                    "type": "subscribe",
                    "product_ids": self.symbols,
                    "channels": channels,
                }
            ).encode()
            await transport.send(frame)

    async def _handle_message(self, msg: object, local_ts: int) -> None:
        if self.mode == "advanced" or self.capture is not None:
            await super()._handle_message(msg, local_ts)
        else:
            # Existing Exchange byte-only consumers retain their normalizer contract.
            await self._emit_records(list(self.normalize(msg, local_ts)))

    async def _handle_control_message(self, msg: object, frame: RawFrame) -> bool:
        if isinstance(msg, dict) and (msg.get("type") == "error" or msg.get("error")):
            raise PublicBookError("Coinbase public subscription rejected")
        return True

    async def _drain_market(self) -> None:
        if (self.mode == "advanced" or self.capture is not None) and any(
            c in {"book_delta", "book_snapshot"} for c in self.channels
        ):
            await super()._drain_market()
