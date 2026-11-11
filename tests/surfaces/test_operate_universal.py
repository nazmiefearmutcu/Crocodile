"""The ``universe-sync`` and ``schedule`` operator commands (task 15, step 7).

Every command runs through ``CliRunner`` over ``entrypoint.build_app()``, with the orchestrator
built on fake bindings (``fakes.py``) and both clocks pinned to ``FIXED_NOW``, so the outcome
never depends on the day the suite runs. Nothing here reaches a real source.
"""

from __future__ import annotations

import datetime
import pathlib
import re
from collections.abc import Iterator

import pytest
from click.testing import Result
from typer.testing import CliRunner

from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.master import ListingMaster, default_listings_path
from crocodile.equity.universal import orchestrator as orchestrator_module
from crocodile.equity.universal.base import SourceUnservableError, UnservableReason
from crocodile.equity.universal.orchestrator import SourceBindings, open_orchestrator
from crocodile.surfaces import entrypoint, operate
from tests.equity.universal.fakes import (
    FIXED_NOW,
    FakeSnapshotSource,
    FakeUniverseSource,
    daily_bar,
    make_observation,
    make_snapshot,
    seed_master,
)

pytestmark = pytest.mark.usefixtures("network_forbidden")

TURKEY_SESSION = datetime.date(2024, 3, 8)
SANCTIONED_MIC: str = next(info.mic for info in ExchangeRegistry().all() if info.sanctioned)
_RUN_REPORT = re.compile(r"Run report: (\S+)")


def _turkey_universe(*, errors: dict[str, BaseException] | None = None) -> FakeUniverseSource:
    observations = [
        make_observation(
            "tradingview", "BIST:THYAO", "XIST", "THYAO", currency="TRY", price_scale=0
        ),
        make_observation(
            "tradingview", "BIST:GARAN", "XIST", "GARAN", currency="TRY", price_scale=0
        ),
    ]
    return FakeUniverseSource(
        "tradingview",
        units={"turkey": frozenset({"XIST"})},
        snapshots={"turkey": make_snapshot("tradingview", observations)},
        errors=dict(errors or {}),
    )


def _turkey_snapshot(*, errors: dict[str, BaseException] | None = None) -> FakeSnapshotSource:
    bar = daily_bar(
        "tradingview", "THYAO@XIST", TURKEY_SESSION, 300.0, currency="TRY", price_scale=0
    )
    snapshot = FakeSnapshotSource(
        "tradingview",
        units={"turkey": frozenset({"XIST"})},
        bars={"turkey": (bar,)},
    )
    snapshot.errors = dict(errors or {})
    return snapshot


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(operate, "_now", lambda: FIXED_NOW)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    universe: FakeUniverseSource | None = None,
    snapshot: FakeSnapshotSource | None = None,
) -> None:
    """Build the orchestrator of every command on the given fakes, on the pinned clock."""
    bindings = SourceBindings(
        universe={} if universe is None else {universe.name: universe},
        snapshot={} if snapshot is None else {snapshot.name: snapshot},
    )
    monkeypatch.setattr(
        "crocodile.surfaces.operate._make_orchestrator",
        lambda settings, sink: open_orchestrator(
            settings, sink, bindings=bindings, clock=lambda: FIXED_NOW
        ),
    )
    monkeypatch.setattr(operate, "_now", lambda: FIXED_NOW)


def _seed_thyao(lake: pathlib.Path) -> None:
    """The listing master the collection reads: a bar is written only for a known listing."""
    master = ListingMaster.for_data_dir(lake)
    try:
        seed_master(
            master,
            [
                make_observation(
                    "tradingview", "BIST:THYAO", "XIST", "THYAO", currency="TRY", price_scale=0
                )
            ],
        )
    finally:
        master.close()


def _invoke(args: list[str], input_text: str | None = None) -> Result:
    return CliRunner().invoke(entrypoint.build_app(), args, input=input_text)


# ---------------------------------------------------------------- the clock and the table


