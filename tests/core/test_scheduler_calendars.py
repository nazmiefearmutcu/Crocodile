"""Calendar-driven scheduler tasks: exchange-local logical dates, close plus delay, batching."""

import dataclasses
import datetime
import logging
import subprocess
import sys
from collections.abc import Callable
from zoneinfo import ZoneInfo

import pytest

from crocodile.core.markets.calendar import AlwaysOpenCalendar, calendar_for
from crocodile.core.scheduler.calendar import MARKET_TZ
from crocodile.core.scheduler.coordinator import ScheduledPullCoordinator
from crocodile.core.scheduler.state import InMemoryStateStore
from tests.core.scheduler_doubles import NYSE_LIKE, NZX, RaisingCalendar

UTC = datetime.UTC


def _utc(*args: int) -> datetime.datetime:
    return datetime.datetime(*args, tzinfo=UTC)


def _recorder() -> tuple[list[datetime.date], Callable[[datetime.date], None]]:
    runs: list[datetime.date] = []

    def job(run_date: datetime.date) -> None:
        runs.append(run_date)

    return runs, job


async def test_the_logical_date_is_the_exchange_local_session_date_not_the_eastern_date() -> None:
    # 16:45 NZDT close on 2026-01-07 is 03:45Z; with the 45 min delay it is due at 04:30Z.
    eastern_label = datetime.datetime(2026, 1, 7, 4, 31, tzinfo=UTC).astimezone(MARKET_TZ).date()
    assert eastern_label == datetime.date(2026, 1, 6)  # the label the old Eastern clock gave

    runs, job = _recorder()
    coord = ScheduledPullCoordinator(InMemoryStateStore())
    coord.register_task("nz", job, calendar=NZX, start_date=datetime.date(2026, 1, 7))
    assert await coord.check_and_run_once(_utc(2026, 1, 7, 4, 31)) == 1
    assert runs == [datetime.date(2026, 1, 7)]


async def test_a_calendar_task_is_due_only_after_close_plus_delay() -> None:
    runs, job = _recorder()
    coord = ScheduledPullCoordinator(InMemoryStateStore())
    coord.register_task("nz", job, calendar=NZX, start_date=datetime.date(2026, 1, 6))

    await coord.check_and_run_once(_utc(2026, 1, 7, 4, 29))
    assert runs == [datetime.date(2026, 1, 6)]

    await coord.check_and_run_once(_utc(2026, 1, 7, 4, 31))
    assert runs == [datetime.date(2026, 1, 6), datetime.date(2026, 1, 7)]

    assert await coord.check_and_run_once(_utc(2026, 1, 7, 4, 40)) == 0
    assert len(runs) == 2


async def test_a_custom_delay_moves_the_due_instant() -> None:
    runs, job = _recorder()
    coord = ScheduledPullCoordinator(InMemoryStateStore())
    coord.register_task(
        "nz",
        job,
        calendar=NZX,
        delay=datetime.timedelta(hours=2),
        start_date=datetime.date(2026, 1, 7),
    )

    await coord.check_and_run_once(_utc(2026, 1, 7, 4, 31))
    assert runs == []  # the latest session that completed by then is Jan 6, before the start

    await coord.check_and_run_once(_utc(2026, 1, 7, 5, 46))
    assert runs == [datetime.date(2026, 1, 7)]


async def test_the_real_nzx_calendar_labels_its_session_by_auckland_date() -> None:
    runs, job = _recorder()
    coord = ScheduledPullCoordinator(InMemoryStateStore())
    coord.register_task(
        "nz", job, calendar=calendar_for("XNZE"), start_date=datetime.date(2026, 1, 7)
    )

    # Generous instants on purpose: the test does not hard-code the library's exact close.
    await coord.check_and_run_once(_utc(2026, 1, 7, 2, 0))
    assert runs == []
    await coord.check_and_run_once(_utc(2026, 1, 7, 6, 0))
    assert runs == [datetime.date(2026, 1, 7)]


