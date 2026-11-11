"""TradingView's keyless scanner as a universal source (``tradingview``).

Two verbs, both one POST per page to ``scanner.tradingview.com/<market>/scan``:

- ``instrument``: the market's universe. Each row becomes a listing observation for the listing
  master and one ``Instrument`` record that is TradingView's own reference row (``native``).
- ``ohlcv``: the end-of-day snapshot. The scanner answers with the current state of a listing's
  latest session and no date, so each row is attributed to the exchange session it describes
  here, and the bar is ``session_snapshot`` (DERIVED): the calendar is one witness, a row stamp
  on that session is the second (spec 3.10b, ``provenance.py``).

Ruling: the scanner is an unofficial aggregator whose terms are not read. Usage is declared
``INTERNAL`` by user decision (spec section 0). Nothing here bypasses a consent, bot, CAPTCHA or
geo wall: a wall is recorded as ``SourceUnservableError``, never retried around.

Ruling: sanctioned markets are default-closed. The sweep never probes them, the provider refuses
them before any request unless the operator opted in through ``Settings``, and the universe and
EOD rows of a sanctioned MIC are dropped and counted even when a market is scanned.

Ruling: the market table in ``data/tradingview_markets.csv`` is evidence, not configuration: every
row carries the date it was measured, and a candidate that was not probed says so in ``notes``.
"""

from __future__ import annotations

import csv
import functools
import importlib.resources
import io
import logging
import math
import re
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Final

import msgspec

from crocodile.core.markets.calendar import ExchangeCalendar, calendar_for
from crocodile.core.markets.currency import QuoteUnit, parse_quote_unit
from crocodile.core.markets.errors import (
    CalendarRangeError,
    InvalidSymbolError,
    MarketError,
    NoGrammarForMarket,
    UnknownCurrencyError,
)
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.grammar import (
    GrammarError,
    PrefixGrammar,
    SuffixGrammar,
    load_grammar,
)
from crocodile.core.markets.master import ListingMaster, ListingObservation
from crocodile.core.markets.sanctions import (
    allowed_countries,
    is_sanctioned,
    require_not_sanctioned,
)
from crocodile.core.markets.symbols import US_COUNTRY, listing_symbol, parse_listing_symbol
from crocodile.core.markets.usage import UsageClass
from crocodile.core.ratelimit import budget
from crocodile.core.ratelimit.token_bucket import TokenBucket
from crocodile.core.schema.enums import (
    CHANNEL_SUCCESSORS,
    AssetClass,
    PriceAdjustment,
    SecurityType,
)
from crocodile.core.schema.provenance import provenance_fields
from crocodile.core.schema.records import OHLCV, Instrument, Record
from crocodile.equity.providers.base import (
    ProviderError,
    PullProvider,
    TransientProviderError,
)
from crocodile.equity.universal.base import (
    UniverseSnapshot,
    UnservableReason,
    apply_universe,
    daily_bar_ts_ns,
    is_valid_isin,
    session_date_of,
)
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
    from crocodile.core.store.catalog import Catalog
    from crocodile.equity.reference.registry import InstrumentRegistry

__all__ = [
    "DEFAULT_EOD_DELAY",
    "EOD_COLUMNS",
    "HOST",
    "PAGE_SIZE",
    "PROBE_COLUMNS",
    "ROW_TIMESTAMP_FIELD",
    "SCAN_URL",
    "SOURCE",
    "SWEEP_CANDIDATES",
    "UNIVERSE_COLUMNS",
    "EodSnapshot",
    "MarketMeasurement",
    "ScanPayloadError",
    "ScanRequestError",
    "ScanResult",
    "ScanRow",
    "SnapshotValues",
    "TradingViewClient",
    "TradingViewMarket",
    "TradingViewProvider",
    "eod_bars_from_scan",
    "load_markets",
    "market_is_sanctioned",
    "market_table_rows",
    "markets_for_country",
    "markets_for_mic",
    "measure_markets",
    "parse_markets_csv",
    "previous_snapshot_values",
    "require_market_not_sanctioned",
    "scan_body",
    "scannable_markets",
    "security_type_for",
    "universe_from_scan",
]

log = logging.getLogger(__name__)

SOURCE: Final = "tradingview"
HOST: Final = "scanner.tradingview.com"
SCAN_URL: Final = "https://scanner.tradingview.com/{market}/scan"
METAINFO_URL: Final = "https://scanner.tradingview.com/{market}/metainfo"
PAGE_SIZE: Final = 1000
DEFAULT_EOD_DELAY: Final = timedelta(minutes=45)
"""Spec 3.9 default settle delay between a session's close and the snapshot that describes it."""
PROBE_COLUMNS: Final[tuple[str, ...]] = ("name", "close", "currency", "exchange")
"""The R&D and task 05 probe shape. Every recorded page projects onto it."""
UNIVERSE_COLUMNS: Final[tuple[str, ...]] = (
    *PROBE_COLUMNS,
    "description",
    "type",
    "subtype",
)
"""Universe scan columns: the probe shape first, then the listing's description and type."""
ROW_TIMESTAMP_FIELD: Final[str | None] = None
"""The per-row update-time column the EOD scan requests, or ``None`` for no row stamp.

Pending: part 11b decides this from the live metainfo and the recorded EOD answers, and the
measured value replaces ``None`` there. Until then the stamp branch is built and tested with an
explicit ``row_timestamp_field="stamp"`` argument, and the default means "no stamp".
"""
_BASE_EOD_COLUMNS: Final[tuple[str, ...]] = (
    "name",
    "exchange",
    "currency",
    "open",
    "high",
    "low",
    "close",
    "volume",
)
EOD_COLUMNS: Final[tuple[str, ...]] = (
    (*_BASE_EOD_COLUMNS, ROW_TIMESTAMP_FIELD)
    if ROW_TIMESTAMP_FIELD is not None
    else _BASE_EOD_COLUMNS
)

