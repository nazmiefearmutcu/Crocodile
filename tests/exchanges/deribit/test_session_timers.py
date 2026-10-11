import asyncio
import json

import pytest

from crocodile.core.errors import FatalConnectorError, TransientConnectorError
from crocodile.crypto.exchanges.deribit.session import DeribitSession


def sent(transport, method):
    return [r for b in transport.sent if (r := json.loads(b))["method"] == method]


def make(clock, **kwargs):
    return DeribitSession(
        interval="raw",
        client_id="id",
        client_secret="secret",
        channels=("book.BTC-PERPETUAL.raw",),
        clock_ns=lambda: clock[0],
        **kwargs,
    )


def tokens(seconds=20, access="access", refresh="refresh"):
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": seconds,
        "token_type": "bearer",
    }


async def ready(session, transport, seconds=20):
    await session.begin(transport)
    await session.handle_control(
        {"id": sent(transport, "public/auth")[-1]["id"], "result": tokens(seconds)}
    )
    sub = sent(transport, "public/subscribe")[-1]
    await session.handle_control({"id": sub["id"], "result": sub["params"]["channels"]})
    for request in sent(transport, "public/set_heartbeat"):
        await session.handle_control({"id": request["id"], "result": "ok"})


@pytest.mark.parametrize("seconds", [True, 0, -1, "10", None, float("nan")])
async def test_bad_expiry_is_rejected(rpc_transport, seconds):
    session = make([0])
    await session.begin(rpc_transport)
    with pytest.raises(FatalConnectorError):
        await session.handle_control(
            {"id": sent(rpc_transport, "public/auth")[-1]["id"], "result": tokens(seconds)}
        )
    await session.close()


async def test_short_lifetime_refresh_and_atomic_token_replacement(rpc_transport):
    clock = [100]
    session = make(clock)
    await ready(session, rpc_transport)
    assert session._refresh_ns == 10_000_000_100
    clock[0] = session._refresh_ns
    await session.tick()
    refresh = sent(rpc_transport, "public/auth")[-1]
    assert refresh["params"] == {"grant_type": "refresh_token", "refresh_token": "refresh"}
    assert session.ready_for_market
    await session.handle_control(
        {"id": refresh["id"], "result": tokens(60, "new_access", "new_refresh")}
    )
    assert session._access_token == "new_access"
    assert session._refresh_token == "new_refresh"
    assert session._expires_ns == clock[0] + 60_000_000_000
    assert "new_access" not in repr(session)
    await session.close()
    assert session._access_token is session._refresh_token is None
    await session.begin(rpc_transport)
    assert sent(rpc_transport, "public/auth")[-1]["params"]["grant_type"] == "client_credentials"
    await session.close()


@pytest.mark.parametrize("reject", [False, True])
async def test_failed_refresh_invalidates_live_session(rpc_transport, reject):
    clock = [0]
    session = make(clock, request_timeout_seconds=2)
    await ready(session, rpc_transport)
    clock[0] = 10_000_000_000
    await session.tick()
    refresh = sent(rpc_transport, "public/auth")[-1]
    if reject:
        with pytest.raises(FatalConnectorError):
            await session.handle_control(
                {"id": refresh["id"], "error": {"code": 13009, "message": "secret-token"}}
            )
    else:
        clock[0] += 2_000_000_000
        with pytest.raises(TransientConnectorError):
            await session.tick()
    assert not session.ready_for_market
    assert session._refresh_token is None
    await session.close()


async def test_idle_test_request_and_out_of_order_acks(rpc_transport):
    session = make([0])
    await ready(session, rpc_transport)
    assert sent(rpc_transport, "public/set_heartbeat")[-1]["params"] == {"interval": 30}
    for _ in range(2):
        assert await session.handle_control(
            {"method": "heartbeat", "params": {"type": "test_request"}}
        )
    tests = sent(rpc_transport, "public/test")
    assert len(tests) == 2
    for request in reversed(tests):
        assert await session.handle_control({"id": request["id"], "result": {"version": "1"}})
        assert not await session.handle_control({"id": request["id"], "result": {"version": "1"}})
    assert session.ready_for_market
    await session.close()


async def test_close_cancels_watchdog_and_pending(rpc_transport):
    session = make([0])
    await session.begin(rpc_transport)
    task = asyncio.create_task(session._watchdog())
    await asyncio.sleep(0)
    await session.close()
    assert task.done()
    assert not session._pending
    assert session._refresh_token is None


async def test_utc_rollback_does_not_change_monotonic_expiry(rpc_transport, monkeypatch):
    clock = [0]
    session = make(clock)
    await ready(session, rpc_transport)
    deadline = session._expires_ns
    monkeypatch.setattr("time.time_ns", lambda: -100_000_000_000)
    clock[0] = 20_000_000_000
    assert session._expires_ns == deadline
    assert not session.ready_for_market
    with pytest.raises(TransientConnectorError, match="expired"):
        await session.tick()
    await session.close()