async def test_sessions_skip_the_calendars_weekend_and_holidays() -> None:
    calendar = dataclasses.replace(NYSE_LIKE, holidays=frozenset({datetime.date(2026, 1, 8)}))
    runs, job = _recorder()
    coord = ScheduledPullCoordinator(InMemoryStateStore(), max_dates_per_tick=10)
    coord.register_task("us", job, calendar=calendar, start_date=datetime.date(2026, 1, 6))

    await coord.check_and_run_once(_utc(2026, 1, 13, 22, 0))

    assert runs == [
        datetime.date(2026, 1, 6),
        datetime.date(2026, 1, 7),
        datetime.date(2026, 1, 9),
        datetime.date(2026, 1, 12),
        datetime.date(2026, 1, 13),
    ]


async def test_catch_up_runs_at_most_max_dates_per_tick() -> None:
    runs, job = _recorder()
    coord = ScheduledPullCoordinator(InMemoryStateStore())
    coord.register_task("us", job, calendar=NYSE_LIKE, start_date=datetime.date(2026, 1, 5))
    now = _utc(2026, 1, 16, 22, 0)

    assert await coord.check_and_run_once(now) == 5
    assert await coord.check_and_run_once(now) == 5
    assert await coord.check_and_run_once(now) == 0
    assert runs == [
        datetime.date(2026, 1, 5),
        datetime.date(2026, 1, 6),
        datetime.date(2026, 1, 7),
        datetime.date(2026, 1, 8),
        datetime.date(2026, 1, 9),
        datetime.date(2026, 1, 12),
        datetime.date(2026, 1, 13),
        datetime.date(2026, 1, 14),
        datetime.date(2026, 1, 15),
        datetime.date(2026, 1, 16),
    ]


async def test_max_dates_per_tick_is_configurable() -> None:
    runs, job = _recorder()
    coord = ScheduledPullCoordinator(InMemoryStateStore(), max_dates_per_tick=3)
    coord.register_task("us", job, calendar=NYSE_LIKE, start_date=datetime.date(2026, 1, 5))
    now = _utc(2026, 1, 16, 22, 0)

    counts = [await coord.check_and_run_once(now) for _ in range(5)]

    assert counts == [3, 3, 3, 1, 0]
    assert len(runs) == 10


async def test_a_crypto_task_on_the_always_open_calendar_runs_on_weekends() -> None:
    runs, job = _recorder()
    coord = ScheduledPullCoordinator(InMemoryStateStore())
    coord.register_task(
        "crypto", job, calendar=AlwaysOpenCalendar(), start_date=datetime.date(2026, 1, 9)
    )

    await coord.check_and_run_once(_utc(2026, 1, 10, 0, 46))
    assert runs == [datetime.date(2026, 1, 9)]
    await coord.check_and_run_once(_utc(2026, 1, 11, 0, 46))  # Saturday session
    assert runs[-1] == datetime.date(2026, 1, 10)
    await coord.check_and_run_once(_utc(2026, 1, 12, 0, 46))  # Sunday session
    assert runs[-1] == datetime.date(2026, 1, 11)


async def test_a_task_without_a_calendar_keeps_the_us_eastern_clock() -> None:
    runs, job = _recorder()
    coord = ScheduledPullCoordinator(InMemoryStateStore())
    coord.register_task("legacy", job)

    await coord.check_and_run_once(_utc(2026, 1, 7, 4, 31))

    assert runs == [datetime.date(2026, 1, 6)]  # the Eastern date, as before this task


async def test_a_task_without_a_date_parameter_runs_once_for_the_latest_session() -> None:
    runs: list[str] = []

    def job() -> None:
        runs.append("ran")

    coord = ScheduledPullCoordinator(InMemoryStateStore())
    coord.register_task("nz", job, calendar=NZX)

    assert await coord.check_and_run_once(_utc(2026, 1, 9, 6, 0)) == 1
    assert coord.state_store.get_last_run("nz")[1] == datetime.date(2026, 1, 9)
    assert await coord.check_and_run_once(_utc(2026, 1, 9, 6, 0)) == 0
    assert runs == ["ran"]