SWEEP_CANDIDATES: Final[Mapping[str, str]] = MappingProxyType(
    {
        # The spec list: TradingView market name -> ISO 3166-1 alpha-2.
        "america": "US",
        "argentina": "AR",
        "australia": "AU",
        "austria": "AT",
        "bahrain": "BH",
        "bangladesh": "BD",
        "belgium": "BE",
        "brazil": "BR",
        "canada": "CA",
        "chile": "CL",
        "china": "CN",
        "colombia": "CO",
        "cyprus": "CY",
        "czech": "CZ",
        "denmark": "DK",
        "egypt": "EG",
        "estonia": "EE",
        "finland": "FI",
        "france": "FR",
        "germany": "DE",
        "greece": "GR",
        "hongkong": "HK",
        "hungary": "HU",
        "iceland": "IS",
        "india": "IN",
        "indonesia": "ID",
        "israel": "IL",
        "italy": "IT",
        "japan": "JP",
        "kenya": "KE",
        "korea": "KR",
        "ksa": "SA",
        "kuwait": "KW",
        "latvia": "LV",
        "lithuania": "LT",
        "luxembourg": "LU",
        "malaysia": "MY",
        "mexico": "MX",
        "morocco": "MA",
        "netherlands": "NL",
        "newzealand": "NZ",
        "nigeria": "NG",
        "norway": "NO",
        "pakistan": "PK",
        "peru": "PE",
        "philippines": "PH",
        "poland": "PL",
        "portugal": "PT",
        "qatar": "QA",
        "romania": "RO",
        "rsa": "ZA",
        "russia": "RU",
        "serbia": "RS",
        "singapore": "SG",
        "slovakia": "SK",
        "spain": "ES",
        "srilanka": "LK",
        "sweden": "SE",
        "switzerland": "CH",
        "taiwan": "TW",
        "thailand": "TH",
        "tunisia": "TN",
        "turkey": "TR",
        "uae": "AE",
        "uk": "GB",
        "venezuela": "VE",
        "vietnam": "VN",
        # Extras ("any others found"; most will 404).
        "ghana": "GH",
        "kazakhstan": "KZ",
        "mongolia": "MN",
        "bulgaria": "BG",
        "croatia": "HR",
        "slovenia": "SI",
        "ukraine": "UA",
        "jordan": "JO",
        "oman": "OM",
        "botswana": "BW",
        "mauritius": "MU",
        "namibia": "NA",
        "tanzania": "TZ",
        "uganda": "UG",
        "zambia": "ZM",
        "zimbabwe": "ZW",
        "malawi": "MW",
        "jamaica": "JM",
        "trinidad": "TT",
        "ecuador": "EC",
        "uruguay": "UY",
        "panama": "PA",
        "costarica": "CR",
        "ireland": "IE",
        "malta": "MT",
        "lebanon": "LB",
        "palestine": "PS",
        "iraq": "IQ",
        "georgia": "GE",
        "armenia": "AM",
        "azerbaijan": "AZ",
        "uzbekistan": "UZ",
        "bosnia": "BA",
        "macedonia": "MK",
        "montenegro": "ME",
        "rwanda": "RW",
        "belarus": "BY",
        "iran": "IR",
        "syria": "SY",
    }
)
"""Every TradingView market the sweep may name, with its ISO country. Sanctioned countries are
listed so the sweep can say it skipped them."""

_MARKETS_RESOURCE: Final = "data/tradingview_markets.csv"
_MARKET_HEADER: Final[tuple[str, ...]] = (
    "market",
    "country",
    "http_status",
    "total_count",
    "exchange_codes",
    "currencies",
    "evidence_date",
    "notes",
)
_SECURITY_TYPES: Final[Mapping[str, SecurityType]] = MappingProxyType(
    {
        "structured": SecurityType.STRUCTURED,
        "bond": SecurityType.BOND,
        "warrant": SecurityType.WARRANT,
    }
)
_YAHOO_LOCAL: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9.]*")
_EPOCH_UTC: Final = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_DAY: Final = timedelta(days=1)
_NS_PER_MICROSECOND: Final = 1_000
_DEFAULT_BACKOFF_S: Final = 5.0
_SUPPORTED_MICS: Final[frozenset[str]] = frozenset(
    {
        "HSTC",
        "XBEL",
        "XBOG",
        "XCAI",
        "XCAS",
        "XCOL",
        "XDHA",
        "XIST",
        "XKAR",
        "XLIM",
        "XNAI",
        "XNSA",
        "XSTC",
    }
)  # the 13 MICs of task 05's grammar_tradingview.csv; task 11b widens it with the live sweep


class ScanPayloadError(ProviderError):
    """A scanner answer whose shape is wrong: no ``data``, or a row that does not match columns."""


class ScanRequestError(ProviderError):
    """The scanner rejected the request (400/422), carrying the body it answered with."""


@dataclass(frozen=True, slots=True)
class ScanRow:
    ticker: str  # row["s"], e.g. "BIST:THYAO"
    values: Mapping[str, Any]  # requested column -> value (zip of columns and row["d"])


@dataclass(frozen=True, slots=True)
class ScanResult:
    market: str
    columns: tuple[str, ...]
    http_status: int  # status of the first page (404 => unknown market)
    total_count: int  # 0 when not found
    rows: tuple[ScanRow, ...]  # de-duplicated by ticker, request order
    complete: bool  # len(rows) == total_count and every page answered


@dataclass(frozen=True, slots=True)
class TradingViewMarket:
    """One row of data/tradingview_markets.csv."""

    market: str
    country: str  # ISO alpha-2
    http_status: int | None  # None: not probed (sanctioned, or awaiting the live sweep)
    total_count: int | None
    exchange_codes: tuple[str, ...]
    currencies: tuple[str, ...]
    evidence_date: date
    notes: str


