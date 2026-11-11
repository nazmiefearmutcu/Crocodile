"""Unit tests for the low-frequency Scheduled Pull Coordinator and US Market Calendar."""

import datetime
import os
import tempfile

import pytest

from crocodile.core.scheduler.calendar import MARKET_TZ, USMarketCalendar
from crocodile.core.scheduler.coordinator import ScheduledPullCoordinator
from crocodile.core.scheduler.state import InMemoryStateStore, JSONFileStateStore


def test_calendar_holidays() -> None:
    """Test standard US Equity market holidays and observation rules."""
    cal = USMarketCalendar()

    # 2026 Holidays
    # New Year's Day: Jan 1 (Thursday)
    assert cal.is_holiday(datetime.date(2026, 1, 1))

    # MLK Day: Jan 19 (3rd Monday)
    assert cal.is_holiday(datetime.date(2026, 1, 19))
    assert not cal.is_holiday(datetime.date(2026, 1, 12))  # 2nd Monday

    # Presidents' Day: Feb 16 (3rd Monday)
    assert cal.is_holiday(datetime.date(2026, 2, 16))

    # Good Friday: April 3 (Easter is April 5, 2026)
    assert cal.is_holiday(datetime.date(2026, 4, 3))

    # Memorial Day: May 25 (Last Monday in May 2026)
    assert cal.is_holiday(datetime.date(2026, 5, 25))

    # Juneteenth: June 19 (Friday)
    assert cal.is_holiday(datetime.date(2026, 6, 19))

    # Independence Day: July 4 is Saturday, observed July 3
    assert cal.is_holiday(datetime.date(2026, 7, 3))
    assert not cal.is_trading_day(datetime.date(2026, 7, 3))

    # Labor Day: Sept 7 (1st Monday)
    assert cal.is_holiday(datetime.date(2026, 9, 7))

    # Thanksgiving: Nov 26 (4th Thursday)
    assert cal.is_holiday(datetime.date(2026, 11, 26))

    # Christmas Day: Dec 25 (Friday)
    assert cal.is_holiday(datetime.date(2026, 12, 25))


def test_calendar_early_closes() -> None:
    """Test detection of early close days (Black Friday, Christmas Eve, etc.)."""
    cal = USMarketCalendar()

    # Black Friday (day after Thanksgiving) in 2026: Nov 27
    assert cal.is_early_close(datetime.date(2026, 11, 27))
    hours = cal.get_market_hours(datetime.date(2026, 11, 27))
    assert hours is not None
    open_dt, close_dt = hours
    assert open_dt.time() == datetime.time(9, 30)
    assert close_dt.time() == datetime.time(13, 0)

    # Christmas Eve 2026: Dec 24 (Thursday) - early close
    assert cal.is_early_close(datetime.date(2026, 12, 24))

    # Normal trading day hours: Jan 2, 2026 (Friday)
    hours = cal.get_market_hours(datetime.date(2026, 1, 2))
    assert hours is not None
    open_dt, close_dt = hours
    assert open_dt.time() == datetime.time(9, 30)
    assert close_dt.time() == datetime.time(16, 0)


def test_calendar_market_open() -> None:
    """Test checking if the market is open at specific times."""
    cal = USMarketCalendar()

    # Wed, Jan 7, 2026 - normal trading day
    # 9:00 AM ET - pre-market (closed)
    dt_pre = datetime.datetime(2026, 1, 7, 9, 0, tzinfo=MARKET_TZ)
    assert not cal.is_market_open(dt_pre)

    # 10:00 AM ET - open
    dt_open = datetime.datetime(2026, 1, 7, 10, 0, tzinfo=MARKET_TZ)
    assert cal.is_market_open(dt_open)

    # 4:30 PM ET - post-market (closed)
    dt_post = datetime.datetime(2026, 1, 7, 16, 30, tzinfo=MARKET_TZ)
    assert not cal.is_market_open(dt_post)

    # Sunday, Jan 4, 2026 (closed)
    dt_sun = datetime.datetime(2026, 1, 4, 12, 0, tzinfo=MARKET_TZ)
    assert not cal.is_market_open(dt_sun)


