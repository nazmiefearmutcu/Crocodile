"""Kraken v2 Decimal order queues, depth truncation and unsigned CRC32."""

from __future__ import annotations

import zlib
from decimal import Decimal
from typing import Literal

from crocodile.core.schema.records import Record
from crocodile.crypto.exchanges.public_books import (
    BookResult,
    Levels,
    PublicBookError,
    decimal,
    iso_ns,
)
from crocodile.crypto.instruments.registry import InstrumentRegistry


def digits(value: Decimal) -> str:
    return format(value, "f").replace(".", "").lstrip("0")


class KrakenBookGate(Levels):
    def __init__(self, mode: str = "book", depth: int = 10) -> None:
        if mode not in {"book", "level3"} or depth not in (
            {10, 100, 1000} if mode == "level3" else {10, 25, 100, 500, 1000}
        ):
            raise PublicBookError("Kraken mode/depth is not supported")
        super().__init__()
        self.mode, self.depth = mode, depth
        self.orders: dict[str, dict[str, dict[str, tuple[Decimal, Decimal]]]] = {}

    def reset(self) -> None:
        super().reset()
        self.orders.clear()

    def apply(self, msg: object, local_ts: int, registry: InstrumentRegistry) -> BookResult:
        if self.invalid:
            return BookResult("drop")
        if not isinstance(msg, dict) or msg.get("channel") != self.mode:
            raise PublicBookError("Kraken unexpected book channel")
        kind = msg.get("type")
        if kind not in {"snapshot", "update"} or not isinstance(msg.get("data"), list):
            raise PublicBookError("Kraken malformed book envelope")
        records: list[Record] = []
        action: Literal["apply", "drop", "snapshot", "gap"] = "drop"
        for data in msg["data"]:
            symbol = data.get("symbol")
            if not isinstance(symbol, str):
                raise PublicBookError("Kraken missing book symbol")
            snapshot = kind == "snapshot"
            if not snapshot and symbol not in self.ready:
                continue
            timestamp = iso_ns(data.get("timestamp"))
            if self.mode == "level3":
                previous = self.orders.get(symbol, {"bids": {}, "asks": {}})
                orders = {
                    side: {} if snapshot else dict(previous[side]) for side in ("bids", "asks")
                }
                for side in ("bids", "asks"):
                    rows = data.get(side, [])
                    if not isinstance(rows, list):
                        raise PublicBookError("Kraken invalid order list")
                    for row in rows:
                        oid = row.get("order_id")
                        if not isinstance(oid, str) or not oid:
                            raise PublicBookError("Kraken invalid order ID")
                        event = "add" if snapshot else row.get("event")
                        if event == "add":
                            if oid in orders[side]:
                                raise PublicBookError("Kraken duplicate order add")
                            orders[side][oid] = (
                                decimal(row.get("limit_price"), positive=True),
                                decimal(row.get("order_qty"), positive=True),
                            )
                        elif event in {"modify", "delete"}:
                            if oid not in orders[side]:
                                raise PublicBookError("Kraken unknown order transition")
                            if event == "delete":
                                del orders[side][oid]
                            else:
                                p = decimal(row.get("limit_price"), positive=True)
                                if p != orders[side][oid][0]:
                                    raise PublicBookError("Kraken modify changed order price")
                                orders[side][oid] = (
                                    p,
                                    decimal(row.get("order_qty"), positive=True),
                                )
                        else:
                            raise PublicBookError("Kraken unknown order event")
                levels: dict[str, dict[Decimal, Decimal]] = {"bids": {}, "asks": {}}
                for side in ("bids", "asks"):
                    prices = sorted({p for p, _ in orders[side].values()}, reverse=side == "bids")[
                        : self.depth
                    ]
                    orders[side] = {
                        oid: values for oid, values in orders[side].items() if values[0] in prices
                    }
                    for p, q in orders[side].values():
                        levels[side][p] = levels[side].get(p, Decimal(0)) + q
                checksum_text = ""
                for side in ("asks", "bids"):
                    for price in sorted(levels[side], reverse=side == "bids")[:10]:
                        for p, q in orders[side].values():
                            if p == price:
                                checksum_text += digits(p) + digits(q)
                next_orders = orders
            else:
                bids, asks = self.level_update(
                    symbol,
                    [(r["price"], r["qty"]) for r in data.get("bids", [])],
                    [(r["price"], r["qty"]) for r in data.get("asks", [])],
                    snapshot=snapshot,
                )
                levels = {
                    "bids": dict(sorted(bids.items(), reverse=True)[: self.depth]),
                    "asks": dict(sorted(asks.items())[: self.depth]),
                }
                checksum_text = "".join(
                    digits(p) + digits(q)
                    for side in ("asks", "bids")
                    for p, q in sorted(levels[side].items(), reverse=side == "bids")[:10]
                )
                next_orders = None
            checksum = data.get("checksum")
            if not isinstance(checksum, int) or isinstance(checksum, bool):
                raise PublicBookError("Kraken checksum missing or invalid")
            if zlib.crc32(checksum_text.encode()) != checksum:
                return self.invalidate()
            if next_orders is not None:
                self.orders[symbol] = next_orders
            result = self.emit(
                "kraken",
                symbol,
                levels["bids"],
                levels["asks"],
                snapshot=snapshot,
                local_ts=local_ts,
                source_ts=timestamp,
                registry=registry,
                derived=self.mode == "level3",
            )
            records.extend(result.records)
            if result.action != "drop":
                action = result.action
        return BookResult(action, tuple(records))
