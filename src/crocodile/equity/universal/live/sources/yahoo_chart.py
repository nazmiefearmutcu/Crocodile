"""Yahoo's chart endpoint as a live source: the last price and today's one-minute bars.

The chart endpoint that :mod:`crocodile.equity.universal.yahoo_chart` reads for history also
states, in its ``meta`` block, where a listing is now: the last regular-market price and its
time, the session's high, low and cumulative volume, and the previous close. Asked for
``interval=1m&range=1d`` it adds the session's one-minute bars. This source reads both from one
request. It publishes no resting orders, so its tier is ``LAST``.

It serves every market the Yahoo grammar covers, which makes it the floor of most markets'
chains: where no publisher exposes a book, this is what says a listing is trading at all, and
its bars are what a volume-at-price profile is built from.

Ruling: the open is the first bar's open, and only when the bars start inside the session the
``meta`` block calls current. Cost if wrong: a stale bar from an earlier day would be reported as
today's open.

Ruling: the price unit is read from ``meta.currency`` exactly (``GBp`` is pence: currency
``GBP``, ``price_scale`` 2). An unknown spelling leaves both unset; it is never read as a major
unit. Cost if wrong: a pence listing would be priced a hundred times too high.

Ruling: Yahoo is an unofficial aggregator whose terms prohibit automated access; usage is
``INTERNAL`` by the same user decision that covers the history source (spec section 0).

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import csv
import functools
import importlib.resources
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, ClassVar, Final
from urllib.parse import quote

from crocodile.core.markets.currency import parse_quote_unit
from crocodile.core.markets.errors import MarketError, UnknownCurrencyError
from crocodile.core.markets.grammar import GrammarError, SuffixGrammar, load_grammar
from crocodile.core.markets.usage import UsageClass
from crocodile.core.ratelimit import budget
from crocodile.equity.universal.live.base import (
    LivePayloadError,
    LiveSource,
    decode_json,
    seconds_ns,
    to_float,
)
from crocodile.equity.universal.live.model import LiveListing, LiveSnapshot, LiveTier

__all__ = [
    "CHART_URL",
    "HOST",
    "SOURCE",
    "MinuteBar",
    "YahooChartLive",
    "extra_suffixes",
    "parse_live_chart",
    "parse_minute_bars",
    "yahoo_symbol",
]

SOURCE: Final = "yahoo_chart"
HOST: Final = "query2.finance.yahoo.com"
CHART_URL: Final = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
_NS_PER_SECOND: Final = 1_000_000_000
_PACKAGE: Final = "crocodile.equity.universal.live"
_EXTRA_RESOURCE: Final = "data/yahoo_extra.csv"


@dataclass(frozen=True, slots=True)
class MinuteBar:
    """One bar of the session as the chart states it. ``ts_ns`` is the bar's start, UTC."""

    ts_ns: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def _grammar() -> SuffixGrammar:
    grammar = load_grammar(SOURCE)
    assert isinstance(grammar, SuffixGrammar)
    return grammar


@functools.cache
def extra_suffixes() -> Mapping[str, str]:
    """Markets the live layer reads from Yahoo beyond the history grammar: MIC to suffix.

    ``data/yahoo_extra.csv`` holds suffixes verified by a live chart request whose ``meta``
    named the exchange, in the same evidence form as the grammar table. They live beside the
    live layer rather than in ``grammar_yahoo_chart.csv`` because that table is generated from
    the research matrix and drives the daily-history pipeline; a row here only lets a live
    read ask the chart endpoint about a listing of that market.
    """
    raw = importlib.resources.files(_PACKAGE).joinpath(_EXTRA_RESOURCE).read_text("ascii")
    table: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(raw)):
        mic, suffix = row["mic"], row["suffix"]
        if not suffix.startswith(".") or mic in table:
            raise ValueError(f"bad yahoo_extra.csv row for {mic!r}")
        table[mic] = suffix
    return MappingProxyType(table)


def yahoo_symbol(listing: LiveListing) -> str | None:
    """Yahoo's spelling of a listing, or ``None`` when no table has a row for its market."""
    try:
        return _grammar().to_source(listing.local_symbol, listing.mic)
    except (GrammarError, MarketError):
        suffix = extra_suffixes().get(listing.mic)
        return f"{listing.local_symbol}{suffix}" if suffix is not None else None


def _result(source: str, body: bytes) -> Mapping[str, Any] | None:
    document = decode_json(source, body)
    chart = document.get("chart") if isinstance(document, dict) else None
    if not isinstance(chart, dict):
        raise LivePayloadError(source, "no chart object in the answer")
    results = chart.get("result")
    if results is None:
        return None
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise LivePayloadError(source, "chart.result is not a non-empty list of objects")
    first: Mapping[str, Any] = results[0]
    return first


