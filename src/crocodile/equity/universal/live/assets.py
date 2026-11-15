"""Instruments beyond listed shares: futures, options, currencies, indices and government bonds.

The live layer answers for a listing of an allow-listed stock exchange. Derivatives, currency
pairs, indices and bonds are not listings of those exchanges, so they are not ``LOCAL@MIC``
symbols and they do not pass through the listing grammar. They are named the way the one keyless
source that covers them names them: ``VENUE:SYMBOL`` (``CME_MINI:ES1!``, ``FX_IDC:EURUSD``,
``TVC:US10Y``, ``BIST:XU100``), which is also the shape a crypto symbol already has
(``deribit:BTC-PERPETUAL``).

What is read, all from TradingView's scanner with plain keyless requests:

- :meth:`AssetFeed.board`: one venue's instruments of a kind, most traded first.
- :meth:`AssetFeed.quotes`: the current row of any tickers, of any kind, in one request.
- :meth:`AssetFeed.option_series`: the series an underlying lists (each contract family and
  expiry, with its size), from a scan that reads three columns and no prices.
- :meth:`AssetFeed.option_chain`: every listed option of an underlying, or of one of its
  series, with the bid, ask, theoretical price, implied volatility and greeks the scanner
  states, as ``OptionsChain`` records.
- :meth:`AssetFeed.minute_bars`: today's one-minute bars from Yahoo's chart endpoint, for the
  instruments whose Yahoo spelling is derivable (currency pairs, continuous futures of the US
  venues) or recorded in ``data/yahoo_index_map.csv``.

Ruling: an :class:`AssetQuote` holds what the scanner row states and nothing else. The scanner
publishes no resting size for any of these instruments, so there is no book and no ``Quote``
record: a futures row becomes a ``DerivativeTicker`` (and an ``OpenInterest`` when it states
one), an index row an ``IndexValue``. A currency pair's bid and ask stay on the quote.

Ruling: ``update_mode`` decides ``delay_s`` per row, as it does for the equity scan. A futures
row of a delayed venue is a delayed row and says so.

Ruling: an option scan is whole or it is refused. The scanner states how many rows match
(``totalCount``); an answer that carries fewer is not a chain, and raises instead of passing
for one.

Ruling: the venue tables in ``data/asset_venues.csv`` are evidence, not configuration: each row
carries the count the scanner returned and the date it was measured.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import csv
import functools
import importlib.resources
import io
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final
from urllib.parse import quote

from crocodile.core.markets.currency import parse_quote_unit
from crocodile.core.markets.errors import UnknownCurrencyError
from crocodile.core.ratelimit import budget
from crocodile.core.schema.enums import AssetClass, OptType
from crocodile.core.schema.provenance import Provenance, provenance_fields, register_basis
from crocodile.core.schema.records import (
    DerivativeTicker,
    IndexValue,
    OpenInterest,
    OptionsChain,
    Record,
)
from crocodile.equity.universal.http import (
    AiohttpFetcher,
    Fetcher,
    HttpRequest,
    HttpResponse,
    raise_if_unservable,
)
from crocodile.equity.universal.live.base import LivePayloadError, decode_json, to_float
from crocodile.equity.universal.live.sources.tradingview import delay_of
from crocodile.equity.universal.live.sources.yahoo_chart import MinuteBar, parse_minute_bars

__all__ = [
    "COLUMNS",
    "OPTION_CHAIN_LIMIT",
    "OPTION_COLUMNS",
    "OPTION_SERIES_COLUMNS",
    "SOURCE",
    "AssetFeed",
    "AssetKind",
    "AssetQuote",
    "OptionSeries",
    "asset_records",
    "board_body",
    "known_venues",
    "option_body",
    "parse_option_chain",
    "parse_option_series",
    "parse_rows",
    "series_body",
    "split_ticker",
    "yahoo_symbol_for",
]

SOURCE: Final = "tradingview"
HOST: Final = "scanner.tradingview.com"
SCAN_URL: Final = "https://scanner.tradingview.com/{market}/scan"
YAHOO_SOURCE: Final = "yahoo_chart"
YAHOO_HOST: Final = "query2.finance.yahoo.com"
YAHOO_URL: Final = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
_PACKAGE: Final = "crocodile.equity.universal.live"
_NS_PER_DAY: Final = 86_400_000_000_000
_EPOCH: Final = date(1970, 1, 1)
_TICKER: Final = re.compile(r"([A-Z0-9_]+):(.+)")
_PAIR: Final = re.compile(r"[A-Z]{6}")
_CONTINUOUS: Final = re.compile(r"([A-Z0-9]+)1!")


class AssetKind(StrEnum):
    """The instrument classes the scanner covers beyond listed shares."""

    FUTURE = "future"
    FOREX = "forex"
    INDEX = "index"
    BOND = "bond"
    OPTION = "option"


SCAN_MARKET: Final[Mapping[AssetKind, str]] = MappingProxyType(
    {
        AssetKind.FUTURE: "futures",
        AssetKind.FOREX: "forex",
        AssetKind.INDEX: "cfd",
        AssetKind.BOND: "bonds",
        AssetKind.OPTION: "options",
    }
)
"""The scanner market each kind is read from. ``bonds`` is the government-bond table."""

DEFAULT_VENUE: Final[Mapping[AssetKind, str]] = MappingProxyType(
    {AssetKind.FOREX: "FX_IDC", AssetKind.BOND: "TVC"}
)
"""The venue a board of this kind shows when the caller names none: the composite currency
feed, and the government-bond table's only venue."""

