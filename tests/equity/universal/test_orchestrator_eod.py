"""End-of-day and intraday collection of the universal orchestrator (task 14b, steps 9 and 11).

Offline throughout: every bar, observation and snapshot is built by ``fakes.py``. The EOD and
intraday tests live in their own module because ``test_orchestrator.py`` carries uncommitted work
of part 14a that this part must not commit.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from crocodile.core.markets.errors import CalendarRangeError, SanctionedMarketError
from crocodile.core.markets.exchanges import ExchangeRegistry
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
SANCTIONED_MIC: str = next(info.mic for info in ExchangeRegistry().all() if info.sanctioned)


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


# ---------------------------------------------------- failure classification (fix round 1)


async def _no_sleep(seconds: float) -> None:
    return None


class _FlakySnapshot:
    """A snapshot source that raises a transient error on its first ``failures`` attempts."""

    def __init__(self, inner: FakeSnapshotSource, failures: int) -> None:
        self.inner = inner
        self.name = inner.name
        self.failures = failures
        self.attempts = 0

    def units_for(self, mics: frozenset[str]) -> dict[str, frozenset[str]]:
        return self.inner.units_for(mics)

    async def snapshot(
        self, unit: str, sessions: dict[str, datetime.date], *, captured_ns: int
    ) -> object:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise TransientProviderError("busy")
        return await self.inner.snapshot(unit, sessions, captured_ns=captured_ns)


class _FailingSink(RecordingSink):
    """A sink whose ``put`` raises ``OSError`` for one symbol and stores every other record."""

    def __init__(self, failing_symbol: str) -> None:
        super().__init__()
        self.failing_symbol = failing_symbol

    async def put(self, record: object) -> None:
        if getattr(record, "symbol", None) == self.failing_symbol:
            raise OSError("disk full")
        await super().put(record)  # type: ignore[arg-type]


def _turkey_snapshot(**kw: object) -> FakeSnapshotSource:
    return FakeSnapshotSource(
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
        **kw,  # type: ignore[arg-type]
    )


async def test_a_transient_snapshot_error_that_clears_on_retry_still_yields_the_bars(
    tmp_path: Path,
) -> None:
    master = _thyao_master(tmp_path)
    flaky = _FlakySnapshot(_turkey_snapshot(), failures=1)
    orch = make_orchestrator(
        tmp_path,
        _bindings(snapshot=(flaky,)),
        master=master,
        sleep=_no_sleep,  # type: ignore[arg-type]
    )
    try:
        report = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
    finally:
        await orch.aclose()

    assert flaky.attempts == 2
    assert report.records_written == 1
    (outcome,) = [o for o in report.market_outcomes("XIST") if o.source == "tradingview"]
    assert outcome.status is OutcomeStatus.OK
    marker = UniversalState.at(tmp_path).eod_marker("tradingview", "XIST")
    assert marker is not None
    assert (marker.session, marker.status) == (TURKEY_SESSION, "ok")


async def test_a_transient_snapshot_error_on_every_attempt_makes_exactly_the_bounded_calls(
    tmp_path: Path,
) -> None:
    master = _thyao_master(tmp_path)
    flaky = _FlakySnapshot(_turkey_snapshot(), failures=10**6)
    orch = make_orchestrator(
        tmp_path,
        _bindings(snapshot=(flaky,)),
        master=master,
        sleep=_no_sleep,  # type: ignore[arg-type]
    )
    try:
        report = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
    finally:
        await orch.aclose()

    assert flaky.attempts == module.MAX_TRANSIENT_ATTEMPTS
    (outcome,) = report.market_outcomes("XIST")
    assert outcome.status is OutcomeStatus.TRANSIENT_ERROR
    assert outcome.source == "tradingview"
    assert UniversalState.at(tmp_path).eod_marker("tradingview", "XIST") is None


async def test_a_history_batch_whose_every_attempt_fails_writes_no_marker(tmp_path: Path) -> None:
    master = _thyao_master(tmp_path)
    history = FakeHistorySource(
        "yahoo_chart",
        bars={"THYAO@XIST": [(TURKEY_SESSION, 300.0)]},
        raises={"THYAO@XIST": TransientProviderError("busy")},
    )
    orch = make_orchestrator(
        tmp_path, _bindings(history=(history,)), master=master, sleep=_no_sleep
    )
    try:
        report = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
    finally:
        await orch.aclose()

    assert len(history.calls) == module.MAX_TRANSIENT_ATTEMPTS
    assert report.records_written == 0
    (outcome,) = [o for o in report.market_outcomes("XIST") if o.source == "yahoo_chart"]
    assert outcome.status is OutcomeStatus.TRANSIENT_ERROR
    assert UniversalState.at(tmp_path).eod_marker("yahoo_chart", "XIST") is None


async def test_a_sink_failure_on_one_unit_is_classified_and_the_other_units_still_run(
    tmp_path: Path,
) -> None:
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
        units={"turkey": frozenset({"XIST"}), "us": frozenset({"XNYS"})},
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
            ),
            "us": (
                daily_bar(
                    "tradingview", "AAPL", NEW_YORK_SESSION, 170.0, currency="USD", price_scale=0
                ),
            ),
        },
        mics_by_symbol={"AAPL": "XNYS"},
    )
    sink = _FailingSink("THYAO@XIST")
    orch = make_orchestrator(tmp_path, _bindings(snapshot=(snapshot,)), master=master, sink=sink)
    try:
        report = await orch.collect_eod(Scope(symbols=("THYAO@XIST", "AAPL")), FIXED_NOW)
    finally:
        await orch.aclose()

    (xist,) = report.market_outcomes("XIST")
    assert xist.status is OutcomeStatus.FAILED
    assert xist.source == "tradingview"
    assert xist.reason is not None and xist.reason.startswith("OSError")
    assert UniversalState.at(tmp_path).eod_marker("tradingview", "XIST") is None
    (xnys,) = [o for o in report.market_outcomes("XNYS") if o.source == "tradingview"]
    assert xnys.status is OutcomeStatus.OK
    assert [record.symbol for record in sink.records] == ["AAPL"]


async def test_a_sink_failure_on_a_history_fallback_write_is_classified_and_leaves_no_marker(
    tmp_path: Path,
) -> None:
    master = _thyao_master(tmp_path)
    history = FakeHistorySource("yahoo_chart", bars={"THYAO@XIST": [(TURKEY_SESSION, 300.0)]})
    orch = make_orchestrator(
        tmp_path,
        _bindings(history=(history,)),
        master=master,
        sink=_FailingSink("THYAO@XIST"),
    )
    try:
        report = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
    finally:
        await orch.aclose()

    (outcome,) = [o for o in report.market_outcomes("XIST") if o.source == "yahoo_chart"]
    assert outcome.status is OutcomeStatus.FAILED
    assert outcome.reason is not None and outcome.reason.startswith("OSError")
    assert UniversalState.at(tmp_path).eod_marker("yahoo_chart", "XIST") is None


async def test_a_sink_failure_on_intraday_bars_is_classified_and_leaves_no_watermark(
    tmp_path: Path,
) -> None:
    master = _thyao_master(tmp_path)
    closed_at = int(FIXED_NS) - 2 * NS_PER_MINUTE
    history = FakeHistorySource(
        "yahoo_chart",
        intraday_bars={"THYAO@XIST": [_minute_bar("THYAO@XIST", closed_at, 10.0)]},
    )
    orch = make_orchestrator(
        tmp_path,
        _bindings(history=(history,)),
        master=master,
        sink=_FailingSink("THYAO@XIST"),
    )
    try:
        report = await orch.collect_intraday(["THYAO@XIST"], "1m", now=FIXED_NOW)
    finally:
        await orch.aclose()

    (outcome,) = report.outcomes
    assert outcome.status is OutcomeStatus.FAILED
    assert outcome.reason is not None and outcome.reason.startswith("OSError")
    assert report.records_written == 0
    assert UniversalState.at(tmp_path).coverage("yahoo_chart", "THYAO@XIST", "ohlcv", "1m") is None


async def test_a_market_outside_its_calendar_bounds_is_reported_as_a_failed_calendar_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def out_of_range(mic: str) -> None:
        raise CalendarRangeError(mic, FIXED_NOW.date(), None, None)

    monkeypatch.setattr(module, "calendar_for", out_of_range)
    master = _thyao_master(tmp_path)
    snapshot = _turkey_snapshot()
    orch = make_orchestrator(tmp_path, _bindings(snapshot=(snapshot,)), master=master)
    try:
        report = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
    finally:
        await orch.aclose()

    assert snapshot.calls == []
    (outcome,) = report.market_outcomes("XIST")
    assert outcome.status is OutcomeStatus.FAILED
    assert outcome.reason == "calendar_range"


async def test_a_market_no_source_serves_is_reported_as_no_source(tmp_path: Path) -> None:
    master = _thyao_master(tmp_path)
    orch = make_orchestrator(tmp_path, _bindings(), master=master)
    try:
        report = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
    finally:
        await orch.aclose()

    (outcome,) = report.market_outcomes("XIST")
    assert outcome.status is OutcomeStatus.NO_SOURCE
    assert outcome.reason == "no source serves this market and channel"


async def test_collect_eod_refuses_a_sanctioned_market_without_asking_any_source(
    tmp_path: Path,
) -> None:
    master = _thyao_master(tmp_path)
    snapshot = _turkey_snapshot()
    orch = make_orchestrator(tmp_path, _bindings(snapshot=(snapshot,)), master=master)
    try:
        report = await orch.collect_eod(Scope(all_markets=True), FIXED_NOW)
    finally:
        await orch.aclose()

    refused = [o for o in report.outcomes if o.mic == SANCTIONED_MIC]
    assert [o.status for o in refused] == [OutcomeStatus.SANCTIONED] * len(refused)
    assert refused
    assert all(SANCTIONED_MIC not in sessions for _unit, sessions, _c in snapshot.calls)


async def test_collect_intraday_reports_no_data_when_the_source_has_no_bars(tmp_path: Path) -> None:
    master = _thyao_master(tmp_path)
    history = FakeHistorySource("yahoo_chart")
    orch = make_orchestrator(tmp_path, _bindings(history=(history,)), master=master)
    try:
        report = await orch.collect_intraday(["THYAO@XIST"], "1m", now=FIXED_NOW)
    finally:
        await orch.aclose()

    assert history.intraday_calls == [("THYAO@XIST", "1m")]
    (outcome,) = report.outcomes
    assert outcome.status is OutcomeStatus.NO_DATA
    assert outcome.source == "yahoo_chart"
    assert report.records_written == 0


async def test_collect_intraday_reports_an_unknown_listing_and_collects_the_known_one(
    tmp_path: Path,
) -> None:
    master = _thyao_master(tmp_path)
    closed_at = int(FIXED_NS) - 2 * NS_PER_MINUTE
    history = FakeHistorySource(
        "yahoo_chart",
        intraday_bars={"THYAO@XIST": [_minute_bar("THYAO@XIST", closed_at, 10.0)]},
    )
    orch = make_orchestrator(tmp_path, _bindings(history=(history,)), master=master)
    try:
        report = await orch.collect_intraday(["NOPE@XIST", "THYAO@XIST"], "1m", now=FIXED_NOW)
    finally:
        await orch.aclose()

    unknown = [o for o in report.outcomes if o.status is OutcomeStatus.UNKNOWN_LISTING]
    assert [(o.subject, o.reason) for o in unknown] == [("NOPE@XIST", "unknown listing")]
    assert report.records_written == 1
    assert [symbol for symbol, _interval in history.intraday_calls] == ["THYAO@XIST"]


async def test_collect_intraday_refuses_when_no_named_listing_is_known(tmp_path: Path) -> None:
    master = _thyao_master(tmp_path)
    history = FakeHistorySource("yahoo_chart")
    orch = make_orchestrator(tmp_path, _bindings(history=(history,)), master=master)
    try:
        with pytest.raises(ValueError, match="none of the named listings"):
            await orch.collect_intraday(["NOPE@XIST"], "1m", now=FIXED_NOW)
    finally:
        await orch.aclose()
    assert history.intraday_calls == []


async def test_collect_intraday_refuses_a_sanctioned_market_before_any_request(
    tmp_path: Path,
) -> None:
    master = _thyao_master(tmp_path)
    seed_master(
        master,
        [make_observation("tradingview", "XX:ABC", SANCTIONED_MIC, "ABC")],
    )
    history = FakeHistorySource("yahoo_chart")
    orch = make_orchestrator(tmp_path, _bindings(history=(history,)), master=master)
    try:
        with pytest.raises(SanctionedMarketError):
            await orch.collect_intraday([f"ABC@{SANCTIONED_MIC}"], "1m", now=FIXED_NOW)
    finally:
        await orch.aclose()
    assert history.intraday_calls == []


# ------------------------------------------- state writes classified per unit (fix round 2)


def _failing_marker(monkeypatch: pytest.MonkeyPatch, mic: str | None) -> None:
    """Make UniversalState.record_eod raise OSError for one MIC (every MIC when mic is None)."""
    original = UniversalState.record_eod

    def record_eod(self: UniversalState, **kwargs: object) -> None:
        if mic is None or kwargs["mic"] == mic:
            raise OSError("state marker write failed")
        original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(UniversalState, "record_eod", record_eod)


def _failing_outcome(monkeypatch: pytest.MonkeyPatch, symbol: str) -> None:
    """Make UniversalState.record_outcome raise OSError for one symbol."""
    original = UniversalState.record_outcome

    def record_outcome(self: UniversalState, **kwargs: object) -> None:
        if kwargs["symbol"] == symbol:
            raise OSError("state outcome write failed")
        original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(UniversalState, "record_outcome", record_outcome)


async def test_a_snapshot_marker_write_error_is_classified_and_the_other_units_still_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        units={"turkey": frozenset({"XIST"}), "us": frozenset({"XNYS"})},
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
            ),
            "us": (
                daily_bar(
                    "tradingview", "AAPL", NEW_YORK_SESSION, 170.0, currency="USD", price_scale=0
                ),
            ),
        },
        mics_by_symbol={"AAPL": "XNYS"},
    )
    sink = RecordingSink()
    _failing_marker(monkeypatch, "XIST")
    orch = make_orchestrator(tmp_path, _bindings(snapshot=(snapshot,)), master=master, sink=sink)
    try:
        report = await orch.collect_eod(Scope(symbols=("THYAO@XIST", "AAPL")), FIXED_NOW)
    finally:
        await orch.aclose()

    assert isinstance(report, module.RunReport)
    (xist,) = report.market_outcomes("XIST")
    assert xist.status is OutcomeStatus.FAILED
    assert xist.source == "tradingview"
    assert xist.reason is not None and xist.reason.startswith("OSError")
    (xnys,) = [o for o in report.market_outcomes("XNYS") if o.source == "tradingview"]
    assert xnys.status is OutcomeStatus.OK
    assert sorted(record.symbol for record in sink.records) == ["AAPL", "THYAO@XIST"]
    assert UniversalState.at(tmp_path).eod_marker("tradingview", "XNYS") is not None
    assert UniversalState.at(tmp_path).eod_marker("tradingview", "XIST") is None


async def test_a_fallback_marker_write_error_is_classified_and_leaves_no_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    master = _thyao_master(tmp_path)
    history = FakeHistorySource("yahoo_chart", bars={"THYAO@XIST": [(TURKEY_SESSION, 300.0)]})
    sink = RecordingSink()
    _failing_marker(monkeypatch, None)
    orch = make_orchestrator(tmp_path, _bindings(history=(history,)), master=master, sink=sink)
    try:
        report = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
    finally:
        await orch.aclose()

    assert isinstance(report, module.RunReport)
    (outcome,) = [o for o in report.market_outcomes("XIST") if o.source == "yahoo_chart"]
    assert outcome.status is OutcomeStatus.FAILED
    assert outcome.reason is not None and outcome.reason.startswith("OSError")
    assert outcome.records == 1
    assert UniversalState.at(tmp_path).eod_marker("yahoo_chart", "XIST") is None


async def test_an_intraday_outcome_write_error_is_classified_and_the_other_listings_still_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    master = _thyao_master(tmp_path, "GARAN")
    closed_at = int(FIXED_NS) - 2 * NS_PER_MINUTE
    history = FakeHistorySource(
        "yahoo_chart",
        intraday_bars={"GARAN@XIST": [_minute_bar("GARAN@XIST", closed_at, 10.0)]},
    )
    _failing_outcome(monkeypatch, "THYAO@XIST")
    orch = make_orchestrator(tmp_path, _bindings(history=(history,)), master=master)
    try:
        report = await orch.collect_intraday(["THYAO@XIST", "GARAN@XIST"], "1m", now=FIXED_NOW)
    finally:
        await orch.aclose()

    assert isinstance(report, module.RunReport)
    by_symbol = {outcome.subject: outcome for outcome in report.outcomes}
    assert by_symbol["THYAO@XIST"].status is OutcomeStatus.FAILED
    assert by_symbol["THYAO@XIST"].reason is not None
    assert by_symbol["THYAO@XIST"].reason.startswith("OSError")
    assert by_symbol["GARAN@XIST"].status is OutcomeStatus.OK
    assert report.records_written == 1


async def test_a_partly_failed_history_batch_writes_no_marker_and_the_next_call_fetches_again(
    tmp_path: Path,
) -> None:
    master = _thyao_master(tmp_path, "ASELS")
    flaky = FakeHistorySource(
        "yahoo_chart",
        bars={
            "ASELS@XIST": [(TURKEY_SESSION, 10.0)],
            "THYAO@XIST": [(TURKEY_SESSION, 300.0)],
        },
        raises={"THYAO@XIST": TransientProviderError("busy")},
    )
    orch = make_orchestrator(tmp_path, _bindings(history=(flaky,)), master=master, sleep=_no_sleep)
    try:
        report = await orch.collect_eod(Scope(mic="XIST"), FIXED_NOW)
    finally:
        await orch.aclose()

    assert {symbol for symbol, _channel, _start, _end in flaky.calls} == {
        "ASELS@XIST",
        "THYAO@XIST",
    }
    failed = [o for o in report.outcomes if o.status is OutcomeStatus.TRANSIENT_ERROR]
    assert [o.subject for o in failed] == ["THYAO@XIST"]
    assert UniversalState.at(tmp_path).eod_marker("yahoo_chart", "XIST") is None

    healed = FakeHistorySource(
        "yahoo_chart",
        bars={
            "ASELS@XIST": [(TURKEY_SESSION, 10.0)],
            "THYAO@XIST": [(TURKEY_SESSION, 300.0)],
        },
    )
    retry = make_orchestrator(
        tmp_path,
        _bindings(history=(healed,)),
        master=ListingMaster.for_data_dir(tmp_path),
        sleep=_no_sleep,
    )
    try:
        await retry.collect_eod(Scope(mic="XIST"), FIXED_NOW)
    finally:
        await retry.aclose()

    assert ("THYAO@XIST", "ohlcv") in {
        (symbol, channel) for symbol, channel, _s, _e in healed.calls
    }
    marker = UniversalState.at(tmp_path).eod_marker("yahoo_chart", "XIST")
    assert marker is not None
    assert (marker.session, marker.status) == (TURKEY_SESSION, "ok")
