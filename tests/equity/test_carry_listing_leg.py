"""``price_leg`` for listing symbols reads ranked session closes; US bare tickers read as before.

The listing branch selects one close per session through ``resolve_daily_bars`` and stamps it at
the session's close instant (the calendar's, or the end of the UTC day when the calendar cannot
place it). A US bare ticker keeps the capture-time behaviour and never reaches the federation.
Offline (``network_forbidden``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from crocodile.core.markets.calendar import calendar_for
from crocodile.core.markets.errors import CalendarRangeError, NotASessionError
from crocodile.core.schema.enums import AssetClass, Side
from crocodile.core.schema.records import OHLCV, Record, Trade
from crocodile.core.store.catalog import Catalog
from crocodile.core.store.parquet_sink import ParquetSink
from crocodile.equity.analytics.carry import price_leg
from crocodile.equity.universal.order import NS_PER_DAY, OrderRow, SourceOrder

pytestmark = pytest.mark.usefixtures("network_forbidden")

_SYMBOL = "THYAO@XIST"
_CAPTURE = 1_720_000_000_000_000_000  # a fixed capture instant, outside the session dates
_ORDER = SourceOrder([OrderRow("XIST", "ohlcv", ("yahoo_chart", "tradingview"))])
D4, D5, D6, D7 = date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6), date(2024, 3, 7)
_DAYS = (D4, D5, D6, D7)


def _epoch_ns(moment: datetime) -> int:
    delta = moment.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def _midnight_ns(day: date) -> int:
    return (day - date(1970, 1, 1)).days * NS_PER_DAY


def _at(day: date, hour: int = 0, minute: int = 0) -> int:
    return _epoch_ns(datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC))


def _bar(
    source: str,
    symbol: str,
    day: date,
    close: float,
    *,
    local_ts: int = _CAPTURE,
    currency: str = "TRY",
) -> OHLCV:
    return OHLCV(
        source=source,
        symbol=symbol,
        symbol_raw=symbol,
        local_ts=local_ts,
        asset_class=AssetClass.EQUITY,
        source_ts=_midnight_ns(day),
        interval="1d",
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1.0,
        currency=currency,
        price_scale=0,
    )


def _lake(root: Path, records: list[Record]) -> Catalog:
    async def _write() -> None:
        sink = ParquetSink(root, max_buffer_rows=10_000, flush_interval_seconds=9999)
        for record in records:
            await sink.put(record)
        await sink.flush()
        await sink.close()

    asyncio.run(_write())
    return Catalog(root)


def _listing_lake(root: Path) -> Catalog:
    """THYAO@XIST: yahoo_chart and tradingview both hold the four sessions, at different closes."""
    records: list[Record] = []
    for index, day in enumerate(_DAYS):
        records.append(_bar("yahoo_chart", _SYMBOL, day, 10.0 + index))
        records.append(_bar("tradingview", _SYMBOL, day, 20.0 + index))
    return _lake(root, records)


@pytest.fixture
def ranked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("crocodile.equity.universal.order.source_order", lambda: _ORDER)


def test_a_listing_price_leg_has_one_close_per_session_from_the_ranked_source(
    tmp_path: Path, ranked: None
) -> None:
    catalog = _listing_lake(tmp_path)
    leg = price_leg(catalog, _SYMBOL, _at(D4), _at(D7, 23, 59))
    assert leg["price"].to_list() == [10.0, 11.0, 12.0, 13.0]
    assert leg.columns == ["local_ts", "price"]


def test_a_listing_price_leg_is_stamped_at_each_sessions_close(
    tmp_path: Path, ranked: None
) -> None:
    catalog = _listing_lake(tmp_path)
    leg = price_leg(catalog, _SYMBOL, _at(D4), _at(D7, 23, 59))
    calendar = calendar_for("XIST")
    expected = [_epoch_ns(calendar.session_close_utc(day)) for day in _DAYS]
    assert leg["local_ts"].to_list() == expected
    assert _CAPTURE not in leg["local_ts"].to_list()


class _Uncalendared:
    """A calendar that cannot place any close, whatever the reason."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def session_close_utc(self, day: date) -> datetime:
        raise self._error


def test_a_listing_price_leg_falls_back_to_the_end_of_the_utc_day_out_of_range(
    tmp_path: Path, ranked: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "crocodile.core.markets.calendar.calendar_for",
        lambda mic: _Uncalendared(CalendarRangeError(mic, D4, None, None)),
    )
    catalog = _listing_lake(tmp_path)
    leg = price_leg(catalog, _SYMBOL, _at(D4), _at(D7, 23, 59))
    expected = [_midnight_ns(day + timedelta(days=1)) - 1 for day in _DAYS]
    assert leg["local_ts"].to_list() == expected


def test_a_listing_price_leg_falls_back_to_the_end_of_the_utc_day_for_a_non_session(
    tmp_path: Path, ranked: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "crocodile.core.markets.calendar.calendar_for",
        lambda mic: _Uncalendared(NotASessionError(mic, D4)),
    )
    catalog = _listing_lake(tmp_path)
    leg = price_leg(catalog, _SYMBOL, _at(D4), _at(D7, 23, 59))
    expected = [_midnight_ns(day + timedelta(days=1)) - 1 for day in _DAYS]
    assert leg["local_ts"].to_list() == expected


def test_a_us_bare_ticker_price_leg_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("a US bare ticker must not reach resolve_daily_bars")

    monkeypatch.setattr("crocodile.equity.universal.order.resolve_daily_bars", _refuse)
    first_capture = _CAPTURE
    second_capture = _CAPTURE + 1_000
    catalog = _lake(
        tmp_path,
        [
            _bar("stooq", "AAPL", D5, 100.0, local_ts=first_capture, currency="USD"),
            _bar("yahoo", "AAPL", D5, 101.0, local_ts=second_capture, currency="USD"),
        ],
    )
    leg = price_leg(catalog, "AAPL", _at(D4), second_capture + 10)
    assert leg["local_ts"].to_list() == [first_capture, second_capture]
    assert leg["price"].to_list() == [100.0, 101.0]


def test_a_listing_with_trades_still_answers_from_trades(
    tmp_path: Path, ranked: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("trades win; the daily-bar federation must not be consulted")

    monkeypatch.setattr("crocodile.equity.universal.order.resolve_daily_bars", _refuse)
    trade = Trade(
        source="yahoo",
        symbol=_SYMBOL,
        symbol_raw=_SYMBOL,
        local_ts=_CAPTURE,
        asset_class=AssetClass.EQUITY,
        source_ts=_CAPTURE,
        id="t1",
        price=42.0,
        amount=1.0,
        side=Side.UNKNOWN,
    )
    catalog = _lake(tmp_path, [trade, _bar("yahoo_chart", _SYMBOL, D5, 10.0)])
    leg = price_leg(catalog, _SYMBOL, _at(D4), _CAPTURE + 10)
    assert leg["price"].to_list() == [42.0]