_ROW_TYPES: Final[Mapping[str, AssetKind]] = MappingProxyType(
    {
        "futures": AssetKind.FUTURE,
        "forex": AssetKind.FOREX,
        "index": AssetKind.INDEX,
        "bond": AssetKind.BOND,
    }
)

COLUMNS: Final[tuple[str, ...]] = (
    "name",
    "description",
    "type",
    "exchange",
    "currency",
    "close",
    "bid",
    "ask",
    "open",
    "high",
    "low",
    "volume",
    "change",
    "update_mode",
    "open_interest",
    "expiration",
    "root",
    "coupon",
    "maturity_date",
)
OPTION_COLUMNS: Final[tuple[str, ...]] = (
    "name",
    "bid",
    "ask",
    "strike",
    "expiration",
    "option-type",
    "theoPrice",
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "currency",
    "root",
)
OPTION_SERIES_COLUMNS: Final[tuple[str, ...]] = ("expiration", "root", "underlying_symbol")
"""What a series scan reads: enough to name each series and the underlying it belongs to."""

OPTION_CHAIN_LIMIT: Final = 100_000
"""The most rows one option scan asks for. The largest chain measured is ``SP:SPX`` with 31,016
contracts (2026-11-12)."""

_US_FUTURES_VENUES: Final = frozenset(
    {"CME", "CME_MINI", "CBOT", "CBOT_MINI", "NYMEX", "NYMEX_MINI", "COMEX", "COMEX_MINI"}
)
"""Venues whose continuous front contract ``ROOT1!`` Yahoo spells ``ROOT=F``."""


@register_basis("scanner_snapshot", level=Provenance.NATIVE, inputs=[])
def _scanner_snapshot(_: Mapping[str, Any]) -> float:
    """A scanner's current row for an instrument, copied: 1.0, by definition.

    Every number on the record is the scanner's own statement of where a future, an index or
    an option stands now: its last price, its open interest, its implied volatility and
    greeks. Each is read and none is reconstructed, so the record is fully sampled at its own
    level and nothing about the method varies between two rows.

    The basis exists for its name. The scanner is an aggregator, not the venue: its row can be
    delayed (the snapshot carries the delay the row states), and an option's greeks are the
    scanner's model, not the exchange's. ``WHERE prov_basis = 'scanner_snapshot'`` separates
    these rows from a venue's own derivative ticker, which ``native`` alone could not.
    """
    return 1.0


