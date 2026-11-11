"""The scheduler wiring of universal end-of-day collection (task 15, step 1).

``build_eod_schedule`` registers one coordinator task per market with that market's calendar.
Every test is offline: the sources are the fakes of ``fakes.py`` and the state store is in
memory. The clock is always a value the test chooses, never the day the suite runs.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from crocodile.core.markets.calendar import CalendarQuality, calendar_for
from crocodile.core.markets.errors import SanctionedMarketError
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.master import ListingMaster
from crocodile.core.scheduler import InMemoryStateStore
from crocodile.equity.universal.base import SourceUnservableError, UnservableReason
from crocodile.equity.universal.orchestrator import (
    EodCollectionError,
    Scope,
    SourceBindings,
    build_eod_schedule,
    eod_task_name,
)
from crocodile.equity.universal.order import source_order
from tests.equity.universal.fakes import (
    FIXED_NOW,
    FakeSnapshotSource,
    daily_bar,
    make_observation,
    make_orchestrator,
    seed_master,
)

pytestmark = pytest.mark.usefixtures("network_forbidden")

TURKEY_SESSION = datetime.date(2024, 3, 8)
WEDNESDAY = datetime.date(2024, 3, 6)
SANCTIONED_MIC: str = next(info.mic for info in ExchangeRegistry().all() if info.sanctioned)


def _bindings(*snapshot: FakeSnapshotSource) -> SourceBindings:
    return SourceBindings(snapshot={source.name: source for source in snapshot})


def _xist_master(tmp_path: Path) -> ListingMaster:
    master = ListingMaster.for_data_dir(tmp_path)
    seed_master(
        master,
        [
            make_observation(
                "tradingview", "BIST:THYAO", "XIST", "THYAO", currency="TRY", price_scale=0
            )
        ],
    )
    return master


def _turkey(*bars: object) -> FakeSnapshotSource:
    return FakeSnapshotSource(
        "tradingview",
        units={"turkey": frozenset({"XIST"})},
        bars={"turkey": tuple(bars)},  # type: ignore[arg-type]
    )


def _snapshot_name(mic: str) -> str:
    """A snapshot source name that sits in the market's daily chain (tradingview when it does)."""
    chain = source_order().order_for(mic, "ohlcv")
    return "tradingview" if "tradingview" in chain else chain[0]


def _weekday_fallback_mic() -> str:
    """A market on the weekday fallback that has a daily source to bind (the chain is not empty)."""
    return next(
        info.mic
        for info in ExchangeRegistry().all()
        if not info.sanctioned
        and calendar_for(info.mic).quality is CalendarQuality.WEEKDAY_FALLBACK
        and source_order().order_for(info.mic, "ohlcv")
    )


async def test_one_task_is_registered_per_market_with_that_markets_calendar(tmp_path: Path) -> None:
    orch = make_orchestrator(tmp_path, _bindings(_turkey()), master=_xist_master(tmp_path))
    try:
        schedule = build_eod_schedule(orch, Scope(country="TR"), state_store=InMemoryStateStore())
    finally:
        await orch.aclose()

    assert eod_task_name("XIST") in schedule.coordinator.tasks
    task = schedule.coordinator.tasks[eod_task_name("XIST")]
    assert task.calendar is calendar_for("XIST")
    assert "XIST" in schedule.mics


async def test_a_market_with_no_bound_source_is_skipped_and_reported(tmp_path: Path) -> None:
    orch = make_orchestrator(tmp_path, SourceBindings(), master=_xist_master(tmp_path))
    try:
        schedule = build_eod_schedule(orch, Scope(mic="XIST"), state_store=InMemoryStateStore())
    finally:
        await orch.aclose()

    assert schedule.skipped == {"XIST": "no_source"}
    assert schedule.mics == ()
    assert eod_task_name("XIST") not in schedule.coordinator.tasks


async def test_an_all_markets_schedule_skips_sanctioned_markets(tmp_path: Path) -> None:
    orch = make_orchestrator(tmp_path, SourceBindings(), master=_xist_master(tmp_path))
    try:
        schedule = build_eod_schedule(
            orch, Scope(all_markets=True), state_store=InMemoryStateStore()
        )
    finally:
        await orch.aclose()

    assert schedule.skipped[SANCTIONED_MIC] == "sanctioned"
    assert SANCTIONED_MIC not in schedule.mics
    assert eod_task_name(SANCTIONED_MIC) not in schedule.coordinator.tasks


