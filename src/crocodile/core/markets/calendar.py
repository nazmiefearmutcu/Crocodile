"""Exchange trading calendars behind one protocol.

Every operating MIC gets one calendar object with the same methods, in the civil time
zone of ``exchanges.csv``. Session dates are local dates; every instant returned is
timezone-aware UTC. The implementations are:

* ``LibraryCalendar`` wraps ``exchange_calendars`` (imported lazily, only when a schedule is
  first needed). It answers from a precomputed schedule and raises ``CalendarRangeError``
  outside the window the library can vouch for.
* ``WeekdayFallbackCalendar`` knows only the weekend and the regular close of a MIC that the
  library does not cover. It knows no holidays: a missing bar on such a day is ``no_data``,
  not a gap.
* ``AlwaysOpenCalendar`` is the 24/7 calendar used for crypto venues.
* ``InHouseCalendar`` adapts the in-house ``USMarketCalendar`` without changing it.

``calendar_for(mic)`` picks the library calendar when ``exchanges.csv`` names an
``xcal_code``, else the weekday fallback. Past the library's upper bound it answers with the
weekday fallback and reports the degradation through ``quality_for(d)`` instead of raising.
Reading ``mic``, ``tz``, ``quality`` and ``xcal_code`` never imports or builds the library.
"""

from __future__ import annotations

import bisect
import functools
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from crocodile.core.markets.errors import CalendarRangeError, NotASessionError
from crocodile.core.markets.exchanges import ExchangeInfo, ExchangeRegistry

if TYPE_CHECKING:
    from crocodile.core.scheduler.calendar import USMarketCalendar

__all__ = [
    "DEFAULT_FALLBACK_CLOSE",
    "MAX_LOOKBACK_DAYS",
    "AlwaysOpenCalendar",
    "CalendarQuality",
    "ExchangeCalendar",
    "InHouseCalendar",
    "LibraryCalendar",
    "WeekdayFallbackCalendar",
    "calendar_for",
]

# Spec 3.8: a calendar whose regular close is unknown closes at 23:00 local time.
DEFAULT_FALLBACK_CLOSE: Final = time(23, 0)
# Search horizon for latest_completed_session, in days.
MAX_LOOKBACK_DAYS: Final = 31

_ONE_DAY: Final = timedelta(days=1)
_ZERO: Final = timedelta(0)
_NEW_YORK: Final = ZoneInfo("America/New_York")
_UTC_ZONE: Final = ZoneInfo("UTC")
# The in-house rules have no range limit; this only bounds the previous-session walk.
_IN_HOUSE_LOOKBACK: Final = 400
_FALLBACK_LOOKBACK: Final = 7


class CalendarQuality(StrEnum):
    """How a session calendar was produced; the markets table records it per MIC."""

    LIBRARY = "library"
    IN_HOUSE = "in_house"
    WEEKDAY_FALLBACK = "weekday_fallback"
    ALWAYS_OPEN = "always_open"


@runtime_checkable
class ExchangeCalendar(Protocol):
    """The calendar contract every MIC and crypto venue is answered through."""

    @property
    def mic(self) -> str: ...

    @property
    def tz(self) -> ZoneInfo:
        """The civil zone in which local session dates live."""
        ...

    @property
    def quality(self) -> CalendarQuality: ...

    def is_session(self, d: date) -> bool: ...

    def quality_for(self, d: date) -> CalendarQuality:
        """The quality that answers for ``d``; differs from ``quality`` only past a bound."""
        ...

    def session_open_utc(self, d: date) -> datetime: ...

    def session_close_utc(self, d: date) -> datetime: ...

    def session_break_utc(self, d: date) -> tuple[datetime, datetime] | None: ...

    def sessions(self, start: date, end: date) -> list[date]: ...

    def previous_session(self, d: date) -> date: ...

    def latest_completed_session(self, now_utc: datetime, delay: timedelta) -> date | None: ...


# ---------------------------------------------------------------------------
# Shared argument checks and the latest-completed-session walk.


def _as_date(value: object) -> date:
    """Return ``value`` if it is a plain date; refuse datetimes, which are dates too."""
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"expected a date, got {type(value).__name__}")
    return value


def _check_span(start: object, end: object) -> None:
    first = _as_date(start)
    last = _as_date(end)
    if first > last:
        raise ValueError(f"start {first.isoformat()} is after end {last.isoformat()}")


