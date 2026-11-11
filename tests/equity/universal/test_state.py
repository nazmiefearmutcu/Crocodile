"""The universal watermark store: coverage unions, outcomes, EOD markers and schema checks.

Every test is offline; the store is a local SQLite file under pytest's tmp_path.
"""

from __future__ import annotations

import datetime
import logging
import sqlite3
from pathlib import Path

import pytest

from crocodile.equity.universal.state import (
    NS_PER_DAY,
    STATE_SCHEMA_VERSION,
    UniversalState,
    missing_ranges,
)

DAY = NS_PER_DAY
D0 = 1_700_000_000_000_000_000 - (1_700_000_000_000_000_000 % DAY)  # a UTC midnight
SESSION = datetime.date(2024, 3, 7)


def _success(state: UniversalState, lo: int, hi: int, *, interval: str = "1d", at: int = 1) -> None:
    state.record_success(
        source="yahoo_chart",
        symbol="THYAO@XIST",
        mic="XIST",
        channel="ohlcv",
        interval=interval,
        covered_from_ns=lo,
        covered_until_ns=hi,
        at_ns=at,
    )


def test_a_fresh_store_has_no_coverage_and_no_eod_marker() -> None:
    state = UniversalState()
    try:
        assert state.coverage("yahoo_chart", "THYAO@XIST", "ohlcv", "1d") is None
        assert state.eod_marker("yahoo_chart", "XIST") is None
        assert state.eod_session("yahoo_chart", "XIST") is None
        assert state.last_success_by_mic() == {}
    finally:
        state.close()


def test_success_extends_coverage_over_touching_ranges() -> None:
    state = UniversalState()
    try:
        _success(state, D0, D0 + 10 * DAY)
        # Starts exactly one day after the stored end: touching, so it merges.
        _success(state, D0 + 11 * DAY, D0 + 20 * DAY)
        coverage = state.coverage("yahoo_chart", "THYAO@XIST", "ohlcv", "1d")
        assert coverage is not None
        assert (coverage.covered_from_ns, coverage.covered_until_ns) == (D0, D0 + 20 * DAY)
        assert coverage.status == "ok"
        assert coverage.last_success_ns == 1
    finally:
        state.close()


def test_a_disjoint_success_leaves_the_gap_uncovered(caplog: pytest.LogCaptureFixture) -> None:
    state = UniversalState()
    try:
        _success(state, D0 + 10 * DAY, D0 + 20 * DAY)
        with caplog.at_level(logging.WARNING):
            _success(state, D0 + 40 * DAY, D0 + 50 * DAY)
        coverage = state.coverage("yahoo_chart", "THYAO@XIST", "ohlcv", "1d")
        assert coverage is not None
        assert (coverage.covered_from_ns, coverage.covered_until_ns) == (
            D0 + 10 * DAY,
            D0 + 20 * DAY,
        )
        assert any("disjoint" in record.getMessage() for record in caplog.records)
    finally:
        state.close()


def test_intraday_coverage_is_a_plain_union() -> None:
    state = UniversalState()
    try:
        _success(state, D0 + 10 * DAY, D0 + 20 * DAY, interval="1m")
        _success(state, D0 + 40 * DAY, D0 + 50 * DAY, interval="1m")
        coverage = state.coverage("yahoo_chart", "THYAO@XIST", "ohlcv", "1m")
        assert coverage is not None
        assert (coverage.covered_from_ns, coverage.covered_until_ns) == (
            D0 + 10 * DAY,
            D0 + 50 * DAY,
        )
    finally:
        state.close()


def test_a_non_success_outcome_leaves_coverage_untouched_and_counts_the_attempt() -> None:
    state = UniversalState()
    try:
        _success(state, D0, D0 + 5 * DAY)
        state.record_outcome(
            source="yahoo_chart",
            symbol="THYAO@XIST",
            mic="XIST",
            channel="ohlcv",
            interval="1d",
            status="transient_error",
            reason="HTTP 503",
            at_ns=7,
        )
        state.record_outcome(
            source="yahoo_chart",
            symbol="THYAO@XIST",
            mic="XIST",
            channel="ohlcv",
            interval="1d",
            status="unservable",
            reason="bot_challenge",
            at_ns=8,
        )
        coverage = state.coverage("yahoo_chart", "THYAO@XIST", "ohlcv", "1d")
        assert coverage is not None
        assert (coverage.covered_from_ns, coverage.covered_until_ns) == (D0, D0 + 5 * DAY)
        assert coverage.status == "unservable"
        assert coverage.reason == "bot_challenge"
        assert coverage.attempts == 2
        assert coverage.last_success_ns == 1
    finally:
        state.close()


