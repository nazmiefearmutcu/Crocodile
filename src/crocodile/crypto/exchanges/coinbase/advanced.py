"""Coinbase Advanced public envelope counter and absolute L2 updates.

The current AsyncAPI declares sequence_num per connection. Heartbeats and
subscription notifications advance it, and it is never a canonical per-book ID.
"""

from __future__ import annotations

from typing import Any, Literal

from crocodile.core.schema.enums import AssetClass, Side
from crocodile.core.schema.records import BookTicker, Record, Trade
from crocodile.crypto.exchanges.public_books import (
    BookResult,
    Levels,
    PublicBookError,
    decimal,
    integer,
    iso_ns,
)
from crocodile.crypto.instruments.registry import InstrumentRegistry


class CoinbaseAdvancedGate(Levels):
    def __init__(self) -> None:
        super().__init__()
        self.last_seq: int | None = None
        self.last_message: object = None

    def reset(self) -> None:
        super().reset()
        self.last_seq = None
        self.last_message = None

    def apply(self, msg: object, local_ts: int, registry: InstrumentRegistry) -> BookResult:
        if self.invalid:
            return BookResult("drop")
        if not isinstance(msg, dict):
            raise PublicBookError("Coinbase Advanced envelope must be an object")
        seq = integer(msg.get("sequence_num"))
        if self.last_seq is not None:
            if seq == self.last_seq:
                return BookResult("drop") if msg == self.last_message else self.invalidate()
            if seq != self.last_seq + 1:
                return self.invalidate()
        self.last_seq, self.last_message = seq, msg
        channel = msg.get("channel")
        if channel in {"heartbeats", "subscriptions"}:
            return BookResult("drop")
        timestamp = iso_ns(msg.get("timestamp"))
        events = msg.get("events")
        if not isinstance(events, list):
            raise PublicBookError("Coinbase Advanced events must be a list")
        records: list[Record] = []
        action: Literal["apply", "drop", "snapshot", "gap"] = "drop"
        for event in events:
            if channel == "l2_data":
                symbol = event.get("product_id")
                kind = event.get("type")
                if not isinstance(symbol, str) or kind not in {"snapshot", "update"}:
                    raise PublicBookError("Coinbase Advanced book event invalid")
                snapshot = kind == "snapshot"
                if not snapshot and symbol not in self.ready:
                    continue
                bids: list[tuple[object, object]] = []
                asks: list[tuple[object, object]] = []
                updates = event.get("updates")
                if not isinstance(updates, list):
                    raise PublicBookError("Coinbase Advanced book update list invalid")
                for update in updates:
                    side = update.get("side")
                    if side not in {"bid", "offer"}:
                        raise PublicBookError("Coinbase Advanced unknown book side")
                    target = bids if side == "bid" else asks
                    target.append((update.get("price_level"), update.get("new_quantity")))
                b, a = self.level_update(symbol, bids, asks, snapshot=snapshot)
                result = self.emit(
                    "coinbase",
                    symbol,
                    b,
                    a,
                    snapshot=snapshot,
                    local_ts=local_ts,
                    source_ts=timestamp,
                    registry=registry,
                )
                records.extend(result.records)
                if result.action != "drop":
                    action = result.action
            elif channel == "ticker":
                for ticker in event.get("tickers", []):
                    symbol = ticker["product_id"]
                    if not all(
                        ticker.get(k) is not None
                        for k in ("best_bid", "best_ask", "best_bid_quantity", "best_ask_quantity")
                    ):
                        continue
                    records.append(
                        BookTicker(
                            **_header(symbol, local_ts, timestamp, registry),
                            bid_px=float(decimal(ticker["best_bid"], positive=True)),
                            ask_px=float(decimal(ticker["best_ask"], positive=True)),
                            bid_sz=float(decimal(ticker["best_bid_quantity"])),
                            ask_sz=float(decimal(ticker["best_ask_quantity"])),
                        )
                    )
                    action = "apply"
            elif channel == "market_trades":
                for trade in event.get("trades", []):
                    symbol = trade["product_id"]
                    raw_side = trade.get("side")
                    # Advanced documents the side of the maker. Canonical Trade
                    # carries aggressor side, so it is the opposite side.
                    side = (
                        Side.SELL
                        if raw_side == "BUY"
                        else Side.BUY
                        if raw_side == "SELL"
                        else Side.UNKNOWN
                    )
                    records.append(
                        Trade(
                            **_header(symbol, local_ts, iso_ns(trade.get("time")), registry),
                            id=str(trade["trade_id"]),
                            price=float(decimal(trade["price"], positive=True)),
                            amount=float(decimal(trade["size"], positive=True)),
                            side=side,
                        )
                    )
                    action = "apply"
            else:
                raise PublicBookError("Coinbase Advanced unexpected public channel")
        return BookResult(action, tuple(records))


def _header(
    symbol: str, local_ts: int, source_ts: int | None, registry: InstrumentRegistry
) -> dict[str, Any]:
    instrument = registry.get_raw("coinbase", symbol)
    return dict(
        source="coinbase",
        symbol=instrument.canonical if instrument else f"coinbase:{symbol}",
        symbol_raw=symbol,
        local_ts=local_ts,
        source_ts=source_ts,
        asset_class=AssetClass.CRYPTO,
    )