def parse_minute_bars(body: bytes) -> list[MinuteBar]:
    """The bars of one chart answer, oldest first. Bars with any missing number are dropped."""
    result = _result(SOURCE, body)
    if result is None:
        return []
    stamps = result.get("timestamp")
    indicators = result.get("indicators")
    quotes = indicators.get("quote") if isinstance(indicators, dict) else None
    if not isinstance(stamps, list) or not isinstance(quotes, list) or not quotes:
        return []
    block = quotes[0]
    if not isinstance(block, dict):
        return []
    columns = [block.get(key) for key in ("open", "high", "low", "close", "volume")]
    if not all(isinstance(column, list) and len(column) == len(stamps) for column in columns):
        return []
    bars: list[MinuteBar] = []
    for index, stamp in enumerate(stamps):
        ts_ns = seconds_ns(stamp)
        values = [to_float(column[index]) for column in columns if isinstance(column, list)]
        if ts_ns is None or len(values) != 5 or any(value is None for value in values):
            continue
        o, h, lo, c, v = (float(value) for value in values if value is not None)
        if min(o, h, lo, c) <= 0.0 or v < 0.0:
            continue
        bars.append(MinuteBar(ts_ns=ts_ns, open=o, high=h, low=lo, close=c, volume=v))
    return bars


def _session_start_ns(meta: Mapping[str, Any]) -> int | None:
    period = meta.get("currentTradingPeriod")
    regular = period.get("regular") if isinstance(period, dict) else None
    return seconds_ns(regular.get("start")) if isinstance(regular, dict) else None


def parse_live_chart(
    body: bytes, listing: LiveListing, *, symbol_raw: str, captured_ns: int
) -> LiveSnapshot | None:
    """Read one ``interval=1m&range=1d`` answer. ``None`` when Yahoo has no such symbol."""
    result = _result(SOURCE, body)
    if result is None:
        return None
    meta = result.get("meta")
    if not isinstance(meta, dict):
        raise LivePayloadError(SOURCE, "chart result has no meta block")
    currency: str | None = None
    price_scale: int | None = None
    unit = meta.get("currency")
    if isinstance(unit, str):
        try:
            parsed = parse_quote_unit(unit)
        except UnknownCurrencyError:
            parsed = None
        if parsed is not None:
            currency, price_scale = parsed.currency, parsed.price_scale
    opened: float | None = None
    start_ns = _session_start_ns(meta)
    bars = parse_minute_bars(body)
    if bars and start_ns is not None and bars[0].ts_ns >= start_ns:
        opened = bars[0].open
    name = meta.get("longName") or meta.get("shortName")
    snapshot = LiveSnapshot(
        source=SOURCE,
        listing=listing,
        symbol_raw=symbol_raw,
        captured_ns=captured_ns,
        source_ts_ns=seconds_ns(meta.get("regularMarketTime")),
        last=to_float(meta.get("regularMarketPrice")),
        prev_close=to_float(meta.get("chartPreviousClose", meta.get("previousClose"))),
        open=opened,
        high=to_float(meta.get("regularMarketDayHigh")),
        low=to_float(meta.get("regularMarketDayLow")),
        volume=to_float(meta.get("regularMarketVolume")),
        currency=currency,
        price_scale=price_scale,
        name=name if isinstance(name, str) else None,
    )
    return None if snapshot.is_empty else snapshot


class YahooChartLive(LiveSource):
    name: ClassVar[str] = SOURCE
    usage_class: ClassVar[UsageClass] = UsageClass.INTERNAL
    attribution: ClassVar[str | None] = None
    official: ClassVar[bool] = False
    supported_mics: ClassVar[frozenset[str]] = frozenset()
    max_tier: ClassVar[LiveTier] = LiveTier.LAST
    depth_levels: ClassVar[int] = 0
    delay_s: ClassVar[int | None] = None
    has_tape: ClassVar[bool] = False
    batch_size: ClassVar[int] = 1
    min_interval_s: ClassVar[float] = 15.0

    def serves(self, listing: LiveListing) -> bool:
        return yahoo_symbol(listing) is not None

    async def chart_body(self, listing: LiveListing) -> tuple[str, bytes] | None:
        """One ``interval=1m&range=1d`` request: ``(Yahoo symbol, body)``, ``None`` on a 404."""
        spelled = yahoo_symbol(listing)
        if spelled is None:
            return None
        await budget(SOURCE, HOST).acquire()
        url = CHART_URL.format(symbol=quote(spelled, safe=""))
        try:
            response = await self._get(url, params=(("interval", "1m"), ("range", "1d")))
        except LivePayloadError as exc:
            if "HTTP 404" in exc.detail:
                return None
            raise
        return spelled, response.body

    async def snapshots(
        self, listings: Sequence[LiveListing], *, captured_ns: int
    ) -> list[LiveSnapshot]:
        out: list[LiveSnapshot] = []
        for listing in listings:
            answer = await self.chart_body(listing)
            if answer is None:
                continue
            spelled, body = answer
            snapshot = parse_live_chart(body, listing, symbol_raw=spelled, captured_ns=captured_ns)
            if snapshot is not None:
                out.append(snapshot)
        return out

    async def minute_bars(self, listing: LiveListing) -> list[MinuteBar]:
        """Today's one-minute bars of a listing, oldest first; empty when Yahoo has none."""
        answer = await self.chart_body(listing)
        return [] if answer is None else parse_minute_bars(answer[1])


def _supported() -> frozenset[str]:
    return _grammar().supported_mics() | frozenset(extra_suffixes())


YahooChartLive.supported_mics = _supported()