def test_the_default_orchestrator_follows_the_operator_clock(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: dict[str, object] = {}

    def recorder(settings: object, sink: object, **kwargs: object) -> str:
        recorded.update(kwargs)
        return "orchestrator"

    monkeypatch.setattr(orchestrator_module, "open_orchestrator", recorder)
    monkeypatch.setattr(operate, "_now", lambda: FIXED_NOW)
    settings = operate._settings_for(tmp_path)
    operate._make_orchestrator(settings, operate._open_sink(tmp_path))

    clock = recorded["clock"]
    assert callable(clock)
    assert clock() == FIXED_NOW
    later = FIXED_NOW + datetime.timedelta(days=1)
    monkeypatch.setattr(operate, "_now", lambda: later)
    assert clock() == later


def test_the_operator_table_lists_universe_sync_and_schedule() -> None:
    names = {command.name for command in operate.build_app().registered_commands}
    assert {"universe-sync", "schedule"} <= names
    result = _invoke(["--help"])
    assert "universe-sync" in result.output
    assert "schedule" in result.output


def test_the_shell_lists_the_new_commands() -> None:
    result = _invoke(["shell"], input_text="help\nexit\n")
    assert result.exit_code == 0, result.output
    assert "universe-sync" in result.output
    assert "schedule" in result.output


# ---------------------------------------------------------------- universe-sync


@pytest.fixture
def universe_bound(monkeypatch: pytest.MonkeyPatch, clock: None) -> Iterator[FakeUniverseSource]:
    universe = _turkey_universe()
    _install(monkeypatch, universe=universe)
    yield universe


def test_universe_sync_writes_the_listing_master_and_prints_a_summary(
    tmp_path: pathlib.Path, universe_bound: FakeUniverseSource
) -> None:
    lake = tmp_path / "lake"
    result = _invoke(["universe-sync", "--mic", "XIST", "--data-dir", str(lake)])
    assert result.exit_code == 0, result.output
    output: str = result.output
    assert "XIST" in output
    assert "tradingview" in output
    master = ListingMaster(default_listings_path(lake), readonly=True)
    try:
        assert sorted(item.local_symbol for item in master.listings(mic="XIST")) == [
            "GARAN",
            "THYAO",
        ]
    finally:
        master.close()
    report_match = _RUN_REPORT.search(output)
    assert report_match is not None
    assert pathlib.Path(report_match.group(1)).is_file()


def test_universe_sync_needs_exactly_one_scope_flag(
    tmp_path: pathlib.Path, universe_bound: FakeUniverseSource
) -> None:
    lake = str(tmp_path / "lake")
    none = _invoke(["universe-sync", "--data-dir", lake])
    assert none.exit_code == 1
    assert "exactly one of --mic, --country or --all" in none.output
    both = _invoke(["universe-sync", "--mic", "XIST", "--country", "TR", "--data-dir", lake])
    assert both.exit_code == 1


def test_universe_sync_refuses_a_sanctioned_market(
    tmp_path: pathlib.Path, universe_bound: FakeUniverseSource
) -> None:
    result = _invoke(
        ["universe-sync", "--mic", SANCTIONED_MIC, "--data-dir", str(tmp_path / "lake")]
    )
    assert result.exit_code == 1
    assert SANCTIONED_MIC in result.output
    assert "Error:" in result.output


def test_universe_sync_exits_non_zero_when_every_fetch_failed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, clock: None
) -> None:
    universe = _turkey_universe(
        errors={
            "turkey": SourceUnservableError("tradingview", UnservableReason.BOT_CHALLENGE, "wall")
        }
    )
    _install(monkeypatch, universe=universe)
    result = _invoke(["universe-sync", "--mic", "XIST", "--data-dir", str(tmp_path / "lake")])
    assert result.exit_code == 1
    assert "unservable" in result.output


# ---------------------------------------------------------------- schedule


def test_schedule_collects_the_session_the_coordinator_dated(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _turkey_snapshot()
    _install(monkeypatch, snapshot=snapshot)
    lake = tmp_path / "lake"
    _seed_thyao(lake)
    result = _invoke(
        ["schedule", "--asset-class", "equity", "--mic", "XIST", "--once", "--data-dir", str(lake)]
    )
    assert result.exit_code == 0, result.output
    assert [sessions for _unit, sessions, _ns in snapshot.calls] == [{"XIST": TURKEY_SESSION}]


def test_schedule_once_runs_one_tick_and_reports_each_market(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, snapshot=_turkey_snapshot())
    lake = tmp_path / "lake"
    _seed_thyao(lake)
    result = _invoke(
        ["schedule", "--asset-class", "equity", "--mic", "XIST", "--once", "--data-dir", str(lake)]
    )
    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines() if "XIST" in line)
    assert "ok" in row
    assert (lake / "state" / "schedule-equity.json").is_file()


def test_schedule_once_twice_does_not_collect_the_same_session_again(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _turkey_snapshot()
    _install(monkeypatch, snapshot=snapshot)
    lake = tmp_path / "lake"
    _seed_thyao(lake)
    args = ["schedule", "--asset-class", "equity", "--mic", "XIST", "--once", "--data-dir"]
    first = _invoke([*args, str(lake)])
    assert first.exit_code == 0, first.output
    second = _invoke([*args, str(lake)])
    assert second.exit_code == 0, second.output
    row = next(line for line in second.output.splitlines() if "XIST" in line)
    assert "not_due" in row
    assert len(snapshot.calls) == 1


def test_schedule_once_exits_non_zero_when_a_market_failed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _turkey_snapshot(
        errors={
            "turkey": SourceUnservableError("tradingview", UnservableReason.BOT_CHALLENGE, "wall")
        }
    )
    _install(monkeypatch, snapshot=snapshot)
    lake = tmp_path / "lake"
    _seed_thyao(lake)
    result = _invoke(
        ["schedule", "--asset-class", "equity", "--mic", "XIST", "--once", "--data-dir", str(lake)]
    )
    assert result.exit_code == 1, result.output
    assert "unservable" in result.output


def test_schedule_refuses_the_crypto_asset_class(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, snapshot=_turkey_snapshot())
    result = _invoke(
        [
            "schedule",
            "--asset-class",
            "crypto",
            "--mic",
            "XIST",
            "--once",
            "--data-dir",
            str(tmp_path / "lake"),
        ]
    )
    assert result.exit_code == 1
    assert "only equity has a scheduled pull" in result.output


def test_schedule_without_once_runs_the_coordinator_loop_until_stopped(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, snapshot=_turkey_snapshot())

    async def stop(self: object, interval_seconds: float = 60.0) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("crocodile.core.scheduler.coordinator.ScheduledPullCoordinator.start", stop)
    result = _invoke(
        [
            "schedule",
            "--asset-class",
            "equity",
            "--mic",
            "XIST",
            "--data-dir",
            str(tmp_path / "lake"),
        ]
    )
    assert result.exit_code == 0, result.output
    assert "Scheduler stopped." in result.output
