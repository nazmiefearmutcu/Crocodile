"""Deutsche Boerse's own public API behind boerse-frankfurt.de: Xetra and the Frankfurt floor.

Three GETs per listing, each keyed by ISIN and MIC (``isin=<isin>&mic=<mic>``):

- ``bid_ask_overview/single``: up to ten levels per side, best first, as ``bidPrice``/``bidUnits``
  and ``askPrice``/``askUnits``.
- ``quote_box/single``: the top of book (``bidLimit``/``bidSize``, ``askLimit``/``askSize``), the
  last price (``lastPrice``), the session open (``open``) and the stamp of the quote
  (``timestamp``, ISO-8601 with ``Z``).
- ``price_information/single``: the previous close (``closingPricePrevTradingDay``), the session
  high and low (``dayHigh``/``dayLow``), the cumulative volume (``turnoverInPieces``), the
  turnover (``turnoverInEur``), and the currency (``currency.originalValue``).

Units. Prices are in EUR, the quoted currency of both markets; the fixture's
``currency.originalValue`` says ``EUR``. Sizes are in shares, and no conversion is applied.
Evidence from the fixtures: the top bid's ``bidUnits`` (326 at 211.95) equals the quote box's
``bidSize`` (326 at the same instant), and ``turnoverInEur`` / ``turnoverInPieces`` is
11,383,590.25 / 53,634, about 212.25 EUR per piece, which is the traded price level. So one
piece is one share, not a board lot of 100.

Ruling: a limit of 0 in the quote box means "no quote" and is an empty slot (``clean_levels``
drops it). When the order-book overview has no usable level on either side, the one-level top
of book from the quote box is used instead, for both sides together, so a book is never built
from two different views. The Frankfurt floor's fixture has a quote box and no ladder, which is
why this fallback exists. Cost if wrong: a consumer would draw a ladder the venue did not
publish.

Ruling: the last price is the quote box's; the price information's last price is used only when
the quote box states none. The publisher states no size for the last print, so ``last_size`` is
``None``. Cost if wrong: a trade record would carry a size nobody published.

Ruling: the publisher does not state a delay, so ``delay_s`` is ``None``. The source publishes no
tape of its own prints. The snapshot's stamp is the quote box's ``timestamp``.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, ClassVar, Final

from crocodile.core.markets.usage import UsageClass
from crocodile.equity.universal.live.base import (
    LivePayloadError,
    LiveSource,
    decode_json,
    to_float,
)
from crocodile.equity.universal.live.model import (
    LiveLevel,
    LiveListing,
    LiveSnapshot,
    LiveTier,
    clean_levels,
)

__all__ = [
    "BOOK_URL",
    "PRICE_URL",
    "QUOTE_URL",
    "SOURCE",
    "BoerseFrankfurtLive",
    "PriceInformation",
    "QuoteBox",
    "compose",
    "parse_book",
    "parse_price_information",
    "parse_quote_box",
]

SOURCE: Final = "boerse_frankfurt"
BOOK_URL: Final = "https://api.boerse-frankfurt.de/v1/data/bid_ask_overview/single"
QUOTE_URL: Final = "https://api.boerse-frankfurt.de/v1/data/quote_box/single"
PRICE_URL: Final = "https://api.boerse-frankfurt.de/v1/data/price_information/single"
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_NS_PER_SECOND: Final = 1_000_000_000

type Book = tuple[tuple[LiveLevel, ...], tuple[LiveLevel, ...]]
"""(bids best first, asks best first)."""


@dataclass(frozen=True, slots=True)
class QuoteBox:
    isin: str | None
    bid_limit: float | None
    bid_size: float | None
    ask_limit: float | None
    ask_size: float | None
    last: float | None
    open: float | None
    ts_ns: int | None


@dataclass(frozen=True, slots=True)
class PriceInformation:
    last: float | None
    prev_close: float | None
    high: float | None
    low: float | None
    volume: float | None
    turnover: float | None
    currency: str | None


def _document(body: bytes) -> dict[str, Any]:
    document = decode_json(SOURCE, body)
    if not isinstance(document, dict):
        raise LivePayloadError(SOURCE, "answer is not a JSON object")
    return document


def _iso_ns(value: Any) -> int | None:
    """An ISO-8601 stamp with an explicit offset as epoch nanoseconds, else ``None``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    delta = moment - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * _NS_PER_SECOND + delta.microseconds * 1_000


