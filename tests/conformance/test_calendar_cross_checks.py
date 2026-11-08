"""Gate: library calendars agree with the in-house XNYS rules and the official BIST 2026 lists.

Every library calendar is built from the packaged ``exchanges.csv``; the expectations below
are the spec's official lists (spec section 3.8), embedded as constants. A future difference
is added to the matching ``_KNOWN_DIFFERENCES`` dict with its reason, never hidden.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Final

import pytest

from crocodile.core.markets.calendar import InHouseCalendar, LibraryCalendar
from crocodile.core.markets.exchanges import ExchangeInfo, ExchangeRegistry

pytestmark = pytest.mark.usefixtures("network_forbidden")

_REPO: Final = Path(__file__).resolve().parents[2]

_XNYS_KNOWN_DIFFERENCES: Final[dict[date, str]] = {
    date(2025, 1, 9): "library closed: NYSE closed for the National Day of Mourning for "
    "President Carter; the in-house rules have no ad hoc closures",
    date(2027, 12, 31): "in-house closed: get_holidays observes New Year 2028 (a Saturday) "
    "on Friday 2027-12-31; NYSE does not, so the library is open",
}
_XNYS_EARLY_CLOSES: Final = (
    date(2024, 7, 3),
    date(2024, 11, 29),
    date(2024, 12, 24),
    date(2025, 7, 3),
    date(2025, 11, 28),
    date(2025, 12, 24),
    date(2026, 11, 27),
    date(2026, 12, 24),
    date(2027, 11, 26),
)

_BIST_2026_CLOSED: Final = (
    date(2026, 1, 1),
    date(2026, 3, 20),
    date(2026, 4, 23),
    date(2026, 5, 1),
    date(2026, 5, 19),
    date(2026, 5, 27),
    date(2026, 5, 28),
    date(2026, 5, 29),
    date(2026, 7, 15),
    date(2026, 10, 29),
)
_BIST_2026_HALF_DAYS: Final = (date(2026, 3, 19), date(2026, 5, 26), date(2026, 10, 28))
# Measured with exchange_calendars 4.13.2: no difference from the official lists.
_BIST_2026_KNOWN_DIFFERENCES: Final[dict[date, str]] = {}

# exchange_calendars models BVC hours on America/New_York (US-DST aligned); exchanges.csv
# keeps the civil zone America/Bogota.
_TZ_KNOWN_DIFFERENCES: Final[dict[str, str]] = {
    "XBOG": "exchange_calendars models BVC hours on America/New_York (US-DST aligned); "
    "exchanges.csv keeps the civil zone America/Bogota",
}


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _days(start: date, end: date) -> Iterator[date]:
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def _registry_info(mic: str) -> ExchangeInfo:
    return ExchangeRegistry().get(mic)


@pytest.fixture(scope="module")
def library_calendars() -> dict[str, LibraryCalendar]:
    """One library calendar per exchanges.csv row with an xcal_code, over a short window."""
    built: dict[str, LibraryCalendar] = {}
    for info in ExchangeRegistry().all():
        if info.xcal_code is None:
            continue
        built[info.mic] = LibraryCalendar(info, start=date(2026, 11, 2), end=date(2026, 11, 13))
    return built


def test_in_house_xnys_matches_the_library_2024_2027_except_the_listed_differences() -> None:
    library = LibraryCalendar(
        _registry_info("XNYS"), start=date(2024, 1, 1), end=date(2027, 12, 31)
    )
    in_house = InHouseCalendar()

    differing: set[date] = set()
    for day in _days(date(2024, 1, 1), date(2027, 12, 31)):
        lib_session = library.is_session(day)
        own_session = in_house.is_session(day)
        if lib_session != own_session:
            differing.add(day)
            continue
        if lib_session:
            assert in_house.session_open_utc(day) == library.session_open_utc(day), day
            assert in_house.session_close_utc(day) == library.session_close_utc(day), day

    assert differing == set(_XNYS_KNOWN_DIFFERENCES)
    for day in _XNYS_EARLY_CLOSES:
        assert library.is_session(day)
        assert library.session_close_utc(day) == in_house.session_close_utc(day), day


def test_bist_2026_official_holidays_and_half_days_match_the_library() -> None:
    bist = LibraryCalendar(
        _registry_info("XIST"), start=date(2026, 1, 1), end=date(2026, 12, 31)
    )
    weekdays_2026 = [
        day for day in _days(date(2026, 1, 1), date(2026, 12, 31)) if day.weekday() < 5
    ]
    closed_weekdays = {day for day in weekdays_2026 if not bist.is_session(day)}
    sessions = bist.sessions(date(2026, 1, 1), date(2026, 12, 31))
    half_days = {day for day in sessions if bist.session_close_utc(day).time() < time(15, 0)}

    unexplained = (closed_weekdays ^ set(_BIST_2026_CLOSED)) | (
        half_days ^ set(_BIST_2026_HALF_DAYS)
    )
    assert set(unexplained) == set(_BIST_2026_KNOWN_DIFFERENCES)

    assert len(sessions) == 251
    assert bist.session_close_utc(date(2026, 11, 9)) == _utc("2026-11-09T15:00")
    # The library closes BIST half days at 12:30 TRT (09:30Z); the spec lists the days only.
    for day in _BIST_2026_HALF_DAYS:
        assert bist.session_close_utc(day) == _utc(f"{day.isoformat()}T09:30")


def test_every_xcal_code_in_exchanges_csv_builds_a_library_calendar(
    library_calendars: dict[str, LibraryCalendar],
) -> None:
    assert len(library_calendars) == 69
    for mic, calendar in library_calendars.items():
        assert calendar.sessions(date(2026, 11, 2), date(2026, 11, 13)), mic


def test_exchanges_csv_zones_agree_with_the_library_except_listed(
    library_calendars: dict[str, LibraryCalendar],
) -> None:
    probes = (_utc("2026-01-15T12:00"), _utc("2026-07-15T12:00"))
    for mic, calendar in library_calendars.items():
        if mic in _TZ_KNOWN_DIFFERENCES:
            continue
        for probe in probes:
            library_offset = probe.astimezone(calendar.library_tz).utcoffset()
            civil_offset = probe.astimezone(calendar.tz).utcoffset()
            assert library_offset == civil_offset, (mic, probe)
    assert set(_TZ_KNOWN_DIFFERENCES) <= set(library_calendars)
    xbog = library_calendars["XBOG"]
    assert xbog.library_tz.key == "America/New_York"
    assert xbog.tz.key == "America/Bogota"


def test_generated_reference_data_is_reproducible_with_the_locked_library() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/build_markets_reference.py", "--check"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
