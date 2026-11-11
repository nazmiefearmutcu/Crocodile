"""The universal orchestrator: scope resolution, universe sync, backfill, EOD and intraday.

Every test runs offline: sources are the fakes in ``fakes.py`` and the listing master and
state are files under pytest's ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass
from pathlib import Path

import msgspec
import pytest

from crocodile.core.config import Settings
from crocodile.core.errors import TransientConnectorError
from crocodile.core.markets.errors import (
    ListingConflictError,
    NoGrammarForMarket,
    SanctionedMarketError,
    UnknownMarketError,
)
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.master import ListingMaster, ListingStatus
from crocodile.core.markets.symbols import listing_symbol
from crocodile.core.schema.records import OHLCV, CorporateAction
from crocodile.equity.providers.base import TransientProviderError, UnsupportedMarketError
from crocodile.equity.universal.base import (
    SourceUnservableError,
    UniverseSnapshot,
    UnservableReason,
)
from crocodile.equity.universal.orchestrator import (
    OutcomeStatus,
    ResolvedScope,
    RunReport,
    Scope,
    SourceBindings,
    UniversalOrchestrator,
    preflight_scope,
    resolve_scope,
)
from crocodile.equity.universal.state import Coverage, UniversalState
from tests.equity.universal.fakes import (
    FakeHistorySource,
    FakeUniverseSource,
    RecordingSink,
    daily_bar,
    instrument,
    make_observation,
    make_orchestrator,
    make_snapshot,
    seed_master,
)

SANCTIONED_MIC: str = next(info.mic for info in ExchangeRegistry().all() if info.sanctioned)


def _seeded_master(tmp_path: Path) -> ListingMaster:
    master = ListingMaster.for_data_dir(tmp_path)
    seed_master(
        master,
        [
            make_observation(
                "tradingview",
                "BIST:THYAO",
                "XIST",
                "THYAO",
                currency="TRY",
                price_scale=0,
                extra_aliases=(("yahoo_chart", "THYAO.IS"),),
            ),
            make_observation(
                "tradingview", "NASDAQ:AAPL", "XNAS", "AAPL", currency="USD", price_scale=0
            ),
        ],
    )
    return master


def test_a_scope_must_name_a_market_or_listings(tmp_path: Path) -> None:
    master = ListingMaster.for_data_dir(tmp_path)
    with pytest.raises(ValueError, match="needs mic, country, all_markets or symbols"):
        resolve_scope(Scope(), master)


def test_a_scope_naming_two_markets_is_refused(tmp_path: Path) -> None:
    master = ListingMaster.for_data_dir(tmp_path)
    with pytest.raises(ValueError):
        resolve_scope(Scope(mic="XIST", country="TR"), master)
    with pytest.raises(ValueError):
        resolve_scope(Scope(mic="XIST", all_markets=True), master)


def test_a_segment_mic_resolves_to_its_operating_mic(tmp_path: Path) -> None:
    resolved = resolve_scope(Scope(mic="XEQY"), ListingMaster.for_data_dir(tmp_path))
    assert resolved.mics == ("XIST",)


def test_a_country_scope_expands_to_its_exchanges(tmp_path: Path) -> None:
    resolved = resolve_scope(Scope(country="TR"), ListingMaster.for_data_dir(tmp_path))
    assert resolved.mics == ("XIST",)


async def test_an_explicitly_named_sanctioned_market_is_refused_before_any_source_is_called(
    tmp_path: Path,
) -> None:
    history = FakeHistorySource()
    universe = FakeUniverseSource(name="nasdaqtrader", units={})
    orch = make_orchestrator(
        tmp_path,
        SourceBindings(history={"yahoo_chart": history}, universe={"nasdaqtrader": universe}),
    )
    try:
        with pytest.raises(SanctionedMarketError):
            await orch.sync_universe(Scope(mic=SANCTIONED_MIC))
        assert history.calls == []
        assert universe.calls == []
    finally:
        await orch.aclose()


def test_an_all_markets_scope_reports_each_sanctioned_listing_without_a_request(
    tmp_path: Path,
) -> None:
    resolved = resolve_scope(Scope(all_markets=True), ListingMaster.for_data_dir(tmp_path))
    refused = {(mic, reason) for _subject, mic, reason in resolved.refused}
    assert (SANCTIONED_MIC, "sanctioned") in refused
    assert SANCTIONED_MIC not in resolved.mics
    assert "XIST" in resolved.mics


def test_the_operator_opt_in_reopens_a_sanctioned_market(tmp_path: Path) -> None:
    resolved = resolve_scope(
        Scope(mic="MISX"), ListingMaster.for_data_dir(tmp_path), allow=frozenset({"RU"})
    )
    assert resolved.mics == ("MISX",)


def test_symbols_resolve_through_canonical_alias_and_bare_forms(tmp_path: Path) -> None:
    master = _seeded_master(tmp_path)
    resolved = resolve_scope(
        Scope(symbols=("tradingview:BIST:THYAO", "yahoo_chart:THYAO.IS", "THYAO@XIST", "AAPL")),
        master,
    )
    assert {listing.symbol for listing in resolved.listings} == {
        listing_symbol("THYAO", "XIST"),
        "AAPL",
    }
    assert resolved.explicit_listings is True
    assert resolved.refused == ()


def test_unknown_symbols_are_reported_and_a_scope_with_none_known_is_refused(
    tmp_path: Path,
) -> None:
    master = _seeded_master(tmp_path)
    resolved = resolve_scope(Scope(symbols=("THYAO@XIST", "NOPE@XIST")), master)
    assert ("NOPE@XIST", None, "unknown_listing") in resolved.refused
    with pytest.raises(ValueError, match="universe-sync"):
        resolve_scope(Scope(symbols=("NOPE@XIST",)), master)


def test_a_listing_outside_the_named_market_is_refused(tmp_path: Path) -> None:
    master = _seeded_master(tmp_path)
    with pytest.raises(ValueError, match="THYAO@XIST"):
        resolve_scope(Scope(mic="XNYS", symbols=("THYAO@XIST",)), master)


def test_preflight_scope_resolves_the_slice_from_the_registries_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(data_dir=tmp_path / "lake")
    resolved = preflight_scope(settings, Scope(country="TR"))
    assert isinstance(resolved, ResolvedScope)
    assert resolved.mics == ("XIST",)
    assert resolved.listings == ()
    assert resolved.explicit_listings is False
    assert list(tmp_path.iterdir()) == []
    assert not (tmp_path / "lake").exists()
    with pytest.raises(SanctionedMarketError):
        preflight_scope(settings, Scope(mic=SANCTIONED_MIC))


def _universe_source(
    name: str, units: dict[str, frozenset[str]], **kw: object
) -> FakeUniverseSource:
    return FakeUniverseSource(name=name, units=units, **kw)  # type: ignore[arg-type]


async def test_sync_universe_applies_each_snapshot_and_writes_its_instrument_records(
    tmp_path: Path,
) -> None:
    observation = make_observation(
        "tradingview", "BIST:THYAO", "XIST", "THYAO", currency="TRY", price_scale=0
    )
    record = instrument("tradingview", "THYAO@XIST", "XIST", name="Turkiye Petrol")
    universe = _universe_source(
        "tradingview",
        {"turkey": frozenset({"XIST"})},
        snapshots={"turkey": make_snapshot("tradingview", [observation], [record])},
    )
    master = ListingMaster.for_data_dir(tmp_path)
    sink = RecordingSink()
    orch = make_orchestrator(
        tmp_path, SourceBindings(universe={"tradingview": universe}), master=master, sink=sink
    )
    try:
        report = await orch.sync_universe(Scope(mic="XIST"), sources=("tradingview",))
        assert master.get("THYAO@XIST") is not None
        assert [item.symbol for item in sink.records] == ["THYAO@XIST"]
        assert report.records_written == 1
        assert report.by_status() == {"ok": 1}
        assert report.report_path is not None
        assert report.report_path.endswith(f"{report.run_id}.json")
    finally:
        await orch.aclose()


async def test_a_unit_shared_by_two_markets_is_fetched_once(tmp_path: Path) -> None:
    observations = [
        make_observation(
            "tradingview", "BIST:THYAO", "XIST", "THYAO", currency="TRY", price_scale=0
        ),
        make_observation(
            "tradingview", "NASDAQ:AAPL", "XNAS", "AAPL", currency="USD", price_scale=0
        ),
    ]
    universe = _universe_source(
        "tradingview",
        {"global": frozenset({"XIST", "XNAS"})},
        snapshots={"global": make_snapshot("tradingview", observations)},
    )
    orch = make_orchestrator(tmp_path, SourceBindings(universe={"tradingview": universe}))
    try:
        report = await orch.sync_universe(Scope(all_markets=True), sources=("tradingview",))
        assert len(universe.calls) == 1
        for mic in ("XIST", "XNAS"):
            assert [o.status for o in report.market_outcomes(mic)] == [OutcomeStatus.OK]
    finally:
        await orch.aclose()


async def test_an_incomplete_snapshot_never_marks_listings_unseen(tmp_path: Path) -> None:
    master = ListingMaster.for_data_dir(tmp_path)
    seed_master(
        master,
        [
            make_observation(
                "tradingview", "BIST:OLDCO", "XIST", "OLDCO", currency="TRY", price_scale=0
            )
        ],
    )
    observation = make_observation(
        "tradingview", "BIST:THYAO", "XIST", "THYAO", currency="TRY", price_scale=0
    )
    snapshot = make_snapshot("tradingview", [observation], complete=False, skipped={"row_limit": 3})
    universe = _universe_source(
        "tradingview", {"turkey": frozenset({"XIST"})}, snapshots={"turkey": snapshot}
    )
    orch = make_orchestrator(
        tmp_path, SourceBindings(universe={"tradingview": universe}), master=master
    )
    try:
        report = await orch.sync_universe(Scope(mic="XIST"), sources=("tradingview",))
        old = master.get("OLDCO@XIST")
        assert old is not None
        assert old.status is ListingStatus.ACTIVE
        (outcome,) = report.market_outcomes("XIST")
        assert outcome.status is OutcomeStatus.OK
        assert outcome.reason is not None
        assert "incomplete read: unseen marking skipped" in outcome.reason
        assert "row_limit=3" in outcome.reason
    finally:
        await orch.aclose()


async def test_a_failed_universe_fetch_applies_nothing(tmp_path: Path) -> None:
    master = ListingMaster.for_data_dir(tmp_path)
    universe = _universe_source(
        "tradingview", {"turkey": frozenset({"XIST"})}, errors={"turkey": RuntimeError("boom")}
    )
    orch = make_orchestrator(
        tmp_path, SourceBindings(universe={"tradingview": universe}), master=master
    )
    try:
        report = await orch.sync_universe(Scope(mic="XIST"), sources=("tradingview",))
        assert master.get("THYAO@XIST") is None
        (outcome,) = report.market_outcomes("XIST")
        assert outcome.status is OutcomeStatus.FAILED
        assert outcome.source == "tradingview"
        assert "RuntimeError: boom" in (outcome.reason or "")
    finally:
        await orch.aclose()


async def test_a_requested_market_missing_from_the_snapshot_is_reported_empty(
    tmp_path: Path,
) -> None:
    observation = make_observation(
        "tradingview", "BIST:THYAO", "XIST", "THYAO", currency="TRY", price_scale=0
    )
    universe = _universe_source(
        "tradingview",
        {"global": frozenset({"XIST", "XNAS"})},
        snapshots={"global": make_snapshot("tradingview", [observation])},
    )
    orch = make_orchestrator(tmp_path, SourceBindings(universe={"tradingview": universe}))
    try:
        report = await orch.sync_universe(Scope(all_markets=True), sources=("tradingview",))
        assert [o.status for o in report.market_outcomes("XNAS")] == [OutcomeStatus.EMPTY_UNIVERSE]
        assert [o.status for o in report.market_outcomes("XIST")] == [OutcomeStatus.OK]
    finally:
        await orch.aclose()


async def test_identity_conflicts_are_reported_and_the_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from crocodile.equity.universal import orchestrator as module
    from crocodile.equity.universal.base import UniverseApplyReport

    def conflicting(master: ListingMaster, snapshot: UniverseSnapshot) -> UniverseApplyReport:
        return UniverseApplyReport(
            source=snapshot.source,
            upserted=0,
            conflicts=("THYAO@XIST already belongs to another ISIN",),
            unseen_marked_mics=(),
        )

    monkeypatch.setattr(module, "apply_universe", conflicting)
    observation = make_observation(
        "tradingview", "BIST:THYAO", "XIST", "THYAO", currency="TRY", price_scale=0
    )
    universe = _universe_source(
        "tradingview",
        {"turkey": frozenset({"XIST"})},
        snapshots={"turkey": make_snapshot("tradingview", [observation])},
    )
    orch = make_orchestrator(tmp_path, SourceBindings(universe={"tradingview": universe}))
    try:
        report = await orch.sync_universe(Scope(mic="XIST"), sources=("tradingview",))
        conflicts = [o for o in report.outcomes if o.status is OutcomeStatus.IDENTITY_CONFLICT]
        assert [o.subject for o in conflicts] == ["tradingview"]
        assert "another ISIN" in (conflicts[0].reason or "")
        assert [o.status for o in report.market_outcomes("XIST")] == [OutcomeStatus.OK]
    finally:
        await orch.aclose()


async def test_a_master_failure_is_isolated_to_its_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from crocodile.equity.universal import orchestrator as module
    from crocodile.equity.universal.base import UniverseApplyReport

    real_apply = module.apply_universe

    def failing_for_xist(master: ListingMaster, snapshot: UniverseSnapshot) -> UniverseApplyReport:
        if "XIST" in snapshot.seen:
            raise RuntimeError("master unavailable")
        return real_apply(master, snapshot)

    monkeypatch.setattr(module, "apply_universe", failing_for_xist)
    turkey = make_observation(
        "tradingview", "BIST:THYAO", "XIST", "THYAO", currency="TRY", price_scale=0
    )
    global_ = make_observation(
        "tradingview", "NASDAQ:AAPL", "XNAS", "AAPL", currency="USD", price_scale=0
    )
    universe = _universe_source(
        "tradingview",
        {"turkey": frozenset({"XIST"}), "global": frozenset({"XNAS"})},
        snapshots={
            "turkey": make_snapshot("tradingview", [turkey]),
            "global": make_snapshot("tradingview", [global_]),
        },
    )
    master = ListingMaster.for_data_dir(tmp_path)
    orch = make_orchestrator(
        tmp_path, SourceBindings(universe={"tradingview": universe}), master=master
    )
    try:
        report = await orch.sync_universe(Scope(all_markets=True), sources=("tradingview",))
        assert master.get("THYAO@XIST") is None
        assert master.get("AAPL") is not None
        assert [(o.status, o.reason) for o in report.market_outcomes("XIST")] == [
            (OutcomeStatus.FAILED, "RuntimeError: master unavailable")
        ]
        assert [o.status for o in report.market_outcomes("XNAS")] == [OutcomeStatus.OK]
    finally:
        await orch.aclose()


async def test_directories_are_applied_before_tradingview(tmp_path: Path) -> None:
    order: list[str] = []

    @dataclass
    class _Ordered(FakeUniverseSource):
        async def universe(self, unit: str, *, captured_ns: int) -> UniverseSnapshot:
            order.append(self.name)
            return await super().universe(unit, captured_ns=captured_ns)

    directory = _Ordered(
        name="nasdaqtrader",
        units={"nasdaqtrader": frozenset({"XNAS"})},
        snapshots={
            "nasdaqtrader": make_snapshot(
                "nasdaqtrader",
                [
                    make_observation(
                        "nasdaqtrader", "AAPL", "XNAS", "AAPL", currency="USD", price_scale=0
                    )
                ],
            )
        },
    )
    tradingview = _Ordered(
        name="tradingview",
        units={"global": frozenset({"XNAS"})},
        snapshots={
            "global": make_snapshot(
                "tradingview",
                [
                    make_observation(
                        "tradingview", "NASDAQ:MSFT", "XNAS", "MSFT", currency="USD", price_scale=0
                    )
                ],
            )
        },
    )
    orch = make_orchestrator(
        tmp_path,
        SourceBindings(universe={"nasdaqtrader": directory, "tradingview": tradingview}),
    )
    try:
        await orch.sync_universe(Scope(mic="XNAS"))
        assert order == ["nasdaqtrader", "tradingview"]
    finally:
        await orch.aclose()


M4 = datetime.date(2024, 3, 4)
M5 = datetime.date(2024, 3, 5)
M6 = datetime.date(2024, 3, 6)
M7 = datetime.date(2024, 3, 7)
M8 = datetime.date(2024, 3, 8)  # the latest completed XIST session at FIXED_NOW


def _midnight(day: datetime.date) -> int:
    moment = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.UTC)
    return int(moment.timestamp()) * 1_000_000_000


def _thyao_master(tmp_path: Path) -> ListingMaster:
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


def _covered(
    orch: UniversalOrchestrator, source: str, symbol: str, channel: str, interval: str
) -> tuple[int, int] | None:
    coverage = orch._state.coverage(source, symbol, channel, interval)
    if coverage is None or coverage.covered_from_ns is None or coverage.covered_until_ns is None:
        return None
    return coverage.covered_from_ns, coverage.covered_until_ns


def _series(*sessions: datetime.date) -> list[tuple[datetime.date, float]]:
    return [(day, 10.0 + index) for index, day in enumerate(sessions)]


async def test_backfill_writes_daily_history_and_corporate_actions_under_the_listing_symbol(
    tmp_path: Path,
) -> None:
    history = FakeHistorySource(
        bars={"THYAO@XIST": _series(M4, M5, M6, M7, M8)},
        dividends={"THYAO@XIST": [(M6, 1.5)]},
    )
    master = _thyao_master(tmp_path)
    sink = RecordingSink()
    orch = make_orchestrator(
        tmp_path, SourceBindings(history={"yahoo_chart": history}), master=master, sink=sink
    )
    try:
        report = await orch.backfill(
            Scope(mic="XIST"),
            ("ohlcv", "corp_action"),
            start=M4,
            end=M8,
            sources=("yahoo_chart",),
        )
        bars = [r for r in sink.records if isinstance(r, OHLCV)]
        actions = [r for r in sink.records if isinstance(r, CorporateAction)]
        assert len(bars) == 5
        assert len(actions) == 1
        assert {r.symbol for r in sink.records} == {"THYAO@XIST"}
        assert report.records_written == 6
        assert {(o.channel, o.status) for o in report.outcomes} == {
            ("ohlcv", OutcomeStatus.OK),
            ("corp_action", OutcomeStatus.OK),
        }
        covered = orch._state.coverage("yahoo_chart", "THYAO@XIST", "ohlcv", "1d")
        assert covered is not None
        assert covered.covered_until_ns == _midnight(M8)
    finally:
        await orch.aclose()


async def test_one_history_call_serves_both_channels(tmp_path: Path) -> None:
    history = FakeHistorySource(
        bars={"THYAO@XIST": _series(M4, M5)},
        dividends={"THYAO@XIST": [(M5, 2.0)]},
    )
    sink = RecordingSink()
    orch = make_orchestrator(
        tmp_path,
        SourceBindings(history={"yahoo_chart": history}),
        master=_thyao_master(tmp_path),
        sink=sink,
    )
    try:
        await orch.backfill(
            Scope(mic="XIST"), ("ohlcv", "corp_action"), start=M4, end=M5, sources=("yahoo_chart",)
        )
        assert len(history.calls) == 1
        assert sum(isinstance(r, CorporateAction) for r in sink.records) == 1
        assert sum(isinstance(r, OHLCV) for r in sink.records) == 2
    finally:
        await orch.aclose()


async def test_backfill_resumes_after_interruption_without_refetching_completed_listings(
    tmp_path: Path,
) -> None:
    master = ListingMaster.for_data_dir(tmp_path)
    seed_master(
        master,
        [
            make_observation(
                "tradingview", f"BIST:{local}", "XIST", local, currency="TRY", price_scale=0
            )
            for local in ("AAA", "BBB", "CCC")
        ],
    )
    gate = asyncio.Event()
    first = FakeHistorySource(
        bars={f"{local}@XIST": _series(M8) for local in ("AAA", "BBB", "CCC")},
        gated={"CCC@XIST"},
        gate=gate,
    )
    sink = RecordingSink()
    orch = make_orchestrator(
        tmp_path,
        SourceBindings(history={"yahoo_chart": first}),
        master=master,
        sink=sink,
        max_concurrency=1,
        checkpoint_every=1,
    )
    task = asyncio.create_task(
        orch.backfill(Scope(mic="XIST"), ("ohlcv",), start=M8, end=M8, sources=("yahoo_chart",))
    )
    for _ in range(10_000):
        if len(first.calls) >= 3:
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await orch.aclose()

    second = FakeHistorySource(
        bars={f"{local}@XIST": _series(M8) for local in ("AAA", "BBB", "CCC")}
    )
    resumed_sink = RecordingSink()
    resumed = make_orchestrator(
        tmp_path,
        SourceBindings(history={"yahoo_chart": second}),
        master=ListingMaster.for_data_dir(tmp_path),
        sink=resumed_sink,
        max_concurrency=1,
    )
    try:
        report = await resumed.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M8, end=M8, sources=("yahoo_chart",)
        )
        assert [call[0] for call in second.calls] == ["CCC@XIST"]
        statuses = {o.subject: o.status for o in report.outcomes}
        assert statuses == {
            "AAA@XIST": OutcomeStatus.ALREADY_COVERED,
            "BBB@XIST": OutcomeStatus.ALREADY_COVERED,
            "CCC@XIST": OutcomeStatus.OK,
        }
    finally:
        await resumed.aclose()


async def test_a_later_end_fetches_only_the_tail(tmp_path: Path) -> None:
    master = _thyao_master(tmp_path)
    first = FakeHistorySource(bars={"THYAO@XIST": _series(M4, M5, M6)})
    orch = make_orchestrator(
        tmp_path, SourceBindings(history={"yahoo_chart": first}), master=master
    )
    try:
        await orch.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M4, end=M6, sources=("yahoo_chart",)
        )
    finally:
        await orch.aclose()
    second = FakeHistorySource(bars={"THYAO@XIST": _series(M4, M5, M6, M7, M8)})
    resumed = make_orchestrator(
        tmp_path,
        SourceBindings(history={"yahoo_chart": second}),
        master=ListingMaster.for_data_dir(tmp_path),
    )
    try:
        await resumed.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M4, end=M8, sources=("yahoo_chart",)
        )
        assert second.calls == [("THYAO@XIST", "ohlcv", _midnight(M7), _midnight(M8))]
    finally:
        await resumed.aclose()


async def test_an_earlier_start_fetches_only_the_head(tmp_path: Path) -> None:
    first = FakeHistorySource(bars={"THYAO@XIST": _series(M6, M7, M8)})
    orch = make_orchestrator(
        tmp_path, SourceBindings(history={"yahoo_chart": first}), master=_thyao_master(tmp_path)
    )
    try:
        await orch.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M6, end=M8, sources=("yahoo_chart",)
        )
    finally:
        await orch.aclose()
    second = FakeHistorySource(bars={"THYAO@XIST": _series(M4, M5, M6, M7, M8)})
    resumed = make_orchestrator(
        tmp_path,
        SourceBindings(history={"yahoo_chart": second}),
        master=ListingMaster.for_data_dir(tmp_path),
    )
    try:
        await resumed.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M4, end=M8, sources=("yahoo_chart",)
        )
        assert second.calls == [("THYAO@XIST", "ohlcv", _midnight(M4), _midnight(M5))]
    finally:
        await resumed.aclose()


async def test_no_bar_is_kept_past_the_latest_completed_session(tmp_path: Path) -> None:
    later = datetime.date(2024, 3, 11)
    history = FakeHistorySource(bars={"THYAO@XIST": _series(M8, later)})
    sink = RecordingSink()
    orch = make_orchestrator(
        tmp_path,
        SourceBindings(history={"yahoo_chart": history}),
        master=_thyao_master(tmp_path),
        sink=sink,
    )
    try:
        await orch.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M8, end=later, sources=("yahoo_chart",)
        )
        assert history.calls[0][3] == _midnight(M8)
        assert all(isinstance(r, OHLCV) and r.source_ts <= _midnight(M8) for r in sink.records)
    finally:
        await orch.aclose()


async def test_an_unpublished_last_session_is_refetched_next_run(tmp_path: Path) -> None:
    first = FakeHistorySource(bars={"THYAO@XIST": _series(M4, M5, M6, M7)})
    orch = make_orchestrator(
        tmp_path, SourceBindings(history={"yahoo_chart": first}), master=_thyao_master(tmp_path)
    )
    try:
        await orch.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M4, end=M8, sources=("yahoo_chart",)
        )
    finally:
        await orch.aclose()
    second = FakeHistorySource(bars={"THYAO@XIST": _series(M4, M5, M6, M7, M8)})
    resumed = make_orchestrator(
        tmp_path,
        SourceBindings(history={"yahoo_chart": second}),
        master=ListingMaster.for_data_dir(tmp_path),
    )
    try:
        report = await resumed.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M4, end=M8, sources=("yahoo_chart",)
        )
        assert second.calls == [("THYAO@XIST", "ohlcv", _midnight(M8), _midnight(M8))]
        assert report.records_written == 1
    finally:
        await resumed.aclose()


async def test_an_unservable_source_is_reported_with_its_reason_and_the_next_source_is_tried(
    tmp_path: Path,
) -> None:
    blocked = FakeHistorySource(
        name="yahoo_chart",
        raises={
            "THYAO@XIST": SourceUnservableError(
                "yahoo_chart", UnservableReason.BOT_CHALLENGE, "captcha"
            )
        },
    )
    backup = FakeHistorySource(name="tradingview", bars={"THYAO@XIST": _series(M8)})
    orch = make_orchestrator(
        tmp_path,
        SourceBindings(history={"yahoo_chart": blocked, "tradingview": backup}),
        master=_thyao_master(tmp_path),
    )
    try:
        report = await orch.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M8, end=M8, sources=("yahoo_chart", "tradingview")
        )
        assert report.unservable_by_listing()["THYAO@XIST"]["yahoo_chart"] == "bot_challenge"
        statuses = {(o.source, o.status) for o in report.outcomes}
        assert ("tradingview", OutcomeStatus.OK) in statuses
    finally:
        await orch.aclose()


async def test_an_unsupported_market_is_reported_as_unservable(tmp_path: Path) -> None:
    history = FakeHistorySource(raises={"THYAO@XIST": UnsupportedMarketError("XIST is not served")})
    orch = make_orchestrator(
        tmp_path, SourceBindings(history={"yahoo_chart": history}), master=_thyao_master(tmp_path)
    )
    try:
        report = await orch.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M8, end=M8, sources=("yahoo_chart",)
        )
        assert (
            report.unservable_by_listing()["THYAO@XIST"]["yahoo_chart"] == "UnsupportedMarketError"
        )
    finally:
        await orch.aclose()


async def test_a_transient_error_is_retried_then_reported_without_advancing_the_watermark(
    tmp_path: Path,
) -> None:
    history = FakeHistorySource(raises={"THYAO@XIST": TransientProviderError("HTTP 503")})
    delays: list[float] = []

    async def record_sleep(seconds: float) -> None:
        delays.append(seconds)

    orch = make_orchestrator(
        tmp_path,
        SourceBindings(history={"yahoo_chart": history}),
        master=_thyao_master(tmp_path),
        sleep=record_sleep,
    )
    try:
        report = await orch.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M8, end=M8, sources=("yahoo_chart",)
        )
        assert len(history.calls) == 3
        assert len(delays) == 2
        assert [o.status for o in report.outcomes] == [OutcomeStatus.TRANSIENT_ERROR]
        assert _covered(orch, "yahoo_chart", "THYAO@XIST", "ohlcv", "1d") is None
    finally:
        await orch.aclose()


async def test_no_data_on_a_first_fetch_does_not_advance_either_channel(tmp_path: Path) -> None:
    history = FakeHistorySource()
    orch = make_orchestrator(
        tmp_path, SourceBindings(history={"yahoo_chart": history}), master=_thyao_master(tmp_path)
    )
    try:
        report = await orch.backfill(
            Scope(mic="XIST"), ("ohlcv", "corp_action"), start=M8, end=M8, sources=("yahoo_chart",)
        )
        assert {(o.channel, o.status) for o in report.outcomes} == {
            ("ohlcv", OutcomeStatus.NO_DATA),
            ("corp_action", OutcomeStatus.NO_DATA),
        }
        assert _covered(orch, "yahoo_chart", "THYAO@XIST", "ohlcv", "1d") is None
        assert _covered(orch, "yahoo_chart", "THYAO@XIST", "corp_action", "") is None
    finally:
        await orch.aclose()


async def test_an_unexpected_error_is_isolated_to_its_listing(tmp_path: Path) -> None:
    master = ListingMaster.for_data_dir(tmp_path)
    seed_master(
        master,
        [
            make_observation(
                "tradingview", f"BIST:{local}", "XIST", local, currency="TRY", price_scale=0
            )
            for local in ("AAA", "BBB")
        ],
    )
    history = FakeHistorySource(
        bars={"AAA@XIST": _series(M8), "BBB@XIST": _series(M8)},
        raises={"AAA@XIST": RuntimeError("boom")},
    )
    orch = make_orchestrator(
        tmp_path, SourceBindings(history={"yahoo_chart": history}), master=master
    )
    try:
        report = await orch.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M8, end=M8, sources=("yahoo_chart",)
        )
        assert [o.status for o in report.market_outcomes("AAA@XIST")] == [OutcomeStatus.FAILED]
        assert "RuntimeError: boom" in (report.market_outcomes("AAA@XIST")[0].reason or "")
        assert [o.status for o in report.market_outcomes("BBB@XIST")] == [OutcomeStatus.OK]
    finally:
        await orch.aclose()


async def test_concurrency_never_exceeds_the_bound(tmp_path: Path) -> None:
    master = ListingMaster.for_data_dir(tmp_path)
    locals_ = [f"L{index:02d}" for index in range(10)]
    seed_master(
        master,
        [
            make_observation(
                "tradingview", f"BIST:{name}", "XIST", name, currency="TRY", price_scale=0
            )
            for name in locals_
        ],
    )
    history = FakeHistorySource(bars={f"{name}@XIST": _series(M8) for name in locals_}, delay=0.01)
    orch = make_orchestrator(
        tmp_path,
        SourceBindings(history={"yahoo_chart": history}),
        master=master,
        max_concurrency=3,
    )
    try:
        await orch.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M8, end=M8, sources=("yahoo_chart",)
        )
        assert history.max_in_flight == 3
        assert len(history.calls) == 10
    finally:
        await orch.aclose()


async def test_a_record_whose_currency_contradicts_its_listing_goes_to_the_dead_letter_queue(
    tmp_path: Path,
) -> None:
    history = FakeHistorySource(
        bars={"THYAO@XIST": _series(M4, M5)},
        currency={"THYAO@XIST": ("USD", 0)},
    )
    sink = RecordingSink()
    orch = make_orchestrator(
        tmp_path,
        SourceBindings(history={"yahoo_chart": history}),
        master=_thyao_master(tmp_path),
        sink=sink,
    )
    try:
        report = await orch.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M4, end=M5, sources=("yahoo_chart",)
        )
        assert not [r for r in sink.records if isinstance(r, OHLCV)]
        assert report.dead_letters == 2
    finally:
        await orch.aclose()


async def test_a_record_for_another_symbol_is_dropped(tmp_path: Path) -> None:
    class _Stray(FakeHistorySource):
        async def _history(self, listing, channel, start_ns, end_ns, captured_ns):  # type: ignore[no-untyped-def]
            yield daily_bar(self.name, "OTHER@XIST", M8, 9.0, captured_ns=captured_ns)
            yield daily_bar(self.name, listing.symbol, M8, 10.0, captured_ns=captured_ns)

    sink = RecordingSink()
    orch = make_orchestrator(
        tmp_path,
        SourceBindings(history={"yahoo_chart": _Stray()}),
        master=_thyao_master(tmp_path),
        sink=sink,
    )
    try:
        await orch.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M8, end=M8, sources=("yahoo_chart",)
        )
        assert [r.symbol for r in sink.records] == ["THYAO@XIST"]
    finally:
        await orch.aclose()


async def test_the_watermark_is_committed_only_after_the_sink_is_flushed(tmp_path: Path) -> None:
    events: list[tuple[str, ...]] = []

    class _Logged(UniversalState):
        def record_success(self, **kwargs: object) -> Coverage:  # type: ignore[override]
            events.append(("commit", str(kwargs["symbol"])))
            return super().record_success(**kwargs)  # type: ignore[arg-type]

    history = FakeHistorySource(bars={"THYAO@XIST": _series(M8)})
    orch = make_orchestrator(
        tmp_path,
        SourceBindings(history={"yahoo_chart": history}),
        master=_thyao_master(tmp_path),
        state=_Logged(UniversalState.path_for(tmp_path)),
        sink=RecordingSink(events),
    )
    try:
        await orch.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M8, end=M8, sources=("yahoo_chart",)
        )
        flush_at = events.index(("flush",))
        commit_at = events.index(("commit", "THYAO@XIST"))
        assert events.index(("put", "THYAO@XIST")) < flush_at < commit_at
    finally:
        await orch.aclose()


async def test_the_run_report_is_persisted_and_lists_every_listing_outcome(tmp_path: Path) -> None:
    history = FakeHistorySource(bars={"THYAO@XIST": _series(M8)})
    orch = make_orchestrator(
        tmp_path, SourceBindings(history={"yahoo_chart": history}), master=_thyao_master(tmp_path)
    )
    try:
        report = await orch.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M8, end=M8, sources=("yahoo_chart",)
        )
    finally:
        await orch.aclose()
    assert report.report_path is not None
    path = tmp_path / "state" / "runs" / f"{report.run_id}.json"
    assert Path(report.report_path) == path
    decoded = msgspec.json.decode(path.read_bytes(), type=RunReport)
    assert decoded.run_id == report.run_id
    assert [o.status for o in decoded.outcomes] == [o.status for o in report.outcomes]


async def test_explicit_sources_restrict_the_chain_and_universal_expands_it_per_market(
    tmp_path: Path,
) -> None:
    yahoo = FakeHistorySource(bars={"THYAO@XIST": _series(M8)})
    other = FakeHistorySource(name="tradingview", bars={"THYAO@XIST": _series(M8)})
    orch = make_orchestrator(
        tmp_path,
        SourceBindings(history={"yahoo_chart": yahoo, "tradingview": other}),
        master=_thyao_master(tmp_path),
    )
    try:
        await orch.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M8, end=M8, sources=("tradingview",)
        )
        assert yahoo.calls == []
        assert len(other.calls) == 1
        await orch.backfill(Scope(mic="XIST"), ("ohlcv",), start=M8, end=M8, sources=("universal",))
        assert len(yahoo.calls) == 1
        assert len(other.calls) == 1
    finally:
        await orch.aclose()


async def test_instrument_is_not_a_backfill_channel(tmp_path: Path) -> None:
    orch = make_orchestrator(tmp_path, SourceBindings(), master=_thyao_master(tmp_path))
    try:
        with pytest.raises(ValueError, match="universe-sync"):
            await orch.backfill(Scope(mic="XIST"), ("instrument",), start=M8, end=M8)
    finally:
        await orch.aclose()


async def test_a_native_source_name_is_refused(tmp_path: Path) -> None:
    orch = make_orchestrator(tmp_path, SourceBindings(), master=_thyao_master(tmp_path))
    try:
        with pytest.raises(ValueError, match="tiingo"):
            await orch.backfill(
                Scope(mic="XIST"), ("ohlcv",), start=M8, end=M8, sources=("tiingo",)
            )
    finally:
        await orch.aclose()


class _WalledError(Exception):
    """A source error carrying a generic ``reason`` attribute (the table's last generic row)."""

    reason = "rate_limited"


async def _no_sleep(seconds: float) -> None:
    return None


_CLASSIFICATION_ROWS = [
    pytest.param(
        SanctionedMarketError("XIST", "TR", "test"),
        OutcomeStatus.SANCTIONED,
        "market XIST (TR) is sanctioned and default-closed: test",
        id="sanctioned",
    ),
    pytest.param(
        UnsupportedMarketError("XIST is not served"),
        OutcomeStatus.UNSERVABLE,
        "UnsupportedMarketError",
        id="unsupported-market",
    ),
    pytest.param(
        NoGrammarForMarket("yahoo_chart", "XIST"),
        OutcomeStatus.UNSERVABLE,
        "NoGrammarForMarket",
        id="no-grammar",
    ),
    pytest.param(
        UnknownMarketError("XIST"),
        OutcomeStatus.UNSERVABLE,
        "UnknownMarketError",
        id="unknown-market",
    ),
    pytest.param(_WalledError(), OutcomeStatus.UNSERVABLE, "rate_limited", id="generic-reason"),
    pytest.param(
        NotImplementedError("no daily bars"),
        OutcomeStatus.UNSERVABLE,
        "not_supported_by_source",
        id="not-implemented",
    ),
    pytest.param(TimeoutError("read timed out"), OutcomeStatus.UNSERVABLE, "timeout", id="timeout"),
    pytest.param(
        ListingConflictError("THYAO@XIST", "ISIN-A", "ISIN-B"),
        OutcomeStatus.IDENTITY_CONFLICT,
        "canonical symbol 'THYAO@XIST' already identifies ISIN-A; refusing to point it at ISIN-B",
        id="identity-conflict",
    ),
    pytest.param(
        TransientProviderError("HTTP 503"),
        OutcomeStatus.TRANSIENT_ERROR,
        "HTTP 503",
        id="transient-provider",
    ),
    pytest.param(
        TransientConnectorError("connection reset"),
        OutcomeStatus.TRANSIENT_ERROR,
        "connection reset",
        id="transient-connector",
    ),
    pytest.param(RuntimeError("boom"), OutcomeStatus.FAILED, "RuntimeError: boom", id="unexpected"),
]


@pytest.mark.parametrize(("error", "status", "reason"), _CLASSIFICATION_ROWS)
async def test_each_classification_row_reaches_the_report_with_its_status_and_reason(
    tmp_path: Path, error: Exception, status: OutcomeStatus, reason: str
) -> None:
    history = FakeHistorySource(raises={"THYAO@XIST": error})
    orch = make_orchestrator(
        tmp_path,
        SourceBindings(history={"yahoo_chart": history}),
        master=_thyao_master(tmp_path),
        sleep=_no_sleep,
    )
    try:
        report = await orch.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M8, end=M8, sources=("yahoo_chart",)
        )
    finally:
        await orch.aclose()
    assert [(o.subject, o.status, o.reason) for o in report.outcomes] == [
        ("THYAO@XIST", status, reason)
    ]


