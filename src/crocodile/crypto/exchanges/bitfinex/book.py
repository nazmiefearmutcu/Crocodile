"""R0 order state, connection-wide SEQ_ALL, and signed top-25 CRC32."""

from __future__ import annotations

import zlib
from decimal import Decimal, InvalidOperation
from typing import Any

from crocodile.crypto.exchanges.public_books import (
    BookResult,
    Levels,
    PublicBookError,
    decimal,
    integer,
)
from crocodile.crypto.instruments.registry import InstrumentRegistry


def _number_text(value: Decimal) -> str:
    # The venue's CRC uses ECMAScript Number.toString: scientific notation below
    # 1e-6 or at/above 1e21, with no zero padding in the exponent.
    normalized = value.normalize()
    exponent = normalized.adjusted()
    if exponent < -6 or exponent >= 21:
        mantissa = format(normalized.scaleb(-exponent), "f")
        return f"{mantissa}e{exponent:+d}" if exponent >= 0 else f"{mantissa}e{exponent}"
    return format(normalized, "f")


class BitfinexBookGate(Levels):
    def __init__(self) -> None:
        super().__init__()
        self.channels: dict[int, str] = {}
        self.orders: dict[str, dict[int, tuple[Decimal, Decimal]]] = {}
        self.last_seq: int | None = None
        self.last_message: object = None

    def reset(self) -> None:
        super().reset()
        self.channels.clear()
        self.orders.clear()
        self.last_seq = None
        self.last_message = None

    def map_channel(self, data: dict[str, Any]) -> None:
        if (
            data.get("channel") != "book"
            or data.get("prec") != "R0"
            or data.get("freq") != "F0"
            or str(data.get("len")) != "250"
            or not isinstance(data.get("symbol"), str)
            or not data["symbol"].startswith("t")
        ):
            raise PublicBookError("Bitfinex unexpected public subscription")
        channel = integer(data.get("chanId"))
        if channel in self.channels and self.channels[channel] != data["symbol"]:
            raise PublicBookError("Bitfinex channel identity changed")
        self.channels[channel] = data["symbol"]

    def checksum(self, symbol: str) -> int:
        orders = self.orders[symbol]
        bids = sorted(
            ((oid, p, q) for oid, (p, q) in orders.items() if q > 0), key=lambda x: (-x[1], x[0])
        )[:25]
        asks = sorted(
            ((oid, p, q) for oid, (p, q) in orders.items() if q < 0), key=lambda x: (x[1], x[0])
        )[:25]
        values: list[str] = []
        for i in range(max(len(bids), len(asks))):
            for side in (bids, asks):
                if i < len(side):
                    oid, _, qty = side[i]
                    values.extend((str(oid), _number_text(qty)))
        crc = zlib.crc32(":".join(values).encode())
        return crc if crc < 2**31 else crc - 2**32

    def apply(self, msg: object, local_ts: int, registry: InstrumentRegistry) -> BookResult:
        if self.invalid:
            return BookResult("drop")
        if not isinstance(msg, list) or len(msg) < 4:
            raise PublicBookError("Bitfinex sequenced timestamped array required")
        channel = integer(msg[0])
        if channel not in self.channels:
            raise PublicBookError("Bitfinex unknown channel")
        symbol = self.channels[channel]
        is_checksum = msg[1] == "cs"
        offset = 3 if is_checksum else 2
        if len(msg) <= offset + 1:
            raise PublicBookError("Bitfinex counter/timestamp missing")
        seq, timestamp = integer(msg[offset]), integer(msg[offset + 1])
        if self.last_seq is not None:
            if seq == self.last_seq:
                return BookResult("drop") if msg == self.last_message else self.invalidate()
            if seq != self.last_seq + 1:
                return self.invalidate()
        self.last_seq, self.last_message = seq, msg
        if msg[1] == "hb":
            return BookResult("drop")
        if is_checksum:
            if symbol not in self.ready:
                return BookResult("drop")
            if not isinstance(msg[2], int) or self.checksum(symbol) != msg[2]:
                return self.invalidate()
            return BookResult("drop")
        raw = msg[1]
        if not isinstance(raw, list):
            raise PublicBookError("Bitfinex raw order invalid")
        snapshot = symbol not in self.ready
        if snapshot and raw and not isinstance(raw[0], list):
            return BookResult("drop")
        rows = raw if not raw or isinstance(raw[0], list) else [raw]
        orders = {} if snapshot else dict(self.orders[symbol])
        for row in rows:
            if not isinstance(row, list) or len(row) < 3:
                raise PublicBookError("Bitfinex raw order invalid")
            oid, price = integer(row[0]), decimal(row[1])
            try:
                qty = Decimal(str(row[2]))
            except InvalidOperation:
                raise PublicBookError("Bitfinex signed quantity invalid") from None
            if not qty.is_finite() or not qty:
                raise PublicBookError("Bitfinex signed quantity invalid")
            decimal(qty.copy_abs(), positive=True)
            if not price:
                if oid not in orders:
                    raise PublicBookError("Bitfinex delete of unknown order")
                del orders[oid]
            else:
                if snapshot and oid in orders:
                    raise PublicBookError("Bitfinex duplicate snapshot order")
                orders[oid] = (price, qty)
        self.orders[symbol] = orders
        bids: dict[Decimal, Decimal] = {}
        asks: dict[Decimal, Decimal] = {}
        for price, qty in orders.values():
            side = bids if qty > 0 else asks
            side[price] = side.get(price, Decimal(0)) + abs(qty)
        return self.emit(
            "bitfinex",
            symbol,
            bids,
            asks,
            snapshot=snapshot,
            local_ts=local_ts,
            source_ts=timestamp * 1_000_000,
            registry=registry,
            derived=True,
        )