@dataclass(frozen=True, slots=True)
class MarketMeasurement:
    """What one probe of a candidate market found, before it becomes a table row."""

    market: str
    country: str
    http_status: int | None
    total_count: int | None
    exchange_codes: Mapping[str, int]  # code -> rows seen in the probe page
    currencies: tuple[str, ...]  # sorted distinct raw currency strings seen
    examples: Mapping[str, str]  # code -> first ticker seen ("BIST:A1YEN")
    evidence_date: date
    notes: str


@dataclass(frozen=True, slots=True)
class SnapshotValues:
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class EodSnapshot:
    captured_at_ns: int
    bars: tuple[OHLCV, ...]  # sorted by symbol
    sessions: Mapping[str, date]  # MIC -> session d, present iff at least one row of that MIC
    # passed attribution (the calendar guard for an unstamped row, a stamp on d for a stamped one),
    # whether or not a bar then survived the stale rule; task 14 turns it into attributed_mics
    skipped: Mapping[str, int]  # reason -> count


# ---------------------------------------------------------------- market table


def parse_markets_csv(text: str) -> tuple[TradingViewMarket, ...]:
    """Parse the market table text, refusing a bad header, a repeated market or an unevidenced row.

    A row with no ``http_status`` must say it was not probed in ``notes``; that is the only way an
    empty status is honest.
    """
    reader = csv.DictReader(io.StringIO(text))
    if tuple(reader.fieldnames or ()) != _MARKET_HEADER:
        raise ValueError(f"tradingview_markets.csv header must be {_MARKET_HEADER!r}")
    seen: set[str] = set()
    markets: list[TradingViewMarket] = []
    for line, row in enumerate(reader, start=2):
        market = row["market"]
        if not market or market in seen:
            raise ValueError(f"line {line}: market {market!r} is missing or repeated")
        seen.add(market)
        status = row["http_status"]
        total = row["total_count"]
        if not status and not row["notes"].startswith("not probed"):
            raise ValueError(f"line {line}: {market} has no status and no 'not probed' note")
        markets.append(
            TradingViewMarket(
                market=market,
                country=row["country"],
                http_status=int(status) if status else None,
                total_count=int(total) if total else None,
                exchange_codes=tuple(row["exchange_codes"].split()),
                currencies=tuple(row["currencies"].split()),
                evidence_date=date.fromisoformat(row["evidence_date"]),
                notes=row["notes"],
            )
        )
    return tuple(sorted(markets, key=lambda item: item.market))


@functools.cache
def load_markets() -> tuple[TradingViewMarket, ...]:
    """The packaged market table, read once and sorted by market."""
    text = (
        importlib.resources.files("crocodile.equity.universal")
        .joinpath(_MARKETS_RESOURCE)
        .read_text(encoding="utf-8")
    )
    return parse_markets_csv(text)


def _country_is_sanctioned(country: str, *, allow: Collection[str] = ()) -> bool:
    return any(
        is_sanctioned(info.mic, allow=allow) for info in ExchangeRegistry().by_country(country)
    )


def market_is_sanctioned(market: TradingViewMarket, *, allow: Collection[str] = ()) -> bool:
    """True when any operating MIC of the market's country is sanctioned and not opted in."""
    return _country_is_sanctioned(market.country, allow=allow)


def require_market_not_sanctioned(
    market: TradingViewMarket, *, allow: Collection[str] = ()
) -> None:
    """Raise SanctionedMarketError naming a real MIC of the market's country, if it is closed."""
    for info in ExchangeRegistry().by_country(market.country):
        if is_sanctioned(info.mic, allow=allow):
            require_not_sanctioned(info.mic, allow=allow)


def scannable_markets(*, allow: Collection[str] = ()) -> tuple[TradingViewMarket, ...]:
    """Markets the R&D or the sweep answered with 200 and that the sanctions rule does not close."""
    return tuple(
        market
        for market in load_markets()
        if market.http_status == 200 and not market_is_sanctioned(market, allow=allow)
    )


@functools.cache
def _grammar() -> PrefixGrammar:
    grammar = load_grammar(SOURCE)
    if not isinstance(grammar, PrefixGrammar):
        raise TypeError("the tradingview grammar is not a prefix grammar")
    return grammar


@functools.cache
def _yahoo_grammar() -> SuffixGrammar:
    grammar = load_grammar("yahoo_chart")
    if not isinstance(grammar, SuffixGrammar):
        raise TypeError("the yahoo_chart grammar is not a suffix grammar")
    return grammar


@functools.cache
def _catalogue() -> ExchangeRegistry:
    return ExchangeRegistry()


def markets_for_mic(mic: str) -> tuple[TradingViewMarket, ...]:
    """The table rows of the markets the grammar names for ``mic`` (its PrefixRow.market)."""
    names = {row.market for row in _grammar().rows if row.mic == mic}
    return tuple(market for market in load_markets() if market.market in names)


def markets_for_country(country: str) -> tuple[TradingViewMarket, ...]:
    return tuple(market for market in load_markets() if market.country == country.upper())


def security_type_for(type_: str | None, subtype: str | None) -> SecurityType:
    """Spec 3.10b type map, lower-cased. Anything not named here is UNKNOWN, not guessed."""
    kind = (type_ or "").lower()
    sub = (subtype or "").lower()
    if kind == "stock":
        return SecurityType.PFD if sub == "preferred" else SecurityType.CS
    if kind == "fund":
        return SecurityType.ETF if sub == "etf" else SecurityType.FUND
    if kind == "dr":
        return SecurityType.GDR if sub == "gdr" else SecurityType.ADR
    return _SECURITY_TYPES.get(kind, SecurityType.UNKNOWN)


