"""Binance stream_1_0.xml, pinned to bed5d358dbe98bfa4722b6c00c27be1e8f0ba40b."""

from __future__ import annotations

from struct import Struct
from typing import Any

import msgspec

from crocodile.core.errors import FatalConnectorError

SCHEMA_ID = 1
SCHEMA_VERSION = 0
SCHEMA_REFERENCE = "bed5d358dbe98bfa4722b6c00c27be1e8f0ba40b"


class SbeProtocolError(FatalConnectorError):
    """Unsupported or malformed wire data makes continuity unavailable."""


class UnsupportedSbeTemplate(SbeProtocolError):
    pass


class SbeTrade(msgspec.Struct, frozen=True, kw_only=True):
    trade_id: int
    price_mantissa: int
    qty_mantissa: int
    is_buyer_maker: bool


class _Common(msgspec.Struct, frozen=True, kw_only=True):
    symbol_raw: str
    event_time_us: int
    price_exponent: int
    qty_exponent: int


class SbeTrades(_Common, frozen=True, kw_only=True):
    transact_time_us: int
    trades: tuple[SbeTrade, ...]


class SbeBestBidAsk(_Common, frozen=True, kw_only=True):
    book_update_id: int
    bid_price_mantissa: int
    bid_qty_mantissa: int
    ask_price_mantissa: int
    ask_qty_mantissa: int


class SbeDepthSnapshot(_Common, frozen=True, kw_only=True):
    book_update_id: int
    bids: tuple[tuple[int, int], ...]
    asks: tuple[tuple[int, int], ...]


class SbeDepthDiff(_Common, frozen=True, kw_only=True):
    first_update_id: int
    final_update_id: int
    bids: tuple[tuple[int, int], ...]
    asks: tuple[tuple[int, int], ...]


SbeMessage = SbeTrades | SbeBestBidAsk | SbeDepthSnapshot | SbeDepthDiff
_HEADER, _GROUP, _TRADE_GROUP = Struct("<HHHH"), Struct("<HH"), Struct("<HI")
_TRADE, _LEVEL = Struct("<qqqB"), Struct("<qq")
_ROOTS = {
    10000: Struct("<qqbb"),
    10001: Struct("<qqbbqqqq"),
    10002: Struct("<qqbb"),
    10003: Struct("<qqqbb"),
}


class _Cursor:
    def __init__(self, payload: bytes) -> None:
        self.payload, self.offset = payload, 0

    def block(self, size: int, layout: Struct) -> tuple[Any, ...]:
        if size < layout.size or size > len(self.payload) - self.offset:
            raise SbeProtocolError("SBE block is truncated or too short")
        values = layout.unpack_from(self.payload, self.offset)
        self.offset += size
        return values

    def group(self, header: Struct, layout: Struct) -> tuple[tuple[Any, ...], ...]:
        block, count = self.block(header.size, header)
        if block < layout.size or count > (len(self.payload) - self.offset) // max(1, block):
            raise SbeProtocolError("SBE group dimensions exceed available payload")
        return tuple(self.block(block, layout) for _ in range(count))

    def symbol(self) -> str:
        if self.offset >= len(self.payload):
            raise SbeProtocolError("SBE symbol length is missing")
        size = self.payload[self.offset]
        self.offset += 1
        if not size or self.offset + size != len(self.payload):
            raise SbeProtocolError("SBE symbol is truncated or has trailing data")
        try:
            return self.payload[self.offset :].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SbeProtocolError("SBE symbol is not UTF-8") from exc


def decode_sbe_frame(payload: bytes) -> SbeMessage:
    if len(payload) > 16 << 20:
        raise SbeProtocolError("SBE frame exceeds supported size")
    cursor = _Cursor(payload)
    block, template, schema, version = cursor.block(8, _HEADER)
    if (schema, version) != (SCHEMA_ID, SCHEMA_VERSION):
        raise SbeProtocolError("Unsupported SBE schema/version")
    layout = _ROOTS.get(template)
    if layout is None:
        raise UnsupportedSbeTemplate("Unsupported SBE template")
    root = cursor.block(block, layout)
    if template == 10000:
        values = cursor.group(_TRADE_GROUP, _TRADE)
        trades = []
        for trade_id, price, qty, maker in values:
            if maker not in (0, 1):
                raise SbeProtocolError("Invalid SBE bool value")
            trades.append(
                SbeTrade(
                    trade_id=trade_id,
                    price_mantissa=price,
                    qty_mantissa=qty,
                    is_buyer_maker=bool(maker),
                )
            )
        return SbeTrades(
            symbol_raw=cursor.symbol(),
            event_time_us=root[0],
            transact_time_us=root[1],
            price_exponent=root[2],
            qty_exponent=root[3],
            trades=tuple(trades),
        )
    if template == 10001:
        return SbeBestBidAsk(
            symbol_raw=cursor.symbol(),
            event_time_us=root[0],
            book_update_id=root[1],
            price_exponent=root[2],
            qty_exponent=root[3],
            bid_price_mantissa=root[4],
            bid_qty_mantissa=root[5],
            ask_price_mantissa=root[6],
            ask_qty_mantissa=root[7],
        )
    bids = cursor.group(_GROUP, _LEVEL)
    asks = cursor.group(_GROUP, _LEVEL)
    symbol = cursor.symbol()
    if template == 10002:
        return SbeDepthSnapshot(
            symbol_raw=symbol,
            event_time_us=root[0],
            book_update_id=root[1],
            price_exponent=root[2],
            qty_exponent=root[3],
            bids=bids,
            asks=asks,
        )
    return SbeDepthDiff(
        symbol_raw=symbol,
        event_time_us=root[0],
        first_update_id=root[1],
        final_update_id=root[2],
        price_exponent=root[3],
        qty_exponent=root[4],
        bids=bids,
        asks=asks,
    )