def test_json_state_store() -> None:
    """Test serialization/deserialization of JSONFileStateStore."""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = os.path.join(tmpdir, "state.json")
        store = JSONFileStateStore(filepath)

        # Retrieve empty state
        ts, dt = store.get_last_run("test_task")
        assert ts is None
        assert dt is None

        # Write state
        run_ts = datetime.datetime(2026, 6, 21, 12, 0, 0, tzinfo=MARKET_TZ)
        run_dt = datetime.date(2026, 6, 20)
        store.update_last_run("test_task", run_ts, run_dt)

        # Read back from same instance
        ts, dt = store.get_last_run("test_task")
        assert ts == run_ts
        assert dt == run_dt

        # Read back from new instance loading same file
        new_store = JSONFileStateStore(filepath)
        ts, dt = new_store.get_last_run("test_task")
        assert ts == run_ts
        assert dt == run_dt


@pytest.mark.asyncio
async def test_coordinator_simple_run() -> None:
    """Test coordinator executes simple registered tasks."""
    state = InMemoryStateStore()
    coord = ScheduledPullCoordinator(state)

    sync_runs = []
    async_runs = []

    def sync_task(run_date: datetime.date) -> None:
        sync_runs.append(run_date)

    async def async_task(run_date: datetime.date) -> None:
        async_runs.append(run_date)

    # Wed, Jan 7, 2026
    start_time = datetime.datetime(2026, 1, 7, 18, 0, tzinfo=MARKET_TZ)

    coord.register_task(
        "sync_t",
        sync_task,
        interval="daily",
        policy="after_hours",
        start_date=datetime.date(2026, 1, 7),
    )
    coord.register_task(
        "async_t",
        async_task,
        interval="daily",
        policy="after_hours",
        start_date=datetime.date(2026, 1, 7),
    )

    # Check and run
    runs = await coord.check_and_run_once(start_time)
    assert runs == 2
    assert sync_runs == [datetime.date(2026, 1, 7)]
    assert async_runs == [datetime.date(2026, 1, 7)]

    # Check states are updated
    ts, dt = state.get_last_run("sync_t")
    assert ts == start_time
    assert dt == datetime.date(2026, 1, 7)

    # Check and run again, should not run again for the same period
    runs = await coord.check_and_run_once(start_time)
    assert runs == 0
    assert len(sync_runs) == 1


@pytest.mark.asyncio
async def test_coordinator_catch_up() -> None:
    """Test catch-up functionality when multiple periods are missed."""
    state = InMemoryStateStore()
    # max_dates_per_tick=1: this test pins the order in which the watermark walks the missed
    # dates, one per tick. Batching (default 5 per tick) is covered in test_scheduler_calendars.py.
    coord = ScheduledPullCoordinator(state, max_dates_per_tick=1)

    executed_dates = []

    async def my_task(run_date: datetime.date) -> None:
        executed_dates.append(run_date)

    # Mark as last run on Wed, Jan 7, 2026
    last_run_time = datetime.datetime(2026, 1, 7, 18, 0, tzinfo=MARKET_TZ)
    state.update_last_run("catch_up_task", last_run_time, datetime.date(2026, 1, 7))

    coord.register_task(
        "catch_up_task",
        my_task,
        interval="daily",
        policy="after_hours",
    )

    # Current time is Fri, Jan 9, 2026 at 18:00 (Market closed)
    # The scheduler should run for Jan 8 first, and update state
    now_time = datetime.datetime(2026, 1, 9, 18, 0, tzinfo=MARKET_TZ)

    # First check_and_run_once should trigger for Jan 8
    runs = await coord.check_and_run_once(now_time)
    assert runs == 1
    assert executed_dates == [datetime.date(2026, 1, 8)]

    # State should update to Jan 8
    _, dt = state.get_last_run("catch_up_task")
    assert dt == datetime.date(2026, 1, 8)

    # Second check_and_run_once should trigger for Jan 9 (since Jan 9 is now also due)
    runs = await coord.check_and_run_once(now_time)
    assert runs == 1
    assert executed_dates == [datetime.date(2026, 1, 8), datetime.date(2026, 1, 9)]

    # State should update to Jan 9
    _, dt = state.get_last_run("catch_up_task")
    assert dt == datetime.date(2026, 1, 9)

    # Third check_and_run_once should do nothing
    runs = await coord.check_and_run_once(now_time)
    assert runs == 0


