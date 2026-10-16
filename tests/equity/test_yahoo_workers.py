"""Offline regressions for Yahoo work on a shared live-data event loop."""

import asyncio
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pandas as pd
import pytest
import requests
from yfinance.exceptions import YFRateLimitError

from crocodile.core.schema.enums import AssetClass
from crocodile.core.schema.records import OHLCV
from crocodile.equity.providers.yahoo import client as yahoo


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Yahoo worker regression attempted network I/O")

    monkeypatch.setattr(requests.Session, "request", forbidden)
    try:
        from curl_cffi.requests import Session
    except ImportError:
        pass
    else:
        monkeypatch.setattr(Session, "request", forbidden)


@pytest.fixture
def real_yf_data(monkeypatch):
    """Use the actual singleton and get binding, replacing only HTTP dispatch."""
    from yfinance.data import YfData

    data = YfData()
    original_session = data._session

    def make_request(self, url, request_method, **kwargs):
        assert self is data
        return request_method(url=url)

    monkeypatch.setattr(YfData, "_make_request", make_request)
    yield data
    with yahoo._YF_WORKER_LOCK:
        data._set_session(original_session)


def _client():
    limiter = SimpleNamespace(acquire=AsyncMock(), update_backoff=Mock())
    return yahoo.YahooClient(rate_limiter=limiter, backoff_delay=0.001)


async def _wait_thread_event(event):
    assert await asyncio.to_thread(event.wait, 2.0)


def _retry_error(kind):
    if kind == "yfinance":
        return YFRateLimitError()
    if kind == "http":
        response = requests.Response()
        response.status_code = 429
        return requests.HTTPError(response=response)
    return RuntimeError("invalid crumb")


@pytest.mark.parametrize("kind", ["yfinance", "http", "crumb"])
@pytest.mark.parametrize("lock_kind", ["cookie", "singleton"])
async def test_retry_waits_for_reset_lock_while_heartbeat_runs(monkeypatch, kind, lock_kind):
    client = _client()
    old_session = client.session
    cookie_lock = threading.Lock()
    held_lock = cookie_lock if lock_kind == "cookie" else threading.Lock()
    held_lock.acquire()
    data = SimpleNamespace(_crumb="old", _cookie="old", _cookie_lock=cookie_lock)

    def yf_data():
        if lock_kind == "singleton":
            with held_lock:
                return data
        return data

    monkeypatch.setattr("yfinance.data.YfData", yf_data)
    reset_started = threading.Event()
    heartbeat = threading.Event()
    heartbeat_before_release = []
    original_create = client._create_session

    def create_session():
        reset_started.set()
        return original_create()

    monkeypatch.setattr(client, "_create_session", create_session)

    # A watchdog releases the real held lock even under the old blocking code.
    # Correctness is the event ordering, not a permissive elapsed-time limit.
    def release_reset_lock():
        heartbeat_before_release.append(heartbeat.wait(2.0))
        held_lock.release()

    watchdog = threading.Thread(target=release_reset_lock)
    watchdog.start()
    sessions = []

    def call(session):
        sessions.append(session)
        if len(sessions) == 1:
            raise _retry_error(kind)
        assert data._crumb is None and data._cookie is None
        assert heartbeat_before_release == [True]
        return "ok"

    task = asyncio.create_task(client._execute_yf_call(call))
    try:
        await _wait_thread_event(reset_started)
        await asyncio.sleep(0)  # independent heartbeat turn while reset waits
        assert held_lock.locked() and len(sessions) == 1
        heartbeat.set()
        assert await task == "ok"
        assert sessions == [old_session, client.session]
        assert client.session is not old_session
        assert client.rate_limiter.update_backoff.call_args_list[0].args == (0.001,)
    finally:
        heartbeat.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(watchdog.join)
        await client.close()


@pytest.mark.parametrize("kind", ["yfinance", "http", "crumb"])
async def test_retry_count_backoff_and_single_reset_are_preserved(monkeypatch, kind):
    client = _client()
    client.backoff_delay = 0.25
    events = []
    resets = Mock(side_effect=lambda: events.append("reset"))
    monkeypatch.setattr(client, "reset_session", resets)
    sleep = AsyncMock(side_effect=lambda delay: events.append(("sleep", delay)))
    monkeypatch.setattr(yahoo.asyncio, "sleep", sleep)
    client.rate_limiter.update_backoff.side_effect = lambda delay: events.append(("backoff", delay))

    def failed_call(session):
        events.append("call")
        raise _retry_error(kind)

    call = Mock(side_effect=failed_call)
    try:
        with pytest.raises(RuntimeError, match="failed after 5 attempts"):
            await client._execute_yf_call(call)
        assert call.call_count == resets.call_count == 5
        delays = [0.25, 0.5, 1.0, 2.0, 4.0]
        assert [c.args[0] for c in sleep.call_args_list] == delays
        assert [c.args[0] for c in client.rate_limiter.update_backoff.call_args_list] == delays
        assert events == [
            event
            for delay in delays
            for event in ("call", ("backoff", delay), "reset", ("sleep", delay))
        ]
    finally:
        await client.close()


