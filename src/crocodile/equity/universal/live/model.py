"""The live snapshot: what one poll of one listing returned, and the records it becomes.

A live source answers "what is this listing doing now" with as much of the following as its
publisher exposes: the last trade, the session's open/high/low and cumulative volume, a ladder of
resting bid and ask levels, and the publisher's own tape of recent prints. ``LiveSnapshot`` holds
exactly what was published. Nothing is filled in: an absent size is ``None``, never ``0.0``, and
a source that publishes one level is a top of book, not a book of one level.

Tiers. ``LiveTier`` names how deep the published ladder is, which is what a consumer needs to
know before it draws anything:

- ``L2``: two or more price levels on at least one side, each with its resting size.
- ``L1``: the best bid and the best ask (sizes when published).
- ``LAST``: no resting orders at all, only the last price and the session figures.

Ruling: the tier is measured on the snapshot, not declared by the source. A source that
advertises five levels and answers with one level during an auction is ``L1`` for that poll.
Cost if wrong: a consumer would draw a book that the venue did not publish.

Ruling: ``delay_s`` is the publisher's own statement of how old the figures are (``0`` for
real time, ``900`` for a fifteen-minute delayed feed, ``None`` when it says nothing). It is
carried on the snapshot and never used to shift a timestamp. Cost if wrong: delayed data would
be stamped as if it were observed at the moment of the request.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from crocodile.core.markets.symbols import listing_symbol
from crocodile.core.schema.enums import AssetClass, Side
from crocodile.core.schema.provenance import provenance_fields
from crocodile.core.schema.records import BookSnapshot, Quote, Record, Trade
from crocodile.equity.universal.live.bases import (
    BASIS_POLLED_BOOK,
    BASIS_POLLED_PRINT,
    BASIS_VOLUME_DELTA,
)

__all__ = [
    "BASIS_POLLED_BOOK",
    "BASIS_POLLED_PRINT",
    "BASIS_VOLUME_DELTA",
    "TIER_RANK",
    "LiveLevel",
    "LiveListing",
    "LivePrint",
    "LiveSnapshot",
    "LiveTier",
    "clean_levels",
    "snapshot_records",
]


class LiveTier(StrEnum):
    """How deep the ladder a snapshot carries is."""

    L2 = "l2"
    L1 = "l1"
    LAST = "last"


TIER_RANK: Final[dict[LiveTier, int]] = {LiveTier.L2: 0, LiveTier.L1: 1, LiveTier.LAST: 2}
"""Lower is deeper. The order decides which source a market's chain asks first."""


@dataclass(frozen=True, slots=True)
class LiveListing:
    """The identity a live source needs: the market, the local symbol and, when known, the ISIN.

    Deliberately smaller than :class:`crocodile.core.markets.master.Listing`: a live read must
    work for a listing nobody has synced into the listing master yet, so everything here can be
    built from a canonical ``LOCAL@MIC`` symbol alone. ``isin`` is optional because only some
    sources key on it; a source that needs it and does not get it answers nothing.
    """

    mic: str
    local_symbol: str
    isin: str | None = None

    @property
    def symbol(self) -> str:
        return listing_symbol(self.local_symbol, self.mic)


@dataclass(frozen=True, slots=True)
class LiveLevel:
    """One resting price level. ``size`` is ``None`` when the publisher states a price only."""

    price: float
    size: float | None


@dataclass(frozen=True, slots=True)
class LivePrint:
    """One print from the publisher's own tape."""

    price: float
    size: float
    ts_ns: int | None
    side: Side = Side.UNKNOWN
    id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class LiveSnapshot:
    source: str
    listing: LiveListing
    symbol_raw: str
    captured_ns: int
    source_ts_ns: int | None = None
    last: float | None = None
    last_size: float | None = None
    """Size of the last print, when the publisher states it beside the last price."""

    prev_close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    """Cumulative session volume in shares (or the unit the exchange quotes volume in)."""

    turnover: float | None = None
    bids: tuple[LiveLevel, ...] = ()
    """Best (highest) bid first."""

    asks: tuple[LiveLevel, ...] = ()
    """Best (lowest) ask first."""

    prints: tuple[LivePrint, ...] = ()
    """The publisher's tape, oldest first. Empty when it publishes none."""

    currency: str | None = None
    price_scale: int | None = None
    delay_s: int | None = None
    name: str | None = None

    @property
    def tier(self) -> LiveTier:
        if max(len(self.bids), len(self.asks)) >= 2:
            return LiveTier.L2
        if self.bids or self.asks:
            return LiveTier.L1
        return LiveTier.LAST

    @property
    def best_bid(self) -> LiveLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> LiveLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def reference_price(self) -> float | None:
        """Mid of the top of book when both sides are quoted, else the last price."""
        bid, ask = self.best_bid, self.best_ask
        if bid is not None and ask is not None:
            return (bid.price + ask.price) / 2.0
        return self.last

    @property
    def is_empty(self) -> bool:
        """True when the poll carried no price at all: nothing to draw and nothing to store."""
        return self.last is None and not self.bids and not self.asks


def _finite_positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0.0