def _latest_completed(cal: ExchangeCalendar, now_utc: datetime, delay: timedelta) -> date | None:
    """Latest session ``s`` with ``close(s) + delay <= now_utc``, searched over 31 days."""
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    if delay < _ZERO:
        raise ValueError("delay must not be negative")
    today = now_utc.astimezone(cal.tz).date()
    for offset in range(MAX_LOOKBACK_DAYS):
        candidate = today - offset * _ONE_DAY
        if cal.is_session(candidate) and cal.session_close_utc(candidate) + delay <= now_utc:
            return candidate
    return None


# ---------------------------------------------------------------------------
# exchange_calendars: lazy schedule builder and the precomputed schedule.


@dataclass(frozen=True, slots=True, eq=False)
class _Schedule:
    window: tuple[date, date]
    library_tz: ZoneInfo
    labels: tuple[date, ...]
    index: dict[date, int]
    opens: tuple[datetime, ...]
    closes: tuple[datetime, ...]
    breaks: tuple[tuple[datetime, datetime] | None, ...]


def _utc_of(stamp: Any) -> datetime:
    value: datetime = stamp.to_pydatetime()
    return value.astimezone(UTC)


def _build_schedule(cal: Any, lo: date, hi: date) -> _Schedule:
    import pandas as pd

    schedule: Any = cal.schedule
    labels = tuple(stamp.date() for stamp in schedule.index)
    opens = tuple(_utc_of(stamp) for stamp in schedule["open"].tolist())
    closes = tuple(_utc_of(stamp) for stamp in schedule["close"].tolist())
    starts = schedule["break_start"].tolist()
    ends = schedule["break_end"].tolist()
    breaks: list[tuple[datetime, datetime] | None] = []
    for start_stamp, end_stamp in zip(starts, ends, strict=True):
        if pd.isna(start_stamp) or pd.isna(end_stamp):
            breaks.append(None)
        else:
            breaks.append((_utc_of(start_stamp), _utc_of(end_stamp)))
    library_tz: ZoneInfo = cal.tz
    return _Schedule(
        window=(lo, hi),
        library_tz=library_tz,
        labels=labels,
        index={day: position for position, day in enumerate(labels)},
        opens=opens,
        closes=closes,
        breaks=tuple(breaks),
    )


def _library_schedule(
    code: str, start: date | None, end: date | None, *, mic: str
) -> _Schedule:
    """Build the schedule of one library calendar; the only place the library is imported.

    With neither bound given the library's own default window is used: 20 years back to one
    year ahead of the library import, which is clock-relative inside the library. Callers that
    need determinism pass explicit bounds.
    """
    import exchange_calendars as xcals

    if start is None and end is None:
        cal: Any = xcals.get_calendar(code)
        cls: Any = type(cal)
        return _build_schedule(cal, _as_day(cls.default_start()), _as_day(cls.default_end()))

    if start is None or end is None:
        defaults: Any = type(xcals.get_calendar(code))
        lo = start if start is not None else _as_day(defaults.default_start())
        hi = end if end is not None else _as_day(defaults.default_end())
    else:
        lo, hi = start, end

    try:
        cal = xcals.get_calendar(code, start=lo.isoformat(), end=hi.isoformat())
    except ValueError:
        # The library refuses windows outside its recorded bounds; clamp, then retry.
        bounds: Any = type(xcals.get_calendar(code))
        bound_min: Any = bounds.bound_min()
        bound_max: Any = bounds.bound_max()
        clamped_lo = max(lo, _as_day(bound_min)) if bound_min is not None else lo
        clamped_hi = min(hi, _as_day(bound_max)) if bound_max is not None else hi
        if clamped_lo > clamped_hi:
            raise CalendarRangeError(mic, lo, clamped_lo, clamped_hi) from None
        lo, hi = clamped_lo, clamped_hi
        cal = xcals.get_calendar(code, start=lo.isoformat(), end=hi.isoformat())
    return _build_schedule(cal, lo, hi)


def _as_day(stamp: Any) -> date:
    day: date = stamp.date()
    return day


def _library_bound_max(code: str) -> date | None:
    """The library's recorded upper bound for ``code``, or None when it declares none."""
    import exchange_calendars as xcals

    bounds: Any = type(xcals.get_calendar(code))
    bound: Any = bounds.bound_max()
    return None if bound is None else _as_day(bound)