async def test_cancelled_request_retains_session_until_worker_finishes(monkeypatch):
    client = _client()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    closed = threading.Event()
    reset = Mock()
    monkeypatch.setattr(client, "reset_session", reset)
    original_close = client.session.close

    def close_session():
        assert finished.is_set()
        closed.set()
        original_close()

    monkeypatch.setattr(client.session, "close", close_session)

    def call(session):
        started.set()
        assert release.wait(2.0)
        assert not closed.is_set()
        finished.set()
        raise YFRateLimitError()  # cancellation wins; do not reset or retry

    task = asyncio.create_task(client._execute_yf_call(call))
    await _wait_thread_event(started)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # repeated cancellation must not abandon worker ownership
    close_task = asyncio.create_task(client.close())
    try:
        await asyncio.sleep(0)
        assert not task.done() and not close_task.done() and not closed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await close_task
        assert finished.is_set() and closed.is_set()
        reset.assert_not_called()
    finally:
        release.set()
        await asyncio.gather(task, close_task, return_exceptions=True)


async def test_cancelled_reset_finishes_before_next_same_client_call(monkeypatch):
    client = _client()
    cookie_lock = threading.Lock()
    cookie_lock.acquire()
    data = SimpleNamespace(_crumb="old", _cookie="old", _cookie_lock=cookie_lock)
    monkeypatch.setattr("yfinance.data.YfData", lambda: data)
    reset_started = threading.Event()
    new_session = requests.Session()
    monkeypatch.setattr(client, "_create_session", lambda: (reset_started.set(), new_session)[1])
    first_call = Mock(side_effect=YFRateLimitError())
    task = asyncio.create_task(client._execute_yf_call(first_call))
    await _wait_thread_event(reset_started)
    task.cancel()
    second_started = threading.Event()
    closed = threading.Event()
    original_close = new_session.close

    def close_session():
        assert data._crumb is None
        closed.set()
        original_close()

    monkeypatch.setattr(new_session, "close", close_session)

    def second_call(session):
        assert session is new_session and data._crumb is None
        assert not closed.is_set()
        second_started.set()
        return "second"

    second = asyncio.create_task(client._execute_yf_call(second_call))
    close_task = asyncio.create_task(client.close())
    try:
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not second_started.is_set() and not closed.is_set()
        cookie_lock.release()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await second == "second"
        await close_task
        assert closed.is_set() and first_call.call_count == 1
    finally:
        if cookie_lock.locked():
            cookie_lock.release()
        await asyncio.gather(task, second, close_task, return_exceptions=True)


async def test_sync_reset_cannot_close_session_in_use_by_worker(monkeypatch):
    client = _client()
    started = threading.Event()
    release = threading.Event()
    close = Mock(wraps=client.session.close)
    monkeypatch.setattr(client.session, "close", close)
    monkeypatch.setattr("yfinance.data.YfData", lambda: SimpleNamespace())

    def call(session):
        started.set()
        assert release.wait(2.0)
        close.assert_not_called()

    task = asyncio.create_task(client._execute_yf_call(call))
    await _wait_thread_event(started)
    reset = asyncio.create_task(asyncio.to_thread(client.reset_session))
    try:
        await asyncio.sleep(0)
        assert not reset.done()
        close.assert_not_called()
        release.set()
        await asyncio.gather(task, reset)
        close.assert_called_once()
    finally:
        release.set()
        await asyncio.gather(task, reset, return_exceptions=True)
        await client.close()


