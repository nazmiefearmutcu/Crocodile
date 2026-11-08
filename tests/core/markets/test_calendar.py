"""Behaviour of the ExchangeCalendar protocol and its four implementations."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from crocodile.core.markets import calendar as calendar_module
from crocodile.core.markets.calendar import (
    AlwaysOpenCalendar,
    CalendarQuality,
    ExchangeCalendar,
    InHouseCalendar,
    LibraryCalendar,
    WeekdayFallbackCalendar,
    calendar_for,
)
from crocodile.core.markets.errors import (
    CalendarRangeError,
    NotASessionError,
    UnknownMarketError,
)
from crocodile.core.markets.exchanges import ExchangeInfo, ExchangeRegistry

pytestmark = pytest.mark.usefixtures("network_forbidden")


def _info(**overrides: object) -> ExchangeInfo:
    values: dict[str, object] = {
        "mic": "ZZTE",
        "name": "Synthetic test exchange",
        "country": "XX",
        "tz": ZoneInfo("Europe/London"),
        "currency": "EUR",
        "quote_scale": 100,
        "weekend": frozenset({5, 6}),
        "close_local": None,
        "xcal_code": None,
        "tier_research": "D",
        "sanctioned": False,
        "notes": "",
    }
    values.update(overrides)
    return ExchangeInfo(**values)  # type: ignore[arg-type]


def _registry_info(mic: str) -> ExchangeInfo:
    return ExchangeRegistry().get(mic)


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_calendar_quality_values() -> None:
    assert [q.value for q in CalendarQuality] == [
        "library",
        "in_house",
        "weekday_fallback",
        "always_open",
    ]


def test_every_implementation_satisfies_the_protocol() -> None:
    assert isinstance(calendar_for("XIST"), ExchangeCalendar)
    assert isinstance(calendar_for("XCAI"), ExchangeCalendar)
    assert isinstance(AlwaysOpenCalendar(), ExchangeCalendar)
    assert isinstance(InHouseCalendar(), ExchangeCalendar)


def test_calendar_for_uses_the_library_when_exchanges_csv_names_a_code() -> None:
    xist = calendar_for("XIST")
    assert xist.quality is CalendarQuality.LIBRARY
    assert xist.xcal_code == "XIST"  # type: ignore[attr-defined]
    assert calendar_for("XJPX").xcal_code == "XTKS"  # type: ignore[attr-defined]
    assert calendar_for("BMEX").xcal_code == "XMAD"  # type: ignore[attr-defined]
    assert calendar_for("MISX").xcal_code == "XMOS"  # type: ignore[attr-defined]
    assert calendar_for("XNAS").xcal_code == "XNYS"  # type: ignore[attr-defined]
    assert calendar_for("XIST") is calendar_for("XIST")


def test_calendar_for_falls_back_to_weekdays_without_a_code() -> None:
    xcai = calendar_for("XCAI")
    assert xcai.quality is CalendarQuality.WEEKDAY_FALLBACK
    assert xcai.is_session(date(2026, 11, 6)) is False  # Friday, weekend for XCAI
    assert xcai.is_session(date(2026, 11, 8)) is True  # Sunday
    assert calendar_for("IEXG").quality is CalendarQuality.WEEKDAY_FALLBACK


def test_calendar_for_refuses_unknown_and_segment_mics() -> None:
    with pytest.raises(UnknownMarketError):
        calendar_for("ZZZZ")
    with pytest.raises(UnknownMarketError):
        calendar_for("XTKS")


def test_cheap_properties_never_build_the_library_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("the library schedule must not be built for cheap properties")

    monkeypatch.setattr(calendar_module, "_library_schedule", _forbidden)
    fresh = LibraryCalendar(_registry_info("XIST"))
    assert fresh.mic == "XIST"
    assert fresh.tz == ZoneInfo("Europe/Istanbul")
    assert fresh.quality is CalendarQuality.LIBRARY
    assert fresh.xcal_code == "XIST"
    via_registry = calendar_for("XIST")
    assert via_registry.quality is CalendarQuality.LIBRARY
    assert via_registry.mic == "XIST"


def test_library_sessions_are_dst_correct_in_utc() -> None:
    xlon = LibraryCalendar(_registry_info("XLON"), start=date(2026, 1, 1), end=date(2026, 12, 31))
    assert xlon.session_open_utc(date(2026, 3, 27)) == _utc("2026-03-27T08:00")
    assert xlon.session_close_utc(date(2026, 3, 27)) == _utc("2026-03-27T16:30")
    assert xlon.session_open_utc(date(2026, 3, 30)) == _utc("2026-03-30T07:00")
    assert xlon.session_close_utc(date(2026, 3, 30)) == _utc("2026-03-30T15:30")

    xnys = LibraryCalendar(_registry_info("XNYS"), start=date(2026, 1, 1), end=date(2026, 12, 31))
    assert xnys.session_open_utc(date(2026, 3, 6)) == _utc("2026-03-06T14:30")
    assert xnys.session_close_utc(date(2026, 3, 6)) == _utc("2026-03-06T21:00")
    assert xnys.session_open_utc(date(2026, 3, 9)) == _utc("2026-03-09T13:30")
    assert xnys.session_close_utc(date(2026, 3, 9)) == _utc("2026-03-09T20:00")


def test_lunch_breaks_come_from_the_library() -> None:
    xjpx = calendar_for("XJPX")
    xhkg = calendar_for("XHKG")
    xshg = calendar_for("XSHG")
    xist = calendar_for("XIST")
    assert xjpx.session_break_utc(date(2026, 11, 9)) == (  # type: ignore[attr-defined]
        _utc("2026-11-09T02:30"),
        _utc("2026-11-09T03:30"),
    )
    assert xhkg.session_break_utc(date(2026, 11, 9)) == (  # type: ignore[attr-defined]
        _utc("2026-11-09T04:00"),
        _utc("2026-11-09T05:00"),
    )
    assert xshg.session_break_utc(date(2026, 11, 9)) == (  # type: ignore[attr-defined]
        _utc("2026-11-09T03:30"),
        _utc("2026-11-09T05:00"),
    )
    assert xist.session_break_utc(date(2026, 11, 9)) is None


def test_library_tz_is_the_civil_zone_and_instants_come_from_the_library() -> None:
    xbog = calendar_for("XBOG")
    assert xbog.tz == ZoneInfo("America/Bogota")
    library = LibraryCalendar(
        _registry_info("XBOG"), start=date(2026, 1, 1), end=date(2026, 12, 31)
    )
    assert library.library_tz.key == "America/New_York"
    assert library.session_close_utc(date(2026, 7, 15)) == _utc("2026-07-15T20:00")
    assert library.session_close_utc(date(2026, 1, 15)) == _utc("2026-01-15T21:00")


def test_weekmask_history_is_honoured() -> None:
    xtae = LibraryCalendar(
        _registry_info("XTAE"), start=date(2025, 12, 1), end=date(2026, 1, 31)
    )
    assert xtae.is_session(date(2025, 12, 28)) is True  # Sunday, Sun-Thu week
    assert xtae.is_session(date(2026, 1, 4)) is True  # Sunday, Sun-Thu week
    assert xtae.is_session(date(2025, 12, 26)) is False  # Friday
    assert xtae.is_session(date(2026, 1, 2)) is False  # Friday
    assert xtae.is_session(date(2026, 1, 5)) is True  # Monday, Mon-Fri week
    assert xtae.is_session(date(2026, 1, 9)) is True  # Friday, Mon-Fri week
    assert xtae.is_session(date(2026, 1, 11)) is False  # Sunday, Mon-Fri week

    xsau = LibraryCalendar(_registry_info("XSAU"), start=date(2026, 1, 1), end=date(2026, 12, 31))
    assert xsau.is_session(date(2026, 11, 6)) is False  # Friday
    assert xsau.is_session(date(2026, 11, 8)) is True  # Sunday


def test_latest_completed_session_waits_for_close_plus_delay() -> None:
    xist = calendar_for("XIST")
    delay = timedelta(minutes=45)
    assert xist.latest_completed_session(_utc("2026-11-09T15:44"), delay) == date(2026, 11, 6)
    assert xist.latest_completed_session(_utc("2026-11-09T15:45"), delay) == date(2026, 11, 9)
    # 2026-10-29 is a Turkish holiday; the last session is the half day 2026-10-28.
    assert xist.latest_completed_session(_utc("2026-10-29T20:00"), delay) == date(2026, 10, 28)
    with pytest.raises(ValueError):
        xist.latest_completed_session(datetime(2026, 11, 9, 15, 45), delay)
    with pytest.raises(ValueError):
        xist.latest_completed_session(_utc("2026-11-09T15:45"), timedelta(minutes=-1))


def test_library_range_errors_are_explicit() -> None:
    xnys = LibraryCalendar(
        _registry_info("XNYS"), start=date(2024, 1, 1), end=date(2027, 12, 31)
    )
    with pytest.raises(CalendarRangeError) as before:
        xnys.is_session(date(2023, 12, 29))
    message = str(before.value)
    assert "XNYS" in message
    assert "2024-01-01" in message
    assert "2027-12-31" in message
    with pytest.raises(CalendarRangeError):
        xnys.is_session(date(2028, 1, 3))
    assert xnys.is_session(date(2024, 1, 1)) is False  # holiday inside the window
    with pytest.raises(CalendarRangeError):
        xnys.previous_session(date(2024, 1, 2))

    clamped = LibraryCalendar(
        _registry_info("XSHG"), start=date(2026, 1, 1), end=date(2027, 12, 31)
    )
    assert clamped.window == (date(2026, 1, 1), date(2026, 12, 31))


def test_library_calendar_beyond_its_bound_raises_but_registry_answers_with_fallback() -> None:
    # The library object itself keeps raising outside its window (spec 3.8). The calendar_for
    # composite answers past bound_max with the weekday fallback instead (ledger ruling).
    with pytest.raises(CalendarRangeError) as after:
        LibraryCalendar(
            _registry_info("XSHG"), start=date(2026, 1, 1), end=date(2027, 12, 31)
        ).is_session(date(2027, 1, 4))
    assert after.value.last == date(2026, 12, 31)

    shanghai = calendar_for("XSHG")
    assert shanghai.is_session(date(2027, 1, 4)) is True  # Monday, weekday fallback
    assert shanghai.is_session(date(2027, 1, 2)) is False  # Saturday
    assert shanghai.quality_for(date(2027, 1, 4)) is CalendarQuality.WEEKDAY_FALLBACK
    assert shanghai.quality_for(date(2026, 12, 31)) is CalendarQuality.LIBRARY
    assert shanghai.session_close_utc(date(2027, 1, 4)) == _utc("2027-01-04T07:00")
    with pytest.raises(CalendarRangeError):
        calendar_for("XNYS").is_session(date(1900, 1, 2))


def test_composite_answers_across_the_upper_bound_without_raising() -> None:
    # calendar_for("XSHG") is cached, so comparing it with itself proves nothing; the expected
    # values below are fixed instants and dates (XSHG closes 15:00 Asia/Shanghai = 07:00 UTC).
    shanghai = calendar_for("XSHG")
    assert shanghai.window[1] == date(2026, 12, 31)
    assert shanghai.sessions(date(2026, 12, 28), date(2027, 1, 8)) == [
        date(2026, 12, 28),
        date(2026, 12, 29),
        date(2026, 12, 30),
        date(2026, 12, 31),
        date(2027, 1, 1),
        date(2027, 1, 4),
        date(2027, 1, 5),
        date(2027, 1, 6),
        date(2027, 1, 7),
        date(2027, 1, 8),
    ]
    assert shanghai.previous_session(date(2027, 1, 1)) == date(2026, 12, 31)
    assert shanghai.previous_session(date(2027, 1, 4)) == date(2027, 1, 1)
    assert shanghai.session_open_utc(date(2027, 1, 4)) == _utc("2027-01-03T16:00")
    assert shanghai.session_break_utc(date(2027, 1, 4)) is None
    delay = timedelta(minutes=45)
    assert shanghai.latest_completed_session(_utc("2027-01-04T08:00"), delay) == date(2027, 1, 4)
    assert shanghai.latest_completed_session(_utc("2027-01-04T06:00"), delay) == date(2027, 1, 1)


def test_library_clamps_the_window_to_bound_min() -> None:
    # The requested start (2019-06-01) precedes XSAU's recorded lower bound, so the library
    # window is clamped to bound_min (2021-01-01) and the upper clamp keeps 2021-03-31.
    xsau = LibraryCalendar(_registry_info("XSAU"), start=date(2019, 6, 1), end=date(2021, 3, 31))
    assert xsau.window == (date(2021, 1, 1), date(2021, 3, 31))


def test_non_sessions_have_no_open_close_or_break() -> None:
    with pytest.raises(NotASessionError):
        calendar_for("XIST").session_open_utc(date(2026, 10, 29))
    with pytest.raises(NotASessionError):
        calendar_for("XIST").session_close_utc(date(2026, 10, 29))
    with pytest.raises(NotASessionError):
        calendar_for("XIST").session_break_utc(date(2026, 10, 29))
    fallback = calendar_for("XCAI")
    with pytest.raises(NotASessionError):
        fallback.session_open_utc(date(2026, 11, 6))
    with pytest.raises(NotASessionError):
        InHouseCalendar().session_close_utc(date(2026, 7, 3))


def test_sessions_are_inclusive_and_ordered() -> None:
    xist = calendar_for("XIST")
    assert xist.sessions(date(2026, 10, 26), date(2026, 11, 2)) == [
        date(2026, 10, 26),
        date(2026, 10, 27),
        date(2026, 10, 28),
        date(2026, 10, 30),
        date(2026, 11, 2),
    ]
    with pytest.raises(ValueError):
        xist.sessions(date(2026, 11, 2), date(2026, 10, 26))


def test_previous_session_is_strictly_before_the_day() -> None:
    xist = calendar_for("XIST")
    assert xist.previous_session(date(2026, 10, 29)) == date(2026, 10, 28)
    assert xist.previous_session(date(2026, 11, 9)) == date(2026, 11, 6)


def test_weekday_fallback_uses_weekend_and_close_from_exchanges_csv() -> None:
    london = WeekdayFallbackCalendar(_info(tz=ZoneInfo("Europe/London"), weekend=frozenset({5, 6})))
    assert london.session_close_utc(date(2026, 3, 27)) == _utc("2026-03-27T23:00")
    assert london.session_close_utc(date(2026, 3, 30)) == _utc("2026-03-30T22:00")
    assert london.session_open_utc(date(2026, 3, 30)) == _utc("2026-03-29T23:00")
    assert london.session_break_utc(date(2026, 3, 30)) is None
    assert london.previous_session(date(2026, 3, 30)) == date(2026, 3, 27)

    cairo = WeekdayFallbackCalendar(
        _info(
            mic="ZZCA",
            tz=ZoneInfo("Africa/Cairo"),
            weekend=frozenset({4, 5}),
            close_local=time(14, 30),
        )
    )
    assert cairo.is_session(date(2026, 11, 8)) is True  # Sunday
    assert cairo.is_session(date(2026, 11, 6)) is False  # Friday
    assert cairo.session_close_utc(date(2026, 11, 8)) == _utc("2026-11-08T12:30")
    assert cairo.quality is CalendarQuality.WEEKDAY_FALLBACK


def test_a_sunday_to_thursday_market_trades_on_sunday_and_not_on_friday() -> None:
    kuwait = calendar_for("XKUW")
    assert kuwait.quality is CalendarQuality.WEEKDAY_FALLBACK
    assert kuwait.is_session(date(2026, 11, 8)) is True  # Sunday
    assert kuwait.is_session(date(2026, 11, 6)) is False  # Friday


def test_always_open_calendar() -> None:
    crypto = AlwaysOpenCalendar()
    assert crypto.is_session(date(2026, 11, 8)) is True
    assert crypto.session_open_utc(date(2026, 11, 8)) == _utc("2026-11-08T00:00")
    assert crypto.session_close_utc(date(2026, 11, 8)) == _utc("2026-11-09T00:00")
    assert crypto.latest_completed_session(_utc("2026-11-09T00:30"), timedelta(minutes=45)) == (
        date(2026, 11, 7)
    )
    assert crypto.latest_completed_session(_utc("2026-11-09T00:45"), timedelta(minutes=45)) == (
        date(2026, 11, 8)
    )
    assert crypto.mic == "24/7"
    assert crypto.tz == ZoneInfo("UTC")
    assert crypto.quality is CalendarQuality.ALWAYS_OPEN
    assert crypto.previous_session(date(2026, 11, 8)) == date(2026, 11, 7)


def test_in_house_adapter_wraps_the_us_market_calendar() -> None:
    adapter = InHouseCalendar()
    assert adapter.quality is CalendarQuality.IN_HOUSE
    assert adapter.mic == "XNYS"
    assert adapter.tz == ZoneInfo("America/New_York")
    assert adapter.is_session(date(2026, 7, 3)) is False
    assert adapter.session_open_utc(date(2026, 1, 2)) == _utc("2026-01-02T14:30")
    assert adapter.session_close_utc(date(2026, 1, 2)) == _utc("2026-01-02T21:00")
    assert adapter.session_close_utc(date(2026, 11, 27)) == _utc("2026-11-27T18:00")
    assert adapter.latest_completed_session(
        _utc("2026-11-27T18:45"), timedelta(minutes=45)
    ) == date(2026, 11, 27)
    assert adapter.latest_completed_session(
        _utc("2026-11-27T18:44"), timedelta(minutes=45)
    ) == date(2026, 11, 25)


def test_datetimes_are_refused_where_dates_are_expected() -> None:
    moment = datetime(2026, 11, 9, 10, tzinfo=UTC)
    with pytest.raises(TypeError):
        calendar_for("XIST").is_session(moment)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        WeekdayFallbackCalendar(_info()).is_session(moment)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AlwaysOpenCalendar().session_open_utc(moment)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        InHouseCalendar().is_session(moment)  # type: ignore[arg-type]
