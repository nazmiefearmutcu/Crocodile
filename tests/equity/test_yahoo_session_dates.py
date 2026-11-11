"""Legacy Yahoo daily session dates, ex-dates and fundamentals units (spec §3.12).

yfinance stamps daily rows at exchange-local midnight. The legacy client must date each bar,
dividend and split by that exchange-local session, and must file fundamentals in the issuer's
``financialCurrency``. Offline only: the payloads are the recorded chart responses under
``fixtures/yahoo_chart``.
"""

import json
import pathlib
import threading
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pandas as pd
import pytest
import requests

from crocodile.core.schema.enums import CorpActionType
from crocodile.core.schema.records import OHLCV, CorporateAction, Fundamental
from crocodile.equity.providers.yahoo import client as yahoo

pytestmark = pytest.mark.usefixtures("network_forbidden")

_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "yahoo_chart"
_NS_PER_DAY = 86_400_000_000_000


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Yahoo session-date test attempted network I/O")

    monkeypatch.setattr(requests.Session, "request", forbidden)
    try:
        from curl_cffi.requests import Session
    except ImportError:
        pass
    else:
        monkeypatch.setattr(Session, "request", forbidden)


def _client():
    limiter = SimpleNamespace(acquire=AsyncMock(), update_backoff=Mock())
    return yahoo.YahooClient(rate_limiter=limiter, backoff_delay=0.001)


def _chart(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))["chart"]["result"][0]


def _yfinance_daily_frame(name: str) -> pd.DataFrame:
    """What yfinance 1.5.2 `Ticker.history(interval="1d", actions=True)` builds from this payload:
    utils.parse_quotes (UTC stamps), utils.set_df_tz (utils.py:614-618, convert to
    meta.exchangeTimezoneName), history.py:421 (re-localise each row's local date at exchange
    midnight), utils.fix_Yahoo_returning_live_separate for 1d (utils.py:689-695: two rows on one
    local date keep the last) and the zero Dividends / Stock Splits columns actions=True adds."""
    result = _chart(name)
    tz = result["meta"]["exchangeTimezoneName"]
    quote = result["indicators"]["quote"][0]
    index = pd.to_datetime(result["timestamp"], unit="s", utc=True).tz_convert(tz)
    frame = pd.DataFrame(
        {
            "Open": quote["open"],
            "High": quote["high"],
            "Low": quote["low"],
            "Close": quote["close"],
            "Volume": quote["volume"],
        },
        index=index,
    )
    frame.index = pd.to_datetime(frame.index.date).tz_localize(
        tz, ambiguous=True, nonexistent="shift_forward"
    )
    frame = frame[~frame.index.duplicated(keep="last")]
    frame["Dividends"] = 0.0
    frame["Stock Splits"] = 0.0
    return frame


def _one_row(day: str, tz: str, **actions: float) -> pd.DataFrame:
    index = pd.DatetimeIndex([day]).tz_localize(tz)
    row = {
        "Open": 1.0,
        "High": 1.0,
        "Low": 1.0,
        "Close": 1.0,
        "Volume": 1.0,
        "Dividends": 0.0,
        "Stock Splits": 0.0,
        **actions,
    }
    return pd.DataFrame({k: [v] for k, v in row.items()}, index=index)


def _bars(records: list[Any]) -> list[OHLCV]:
    return [r for r in records if isinstance(r, OHLCV)]


def _actions(records: list[Any]) -> list[CorporateAction]:
    return [r for r in records if isinstance(r, CorporateAction)]


def _utc_midnight_ns(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp()) * 1_000_000_000