@pytest.mark.parametrize("operation", ["request", "sync_reset", "close"])
async def test_real_singleton_cross_client_ownership(monkeypatch, real_yf_data, operation):
    a, b = _client(), _client()
    a_session, b_session = a.session, b.session
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    b_started = threading.Event()
    closed = []

    for session in (a_session, b_session):
        original_close = session.close

        def close(session=session, original_close=original_close):
            assert finished.is_set(), "no cross-client close while A owns YfData"
            closed.append(session)
            original_close()

        monkeypatch.setattr(session, "close", close)

    def a_get(**kwargs):
        assert real_yf_data._session is a_session
        started.set()
        assert release.wait(2.0), "bounded shared-gate watchdog"
        assert not closed and not b_started.is_set()
        assert real_yf_data._session is a_session
        finished.set()
        return SimpleNamespace(url="https://example.invalid/mock")

    def b_get(**kwargs):
        assert finished.is_set() and real_yf_data._session is b_session
        b_started.set()
        return SimpleNamespace(url="https://example.invalid/mock")

    monkeypatch.setattr(a_session, "get", a_get)
    monkeypatch.setattr(b_session, "get", b_get)

    def call(symbol, session):
        ticker = yahoo.yf.Ticker(symbol, session=session)
        assert ticker._data is real_yf_data
        return ticker._data.get("https://example.invalid/mock")

    task = asyncio.create_task(a._execute_yf_call(call, "A"))
    await _wait_thread_event(started)
    if operation == "request":
        other = asyncio.create_task(b._execute_yf_call(call, "B"))
    elif operation == "sync_reset":
        other = asyncio.create_task(asyncio.to_thread(b.reset_session))
    else:
        other = asyncio.create_task(b.close())
    try:
        await asyncio.sleep(0)  # heartbeat while another client waits at the gate
        assert not other.done() and not b_started.is_set() and not closed
        assert b.session is b_session and real_yf_data._session is a_session
        release.set()
        await asyncio.gather(task, other)
        if operation == "request":
            assert b_started.is_set()
        elif operation == "sync_reset":
            assert b.session is not b_session
            assert real_yf_data._crumb is None and real_yf_data._cookie is None
        else:
            assert closed == [b_session]
        await asyncio.gather(a.close(), b.close())
        default_ticker = yahoo.yf.Ticker("DEFAULT")  # actual no-explicit-session path
        assert default_ticker._data is real_yf_data
        assert real_yf_data._session not in closed
        default_ticker.session.close()  # this unused per-Ticker session is distinct
    finally:
        release.set()
        await asyncio.gather(task, other, return_exceptions=True)
        await asyncio.gather(a.close(), b.close())


@pytest.mark.parametrize("worker_error", [False, True])
async def test_real_singleton_cancelled_gate_wait_skips_io_and_drains_errors(
    monkeypatch, real_yf_data, worker_error
):
    a, b = _client(), _client()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    submitted = asyncio.Event()
    b_tickers = []
    closed = []
    original_worker = b._run_session_worker

    async def worker(func, **kwargs):
        submitted.set()
        return await original_worker(func, **kwargs)

    monkeypatch.setattr(b, "_run_session_worker", worker)
    for session in (a.session, b.session):
        original_close = session.close

        def close(session=session, original_close=original_close):
            assert finished.is_set()
            assert real_yf_data._session is not session
            closed.append(session)
            original_close()

        monkeypatch.setattr(session, "close", close)

    def get(**kwargs):
        assert real_yf_data._session is a.session
        started.set()
        assert release.wait(2.0), "bounded cancellation watchdog"
        assert not closed
        finished.set()
        if worker_error:
            raise OSError("offline worker failure")
        return SimpleNamespace(url="https://example.invalid/mock")

    monkeypatch.setattr(a.session, "get", get)

    def call_a(session):
        ticker = yahoo.yf.Ticker("A", session=session)
        assert ticker._data is real_yf_data
        return ticker._data.get("https://example.invalid/mock")

    def call_b(session):
        b_tickers.append(yahoo.yf.Ticker("B", session=session))
        raise AssertionError("cancelled gate waiter started a provider request")

    first = asyncio.create_task(a._execute_yf_call(call_a))
    await _wait_thread_event(started)
    second = asyncio.create_task(b._execute_yf_call(call_b))
    await asyncio.wait_for(submitted.wait(), 1.0)
    first.cancel()
    second.cancel()
    await asyncio.sleep(0)
    first.cancel()
    second.cancel()
    close = asyncio.gather(a.close(), b.close())
    try:
        await asyncio.sleep(0)  # cancelled workers still own their gate waits
        assert not first.done() and not second.done() and not close.done()
        assert not closed and not b_tickers
        release.set()
        for task in (first, second):
            with pytest.raises(asyncio.CancelledError):
                await task
        await close
        assert finished.is_set() and len(closed) == 2 and not b_tickers
        await asyncio.sleep(0)
        assert not b_tickers  # no provider work can appear after return
    finally:
        release.set()
        await asyncio.gather(first, second, close, return_exceptions=True)


