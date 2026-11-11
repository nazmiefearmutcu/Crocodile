"""Tests for the yahoo_chart universal source (task 10b).

Offline throughout. Fixtures are recorded Yahoo payloads under
``fixtures/yahoo_chart`` or hand-built ones whose file carries a ``_synthetic`` key; a test
that uses a synthetic payload says so in its docstring. No test reads the network.
"""

from __future__ import annotations

import asyncio
import csv
import importlib.resources
import io
import pathlib
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import msgspec
import pytest

from crocodile.core.config import Settings
from crocodile.core.markets.currency import UnknownCurrencyError, parse_quote_unit
from crocodile.core.markets.errors import NoGrammarForMarket, SanctionedMarketError
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.master import ListingMaster, ListingObservation
from crocodile.core.markets.usage import UsageClass
from crocodile.core.ratelimit.token_bucket import TokenBucket
from crocodile.core.schema.enums import AssetClass, CorpActionType, SecurityType
from crocodile.core.schema.provenance import Provenance
from crocodile.core.schema.records import OHLCV, CorporateAction
from crocodile.core.sink.memory import MemorySink
from crocodile.equity.providers import factory
from crocodile.equity.providers.base import TransientProviderError
from crocodile.equity.reference.registry import InstrumentRegistry
from crocodile.equity.universal import yahoo_chart as yc
from crocodile.equity.universal.base import (
    SourceUnservableError,
    UnservableReason,
    contract_violations,
    daily_bar_ts_ns,
)
from crocodile.equity.universal.http import HttpRequest, HttpResponse

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "yahoo_chart"
CAPTURED_NS = 1_794_000_000_000_000_000

# Recorded fixture -> (Yahoo exchangeName, the MIC the name must map to). Evidence: the R&D
# fixture set yahoo_chart_g8 (2026-11-08); the 2y/1mo files are the dividend payloads.
RECORDED: dict[str, tuple[str, str]] = {
    "THYAO.IS_5d_1d.json": ("IST", "XIST"),
    "7203.T_5d_1d.json": ("JPX", "XJPX"),
    "2330.TW_5d_1d.json": ("TAI", "XTAI"),
    "BHP.AX_5d_1d.json": ("ASX", "XASX"),
    "PTT.BK_5d_1d.json": ("SET", "XBKK"),
    "NOVO-B.CO_5d_1d.json": ("CPH", "XCSE"),
    "ALV.F_5d_1d.json": ("FRA", "XFRA"),
    "NOKIA.HE_5d_1d.json": ("HEL", "XHEL"),
    "005930.KS_2y_1mo.json": ("KSC", "XKRX"),
    "RELIANCE.NS_2y_1mo.json": ("NSI", "XNSE"),
    "0700.HK_2y_1mo.json": ("HKG", "XHKG"),
    "600519.SS_2y_1mo.json": ("SHH", "XSHG"),
    "2330.TW_2y_1mo.json": ("TAI", "XTAI"),
    "7203.T_2y_1mo.json": ("JPX", "XJPX"),
    "BHP.AX_2y_1mo.json": ("ASX", "XASX"),
    "SAP.DE_2y_1mo.json": ("GER", "XETR"),
}


def _decode(name: str) -> dict[str, Any]:
    return msgspec.json.decode((FIXTURES / name).read_bytes())


def _csv_rows() -> list[dict[str, str]]:
    text = (
        importlib.resources.files("crocodile.equity.universal")
        .joinpath("data/yahoo_chart_exchange_names.csv")
        .read_text(encoding="utf-8")
    )
    return list(csv.DictReader(io.StringIO(text)))


@pytest.mark.parametrize(("name", "exchange", "mic"), [(k, *v) for k, v in RECORDED.items()])
def test_every_recorded_fixture_exchange_name_is_in_the_table(
    name: str, exchange: str, mic: str
) -> None:
    meta = _decode(name)["chart"]["result"][0]["meta"]
    assert meta["exchangeName"] == exchange
    assert mic in yc.load_exchange_names()[exchange]


def test_the_exchange_name_table_only_names_listed_markets() -> None:
    registry = ExchangeRegistry()
    pairs = [(row["exchange_name"], row["mic"]) for row in _csv_rows()]
    assert len(pairs) == len(set(pairs)), "a (name, mic) row is duplicated"
    for _name, mic in pairs:
        assert mic in registry, f"{mic} is not an allow-listed operating MIC"


def test_the_exchange_name_table_is_sorted_by_name() -> None:
    names = [row["exchange_name"] for row in _csv_rows()]
    assert names == sorted(names)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("EQUITY", SecurityType.CS),
        ("ETF", SecurityType.ETF),
        ("MUTUALFUND", SecurityType.FUND),
        ("INDEX", SecurityType.INDEX),
        ("CURRENCY", SecurityType.UNKNOWN),
        (None, SecurityType.UNKNOWN),
    ],
)
def test_instrument_type_maps_to_security_type(raw: str | None, expected: SecurityType) -> None:
    assert yc.security_type_for(raw) is expected


# --- Step 6: daily parse ----------------------------------------------------------------

# Recorded 5d/1d payload -> (canonical symbol, local, MIC, Yahoo symbol, exchange timezone).
FIVE_DAY: dict[str, tuple[str, str, str, str, str]] = {
    "THYAO.IS_5d_1d.json": ("THYAO@XIST", "THYAO", "XIST", "THYAO.IS", "Europe/Istanbul"),
    "PTT.BK_5d_1d.json": ("PTT@XBKK", "PTT", "XBKK", "PTT.BK", "Asia/Bangkok"),
    "NOVO-B.CO_5d_1d.json": ("NOVO-B@XCSE", "NOVO-B", "XCSE", "NOVO-B.CO", "Europe/Copenhagen"),
    "ALV.F_5d_1d.json": ("ALV@XFRA", "ALV", "XFRA", "ALV.F", "Europe/Berlin"),
    "NOKIA.HE_5d_1d.json": ("NOKIA@XHEL", "NOKIA", "XHEL", "NOKIA.HE", "Europe/Helsinki"),
    "2330.TW_5d_1d.json": ("2330@XTAI", "2330", "XTAI", "2330.TW", "Asia/Taipei"),
    "BHP.AX_5d_1d.json": ("BHP@XASX", "BHP", "XASX", "BHP.AX", "Australia/Sydney"),
    "7203.T_5d_1d.json": ("7203@XJPX", "7203", "XJPX", "7203.T", "Asia/Tokyo"),
}
# Every session in the 5d payloads is on or before this date, so no daily bar is incomplete.
ALL_DONE = date(2026, 12, 31)


