"""Coordinator for low-frequency scheduled pulls (daily, weekly, bi-monthly, etc.).

Two clocks coexist (spec 3.9):

- A task without a calendar keeps the US Eastern clock (``USMarketCalendar``): its logical
  date is the Eastern date of the tick and it is due at its target time, as before.
- A task with an ``ExchangeCalendar`` has logical dates that are the calendar's local session
  dates, and it is due once the session has closed for ``delay`` (default 45 minutes).
  Crypto tasks pass ``AlwaysOpenCalendar``.

Failures are counted per (task, logical date) in the state store, so a restart keeps them.
After ``max_failures`` the date is recorded as a :class:`GapEntry` (``no_data`` when the
calendar answers the date through its weekday fallback) and the watermark moves past it, so
one bad date cannot wedge a task. Each tick runs at most ``max_dates_per_tick`` attempts per
task, and a task that raises is logged without stopping the other tasks.

Rulings, with the cost if each is wrong:

- Ruling: batching and the persistent failure counter apply to calendar-less tasks too;
  ``calendar=None`` keeps only the US clock (spec 3.9). Cost if wrong: a caller relying on one
  date per tick sees up to five; it passes ``max_dates_per_tick=1``.
- Ruling: the gap entry is written on the failure that reaches ``max_failures``, and the loop
  continues with the next date in the same tick. Cost if wrong: one more date per tick than a
  "skip until next tick" reading; still bounded by ``max_dates_per_tick``.
- Ruling: a gap decides its kind on ``quality_for(logical date)``, never on ``quality``.
  Cost if wrong: a miss past a library bound is recorded as a real gap instead of ``no_data``.
"""

import asyncio
import datetime
import inspect
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

from crocodile.core.scheduler.calendar import MARKET_TZ, USMarketCalendar
from crocodile.core.scheduler.state import GapEntry, GapKind, SchedulerStateStore

if TYPE_CHECKING:
    from crocodile.core.markets.calendar import ExchangeCalendar

logger = logging.getLogger("crocodile.scheduler")

# Spec 3.9: a session's data is due this long after its close.
DEFAULT_CLOSE_DELAY: Final[datetime.timedelta] = datetime.timedelta(minutes=45)
DEFAULT_MAX_FAILURES: Final[int] = 3
DEFAULT_MAX_DATES_PER_TICK: Final[int] = 5
DEFAULT_RETRY_COOLDOWN: Final[datetime.timedelta] = datetime.timedelta(seconds=300)

_ONE_DAY: Final[datetime.timedelta] = datetime.timedelta(days=1)
_ZERO: Final[datetime.timedelta] = datetime.timedelta(0)


class PullTask:
    """Represents a scheduled pull task configuration."""

    name: str
    func: Callable[..., Any]
    interval: str
    policy: str
    run_on_non_trading_days: bool
    target_time: datetime.time
    start_date: datetime.date | None
    accepts_date: bool
    calendar: "ExchangeCalendar | None"
    delay: datetime.timedelta

    def __init__(
        self,
        name: str,
        func: Callable[..., Any],
        interval: str = "daily",
        policy: str = "anytime",
        run_on_non_trading_days: bool = False,
        target_time: datetime.time | None = None,
        start_date: datetime.date | None = None,
        *,
        calendar: "ExchangeCalendar | None" = None,
        delay: datetime.timedelta = DEFAULT_CLOSE_DELAY,
    ) -> None:
        self.name = name
        self.func = func
        self.interval = interval
        self.policy = policy
        self.run_on_non_trading_days = run_on_non_trading_days
        self.calendar = calendar
        self.delay = delay

        # Determine default target time based on policy if not specified
        if target_time is None:
            if policy == "after_hours":
                self.target_time = datetime.time(16, 30)
            elif policy == "during_hours":
                self.target_time = datetime.time(9, 30)
            else:
                self.target_time = datetime.time(0, 0)
        else:
            self.target_time = target_time

        self.start_date = start_date

        # Check if function accepts a date parameter
        sig = inspect.signature(func)
        try:
            sig.bind(datetime.date.today())
            self.accepts_date = True
        except TypeError:
            self.accepts_date = False