# --- daily bars -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "symbol", "expected_dates", "first_source_ts", "first_close"),
    [
        (
            "thyao_is.json",
            "THYAO.IS",
            ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"],
            1_793_577_600_000_000_000,
            261.0,
        ),
        (
            "7203_t.json",
            "7203.T",
            ["2026-10-30", "2026-11-02", "2026-11-04", "2026-11-05", "2026-11-06"],
            1_793_318_400_000_000_000,
            3070.0,
        ),
        (
            "2330_tw.json",
            "2330.TW",
            ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"],
            1_793_577_600_000_000_000,
            2330.0,
        ),
        (
            "bhp_ax.json",
            "BHP.AX",
            ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"],
            1_793_577_600_000_000_000,
            56.7400016784668,
        ),
    ],
)
def test_daily_bars_land_on_the_exchange_local_session_date(
    fixture, symbol, expected_dates, first_source_ts, first_close
):
    frame = _yfinance_daily_frame(fixture)
    bars = _bars(yahoo._daily_records(frame, symbol, 7))

    assert [datetime.fromtimestamp(b.source_ts // 10**9, tz=UTC).date().isoformat()
            for b in bars] == expected_dates
    assert all(b.source_ts % _NS_PER_DAY == 0 for b in bars)
    assert bars[0].source_ts == first_source_ts
    assert bars[0].close == first_close
    assert all(b.local_ts == 7 for b in bars)


def test_a_new_york_daily_bar_is_stamped_at_utc_midnight_not_at_local_midnight():
    # AAPL case of spec §3.12. Synthetic: no AAPL payload was staged for this test; the DST
    # start (2026-03-08) and end (2026-11-01) fall inside the index.
    index = pd.DatetimeIndex(["2026-03-06", "2026-03-09", "2026-11-02"]).tz_localize(
        "America/New_York"
    )
    frame = pd.DataFrame(
        {
            "Open": [1.0, 1.0, 1.0],
            "High": [1.0, 1.0, 1.0],
            "Low": [1.0, 1.0, 1.0],
            "Close": [1.0, 1.0, 1.0],
            "Volume": [1.0, 1.0, 1.0],
            "Dividends": [0.0, 0.0, 0.0],
            "Stock Splits": [0.0, 0.0, 0.0],
        },
        index=index,
    )
    bars = _bars(yahoo._daily_records(frame, "AAPL", 1))
    assert [b.source_ts for b in bars] == [
        1_772_755_200_000_000_000,
        1_773_014_400_000_000_000,
        1_793_577_600_000_000_000,
    ]


# --- dividends and splits -------------------------------------------------------------------


def _first_dividend(name: str) -> tuple[date, float]:
    events = _chart(name)["events"]["dividends"]
    first = min(events.values(), key=lambda e: e["date"])
    tz = _chart(name)["meta"]["exchangeTimezoneName"]
    day = pd.Timestamp(first["date"], unit="s", tz="UTC").tz_convert(tz).date()
    return day, first["amount"]


@pytest.mark.parametrize(
    ("fixture", "tz", "ex_date", "amount", "source_ts"),
    [
        ("2330_tw_1mo_2y_events.json", "Asia/Taipei", "2024-12-12", 3.999637, 1_733_961_600_000_000_000),
        ("7203_t_1mo_2y_events.json", "Asia/Tokyo", "2025-03-28", 50.0, 1_743_120_000_000_000_000),
        ("bhp_ax_1mo_2y_events.json", "Australia/Sydney", "2025-03-06", 0.790935, 1_741_219_200_000_000_000),
    ],
)
def test_a_dividend_ex_date_comes_from_the_same_local_date(
    fixture, tz, ex_date, amount, source_ts
):
    derived_day, derived_amount = _first_dividend(fixture)
    assert derived_day.isoformat() == ex_date
    assert derived_amount == pytest.approx(amount)

    frame = _one_row(derived_day.isoformat(), tz, Dividends=derived_amount)
    dividends = [a for a in _actions(yahoo._daily_records(frame, "X", 1))
                 if a.type == CorpActionType.DIVIDEND_CASH]
    assert len(dividends) == 1
    assert dividends[0].ex_date == ex_date
    assert dividends[0].value == pytest.approx(amount)
    assert dividends[0].source_ts == source_ts


def test_a_dividend_on_an_istanbul_session_keeps_its_local_date():
    frame = _one_row("2026-11-04", "Europe/Istanbul", Dividends=3.5)
    dividends = [a for a in _actions(yahoo._daily_records(frame, "THYAO.IS", 1))
                 if a.type == CorpActionType.DIVIDEND_CASH]
    assert len(dividends) == 1
    assert dividends[0].ex_date == "2026-11-04"
    assert dividends[0].source_ts == 1_793_750_400_000_000_000


def test_a_split_ex_date_comes_from_the_same_local_date():
    frame = _one_row("2021-09-29", "Asia/Tokyo", **{"Stock Splits": 5.0})
    splits = [a for a in _actions(yahoo._daily_records(frame, "7203.T", 1))
              if a.type == CorpActionType.SPLIT]
    assert len(splits) == 1
    assert splits[0].ex_date == "2021-09-29"
    assert splits[0].source_ts == 1_632_873_600_000_000_000


def test_a_naive_daily_index_keeps_its_own_date():
    index = pd.to_datetime(["2026-06-15"])
    frame = pd.DataFrame(
        {
            "Open": [1.0],
            "High": [1.0],
            "Low": [1.0],
            "Close": [1.0],
            "Volume": [1.0],
            "Dividends": [0.0],
            "Stock Splits": [0.0],
        },
        index=index,
    )
    records = yahoo._daily_records(frame, "NAIVE", 1)
    bars = _bars(records)
    assert len(bars) == 1
    assert bars[0].source_ts == _utc_midnight_ns("2026-06-15")
    assert datetime.fromtimestamp(bars[0].source_ts // 10**9, tz=UTC).date().isoformat() == (
        "2026-06-15"
    )


async def test_eod_history_converts_off_the_event_loop_with_one_capture_time(monkeypatch):
    client = _client()
    frame = _yfinance_daily_frame("thyao_is.json")
    monkeypatch.setattr(client, "_execute_yf_call", AsyncMock(return_value=frame))
    monkeypatch.setattr(yahoo.time, "time_ns", lambda: 42)
    threads: list[threading.Thread] = []
    original = yahoo._daily_records

    def conversion(*args):
        threads.append(threading.current_thread())
        return original(*args)

    monkeypatch.setattr(yahoo, "_daily_records", conversion)
    try:
        records = await client.fetch_eod_history("THYAO.IS")
        assert len(threads) == 1
        assert threads[0] is not threading.current_thread()
        assert records
        assert all(r.local_ts == 42 for r in records)
    finally:
        await client.close()