def scan_body(market: str, columns: Sequence[str], offset: int, limit: int) -> dict[str, Any]:
    """The scanner request body for one page: market, columns and ``[offset, offset+limit]``.

    The body is the one the R&D and task 05 probes answered to; the sort is by name so pages are
    stable across requests.
    """
    return {
        "markets": [market],
        "symbols": {"query": {"types": []}, "tickers": []},
        "options": {"lang": "en"},
        "columns": list(columns),
        "sort": {"sortBy": "name", "sortOrder": "asc"},
        "range": [offset, offset + limit],
    }


async def measure_markets(
    client: TradingViewClient,
    candidates: Mapping[str, str],
    *,
    columns: Sequence[str],
    evidence_date: date,
    probe_rows: int = PAGE_SIZE,
) -> list[MarketMeasurement]:
    """Probe each candidate once (one page) and report what the answer holds, sorted by market.

    A candidate whose country has a sanctioned MIC is not probed: it gets no request, an empty
    status and a ``not probed: sanctioned (<ISO2>)`` note, and no opt-in reaches this path.
    """
    measurements: list[MarketMeasurement] = []
    cols = tuple(columns)
    for market in sorted(candidates):
        country = candidates[market]
        if _country_is_sanctioned(country):
            measurements.append(
                MarketMeasurement(
                    market=market,
                    country=country,
                    http_status=None,
                    total_count=None,
                    exchange_codes={},
                    currencies=(),
                    examples={},
                    evidence_date=evidence_date,
                    notes=f"not probed: sanctioned ({country})",
                )
            )
            continue
        result = await client.scan(market, cols, max_rows=probe_rows)
        codes: Counter[str] = Counter()
        examples: dict[str, str] = {}
        currencies: set[str] = set()
        for row in result.rows:
            code = row.ticker.split(":", 1)[0]
            codes[code] += 1
            examples.setdefault(code, row.ticker)
            currency = row.values.get("currency")
            if isinstance(currency, str) and currency:
                currencies.add(currency)
        measurements.append(
            MarketMeasurement(
                market=market,
                country=country,
                http_status=result.http_status,
                total_count=result.total_count,
                exchange_codes=dict(sorted(codes.items())),
                currencies=tuple(sorted(currencies)),
                examples=dict(sorted(examples.items())),
                evidence_date=evidence_date,
                notes="",
            )
        )
    return measurements


def market_table_rows(measurements: Sequence[MarketMeasurement]) -> list[dict[str, str]]:
    """CSV rows for data/tradingview_markets.csv, in the order given (measure_markets sorts)."""
    rows: list[dict[str, str]] = []
    for item in measurements:
        rows.append(
            {
                "market": item.market,
                "country": item.country,
                "http_status": "" if item.http_status is None else str(item.http_status),
                "total_count": "" if item.total_count is None else str(item.total_count),
                "exchange_codes": " ".join(sorted(item.exchange_codes)),
                "currencies": " ".join(sorted(item.currencies)),
                "evidence_date": item.evidence_date.isoformat(),
                "notes": item.notes,
            }
        )
    return rows


# ---------------------------------------------------------------------- client


@dataclass(frozen=True, slots=True)
class _Page:
    status: int
    total_count: int
    rows: tuple[ScanRow, ...]


def _retry_after(response: HttpResponse) -> float:
    raw = response.headers.get("retry-after")
    if raw is None:
        return _DEFAULT_BACKOFF_S
    try:
        seconds = float(raw)
    except ValueError:
        return _DEFAULT_BACKOFF_S
    return seconds if seconds >= 0 and math.isfinite(seconds) else _DEFAULT_BACKOFF_S


def _body_text(response: HttpResponse) -> str:
    return response.body.decode("utf-8", "replace")[:500]


def _decode_page(body: bytes, columns: Sequence[str], market: str) -> _Page:
    try:
        payload = msgspec.json.decode(body)
    except msgspec.DecodeError as exc:
        raise ScanPayloadError(f"tradingview sent a page for {market} that is not JSON") from exc
    if not isinstance(payload, dict):
        raise ScanPayloadError(f"tradingview sent JSON for {market} that is not an object")
    total = payload.get("totalCount")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ScanPayloadError(f"tradingview page for {market} has no usable totalCount")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ScanPayloadError(f"tradingview page for {market} has no data list")
    rows: list[ScanRow] = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("s"), str):
            raise ScanPayloadError(f"tradingview row for {market} has no ticker")
        values = item.get("d")
        if not isinstance(values, list) or len(values) != len(columns):
            raise ScanPayloadError(
                f"tradingview row {item.get('s')!r} does not match the {len(columns)} columns"
            )
        rows.append(ScanRow(ticker=item["s"], values=dict(zip(columns, values, strict=True))))
    return _Page(status=200, total_count=total, rows=tuple(rows))


def _absorb(seen: dict[str, ScanRow], rows: Sequence[ScanRow]) -> None:
    for row in rows:
        seen.setdefault(row.ticker, row)