def parse_book(body: bytes) -> Book:
    """Read one ``bid_ask_overview`` answer: bids best first, asks best first, sizes in shares."""
    rows = _document(body).get("data")
    if not isinstance(rows, list):
        raise LivePayloadError(SOURCE, "no data list in the order book")
    bids: list[tuple[float | None, float | None]] = []
    asks: list[tuple[float | None, float | None]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        bids.append((to_float(row.get("bidPrice")), to_float(row.get("bidUnits"))))
        asks.append((to_float(row.get("askPrice")), to_float(row.get("askUnits"))))
    return clean_levels(bids, descending=True), clean_levels(asks, descending=False)


def parse_quote_box(body: bytes) -> QuoteBox:
    """Read one ``quote_box`` answer: the top of book, the last price, the open and the stamp."""
    document = _document(body)
    if "isin" not in document:
        raise LivePayloadError(SOURCE, "no isin in the quote box")
    isin = document.get("isin")
    return QuoteBox(
        isin=isin if isinstance(isin, str) else None,
        bid_limit=to_float(document.get("bidLimit")),
        bid_size=to_float(document.get("bidSize")),
        ask_limit=to_float(document.get("askLimit")),
        ask_size=to_float(document.get("askSize")),
        last=to_float(document.get("lastPrice")),
        open=to_float(document.get("open")),
        ts_ns=_iso_ns(document.get("timestamp")),
    )


def parse_price_information(body: bytes) -> PriceInformation:
    """Read one ``price_information`` answer: previous close, session range, volume, currency."""
    document = _document(body)
    if "closingPricePrevTradingDay" not in document and "lastPrice" not in document:
        raise LivePayloadError(SOURCE, "no price fields in the price information")
    currency = document.get("currency")
    code = currency.get("originalValue") if isinstance(currency, dict) else None
    code_text = code.strip() if isinstance(code, str) else ""
    return PriceInformation(
        last=to_float(document.get("lastPrice")),
        prev_close=to_float(document.get("closingPricePrevTradingDay")),
        high=to_float(document.get("dayHigh")),
        low=to_float(document.get("dayLow")),
        volume=to_float(document.get("turnoverInPieces")),
        turnover=to_float(document.get("turnoverInEur")),
        currency=code_text if code_text else None,
    )


def compose(
    listing: LiveListing,
    captured_ns: int,
    book: Book | None,
    quote: QuoteBox,
    price: PriceInformation | None,
) -> LiveSnapshot | None:
    """One listing's snapshot from its three answers, or ``None`` when there is nothing to say.

    ``None`` comes back when the quote box is for another ISIN, or when no price at all was
    published. A missing ``book`` or ``price`` (its request failed as a payload error) leaves
    the fields it would have filled empty.
    """
    isin = listing.isin
    if isin is None or quote.isin != isin:
        return None
    bids: tuple[LiveLevel, ...] = ()
    asks: tuple[LiveLevel, ...] = ()
    if book is not None:
        bids, asks = book
    if not bids and not asks:
        bids = clean_levels([(quote.bid_limit, quote.bid_size)], descending=True)
        asks = clean_levels([(quote.ask_limit, quote.ask_size)], descending=False)
    price_last = price.last if price is not None else None
    snapshot = LiveSnapshot(
        source=SOURCE,
        listing=listing,
        symbol_raw=isin,
        captured_ns=captured_ns,
        source_ts_ns=quote.ts_ns,
        last=quote.last if quote.last is not None else price_last,
        prev_close=price.prev_close if price is not None else None,
        open=quote.open,
        high=price.high if price is not None else None,
        low=price.low if price is not None else None,
        volume=price.volume if price is not None else None,
        turnover=price.turnover if price is not None else None,
        bids=bids,
        asks=asks,
        currency=price.currency if price is not None else None,
        price_scale=0,
    )
    return None if snapshot.is_empty else snapshot


class BoerseFrankfurtLive(LiveSource):
    name: ClassVar[str] = SOURCE
    usage_class: ClassVar[UsageClass] = UsageClass.DISPLAY
    attribution: ClassVar[str | None] = "Source: Deutsche Boerse AG (boerse-frankfurt.de)"
    official: ClassVar[bool] = True
    supported_mics: ClassVar[frozenset[str]] = frozenset({"XETR", "XFRA"})
    max_tier: ClassVar[LiveTier] = LiveTier.L2
    depth_levels: ClassVar[int] = 10
    tier_overrides: ClassVar[Mapping[str, tuple[LiveTier, int]]] = MappingProxyType(
        {"XFRA": (LiveTier.L1, 1)}
    )
    delay_s: ClassVar[int | None] = None
    has_tape: ClassVar[bool] = False
    needs_isin: ClassVar[bool] = True
    batch_size: ClassVar[int] = 1
    min_interval_s: ClassVar[float] = 10.0

    async def snapshots(
        self, listings: Sequence[LiveListing], *, captured_ns: int
    ) -> list[LiveSnapshot]:
        out: list[LiveSnapshot] = []
        for listing in listings:
            isin = listing.isin
            if isin is None or not self.serves(listing):
                continue
            params = (("isin", isin), ("mic", listing.mic))
            book: Book | None
            try:
                book = parse_book((await self._get(BOOK_URL, params=params)).body)
            except LivePayloadError:
                book = None
            quote = parse_quote_box((await self._get(QUOTE_URL, params=params)).body)
            price: PriceInformation | None
            try:
                price = parse_price_information((await self._get(PRICE_URL, params=params)).body)
            except LivePayloadError:
                price = None
            snapshot = compose(listing, captured_ns, book, quote, price)
            if snapshot is not None:
                out.append(snapshot)
        return out
