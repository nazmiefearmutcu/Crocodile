"""Canonical floats are a view; raw integer precision remains in capture."""

from collections.abc import Iterable
from decimal import Decimal, DecimalException
from math import isfinite

from crocodile.core.schema.enums import AssetClass, Side
from crocodile.core.schema.records import BookDelta, BookSnapshot, BookTicker, Record, Trade
from crocodile.crypto.exchanges.binance.book_session import BinanceDepthEvent
from crocodile.crypto.exchanges.binance.sbe import (
    SbeBestBidAsk,
    SbeDepthDiff,
    SbeDepthSnapshot,
    SbeMessage,
    SbeProtocolError,
    SbeTrades,
)
from crocodile.crypto.instruments.registry import InstrumentRegistry


def _value(mantissa: int, exponent: int, *, price: bool = False) -> float:
    try:
        value = float(Decimal(mantissa).scaleb(exponent))
    except (DecimalException, OverflowError, ValueError) as exc:
        raise SbeProtocolError("SBE decimal cannot be represented canonically") from exc
    if not isfinite(value) or value < 0 or (price and value <= 0):
        raise SbeProtocolError("Invalid canonical SBE price or quantity")
    if mantissa != 0 and value == 0:
        raise SbeProtocolError("SBE decimal underflows canonical precision")
    return value


def _timestamp(microseconds: int) -> int:
    value = microseconds * 1000
    if not -(1 << 63) <= value < 1 << 63:
        raise SbeProtocolError("SBE timestamp exceeds canonical nanosecond range")
    return value


def _levels(
    event: SbeDepthDiff | SbeDepthSnapshot, levels: tuple[tuple[int, int], ...]
) -> tuple[tuple[float, float], ...]:
    return tuple(
        (_value(p, event.price_exponent, price=True), _value(q, event.qty_exponent))
        for p, q in levels
    )


def to_depth_event(event: SbeDepthDiff) -> BinanceDepthEvent:
    return BinanceDepthEvent(
        symbol_raw=event.symbol_raw,
        source_ts_ns=_timestamp(event.event_time_us),
        U=event.first_update_id,
        u=event.final_update_id,
        pu=None,
        bids=_levels(event, event.bids),
        asks=_levels(event, event.asks),
    )


def normalize_sbe_message(
    event: SbeMessage, *, local_ts: int, registry: InstrumentRegistry
) -> Iterable[Record]:
    source = "binance-spot"
    inst = registry.get_raw(source, event.symbol_raw)
    symbol = inst.canonical if inst is not None else f"{source}:{event.symbol_raw}"
    if isinstance(event, SbeTrades):
        for trade in event.trades:
            yield Trade(
                source=source,
                symbol=symbol,
                symbol_raw=event.symbol_raw,
                local_ts=local_ts,
                source_ts=_timestamp(event.transact_time_us),
                asset_class=AssetClass.CRYPTO,
                id=str(trade.trade_id),
                price=_value(trade.price_mantissa, event.price_exponent, price=True),
                amount=_value(trade.qty_mantissa, event.qty_exponent),
                side=Side.SELL if trade.is_buyer_maker else Side.BUY,
            )
    elif isinstance(event, SbeBestBidAsk):
        yield BookTicker(
            source=source,
            symbol=symbol,
            symbol_raw=event.symbol_raw,
            local_ts=local_ts,
            source_ts=_timestamp(event.event_time_us),
            asset_class=AssetClass.CRYPTO,
            update_id=event.book_update_id,
            bid_px=_value(event.bid_price_mantissa, event.price_exponent, price=True),
            bid_sz=_value(event.bid_qty_mantissa, event.qty_exponent),
            ask_px=_value(event.ask_price_mantissa, event.price_exponent, price=True),
            ask_sz=_value(event.ask_qty_mantissa, event.qty_exponent),
        )
    elif isinstance(event, SbeDepthSnapshot):
        bids, asks = _levels(event, event.bids), _levels(event, event.asks)
        yield BookSnapshot(
            source=source,
            symbol=symbol,
            symbol_raw=event.symbol_raw,
            local_ts=local_ts,
            source_ts=_timestamp(event.event_time_us),
            asset_class=AssetClass.CRYPTO,
            sequence_id=event.book_update_id,
            bids=list(bids),
            asks=list(asks),
            depth=len(bids) + len(asks),
        )
    else:
        depth = to_depth_event(event)
        yield BookDelta(
            source=source,
            symbol=symbol,
            symbol_raw=event.symbol_raw,
            local_ts=local_ts,
            source_ts=depth.source_ts_ns,
            asset_class=AssetClass.CRYPTO,
            seq_id=depth.u,
            prev_seq_id=None,
            bids=list(depth.bids),
            asks=list(depth.asks),
        )