class TradingViewClient:
    """HTTP access to the scanner. One request per call; nothing is retried here.

    Every request waits on the shared ``budget(SOURCE, HOST)`` bucket, fetched lazily inside the
    coroutine (interfaces ruling 4). 429 backs that bucket off; 5xx is transient; walls raise
    through ``raise_if_unservable``.
    """

    def __init__(self, *, fetch: Fetcher | None = None, bucket: TokenBucket | None = None) -> None:
        self._owned: AiohttpFetcher | None = None
        if fetch is None:
            self._owned = AiohttpFetcher()
            fetch = self._owned
        self._fetch: Fetcher = fetch
        self._bucket = bucket

    async def _send(self, request: HttpRequest, bucket: TokenBucket) -> HttpResponse:
        await bucket.acquire()
        response = await self._fetch(request)
        raise_if_unservable(response, source=SOURCE, expect="json")
        if response.status == 429:
            bucket.update_backoff(_retry_after(response))
            raise TransientProviderError(f"tradingview rate-limited (429) for {request.url}")
        if 500 <= response.status <= 599:
            raise TransientProviderError(
                f"tradingview answered {response.status} for {request.url}"
            )
        return response

    async def _post_page(
        self, market: str, columns: tuple[str, ...], offset: int, limit: int
    ) -> _Page:
        bucket = self._bucket if self._bucket is not None else budget(SOURCE, HOST)
        request = HttpRequest(
            source=SOURCE,
            method="POST",
            url=SCAN_URL.format(market=market),
            json_body=scan_body(market, columns, offset, limit),
        )
        response = await self._send(request, bucket)
        if response.status == 404:
            return _Page(status=404, total_count=0, rows=())
        if response.status in (400, 422):
            raise ScanRequestError(
                f"tradingview rejected the scan of {market} ({response.status}): "
                f"{_body_text(response)}"
            )
        if response.status != 200:
            raise ScanPayloadError(
                f"tradingview answered an unexpected status {response.status} for {market}"
            )
        return _decode_page(response.body, columns, market)

    async def scan(
        self,
        market: str,
        columns: Sequence[str],
        *,
        page_size: int = PAGE_SIZE,
        max_rows: int | None = None,
    ) -> ScanResult:
        """Read a market's rows for ``columns``.

        ``max_rows`` asks for one page ``[0, max_rows]`` (the sweep probe), with
        ``complete = total_count <= max_rows``. Otherwise pages advance by ``page_size`` until the
        first page's ``totalCount`` is reached; a page with no rows stops the loop incomplete.
        """
        cols = tuple(columns)
        if max_rows is not None:
            page = await self._post_page(market, cols, 0, max_rows)
            if page.status == 404:
                return ScanResult(market, cols, 404, 0, (), complete=False)
            seen: dict[str, ScanRow] = {}
            _absorb(seen, page.rows)
            return ScanResult(
                market,
                cols,
                200,
                page.total_count,
                tuple(seen.values()),
                complete=page.total_count <= max_rows,
            )
        first = await self._post_page(market, cols, 0, page_size)
        if first.status == 404:
            return ScanResult(market, cols, 404, 0, (), complete=False)
        collected: dict[str, ScanRow] = {}
        _absorb(collected, first.rows)
        total = first.total_count
        answered = True
        offset = page_size
        while offset < total:
            page = await self._post_page(market, cols, offset, page_size)
            if page.status != 200 or not page.rows:
                answered = False
                break
            _absorb(collected, page.rows)
            offset += page_size
        rows = tuple(collected.values())
        return ScanResult(
            market,
            cols,
            200,
            total,
            rows,
            complete=answered and len(rows) == total,
        )

    async def metainfo(self, market: str) -> Mapping[str, Any]:
        """The market's column catalogue: GET, then one POST {} if the GET is refused."""
        bucket = self._bucket if self._bucket is not None else budget(SOURCE, HOST)
        url = METAINFO_URL.format(market=market)
        response = await self._send(HttpRequest(source=SOURCE, method="GET", url=url), bucket)
        if response.status in (404, 405):
            response = await self._send(
                HttpRequest(source=SOURCE, method="POST", url=url, json_body={}), bucket
            )
        if response.status != 200:
            raise ScanRequestError(
                f"tradingview metainfo for {market} answered {response.status}: "
                f"{_body_text(response)}"
            )
        try:
            payload = msgspec.json.decode(response.body)
        except msgspec.DecodeError as exc:
            raise ScanPayloadError(f"tradingview metainfo for {market} is not JSON") from exc
        if not isinstance(payload, dict):
            raise ScanPayloadError(f"tradingview metainfo for {market} is not an object")
        return payload

    async def close(self) -> None:
        if self._owned is not None:
            await self._owned.close()


# ------------------------------------------------------------- row resolution


@dataclass(frozen=True, slots=True)
class _Resolved:
    ticker: str
    local: str
    mic: str
    symbol: str
    currency: str
    price_scale: int


def _count(skipped: Counter[str], reason: str) -> None:
    skipped[reason] += 1


def _resolve(
    row: ScanRow,
    allow: Collection[str],
    skipped: Counter[str],
) -> _Resolved | None:
    """A scanner row to a canonical listing, or None with the reason counted in ``skipped``."""
    grammar = _grammar()
    catalogue = _catalogue()
    code = row.ticker.split(":", 1)[0]
    try:
        local, mic = grammar.from_source(row.ticker)
    except NoGrammarForMarket:
        _count(skipped, f"unknown_exchange_code:{code}")
        return None
    except GrammarError:
        _count(skipped, "malformed_ticker")
        return None
    if mic not in catalogue:
        _count(skipped, f"market_not_listed:{mic}")
        return None
    if is_sanctioned(mic, allow=allow):
        _count(skipped, f"sanctioned:{mic}")
        return None
    if row.values.get("exchange") != code:
        _count(skipped, "exchange_mismatch")
        return None
    try:
        symbol = listing_symbol(local, mic)
    except InvalidSymbolError:
        _count(skipped, "invalid_local_symbol")
        return None
    raw_currency = row.values.get("currency")
    if not isinstance(raw_currency, str) or not raw_currency:
        _count(skipped, "missing_currency")
        return None
    try:
        unit: QuoteUnit = parse_quote_unit(raw_currency)
    except UnknownCurrencyError:
        _count(skipped, f"unknown_currency:{raw_currency}")
        return None
    return _Resolved(
        ticker=row.ticker,
        local=local,
        mic=mic,
        symbol=symbol,
        currency=unit.currency,
        price_scale=unit.price_scale,
    )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _yahoo_alias(local: str, mic: str) -> tuple[tuple[str, str], ...]:
    """The Yahoo spelling of the listing, when the Yahoo grammar covers the MIC and the local
    symbol is one its rules can write; no alias rather than a wrong one."""
    yahoo = _yahoo_grammar()
    if mic not in yahoo.supported_mics() or _YAHOO_LOCAL.fullmatch(local) is None:
        return ()
    try:
        spelled = yahoo.to_source(local, mic)
    except (NoGrammarForMarket, GrammarError):
        return ()
    return (("yahoo_chart", spelled),)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return float(value)