class LibraryCalendar:
    """A calendar answered by ``exchange_calendars`` from a precomputed schedule.

    Construction is cheap and imports nothing. The first query builds the schedule for the
    window ``[start, end]`` (clamped to the library's recorded bounds), and every later query
    is answered from that schedule. Dates outside the window raise ``CalendarRangeError``.
    """

    def __init__(
        self, info: ExchangeInfo, *, start: date | None = None, end: date | None = None
    ) -> None:
        if info.xcal_code is None:
            raise ValueError(f"{info.mic} has no exchange_calendars code")
        if start is not None:
            _as_date(start)
        if end is not None:
            _as_date(end)
        self._info = info
        self._code: str = info.xcal_code
        self._start = start
        self._end = end

    @property
    def mic(self) -> str:
        return self._info.mic

    @property
    def tz(self) -> ZoneInfo:
        return self._info.tz

    @property
    def quality(self) -> CalendarQuality:
        return CalendarQuality.LIBRARY

    @property
    def xcal_code(self) -> str:
        return self._code

    @functools.cached_property
    def _schedule(self) -> _Schedule:
        return _library_schedule(self._code, self._start, self._end, mic=self.mic)

    @property
    def window(self) -> tuple[date, date]:
        """Effective ``[first, last]`` dates this calendar answers for; builds the schedule."""
        return self._schedule.window

    @property
    def library_tz(self) -> ZoneInfo:
        """The library's own model zone; it can differ from ``tz`` (see XBOG)."""
        return self._schedule.library_tz

    def _position(self, d: date) -> int | None:
        _as_date(d)
        schedule = self._schedule
        first, last = schedule.window
        if d < first or d > last:
            raise CalendarRangeError(self.mic, d, first, last)
        return schedule.index.get(d)

    def _session_position(self, d: date) -> int:
        position = self._position(d)
        if position is None:
            raise NotASessionError(self.mic, d)
        return position

    def is_session(self, d: date) -> bool:
        return self._position(d) is not None

    def quality_for(self, d: date) -> CalendarQuality:
        self._position(d)
        return CalendarQuality.LIBRARY

    def session_open_utc(self, d: date) -> datetime:
        return self._schedule.opens[self._session_position(d)]

    def session_close_utc(self, d: date) -> datetime:
        return self._schedule.closes[self._session_position(d)]

    def session_break_utc(self, d: date) -> tuple[datetime, datetime] | None:
        return self._schedule.breaks[self._session_position(d)]

    def sessions(self, start: date, end: date) -> list[date]:
        _check_span(start, end)
        schedule = self._schedule
        first, last = schedule.window
        for bound in (start, end):
            if bound < first or bound > last:
                raise CalendarRangeError(self.mic, bound, first, last)
        low = bisect.bisect_left(schedule.labels, start)
        high = bisect.bisect_right(schedule.labels, end)
        return list(schedule.labels[low:high])

    def previous_session(self, d: date) -> date:
        _as_date(d)
        schedule = self._schedule
        first, last = schedule.window
        if d - _ONE_DAY > last:
            raise CalendarRangeError(self.mic, d, first, last)
        position = bisect.bisect_left(schedule.labels, d) - 1
        if position < 0:
            raise CalendarRangeError(self.mic, d, first, last)
        return schedule.labels[position]

    def latest_completed_session(self, now_utc: datetime, delay: timedelta) -> date | None:
        return _latest_completed(self, now_utc, delay)


# ---------------------------------------------------------------------------
# Weekday fallback: weekend and regular close from exchanges.csv, no holidays.


class WeekdayFallbackCalendar:
    """Sessions are the days outside the MIC's weekend; holidays are not known.

    A missing bar on a weekday this calendar calls a session is ``no_data``, not a gap. The
    open is unknown and reported as local midnight; the close is the ``exchanges.csv`` close
    or ``DEFAULT_FALLBACK_CLOSE``. There is no break and no range limit.
    """

    def __init__(self, info: ExchangeInfo) -> None:
        self._info = info

    @property
    def mic(self) -> str:
        return self._info.mic

    @property
    def tz(self) -> ZoneInfo:
        return self._info.tz

    @property
    def quality(self) -> CalendarQuality:
        return CalendarQuality.WEEKDAY_FALLBACK

    def is_session(self, d: date) -> bool:
        return _as_date(d).weekday() not in self._info.weekend

    def quality_for(self, d: date) -> CalendarQuality:
        _as_date(d)
        return CalendarQuality.WEEKDAY_FALLBACK

    def _require_session(self, d: date) -> None:
        if not self.is_session(d):
            raise NotASessionError(self.mic, d)

    def session_open_utc(self, d: date) -> datetime:
        self._require_session(d)
        return datetime.combine(d, time(0, 0), tzinfo=self.tz).astimezone(UTC)

    def session_close_utc(self, d: date) -> datetime:
        self._require_session(d)
        close = self._info.close_local or DEFAULT_FALLBACK_CLOSE
        return datetime.combine(d, close, tzinfo=self.tz).astimezone(UTC)

    def session_break_utc(self, d: date) -> tuple[datetime, datetime] | None:
        self._require_session(d)
        return None

    def sessions(self, start: date, end: date) -> list[date]:
        _check_span(start, end)
        found: list[date] = []
        day = start
        while day <= end:
            if self.is_session(day):
                found.append(day)
            day += _ONE_DAY
        return found

    def previous_session(self, d: date) -> date:
        _as_date(d)
        for offset in range(1, _FALLBACK_LOOKBACK + 1):
            candidate = d - offset * _ONE_DAY
            if self.is_session(candidate):
                return candidate
        raise ValueError(f"{self.mic}: no weekday session within {_FALLBACK_LOOKBACK} days")

    def latest_completed_session(self, now_utc: datetime, delay: timedelta) -> date | None:
        return _latest_completed(self, now_utc, delay)