async def test_a_market_no_history_source_serves_is_reported_no_source(tmp_path: Path) -> None:
    history = FakeHistorySource(mics=frozenset({"XNAS"}), bars={"THYAO@XIST": _series(M8)})
    orch = make_orchestrator(
        tmp_path, SourceBindings(history={"yahoo_chart": history}), master=_thyao_master(tmp_path)
    )
    try:
        report = await orch.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M8, end=M8, sources=("yahoo_chart",)
        )
    finally:
        await orch.aclose()
    assert history.calls == []
    assert [(o.channel, o.status) for o in report.outcomes] == [("ohlcv", OutcomeStatus.NO_SOURCE)]


async def test_an_all_markets_backfill_reports_a_refused_sanctioned_listing(tmp_path: Path) -> None:
    master = _thyao_master(tmp_path)
    refused_symbol = f"FOO@{SANCTIONED_MIC}"
    seed_master(
        master,
        [make_observation("tradingview", f"{SANCTIONED_MIC}:FOO", SANCTIONED_MIC, "FOO")],
    )
    history = FakeHistorySource(bars={"THYAO@XIST": _series(M8)})
    orch = make_orchestrator(
        tmp_path, SourceBindings(history={"yahoo_chart": history}), master=master
    )
    try:
        report = await orch.backfill(
            Scope(all_markets=True), ("ohlcv",), start=M8, end=M8, sources=("yahoo_chart",)
        )
    finally:
        await orch.aclose()
    refused = [o for o in report.outcomes if o.subject == refused_symbol]
    assert [(o.channel, o.status, o.source) for o in refused] == [
        ("ohlcv", OutcomeStatus.SANCTIONED, None)
    ]
    assert {call[0] for call in history.calls} == {"THYAO@XIST"}


