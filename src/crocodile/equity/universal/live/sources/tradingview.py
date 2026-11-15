"""TradingView's scanner as a live source: the session's figures for many listings at once.

One POST to ``scanner.tradingview.com/global/scan`` with a ``symbols.tickers`` list answers for
every listed ticker in one page: the last price (``close``), the session's open, high, low and
volume, the quote unit, and ``update_mode``, the scanner's own statement of how fresh the row is
(``streaming``, ``delayed_streaming_900`` for a fifteen-minute delay, ``endofday``). The scanner
publishes no resting orders for equities (its ``bid`` and ``ask`` columns are null outside
forex), so the tier is ``LAST``.

It is the widest batch source of the live layer: a watch list of a hundred listings across
twenty markets is one request. That is what the quote and mover tables of a terminal are filled
from where no deeper source exists.

Ruling: ``update_mode`` decides ``delay_s`` per row. ``endofday`` and unknown modes leave it
``None``: the row is the last completed session's state, not a delayed live one.

Ruling: the scanner's ``time`` column is the start of the bar the row describes, not the time of
the last trade, so it is not used as ``source_ts``. Cost if wrong: every row would carry the
session open as the time of its last print.

Ruling: a row's ``isin`` is copied onto the snapshot's listing only when it passes the ISO 6166
check digit and the caller's listing carried none. That is how a consumer that knows a listing
by symbol alone learns the ISIN the ISIN-keyed publishers need. An ISIN names the instrument,
never the listing, so it is never used to decide which row belongs to which listing.

Ruling: the scanner is an unofficial aggregator whose terms are not read; usage is ``INTERNAL``
by the same user decision that covers the universe source (spec section 0).

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, ClassVar, Final

from crocodile.core.markets.currency import parse_quote_unit
from crocodile.core.markets.errors import MarketError, UnknownCurrencyError
from crocodile.core.markets.grammar import GrammarError, PrefixGrammar, load_grammar
from crocodile.core.markets.usage import UsageClass
from crocodile.core.ratelimit import budget
from crocodile.equity.universal.base import is_valid_isin
from crocodile.equity.universal.live.base import (
    LivePayloadError,
    LiveSource,
    decode_json,
    to_float,
)
from crocodile.equity.universal.live.model import LiveListing, LiveSnapshot, LiveTier

__all__ = [
    "COLUMNS",
    "HOST",
    "MARKET_SCAN_URL",
    "SCAN_URL",
    "SOURCE",
    "TradingViewLive",
    "board_body",
    "delay_of",
    "parse_board",
    "parse_scan",
    "scan_body",
    "ticker_of",
]

SOURCE: Final = "tradingview"
HOST: Final = "scanner.tradingview.com"
SCAN_URL: Final = "https://scanner.tradingview.com/global/scan"
MARKET_SCAN_URL: Final = "https://scanner.tradingview.com/{market}/scan"
COLUMNS: Final[tuple[str, ...]] = (
    "name",
    "description",
    "currency",
    "close",
    "open",
    "high",
    "low",
    "volume",
    "change",
    "update_mode",
    "Value.Traded",
    "isin",
)
_DELAYED: Final = re.compile(r"delayed_streaming_(\d+)")


def _grammar() -> PrefixGrammar:
    grammar = load_grammar(SOURCE)
    assert isinstance(grammar, PrefixGrammar)
    return grammar


def ticker_of(listing: LiveListing) -> str | None:
    """TradingView's ``CODE:LOCAL`` spelling of a listing, or ``None`` without a grammar row."""
    try:
        return _grammar().to_source(listing.local_symbol, listing.mic)
    except (GrammarError, MarketError):
        return None


def delay_of(update_mode: object) -> int | None:
    """Seconds of delay an ``update_mode`` states; ``None`` for end-of-day and unknown modes."""
    if update_mode == "streaming":
        return 0
    if isinstance(update_mode, str):
        match = _DELAYED.fullmatch(update_mode)
        if match is not None:
            return int(match.group(1))
    return None


def scan_body(tickers: Sequence[str]) -> dict[str, Any]:
    return {"columns": list(COLUMNS), "symbols": {"tickers": list(tickers)}}


def board_body(market_codes: Sequence[str], limit: int) -> dict[str, Any]:
    """The scan of one exchange's listings, most traded first."""
    return {
        "columns": list(COLUMNS),
        "filter": [{"left": "exchange", "operation": "in_range", "right": list(market_codes)}],
        "sort": {"sortBy": "volume", "sortOrder": "desc"},
        "range": [0, limit],
    }


def _listing_of_ticker(ticker: str, mic: str) -> LiveListing | None:
    try:
        local, found = _grammar().from_source(ticker)
    except (GrammarError, MarketError):
        return None
    return LiveListing(mic=found, local_symbol=local) if found == mic else None


def parse_board(body: bytes, mic: str, *, captured_ns: int) -> list[LiveSnapshot]:
    """Read one exchange scan into snapshots of every row that belongs to ``mic``."""
    return _parse(body, None, mic, captured_ns)