@pytest.mark.parametrize("index_kind", ["utc", "new_york", "naive", "strings"])
async def test_intraday_worker_preserves_exact_records(monkeypatch, index_kind):
    timestamps = ["2026-06-16 14:31:00", "2026-06-16 14:30:00", "2026-06-16 14:32:00"]
    index = pd.to_datetime(timestamps)
    if index_kind == "utc":
        index = index.tz_localize("UTC")
    elif index_kind == "new_york":
        index = index.tz_localize("UTC").tz_convert("America/New_York")
    elif index_kind == "strings":
        index = pd.Index(timestamps)
    frame = pd.DataFrame(
        {
            "Open": ["1.5", 4.0, 3.0],
            "High": [2.0, 5.0, 4.0],
            "Low": [1.0, 3.0, 2.0],
            "Close": [1.8, float("nan"), 3.8],
            "Volume": [100, 200, "300"],
            "Dividends": [10, 20, 30],
        },
        index=index,
    )
    client = _client()
    monkeypatch.setattr(client, "_execute_yf_call", AsyncMock(return_value=frame))
    monkeypatch.setattr(yahoo.time, "time_ns", lambda: 123456789)
    try:
        got = await client.fetch_intraday_bars("aapl", "5m")
        expected = [
            OHLCV(
                source="yahoo",
                symbol="aapl",
                symbol_raw="aapl",
                source_ts=int(datetime(2026, 6, 16, 14, minute, tzinfo=UTC).timestamp() * 1e9),
                local_ts=123456789,
                interval="5m",
                open=open_px,
                high=high,
                low=low,
                close=close_px,
                volume=volume,
                vwap=None,
                num_trades=None,
                asset_class=AssetClass.EQUITY,
            )
            for minute, open_px, high, low, close_px, volume in [
                (31, 1.5, 2.0, 1.0, 1.8, 100.0),
                (32, 3.0, 4.0, 2.0, 3.8, 300.0),
            ]
        ]
        assert got == expected  # input order, every header/field, and skipped NaN row
    finally:
        await client.close()


async def test_complete_conversion_runs_off_loop_in_one_task(monkeypatch):
    client = _client()
    frame = pd.DataFrame(
        {"Open": [1.0], "High": [2.0], "Low": [0.5], "Close": [1.5], "Volume": [10]},
        index=pd.to_datetime(["2026-06-16 14:30:00"]),
    )
    monkeypatch.setattr(client, "_execute_yf_call", AsyncMock(return_value=frame))
    started = threading.Event()
    release = threading.Event()
    calls = []
    original = yahoo._intraday_records

    def conversion(*args):
        calls.append(threading.current_thread())
        started.set()
        assert release.wait(2.0)
        return original(*args)

    monkeypatch.setattr(yahoo, "_intraday_records", conversion)
    task = asyncio.create_task(client.fetch_intraday_bars("AAPL", "1m"))
    try:
        await _wait_thread_event(started)
        await asyncio.sleep(0)  # heartbeat completes before conversion can finish
        assert not task.done() and len(calls) == 1
        assert calls[0] is not threading.current_thread()
        release.set()
        assert len(await task) == 1
        assert len(calls) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.parametrize("field", ["Open", "High", "Low", "Close", "Volume"])
@pytest.mark.parametrize("invalid", [None, float("nan"), "not a number"])
async def test_intraday_skips_invalid_row_for_every_required_field(monkeypatch, field, invalid):
    client = _client()
    frame = pd.DataFrame(
        {name: [1.0, 2.0] for name in ("Open", "High", "Low", "Close", "Volume")},
        index=pd.to_datetime(["2026-06-16 14:30", "2026-06-16 14:31"]),
    )
    frame[field] = frame[field].astype(object)
    frame.loc[frame.index[0], field] = invalid
    monkeypatch.setattr(client, "_execute_yf_call", AsyncMock(return_value=frame))
    try:
        records = await client.fetch_intraday_bars("AAPL", "1m")
        assert len(records) == 1
        expected_ts = int(datetime(2026, 6, 16, 14, 31, tzinfo=UTC).timestamp() * 1e9)
        assert records[0].source_ts == expected_ts
        assert records[0].open == records[0].high == records[0].low == records[0].close == 2.0
        assert records[0].volume == 2.0
    finally:
        await client.close()


@pytest.mark.parametrize("healthy", [True, False])
async def test_empty_frame_health_behavior_does_not_schedule_conversion(monkeypatch, healthy):
    client = _client()
    monkeypatch.setattr(client, "_execute_yf_call", AsyncMock(return_value=pd.DataFrame()))
    health = AsyncMock(return_value=healthy)
    monkeypatch.setattr(client, "_check_health", health)
    converter = Mock(side_effect=AssertionError("empty frame must not be converted"))
    monkeypatch.setattr(yahoo, "_intraday_records", converter)
    try:
        if healthy:
            assert await client.fetch_intraday_bars("AAPL", "1m") == []
        else:
            with pytest.raises(yahoo.YahooError, match="integration is unhealthy"):
                await client.fetch_intraday_bars("AAPL", "1m")
        health.assert_awaited_once()
        converter.assert_not_called()
    finally:
        await client.close()