async def test_an_unknown_symbol_is_reported_as_unknown_listing_by_backfill(tmp_path: Path) -> None:
    history = FakeHistorySource(bars={"THYAO@XIST": _series(M8)})
    orch = make_orchestrator(
        tmp_path, SourceBindings(history={"yahoo_chart": history}), master=_thyao_master(tmp_path)
    )
    try:
        report = await orch.backfill(
            Scope(symbols=("THYAO@XIST", "NOPE@XIST")),
            ("ohlcv",),
            start=M8,
            end=M8,
            sources=("yahoo_chart",),
        )
    finally:
        await orch.aclose()
    assert {o.subject: o.status for o in report.outcomes} == {
        "THYAO@XIST": OutcomeStatus.OK,
        "NOPE@XIST": OutcomeStatus.UNKNOWN_LISTING,
    }


async def test_an_incremental_fetch_with_nothing_new_is_ok_with_no_new_sessions(
    tmp_path: Path,
) -> None:
    master = _thyao_master(tmp_path)
    first = FakeHistorySource(bars={"THYAO@XIST": _series(M4, M5)})
    orch = make_orchestrator(
        tmp_path, SourceBindings(history={"yahoo_chart": first}), master=master
    )
    try:
        await orch.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M4, end=M5, sources=("yahoo_chart",)
        )
    finally:
        await orch.aclose()
    second = FakeHistorySource(bars={"THYAO@XIST": _series(M4, M5)})
    resumed = make_orchestrator(
        tmp_path,
        SourceBindings(history={"yahoo_chart": second}),
        master=ListingMaster.for_data_dir(tmp_path),
    )
    try:
        report = await resumed.backfill(
            Scope(mic="XIST"), ("ohlcv",), start=M4, end=M8, sources=("yahoo_chart",)
        )
    finally:
        await resumed.aclose()
    assert second.calls == [("THYAO@XIST", "ohlcv", _midnight(M6), _midnight(M8))]
    (outcome,) = report.outcomes
    assert (outcome.status, outcome.reason, outcome.records) == (
        OutcomeStatus.OK,
        "no new sessions",
        0,
    )
