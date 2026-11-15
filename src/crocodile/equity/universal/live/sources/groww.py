"""Groww's live book and last price for the National Stock Exchange of India (``groww.in``).

Groww is a broker, not the exchange: these are its public market-data endpoints, a broker's feed
of NSE prices, so the source is not official. Two GETs serve one listing, addressed by its NSE
symbol in the URL path and percent-encoded, because NSE symbols can carry ``&`` and ``-``:

- ``tr_live_book``: the five best bid and ask levels in ``buyBook`` and ``sellBook``, keyed
  ``"1"`` to ``"5"``, level ``"1"`` the best. ``qty`` is a count of shares.
- ``tr_live_prices``: the last trade (``ltp``, ``lastTradeQty``), the session's open, high and
  low, the previous close (``close``), and the cumulative ``volume``.

Units. Prices are rupees and every size and volume is already a count of shares, so nothing is
converted. Evidence, from the captured RELIANCE answers: level ``"1"`` of the bid book is
``1335.7`` with ``qty`` ``210``; ``volume`` ``8270544`` is the session total; ``dayChange``
``0.3999...`` is ``ltp`` ``1335.8`` minus ``close`` ``1335.4``, which is why ``close`` is read as
the previous close.

Ruling: the stamps are epoch SECONDS although the names say milliseconds. ``tsInMillis`` and
``lastTradeTime`` are 10-digit numbers in the fixture (``1794471338`` is 2026-11-12 08:15:38 UTC).
A stamp below ``10**11`` is read as seconds and anything else as milliseconds, so either spelling
resolves to the right instant. Cost if wrong: every timestamp would be off by a factor of 1000.

Ruling: the book answer carries the listing's symbol, and a book whose symbol is not the
listing's is not attached. The BSE endpoint is keyed by a numeric scrip code, not by the local
symbol, so XBOM is not served here.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, ClassVar, Final
from urllib.parse import quote

from crocodile.core.markets.usage import UsageClass
from crocodile.equity.universal.live.base import (
    LivePayloadError,
    LiveSource,
    decode_json,
    millis_ns,
    seconds_ns,
    to_float,
)
from crocodile.equity.universal.live.model import (
    LiveLevel,
    LiveListing,
    LiveSnapshot,
    LiveTier,
    clean_levels,
)

__all__ = ["BOOK_URL", "PRICE_URL", "SOURCE", "GrowwLive", "parse_book", "parse_price"]

SOURCE: Final = "groww"
BOOK_URL: Final = (
    "https://groww.in/v1/api/stocks_data/v1/tr_live_book/exchange/NSE/segment/CASH/{symbol}/latest"
)
PRICE_URL: Final = (
    "https://groww.in/v1/api/stocks_data/v1/tr_live_prices"
    "/exchange/NSE/segment/CASH/{symbol}/latest"
)
_CURRENCY: Final = "INR"
_SECONDS_BELOW: Final = 10**11


def _stamp_ns(value: Any) -> int | None:
    """An epoch stamp in seconds or milliseconds, told apart by magnitude, as UTC nanoseconds."""
    number = to_float(value)
    if number is None or number <= 0:
        return None
    return seconds_ns(number) if number < _SECONDS_BELOW else millis_ns(number)


def _positive(value: Any) -> float | None:
    """A published price or size above zero, or ``None``: a zero is not a price."""
    number = to_float(value)
    return number if number is not None and number > 0 else None


def _non_negative(value: Any) -> float | None:
    """A published volume, zero included (no trade yet is a real zero), or ``None``."""
    number = to_float(value)
    return number if number is not None and number >= 0 else None


def _ladder(book: Mapping[str, Any], *, descending: bool) -> tuple[LiveLevel, ...]:
    keys = sorted((key for key in book if key.isascii() and key.isdigit()), key=int)
    pairs: list[tuple[float | None, float | None]] = []
    for key in keys:
        entry = book[key]
        fields: Mapping[str, Any] = entry if isinstance(entry, dict) else {}
        pairs.append((to_float(fields.get("price")), to_float(fields.get("qty"))))
    return clean_levels(pairs, descending=descending)


def parse_price(body: bytes, listing: LiveListing, *, captured_ns: int) -> LiveSnapshot | None:
    """Read one ``tr_live_prices`` answer. ``None`` when it names another symbol.

    A price answer with no ``ltp`` is still a snapshot: its session figures stand, ``last`` is
    ``None`` and no trade is built. Without a book the snapshot has no ladder; the caller attaches
    one with :func:`parse_book`, and drops the snapshot if it is still empty. A 2xx body with no
    ``symbol`` is not this document and raises.
    """
    document = decode_json(SOURCE, body)
    symbol = document.get("symbol") if isinstance(document, dict) else None
    if not isinstance(symbol, str):
        raise LivePayloadError(SOURCE, "no symbol in the price answer")
    if symbol != listing.local_symbol:
        return None
    last = _positive(document.get("ltp"))
    last_qty = _positive(document.get("lastTradeQty"))
    return LiveSnapshot(
        source=SOURCE,
        listing=listing,
        symbol_raw=symbol,
        captured_ns=captured_ns,
        source_ts_ns=_stamp_ns(document.get("tsInMillis")),
        last=last,
        last_size=last_qty if last is not None else None,
        prev_close=_positive(document.get("close")),
        open=_positive(document.get("open")),
        high=_positive(document.get("high")),
        low=_positive(document.get("low")),
        volume=_non_negative(document.get("volume")),
        currency=_CURRENCY,
        price_scale=0,
    )


def parse_book(
    body: bytes, *, listing: LiveListing | None = None
) -> tuple[tuple[LiveLevel, ...], tuple[LiveLevel, ...]]:
    """Read one ``tr_live_book`` answer into ``(bids, asks)``, best first on each side.

    When ``listing`` is given and the book names another symbol, both sides are empty. A 2xx body
    without ``buyBook`` and ``sellBook`` objects is not this document and raises.
    """
    document = decode_json(SOURCE, body)
    if not isinstance(document, dict):
        raise LivePayloadError(SOURCE, "the book answer is not an object")
    buy, sell = document.get("buyBook"), document.get("sellBook")
    if not isinstance(buy, dict) or not isinstance(sell, dict):
        raise LivePayloadError(SOURCE, "no buyBook and sellBook in the book answer")
    if listing is not None and document.get("symbol") != listing.local_symbol:
        return (), ()
    return _ladder(buy, descending=True), _ladder(sell, descending=False)


class GrowwLive(LiveSource):
    name: ClassVar[str] = SOURCE
    usage_class: ClassVar[UsageClass] = UsageClass.INTERNAL
    attribution: ClassVar[str | None] = None
    official: ClassVar[bool] = False
    supported_mics: ClassVar[frozenset[str]] = frozenset({"XNSE"})
    max_tier: ClassVar[LiveTier] = LiveTier.L2
    depth_levels: ClassVar[int] = 5
    delay_s: ClassVar[int | None] = None
    has_tape: ClassVar[bool] = True
    batch_size: ClassVar[int] = 1
    min_interval_s: ClassVar[float] = 5.0

    async def snapshots(
        self, listings: Sequence[LiveListing], *, captured_ns: int
    ) -> list[LiveSnapshot]:
        out: list[LiveSnapshot] = []
        for listing in listings:
            if not self.serves(listing):
                continue
            symbol = quote(listing.local_symbol, safe="")
            price = await self._get(PRICE_URL.format(symbol=symbol))
            snapshot = parse_price(price.body, listing, captured_ns=captured_ns)
            if snapshot is None:
                continue
            try:
                book = await self._get(BOOK_URL.format(symbol=symbol))
                bids, asks = parse_book(book.body, listing=listing)
            except LivePayloadError:
                bids, asks = (), ()
            merged = replace(snapshot, bids=bids, asks=asks)
            if not merged.is_empty:
                out.append(merged)
        return out
