import asyncio
import json

import pytest

from crocodile.core.errors import FatalConnectorError, TransientConnectorError
from crocodile.crypto.exchanges.deribit.session import DeribitSession

CHANNEL = "book.BTC-PERPETUAL.raw"


def make(**kwargs):
    return DeribitSession(
        interval="raw",
        client_id="ID_SENTINEL",
        client_secret="SECRET_SENTINEL",
        channels=(CHANNEL,),
        **kwargs,
    )


def requests(transport, method):
    return [r for b in transport.sent if (r := json.loads(b))["method"] == method]


async def authenticate(session, transport):
    auth = requests(transport, "public/auth")[-1]
    await session.handle_control(
        {
            "id": auth["id"],
            "result": {
                "access_token": "ACCESS_SENTINEL",
                "refresh_token": "REFRESH_SENTINEL",
                "expires_in": 900,
                "token_type": "bearer",
            },
        }
    )


async def test_auth_then_exact_subscribe_ack(rpc_transport):
    session = make()
    await asyncio.wait_for(session.begin(rpc_transport), 1)
    assert not requests(rpc_transport, "public/subscribe")
    assert not session.ready_for_market
    await authenticate(session, rpc_transport)
    (sub,) = requests(rpc_transport, "public/subscribe")
    assert not session.ready_for_market
    await session.handle_control({"id": sub["id"], "result": [CHANNEL]})
    assert session.ready_for_market
    await session.close()


async def test_late_old_connection_ack_does_not_open_new_session(rpc_transport):
    session = make()
    await session.begin(rpc_transport)
    old = requests(rpc_transport, "public/auth")[-1]["id"]
    await session.close()
    await session.begin(rpc_transport)
    assert requests(rpc_transport, "public/auth")[-1]["id"] > old
    assert not await session.handle_control(
        {"id": old, "result": {"access_token": "old", "refresh_token": "old", "expires_in": 99}}
    )
    assert not session.ready_for_market
    assert not requests(rpc_transport, "public/subscribe")
    await session.close()


@pytest.mark.parametrize(
    "response",
    [
        {"error": {"code": 13004, "message": "SECRET_SENTINEL"}},
        {"result": {"access_token": "ACCESS_SENTINEL", "expires_in": 900}},
    ],
)
async def test_auth_failures_are_sanitized(rpc_transport, response):
    session = make()
    await session.begin(rpc_transport)
    with pytest.raises(FatalConnectorError) as error:
        await session.handle_control(
            {"id": requests(rpc_transport, "public/auth")[0]["id"], **response}
        )
    assert "SENTINEL" not in str(error.value)
    assert not session.ready_for_market
    await session.close()


async def test_missing_requested_channel_is_fatal(rpc_transport):
    session = make()
    await session.begin(rpc_transport)
    await authenticate(session, rpc_transport)
    with pytest.raises(FatalConnectorError):
        await session.handle_control(
            {"id": requests(rpc_transport, "public/subscribe")[0]["id"], "result": []}
        )
    await session.close()


async def test_request_timeout_closes_readiness_and_pending(rpc_transport):
    clock = [0]
    session = make(clock_ns=lambda: clock[0], request_timeout_seconds=1)
    await session.begin(rpc_transport)
    clock[0] = 1_000_000_000
    with pytest.raises(TransientConnectorError, match="timeout"):
        await session.tick()
    assert not session.ready_for_market
    assert not session._pending
    await session.close()


async def test_anonymous_subscription_has_correlated_ack(rpc_transport):
    session = DeribitSession(
        interval="100ms", client_id=None, client_secret=None, channels=("book.BTC-PERPETUAL.100ms",)
    )
    await session.begin(rpc_transport)
    assert not requests(rpc_transport, "public/auth")
    sub = requests(rpc_transport, "public/subscribe")[0]
    await session.handle_control({"id": sub["id"], "result": sub["params"]["channels"]})
    assert session.ready_for_market
    await session.close()
