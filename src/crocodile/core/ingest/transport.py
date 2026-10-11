"""WS transport Protocol + FakeTransport for deterministic testing.

Also provides ``AiohttpWsTransport`` — a live aiohttp-backed WebSocket
transport used by the ``collect`` CLI command.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol, runtime_checkable

from crocodile.core.capture.schema import RawFrame
from crocodile.core.errors import TransientConnectorError


@runtime_checkable
class Transport(Protocol):
    """Minimal interface that every WS transport must satisfy."""

    async def connect(self) -> None: ...

    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def send(self, data: bytes) -> None: ...

    async def close(self) -> None: ...


class FakeTransport:
    """Yields canned frames then stops — drives connectors without network."""

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = frames
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[bytes]:
        for frame in self._frames:
            yield frame

    async def send(self, data: bytes) -> None:
        pass  # no-op for tests

    async def close(self) -> None:
        self._connected = False


async def iter_transport_frames(transport: Transport) -> AsyncIterator[RawFrame]:
    """Prefer observed frame metadata; legacy fixtures explicitly lack it."""
    iterator = getattr(transport, "iter_frames", None)
    if iterator is not None:
        async for frame in iterator():
            yield frame
    else:
        async for payload in transport:
            yield RawFrame(payload=payload, frame_kind="unknown", received_utc_ns=None,
                           received_monotonic_ns=None, connection_id=None,
                           receive_ordinal=None)


class AiohttpWsTransport:
    """Live WebSocket transport backed by ``aiohttp``.

    Used by the ``collect`` CLI command to connect to exchange WebSocket
    endpoints.  Keeps a single ``aiohttp.ClientSession`` open for the
    lifetime of the connection; ``close()`` tears both the WS and the
    session down cleanly.

    Args:
        url: Full WebSocket URL, e.g. ``wss://www.deribit.com/ws/api/v2``.
        ssl: Passed to aiohttp as the SSL argument.  ``None`` (default) uses
            certifi CA bundle when available, else system defaults.  ``False``
            disables verification (dev only).  An ``ssl.SSLContext`` is used
            as-is.
    """

    def __init__(self, url: str, *, ssl: Any = None,
                 headers: Mapping[str, str] | None = None) -> None:
        self._url = url
        self._ssl = ssl  # None → resolve at connect(); False/SSLContext explicit
        # Typed as Any to avoid importing aiohttp at module level (lazy import).
        self._session: Any = None  # aiohttp.ClientSession
        self._ws: Any = None  # aiohttp.ClientWebSocketResponse
        self._headers = dict(headers) if headers is not None else None
        self._connection_id = uuid.uuid4().hex
        self._receive_ordinal = 0
        self._reading = False

    def _resolve_ssl(self) -> Any:
        if self._ssl is not None:
            return self._ssl
        try:
            import ssl as ssl_mod

            import certifi

            return ssl_mod.create_default_context(cafile=certifi.where())
        except Exception:
            return True  # aiohttp default verification

    async def connect(self) -> None:
        import aiohttp  # lazy — keeps CLI import-time fast

        # Merge of two hardening lines: env-gated SSL (certifi CA) from the
        # FlowMap visualizer line + session-leak safety from the ralph line.
        # Create the session first with the resolved SSL connector; only publish
        # it on self after ws_connect succeeds. If ws_connect raises, close the
        # session here so callers that never reach close() cannot leak it.
        ssl_arg = self._resolve_ssl()
        connector = aiohttp.TCPConnector(ssl=ssl_arg)
        session = aiohttp.ClientSession(connector=connector)
        try:
            options = {} if self._headers is None else {"headers": self._headers}
            ws = await session.ws_connect(self._url, heartbeat=20.0, ssl=ssl_arg, **options)
        except Exception:
            try:
                await session.close()
            except Exception:
                pass
            raise
        self._session = session
        self._ws = ws
        self._connection_id = uuid.uuid4().hex
        self._receive_ordinal = 0

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[bytes]:
        async for frame in self.iter_frames():
            yield frame.payload

    async def iter_frames(self) -> AsyncIterator[RawFrame]:
        import aiohttp

        if self._reading:
            raise RuntimeError("WebSocket already has an active reader")
        if self._ws is None:
            raise TransientConnectorError("WebSocket is not connected")
        self._reading = True
        try:
            async for msg in self._ws:
                if msg.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                self._receive_ordinal += 1
                is_text = msg.type == aiohttp.WSMsgType.TEXT
                yield RawFrame(
                    payload=msg.data.encode("utf-8") if is_text else msg.data,
                    frame_kind="text" if is_text else "binary",
                    received_utc_ns=time.time_ns(), received_monotonic_ns=time.monotonic_ns(),
                    connection_id=self._connection_id, receive_ordinal=self._receive_ordinal,
                )
            raise TransientConnectorError("WebSocket closed")
        finally:
            self._reading = False

    async def send(self, data: bytes) -> None:
        if self._ws is not None:
            text = data.decode() if isinstance(data, (bytes, bytearray)) else data
            await self._ws.send_str(text)

    async def close(self) -> None:
        ws, session = self._ws, self._session
        self._ws = None
        self._session = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if session is not None:
            try:
                await session.close()
                # Yield once so aiohttp's connector can finish tearing down its
                # (SSL) transports before the caller closes the event loop —
                # avoids "Task was destroyed but it is pending" warnings.
                await asyncio.sleep(0)
            except Exception:
                pass