def universe_from_scan(
    scan: ScanResult,
    *,
    captured_at_ns: int,
    allow: Collection[str] = (),
) -> UniverseSnapshot:
    """Observations for the listing master and native Instrument records, from one universe scan.

    Every row either becomes one observation and one record, or is skipped with a counted reason
    (unknown exchange code, malformed ticker, unlisted or sanctioned MIC, exchange mismatch,
    invalid local symbol, missing or unknown currency). ``complete`` is the scan's own answer.
    """
    catalogue = _catalogue()
    skipped: Counter[str] = Counter()
    observations: list[ListingObservation] = []
    records: list[Instrument] = []
    seen: dict[str, set[str]] = {}
    tail = provenance_fields("native")
    for row in scan.rows:
        resolved = _resolve(row, allow, skipped)
        if resolved is None:
            continue
        values = row.values
        security_type = security_type_for(_text(values.get("type")), _text(values.get("subtype")))
        name = _text(values.get("description"))
        isin_value = values.get("isin")
        isin = isin_value if isinstance(isin_value, str) and is_valid_isin(isin_value) else None
        observations.append(
            ListingObservation(
                source=SOURCE,
                mic=resolved.mic,
                local_symbol=resolved.local,
                symbol_raw=row.ticker,
                observed_ns=captured_at_ns,
                currency=resolved.currency,
                price_scale=resolved.price_scale,
                name=name,
                security_type=security_type,
                isin=isin,
                extra_aliases=_yahoo_alias(resolved.local, resolved.mic),
            )
        )
        records.append(
            Instrument(
                source=SOURCE,
                symbol=resolved.symbol,
                symbol_raw=row.ticker,
                local_ts=captured_at_ns,
                source_ts=None,
                asset_class=AssetClass.EQUITY,
                prov=tail.prov,
                prov_basis=tail.prov_basis,
                prov_confidence=tail.prov_confidence,
                prov_inputs=tail.prov_inputs,
                name=name,
                exchange=resolved.mic,
                security_type=security_type,
                mic=resolved.mic,
                segment_mic=None,
                isin=isin,
                country=catalogue.get(resolved.mic).country,
                currency=resolved.currency,
                price_scale=resolved.price_scale,
            )
        )
        seen.setdefault(resolved.mic, set()).add(resolved.local)
    observations.sort(key=lambda item: (item.mic, item.local_symbol))
    records.sort(key=lambda item: item.symbol)
    return UniverseSnapshot(
        source=SOURCE,
        captured_at_ns=captured_at_ns,
        observations=tuple(observations),
        records=tuple(records),
        seen={mic: frozenset(locals_) for mic, locals_ in sorted(seen.items())},
        complete=scan.complete,
        skipped=dict(sorted(skipped.items())),
    )


# ------------------------------------------------------------------------ EOD


def _next_open_reason(cal: ExchangeCalendar, d: date, captured_utc: datetime) -> str | None:
    """The guard for an unstamped row: None when the capture precedes the next session's open."""
    try:
        upcoming = cal.sessions(d + _ONE_DAY, d + timedelta(days=31))
    except CalendarRangeError:
        return "calendar_range"
    if not upcoming:
        return "calendar_range"
    if captured_utc >= cal.session_open_utc(upcoming[0]):
        return "session_open"
    return None