@dataclass(frozen=True, slots=True, kw_only=True)
class AssetQuote:
    """One scanner row. Every optional field is ``None`` when the row does not state it."""

    kind: AssetKind
    ticker: str
    """``VENUE:SYMBOL`` exactly as the scanner spells it."""

    venue: str
    symbol: str
    captured_ns: int
    name: str | None = None
    currency: str | None = None
    price_scale: int | None = None
    last: float | None = None
    bid: float | None = None
    ask: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None
    volume: float | None = None
    open_interest: float | None = None
    expiry: date | None = None
    """Last trading day of a dated future; ``None`` for a continuous contract."""

    root: str | None = None
    """The contract family of a future (``NSE:NIFTY``), as the scanner names it."""

    coupon: float | None = None
    maturity: date | None = None
    delay_s: int | None = None

    @property
    def change_pct(self) -> float | None:
        if self.last is None or self.prev_close is None or not self.prev_close > 0.0:
            return None
        return (self.last - self.prev_close) / self.prev_close * 100.0


@dataclass(frozen=True, slots=True, kw_only=True)
class OptionSeries:
    """One listed series of an underlying: a contract family and an expiry.

    An index can list several families that expire on one day (``SPX`` and ``SPXW`` on a third
    Friday), with the same strikes. A chain is therefore one series, never one day.
    """

    underlying: str
    root: str
    expiry: date
    contracts: int
    """How many contracts the scanner lists in the series, calls and puts together."""


def split_ticker(ticker: str) -> tuple[str, str]:
    """``VENUE:SYMBOL`` as ``(venue, symbol)``; anything else is a ``ValueError``."""
    match = _TICKER.fullmatch(ticker)
    if match is None:
        raise ValueError(f"not a VENUE:SYMBOL ticker: {ticker!r}")
    return match.group(1), match.group(2)