def parse_scan(
    body: bytes, listings: Sequence[LiveListing], *, captured_ns: int
) -> list[LiveSnapshot]:
    """Read one scan answer into snapshots for the requested listings."""
    wanted: dict[str, LiveListing] = {}
    for listing in listings:
        ticker = ticker_of(listing)
        if ticker is not None:
            wanted[ticker] = listing
    return _parse(body, wanted, None, captured_ns)


def _parse(
    body: bytes, wanted: dict[str, LiveListing] | None, mic: str | None, captured_ns: int
) -> list[LiveSnapshot]:
    document = decode_json(SOURCE, body)
    rows = document.get("data") if isinstance(document, dict) else None
    if rows is None and isinstance(document, dict) and document.get("totalCount") == 0:
        return []
    if not isinstance(rows, list):
        raise LivePayloadError(SOURCE, "no data array in the scan answer")
    out: list[LiveSnapshot] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = row.get("s")
        values = row.get("d")
        if not isinstance(ticker, str) or not isinstance(values, list):
            continue
        if wanted is not None:
            listing_or_none = wanted.get(ticker)
        else:
            listing_or_none = _listing_of_ticker(ticker, mic) if mic is not None else None
        if listing_or_none is None or len(values) != len(COLUMNS):
            continue
        cells = dict(zip(COLUMNS, values, strict=True))
        currency: str | None = None
        price_scale: int | None = None
        unit = cells["currency"]
        if isinstance(unit, str):
            try:
                parsed = parse_quote_unit(unit)
            except UnknownCurrencyError:
                parsed = None
            if parsed is not None:
                currency, price_scale = parsed.currency, parsed.price_scale
        last = to_float(cells["close"])
        change = to_float(cells["change"])
        prev_close: float | None = None
        if last is not None and change is not None and change > -100.0:
            prev_close = round(last / (1.0 + change / 100.0), 6)
        description = cells["description"]
        isin = cells["isin"]
        if listing_or_none.isin is None and isinstance(isin, str) and is_valid_isin(isin):
            listing_or_none = LiveListing(
                mic=listing_or_none.mic, local_symbol=listing_or_none.local_symbol, isin=isin
            )
        snapshot = LiveSnapshot(
            source=SOURCE,
            listing=listing_or_none,
            symbol_raw=ticker,
            captured_ns=captured_ns,
            last=last,
            prev_close=prev_close,
            open=to_float(cells["open"]),
            high=to_float(cells["high"]),
            low=to_float(cells["low"]),
            volume=to_float(cells["volume"]),
            turnover=to_float(cells["Value.Traded"]),
            currency=currency,
            price_scale=price_scale,
            delay_s=delay_of(cells["update_mode"]),
            name=description if isinstance(description, str) else None,
        )
        if not snapshot.is_empty:
            out.append(snapshot)
    return out


class TradingViewLive(LiveSource):
    name: ClassVar[str] = SOURCE
    usage_class: ClassVar[UsageClass] = UsageClass.INTERNAL
    attribution: ClassVar[str | None] = None
    official: ClassVar[bool] = False
    supported_mics: ClassVar[frozenset[str]] = frozenset()
    max_tier: ClassVar[LiveTier] = LiveTier.LAST
    depth_levels: ClassVar[int] = 0
    delay_s: ClassVar[int | None] = None
    has_tape: ClassVar[bool] = False
    batch_size: ClassVar[int] = 200
    min_interval_s: ClassVar[float] = 15.0

    enumerates: ClassVar[bool] = True

    def serves(self, listing: LiveListing) -> bool:
        return ticker_of(listing) is not None

    async def board(
        self, mic: str, *, captured_ns: int, limit: int | None = None
    ) -> list[LiveSnapshot]:
        """The most traded listings of ``mic`` (at most ``limit``, default 150), one request."""
        grammar = _grammar()
        codes = sorted(row.code for row in grammar.rows if row.mic == mic)
        if not codes:
            return []
        await budget(SOURCE, HOST).acquire()
        url = MARKET_SCAN_URL.format(market=grammar.market_for(mic))
        response = await self._post(url, board_body(codes, limit if limit is not None else 150))
        return parse_board(response.body, mic, captured_ns=captured_ns)

    async def snapshots(
        self, listings: Sequence[LiveListing], *, captured_ns: int
    ) -> list[LiveSnapshot]:
        served = [listing for listing in listings if self.serves(listing)]
        tickers = [ticker for ticker in map(ticker_of, served) if ticker is not None]
        if not tickers:
            return []
        await budget(SOURCE, HOST).acquire()
        response = await self._post(SCAN_URL, scan_body(tickers))
        return parse_scan(response.body, served, captured_ns=captured_ns)


def _supported() -> frozenset[str]:
    return _grammar().supported_mics()


TradingViewLive.supported_mics = _supported()