def eod_bars_from_scan(
    scan: ScanResult,
    *,
    captured_at_ns: int,
    previous: Mapping[str, SnapshotValues],
    calendars: Callable[[str], ExchangeCalendar] = calendar_for,
    delay: timedelta = DEFAULT_EOD_DELAY,
    row_timestamp_field: str | None = ROW_TIMESTAMP_FIELD,
    allow: Collection[str] = (),
) -> EodSnapshot:
    """Daily bars attributed to the exchange session each row describes.

    Per MIC, ``d`` is the calendar's latest session whose close plus ``delay`` has passed by the
    capture instant; ``None`` skips every row of that MIC (``no_completed_session``). Rows are then
    decided in order:

    1. any of open, high, low or close null or non-positive: ``null_price``;
    2. volume null, zero or negative: ``no_volume``;
    3. a row stamp (``row_timestamp_field`` set, and the row carries a numeric stamp in epoch
       seconds) decides the session. Its exchange-local date before ``d``: ``row_timestamp_before_
       session``; after ``d``: ``row_timestamp_after_session``; on ``d`` but before the session's
       open: ``row_timestamp_before_session``; on ``d`` and after the open: attributed with two
       witnesses, and the next-open guard does not apply to it;
    4. an unstamped row is held to the calendar's next-open guard: no session in the following
       31 days, or a date before the calendar's window, is ``calendar_range``; a capture at or
       after the next session's open is ``session_open`` (the row may describe a session in
       progress). Attributed with one witness;
    5. an attributed row whose values equal ``previous[symbol]`` exactly is ``stale_identical``.

    ``sessions`` names a MIC iff at least one of its rows reached step 3 or 4 (whether or not a
    bar survived step 5). On ``WEEKDAY_FALLBACK`` calendars (most TradingView-only markets: XNSA,
    XNAI, XDHA, XCAI, XSTC) and on library calendars past their bound (XSHG, XBOM and XSES after
    2026-12-31, where ``quality_for(d)`` is ``WEEKDAY_FALLBACK``), the guard uses the calendar's
    local-midnight placeholder for the next open and its 23:00 local placeholder close, so an
    unstamped capture is attributable only between close plus the delay and local midnight before
    a weekday session. A bar is built only from an attributed row, so every emitted bar has a
    completed session from the calendar and ``source_ts == daily_bar_ts_ns(d)``.

    Bars use ``adjustment=NONE``: a session's own bar captured the evening it closed carries the
    traded prices. The stamp value is read as epoch seconds; a source that stamps in another unit
    must convert it here and say so.
    """
    skipped: Counter[str] = Counter()
    by_mic: dict[str, list[tuple[_Resolved, ScanRow]]] = {}
    for row in scan.rows:
        resolved = _resolve(row, allow, skipped)
        if resolved is not None:
            by_mic.setdefault(resolved.mic, []).append((resolved, row))
    captured_utc = _EPOCH_UTC + timedelta(microseconds=captured_at_ns // _NS_PER_MICROSECOND)
    bars: list[OHLCV] = []
    sessions: dict[str, date] = {}
    for mic in sorted(by_mic):
        items = by_mic[mic]
        cal = calendars(mic)
        try:
            d = cal.latest_completed_session(captured_utc, delay)
        except CalendarRangeError:
            for _ in items:
                _count(skipped, "calendar_range")
            continue
        if d is None:
            for _ in items:
                _count(skipped, "no_completed_session")
            continue
        guard = _next_open_reason(cal, d, captured_utc)
        session_open_utc = cal.session_open_utc(d)
        for resolved, row in items:
            values = row.values
            prices = [_number(values.get(key)) for key in ("open", "high", "low", "close")]
            if any(price is None or price <= 0 for price in prices):
                _count(skipped, "null_price")
                continue
            open_, high, low, close = (float(price) for price in prices if price is not None)
            volume = _number(values.get("volume"))
            if volume is None or volume <= 0:
                _count(skipped, "no_volume")
                continue
            witnesses = 1
            stamp = _number(values.get(row_timestamp_field)) if row_timestamp_field else None
            if row_timestamp_field is not None and stamp is not None:
                whole = math.floor(stamp)
                stamp_day = session_date_of(int(whole), cal.tz)
                stamp_utc = _EPOCH_UTC + timedelta(seconds=whole)
                if stamp_day < d:
                    _count(skipped, "row_timestamp_before_session")
                    continue
                if stamp_day > d:
                    _count(skipped, "row_timestamp_after_session")
                    continue
                if stamp_utc < session_open_utc:
                    _count(skipped, "row_timestamp_before_session")
                    continue
                witnesses = 2
            else:
                if guard is not None:
                    _count(skipped, guard)
                    continue
            sessions[mic] = d
            values_now = SnapshotValues(
                open=open_, high=high, low=low, close=close, volume=float(volume)
            )
            if witnesses == 1 and previous.get(resolved.symbol) == values_now:
                _count(skipped, "stale_identical")
                continue
            tail = provenance_fields("session_snapshot", {"n_session_witnesses": witnesses})
            bars.append(
                OHLCV(
                    source=SOURCE,
                    symbol=resolved.symbol,
                    symbol_raw=resolved.ticker,
                    local_ts=captured_at_ns,
                    source_ts=daily_bar_ts_ns(d),
                    asset_class=AssetClass.EQUITY,
                    prov=tail.prov,
                    prov_basis=tail.prov_basis,
                    prov_confidence=tail.prov_confidence,
                    prov_inputs=tail.prov_inputs,
                    interval="1d",
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=float(volume),
                    currency=resolved.currency,
                    price_scale=resolved.price_scale,
                    adjustment=PriceAdjustment.NONE,
                )
            )
    bars.sort(key=lambda item: item.symbol)
    return EodSnapshot(
        captured_at_ns=captured_at_ns,
        bars=tuple(bars),
        sessions=dict(sorted(sessions.items())),
        skipped=dict(sorted(skipped.items())),
    )


def previous_snapshot_values(catalog: Catalog, symbols: Sequence[str]) -> dict[str, SnapshotValues]:
    """The latest TradingView daily bar per symbol in the lake, for the stale-identical rule.

    Values are bound as placeholders, never interpolated. An empty lake (no ``ohlcv`` channel)
    answers with nothing.
    """
    if "ohlcv" not in catalog.list_channels() or not symbols:
        return {}
    placeholders = ", ".join("?" for _ in symbols)
    sql = (
        "SELECT symbol, open, high, low, close, volume FROM ohlcv "
        "WHERE source = 'tradingview' AND interval = '1d' "
        f"AND symbol IN ({placeholders}) "
        "QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY source_ts DESC, local_ts DESC) = 1"
    )
    frame = catalog.query(sql, params=list(symbols))
    out: dict[str, SnapshotValues] = {}
    for row in frame.iter_rows(named=True):
        out[str(row["symbol"])] = SnapshotValues(
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )
    return out


# -------------------------------------------------------------------- provider


class TradingViewProvider(PullProvider):
    """The universe and the end-of-day snapshot of TradingView's scanner, one pass per run."""

    name = "tradingview"
    rest_url = "https://scanner.tradingview.com"
    usage_class: ClassVar[UsageClass] = UsageClass.INTERNAL
    supported_channels: ClassVar[frozenset[str] | None] = frozenset({"ohlcv", "instrument"})
    supported_mics: ClassVar[frozenset[str] | None] = _SUPPORTED_MICS
    unservable_channels: ClassVar[Mapping[str, str]] = MappingProxyType(
        {
            "quote": (
                "the scanner snapshot carries no bid or ask sizes, so a quote channel would be "
                "the last traded price under another name"
            ),
        }
    )
    unservable: ClassVar[Mapping[UnservableReason, str]] = MappingProxyType(
        {
            UnservableReason.BOT_CHALLENGE: "a consent, bot or redirect wall was served",
            UnservableReason.FORBIDDEN: "the scanner answered 401 or 403 to this client",
            UnservableReason.GEO_BLOCKED: "the scanner answered 451 for this location",
            UnservableReason.TIMEOUT: "the scanner request timed out on the client side",
            UnservableReason.UNREACHABLE: "the scanner host could not be reached",
        }
    )
    attribution: ClassVar[str | None] = None
    wants_settings: ClassVar[bool] = True

    @classmethod
    def grammar(cls) -> PrefixGrammar:
        return _grammar()

    def __init__(
        self,
        symbols: list[str],
        channels: list[str],
        out: Sink,
        registry: InstrumentRegistry,
        *,
        client: TradingViewClient | None = None,
        settings: Settings | None = None,
        master: ListingMaster | None = None,
        previous_loader: Callable[[Sequence[str]], Mapping[str, SnapshotValues]] | None = None,
        clock: Callable[[], int] = time.time_ns,
        **_: Any,
    ) -> None:
        # The opt-in is read before the base constructor, whose market-scope guard consults it.
        self._allow = allowed_countries(settings)
        self._client = client if client is not None else TradingViewClient()
        self._master = master
        self._settings = settings
        self._previous_loader = previous_loader
        self._clock = clock
        super().__init__(symbols, channels, out, registry)

    def _reject_unservable_mics(self) -> None:
        """Refuse a sanctioned MIC before the base scope guard, then apply that guard.

        ``["*"]`` means every scannable market, not a listing, so it is always in scope.
        """
        if self.symbols == ["*"]:
            return
        for symbol in self.symbols:
            if "@" not in symbol:
                continue
            try:
                _, mic = parse_listing_symbol(symbol)
            except MarketError:
                continue  # the base guard reports an unparseable or unlisted symbol
            if mic is not None:
                require_not_sanctioned(mic, allow=self._allow)
        super()._reject_unservable_mics()

    def _scope(self) -> tuple[TradingViewMarket, ...]:
        """The markets this pass reads. A sanctioned scope is refused before any request."""
        if self.symbols == ["*"]:
            return scannable_markets(allow=self._allow)
        found: dict[str, TradingViewMarket] = {}
        for symbol in self.symbols:
            _, mic = parse_listing_symbol(symbol)
            if mic is None:
                markets = markets_for_country(US_COUNTRY)
                label = US_COUNTRY
            else:
                require_not_sanctioned(mic, allow=self._allow)
                markets = markets_for_mic(mic)
                label = mic
            if not markets:
                raise ValueError(f"no TradingView market is named for {label}")
            for market in markets:
                found[market.market] = market
        return tuple(found[name] for name in sorted(found))

    def _previous_for(self, symbols: Sequence[str]) -> dict[str, SnapshotValues]:
        if self._previous_loader is not None:
            return dict(self._previous_loader(symbols))
        if self._settings is None:
            raise ValueError("tradingview EOD snapshots need a previous snapshot in the lake")
        from crocodile.core.store.catalog import Catalog

        return previous_snapshot_values(Catalog(self._settings.data_dir), symbols)

    async def _run_instrument(self, scope: Sequence[TradingViewMarket]) -> None:
        master = self._master
        owned: ListingMaster | None = None
        star = self.symbols == ["*"]
        if master is None and star and self._settings is not None:
            owned = ListingMaster.for_data_dir(self._settings.data_dir)
            master = owned
        wanted = None if star else frozenset(self.symbols)
        try:
            for market in scope:
                require_market_not_sanctioned(market, allow=self._allow)
                scan = await self._client.scan(market.market, UNIVERSE_COLUMNS)
                snapshot = universe_from_scan(scan, captured_at_ns=self._clock(), allow=self._allow)
                for record in snapshot.records:
                    if wanted is None or record.symbol in wanted:
                        await self.out.put(record)
                if star and master is not None:
                    report = apply_universe(master, snapshot)
                    log.info(
                        "tradingview %s: %d upserted, %d conflicts, unseen marked %s",
                        market.market,
                        report.upserted,
                        len(report.conflicts),
                        list(report.unseen_marked_mics),
                    )
        finally:
            if owned is not None:
                owned.close()

    async def _run_ohlcv(self, scope: Sequence[TradingViewMarket]) -> None:
        star = self.symbols == ["*"]
        wanted = None if star else frozenset(self.symbols)
        for market in scope:
            require_market_not_sanctioned(market, allow=self._allow)
            captured_at_ns = self._clock()
            scan = await self._client.scan(market.market, EOD_COLUMNS)
            symbols = sorted(
                {
                    resolved.symbol
                    for row in scan.rows
                    if (resolved := _resolve(row, self._allow, Counter())) is not None
                    and (wanted is None or resolved.symbol in wanted)
                }
            )
            previous = self._previous_for(symbols)
            eod = eod_bars_from_scan(
                scan,
                captured_at_ns=captured_at_ns,
                previous=previous,
                delay=DEFAULT_EOD_DELAY,
                row_timestamp_field=ROW_TIMESTAMP_FIELD,
                allow=self._allow,
            )
            for bar in eod.bars:
                if wanted is None or bar.symbol in wanted:
                    await self.out.put(bar)

    async def run(self, max_reconnects: int = -1) -> None:
        """One pass over the scope, then return: the universe and/or the EOD snapshot."""
        wanted_channels = {CHANNEL_SUCCESSORS.get(channel, channel) for channel in self.channels}
        if "ohlcv" in wanted_channels and self._previous_loader is None and self._settings is None:
            raise ValueError(
                "tradingview EOD snapshots compare against the previous snapshot in the lake; "
                "pass settings (the lake) or a previous_loader"
            )
        scope = self._scope()
        if "instrument" in wanted_channels:
            await self._run_instrument(scope)
        if "ohlcv" in wanted_channels:
            await self._run_ohlcv(scope)

    async def backfill(
        self, channel: str, symbol: str, start_ns: int, end_ns: int
    ) -> AsyncIterator[Record]:
        raise ValueError(
            "tradingview publishes the current session only and keeps no history; take a "
            "snapshot with collect --sources tradingview or let the universal orchestrator "
            "schedule it"
        )
        yield  # pragma: no cover - unreachable; makes this an async generator

    async def close(self) -> None:
        await self._client.close()