# ---------------------------------------------------------------------------
# Always open: 24/7 venues such as crypto.


class AlwaysOpenCalendar:
    """Every date is a session, from 00:00 UTC to 00:00 UTC the next day."""

    def __init__(self, mic: str = "24/7") -> None:
        self._mic = mic

    @property
    def mic(self) -> str:
        return self._mic

    @property
    def tz(self) -> ZoneInfo:
        return _UTC_ZONE

    @property
    def quality(self) -> CalendarQuality:
        return CalendarQuality.ALWAYS_OPEN

    def is_session(self, d: date) -> bool:
        _as_date(d)
        return True

    def quality_for(self, d: date) -> CalendarQuality:
        _as_date(d)
        return CalendarQuality.ALWAYS_OPEN

    def session_open_utc(self, d: date) -> datetime:
        return datetime.combine(_as_date(d), time(0, 0), tzinfo=UTC)

    def session_close_utc(self, d: date) -> datetime:
        return self.session_open_utc(d) + _ONE_DAY

    def session_break_utc(self, d: date) -> tuple[datetime, datetime] | None:
        _as_date(d)
        return None

    def sessions(self, start: date, end: date) -> list[date]:
        _check_span(start, end)
        found: list[date] = []
        day = start
        while day <= end:
            found.append(day)
            day += _ONE_DAY
        return found

    def previous_session(self, d: date) -> date:
        return _as_date(d) - _ONE_DAY

    def latest_completed_session(self, now_utc: datetime, delay: timedelta) -> date | None:
        return _latest_completed(self, now_utc, delay)


# ---------------------------------------------------------------------------
# In-house adapter: the existing US rules, unchanged.


class InHouseCalendar:
    """Adapter over the in-house ``USMarketCalendar`` (America/New_York, no range limit).

    The scheduler module is imported only when no calendar is passed, so importing this
    module never pulls in ``crocodile.core.scheduler`` (which imports the coordinator).
    """

    def __init__(self, calendar: USMarketCalendar | None = None, *, mic: str = "XNYS") -> None:
        if calendar is None:
            from crocodile.core.scheduler import calendar as scheduler_calendar

            calendar = scheduler_calendar.USMarketCalendar()
        self._calendar = calendar
        self._mic = mic

    @property
    def mic(self) -> str:
        return self._mic

    @property
    def tz(self) -> ZoneInfo:
        return _NEW_YORK

    @property
    def quality(self) -> CalendarQuality:
        return CalendarQuality.IN_HOUSE

    def is_session(self, d: date) -> bool:
        return bool(self._calendar.is_trading_day(_as_date(d)))

    def quality_for(self, d: date) -> CalendarQuality:
        _as_date(d)
        return CalendarQuality.IN_HOUSE

    def _hours(self, d: date) -> tuple[datetime, datetime]:
        hours = self._calendar.get_market_hours(_as_date(d)) if self.is_session(d) else None
        if hours is None:
            raise NotASessionError(self.mic, d)
        return hours

    def session_open_utc(self, d: date) -> datetime:
        return self._hours(d)[0].astimezone(UTC)

    def session_close_utc(self, d: date) -> datetime:
        return self._hours(d)[1].astimezone(UTC)

    def session_break_utc(self, d: date) -> tuple[datetime, datetime] | None:
        self._hours(d)
        return None

    def sessions(self, start: date, end: date) -> list[date]:
        _check_span(start, end)
        found: list[date] = []
        day = start
        while day <= end:
            if self.is_session(day):
                found.append(day)
            day += _ONE_DAY
        return found

    def previous_session(self, d: date) -> date:
        day = _as_date(d) - _ONE_DAY
        for _ in range(_IN_HOUSE_LOOKBACK):
            if self.is_session(day):
                return day
            day -= _ONE_DAY
        raise ValueError(f"{self.mic}: no session within {_IN_HOUSE_LOOKBACK} days")

    def latest_completed_session(self, now_utc: datetime, delay: timedelta) -> date | None:
        return _latest_completed(self, now_utc, delay)