def _listing(name: str) -> yc.ChartListing:
    symbol, local, mic, yahoo, tz = FIVE_DAY[name]
    return yc.ChartListing(
        symbol=symbol,
        local=local,
        mics=frozenset({mic}),
        yahoo_symbol=yahoo,
        tz=ZoneInfo(tz),
        calendar_mic=mic,
    )


def _parse(
    payload: dict[str, Any],
    listing: yc.ChartListing,
    *,
    interval: str = "1d",
    through: date | None = ALL_DONE,
    window: tuple[int, int] | None = None,
) -> yc.ChartParse:
    return yc.parse_chart(
        payload,
        listing,
        interval=interval,
        captured_at_ns=CAPTURED_NS,
        through_session=through,
        window=window,
    )


def _bars(parse: yc.ChartParse) -> list[OHLCV]:
    return [rec for rec in parse.records if isinstance(rec, OHLCV)]


def _session_of(source_ts: int) -> date:
    return datetime.fromtimestamp(source_ts // 1_000_000_000, tz=UTC).date()


def _chart(payload: dict[str, Any]) -> dict[str, Any]:
    """The single result object of a payload (a deep copy is the caller's to make)."""
    return payload["chart"]["result"][0]


EXPECTED_SESSIONS: dict[str, list[date]] = {
    "THYAO.IS_5d_1d.json": [date(2026, 11, d) for d in range(2, 7)],
    "PTT.BK_5d_1d.json": [date(2026, 11, d) for d in range(2, 7)],
    "NOVO-B.CO_5d_1d.json": [date(2026, 11, d) for d in range(2, 7)],
    "ALV.F_5d_1d.json": [date(2026, 11, d) for d in range(2, 7)],
    "NOKIA.HE_5d_1d.json": [date(2026, 11, d) for d in range(2, 7)],
    "2330.TW_5d_1d.json": [date(2026, 11, d) for d in range(2, 7)],
    "BHP.AX_5d_1d.json": [date(2026, 11, d) for d in range(2, 7)],
    "7203.T_5d_1d.json": [
        date(2026, 10, 30),
        date(2026, 11, 2),
        date(2026, 11, 4),
        date(2026, 11, 5),
        date(2026, 11, 6),
    ],
}


@pytest.mark.parametrize("name", sorted(EXPECTED_SESSIONS))
def test_daily_bars_land_on_the_exchange_local_session_date(name: str) -> None:
    """Recorded 5d payload (real Yahoo answer), so no synthetic data is involved."""
    parse = _parse(_decode(name), _listing(name))
    bars = _bars(parse)
    assert [_session_of(bar.source_ts) for bar in bars] == EXPECTED_SESSIONS[name]
    for bar in bars:
        assert bar.source_ts == daily_bar_ts_ns(_session_of(bar.source_ts))
        assert bar.interval == "1d"


def test_bhp_is_the_case_utc_truncation_gets_wrong() -> None:
    """Recorded BHP.AX 5d payload: first raw stamp is 2026-11-01 UTC, session 2026-11-02."""
    parse = _parse(_decode("BHP.AX_5d_1d.json"), _listing("BHP.AX_5d_1d.json"))
    assert _bars(parse)[0].source_ts == 1_793_577_600_000_000_000


def test_two_rows_for_one_session_keep_the_later_one() -> None:
    """Recorded BHP.AX payload has six stamps and five sessions; the trailing row wins."""
    parse = _parse(_decode("BHP.AX_5d_1d.json"), _listing("BHP.AX_5d_1d.json"))
    bars = _bars(parse)
    assert len(bars) == 5
    last = next(bar for bar in bars if _session_of(bar.source_ts) == date(2026, 11, 6))
    assert last.volume == 10_574_780.0
    assert parse.dropped_duplicate_dates == 1


def test_yahoo_timezone_fields_are_ignored() -> None:
    """Recorded THYAO payload with its timezone fields replaced: identical records result."""
    base = _parse(_decode("THYAO.IS_5d_1d.json"), _listing("THYAO.IS_5d_1d.json"))
    payload = _decode("THYAO.IS_5d_1d.json")
    meta = _chart(payload)["meta"]
    meta["exchangeTimezoneName"] = "America/New_York"
    meta["timezone"] = "EST"
    meta["gmtoffset"] = -18000
    assert _parse(payload, _listing("THYAO.IS_5d_1d.json")).records == base.records


@pytest.mark.parametrize(
    ("name", "currency"),
    [
        ("THYAO.IS_5d_1d.json", "TRY"),
        ("7203.T_5d_1d.json", "JPY"),
        ("2330.TW_5d_1d.json", "TWD"),
        ("BHP.AX_5d_1d.json", "AUD"),
        ("PTT.BK_5d_1d.json", "THB"),
        ("NOVO-B.CO_5d_1d.json", "DKK"),
        ("ALV.F_5d_1d.json", "EUR"),
        ("NOKIA.HE_5d_1d.json", "EUR"),
    ],
)
def test_currency_and_scale_come_from_meta(name: str, currency: str) -> None:
    bars = _bars(_parse(_decode(name), _listing(name)))
    assert bars
    for bar in bars:
        assert bar.currency == currency
        assert bar.price_scale == 0


def test_pence_quoted_listing_is_gbp_scale_2() -> None:
    """Synthetic LSE payload (VOD.L_gbp_synthetic.json, built by hand, not a recording).

    Yahoo quotes .L in pence (meta currency GBp); the record is GBP at scale 2 and the closes
    are the published pence values, unchanged (spec section 6 acceptance 3).
    """
    payload = _decode("VOD.L_gbp_synthetic.json")
    listing = yc.ChartListing(
        symbol="VOD@XLON",
        local="VOD",
        mics=frozenset({"XLON"}),
        yahoo_symbol="VOD.L",
        tz=ZoneInfo("Europe/London"),
        calendar_mic="XLON",
    )
    bars = _bars(_parse(payload, listing))
    assert [bar.currency for bar in bars] == ["GBP", "GBP"]
    assert [bar.price_scale for bar in bars] == [2, 2]
    assert [bar.close for bar in bars] == [72.5, 73.1]


def test_unknown_currency_is_refused() -> None:
    payload = _decode("THYAO.IS_5d_1d.json")
    _chart(payload)["meta"]["currency"] = "XYZ"
    with pytest.raises(UnknownCurrencyError):
        _parse(payload, _listing("THYAO.IS_5d_1d.json"))


def test_missing_currency_is_refused() -> None:
    payload = _decode("THYAO.IS_5d_1d.json")
    del _chart(payload)["meta"]["currency"]
    with pytest.raises(yc.ChartPayloadError):
        _parse(payload, _listing("THYAO.IS_5d_1d.json"))


def test_exchange_name_contradicting_the_mic_rejects_the_response() -> None:
    listing = yc.ChartListing(
        symbol="THYAO@XJPX",
        local="THYAO",
        mics=frozenset({"XJPX"}),
        yahoo_symbol="THYAO.IS",
        tz=ZoneInfo("Europe/Istanbul"),
        calendar_mic="XJPX",
    )
    with pytest.raises(yc.ExchangeNameMismatchError):
        _parse(_decode("THYAO.IS_5d_1d.json"), listing)


def test_an_unlisted_exchange_name_is_written_but_unverified() -> None:
    payload = _decode("THYAO.IS_5d_1d.json")
    _chart(payload)["meta"]["exchangeName"] = "ZZZ"
    parse = _parse(payload, _listing("THYAO.IS_5d_1d.json"))
    assert len(_bars(parse)) == 5
    assert parse.exchange_name_verified is False


def test_granularity_mismatch_is_refused() -> None:
    """A 1mo payload parsed as daily must never become daily bars."""
    listing = yc.ChartListing(
        symbol="2330@XTAI",
        local="2330",
        mics=frozenset({"XTAI"}),
        yahoo_symbol="2330.TW",
        tz=ZoneInfo("Asia/Taipei"),
        calendar_mic="XTAI",
    )
    with pytest.raises(yc.ChartPayloadError):
        _parse(_decode("2330.TW_2y_1mo.json"), listing, interval="1d")


def test_rows_with_a_null_price_are_dropped_and_counted() -> None:
    payload = _decode("THYAO.IS_5d_1d.json")
    _chart(payload)["indicators"]["quote"][0]["close"][2] = None
    parse = _parse(payload, _listing("THYAO.IS_5d_1d.json"))
    assert len(_bars(parse)) == 4
    assert parse.dropped_null_rows == 1


def test_zero_volume_rows_are_kept() -> None:
    payload = _decode("THYAO.IS_5d_1d.json")
    _chart(payload)["indicators"]["quote"][0]["volume"][0] = 0
    bars = _bars(_parse(payload, _listing("THYAO.IS_5d_1d.json")))
    assert len(bars) == 5
    assert bars[0].volume == 0.0


def test_sessions_after_the_latest_completed_one_are_dropped() -> None:
    parse = _parse(
        _decode("THYAO.IS_5d_1d.json"),
        _listing("THYAO.IS_5d_1d.json"),
        through=date(2026, 11, 5),
    )
    assert len(_bars(parse)) == 4
    assert parse.dropped_incomplete == 1


def test_the_window_filters_on_the_session_date() -> None:
    window = (daily_bar_ts_ns(date(2026, 11, 3)), daily_bar_ts_ns(date(2026, 11, 5)))
    parse = _parse(_decode("THYAO.IS_5d_1d.json"), _listing("THYAO.IS_5d_1d.json"), window=window)
    assert len(_bars(parse)) == 3


def test_adjclose_never_reaches_a_record() -> None:
    """Recorded THYAO payload with every adjclose halved: the closes are unchanged."""
    base = _parse(_decode("THYAO.IS_5d_1d.json"), _listing("THYAO.IS_5d_1d.json"))
    payload = _decode("THYAO.IS_5d_1d.json")
    adj = _chart(payload)["indicators"]["adjclose"][0]["adjclose"]
    _chart(payload)["indicators"]["adjclose"][0]["adjclose"] = [v * 0.5 for v in adj]
    parse = _parse(payload, _listing("THYAO.IS_5d_1d.json"))
    assert parse.records == base.records


def test_daily_bars_carry_the_module_adjustment() -> None:
    bars = _bars(_parse(_decode("THYAO.IS_5d_1d.json"), _listing("THYAO.IS_5d_1d.json")))
    assert bars
    for bar in bars:
        assert bar.adjustment is yc.YAHOO_CHART_ADJUSTMENT


def test_records_are_native_and_named_for_the_source() -> None:
    parse = _parse(_decode("THYAO.IS_5d_1d.json"), _listing("THYAO.IS_5d_1d.json"))
    bar = _bars(parse)[0]
    assert bar.prov is Provenance.NATIVE
    assert bar.prov_basis == "native"
    assert bar.prov_confidence == 1.0
    assert bar.source == "yahoo_chart"
    assert bar.symbol == "THYAO@XIST"
    assert bar.symbol_raw == "THYAO.IS"
    assert bar.local_ts == CAPTURED_NS
    assert bar.asset_class is AssetClass.EQUITY


def test_parse_is_deterministic() -> None:
    first = _parse(_decode("THYAO.IS_5d_1d.json"), _listing("THYAO.IS_5d_1d.json"))
    second = _parse(_decode("THYAO.IS_5d_1d.json"), _listing("THYAO.IS_5d_1d.json"))
    assert first.records == second.records


# --- Step 7: corporate actions ----------------------------------------------------------

# Recorded 2y/1mo dividend payloads (yf_1..yf_8): file -> (currency, timezone, expected).
# Expected rows are (ex_date, amount) as the brief fixes them, from the entries' own dates.
DIVIDENDS: dict[str, tuple[str, str, list[tuple[str, float]]]] = {
    "2330.TW_2y_1mo.json": (
        "TWD",
        "Asia/Taipei",
        [
            ("2024-12-12", 3.999637),
            ("2025-03-18", 4.50002),
            ("2025-06-12", 4.50002),
            ("2025-09-16", 5.000018),
            ("2025-12-11", 5.000011),
            ("2026-03-17", 6.000036),
            ("2026-06-11", 6.000036),
            ("2026-09-16", 7.000001),
        ],
    ),
    "7203.T_2y_1mo.json": (
        "JPY",
        "Asia/Tokyo",
        [("2025-03-28", 50.0), ("2025-09-29", 45.0), ("2026-03-30", 50.0), ("2026-09-29", 50.0)],
    ),
    "005930.KS_2y_1mo.json": (
        "KRW",
        "Asia/Seoul",
        [
            ("2024-12-27", 363.0),
            ("2025-03-28", 365.0),
            ("2025-06-27", 367.0),
            ("2025-09-29", 370.0),
            ("2025-12-29", 566.0),
            ("2026-03-30", 372.0),
            ("2026-06-29", 374.0),
            ("2026-09-29", 4601.0),
        ],
    ),
    "RELIANCE.NS_2y_1mo.json": (
        "INR",
        "Asia/Kolkata",
        [("2025-08-14", 5.5), ("2026-06-05", 6.0)],
    ),
    "0700.HK_2y_1mo.json": ("HKD", "Asia/Hong_Kong", [("2025-05-16", 4.5), ("2026-05-15", 5.3)]),
    "600519.SS_2y_1mo.json": (
        "CNY",
        "Asia/Shanghai",
        [
            ("2024-12-20", 23.882),
            ("2025-06-26", 27.673),
            ("2025-12-19", 23.957),
            ("2026-06-26", 28.02423),
        ],
    ),
    "SAP.DE_2y_1mo.json": ("EUR", "Europe/Berlin", [("2025-05-14", 2.35), ("2026-05-06", 2.5)]),
    "BHP.AX_2y_1mo.json": (
        "AUD",
        "Australia/Sydney",
        [
            ("2025-03-06", 0.790935),
            ("2025-09-04", 0.919273),
            ("2026-03-05", 1.038469),
            ("2026-09-03", 1.379887),
        ],
    ),
}


def _us_listing() -> yc.ChartListing:
    return yc.ChartListing(
        symbol="NVDA",
        local="NVDA",
        mics=frozenset({"XNAS", "XNYS"}),
        yahoo_symbol="NVDA",
        tz=ZoneInfo("America/New_York"),
        calendar_mic="XNYS",
    )


@pytest.mark.parametrize("name", sorted(DIVIDENDS))
def test_dividends_become_cash_dividends_on_the_local_ex_date(name: str) -> None:
    """Recorded 1mo/2y payloads: each dividend is dated by its own entry, in the local zone."""
    currency, timezone, expected = DIVIDENDS[name]
    payload = _decode(name)
    meta = _chart(payload)["meta"]
    listing = yc.ChartListing(
        symbol=meta["symbol"],
        local=meta["symbol"],
        mics=frozenset({"XTAI"}),
        yahoo_symbol=meta["symbol"],
        tz=ZoneInfo(timezone),
        calendar_mic="XTAI",
    )
    actions, skipped = yc.parse_corporate_actions(
        payload,
        listing,
        captured_at_ns=CAPTURED_NS,
        quote_unit=parse_quote_unit(currency),
    )
    assert skipped == 0
    assert [(a.ex_date, a.value) for a in actions] == expected
    for action in actions:
        assert action.type is CorpActionType.DIVIDEND_CASH
        assert action.currency == currency
        assert action.price_scale == 0
        assert action.source_ts == daily_bar_ts_ns(date.fromisoformat(action.ex_date))


def test_bhp_dividend_ex_dates_do_not_slip_a_day() -> None:
    """Recorded BHP.AX payload: the 2025-03 and 2026-03 entries are 23:00Z of the day before."""
    payload = _decode("BHP.AX_2y_1mo.json")
    actions, _ = yc.parse_corporate_actions(
        payload,
        _bhp_listing(),
        captured_at_ns=CAPTURED_NS,
        quote_unit=parse_quote_unit("AUD"),
    )
    dates = [a.ex_date for a in actions]
    assert "2025-03-06" in dates
    assert "2026-03-05" in dates
    assert "2025-03-05" not in dates
    assert "2026-03-04" not in dates


def _bhp_listing() -> yc.ChartListing:
    return yc.ChartListing(
        symbol="BHP@XASX",
        local="BHP",
        mics=frozenset({"XASX"}),
        yahoo_symbol="BHP.AX",
        tz=ZoneInfo("Australia/Sydney"),
        calendar_mic="XASX",
    )


def test_a_split_is_a_ratio_without_a_currency() -> None:
    """Synthetic NVDA payload (NVDA_split_synthetic.json, hand-built): a 10-for-1 split."""
    payload = _decode("NVDA_split_synthetic.json")
    actions, skipped = yc.parse_corporate_actions(
        payload, _us_listing(), captured_at_ns=CAPTURED_NS, quote_unit=parse_quote_unit("USD")
    )
    assert skipped == 0
    assert len(actions) == 1
    split = actions[0]
    assert split.type is CorpActionType.SPLIT
    assert split.value == 10.0
    assert split.ex_date == "2024-06-10"
    assert split.currency is None
    assert split.price_scale is None


def test_malformed_events_are_skipped_and_counted() -> None:
    """Synthetic NVDA payload with a dividend lacking ``amount`` and a split with denominator 0."""
    payload = _decode("NVDA_split_synthetic.json")
    events = _chart(payload)["events"]
    events["dividends"] = {"1718026200": {"date": 1718026200}}
    events["splits"]["1718026200"]["denominator"] = 0
    actions, skipped = yc.parse_corporate_actions(
        payload, _us_listing(), captured_at_ns=CAPTURED_NS, quote_unit=parse_quote_unit("USD")
    )
    assert actions == ()
    assert skipped == 2


def test_parse_chart_emits_bars_then_events() -> None:
    """Recorded THYAO 5d bars merged with a synthetic dividend block (THYAO_events_synthetic)."""
    payload = _decode("THYAO.IS_5d_1d.json")
    extra = _decode("THYAO_events_synthetic.json")
    _chart(payload)["events"] = extra["events"]
    parse = _parse(payload, _listing("THYAO.IS_5d_1d.json"))
    kinds = [type(record).__name__ for record in parse.records]
    assert kinds == ["OHLCV"] * 5 + ["CorporateAction"]
    dividend = parse.records[-1]
    assert isinstance(dividend, CorporateAction)
    assert dividend.type is CorpActionType.DIVIDEND_CASH
    assert dividend.value == 3.5
    assert dividend.ex_date == "2026-11-05"
    assert dividend.currency == "TRY"


# --- Step 8: intraday -------------------------------------------------------------------

# Synthetic 5m payload (THYAO_5m_synthetic.json, hand-built): bars at 1793946600, 1793946900 and
# 1793947200, each 300 s long, and one dividend that intraday parsing must not emit.
INTRADAY_CLOSED_NS = 1_793_948_000_000_000_000  # after the last 5m bar has closed
LAST_5M_START = 1_793_947_200


def _intraday_listing() -> yc.ChartListing:
    return _listing("THYAO.IS_5d_1d.json")


def test_intraday_bars_keep_their_own_instant() -> None:
    """Synthetic 5m payload: source_ts is the raw bar start, no adjustment, no corporate actions."""
    parse = yc.parse_chart(
        _decode("THYAO_5m_synthetic.json"),
        _intraday_listing(),
        interval="5m",
        captured_at_ns=INTRADAY_CLOSED_NS,
        through_session=None,
    )
    bars = _bars(parse)
    assert [bar.source_ts for bar in bars] == [
        1_793_946_600 * 1_000_000_000,
        1_793_946_900 * 1_000_000_000,
        LAST_5M_START * 1_000_000_000,
    ]
    for bar in bars:
        assert bar.interval == "5m"
        assert bar.adjustment is None
    assert not [record for record in parse.records if isinstance(record, CorporateAction)]


def test_the_bar_still_open_at_capture_is_dropped() -> None:
    """Synthetic 5m payload captured 2 minutes after the last bar's start: that bar is open."""
    parse = yc.parse_chart(
        _decode("THYAO_5m_synthetic.json"),
        _intraday_listing(),
        interval="5m",
        captured_at_ns=(LAST_5M_START + 120) * 1_000_000_000,
        through_session=None,
    )
    assert parse.dropped_incomplete == 1
    assert len(_bars(parse)) == 2


def test_intraday_windows_match_yahoo() -> None:
    assert dict(yc.INTRADAY_WINDOWS) == {
        "1m": timedelta(days=7),
        "5m": timedelta(days=60),
        "15m": timedelta(days=60),
        "30m": timedelta(days=60),
        "1h": timedelta(days=730),
    }
    captured = 1_794_000_000_000_000_000
    assert yc.intraday_window("1m", captured)[0] == captured - 7 * 86_400 * 1_000_000_000
    assert yc.intraday_window("1m", captured)[1] == captured


def test_1h_is_requested_as_60m() -> None:
    """Yahoo's 60m granularity is parsed back as the canonical 1h interval."""
    assert yc.YAHOO_INTERVAL["1h"] == "60m"
    payload = _decode("THYAO_5m_synthetic.json")
    _chart(payload)["meta"]["dataGranularity"] = "60m"
    parse = yc.parse_chart(
        payload,
        _intraday_listing(),
        interval="1h",
        # A 1h bar is open for an hour: capture after the last bar's hour has passed.
        captured_at_ns=(LAST_5M_START + 3_600 + 60) * 1_000_000_000,
        through_session=None,
    )
    assert _bars(parse)
    assert {bar.interval for bar in _bars(parse)} == {"1h"}


# --- Step 9: client (offline: an injected fetcher and a recording bucket) ---------------

NS = 1_000_000_000


class _Fake:
    """An injected fetcher: records each request and answers with the prepared responses."""

    def __init__(self, *responses: HttpResponse) -> None:
        self.requests: list[HttpRequest] = []
        self._responses = list(responses)

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self._responses.pop(0)


def _response(
    status: int,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    content_type: str = "application/json",
) -> HttpResponse:
    all_headers = {"content-type": content_type, **(headers or {})}
    url = "https://query2.finance.yahoo.com/v8/finance/chart/THYAO.IS"
    return HttpResponse(status=status, headers=all_headers, body=body, url=url)


def _clock_bucket(now: list[float]) -> TokenBucket:
    return TokenBucket(capacity=100, refill_rate=100, time_func=lambda: now[0])


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_the_request_names_the_v8_chart_endpoint_and_its_parameters(
    network_forbidden: None,
) -> None:
    fake = _Fake(_response(404))
    client = yc.YahooChartClient(fetch=fake, bucket=TokenBucket(capacity=100, refill_rate=100))
    asyncio.run(client.fetch_chart("THYAO.IS", interval="1d", period1=1, period2=2))
    (request,) = fake.requests
    assert request.method == "GET"
    assert request.source == "yahoo_chart"
    assert request.url == "https://query2.finance.yahoo.com/v8/finance/chart/THYAO.IS"
    assert request.params == (
        ("period1", "1"),
        ("period2", "2"),
        ("interval", "1d"),
        ("events", yc.EVENTS_PARAM),
        ("includeAdjustedClose", "true"),
    )


def test_full_history_is_one_request(network_forbidden: None) -> None:
    captured = CAPTURED_NS
    start, end = yc.full_history_window(captured)
    fake = _Fake(_response(404))
    client = yc.YahooChartClient(fetch=fake, bucket=TokenBucket(capacity=100, refill_rate=100))
    asyncio.run(
        client.fetch_listing(
            _listing("THYAO.IS_5d_1d.json"),
            interval="1d",
            start_ns=start,
            end_ns=end,
            captured_at_ns=captured,
            through_session=ALL_DONE,
        )
    )
    assert len(fake.requests) == 1
    params = dict(fake.requests[0].params)
    assert params["period1"] == str(captured // NS - 99 * 365 * 86_400 - 86_400)
    assert params["period2"] == str(captured // NS + 86_400)


def test_404_is_not_found_not_an_error(network_forbidden: None) -> None:
    fake = _Fake(_response(404))
    client = yc.YahooChartClient(fetch=fake, bucket=TokenBucket(capacity=100, refill_rate=100))
    result = asyncio.run(
        client.fetch_listing(
            _listing("THYAO.IS_5d_1d.json"),
            interval="1d",
            start_ns=0,
            end_ns=CAPTURED_NS,
            captured_at_ns=CAPTURED_NS,
            through_session=ALL_DONE,
        )
    )
    assert result.status is yc.ChartStatus.NOT_FOUND
    assert result.http_status == 404
    assert result.parse is None


def test_a_not_found_error_body_is_not_found(network_forbidden: None) -> None:
    """A 200 whose body reads ``result: null`` with a Not Found error is NOT_FOUND."""
    fake = _Fake(_response(200, _fixture_bytes("not_found_404.json")))
    client = yc.YahooChartClient(fetch=fake, bucket=TokenBucket(capacity=100, refill_rate=100))
    result = asyncio.run(
        client.fetch_listing(
            _listing("THYAO.IS_5d_1d.json"),
            interval="1d",
            start_ns=0,
            end_ns=CAPTURED_NS,
            captured_at_ns=CAPTURED_NS,
            through_session=ALL_DONE,
        )
    )
    assert result.status is yc.ChartStatus.NOT_FOUND


def test_an_empty_result_is_not_found(network_forbidden: None) -> None:
    body = b'{"chart": {"result": [], "error": null}}'
    fake = _Fake(_response(200, body))
    client = yc.YahooChartClient(fetch=fake, bucket=TokenBucket(capacity=100, refill_rate=100))
    result = asyncio.run(
        client.fetch_listing(
            _listing("THYAO.IS_5d_1d.json"),
            interval="1d",
            start_ns=0,
            end_ns=CAPTURED_NS,
            captured_at_ns=CAPTURED_NS,
            through_session=ALL_DONE,
        )
    )
    assert result.status is yc.ChartStatus.NOT_FOUND


def test_429_backs_off_the_shared_budget_and_raises_transient(network_forbidden: None) -> None:
    now = [1_000.0]
    bucket = _clock_bucket(now)
    fake = _Fake(_response(429, headers={"retry-after": "7"}))
    client = yc.YahooChartClient(fetch=fake, bucket=bucket)
    with pytest.raises(TransientProviderError):
        asyncio.run(client.fetch_chart("THYAO.IS", interval="1d", period1=1, period2=2))
    assert bucket.backoff_remaining == pytest.approx(7.0)


def test_429_without_retry_after_uses_the_default_backoff(network_forbidden: None) -> None:
    now = [1_000.0]
    bucket = _clock_bucket(now)
    fake = _Fake(_response(429))
    client = yc.YahooChartClient(fetch=fake, bucket=bucket)
    with pytest.raises(TransientProviderError):
        asyncio.run(client.fetch_chart("THYAO.IS", interval="1d", period1=1, period2=2))
    assert bucket.backoff_remaining == pytest.approx(yc.DEFAULT_BACKOFF_S)


def test_5xx_is_transient_without_backoff(network_forbidden: None) -> None:
    now = [1_000.0]
    bucket = _clock_bucket(now)
    fake = _Fake(_response(503))
    client = yc.YahooChartClient(fetch=fake, bucket=bucket)
    with pytest.raises(TransientProviderError):
        asyncio.run(client.fetch_chart("THYAO.IS", interval="1d", period1=1, period2=2))
    assert bucket.backoff_remaining == 0.0


def test_400_is_a_request_error_with_yahoos_description(network_forbidden: None) -> None:
    body = (
        b'{"chart": {"result": null, "error": {"code": "Bad Request", '
        b'"description": "Invalid input"}}}'
    )
    fake = _Fake(_response(400, body))
    client = yc.YahooChartClient(fetch=fake, bucket=TokenBucket(capacity=100, refill_rate=100))
    with pytest.raises(yc.ChartRequestError, match="Invalid input"):
        asyncio.run(client.fetch_chart("THYAO.IS", interval="1d", period1=1, period2=2))


def test_html_is_a_bot_challenge(network_forbidden: None) -> None:
    """Synthetic consent page (consent_wall.html) answered with 200 text/html."""
    body = _fixture_bytes("consent_wall.html")
    fake = _Fake(_response(200, body, content_type="text/html"))
    client = yc.YahooChartClient(fetch=fake, bucket=TokenBucket(capacity=100, refill_rate=100))
    with pytest.raises(SourceUnservableError) as caught:
        asyncio.run(client.fetch_chart("THYAO.IS", interval="1d", period1=1, period2=2))
    assert caught.value.reason is UnservableReason.BOT_CHALLENGE
    assert caught.value.source == "yahoo_chart"


def test_a_redirect_is_a_bot_challenge(network_forbidden: None) -> None:
    fake = _Fake(_response(302, headers={"location": "https://consent.example/"}))
    client = yc.YahooChartClient(fetch=fake, bucket=TokenBucket(capacity=100, refill_rate=100))
    with pytest.raises(SourceUnservableError) as caught:
        asyncio.run(client.fetch_chart("THYAO.IS", interval="1d", period1=1, period2=2))
    assert caught.value.reason is UnservableReason.BOT_CHALLENGE


def test_403_is_forbidden(network_forbidden: None) -> None:
    fake = _Fake(_response(403))
    client = yc.YahooChartClient(fetch=fake, bucket=TokenBucket(capacity=100, refill_rate=100))
    with pytest.raises(SourceUnservableError) as caught:
        asyncio.run(client.fetch_chart("THYAO.IS", interval="1d", period1=1, period2=2))
    assert caught.value.reason is UnservableReason.FORBIDDEN
    assert caught.value.source == "yahoo_chart"


def test_every_request_spends_one_token(network_forbidden: None) -> None:
    bucket = TokenBucket(capacity=100, refill_rate=100)
    spent: list[float] = []
    real_acquire = bucket.acquire

    async def counting(tokens: float = 1.0) -> None:
        spent.append(tokens)
        await real_acquire(tokens)

    bucket.acquire = counting  # type: ignore[method-assign]
    fake = _Fake(_response(404), _response(404))
    client = yc.YahooChartClient(fetch=fake, bucket=bucket)

    async def two() -> None:
        await client.fetch_chart("THYAO.IS", interval="1d", period1=1, period2=2)
        await client.fetch_chart("THYAO.IS", interval="1d", period1=1, period2=2)

    asyncio.run(two())
    assert spent == [1.0, 1.0]


def test_the_default_bucket_comes_from_the_shared_budget(
    monkeypatch: pytest.MonkeyPatch, network_forbidden: None
) -> None:
    """With no bucket injected the client asks the shared budget, lazily, inside the request."""
    seen: list[tuple[str, str]] = []
    shared = TokenBucket(capacity=100, refill_rate=100)

    def fake_budget(source: str, host: str, **_: object) -> TokenBucket:
        seen.append((source, host))
        return shared

    monkeypatch.setattr(yc, "budget", fake_budget)
    fake = _Fake(_response(404))
    client = yc.YahooChartClient(fetch=fake)
    assert seen == []
    asyncio.run(client.fetch_chart("THYAO.IS", interval="1d", period1=1, period2=2))
    assert seen == [("yahoo_chart", "query2.finance.yahoo.com")]


# --- Step 10: resolution, provider, registration ----------------------------------------


async def _collect(agen: Any) -> list[Any]:
    return [record async for record in agen]


def _provider(
    *responses: HttpResponse,
    symbols: list[str] | None = None,
    master: ListingMaster | None = None,
    settings: Settings | None = None,
    interval: str = "1d",
) -> tuple[yc.YahooChartProvider, _Fake]:
    fake = _Fake(*responses)
    client = yc.YahooChartClient(fetch=fake, bucket=TokenBucket(capacity=100, refill_rate=100))
    provider = yc.YahooChartProvider(
        symbols or [],
        ["ohlcv", "corp_action"],
        MemorySink(),
        InstrumentRegistry(),
        client=client,
        master=master,
        settings=Settings() if settings is None else settings,
        interval=interval,
        clock=lambda: CAPTURED_NS,
    )
    return provider, fake


def _master_with(*observations: ListingObservation) -> ListingMaster:
    master = ListingMaster(":memory:")
    for observation in observations:
        master.upsert(observation)
    return master


def test_a_canonical_symbol_resolves_through_the_grammar() -> None:
    listing = yc.resolve_listing("THYAO@XIST")
    assert listing.yahoo_symbol == "THYAO.IS"
    assert listing.tz == ZoneInfo("Europe/Istanbul")
    assert listing.calendar_mic == "XIST"
    assert listing.mics == frozenset({"XIST"})


def test_a_raw_yahoo_symbol_resolves_back_to_its_listing() -> None:
    assert yc.resolve_listing("yahoo_chart:7203.T").symbol == "7203@XJPX"


def test_a_raw_unsuffixed_yahoo_symbol_is_a_us_listing() -> None:
    listing = yc.resolve_listing("yahoo_chart:AAPL")
    assert listing.symbol == "AAPL"
    assert listing.mics == frozenset({"XNAS", "XNYS"})


def test_a_bare_symbol_is_a_us_listing() -> None:
    listing = yc.resolve_listing("BRK.B")
    assert listing.symbol == "BRK.B"
    assert listing.yahoo_symbol == "BRK-B"
    assert listing.mics == frozenset({"XNAS", "XNYS"})
    assert listing.tz == ZoneInfo("America/New_York")
    assert listing.calendar_mic == "XNYS"


def test_a_kosdaq_listing_the_master_knows_uses_its_segment_suffix() -> None:
    master = _master_with(
        ListingObservation(
            source="krx_test",
            mic="XKRX",
            segment_mic="XKOS",
            local_symbol="247540",
            symbol_raw="247540",
            observed_ns=1,
        )
    )
    assert yc.resolve_listing("247540@XKRX", master=master).yahoo_symbol == "247540.KQ"
    assert yc.resolve_listing("247540@XKRX").yahoo_symbol == "247540.KS"


def test_a_sanctioned_market_is_refused_before_any_request(network_forbidden: None) -> None:
    with pytest.raises(SanctionedMarketError):
        yc.resolve_listing("SBER@MISX")
    assert yc.resolve_listing("SBER@MISX", allow={"RU"}).yahoo_symbol == "SBER.ME"
    provider, fake = _provider(symbols=["SBER@MISX"])
    with pytest.raises(SanctionedMarketError):
        asyncio.run(_collect(provider.backfill("ohlcv", "SBER@MISX", 0, CAPTURED_NS)))
    assert fake.requests == []


def test_the_provider_takes_the_sanctions_opt_in_from_settings() -> None:
    opened = yc.YahooChartProvider(
        [],
        ["ohlcv"],
        MemorySink(),
        InstrumentRegistry(),
        settings=Settings(allow_sanctioned_markets="RU"),
    )
    assert opened._resolve("SBER@MISX").yahoo_symbol == "SBER.ME"
    closed = yc.YahooChartProvider(
        [], ["ohlcv"], MemorySink(), InstrumentRegistry(), settings=Settings()
    )
    with pytest.raises(SanctionedMarketError):
        closed._resolve("SBER@MISX")


def test_a_market_without_grammar_is_refused() -> None:
    with pytest.raises(NoGrammarForMarket):
        yc.resolve_listing("CERA.N0000@XCOL")


def test_the_provider_is_registered_with_its_declared_channels(network_forbidden: None) -> None:
    client = yc.YahooChartClient(fetch=_Fake(), bucket=TokenBucket(capacity=100, refill_rate=100))
    provider = factory.make_provider(
        "yahoo_chart",
        symbols=["THYAO@XIST"],
        channels=["ohlcv"],
        out=MemorySink(),
        registry=InstrumentRegistry(),
        client=client,
    )
    assert isinstance(provider, yc.YahooChartProvider)
    assert factory.supported_channels("yahoo_chart") == frozenset({"ohlcv", "corp_action"})
    assert contract_violations(yc.YahooChartProvider) == []
    assert yc.YahooChartProvider.usage_class is UsageClass.INTERNAL


def test_supported_mics_are_exactly_the_yahoo_grammar_mics() -> None:
    assert yc.YahooChartProvider.supported_mics == yc.YahooChartProvider.grammar().supported_mics()


def test_backfill_yields_bars_and_events_from_one_request(network_forbidden: None) -> None:
    """Recorded THYAO 5d payload plus a synthetic dividend block: one request, both channels."""
    payload = _decode("THYAO.IS_5d_1d.json")
    payload["chart"]["result"][0]["events"] = _decode("THYAO_events_synthetic.json")["events"]
    provider, fake = _provider(
        _response(200, msgspec.json.encode(payload)), symbols=["THYAO@XIST"]
    )
    start = daily_bar_ts_ns(date(2026, 11, 1))
    end = daily_bar_ts_ns(date(2026, 11, 7))
    records = asyncio.run(_collect(provider.backfill("corp_action", "THYAO@XIST", start, end)))
    assert len(fake.requests) == 1
    assert {type(record).__name__ for record in records} == {"OHLCV", "CorporateAction"}
    assert all(record.local_ts == CAPTURED_NS for record in records)


def test_backfill_refuses_a_channel_it_does_not_serve() -> None:
    provider, _ = _provider(symbols=["THYAO@XIST"])
    with pytest.raises(ValueError, match="yahoo_chart serves"):
        asyncio.run(_collect(provider.backfill("instrument", "THYAO@XIST", 0, CAPTURED_NS)))


def test_intraday_backfill_refuses_history_outside_the_window(network_forbidden: None) -> None:
    provider, fake = _provider(symbols=["THYAO@XIST"], interval="5m")
    start = CAPTURED_NS - 61 * 86_400 * 1_000_000_000
    with pytest.raises(ValueError, match="window"):
        asyncio.run(_collect(provider.backfill("ohlcv", "THYAO@XIST", start, CAPTURED_NS)))
    assert fake.requests == []


def test_a_currency_mismatch_with_the_listing_master_goes_to_the_dead_letter_queue(
    network_forbidden: None,
) -> None:
    master = _master_with(
        ListingObservation(
            source="test",
            mic="XIST",
            local_symbol="THYAO",
            symbol_raw="THYAO",
            observed_ns=1,
            currency="EUR",
            price_scale=0,
        )
    )
    provider, _ = _provider(
        _response(200, msgspec.json.encode(_decode("THYAO.IS_5d_1d.json"))),
        symbols=["THYAO@XIST"],
        master=master,
    )
    start = daily_bar_ts_ns(date(2026, 11, 1))
    end = daily_bar_ts_ns(date(2026, 11, 7))
    records = asyncio.run(_collect(provider.backfill("ohlcv", "THYAO@XIST", start, end)))
    assert records == []
    letters = provider._dlq.drain()
    assert len(letters) == 5
    assert {letter.error_type for letter in letters} == {"CurrencyMismatchError"}


def test_not_found_yields_nothing(network_forbidden: None) -> None:
    provider, _ = _provider(_response(404), symbols=["THYAO@XIST"])
    start = daily_bar_ts_ns(date(2026, 11, 1))
    end = daily_bar_ts_ns(date(2026, 11, 7))
    assert asyncio.run(_collect(provider.backfill("ohlcv", "THYAO@XIST", start, end))) == []


def test_close_closes_the_client() -> None:
    client = Mock()
    client.close = AsyncMock()
    provider = yc.YahooChartProvider(
        [], ["ohlcv"], MemorySink(), InstrumentRegistry(), client=client, settings=Settings()
    )
    asyncio.run(provider.close())
    client.close.assert_awaited_once()