def clean_levels(
    levels: Sequence[tuple[float | None, float | None]], *, descending: bool
) -> tuple[LiveLevel, ...]:
    """Drop empty slots, keep published order, and check it is monotone.

    A publisher pads a short ladder with zero prices, so a non-positive or non-finite price is
    an empty slot, not a level. A negative or non-finite size is dropped to ``None``: the price
    was stated, the size was not. A zero size is kept only as ``None`` for the same reason; a
    resting level of nothing is not a level the venue could have published.

    The surviving levels must run strictly away from the touch (``descending`` for bids). A
    ladder that does not is a parse of the wrong fields, so it is refused whole with an empty
    tuple instead of being sorted into something that looks right.
    """
    kept: list[LiveLevel] = []
    for price, size in levels:
        if price is None or not _finite_positive(price):
            continue
        stated = size if size is not None and math.isfinite(size) and size > 0.0 else None
        kept.append(LiveLevel(price=float(price), size=stated))
    for earlier, later in itertools.pairwise(kept):
        if descending and not later.price < earlier.price:
            return ()
        if not descending and not later.price > earlier.price:
            return ()
    return tuple(kept)


def _sized(levels: Sequence[LiveLevel]) -> list[tuple[float, float]]:
    return [(level.price, level.size) for level in levels if level.size is not None]


def _trade(
    snapshot: LiveSnapshot,
    *,
    trade_id: str,
    price: float,
    size: float,
    side: Side,
    ts_ns: int | None,
) -> Trade:
    tail = provenance_fields("polled_last_print")
    return Trade(
        source=snapshot.source,
        symbol=snapshot.listing.symbol,
        symbol_raw=snapshot.symbol_raw,
        local_ts=snapshot.captured_ns,
        asset_class=AssetClass.EQUITY,
        source_ts=ts_ns,
        prov=tail.prov,
        prov_basis=tail.prov_basis,
        prov_confidence=tail.prov_confidence,
        prov_inputs=tail.prov_inputs,
        id=trade_id,
        price=price,
        amount=size,
        side=side,
        currency=snapshot.currency,
        price_scale=snapshot.price_scale,
    )


def snapshot_records(snapshot: LiveSnapshot) -> list[Record]:
    """The canonical records one snapshot supports, and nothing it does not.

    - ``book_snapshot``: when at least one side carries a sized level. ``depth`` is the number of
      levels the publisher exposed on its deeper side, which is how a reader tells a five-level
      ladder from a full book.
    - ``quote``: when both sides of the top of book are stated with sizes. A ``Quote`` cannot
      exist without both sizes, so a price-only top of book produces none.
    - ``trade``: one per print of the publisher's tape; or, when it publishes no tape but states
      the size of its last print, that one print. A last price with no size produces no trade.
    """
    records: list[Record] = []
    book_tail = provenance_fields("polled_book_snapshot")
    bids, asks = _sized(snapshot.bids), _sized(snapshot.asks)
    if bids or asks:
        records.append(
            BookSnapshot(
                source=snapshot.source,
                symbol=snapshot.listing.symbol,
                symbol_raw=snapshot.symbol_raw,
                local_ts=snapshot.captured_ns,
                asset_class=AssetClass.EQUITY,
                source_ts=snapshot.source_ts_ns,
                prov=book_tail.prov,
                prov_basis=book_tail.prov_basis,
                prov_confidence=book_tail.prov_confidence,
                prov_inputs=book_tail.prov_inputs,
                bids=bids,
                asks=asks,
                depth=max(len(snapshot.bids), len(snapshot.asks)),
            )
        )
    bid, ask = snapshot.best_bid, snapshot.best_ask
    if bid is not None and ask is not None and bid.size is not None and ask.size is not None:
        records.append(
            Quote(
                source=snapshot.source,
                symbol=snapshot.listing.symbol,
                symbol_raw=snapshot.symbol_raw,
                local_ts=snapshot.captured_ns,
                asset_class=AssetClass.EQUITY,
                source_ts=snapshot.source_ts_ns,
                prov=book_tail.prov,
                prov_basis=book_tail.prov_basis,
                prov_confidence=book_tail.prov_confidence,
                prov_inputs=book_tail.prov_inputs,
                bid_px=bid.price,
                bid_sz=bid.size,
                ask_px=ask.price,
                ask_sz=ask.size,
                currency=snapshot.currency,
                price_scale=snapshot.price_scale,
            )
        )
    if snapshot.prints:
        for index, item in enumerate(snapshot.prints):
            records.append(
                _trade(
                    snapshot,
                    trade_id=item.id if item.id is not None else f"{item.ts_ns}-{index}",
                    price=item.price,
                    size=item.size,
                    side=item.side,
                    ts_ns=item.ts_ns,
                )
            )
    elif (
        snapshot.last is not None
        and snapshot.last_size is not None
        and _finite_positive(snapshot.last)
        and _finite_positive(snapshot.last_size)
    ):
        stamp = snapshot.source_ts_ns if snapshot.source_ts_ns is not None else snapshot.captured_ns
        records.append(
            _trade(
                snapshot,
                trade_id=str(stamp),
                price=snapshot.last,
                size=snapshot.last_size,
                side=Side.UNKNOWN,
                ts_ns=snapshot.source_ts_ns,
            )
        )
    return records
