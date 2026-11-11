"""Persistent failure counts and gap entries in the scheduler state store (spec 3.9)."""

import dataclasses
import datetime
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from crocodile.core.markets.calendar import CalendarQuality
from crocodile.core.scheduler.coordinator import ScheduledPullCoordinator
from crocodile.core.scheduler.state import (
    FailureRecord,
    GapEntry,
    InMemoryStateStore,
    JSONFileStateStore,
    SchedulerStateStore,
)
from tests.core.scheduler_doubles import NYSE_LIKE

UTC = datetime.UTC
JAN6 = datetime.date(2026, 1, 6)
JAN7 = datetime.date(2026, 1, 7)
T0 = datetime.datetime(2026, 1, 7, 22, 0, tzinfo=UTC)


def _gap(task: str, logical_date: datetime.date, *, kind: str = "gap") -> GapEntry:
    return GapEntry(
        task=task,
        logical_date=logical_date,
        reason="RuntimeError: feed down",
        failures=3,
        recorded_at=T0,
        kind=kind,  # type: ignore[arg-type]
    )


def test_in_memory_store_counts_failures_per_task_and_date() -> None:
    store = InMemoryStateStore()
    first = store.record_failure("t", JAN6, T0, "RuntimeError: a")
    second = store.record_failure("t", JAN6, T0, "ValueError: b")

    assert isinstance(first, FailureRecord)
    assert first.count == 1
    assert second.count == 2
    assert second.last_error == "ValueError: b"
    assert store.get_failure("t", JAN6) == second
    assert store.get_failure("t", JAN7) is None
    assert store.get_failure("u", JAN6) is None

    store.clear_failure("t", JAN6)
    assert store.get_failure("t", JAN6) is None


def test_json_store_persists_failures_and_gaps_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = JSONFileStateStore(str(path))
    store.record_failure("t", JAN6, T0, "RuntimeError: feed down")
    store.record_failure("t", JAN6, T0 + datetime.timedelta(minutes=5), "RuntimeError: again")
    gap = _gap("t", JAN6)
    store.record_gap(gap)

    reopened = JSONFileStateStore(str(path))
    failure = reopened.get_failure("t", JAN6)
    assert failure is not None
    assert failure.count == 2
    assert failure.last_failure == T0 + datetime.timedelta(minutes=5)
    assert failure.last_failure.tzinfo is not None
    assert failure.last_error == "RuntimeError: again"
    assert reopened.gaps("t") == [gap]