@pytest.mark.asyncio
async def test_coordinator_policies() -> None:
    """Test policy filters (anytime, during_hours, after_hours)."""
    state = InMemoryStateStore()
    coord = ScheduledPullCoordinator(state)

    during_ran = False
    after_ran = False

    async def during_task() -> None:
        nonlocal during_ran
        during_ran = True

    async def after_task() -> None:
        nonlocal after_ran
        after_ran = True

    coord.register_task(
        "during",
        during_task,
        interval="daily",
        policy="during_hours",
        target_time=datetime.time(10, 0),
        start_date=datetime.date(2026, 1, 7),
    )
    coord.register_task(
        "after",
        after_task,
        interval="daily",
        policy="after_hours",
        target_time=datetime.time(16, 30),
        start_date=datetime.date(2026, 1, 7),
    )

    # Scenario A: Jan 7, 2026 at 11:00 AM (Market is open)
    # The "during" task is due (time >= 10:00, policy during_hours met).
    # The "after" task is not due (time < 16:30, policy after_hours not met).
    now_during = datetime.datetime(2026, 1, 7, 11, 0, tzinfo=MARKET_TZ)
    runs = await coord.check_and_run_once(now_during)
    assert runs == 1
    assert during_ran
    assert not after_ran

    # Scenario B: Jan 7, 2026 at 5:00 PM (Market is closed)
    # The "after" task is due (time >= 16:30, policy after_hours met).
    now_after = datetime.datetime(2026, 1, 7, 17, 0, tzinfo=MARKET_TZ)
    runs = await coord.check_and_run_once(now_after)
    assert runs == 1
    assert after_ran


@pytest.mark.asyncio
async def test_coordinator_intervals() -> None:
    """Test interval calculations (weekly, semi_monthly, monthly, bi_monthly)."""
    state = InMemoryStateStore()
    # max_dates_per_tick=1: each tick pins one due date of the interval; batching (default 5)
    # is covered in test_scheduler_calendars.py.
    coord = ScheduledPullCoordinator(state, max_dates_per_tick=1)

    weekly_runs = []
    semi_monthly_runs = []
    monthly_runs = []
    bi_monthly_runs = []

    coord.register_task("w", lambda d: weekly_runs.append(d), interval="weekly")
    coord.register_task("s", lambda d: semi_monthly_runs.append(d), interval="semi_monthly")
    coord.register_task("m", lambda d: monthly_runs.append(d), interval="monthly")
    coord.register_task("b", lambda d: bi_monthly_runs.append(d), interval="bi_monthly")

    # Initial runs: Wed, Jan 7, 2026
    t1 = datetime.datetime(2026, 1, 7, 12, 0, tzinfo=MARKET_TZ)
    await coord.check_and_run_once(t1)
    assert weekly_runs == [datetime.date(2026, 1, 7)]
    assert semi_monthly_runs == [datetime.date(2026, 1, 7)]
    assert monthly_runs == [datetime.date(2026, 1, 7)]
    assert bi_monthly_runs == [datetime.date(2026, 1, 7)]

    # Clear run logs for easier verification of next steps
    weekly_runs.clear()
    semi_monthly_runs.clear()
    monthly_runs.clear()
    bi_monthly_runs.clear()

    # Move to Fri, Jan 9, 2026 (same week, same semi-month, same month) - none should run
    t2 = datetime.datetime(2026, 1, 9, 12, 0, tzinfo=MARKET_TZ)
    await coord.check_and_run_once(t2)
    assert not weekly_runs
    assert not semi_monthly_runs
    assert not monthly_runs
    assert not bi_monthly_runs

    # Move to Monday, Jan 12, 2026 (new ISO week, same semi-month, same month) - weekly should run
    t3 = datetime.datetime(2026, 1, 12, 12, 0, tzinfo=MARKET_TZ)
    await coord.check_and_run_once(t3)
    assert weekly_runs == [datetime.date(2026, 1, 12)]
    assert not semi_monthly_runs
    assert not monthly_runs
    assert not bi_monthly_runs
    weekly_runs.clear()

    # Move to Fri, Jan 16, 2026 (same week, new semi-month [>= 15th], same month)
    # semi_monthly should run
    t4 = datetime.datetime(2026, 1, 16, 12, 0, tzinfo=MARKET_TZ)
    await coord.check_and_run_once(t4)
    assert not weekly_runs
    assert semi_monthly_runs == [datetime.date(2026, 1, 15)]
    assert not monthly_runs
    assert not bi_monthly_runs
    semi_monthly_runs.clear()

    # Move to Monday, Feb 2, 2026 (new week, new semi-month [1st], new month,
    # same bi-month [Jan/Feb same period])
    # weekly, semi_monthly, and monthly should run. bi-monthly should NOT.
    t5 = datetime.datetime(2026, 2, 2, 12, 0, tzinfo=MARKET_TZ)
    await coord.check_and_run_once(t5)
    assert weekly_runs == [datetime.date(2026, 1, 20)]
    assert semi_monthly_runs == [datetime.date(2026, 2, 2)]
    assert monthly_runs == [datetime.date(2026, 2, 2)]
    assert not bi_monthly_runs
    weekly_runs.clear()
    semi_monthly_runs.clear()
    monthly_runs.clear()

    # Move to Monday, Mar 2, 2026 (new bi-month [Mar/Apr is new period])
    # bi-monthly should run (along with others)
    t6 = datetime.datetime(2026, 3, 2, 12, 0, tzinfo=MARKET_TZ)
    await coord.check_and_run_once(t6)
    assert weekly_runs == [datetime.date(2026, 1, 26)]
    assert semi_monthly_runs == [datetime.date(2026, 2, 17)]
    assert monthly_runs == [datetime.date(2026, 3, 2)]
    assert bi_monthly_runs == [datetime.date(2026, 3, 2)]