def test_coverage_survives_reopening_the_file(tmp_path: Path) -> None:
    path = tmp_path / "state" / "universal.sqlite"
    state = UniversalState(path)
    _success(state, D0, D0 + 3 * DAY)
    state.record_eod(source="tradingview", mic="XIST", session=SESSION, at_ns=99, status="no_data")
    state.close()

    reopened = UniversalState(path)
    try:
        coverage = reopened.coverage("yahoo_chart", "THYAO@XIST", "ohlcv", "1d")
        assert coverage is not None
        assert coverage.covered_until_ns == D0 + 3 * DAY
        marker = reopened.eod_marker("tradingview", "XIST")
        assert marker is not None
        assert marker.session == SESSION
    finally:
        reopened.close()


def test_a_newer_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "universal.sqlite"
    state = UniversalState(path)
    state.close()
    with sqlite3.connect(str(path)) as conn:
        conn.execute("UPDATE schema_version SET version = ?", (STATE_SCHEMA_VERSION + 1,))
    with pytest.raises(RuntimeError) as info:
        UniversalState(path)
    message = str(info.value)
    assert str(STATE_SCHEMA_VERSION + 1) in message
    assert str(STATE_SCHEMA_VERSION) in message


def test_the_store_lives_under_the_data_dirs_state_folder(tmp_path: Path) -> None:
    assert UniversalState.path_for(tmp_path) == tmp_path / "state" / "universal.sqlite"
    state = UniversalState.at(tmp_path)
    state.close()
    assert (tmp_path / "state" / "universal.sqlite").is_file()


@pytest.mark.parametrize(
    ("req_from", "req_to", "covered", "expected"),
    [
        pytest.param(0, 10 * DAY, None, [(0, 10 * DAY)], id="none"),
        pytest.param(D0 + 5 * DAY, D0 + 15 * DAY, (D0, D0 + 20 * DAY), [], id="inside"),
        pytest.param(
            D0 - 10 * DAY,
            D0 + 15 * DAY,
            (D0, D0 + 20 * DAY),
            [(D0 - 10 * DAY, D0 - DAY)],
            id="head",
        ),
        pytest.param(
            D0 + 5 * DAY,
            D0 + 30 * DAY,
            (D0, D0 + 20 * DAY),
            [(D0 + 21 * DAY, D0 + 30 * DAY)],
            id="tail",
        ),
        pytest.param(
            D0 - 10 * DAY,
            D0 + 30 * DAY,
            (D0, D0 + 20 * DAY),
            [(D0 - 10 * DAY, D0 - DAY), (D0 + 21 * DAY, D0 + 30 * DAY)],
            id="both",
        ),
        pytest.param(0, 5 * DAY, (D0 + 10 * DAY, D0 + 20 * DAY), [(0, 5 * DAY)], id="before"),
        pytest.param(D0 + 10 * DAY, D0, None, [], id="inverted"),
    ],
)
def test_missing_ranges_returns_head_and_tail_outside_coverage(
    req_from: int,
    req_to: int,
    covered: tuple[int, int] | None,
    expected: list[tuple[int, int]],
) -> None:
    assert missing_ranges(req_from, req_to, covered) == expected


def test_last_success_by_mic_takes_the_latest_of_backfill_and_eod() -> None:
    state = UniversalState()
    try:
        _success(state, D0, D0 + DAY, at=100)
        state.record_eod(source="tradingview", mic="XIST", session=SESSION, at_ns=500)
        state.record_eod(source="yahoo_chart", mic="XIST", session=SESSION, at_ns=50)
        assert state.last_success_by_mic() == {"XIST": 500}
    finally:
        state.close()


def test_an_eod_marker_keeps_its_status() -> None:
    state = UniversalState()
    try:
        state.record_eod(
            source="tradingview", mic="XIST", session=SESSION, at_ns=5, status="no_data"
        )
        marker = state.eod_marker("tradingview", "XIST")
        assert marker is not None
        assert marker.status == "no_data"
        assert marker.updated_ns == 5
        assert state.eod_session("tradingview", "XIST") == SESSION
    finally:
        state.close()
