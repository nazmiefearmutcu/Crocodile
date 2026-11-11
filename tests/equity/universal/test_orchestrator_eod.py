"""End-of-day and intraday collection of the universal orchestrator (task 14b, steps 9 and 11).

Offline throughout: every bar, observation and snapshot is built by ``fakes.py``. The EOD and
intraday tests live in their own module because ``test_orchestrator.py`` carries uncommitted work
of part 14a that this part must not commit.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from crocodile.core.markets.master import ListingMaster
from crocodile.core.schema.enums import AssetClass
from crocodile.core.schema.provenance import provenance_fields
from crocodile.core.schema.records import OHLCV
from crocodile.equity.providers.base import TransientProviderError
from crocodile.equity.universal import orchestrator as module
from crocodile.equity.universal.base import daily_bar_ts_ns
from crocodile.equity.universal.orchestrator import OutcomeStatus, Scope, SourceBindings
from crocodile.equity.universal.state import UniversalState
from tests.equity.universal.fakes import (
    FIXED_NOW,
    FIXED_NS,
    FakeHistorySource,
    FakeSnapshotSource,
    RecordingSink,
    daily_bar,
    make_observation,
    make_orchestrator,
    seed_master,
)

TURKEY_SESSION = datetime.date(2024, 3, 8)
NEW_YORK_SESSION = datetime.date(2024, 3, 7)
NS_PER_MINUTE = 60 * 1_000_000_000


def _bindings(
    *,
    history: tuple[FakeHistorySource, ...] = (),
    snapshot: tuple[FakeSnapshotSource, ...] = (),
) -> SourceBindings:
    return SourceBindings(
        history={source.name: source for source in history},
        snapshot={source.name: source for source in snapshot},
    )


def _thyao_master(tmp_path: Path, *extra: str) -> ListingMaster:
    master = ListingMaster.for_data_dir(tmp_path)
    observations = [
        make_observation(
            "tradingview", "BIST:THYAO", "XIST", "THYAO", currency="TRY", price_scale=0
        )
    ]
    for local in extra:
        observations.append(
            make_observation(
                "tradingview", f"BIST:{local}", "XIST", local, currency="TRY", price_scale=0
            )
        )
    seed_master(master, observations)
    return master


def _minute_bar(symbol: str, ts_ns: int, close: float) -> OHLCV:
    fields = provenance_fields("native")
    return OHLCV(
        source="yahoo_chart",
        symbol=symbol,
        symbol_raw=symbol,
        local_ts=FIXED_NS,
        asset_class=AssetClass.EQUITY,
        source_ts=ts_ns,
        interval="1m",
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1.0,
        currency="TRY",
        price_scale=0,
        prov=fields.prov,
        prov_basis=fields.prov_basis,
        prov_confidence=fields.prov_confidence,
        prov_inputs=fields.prov_inputs,
    )


class _NoCompletedSession:
    def latest_completed_session(self, now: datetime.datetime, delay: datetime.timedelta) -> None:
        return None


# ---------------------------------------------------------------- collect_eod (step 9)


async def test_collect_eod_snapshots_each_market_once_per_completed_session(tmp_path: Path) -> None:
    master = _thyao_master(tmp_path)
    snapshot = FakeSnapshotSource(
        "tradingview",
        units={"turkey": frozenset({"XIST"})},
        bars={
            "turkey": (
                daily_bar(
                    "tradingview",
                    "THYAO@XIST",
                    TURKEY_SESSION,
                    300.0,
                    currency="TRY",
                    price_scale=0,
                ),
            )
        },
    )
    orch = make_orchestrator(tmp_path, _bindings(snapshot=(snapshot,)), master=master)
    try:
        first = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
        second = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
    finally:
        await orch.aclose()

    assert [(unit, sessions) for unit, sessions, _ in snapshot.calls] == [
        ("turkey", {"XIST": TURKEY_SESSION})
    ]
    assert first.records_written == 1
    (xist,) = [o for o in first.market_outcomes("XIST") if o.status is OutcomeStatus.OK]
    assert xist.source == "tradingview"
    assert second.records_written == 0
    assert [o.status for o in second.market_outcomes("XIST")] == [OutcomeStatus.ALREADY_COVERED]


async def test_a_market_already_collected_for_its_latest_session_is_reported_and_not_fetched_again(
    tmp_path: Path,
) -> None:
    master = _thyao_master(tmp_path)
    state = UniversalState.at(tmp_path)
    state.record_eod(
        source="tradingview",
        mic="XIST",
        session=TURKEY_SESSION,
        at_ns=FIXED_NS,
        status="ok",
    )
    snapshot = FakeSnapshotSource("tradingview", units={"turkey": frozenset({"XIST"})})
    orch = make_orchestrator(tmp_path, _bindings(snapshot=(snapshot,)), master=master, state=state)
    try:
        report = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
    finally:
        await orch.aclose()

    assert snapshot.calls == []
    (outcome,) = report.market_outcomes("XIST")
    assert outcome.status is OutcomeStatus.ALREADY_COVERED
    assert outcome.source == "tradingview"
    assert outcome.reason == "ok"


async def test_a_session_without_bars_is_marked_no_data_and_not_resnapshotted(
    tmp_path: Path,
) -> None:
    master = _thyao_master(tmp_path)
    snapshot = FakeSnapshotSource("tradingview", units={"turkey": frozenset({"XIST"})})
    orch = make_orchestrator(tmp_path, _bindings(snapshot=(snapshot,)), master=master)
    try:
        first = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
        second = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
    finally:
        await orch.aclose()

    assert len(snapshot.calls) == 1
    (outcome,) = [o for o in first.market_outcomes("XIST") if o.source == "tradingview"]
    assert outcome.status is OutcomeStatus.NO_DATA
    marker = UniversalState.at(tmp_path).eod_marker("tradingview", "XIST")
    assert marker is not None
    assert (marker.session, marker.status) == (TURKEY_SESSION, "no_data")
    assert [o.reason for o in second.market_outcomes("XIST")] == ["no_data"]


async def test_a_market_whose_session_has_not_completed_is_left_out_of_a_shared_unit(
    tmp_path: Path,
) -> None:
    # The Istanbul session of 2024-03-08 closed at 15:00 UTC and the New York session of the same
    # day closes at 21:00 UTC; at 18:00 UTC the shared unit may stamp Istanbul with 2024-03-08 and
    # New York only with its own latest completed session, 2024-03-07, never with 2024-03-08.
    master = _thyao_master(tmp_path)
    seed_master(
        master,
        [
            make_observation(
                "tradingview", "NASDAQ:AAPL", "XNYS", "AAPL", currency="USD", price_scale=0
            )
        ],
    )
    snapshot = FakeSnapshotSource(
        "tradingview",
        units={"shared": frozenset({"XIST", "XNYS"})},
        bars={
            "shared": (
                daily_bar(
                    "tradingview",
                    "THYAO@XIST",
                    TURKEY_SESSION,
                    300.0,
                    currency="TRY",
                    price_scale=0,
                ),
                daily_bar(
                    "tradingview", "AAPL", NEW_YORK_SESSION, 170.0, currency="USD", price_scale=0
                ),
            )
        },
        mics_by_symbol={"AAPL": "XNYS"},
    )
    orch = make_orchestrator(tmp_path, _bindings(snapshot=(snapshot,)), master=master)
    try:
        report = await orch.collect_eod(Scope(symbols=("THYAO@XIST", "AAPL")), FIXED_NOW)
    finally:
        await orch.aclose()

    (_unit, sessions, _captured) = snapshot.calls[0]
    assert sessions == {"XIST": TURKEY_SESSION, "XNYS": NEW_YORK_SESSION}
    assert report.records_written == 2
    xnys = [o for o in report.market_outcomes("XNYS") if o.source == "tradingview"]
    assert [o.status for o in xnys] == [OutcomeStatus.OK]


async def test_a_market_with_no_completed_session_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "calendar_for", lambda mic: _NoCompletedSession())
    master = _thyao_master(tmp_path)
    snapshot = FakeSnapshotSource("tradingview", units={"turkey": frozenset({"XIST"})})
    orch = make_orchestrator(tmp_path, _bindings(snapshot=(snapshot,)), master=master)
    try:
        report = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
    finally:
        await orch.aclose()

    assert snapshot.calls == []
    (outcome,) = report.market_outcomes("XIST")
    assert outcome.status is OutcomeStatus.NO_SESSION


async def test_markets_without_a_snapshot_source_fall_back_to_a_bounded_history_batch(
    tmp_path: Path,
) -> None:
    names = ("ASELS", "GARAN", "KCHOL", "SISE", "THYAO")
    master = _thyao_master(tmp_path, "ASELS", "GARAN", "KCHOL", "SISE")
    history = FakeHistorySource(
        "yahoo_chart",
        bars={
            f"{local}@XIST": [(TURKEY_SESSION, 10.0 + index)] for index, local in enumerate(names)
        },
    )
    orch = make_orchestrator(
        tmp_path,
        _bindings(history=(history,)),
        master=master,
        eod_fallback_limit=2,
    )
    try:
        report = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
    finally:
        await orch.aclose()

    assert len(history.calls) == 2
    assert {symbol for symbol, _channel, _start, _end in history.calls} == {
        "ASELS@XIST",
        "GARAN@XIST",
    }
    budget = [o for o in report.outcomes if o.status is OutcomeStatus.BUDGET_EXHAUSTED]
    assert len(budget) == 3
    (market,) = [o for o in report.market_outcomes("XIST") if o.source == "yahoo_chart"]
    assert (market.status, market.channel) == (OutcomeStatus.OK, "ohlcv")
    marker = UniversalState.at(tmp_path).eod_marker("yahoo_chart", "XIST")
    assert marker is not None
    assert (marker.session, marker.status) == (TURKEY_SESSION, "ok")


async def test_the_fallback_batch_rotates_by_staleness_across_sessions(tmp_path: Path) -> None:
    master = _thyao_master(tmp_path, "ASELS", "GARAN", "KCHOL", "SISE")
    history = FakeHistorySource(
        "yahoo_chart",
        bars={
            "ASELS@XIST": [(TURKEY_SESSION, 1.0)],
            "GARAN@XIST": [(TURKEY_SESSION, 1.0)],
            "KCHOL@XIST": [(TURKEY_SESSION, 1.0)],
            "SISE@XIST": [(TURKEY_SESSION, 1.0)],
            "THYAO@XIST": [(TURKEY_SESSION, 1.0)],
        },
    )
    orch = make_orchestrator(
        tmp_path, _bindings(history=(history,)), master=master, eod_fallback_limit=2
    )
    try:
        await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
        history.calls.clear()
        next_session_now = datetime.datetime(2024, 3, 11, 18, 0, tzinfo=datetime.UTC)
        await orch.collect_eod(Scope(mic="XIST"), next_session_now)
    finally:
        await orch.aclose()

    assert {symbol for symbol, _c, _s, _e in history.calls} == {"KCHOL@XIST", "SISE@XIST"}


async def test_a_failed_snapshot_falls_back_to_history_and_retries_next_tick(
    tmp_path: Path,
) -> None:
    master = _thyao_master(tmp_path)
    snapshot = FakeSnapshotSource(
        "tradingview",
        units={"turkey": frozenset({"XIST"})},
        bars={
            "turkey": (
                daily_bar(
                    "tradingview",
                    "THYAO@XIST",
                    TURKEY_SESSION,
                    300.0,
                    currency="TRY",
                    price_scale=0,
                ),
            )
        },
        errors={"turkey": TimeoutError()},
    )
    history = FakeHistorySource(
        "yahoo_chart",
        bars={"THYAO@XIST": [(TURKEY_SESSION, 300.0)]},
        currency={"THYAO@XIST": ("TRY", 0)},
    )
    orch = make_orchestrator(
        tmp_path, _bindings(history=(history,), snapshot=(snapshot,)), master=master
    )
    try:
        first = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
        assert UniversalState.at(tmp_path).eod_marker("tradingview", "XIST") is None
        assert [o.source for o in first.market_outcomes("XIST")] == ["yahoo_chart"]
        del snapshot.errors["turkey"]
        second = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
    finally:
        await orch.aclose()

    assert len(snapshot.calls) == 2
    snapshot_marker = UniversalState.at(tmp_path).eod_marker("tradingview", "XIST")
    assert snapshot_marker is not None
    assert snapshot_marker.status == "ok"
    assert [o.source for o in second.market_outcomes("XIST")] == ["tradingview"]


async def test_a_market_the_snapshot_could_not_attribute_falls_back_to_history_and_keeps_no_marker(
    tmp_path: Path,
) -> None:
    master = _thyao_master(tmp_path)
    bar = daily_bar(
        "tradingview", "THYAO@XIST", TURKEY_SESSION, 300.0, currency="TRY", price_scale=0
    )
    snapshot = FakeSnapshotSource(
        "tradingview",
        units={"turkey": frozenset({"XIST"})},
        bars={"turkey": (bar,)},
        unattributed={"turkey": frozenset({"XIST"})},
    )
    history = FakeHistorySource("yahoo_chart", bars={"THYAO@XIST": [(TURKEY_SESSION, 300.0)]})
    orch = make_orchestrator(
        tmp_path, _bindings(history=(history,), snapshot=(snapshot,)), master=master
    )
    try:
        first = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
        assert UniversalState.at(tmp_path).eod_marker("tradingview", "XIST") is None
        not_attributable = [
            o for o in first.market_outcomes("XIST") if o.status is OutcomeStatus.NO_SESSION
        ]
        assert [o.reason for o in not_attributable] == ["not_attributable"]
        (fallback,) = [o for o in first.market_outcomes("XIST") if o.source == "yahoo_chart"]
        assert fallback.status is OutcomeStatus.OK
        assert [start for _s, _c, start, _e in history.calls] == [daily_bar_ts_ns(TURKEY_SESSION)]

        second = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
        assert len(snapshot.calls) == 1
        assert [o.status for o in second.market_outcomes("XIST") if o.source == "tradingview"] == [
            OutcomeStatus.NO_SESSION
        ]
        assert len(history.calls) == 1
    finally:
        await orch.aclose()

    third_master = ListingMaster.for_data_dir(tmp_path)
    third_state = UniversalState.at(tmp_path)
    fresh = make_orchestrator(
        tmp_path,
        _bindings(history=(history,), snapshot=(snapshot,)),
        master=third_master,
        state=third_state,
    )
    try:
        await fresh.collect_eod(Scope(mic="XIST"), FIXED_NOW)
    finally:
        await fresh.aclose()
    assert len(snapshot.calls) == 2


async def test_explicit_listings_filter_the_snapshot_rows(tmp_path: Path) -> None:
    master = _thyao_master(tmp_path, "ASELS")
    snapshot = FakeSnapshotSource(
        "tradingview",
        units={"turkey": frozenset({"XIST"})},
        bars={
            "turkey": (
                daily_bar(
                    "tradingview",
                    "THYAO@XIST",
                    TURKEY_SESSION,
                    300.0,
                    currency="TRY",
                    price_scale=0,
                ),
                daily_bar(
                    "tradingview", "ASELS@XIST", TURKEY_SESSION, 50.0, currency="TRY", price_scale=0
                ),
            )
        },
    )
    sink = RecordingSink()
    orch = make_orchestrator(tmp_path, _bindings(snapshot=(snapshot,)), master=master, sink=sink)
    try:
        report = await orch.collect_eod(Scope(symbols=("THYAO@XIST",)), FIXED_NOW)
    finally:
        await orch.aclose()

    assert [record.symbol for record in sink.records] == ["THYAO@XIST"]
    assert report.records_written == 1


# ---------------------------------------------------------------- collect_intraday (step 11)


async def test_collect_intraday_writes_only_closed_bars_newer_than_the_watermark(
    tmp_path: Path,
) -> None:
    master = _thyao_master(tmp_path)
    closed_at = int(FIXED_NS) - 2 * NS_PER_MINUTE  # 17:58 UTC: closed well before FIXED_NOW
    history = FakeHistorySource(
        "yahoo_chart",
        intraday_bars={
            "THYAO@XIST": [
                _minute_bar("THYAO@XIST", closed_at, 10.0),
                _minute_bar("THYAO@XIST", closed_at + NS_PER_MINUTE, 11.0),
                _minute_bar("THYAO@XIST", FIXED_NS, 12.0),  # 18:00 UTC: still open, not closed
            ]
        },
    )
    orch = make_orchestrator(tmp_path, _bindings(history=(history,)), master=master)
    try:
        first = await orch.collect_intraday(["THYAO@XIST"], "1m", now=FIXED_NOW)
        second = await orch.collect_intraday(["THYAO@XIST"], "1m", now=FIXED_NOW)
    finally:
        await orch.aclose()

    assert first.records_written == 2
    assert second.records_written == 0
    assert [o.status for o in second.outcomes] == [OutcomeStatus.ALREADY_COVERED]
    assert history.intraday_calls == [("THYAO@XIST", "1m"), ("THYAO@XIST", "1m")]


async def test_collect_intraday_refuses_more_listings_than_the_cap(tmp_path: Path) -> None:
    master = _thyao_master(tmp_path)
    history = FakeHistorySource("yahoo_chart")
    orch = make_orchestrator(tmp_path, _bindings(history=(history,)), master=master)
    try:
        with pytest.raises(ValueError, match="at most"):
            await orch.collect_intraday(
                [f"S{index}@XIST" for index in range(module.MAX_INTRADAY_LISTINGS + 1)],
                "1m",
                now=FIXED_NOW,
            )
    finally:
        await orch.aclose()
    assert history.intraday_calls == []


async def test_collect_intraday_refuses_an_interval_yahoo_does_not_serve(tmp_path: Path) -> None:
    master = _thyao_master(tmp_path)
    history = FakeHistorySource("yahoo_chart")
    orch = make_orchestrator(tmp_path, _bindings(history=(history,)), master=master)
    try:
        with pytest.raises(ValueError, match="2m"):
            await orch.collect_intraday(["THYAO@XIST"], "2m", now=FIXED_NOW)
    finally:
        await orch.aclose()
    assert history.intraday_calls == []


async def test_a_transient_snapshot_error_without_history_is_reported_not_raised(
    tmp_path: Path,
) -> None:
    master = _thyao_master(tmp_path)
    snapshot = FakeSnapshotSource(
        "tradingview",
        units={"turkey": frozenset({"XIST"})},
        errors={"turkey": TransientProviderError("busy")},
    )
    orch = make_orchestrator(tmp_path, _bindings(snapshot=(snapshot,)), master=master)
    try:
        report = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
    finally:
        await orch.aclose()
    (outcome,) = report.market_outcomes("XIST")
    assert outcome.status is OutcomeStatus.TRANSIENT_ERROR
    assert outcome.source == "tradingview"
    assert UniversalState.at(tmp_path).eod_marker("tradingview", "XIST") is None