async def test_weekly_interval_runs_on_the_first_session_of_each_week() -> None:
    calendar = dataclasses.replace(NYSE_LIKE, holidays=frozenset({datetime.date(2026, 1, 19)}))
    runs, job = _recorder()
    coord = ScheduledPullCoordinator(InMemoryStateStore(), max_dates_per_tick=10)
    coord.register_task(
        "weekly",
        job,
        interval="weekly",
        calendar=calendar,
        start_date=datetime.date(2026, 1, 7),
    )

    await coord.check_and_run_once(_utc(2026, 1, 21, 22, 0))

    # The Monday holiday moves that week's first session to Tuesday.
    assert runs == [
        datetime.date(2026, 1, 7),
        datetime.date(2026, 1, 12),
        datetime.date(2026, 1, 20),
    ]


@pytest.mark.parametrize(
    ("kwargs", "field_name"),
    [
        ({"policy": "during_hours"}, "policy"),
        ({"target_time": datetime.time(16, 0)}, "target_time"),
        ({"run_on_non_trading_days": True}, "run_on_non_trading_days"),
    ],
)
def test_a_calendar_task_refuses_conflicting_options(
    kwargs: dict[str, object], field_name: str
) -> None:
    coord = ScheduledPullCoordinator(InMemoryStateStore())

    with pytest.raises(ValueError, match=field_name):
        coord.register_task("nz", lambda d: None, calendar=NZX, **kwargs)  # type: ignore[arg-type]


def test_a_negative_delay_is_refused() -> None:
    coord = ScheduledPullCoordinator(InMemoryStateStore())

    with pytest.raises(ValueError, match="delay"):
        coord.register_task("nz", lambda d: None, calendar=NZX, delay=-datetime.timedelta(1))


async def test_a_calendar_task_accepts_the_after_hours_policy() -> None:
    runs, job = _recorder()
    coord = ScheduledPullCoordinator(InMemoryStateStore())
    coord.register_task(
        "eod:XNYS",
        job,
        interval="daily",
        policy="after_hours",
        calendar=NYSE_LIKE,
        start_date=datetime.date(2026, 1, 7),
    )

    await coord.check_and_run_once(_utc(2026, 1, 7, 22, 0))

    assert runs == [datetime.date(2026, 1, 7)]


def test_the_coordinator_refuses_non_positive_limits() -> None:
    store = InMemoryStateStore()
    with pytest.raises(ValueError):
        ScheduledPullCoordinator(store, max_failures=0)
    with pytest.raises(ValueError):
        ScheduledPullCoordinator(store, max_dates_per_tick=0)
    with pytest.raises(ValueError):
        ScheduledPullCoordinator(store, retry_cooldown=datetime.timedelta(seconds=-1))


async def test_a_calendar_that_cannot_answer_does_not_starve_the_other_tasks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken_runs, broken_job = _recorder()
    healthy_runs, healthy_job = _recorder()
    coord = ScheduledPullCoordinator(InMemoryStateStore())
    coord.register_task(
        "broken",
        broken_job,
        calendar=RaisingCalendar("XBAD", ZoneInfo("UTC"), datetime.time(16, 0)),
        start_date=datetime.date(2026, 1, 7),
    )
    coord.register_task(
        "healthy", healthy_job, calendar=NYSE_LIKE, start_date=datetime.date(2026, 1, 7)
    )

    with caplog.at_level(logging.ERROR, logger="crocodile.scheduler"):
        count = await coord.check_and_run_once(_utc(2026, 1, 7, 22, 0))

    assert count == 1
    assert healthy_runs == [datetime.date(2026, 1, 7)]
    assert broken_runs == []
    assert any("broken" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    "order",
    [
        ("crocodile.core.markets.calendar", "crocodile.core.scheduler"),
        ("crocodile.core.scheduler", "crocodile.core.markets.calendar"),
    ],
)
def test_importing_the_calendar_module_first_does_not_cycle(order: tuple[str, str]) -> None:
    first, second = order
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}; import {second}"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