def test_historical_holidays_awareness() -> None:
    """Test that MLK Day is not a holiday before 1998
    and Juneteenth is not a holiday before 2022.
    """
    cal = USMarketCalendar()
    # 1995 MLK Day: 3rd Monday of Jan was Jan 16
    assert not cal.is_holiday(datetime.date(1995, 1, 16))
    # 2020 Juneteenth: June 19
    assert not cal.is_holiday(datetime.date(2020, 6, 19))
    # MLK Day in 2000 (after 1998)
    assert cal.is_holiday(datetime.date(2000, 1, 17))
    # Juneteenth in 2023 (after 2022)
    assert cal.is_holiday(datetime.date(2023, 6, 19))


def test_nth_weekday_of_month_bounds() -> None:
    """Test that nth_weekday_of_month returns None for invalid or out-of-bound inputs."""
    from crocodile.core.scheduler.calendar import nth_weekday_of_month

    # 5th Monday of Jan 2026 (Jan 2026 has 4 Mondays: 5, 12, 19, 26)
    assert nth_weekday_of_month(2026, 1, 0, 5) is None
    # 0-th or negative weekday
    assert nth_weekday_of_month(2026, 1, 0, 0) is None
    assert nth_weekday_of_month(2026, 1, 0, -1) is None


@pytest.mark.asyncio
async def test_coordinator_contradiction_registration() -> None:
    """Test that registering a task with during_hours policy
    and run_on_non_trading_days=True raises ValueError.
    """
    coord = ScheduledPullCoordinator(InMemoryStateStore())
    with pytest.raises(ValueError, match="Logical contradiction"):
        coord.register_task(
            "invalid_task",
            lambda d: None,
            policy="during_hours",
            run_on_non_trading_days=True,
        )