def test_update_last_run_keeps_failures_and_gaps(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = JSONFileStateStore(str(path))
    store.record_failure("t", JAN6, T0, "RuntimeError: feed down")
    store.record_gap(_gap("t", JAN7, kind="no_data"))
    store.update_last_run("t", T0, JAN6)

    reopened = JSONFileStateStore(str(path))
    assert reopened.get_last_run("t") == (T0, JAN6)
    assert reopened.get_failure("t", JAN6) is not None
    assert reopened.gaps("t") == [_gap("t", JAN7, kind="no_data")]


def test_a_state_file_written_before_failures_were_persisted_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    old_record = {
        "t": {
            "last_run_timestamp": "2026-01-07T18:00:00-05:00",
            "last_run_date": "2026-01-07",
        }
    }
    path.write_text(json.dumps(old_record), encoding="utf-8")

    store = JSONFileStateStore(str(path))
    last_run = store.get_last_run("t")
    assert last_run[1] == JAN7
    assert store.get_failure("t", JAN6) is None
    assert store.gaps() == []

    store.record_failure("t", JAN6, T0, "RuntimeError: x")
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["t"]["last_run_date"] == "2026-01-07"
    assert written["t"]["last_run_timestamp"] == "2026-01-07T18:00:00-05:00"
    assert JSONFileStateStore(str(path)).get_failure("t", JAN6) is not None


def test_gaps_are_listed_per_task_and_in_date_order() -> None:
    store = InMemoryStateStore()
    store.record_gap(_gap("b", JAN7))
    store.record_gap(_gap("a", JAN7))
    store.record_gap(_gap("a", JAN6))

    assert [(g.task, g.logical_date) for g in store.gaps()] == [
        ("a", JAN6),
        ("a", JAN7),
        ("b", JAN7),
    ]
    assert [g.logical_date for g in store.gaps("a")] == [JAN6, JAN7]
    assert all(g.task == "a" for g in store.gaps("a"))


def test_a_store_missing_the_new_methods_cannot_be_instantiated() -> None:
    class OldStore(SchedulerStateStore):
        def get_last_run(self, task_name: str):  # type: ignore[no-untyped-def]
            return None, None

        def update_last_run(self, task_name, run_time, run_date) -> None:  # type: ignore[no-untyped-def]
            return None

    with pytest.raises(TypeError):
        OldStore()  # type: ignore[abstract]


def _after(seconds: int) -> datetime.datetime:
    return T0 + datetime.timedelta(seconds=seconds)


def _failing_on(
    bad_dates: set[datetime.date], runs: list[datetime.date]
) -> Callable[[datetime.date], None]:
    def job(run_date: datetime.date) -> None:
        if run_date in bad_dates:
            raise RuntimeError("feed down")
        runs.append(run_date)

    return job


async def test_a_date_that_keeps_failing_is_recorded_as_a_gap_and_stepped_past() -> None:
    runs: list[datetime.date] = []
    coord = ScheduledPullCoordinator(InMemoryStateStore())
    coord.register_task("t", _failing_on({JAN6}, runs), calendar=NYSE_LIKE, start_date=JAN6)

    assert await coord.check_and_run_once(_after(0)) == 0  # failure 1
    assert await coord.check_and_run_once(_after(301)) == 0  # failure 2
    assert await coord.check_and_run_once(_after(602)) == 1  # failure 3: gap, then Jan 7 runs

    assert runs == [JAN7]
    assert coord.gaps("t") == [
        GapEntry(
            task="t",
            logical_date=JAN6,
            reason="RuntimeError: feed down",
            failures=3,
            recorded_at=_after(602),
            kind="gap",
        )
    ]
    assert coord.state_store.get_last_run("t")[1] == JAN7
    assert coord.state_store.get_failure("t", JAN6) is None


async def test_the_failure_count_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    runs: list[datetime.date] = []
    job = _failing_on({JAN6}, runs)

    first = ScheduledPullCoordinator(JSONFileStateStore(str(path)))
    first.register_task("t", job, calendar=NYSE_LIKE, start_date=JAN6)
    assert await first.check_and_run_once(_after(0)) == 0
    assert await first.check_and_run_once(_after(301)) == 0

    # A new coordinator on a new store instance over the same file: the count carries on.
    second = ScheduledPullCoordinator(JSONFileStateStore(str(path)))
    second.register_task("t", job, calendar=NYSE_LIKE, start_date=JAN6)
    assert await second.check_and_run_once(_after(602)) == 1

    assert [g.logical_date for g in second.gaps("t")] == [JAN6]
    assert second.gaps("t")[0].failures == 3


async def test_a_calendar_less_task_no_longer_wedges_on_one_date() -> None:
    runs: list[datetime.date] = []
    coord = ScheduledPullCoordinator(InMemoryStateStore())
    coord.register_task("eastern", _failing_on({datetime.date(2026, 1, 7)}, runs))
    noon = datetime.datetime(
        2026, 1, 7, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-5))
    )

    for offset in (0, 301, 602):
        await coord.check_and_run_once(noon + datetime.timedelta(seconds=offset))
    assert [g.logical_date for g in coord.gaps("eastern")] == [datetime.date(2026, 1, 7)]
    assert [g.kind for g in coord.gaps("eastern")] == ["gap"]

    next_day = datetime.datetime(
        2026, 1, 8, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-5))
    )
    assert await coord.check_and_run_once(next_day) == 1
    assert runs == [datetime.date(2026, 1, 8)]