class ScheduledPullCoordinator:
    """Coordinates low-frequency data pulls, respecting trading hours and calendars.

    ``tasks`` maps task names to :class:`PullTask` and is part of the public surface: the
    operator ``schedule`` command inspects it.
    """

    def __init__(
        self,
        state_store: SchedulerStateStore,
        calendar: USMarketCalendar | None = None,
        *,
        max_failures: int = DEFAULT_MAX_FAILURES,
        max_dates_per_tick: int = DEFAULT_MAX_DATES_PER_TICK,
        retry_cooldown: datetime.timedelta = DEFAULT_RETRY_COOLDOWN,
    ) -> None:
        if max_failures < 1:
            raise ValueError(f"max_failures must be at least 1, got {max_failures}")
        if max_dates_per_tick < 1:
            raise ValueError(f"max_dates_per_tick must be at least 1, got {max_dates_per_tick}")
        if retry_cooldown < _ZERO:
            raise ValueError(f"retry_cooldown must not be negative, got {retry_cooldown}")
        self.state_store = state_store
        self.calendar = calendar or USMarketCalendar()
        self.max_failures = max_failures
        self.max_dates_per_tick = max_dates_per_tick
        self.retry_cooldown = retry_cooldown
        self.tasks: dict[str, PullTask] = {}

    def register_task(
        self,
        name: str,
        func: Callable[..., Any],
        interval: str = "daily",
        policy: str = "anytime",
        run_on_non_trading_days: bool = False,
        target_time: datetime.time | None = None,
        start_date: datetime.date | None = None,
        *,
        calendar: "ExchangeCalendar | None" = None,
        delay: datetime.timedelta = DEFAULT_CLOSE_DELAY,
    ) -> None:
        """Register a new task with the coordinator.

        A task with a ``calendar`` takes its logical dates from that calendar and is due
        ``delay`` after each session closes. Such a task accepts ``policy="anytime"`` or
        ``"after_hours"`` (both mean "due at close plus delay"); ``"during_hours"``, a
        ``target_time`` and ``run_on_non_trading_days`` are refused, because the calendar's
        sessions and closes already decide those.
        """
        if name in self.tasks:
            raise ValueError(f"Task with name '{name}' is already registered.")

        valid_intervals = {"daily", "weekly", "semi_monthly", "monthly", "bi_monthly"}
        if interval not in valid_intervals:
            raise ValueError(f"Invalid interval '{interval}'. Must be one of {valid_intervals}")

        valid_policies = {"anytime", "during_hours", "after_hours"}
        if policy not in valid_policies:
            raise ValueError(f"Invalid policy '{policy}'. Must be one of {valid_policies}")

        if run_on_non_trading_days and policy == "during_hours":
            raise ValueError(
                "Logical contradiction: run_on_non_trading_days cannot be True "
                "when policy is 'during_hours', as markets are closed on non-trading days."
            )

        if delay < _ZERO:
            raise ValueError(f"delay must not be negative, got {delay}")

        if calendar is not None:
            if policy == "during_hours":
                raise ValueError(
                    f"Task '{name}' has a calendar and cannot use policy 'during_hours': "
                    "its runs are due after the session closes."
                )
            if target_time is not None:
                raise ValueError(
                    f"Task '{name}' has a calendar and cannot set target_time: "
                    "the calendar's session close is its target."
                )
            if run_on_non_trading_days:
                raise ValueError(
                    f"Task '{name}' has a calendar and cannot set run_on_non_trading_days: "
                    "its logical dates are the calendar's sessions."
                )

        self.tasks[name] = PullTask(
            name=name,
            func=func,
            interval=interval,
            policy=policy,
            run_on_non_trading_days=run_on_non_trading_days,
            target_time=target_time,
            start_date=start_date,
            calendar=calendar,
            delay=delay,
        )
        logger.info(
            f"Registered scheduled task: {name} (interval={interval}, "
            f"policy={policy}, target_time={target_time})"
        )
        if not self.tasks[name].accepts_date:
            logger.warning(
                f"Task '{name}' does not accept a date parameter. "
                "Bypassing historical catch-up; this task will only execute for the current period."
            )

    def _is_new_period(
        self, interval: str, prev_date: datetime.date, next_date: datetime.date
    ) -> bool:
        """Determine if a new scheduling period has started between two dates."""
        if interval == "daily":
            return next_date > prev_date

        if interval == "weekly":
            return next_date.isocalendar()[:2] != prev_date.isocalendar()[:2]

        if interval == "semi_monthly":
            prev_period = (
                prev_date.year,
                prev_date.month,
                1 if prev_date.day < 15 else 2,
            )
            next_period = (
                next_date.year,
                next_date.month,
                1 if next_date.day < 15 else 2,
            )
            return next_period != prev_period

        if interval == "monthly":
            return (next_date.year, next_date.month) != (
                prev_date.year,
                prev_date.month,
            )

        if interval == "bi_monthly":
            prev_bi = (prev_date.year, (prev_date.month - 1) // 2)
            next_bi = (next_date.year, (next_date.month - 1) // 2)
            return next_bi != prev_bi

        return False

    def get_next_due_date(self, task: PullTask, now: datetime.datetime) -> datetime.date | None:
        """Calculate the next date for which the task is due to run.

        ``now`` is an aware instant. A calendar-less task reads it on the US Eastern clock; a
        calendar task reads it through its calendar. Returns None if the task is not due.
        """
        if task.calendar is not None:
            return self._next_session_due(task, task.calendar, now)

        now_et = now.astimezone(MARKET_TZ)
        current_date = now_et.date()
        _, last_run_date = self.state_store.get_last_run(task.name)

        if not task.accepts_date:
            # Bypass catch-up: only check the current date/period.
            # If it already ran in the current period, it is not due.
            if last_run_date is not None and not self._is_new_period(
                task.interval, last_run_date, current_date
            ):
                return None
            if task.run_on_non_trading_days or self.calendar.is_trading_day(current_date):
                if task.start_date is None or current_date >= task.start_date:
                    if now_et.time() >= task.target_time:
                        return current_date
            return None

        if last_run_date is None:
            # If the task has never run, determine the starting reference point
            start_ref = task.start_date if task.start_date is not None else current_date
            # Check date-by-date forward to find the oldest valid run target
            d = start_ref
            while d <= current_date:
                if task.run_on_non_trading_days or self.calendar.is_trading_day(d):
                    if d < current_date:
                        return d
                    if d == current_date:
                        if now_et.time() >= task.target_time:
                            return d
                d += _ONE_DAY
            return None

        # Find the next period date to run
        d = last_run_date + _ONE_DAY
        while d <= current_date:
            if task.run_on_non_trading_days or self.calendar.is_trading_day(d):
                if self._is_new_period(task.interval, last_run_date, d):
                    if d < current_date:
                        return d
                    if d == current_date:
                        if now_et.time() >= task.target_time:
                            return d
            d += _ONE_DAY

        return None

    def _next_session_due(
        self, task: PullTask, calendar: "ExchangeCalendar", now: datetime.datetime
    ) -> datetime.date | None:
        """The next logical session date of a calendar task, or None if nothing is due.

        Only sessions whose close plus delay has passed count (``latest_completed_session``).
        """
        latest = calendar.latest_completed_session(now.astimezone(datetime.UTC), task.delay)
        if latest is None:
            return None
        _, last_run_date = self.state_store.get_last_run(task.name)

        if not task.accepts_date:
            # Without a date parameter the task runs once for the latest completed session.
            if last_run_date is not None and not self._is_new_period(
                task.interval, last_run_date, latest
            ):
                return None
            if task.start_date is not None and latest < task.start_date:
                return None
            return latest

        if last_run_date is None and task.start_date is None:
            # No historical backlog by default, as for calendar-less tasks.
            return latest

        start = task.start_date if last_run_date is None else last_run_date + _ONE_DAY
        if start is None or start > latest:
            return None
        for session in calendar.sessions(start, latest):
            if last_run_date is None or self._is_new_period(task.interval, last_run_date, session):
                return session
        return None

    def _gap_kind(self, task: PullTask, run_date: datetime.date) -> GapKind:
        """``no_data`` when the calendar answers ``run_date`` through its weekday fallback.

        Decided on ``quality_for(run_date)``: a library calendar past its bound answers the
        date through the fallback, which knows no holidays (spec 3.8).
        """
        from crocodile.core.markets.calendar import CalendarQuality

        if task.calendar is not None and (
            task.calendar.quality_for(run_date) is CalendarQuality.WEEKDAY_FALLBACK
        ):
            return "no_data"
        return "gap"

    async def _execute_task(self, task: PullTask, run_date: datetime.date) -> None:
        """Execute a task function, resolving sync/async and parameters."""
        is_async = asyncio.iscoroutinefunction(task.func) or (
            callable(task.func)
            and asyncio.iscoroutinefunction(getattr(task.func, "__call__", None))  # noqa: B004
        )

        if is_async:
            if task.accepts_date:
                await task.func(run_date)
            else:
                await task.func()
        else:
            loop = asyncio.get_running_loop()
            if task.accepts_date:
                await loop.run_in_executor(None, task.func, run_date)
            else:
                await loop.run_in_executor(None, task.func)

    def _policy_allows(
        self, task: PullTask, now_et: datetime.datetime, run_date: datetime.date
    ) -> bool:
        """The calendar-less policy checks of the US clock (calendar tasks skip them)."""
        if task.policy == "during_hours":
            if not self.calendar.is_market_open(now_et):
                logger.debug(f"Task '{task.name}' is due for {run_date} but market is closed.")
                return False
        elif task.policy == "after_hours":
            if self.calendar.is_market_open(now_et):
                logger.debug(f"Task '{task.name}' is due for {run_date} but market is open.")
                return False
        return True

    async def _run_task(
        self, task: PullTask, now: datetime.datetime, now_et: datetime.datetime
    ) -> int:
        """Run up to ``max_dates_per_tick`` attempts of one task; returns the successes."""
        succeeded = 0
        for _ in range(self.max_dates_per_tick):
            run_date = self.get_next_due_date(task, now)
            if run_date is None:
                break

            if task.calendar is None and not self._policy_allows(task, now_et, run_date):
                break

            failure = self.state_store.get_failure(task.name, run_date)
            if failure is not None and now - failure.last_failure < self.retry_cooldown:
                logger.debug(
                    f"Task '{task.name}' for logical date {run_date} is in cool-down "
                    f"after {failure.count} failure(s)."
                )
                break

            # Calendar-less tasks keep the Eastern stamps they stored before; calendar tasks
            # stamp the UTC instant.
            stamp = now_et if task.calendar is None else now
            logger.info(f"Executing task '{task.name}' for logical date: {run_date}")
            try:
                await self._execute_task(task, run_date)
            except Exception as exc:
                logger.exception(
                    f"Error executing task '{task.name}' for logical date {run_date}: {exc}"
                )
                record = self.state_store.record_failure(
                    task.name, run_date, stamp, f"{type(exc).__name__}: {exc}"
                )
                if record.count < self.max_failures:
                    break
                self.state_store.record_gap(
                    GapEntry(
                        task=task.name,
                        logical_date=run_date,
                        reason=record.last_error,
                        failures=record.count,
                        recorded_at=stamp,
                        kind=self._gap_kind(task, run_date),
                    )
                )
                self.state_store.update_last_run(task.name, stamp, run_date)
                self.state_store.clear_failure(task.name, run_date)
                logger.warning(
                    f"Task '{task.name}' failed {record.count} times for logical date "
                    f"{run_date}; recorded as a gap and stepped past."
                )
                continue

            self.state_store.update_last_run(task.name, stamp, run_date)
            self.state_store.clear_failure(task.name, run_date)
            succeeded += 1
            logger.info(f"Successfully completed task '{task.name}' for {run_date}")

        return succeeded

    async def check_and_run_once(self, current_time: datetime.datetime | None = None) -> int:
        """Scan all registered tasks, check due status, and execute due tasks.

        Returns the number of (task, logical date) executions that succeeded this tick.
        A task that raises while being evaluated is logged and the others still run.
        """
        now = (current_time or datetime.datetime.now(datetime.UTC)).astimezone(datetime.UTC)
        now_et = now.astimezone(MARKET_TZ)
        executed_count = 0

        for task in list(self.tasks.values()):
            try:
                executed_count += await self._run_task(task, now, now_et)
            except Exception:
                logger.exception(
                    f"Scheduled task '{task.name}' could not be evaluated; other tasks still run."
                )

        return executed_count

    def gaps(self, task_name: str | None = None) -> list[GapEntry]:
        """Gap entries recorded for one task, or for every task."""
        return self.state_store.gaps(task_name)

    async def start(self, interval_seconds: float = 60.0) -> None:
        """Run the scheduling coordinator loop continuously."""
        logger.info("Starting scheduled pull coordinator loop.")
        try:
            while True:
                try:
                    await self.check_and_run_once()
                except Exception as e:
                    logger.exception(
                        f"Unhandled error in scheduled pull coordinator loop iteration: {e}"
                    )
                await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("Scheduled pull coordinator loop cancelled.")
            raise