@pytest.mark.asyncio
async def test_coordinator_parameterless_bypass_catchup() -> None:
    """Test that a task not accepting run_date parameter bypasses catchup."""
    state = InMemoryStateStore()
    coord = ScheduledPullCoordinator(state)
    call_count = 0

    async def no_param_task() -> None:
        nonlocal call_count
        call_count += 1

    # Wed, Jan 7, 2026
    start_time = datetime.datetime(2026, 1, 7, 18, 0, tzinfo=MARKET_TZ)
    coord.register_task(
        "no_param",
        no_param_task,
        interval="daily",
        policy="after_hours",
        start_date=datetime.date(2026, 1, 5),  # 2 days in the past
    )

    # First check: should run once for Jan 7, bypassing historical catch-up for Jan 5, 6
    runs = await coord.check_and_run_once(start_time)
    assert runs == 1
    assert call_count == 1

    # State updated to Jan 7
    _, dt = state.get_last_run("no_param")
    assert dt == datetime.date(2026, 1, 7)


@pytest.mark.asyncio
async def test_coordinator_first_run_full_catchup() -> None:
    """Test that a newly registered task catches up day-by-day starting from start_date forward."""
    state = InMemoryStateStore()
    # max_dates_per_tick=1: the test asserts one catch-up date per tick; the default batch of 5
    # is covered in test_scheduler_calendars.py.
    coord = ScheduledPullCoordinator(state, max_dates_per_tick=1)
    executed_dates = []

    def task_with_date(run_date: datetime.date) -> None:
        executed_dates.append(run_date)

    coord.register_task(
        "catch_up",
        task_with_date,
        interval="daily",
        policy="anytime",
        start_date=datetime.date(2026, 1, 5),  # Monday
    )

    # Current time is Wed, Jan 7, 2026 at 12:00
    now_time = datetime.datetime(2026, 1, 7, 12, 0, tzinfo=MARKET_TZ)

    # First run: should execute oldest missed date (Jan 5)
    runs = await coord.check_and_run_once(now_time)
    assert runs == 1
    assert executed_dates == [datetime.date(2026, 1, 5)]

    # Second run: should execute next missed date (Jan 6)
    runs = await coord.check_and_run_once(now_time)
    assert runs == 1
    assert executed_dates == [datetime.date(2026, 1, 5), datetime.date(2026, 1, 6)]

    # Third run: should execute current date (Jan 7)
    runs = await coord.check_and_run_once(now_time)
    assert runs == 1
    assert executed_dates == [
        datetime.date(2026, 1, 5),
        datetime.date(2026, 1, 6),
        datetime.date(2026, 1, 7),
    ]


@pytest.mark.asyncio
async def test_coordinator_task_retry_and_cooldown() -> None:
    """Test task failure cooldown (5 min) and max retry limit (3)."""
    state = InMemoryStateStore()
    coord = ScheduledPullCoordinator(state)
    run_count = 0

    def failing_task(run_date: datetime.date) -> None:
        nonlocal run_count
        run_count += 1
        raise RuntimeError("Task failed")

    coord.register_task("failing", failing_task, interval="daily", policy="anytime")

    now = datetime.datetime(2026, 1, 7, 12, 0, tzinfo=MARKET_TZ)

    # 1st try: runs and fails
    runs = await coord.check_and_run_once(now)
    assert runs == 0
    assert run_count == 1

    # Immediate check (10s later): should skip due to 5-min cooldown
    runs = await coord.check_and_run_once(now + datetime.timedelta(seconds=10))
    assert runs == 0
    assert run_count == 1

    # Check after 5 minutes (301s later): runs and fails (2nd try)
    runs = await coord.check_and_run_once(now + datetime.timedelta(seconds=301))
    assert runs == 0
    assert run_count == 2

    # Check after another 5 minutes (602s later): runs and fails (3rd try)
    runs = await coord.check_and_run_once(now + datetime.timedelta(seconds=602))
    assert runs == 0
    assert run_count == 3

    # The third failure reached max_failures: Jan 7 is recorded as a gap and the watermark
    # moved past it (spec 3.9), so nothing is due any more. Previously the date was skipped
    # with no record; now it is a GapEntry in the state store.
    assert [g.logical_date for g in coord.gaps("failing")] == [datetime.date(2026, 1, 7)]
    assert state.get_last_run("failing")[1] == datetime.date(2026, 1, 7)

    # Check after another 5 minutes (903s later): nothing is due (the gap stepped past Jan 7)
    runs = await coord.check_and_run_once(now + datetime.timedelta(seconds=903))
    assert runs == 0
    assert run_count == 3