# ---------------------------------------------------------------------------
# calendar_for: the composite that answers past the library's bound.


class _LibraryWithFallback:
    """The calendar ``calendar_for`` returns for a MIC that has an ``xcal_code``.

    Inside the library's window it answers from the library. Dates after its upper bound
    (``bound_max``, or the default window end when the library declares none) are answered
    by the weekday fallback instead of raising, and ``quality_for`` reports the switch.
    Dates before the library's lower bound still raise ``CalendarRangeError``.
    """

    def __init__(self, info: ExchangeInfo) -> None:
        if info.xcal_code is None:
            raise ValueError(f"{info.mic} has no exchange_calendars code")
        self._info = info
        self._code: str = info.xcal_code
        self._fallback = WeekdayFallbackCalendar(info)

    @functools.cached_property
    def _library(self) -> LibraryCalendar:
        return LibraryCalendar(self._info, end=_library_bound_max(self._code))

    @property
    def mic(self) -> str:
        return self._info.mic

    @property
    def tz(self) -> ZoneInfo:
        return self._info.tz

    @property
    def quality(self) -> CalendarQuality:
        return CalendarQuality.LIBRARY

    @property
    def xcal_code(self) -> str:
        return self._code

    @property
    def window(self) -> tuple[date, date]:
        return self._library.window

    @property
    def library_tz(self) -> ZoneInfo:
        return self._library.library_tz

    def _beyond(self, d: date) -> bool:
        _as_date(d)
        return d > self._library.window[1]

    def is_session(self, d: date) -> bool:
        if self._beyond(d):
            return self._fallback.is_session(d)
        return self._library.is_session(d)

    def quality_for(self, d: date) -> CalendarQuality:
        if self._beyond(d):
            return CalendarQuality.WEEKDAY_FALLBACK
        return self._library.quality_for(d)

    def session_open_utc(self, d: date) -> datetime:
        source: ExchangeCalendar = self._fallback if self._beyond(d) else self._library
        return source.session_open_utc(d)

    def session_close_utc(self, d: date) -> datetime:
        source: ExchangeCalendar = self._fallback if self._beyond(d) else self._library
        return source.session_close_utc(d)

    def session_break_utc(self, d: date) -> tuple[datetime, datetime] | None:
        source: ExchangeCalendar = self._fallback if self._beyond(d) else self._library
        return source.session_break_utc(d)

    def sessions(self, start: date, end: date) -> list[date]:
        _check_span(start, end)
        last = self._library.window[1]
        found: list[date] = []
        if start <= last:
            found.extend(self._library.sessions(start, min(end, last)))
        if end > last:
            found.extend(self._fallback.sessions(max(start, last + _ONE_DAY), end))
        return found

    def previous_session(self, d: date) -> date:
        if not self._beyond(d):
            return self._library.previous_session(d)
        last = self._library.window[1]
        candidate = self._fallback.previous_session(d)
        if candidate > last:
            return candidate
        # No fallback session lies between the library's last day and d: the answer is the
        # library's latest session on or before its last day.
        return self._library.previous_session(last + _ONE_DAY)

    def latest_completed_session(self, now_utc: datetime, delay: timedelta) -> date | None:
        return _latest_completed(self, now_utc, delay)


@functools.cache
def calendar_for(mic: str) -> ExchangeCalendar:
    """The calendar for an operating MIC; cached, and refuses unknown and segment MICs.

    A MIC with an ``xcal_code`` gets the library calendar (with the weekday fallback past the
    library bound). A MIC without one gets the weekday fallback. Never returns the in-house
    adapter: the scheduler keeps its ``None`` default behind the protocol (spec 3.9).
    """
    info = ExchangeRegistry().get(mic)
    if info.xcal_code is None:
        return WeekdayFallbackCalendar(info)
    return _LibraryWithFallback(info)
