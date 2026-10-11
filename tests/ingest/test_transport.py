"""Unit tests for AiohttpWsTransport — send frames + connect session cleanup.

Root-cause bugs covered:
- send() used send_bytes() which Deribit silently rejects with bad_request
  (code 11050).  Fix: decode to str and use send_str() instead.
- connect() created ClientSession then called ws_connect; if ws_connect
  raised, the session was never closed (leak).
"""
from __future__ import annotations

import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from crocodile.core.ingest.transport import AiohttpWsTransport

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_transport_with_fake_ws() -> tuple[AiohttpWsTransport, MagicMock]:
    """Return an AiohttpWsTransport whose _ws is pre-set to a MagicMock."""
    transport = AiohttpWsTransport("wss://fake.example.com/ws")
    fake_ws = MagicMock()
    fake_ws.send_str = AsyncMock()
    fake_ws.send_bytes = AsyncMock()
    transport._ws = fake_ws  # inject without calling connect()
    return transport, fake_ws


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAiohttpWsTransportSend:
    def test_send_uses_text_frame_not_binary(self) -> None:
        """send() must call send_str with the decoded string, not send_bytes."""
        transport, fake_ws = _make_transport_with_fake_ws()
        payload = b'{"hello":"world"}'

        asyncio.run(transport.send(payload))

        fake_ws.send_str.assert_awaited_once_with('{"hello":"world"}')
        fake_ws.send_bytes.assert_not_called()

    def test_send_noop_when_ws_is_none(self) -> None:
        """send() must be a no-op (no exception) when _ws is None."""
        transport = AiohttpWsTransport("wss://fake.example.com/ws")
        assert transport._ws is None
        # Should not raise
        asyncio.run(transport.send(b'{"op":"ping"}'))

    def test_send_str_receives_decoded_unicode(self) -> None:
        """Payload with unicode round-trips correctly through decode."""
        transport, fake_ws = _make_transport_with_fake_ws()
        # A realistic Deribit subscribe message (kept as a bytes literal)
        channel = "book.BTC-PERPETUAL.100ms"
        payload = (
            b'{"jsonrpc":"2.0","method":"public/subscribe",'
            b'"params":{"channels":["book.BTC-PERPETUAL.100ms"]},"id":1}'
        )

        asyncio.run(transport.send(payload))

        expected = (
            '{"jsonrpc":"2.0","method":"public/subscribe",'
            f'"params":{{"channels":["{channel}"]}},"id":1}}'
        )
        fake_ws.send_str.assert_awaited_once_with(expected)
        fake_ws.send_bytes.assert_not_called()


class TestAiohttpWsTransportConnect:
    def test_connect_closes_session_when_ws_connect_fails(self) -> None:
        """If ws_connect raises, the ClientSession must still be closed.

        Regression: connect() assigned ClientSession to self then awaited
        ws_connect; a failure left the session open when callers never
        reached close() (or when self._session was never published).
        """
        transport = AiohttpWsTransport("wss://fake.example.com/ws")
        fake_session = MagicMock()
        fake_session.close = AsyncMock()
        fake_session.ws_connect = AsyncMock(side_effect=ConnectionError("boom"))

        with patch("aiohttp.ClientSession", return_value=fake_session):
            with pytest.raises(ConnectionError, match="boom"):
                asyncio.run(transport.connect())

        fake_session.close.assert_awaited_once()
        # Failure must not leave a live session/ws on the transport.
        assert transport._session is None
        assert transport._ws is None

    def test_connect_success_keeps_session_and_ws(self) -> None:
        """On success, session and ws are stored for later close()/send()."""
        transport = AiohttpWsTransport("wss://fake.example.com/ws")
        fake_ws = MagicMock()
        fake_session = MagicMock()
        fake_session.close = AsyncMock()
        fake_session.ws_connect = AsyncMock(return_value=fake_ws)

        with patch("aiohttp.ClientSession", return_value=fake_session):
            asyncio.run(transport.connect())

        assert transport._session is fake_session
        assert transport._ws is fake_ws
        # Merged transport passes the resolved SSL context through to ws_connect
        # (certifi CA bootstrap from the visualizer line); the session is still
        # created once and kept on success (ralph leak-safety line).
        fake_session.ws_connect.assert_awaited_once_with(
            "wss://fake.example.com/ws", heartbeat=20.0, ssl=ANY, max_msg_size=4 << 20
        )
        fake_session.close.assert_not_awaited()