def _yyyymmdd(value: Any) -> date | None:
    number = to_float(value)
    if number is None:
        return None
    try:
        whole = int(number)
        return date(whole // 10_000, whole // 100 % 100, whole % 100)
    except (ValueError, OverflowError):
        # Not a calendar day: a month 13, or a number no date can hold.
        return None


def _positive(value: Any) -> float | None:
    number = to_float(value)
    return number if number is not None and number > 0.0 else None


def _non_negative(value: Any) -> float | None:
    number = to_float(value)
    return number if number is not None and number >= 0.0 else None


def _document(body: bytes) -> Mapping[str, Any]:
    document = decode_json(SOURCE, body)
    if not isinstance(document, dict):
        raise LivePayloadError(SOURCE, "no data array in the scan answer")
    return document


def _rows_of(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = document.get("data")
    if rows is None and document.get("totalCount") == 0:
        return []
    if not isinstance(rows, list):
        raise LivePayloadError(SOURCE, "no data array in the scan answer")
    return [row for row in rows if isinstance(row, dict)]


def _rows(body: bytes) -> list[Mapping[str, Any]]:
    return _rows_of(_document(body))


def _whole_rows(body: bytes) -> list[Mapping[str, Any]]:
    """The rows of an option scan, refused when the source answered only a part of them.

    The scanner states how many rows match. An answer that states no count, or one that
    cannot be read as a number, cannot be shown to be whole and is refused as well.
    """
    document = _document(body)
    rows = _rows_of(document)
    answered = document.get("data")
    count = len(answered) if isinstance(answered, list) else 0
    total = document.get("totalCount")
    if isinstance(total, bool) or not isinstance(total, (int, float)) or total != total:
        raise LivePayloadError(SOURCE, "the option scan states no row count")
    if total > count:
        raise LivePayloadError(
            SOURCE,
            f"the option scan answered {count} of {total:g} rows; narrow it or raise limit",
        )
    return rows


def parse_rows(body: bytes, *, captured_ns: int) -> list[AssetQuote]:
    """Every row of a scan made with :data:`COLUMNS` whose type is a covered kind."""
    out: list[AssetQuote] = []
    for row in _rows(body):
        ticker = row.get("s")
        values = row.get("d")
        if not isinstance(ticker, str) or not isinstance(values, list):
            continue
        if len(values) != len(COLUMNS):
            continue
        cells = dict(zip(COLUMNS, values, strict=True))
        kind = _ROW_TYPES.get(str(cells["type"]))
        if kind is None:
            continue
        try:
            venue, symbol = split_ticker(ticker)
        except ValueError:
            continue
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
        last = _positive(cells["close"])
        change = to_float(cells["change"])
        prev_close: float | None = None
        if last is not None and change is not None and change > -100.0:
            prev_close = round(last / (1.0 + change / 100.0), 8)
        description = cells["description"]
        root = cells["root"]
        out.append(
            AssetQuote(
                kind=kind,
                ticker=ticker,
                venue=venue,
                symbol=symbol,
                captured_ns=captured_ns,
                name=description if isinstance(description, str) else None,
                currency=currency,
                price_scale=price_scale,
                last=last,
                bid=_positive(cells["bid"]),
                ask=_positive(cells["ask"]),
                open=_positive(cells["open"]),
                high=_positive(cells["high"]),
                low=_positive(cells["low"]),
                prev_close=prev_close,
                volume=_non_negative(cells["volume"]),
                open_interest=_non_negative(cells["open_interest"]),
                expiry=_yyyymmdd(cells["expiration"]),
                root=root if isinstance(root, str) else None,
                coupon=to_float(cells["coupon"]),
                maturity=_yyyymmdd(cells["maturity_date"]),
                delay_s=delay_of(cells["update_mode"]),
            )
        )
    return out


def board_body(venue: str | None, limit: int) -> dict[str, Any]:
    """The scan of one venue's instruments (or of the whole market), most traded first."""
    body: dict[str, Any] = {
        "columns": list(COLUMNS),
        "sort": {"sortBy": "volume", "sortOrder": "desc"},
        "range": [0, limit],
    }
    if venue is not None:
        body["filter"] = [{"left": "exchange", "operation": "in_range", "right": [venue]}]
    return body


def _yyyymmdd_of(day: date) -> int:
    return day.year * 10_000 + day.month * 100 + day.day


def option_body(
    underlying: str, limit: int, *, expiry: date | None = None, root: str | None = None
) -> dict[str, Any]:
    """The scan of the listed options of one underlying ticker.

    ``expiry`` and ``root`` narrow it to one day and one contract family; both together name
    one series.
    """
    body: dict[str, Any] = {
        "columns": list(OPTION_COLUMNS),
        "index_filters": [{"name": "underlying_symbol", "values": [underlying]}],
        "range": [0, limit],
    }
    narrowed: list[dict[str, Any]] = []
    if expiry is not None:
        narrowed.append({"left": "expiration", "operation": "equal", "right": _yyyymmdd_of(expiry)})
    if root is not None:
        narrowed.append({"left": "root", "operation": "equal", "right": root})
    if narrowed:
        body["filter"] = narrowed
    return body


def series_body(underlyings: Sequence[str], limit: int) -> dict[str, Any]:
    """The scan that lists the series of one underlying, or of several candidate tickers."""
    return {
        "columns": list(OPTION_SERIES_COLUMNS),
        "index_filters": [{"name": "underlying_symbol", "values": list(underlyings)}],
        "range": [0, limit],
    }


def parse_option_series(body: bytes) -> list[OptionSeries]:
    """The series of a series scan, in underlying, expiry and family order.

    A row without an expiry, a family or an underlying names no series and is dropped.
    """
    counts: dict[tuple[str, date, str], int] = {}
    for row in _whole_rows(body):
        values = row.get("d")
        if not isinstance(values, list) or len(values) != len(OPTION_SERIES_COLUMNS):
            continue
        cells = dict(zip(OPTION_SERIES_COLUMNS, values, strict=True))
        expiry = _yyyymmdd(cells["expiration"])
        root = cells["root"]
        underlying = cells["underlying_symbol"]
        if expiry is None or not isinstance(root, str) or not root:
            continue
        if not isinstance(underlying, str) or not underlying:
            continue
        key = (underlying, expiry, root)
        counts[key] = counts.get(key, 0) + 1
    return [
        OptionSeries(underlying=underlying, root=root, expiry=expiry, contracts=contracts)
        for (underlying, expiry, root), contracts in sorted(counts.items())
    ]


def _expiry_ns(day: date) -> int:
    return (day - _EPOCH).days * _NS_PER_DAY


def parse_option_chain(body: bytes, underlying: str, *, captured_ns: int) -> list[OptionsChain]:
    """One ``OptionsChain`` record per contract of an option scan, in strike and expiry order.

    A row without a strike, an expiry or a recognised right is not a contract and is dropped:
    the whole-answer rule is about the rows the source sent, not about how many of them name
    a contract.
    ``mark_price`` is the scanner's theoretical price and ``mark_iv`` its implied volatility;
    both, and the greeks, are the scanner's model. Sizes, volume and open interest are not
    published for options and stay ``None``. An answer that carries fewer rows than the scanner
    says match is refused (``LivePayloadError``).
    """
    tail = provenance_fields("scanner_snapshot")
    out: list[OptionsChain] = []
    for row in _whole_rows(body):
        ticker = row.get("s")
        values = row.get("d")
        if not isinstance(ticker, str) or not isinstance(values, list):
            continue
        if len(values) != len(OPTION_COLUMNS):
            continue
        cells = dict(zip(OPTION_COLUMNS, values, strict=True))
        strike = _positive(cells["strike"])
        expiry = _yyyymmdd(cells["expiration"])
        right = {"call": OptType.CALL, "put": OptType.PUT}.get(str(cells["option-type"]))
        if strike is None or expiry is None or right is None:
            continue
        out.append(
            OptionsChain(
                source=SOURCE,
                symbol=ticker,
                symbol_raw=ticker,
                local_ts=captured_ns,
                asset_class=AssetClass.EQUITY,
                source_ts=None,
                prov=tail.prov,
                prov_basis=tail.prov_basis,
                prov_confidence=tail.prov_confidence,
                prov_inputs=tail.prov_inputs,
                underlying=underlying,
                underlying_price=None,
                strike=strike,
                expiry=_expiry_ns(expiry),
                opt_type=right,
                mark_price=_non_negative(cells["theoPrice"]),
                mark_iv=_non_negative(cells["iv"]),
                bid_px=_non_negative(cells["bid"]),
                ask_px=_non_negative(cells["ask"]),
                delta=to_float(cells["delta"]),
                gamma=to_float(cells["gamma"]),
                vega=to_float(cells["vega"]),
                theta=to_float(cells["theta"]),
                rho=to_float(cells["rho"]),
            )
        )
    out.sort(key=lambda record: (record.expiry, record.strike, record.opt_type.value))
    return out


def asset_records(quote_row: AssetQuote) -> list[Record]:
    """The canonical records one scanner row supports.

    A future with a last price is a ``DerivativeTicker`` (carrying its open interest when the
    row states one), plus an ``OpenInterest`` for that figure. An index with a value is an
    ``IndexValue``. A currency pair or a bond has no record here: no canonical record states a
    bid and an ask without sizes, and none is invented for it.
    """
    tail = provenance_fields("scanner_snapshot")
    records: list[Record] = []
    if quote_row.kind is AssetKind.FUTURE and quote_row.last is not None:
        records.append(
            DerivativeTicker(
                source=SOURCE,
                symbol=quote_row.ticker,
                symbol_raw=quote_row.ticker,
                local_ts=quote_row.captured_ns,
                asset_class=AssetClass.EQUITY,
                source_ts=None,
                prov=tail.prov,
                prov_basis=tail.prov_basis,
                prov_confidence=tail.prov_confidence,
                prov_inputs=tail.prov_inputs,
                last_price=quote_row.last,
                open_interest=quote_row.open_interest,
            )
        )
        if quote_row.open_interest is not None:
            records.append(
                OpenInterest(
                    source=SOURCE,
                    symbol=quote_row.ticker,
                    symbol_raw=quote_row.ticker,
                    local_ts=quote_row.captured_ns,
                    asset_class=AssetClass.EQUITY,
                    source_ts=None,
                    prov=tail.prov,
                    prov_basis=tail.prov_basis,
                    prov_confidence=tail.prov_confidence,
                    prov_inputs=tail.prov_inputs,
                    open_interest=quote_row.open_interest,
                )
            )
    elif quote_row.kind is AssetKind.INDEX and quote_row.last is not None:
        records.append(
            IndexValue(
                source=SOURCE,
                symbol=quote_row.ticker,
                symbol_raw=quote_row.ticker,
                local_ts=quote_row.captured_ns,
                asset_class=AssetClass.EQUITY,
                source_ts=None,
                prov=tail.prov,
                prov_basis=tail.prov_basis,
                prov_confidence=tail.prov_confidence,
                prov_inputs=tail.prov_inputs,
                value=quote_row.last,
            )
        )
    return records


@functools.cache
def known_venues(kind: AssetKind) -> Mapping[str, int]:
    """Venues the scanner listed for a kind, with the instrument count it returned.

    Read from ``data/asset_venues.csv`` (measured, dated). Pure: no request is made, so a
    catalogue of venues can be shown before any network call.
    """
    raw = importlib.resources.files(_PACKAGE).joinpath("data/asset_venues.csv").read_text("ascii")
    table: dict[str, int] = {}
    for row in csv.DictReader(io.StringIO(raw)):
        if row["kind"] == kind.value:
            table[row["venue"]] = int(row["count"])
    return MappingProxyType(table)


@functools.cache
def _index_map() -> Mapping[str, str]:
    raw = (
        importlib.resources.files(_PACKAGE).joinpath("data/yahoo_index_map.csv").read_text("ascii")
    )
    return MappingProxyType(
        {row["ticker"]: row["yahoo"] for row in csv.DictReader(io.StringIO(raw))}
    )


def yahoo_symbol_for(kind: AssetKind, ticker: str) -> str | None:
    """Yahoo's spelling of an instrument, or ``None`` when it cannot be derived.

    - A currency pair of six letters is ``EURUSD=X``.
    - The continuous front contract of a US futures venue (``CME_MINI:ES1!``) is ``ES=F``.
    - An index is looked up in ``data/yahoo_index_map.csv``, whose rows were each verified by a
      chart answer of type ``INDEX``.

    Nothing else is guessed: a dated contract, a future of another venue and a bond have no
    Yahoo spelling here, and so no minute bars.
    """
    try:
        venue, symbol = split_ticker(ticker)
    except ValueError:
        return None
    if kind is AssetKind.FOREX:
        return f"{symbol}=X" if _PAIR.fullmatch(symbol) else None
    if kind is AssetKind.FUTURE:
        match = _CONTINUOUS.fullmatch(symbol)
        if match is not None and venue in _US_FUTURES_VENUES:
            return f"{match.group(1)}=F"
        return None
    if kind is AssetKind.INDEX:
        return _index_map().get(ticker)
    return None


class AssetFeed:
    """Boards, quotes, option chains and minute bars of the non-share instruments."""

    def __init__(
        self, *, fetch: Fetcher | None = None, clock_ns: Callable[[], int] | None = None
    ) -> None:
        self._owned: AiohttpFetcher | None = None
        if fetch is None:
            self._owned = AiohttpFetcher()
            fetch = self._owned
        self._fetch: Fetcher = fetch
        self._clock: Callable[[], int] = clock_ns if clock_ns is not None else time.time_ns

    async def close(self) -> None:
        if self._owned is not None:
            await self._owned.close()

    async def _scan(self, market: str, body: dict[str, Any]) -> HttpResponse:
        await budget(SOURCE, HOST).acquire()
        response = await self._fetch(
            HttpRequest(
                source=SOURCE, method="POST", url=SCAN_URL.format(market=market), json_body=body
            )
        )
        raise_if_unservable(response, source=SOURCE, expect="json")
        if not 200 <= response.status <= 299 or not response.body:
            raise LivePayloadError(SOURCE, f"HTTP {response.status}")
        return response

    async def board(
        self, kind: AssetKind, venue: str | None = None, *, limit: int = 200
    ) -> list[AssetQuote]:
        """One venue's instruments of ``kind``, most traded first (at most ``limit``).

        ``venue`` defaults to :data:`DEFAULT_VENUE` for kinds that have one; a futures board
        needs it named, because the futures market spans every derivatives exchange. Options
        have no board: ask for a chain.
        """
        if kind is AssetKind.OPTION:
            raise ValueError("options have no board; use option_chain(underlying)")
        chosen = venue if venue is not None else DEFAULT_VENUE.get(kind)
        response = await self._scan(SCAN_MARKET[kind], board_body(chosen, limit))
        rows = parse_rows(response.body, captured_ns=self._clock())
        return [row for row in rows if row.kind is kind]

    async def quotes(self, tickers: Sequence[str]) -> list[AssetQuote]:
        """The current rows of any tickers, of any kind, in one request."""
        wanted = [ticker for ticker in dict.fromkeys(tickers) if _TICKER.fullmatch(ticker)]
        if not wanted:
            return []
        body = {"columns": list(COLUMNS), "symbols": {"tickers": wanted}}
        response = await self._scan("global", body)
        return parse_rows(response.body, captured_ns=self._clock())

    async def option_series(self, underlying: str | Sequence[str]) -> list[OptionSeries]:
        """The series an underlying lists: each contract family and expiry, with its size.

        Several candidate tickers may be named when the venue a share is listed on is not
        known (``NASDAQ:IBM``, ``NYSE:IBM``): each series states the underlying it belongs to.
        One request, three columns, no prices.
        """
        wanted = [underlying] if isinstance(underlying, str) else list(dict.fromkeys(underlying))
        if not wanted:
            return []
        for ticker in wanted:
            split_ticker(ticker)
        response = await self._scan(
            SCAN_MARKET[AssetKind.OPTION], series_body(wanted, OPTION_CHAIN_LIMIT)
        )
        return parse_option_series(response.body)

    async def option_chain(
        self,
        underlying: str,
        *,
        expiry: date | None = None,
        root: str | None = None,
        limit: int = OPTION_CHAIN_LIMIT,
    ) -> list[OptionsChain]:
        """The listed options of an underlying ticker (``NASDAQ:AAPL``, ``BIST:THYAO``).

        Without ``expiry`` and ``root`` that is every contract of every series, which for an
        index is tens of thousands of rows. Name both (from :meth:`option_series`) to read one
        series. The answer is whole or the call raises: nothing is cut off silently.
        """
        split_ticker(underlying)
        response = await self._scan(
            SCAN_MARKET[AssetKind.OPTION],
            option_body(underlying, limit, expiry=expiry, root=root),
        )
        return parse_option_chain(response.body, underlying, captured_ns=self._clock())

    async def minute_bars(self, kind: AssetKind, ticker: str) -> list[MinuteBar]:
        """Today's one-minute bars of an instrument Yahoo spells; empty when it spells none."""
        spelled = yahoo_symbol_for(kind, ticker)
        if spelled is None:
            return []
        await budget(YAHOO_SOURCE, YAHOO_HOST).acquire()
        response = await self._fetch(
            HttpRequest(
                source=YAHOO_SOURCE,
                method="GET",
                url=YAHOO_URL.format(symbol=quote(spelled, safe="")),
                params=(("interval", "1m"), ("range", "1d")),
            )
        )
        raise_if_unservable(response, source=YAHOO_SOURCE, expect="json")
        if response.status == 404 or not response.body:
            return []
        if not 200 <= response.status <= 299:
            raise LivePayloadError(YAHOO_SOURCE, f"HTTP {response.status}")
        return parse_minute_bars(response.body)
