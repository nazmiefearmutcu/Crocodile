"""Yahoo Finance chart endpoint as a universal source (``yahoo_chart``).

Ruling: the chart endpoint is a keyless, unofficial aggregator. Yahoo's terms forbid automated
access; this source is on by default by user decision and is declared ``usage_class=INTERNAL``
(spec section 0 and 3.10a). Nothing here bypasses a consent, bot, CAPTCHA or geo wall.

Ruling: Yahoo daily bars use the `native` basis; no `yahoo_chart_daily` is registered. The chart
endpoint publishes the session's bar itself; the only transformation is re-anchoring its
open-instant stamp to UTC midnight of the exchange-local session date, a change of encoding of the
same fact (the codebase's own `_expiry_to_ns` ruling, `yahoo/client.py:179-205`). A second basis
would have to either return a constant 1.0 -- `native` under another name, which Gate 3c
(`tests/conformance/test_gates.py:1950-2268`) would make us declare in `CONSTANT_BY_DEFINITION`
with the same argument -- or invent a formula with nothing observable to divide.
`source='yahoo_chart'` already separates these rows, and `adjustment` carries the split fact.
Cost if wrong: a later need
to filter Yahoo daily bars by basis means registering the basis then and re-labelling nothing
already written (old rows stay `native`, which remains true).

Ruling 2: an unknown ``exchangeName`` is not a contradiction. It is written with
``exchange_name_verified=False``; only a name that the table knows and that names another MIC
rejects the response.

Ruling 3: a missing currency refuses the response rather than writing ``NULL`` units.

Ruling 4: duplicate session rows keep the later timestamp (Yahoo appends a trailing row stamped
``regularMarketTime``), and the other row is counted in ``dropped_duplicate_dates``.

Ruling 5: Yahoo ToS forbids automated access; the source is on by default by user decision with
``usage_class=INTERNAL`` (spec section 0, 3.10a).
"""

from __future__ import annotations

import csv
import functools
import importlib.resources
import io
import json
import logging
import math
import time
from collections.abc import AsyncIterator, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Final
from urllib.parse import quote
from zoneinfo import ZoneInfo

import msgspec

from crocodile.core.markets.calendar import calendar_for
from crocodile.core.markets.currency import (
    QuoteUnit,
    check_currency_consistent,
    parse_quote_unit,
)
from crocodile.core.markets.errors import (
    CalendarRangeError,
    CurrencyMismatchError,
    NoGrammarForMarket,
)
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.grammar import (
    AmbiguousSourceSymbol,
    SuffixGrammar,
    UnknownSourceSymbol,
    load_grammar,
)
from crocodile.core.markets.master import ListingMaster
from crocodile.core.markets.sanctions import allowed_countries, require_not_sanctioned
from crocodile.core.markets.symbols import US_COUNTRY, listing_symbol, parse_listing_symbol
from crocodile.core.markets.usage import UsageClass
from crocodile.core.ratelimit import budget
from crocodile.core.ratelimit.token_bucket import TokenBucket
from crocodile.core.schema.enums import (
    CHANNEL_SUCCESSORS,
    AssetClass,
    CorpActionType,
    PriceAdjustment,
    SecurityType,
)
from crocodile.core.schema.provenance import provenance_fields
from crocodile.core.schema.records import OHLCV, CorporateAction, Record
from crocodile.equity.providers.base import (
    ProviderError,
    PullProvider,
    TransientProviderError,
    require_supported_mic,
)
from crocodile.equity.universal.base import UnservableReason, daily_bar_ts_ns, session_date_of
from crocodile.equity.universal.http import (
    AiohttpFetcher,
    Fetcher,
    HttpRequest,
    HttpResponse,
    raise_if_unservable,
)

if TYPE_CHECKING:
    from crocodile.core.config import Settings
    from crocodile.core.sink.base import Sink
    from crocodile.equity.reference.registry import InstrumentRegistry

log = logging.getLogger(__name__)