async def test_an_explicitly_named_sanctioned_market_is_refused(tmp_path: Path) -> None:
    orch = make_orchestrator(tmp_path, _bindings(_turkey()), master=_xist_master(tmp_path))
    try:
        with pytest.raises(SanctionedMarketError):
            build_eod_schedule(orch, Scope(mic=SANCTIONED_MIC), state_store=InMemoryStateStore())
    finally:
        await orch.aclose()


async def test_a_due_market_is_collected_once_per_session(tmp_path: Path) -> None:
    snapshot = _turkey(
        daily_bar("tradingview", "THYAO@XIST", TURKEY_SESSION, 300.0, currency="TRY", price_scale=0)
    )
    orch = make_orchestrator(
        tmp_path,
        _bindings(snapshot),
        master=_xist_master(tmp_path),
        clock=lambda: FIXED_NOW,
    )
    try:
        schedule = build_eod_schedule(orch, Scope(mic="XIST"), state_store=InMemoryStateStore())
        first = await schedule.coordinator.check_and_run_once(current_time=FIXED_NOW)
        second = await schedule.coordinator.check_and_run_once(current_time=FIXED_NOW)
    finally:
        await orch.aclose()

    assert first == 1
    assert second == 0
    assert len(snapshot.calls) == 1
    assert snapshot.calls[0][1] == {"XIST": TURKEY_SESSION}
    assert schedule.failures == {}
    assert schedule.reports["XIST"].records_written == 1


async def test_a_failed_collection_is_raised_to_the_coordinator(tmp_path: Path) -> None:
    snapshot = _turkey()
    snapshot.errors["turkey"] = SourceUnservableError(
        "tradingview", UnservableReason.BOT_CHALLENGE, "recorded wall"
    )
    orch = make_orchestrator(tmp_path, _bindings(snapshot), master=_xist_master(tmp_path))
    try:
        schedule = build_eod_schedule(orch, Scope(mic="XIST"), state_store=InMemoryStateStore())
        executed = await schedule.coordinator.check_and_run_once(current_time=FIXED_NOW)
    finally:
        await orch.aclose()

    assert executed == 0
    assert "XIST" in schedule.failures
    assert "bot_challenge" in schedule.failures["XIST"]
    failure = schedule.coordinator.state_store.get_failure(eod_task_name("XIST"), TURKEY_SESSION)
    assert failure is not None
    assert failure.count == 1


async def test_a_session_without_bars_becomes_a_no_data_gap_on_a_weekday_fallback_calendar(
    tmp_path: Path,
) -> None:
    mic = _weekday_fallback_mic()
    snapshot = FakeSnapshotSource(_snapshot_name(mic), units={"unit": frozenset({mic})})
    tick = calendar_for(mic).session_close_utc(WEDNESDAY) + datetime.timedelta(hours=1)
    orch = make_orchestrator(
        tmp_path,
        _bindings(snapshot),
        master=ListingMaster.for_data_dir(tmp_path),
        clock=lambda: tick,
    )
    try:
        schedule = build_eod_schedule(
            orch, Scope(mic=mic), state_store=InMemoryStateStore(), max_failures=1
        )
        await schedule.coordinator.check_and_run_once(current_time=tick)
    finally:
        await orch.aclose()

    (entry,) = schedule.coordinator.gaps(eod_task_name(mic))
    assert entry.kind == "no_data"
    assert entry.logical_date == WEDNESDAY
    assert len(snapshot.calls) == 1


async def test_a_session_without_bars_becomes_a_gap_on_a_library_calendar(tmp_path: Path) -> None:
    snapshot = _turkey()
    tick = calendar_for("XIST").session_close_utc(WEDNESDAY) + datetime.timedelta(hours=1)
    orch = make_orchestrator(
        tmp_path,
        _bindings(snapshot),
        master=_xist_master(tmp_path),
        clock=lambda: tick,
    )
    try:
        schedule = build_eod_schedule(
            orch, Scope(mic="XIST"), state_store=InMemoryStateStore(), max_failures=1
        )
        await schedule.coordinator.check_and_run_once(current_time=tick)
    finally:
        await orch.aclose()

    (entry,) = schedule.coordinator.gaps(eod_task_name("XIST"))
    assert entry.kind == "gap"
    assert entry.logical_date == WEDNESDAY
    assert len(snapshot.calls) == 1


def test_eod_collection_error_names_the_market_and_reason() -> None:
    error = EodCollectionError("XIST", "no bars for the session")
    assert str(error) == "EOD collection for XIST failed: no bars for the session"
    assert (error.mic, error.reason) == ("XIST", "no bars for the session")
