"""Test doubles for the scheduler's exchange calendars (not collected: no ``test_`` prefix).

``FixedCalendar`` answers the ``ExchangeCalendar`` members the coordinator calls, with explicit
hours, weekend and holidays, so scheduler tests are deterministic and offline.
"""

import datetime
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from crocodile.core.markets.calendar import CalendarQuality
from crocodile.core.markets.errors import CalendarRangeError

_ONE_DAY = datetime.timedelta(days=1)
_LOOKBACK_DAYS = 31


@dataclass(frozen=True)
class FixedCalendar:
    """An ExchangeCalendar with explicit hours, weekend and holidays, for deterministic tests."""

    mic: str
    tz: ZoneInfo
    close_local: datetime.time
    open_local: datetime.time = datetime.time(10, 0)
    weekend: frozenset[int] = frozenset({5, 6})
    holidays: frozenset[datetime.date] = field(default_factory=frozenset)
    quality: CalendarQuality = CalendarQuality.LIBRARY
    # Models a library bound: dates after this day answer as WEEKDAY_FALLBACK (task 03 ruling).
    fallback_after: datetime.date | None = None

    def is_session(self, d: datetime.date) -> bool:
        return d.weekday() not in self.weekend and d not in self.holidays

    def quality_for(self, d: datetime.date) -> CalendarQuality:
        if self.fallback_after is not None and d > self.fallback_after:
            return CalendarQuality.WEEKDAY_FALLBACK
        return self.quality

    def session_open_utc(self, d: datetime.date) -> datetime.datetime:
        local = datetime.datetime.combine(d, self.open_local, tzinfo=self.tz)
        return local.astimezone(datetime.UTC)

    def session_close_utc(self, d: datetime.date) -> datetime.datetime:
        local = datetime.datetime.combine(d, self.close_local, tzinfo=self.tz)
        return local.astimezone(datetime.UTC)

    def session_break_utc(
        self, d: datetime.date
    ) -> tuple[datetime.datetime, datetime.datetime] | None:
        return None

    def sessions(self, start: datetime.date, end: datetime.date) -> list[datetime.date]:
        if start > end:
            raise ValueError(f"start {start.isoformat()} is after end {end.isoformat()}")
        found: list[datetime.date] = []
        day = start
        while day <= end:
            if self.is_session(day):
                found.append(day)
            day += _ONE_DAY
        return found

    def previous_session(self, d: datetime.date) -> datetime.date:
        candidate = d - _ONE_DAY
        while not self.is_session(candidate):
            candidate -= _ONE_DAY
        return candidate

    def latest_completed_session(
        self, now_utc: datetime.datetime, delay: datetime.timedelta
    ) -> datetime.date | None:
        today = now_utc.astimezone(self.tz).date()
        for offset in range(_LOOKBACK_DAYS):
            candidate = today - offset * _ONE_DAY
            if self.is_session(candidate) and self.session_close_utc(candidate) + delay <= now_utc:
                return candidate
        return None


@dataclass(frozen=True)
class RaisingCalendar(FixedCalendar):
    """A calendar that cannot answer: its latest session raises, as an out-of-range library does."""

    def latest_completed_session(
        self, now_utc: datetime.datetime, delay: datetime.timedelta
    ) -> datetime.date | None:
        requested = now_utc.astimezone(self.tz).date()
        raise CalendarRangeError(self.mic, requested, None, None)


NZX = FixedCalendar("XNZE", ZoneInfo("Pacific/Auckland"), datetime.time(16, 45))
NYSE_LIKE = FixedCalendar("XNYS", ZoneInfo("America/New_York"), datetime.time(16, 0))