SOURCE: Final = "yahoo_chart"
HOST: Final = "query2.finance.yahoo.com"
CHART_URL: Final = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
EVENTS_PARAM: Final = "div,split"
DAILY_INTERVAL: Final = "1d"
INTRADAY_WINDOWS: Final[Mapping[str, timedelta]] = MappingProxyType(
    {
        "1m": timedelta(days=7),
        "5m": timedelta(days=60),
        "15m": timedelta(days=60),
        "30m": timedelta(days=60),
        "1h": timedelta(days=730),
    }
)
YAHOO_INTERVAL: Final[Mapping[str, str]] = MappingProxyType(
    {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m", "1d": "1d"}
)
FULL_HISTORY: Final = timedelta(days=99 * 365)
DEFAULT_BACKOFF_S: Final = 5.0
# Measured 2026-11-11 (task 10b, live: one NVDA chart request, HTTP 200, exchangeName NMS, USD).
# Raw close 2024-06-07 = 120.888 (New York session) is below the 200 threshold of the decision
# rule (< 200 SPLIT, > 1000 NONE, else None), and events.splits carries the 10:1 split of
# 2024-06-10, so the published daily closes are split-adjusted. Pinned by
# tests/equity/universal/test_yahoo_chart.py::test_the_adjustment_constant_is_the_measured_value.
YAHOO_CHART_ADJUSTMENT: Final[PriceAdjustment | None] = PriceAdjustment.SPLIT

_NS_PER_SECOND: Final = 1_000_000_000
_NS_PER_MICROSECOND: Final = 1_000
_EXCHANGE_NAMES_RESOURCE: Final = "data/yahoo_chart_exchange_names.csv"
_SECURITY_TYPES: Final[Mapping[str, SecurityType]] = MappingProxyType(
    {
        "EQUITY": SecurityType.CS,
        "ETF": SecurityType.ETF,
        "MUTUALFUND": SecurityType.FUND,
        "INDEX": SecurityType.INDEX,
    }
)


class ChartPayloadError(ProviderError):
    """A chart payload that is malformed or internally inconsistent."""


class ChartRequestError(ProviderError):
    """Yahoo rejected the request itself (HTTP 400 or 422); carries Yahoo's own description."""


class ExchangeNameMismatchError(ProviderError):
    """``meta.exchangeName`` names a different MIC than the one requested."""


@dataclass(frozen=True, slots=True)
class ChartListing:
    """One listing as the chart endpoint sees it."""

    symbol: str
    local: str
    mics: frozenset[str]
    yahoo_symbol: str
    tz: ZoneInfo
    calendar_mic: str


@dataclass(frozen=True, slots=True)
class ChartParse:
    """The records of one parsed payload and the counts of everything that was dropped."""

    records: tuple[Record, ...]
    quote_unit: QuoteUnit
    security_type: SecurityType
    exchange_name: str | None
    exchange_name_verified: bool
    dropped_incomplete: int
    dropped_duplicate_dates: int
    dropped_null_rows: int
    dropped_outside_window: int
    skipped_events: int


class ChartStatus(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class ChartResult:
    listing: ChartListing
    status: ChartStatus
    http_status: int
    parse: ChartParse | None


@functools.cache
def load_exchange_names() -> Mapping[str, frozenset[str]]:
    """Yahoo ``exchangeName`` -> the operating MICs it is evidenced to name (cached)."""
    text = (
        importlib.resources.files("crocodile.equity.universal")
        .joinpath(_EXCHANGE_NAMES_RESOURCE)
        .read_text(encoding="utf-8")
    )
    names: dict[str, set[str]] = {}
    for row in csv.DictReader(io.StringIO(text)):
        names.setdefault(row["exchange_name"], set()).add(row["mic"])
    return MappingProxyType({name: frozenset(mics) for name, mics in names.items()})


def security_type_for(instrument_type: str | None) -> SecurityType:
    """Map Yahoo ``meta.instrumentType`` to the reference vocabulary; anything else is UNKNOWN."""
    if instrument_type is None:
        return SecurityType.UNKNOWN
    return _SECURITY_TYPES.get(instrument_type, SecurityType.UNKNOWN)


def _ns(span: timedelta) -> int:
    """A whole-microsecond span as nanoseconds."""
    return (span // timedelta(microseconds=1)) * _NS_PER_MICROSECOND


def intraday_window(interval: str, captured_at_ns: int) -> tuple[int, int]:
    """(earliest, latest) instants Yahoo can serve for an intraday ``interval``."""
    span = INTRADAY_WINDOWS.get(interval)
    if span is None:
        raise ValueError(f"{interval!r} has no intraday window; the intraday intervals are "
                         f"{sorted(INTRADAY_WINDOWS)}")
    return captured_at_ns - _ns(span), captured_at_ns


def full_history_window(captured_at_ns: int) -> tuple[int, int]:
    """(earliest, latest) instants of a full-history daily request."""
    return captured_at_ns - _ns(FULL_HISTORY), captured_at_ns


_INTERVAL_SECONDS: Final[Mapping[str, int]] = MappingProxyType(
    {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "1d": 86_400}
)
_WARNED_EXCHANGE_NAMES: set[str] = set()


@dataclass(frozen=True, slots=True)
class _Row:
    ts: int
    source_ts: int
    values: tuple[float, float, float, float, float]


def _result(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    chart = payload.get("chart")
    if not isinstance(chart, Mapping):
        raise ChartPayloadError("the payload has no chart object")
    results = chart.get("result")
    if not isinstance(results, list) or not results or not isinstance(results[0], Mapping):
        raise ChartPayloadError("the payload carries no result object")
    return results[0]


def _quote_unit(meta: Mapping[str, Any]) -> QuoteUnit:
    raw = meta.get("currency")
    if not isinstance(raw, str) or not raw:
        raise ChartPayloadError("the payload names no currency; refusing rather than writing NULL")
    return parse_quote_unit(raw)


def _check_exchange_name(
    meta: Mapping[str, Any],
    listing: ChartListing,
    table: Mapping[str, frozenset[str]],
) -> tuple[str | None, bool]:
    raw = meta.get("exchangeName")
    name = raw if isinstance(raw, str) and raw else None
    if name is None:
        return None, False
    mics = table.get(name)
    if mics is None:
        if name not in _WARNED_EXCHANGE_NAMES:
            _WARNED_EXCHANGE_NAMES.add(name)
            log.warning("yahoo_chart exchangeName %r is not in the evidence table", name)
        return name, False
    if not (mics & listing.mics):
        raise ExchangeNameMismatchError(
            f"yahoo_chart exchangeName {name!r} names {sorted(mics)}, not {sorted(listing.mics)}"
        )
    return name, True


def _columns(result: Mapping[str, Any]) -> tuple[list[Any], list[list[Any]]]:
    """The timestamp list and the open/high/low/close/volume columns, all of one length."""
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators")
    quotes = indicators.get("quote") if isinstance(indicators, Mapping) else None
    quote: Mapping[str, Any] = quotes[0] if isinstance(quotes, list) and quotes else {}
    if not isinstance(timestamps, list):
        raise ChartPayloadError("the timestamp field is not a list")
    columns: list[list[Any]] = []
    for key in ("open", "high", "low", "close", "volume"):
        column = quote.get(key) or []
        if not isinstance(column, list) or len(column) != len(timestamps):
            raise ChartPayloadError(f"the {key} column does not match the timestamp list")
        columns.append(column)
    return timestamps, columns


def _as_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ChartPayloadError(f"a price or volume is not a number: {value!r}")
    return float(value)


def parse_chart(
    payload: Mapping[str, Any],
    listing: ChartListing,
    *,
    interval: str,
    captured_at_ns: int,
    through_session: date | None = None,
    window: tuple[int, int] | None = None,
    exchange_names: Mapping[str, frozenset[str]] | None = None,
) -> ChartParse:
    """Parse one decoded chart payload into OHLCV records (and, for daily, corporate actions).

    Pure: no clock (``captured_at_ns`` is passed in) and no network. Daily bars are stamped at
    UTC midnight of the exchange-local session date; intraday bars keep their own instant.
    """
    if interval not in YAHOO_INTERVAL:
        raise ValueError(f"interval {interval!r} is not one of {sorted(YAHOO_INTERVAL)}")
    result = _result(payload)
    meta = result.get("meta")
    if not isinstance(meta, Mapping):
        raise ChartPayloadError("the result carries no meta object")
    if meta.get("dataGranularity") != YAHOO_INTERVAL[interval]:
        raise ChartPayloadError(
            f"dataGranularity {meta.get('dataGranularity')!r} is not {YAHOO_INTERVAL[interval]!r}"
        )
    quote_unit = _quote_unit(meta)
    table = load_exchange_names() if exchange_names is None else exchange_names
    exchange_name, verified = _check_exchange_name(meta, listing, table)
    security_type = security_type_for(meta.get("instrumentType"))
    timestamps, columns = _columns(result)
    opens, highs, lows, closes, volumes = columns

    daily = interval == DAILY_INTERVAL
    interval_s = _INTERVAL_SECONDS[interval]
    captured_s = captured_at_ns // _NS_PER_SECOND
    dropped_incomplete = dropped_duplicate = dropped_null = dropped_outside = 0
    by_key: dict[int, _Row] = {}
    for index, ts in enumerate(timestamps):
        raw_values = (opens[index], highs[index], lows[index], closes[index], volumes[index])
        if any(value is None for value in raw_values):
            dropped_null += 1
            continue
        values = tuple(_as_number(value) for value in raw_values)
        if not isinstance(ts, int) or isinstance(ts, bool):
            raise ChartPayloadError(f"a timestamp is not an integer: {ts!r}")
        if daily:
            session = session_date_of(ts, listing.tz)
            if through_session is not None and session > through_session:
                dropped_incomplete += 1
                continue
            source_ts = daily_bar_ts_ns(session)
        else:
            if ts + interval_s > captured_s:
                dropped_incomplete += 1
                continue
            source_ts = ts * _NS_PER_SECOND
        if window is not None and not window[0] <= source_ts <= window[1]:
            dropped_outside += 1
            continue
        row_values = (values[0], values[1], values[2], values[3], values[4])
        previous = by_key.get(source_ts)
        if previous is None or ts > previous.ts:
            if previous is not None:
                dropped_duplicate += 1
            by_key[source_ts] = _Row(ts=ts, source_ts=source_ts, values=row_values)
        else:
            dropped_duplicate += 1

    tail = provenance_fields("native")
    bars: list[Record] = []
    for source_ts in sorted(by_key):
        row = by_key[source_ts]
        open_, high, low, close, volume = row.values
        bars.append(
            OHLCV(
                source=SOURCE,
                symbol=listing.symbol,
                symbol_raw=listing.yahoo_symbol,
                source_ts=source_ts,
                local_ts=captured_at_ns,
                asset_class=AssetClass.EQUITY,
                prov=tail.prov,
                prov_basis=tail.prov_basis,
                prov_confidence=tail.prov_confidence,
                prov_inputs=tail.prov_inputs,
                interval=interval,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
                currency=quote_unit.currency,
                price_scale=quote_unit.price_scale,
                adjustment=YAHOO_CHART_ADJUSTMENT if daily else None,
            )
        )

    events: tuple[Record, ...] = ()
    skipped_events = 0
    if daily and "events" in result:
        actions, skipped_events = parse_corporate_actions(
            payload, listing, captured_at_ns=captured_at_ns, quote_unit=quote_unit, window=window
        )
        events = tuple(actions)
    return ChartParse(
        records=tuple(bars) + events,
        quote_unit=quote_unit,
        security_type=security_type,
        exchange_name=exchange_name,
        exchange_name_verified=verified,
        dropped_incomplete=dropped_incomplete,
        dropped_duplicate_dates=dropped_duplicate,
        dropped_null_rows=dropped_null,
        dropped_outside_window=dropped_outside,
        skipped_events=skipped_events,
    )


def _number(value: object) -> float | None:
    """The value as a float when it is a JSON number (a bool is not one), else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def parse_corporate_actions(
    payload: Mapping[str, Any],
    listing: ChartListing,
    *,
    captured_at_ns: int,
    quote_unit: QuoteUnit,
    window: tuple[int, int] | None = None,
) -> tuple[tuple[CorporateAction, ...], int]:
    """Dividends and splits of one payload, plus the count of malformed entries skipped.

    A dividend is dated by its own ``date`` entry in the listing's zone (never by the key of the
    ``dividends`` map). A split is a ratio: ``value`` is new shares per old share, and it carries
    no currency or price scale. Pure: no clock and no network.
    """
    result = _result(payload)
    events = result.get("events")
    if not isinstance(events, Mapping):
        return (), 0
    tail = provenance_fields("native")
    actions: list[CorporateAction] = []
    skipped = 0

    dividends = events.get("dividends")
    if isinstance(dividends, Mapping):
        for entry in dividends.values():
            amount = _number(entry.get("amount")) if isinstance(entry, Mapping) else None
            stamp = _number(entry.get("date")) if isinstance(entry, Mapping) else None
            if amount is None or stamp is None:
                skipped += 1
                continue
            ex = session_date_of(int(stamp), listing.tz)
            source_ts = daily_bar_ts_ns(ex)
            if window is not None and not window[0] <= source_ts <= window[1]:
                continue
            actions.append(
                CorporateAction(
                    source=SOURCE,
                    symbol=listing.symbol,
                    symbol_raw=listing.yahoo_symbol,
                    source_ts=source_ts,
                    local_ts=captured_at_ns,
                    asset_class=AssetClass.EQUITY,
                    prov=tail.prov,
                    prov_basis=tail.prov_basis,
                    prov_confidence=tail.prov_confidence,
                    prov_inputs=tail.prov_inputs,
                    ex_date=ex.isoformat(),
                    type=CorpActionType.DIVIDEND_CASH,
                    value=amount,
                    currency=quote_unit.currency,
                    price_scale=quote_unit.price_scale,
                )
            )

    splits = events.get("splits")
    if isinstance(splits, Mapping):
        for entry in splits.values():
            numerator = _number(entry.get("numerator")) if isinstance(entry, Mapping) else None
            denominator = _number(entry.get("denominator")) if isinstance(entry, Mapping) else None
            stamp = _number(entry.get("date")) if isinstance(entry, Mapping) else None
            if (
                numerator is None
                or denominator is None
                or stamp is None
                or numerator <= 0
                or denominator <= 0
            ):
                skipped += 1
                continue
            ex = session_date_of(int(stamp), listing.tz)
            source_ts = daily_bar_ts_ns(ex)
            if window is not None and not window[0] <= source_ts <= window[1]:
                continue
            actions.append(
                CorporateAction(
                    source=SOURCE,
                    symbol=listing.symbol,
                    symbol_raw=listing.yahoo_symbol,
                    source_ts=source_ts,
                    local_ts=captured_at_ns,
                    asset_class=AssetClass.EQUITY,
                    prov=tail.prov,
                    prov_basis=tail.prov_basis,
                    prov_confidence=tail.prov_confidence,
                    prov_inputs=tail.prov_inputs,
                    ex_date=ex.isoformat(),
                    type=CorpActionType.SPLIT,
                    value=numerator / denominator,
                    currency=None,
                    price_scale=None,
                )
            )

    actions.sort(key=lambda action: (action.ex_date, action.type.value))
    return tuple(actions), skipped


def _retry_after(response: HttpResponse) -> float:
    """The numeric Retry-After of a 429, else the module default (HTTP-date form is not used)."""
    raw = response.headers.get("retry-after")
    if raw is None:
        return DEFAULT_BACKOFF_S
    try:
        seconds = float(raw)
    except ValueError:
        return DEFAULT_BACKOFF_S
    return seconds if seconds >= 0 and math.isfinite(seconds) else DEFAULT_BACKOFF_S


def _error_description(response: HttpResponse) -> str:
    try:
        payload = msgspec.json.decode(response.body)
    except msgspec.DecodeError:
        return "no description given"
    chart = payload.get("chart") if isinstance(payload, dict) else None
    error = chart.get("error") if isinstance(chart, dict) else None
    description = error.get("description") if isinstance(error, dict) else None
    return str(description) if description else "no description given"


def _is_not_found(payload: Mapping[str, Any]) -> bool:
    """A 200 body that names no result: a null/empty result with no error or a Not Found error."""
    chart = payload.get("chart")
    if not isinstance(chart, Mapping):
        return False
    result = chart.get("result")
    if result is not None and result != []:
        return False
    error = chart.get("error")
    return error is None or (isinstance(error, Mapping) and error.get("code") == "Not Found")


class YahooChartClient:
    """HTTP access to the chart endpoint. One request per call; nothing is retried here."""

    def __init__(self, *, fetch: Fetcher | None = None, bucket: TokenBucket | None = None) -> None:
        self._owned: AiohttpFetcher | None = None
        if fetch is None:
            self._owned = AiohttpFetcher()
            fetch = self._owned
        self._fetch: Fetcher = fetch
        self._bucket = bucket

    async def fetch_chart(
        self, yahoo_symbol: str, *, interval: str, period1: int, period2: int
    ) -> tuple[int, Mapping[str, Any] | None]:
        """One chart request. Returns (HTTP status, decoded payload), payload None on a 404."""
        bucket = self._bucket if self._bucket is not None else budget(SOURCE, HOST)
        await bucket.acquire()
        request = HttpRequest(
            source=SOURCE,
            method="GET",
            url=CHART_URL.format(symbol=quote(yahoo_symbol, safe="")),
            params=(
                ("period1", str(period1)),
                ("period2", str(period2)),
                ("interval", YAHOO_INTERVAL[interval]),
                ("events", EVENTS_PARAM),
                ("includeAdjustedClose", "true"),
            ),
        )
        response = await self._fetch(request)
        raise_if_unservable(response, source=SOURCE, expect="json")
        status = response.status
        if status == 404:
            return status, None
        if status == 429:
            bucket.update_backoff(_retry_after(response))
            raise TransientProviderError(f"yahoo_chart rate-limited (429) for {yahoo_symbol}")
        if 500 <= status <= 599:
            raise TransientProviderError(f"yahoo_chart answered {status} for {yahoo_symbol}")
        if status in (400, 422):
            raise ChartRequestError(
                f"yahoo_chart rejected the request ({status}): {_error_description(response)}"
            )
        if status != 200:
            raise ChartPayloadError(f"yahoo_chart answered an unexpected status {status}")
        try:
            payload = msgspec.json.decode(response.body)
        except msgspec.DecodeError as exc:
            raise ChartPayloadError(f"yahoo_chart sent a body that is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ChartPayloadError("yahoo_chart sent JSON that is not an object")
        return status, payload

    async def fetch_listing(
        self,
        listing: ChartListing,
        *,
        interval: str,
        start_ns: int,
        end_ns: int,
        captured_at_ns: int,
        through_session: date | None,
    ) -> ChartResult:
        """Fetch and parse one listing's window; the request window pads a daily window by a day."""
        if interval == DAILY_INTERVAL:
            period1 = start_ns // _NS_PER_SECOND - 86_400
            period2 = end_ns // _NS_PER_SECOND + 86_400
        else:
            period1 = start_ns // _NS_PER_SECOND
            period2 = end_ns // _NS_PER_SECOND
        status, payload = await self.fetch_chart(
            listing.yahoo_symbol, interval=interval, period1=period1, period2=period2
        )
        if payload is None or _is_not_found(payload):
            log.info("yahoo_chart has no data for %s (HTTP %s)", listing.yahoo_symbol, status)
            return ChartResult(listing, ChartStatus.NOT_FOUND, status, None)
        parse = parse_chart(
            payload,
            listing,
            interval=interval,
            captured_at_ns=captured_at_ns,
            through_session=through_session,
            window=(start_ns, end_ns),
        )
        return ChartResult(listing, ChartStatus.OK, status, parse)

    async def close(self) -> None:
        if self._owned is not None:
            await self._owned.close()


_RAW_PREFIX: Final = "yahoo_chart:"


@functools.cache
def _yahoo_grammar() -> SuffixGrammar:
    grammar = load_grammar(SOURCE)
    if not isinstance(grammar, SuffixGrammar):
        raise TypeError("the yahoo_chart grammar is not a suffix grammar")
    return grammar


def _us_mics(grammar: SuffixGrammar, catalogue: ExchangeRegistry) -> frozenset[str]:
    supported = grammar.supported_mics()
    return frozenset(
        info.mic for info in catalogue.by_country(US_COUNTRY) if info.mic in supported
    )


def _us_listing(local: str, grammar: SuffixGrammar, catalogue: ExchangeRegistry) -> ChartListing:
    """A bare US ticker: every grammar MIC of the US must agree on its spelling and zone."""
    mics = _us_mics(grammar, catalogue)
    if not mics:
        raise NoGrammarForMarket(SOURCE, US_COUNTRY)
    spellings = {grammar.to_source(local, mic) for mic in mics}
    zones = {catalogue.get(mic).tz for mic in mics}
    if len(spellings) != 1 or len(zones) != 1:
        raise ValueError(f"the US MICs disagree on the spelling or zone of {local!r}")
    return ChartListing(
        symbol=local,
        local=local,
        mics=mics,
        yahoo_symbol=spellings.pop(),
        tz=zones.pop(),
        calendar_mic="XNYS" if "XNYS" in mics else min(mics),
    )


def _raw_segment(grammar: SuffixGrammar, raw: str, local: str, mic: str) -> str | None:
    """The ISO 10383 segment a raw Yahoo spelling names, or None for the MIC's primary spelling.

    The grammar reads ``.KQ`` back to ``XKRX`` alone, so the segment is recovered by writing the
    spelling again. A raw symbol that no row of the MIC writes back exactly is refused, never
    rewritten to another spelling.
    """
    if grammar.to_source(local, mic) == raw:
        return None
    for row in grammar.rows:
        if row.mic == mic and row.is_segment:
            segment = row.rule[len("segment:") :]
            if grammar.to_source(local, mic, segment_mic=segment) == raw:
                return segment
    raise UnknownSourceSymbol(SOURCE, raw)


def _mic_listing(
    local: str,
    mic: str,
    grammar: SuffixGrammar,
    catalogue: ExchangeRegistry,
    *,
    allow: Collection[str],
    master: ListingMaster | None,
    raw: str | None = None,
) -> ChartListing:
    info = catalogue.get(mic)
    require_not_sanctioned(mic, allow=allow)
    symbol = listing_symbol(local, mic)
    if raw is not None:
        segment_mic = _raw_segment(grammar, raw, local, mic)
    else:
        known = master.get(symbol) if master is not None else None
        segment_mic = known.segment_mic if known is not None else None
    return ChartListing(
        symbol=symbol,
        local=local,
        mics=frozenset({mic}),
        yahoo_symbol=grammar.to_source(local, mic, segment_mic=segment_mic),
        tz=info.tz,
        calendar_mic=mic,
    )


def resolve_listing(
    symbol: str,
    *,
    allow: Collection[str] = (),
    master: ListingMaster | None = None,
) -> ChartListing:
    """Resolve a canonical symbol, a bare US ticker or ``yahoo_chart:<raw>`` to a listing.

    Pure: the sanctions opt-in comes in as ``allow``; no request is made. A sanctioned MIC is
    refused before any grammar call (``require_not_sanctioned``), and an unlisted MIC before both.
    """
    grammar = _yahoo_grammar()
    catalogue = ExchangeRegistry()
    if symbol.startswith(_RAW_PREFIX):
        raw = symbol[len(_RAW_PREFIX) :]
        try:
            local, mic = grammar.from_source(raw)
        except AmbiguousSourceSymbol as exc:
            if not exc.candidates or not all(
                catalogue.get(candidate).country == US_COUNTRY for candidate in exc.candidates
            ):
                raise
            return _us_listing(raw.replace("-", "."), grammar, catalogue)
        return _mic_listing(local, mic, grammar, catalogue, allow=allow, master=master, raw=raw)
    local, mic_or_none = parse_listing_symbol(symbol)
    if mic_or_none is None:
        return _us_listing(local, grammar, catalogue)
    return _mic_listing(local, mic_or_none, grammar, catalogue, allow=allow, master=master)


class YahooChartProvider(PullProvider):
    """Daily and intraday OHLCV plus corporate actions from one chart request each.

    ``backfill`` yields bars and corporate actions from the same request whichever of the two
    channels was asked (precedent: ``yahoo/connector.py``). ``usage_class`` is INTERNAL.
    """

    name = "yahoo_chart"
    rest_url = "https://query2.finance.yahoo.com"
    usage_class: ClassVar[UsageClass] = UsageClass.INTERNAL
    supported_channels: ClassVar[frozenset[str] | None] = frozenset({"ohlcv", "corp_action"})
    supported_mics: ClassVar[frozenset[str] | None] = frozenset(
        {
            "ASEX", "BMEX", "BVMF", "MISX", "ROCO", "XAMS", "XASX", "XBKK", "XBOG", "XBOM", "XBRU",
            "XBSE", "XBUD", "XBUE", "XCAI", "XCSE", "XDUB", "XETR", "XFRA", "XHEL", "XHKG", "XIDX",
            "XIST", "XJPX", "XJSE", "XKAR", "XKLS", "XKRX", "XLIM", "XLIS", "XLON", "XMEX", "XMIL",
            "XNAS", "XNSE", "XNYS", "XNZE", "XOSL", "XPAR", "XPRA", "XSAU", "XSES", "XSGO", "XSHE",
            "XSHG", "XSTC", "XSTO", "XSTU", "XSWX", "XTAE", "XTAI", "XTSE", "XWAR", "XWBO",
        }
    )  # the 54 MICs of task 05's grammar_yahoo_chart.csv; a test keeps literal == grammar
    unservable_channels: ClassVar[Mapping[str, str]] = MappingProxyType(
        {
            "instrument": (
                "Yahoo has no instrument snapshot in the chart endpoint; the reference "
                "channels come from the directories and TradingView, not from this source."
            ),
            "quote": (
                "the chart endpoint is a history source and serves no live quote; "
                "a quote poll would refetch yesterday's bars."
            ),
        }
    )
    unservable: ClassVar[Mapping[UnservableReason, str]] = MappingProxyType(
        {
            UnservableReason.BOT_CHALLENGE: "a consent, bot or redirect wall was served",
            UnservableReason.FORBIDDEN: "Yahoo answered 401 or 403 to this client",
            UnservableReason.GEO_BLOCKED: "Yahoo answered 451 for this location",
            UnservableReason.TIMEOUT: "the chart request timed out on the client side",
            UnservableReason.UNREACHABLE: "the chart host could not be reached",
        }
    )
    attribution: ClassVar[str | None] = None
    wants_settings: ClassVar[bool] = True

    @classmethod
    def grammar(cls) -> SuffixGrammar:
        return _yahoo_grammar()

    def __init__(
        self,
        symbols: list[str],
        channels: list[str],
        out: Sink,
        registry: InstrumentRegistry,
        *,
        client: YahooChartClient | None = None,
        master: ListingMaster | None = None,
        settings: Settings | None = None,
        interval: str = DAILY_INTERVAL,
        clock: Callable[[], int] = time.time_ns,
        **_: Any,
    ) -> None:
        super().__init__(symbols, channels, out, registry)
        if interval not in YAHOO_INTERVAL:
            raise ValueError(f"interval {interval!r} is not one of {sorted(YAHOO_INTERVAL)}")
        self._client = client if client is not None else YahooChartClient()
        self._master = master
        self._allow = allowed_countries(settings)
        self._interval = interval
        self._clock = clock

    def _resolve(self, symbol: str) -> ChartListing:
        return resolve_listing(symbol, allow=self._allow, master=self._master)

    def _through_session(self, listing: ChartListing, captured_at_ns: int) -> date:
        """The last session completed by the capture instant; never None (None means no filter)."""
        calendar = calendar_for(listing.calendar_mic)
        now = datetime.fromtimestamp(captured_at_ns / 1e9, tz=UTC)
        try:
            session = calendar.latest_completed_session(now, timedelta(0))
        except CalendarRangeError:
            session = None
        if session is None:
            session = session_date_of(captured_at_ns // _NS_PER_SECOND, listing.tz) - timedelta(
                days=1
            )
        return session

    async def backfill(
        self, channel: str, symbol: str, start_ns: int, end_ns: int
    ) -> AsyncIterator[Record]:
        served = self.supported_channels or frozenset()
        wanted = CHANNEL_SUCCESSORS.get(channel, channel)
        if wanted not in served:
            raise ValueError(f"yahoo_chart serves {sorted(served)}, not {channel!r}")
        listing = self._resolve(symbol)
        for mic in sorted(listing.mics):
            require_supported_mic(self, mic)
        captured_at_ns = self._clock()
        if self._interval != DAILY_INTERVAL:
            earliest, _ = intraday_window(self._interval, captured_at_ns)
            if start_ns < earliest:
                raise ValueError(
                    f"yahoo_chart {self._interval} history starts at {earliest} ns; the "
                    f"{self._interval} window is {INTRADAY_WINDOWS[self._interval]} and "
                    f"start_ns {start_ns} is older"
                )
        result = await self._client.fetch_listing(
            listing,
            interval=self._interval,
            start_ns=start_ns,
            end_ns=end_ns,
            captured_at_ns=captured_at_ns,
            through_session=self._through_session(listing, captured_at_ns),
        )
        if result.parse is None:
            log.info("yahoo_chart yielded nothing for %s", symbol)
            return
        known = self._master.get(listing.symbol) if self._master is not None else None
        for record in result.parse.records:
            if known is not None and isinstance(record, OHLCV | CorporateAction):
                try:
                    check_currency_consistent(record.currency, record.price_scale, known)
                except CurrencyMismatchError as exc:
                    detail = {
                        "symbol": listing.symbol,
                        "record": {"currency": record.currency, "price_scale": record.price_scale},
                        "listing": {"currency": known.currency, "price_scale": known.price_scale},
                    }
                    self._dlq.put(
                        captured_at_ns,
                        json.dumps(detail, sort_keys=True).encode("utf-8"),
                        "CurrencyMismatchError",
                        str(exc),
                    )
                    continue
            yield record

    async def close(self) -> None:
        await self._client.close()