async def test_a_gap_on_a_weekday_fallback_calendar_is_recorded_as_no_data() -> None:
    calendar = dataclasses.replace(NYSE_LIKE, quality=CalendarQuality.WEEKDAY_FALLBACK)
    runs: list[datetime.date] = []
    coord = ScheduledPullCoordinator(InMemoryStateStore(), max_failures=1)
    coord.register_task("t", _failing_on({JAN6}, runs), calendar=calendar, start_date=JAN6)

    await coord.check_and_run_once(_after(0))

    assert [g.kind for g in coord.gaps("t")] == ["no_data"]


async def test_a_gap_past_a_library_bound_is_recorded_as_no_data() -> None:
    # quality stays LIBRARY; only the dates after the bound answer through the fallback.
    calendar = dataclasses.replace(NYSE_LIKE, fallback_after=JAN6)
    runs: list[datetime.date] = []
    coord = ScheduledPullCoordinator(InMemoryStateStore(), max_failures=1, max_dates_per_tick=2)
    coord.register_task("t", _failing_on({JAN6, JAN7}, runs), calendar=calendar, start_date=JAN6)

    await coord.check_and_run_once(_after(0))

    assert [(g.logical_date, g.kind) for g in coord.gaps("t")] == [
        (JAN6, "gap"),
        (JAN7, "no_data"),
    ]


async def test_max_failures_is_configurable() -> None:
    runs: list[datetime.date] = []
    coord = ScheduledPullCoordinator(InMemoryStateStore(), max_failures=1, max_dates_per_tick=1)
    coord.register_task("t", _failing_on({JAN6}, runs), calendar=NYSE_LIKE, start_date=JAN6)

    await coord.check_and_run_once(_after(0))

    assert [g.failures for g in coord.gaps("t")] == [1]


async def test_a_success_clears_the_failure_count() -> None:
    calls = {"n": 0}
    runs: list[datetime.date] = []

    def flaky(run_date: datetime.date) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("once")
        runs.append(run_date)

    coord = ScheduledPullCoordinator(InMemoryStateStore(), max_dates_per_tick=1)
    coord.register_task("t", flaky, calendar=NYSE_LIKE, start_date=JAN6)

    await coord.check_and_run_once(_after(0))
    assert await coord.check_and_run_once(_after(301)) == 1

    assert runs == [JAN6]
    assert coord.state_store.get_failure("t", JAN6) is None
    assert coord.gaps() == []


async def test_the_cooldown_still_spaces_retries() -> None:
    runs: list[datetime.date] = []
    coord = ScheduledPullCoordinator(
        InMemoryStateStore(), retry_cooldown=datetime.timedelta(seconds=60), max_dates_per_tick=1
    )
    coord.register_task("t", _failing_on({JAN6}, runs), calendar=NYSE_LIKE, start_date=JAN6)

    await coord.check_and_run_once(_after(0))
    failure = coord.state_store.get_failure("t", JAN6)
    assert failure is not None and failure.count == 1

    await coord.check_and_run_once(_after(30))  # inside the cooldown: no attempt
    failure = coord.state_store.get_failure("t", JAN6)
    assert failure is not None and failure.count == 1

    await coord.check_and_run_once(_after(61))  # cooldown elapsed: a second attempt
    failure = coord.state_store.get_failure("t", JAN6)
    assert failure is not None and failure.count == 2


async def test_a_gap_counts_against_the_tick_budget() -> None:
    runs: list[datetime.date] = []
    coord = ScheduledPullCoordinator(InMemoryStateStore(), max_failures=1, max_dates_per_tick=2)
    coord.register_task("t", _failing_on({JAN6}, runs), calendar=NYSE_LIKE, start_date=JAN6)
    now = datetime.datetime(2026, 1, 9, 22, 0, tzinfo=UTC)

    # Jan 6 fails into a gap, and the second attempt of this tick runs Jan 7.
    assert await coord.check_and_run_once(now) == 1
    assert runs == [JAN7]
    # The next tick spends its two attempts on Jan 8 and Jan 9.
    assert await coord.check_and_run_once(now) == 2
    assert runs == [JAN7, datetime.date(2026, 1, 8), datetime.date(2026, 1, 9)]
