"""Connection-scoped JSON-RPC routing; market processing never waits for an ack."""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, NoReturn

from crocodile.core.errors import FatalConnectorError, TransientConnectorError
from crocodile.core.ingest.transport import Transport


class SessionState(StrEnum):
    DISCONNECTED = "disconnected"
    AUTH_PENDING = "auth_pending"
    SUBSCRIBE_PENDING = "subscribe_pending"
    SNAPSHOT_PENDING = "snapshot_pending"
    LIVE = "live"
    FAILED = "failed"


@dataclass(frozen=True)
class PendingRequest:
    request_id: int
    connection_epoch: int
    method: str
    deadline_monotonic_ns: int
    channels: tuple[str, ...] = ()
    symbol: str | None = None


class DeribitSession:
    def __init__(
        self,
        *,
        interval: Literal["100ms", "raw"],
        client_id: str | None,
        client_secret: str | None,
        channels: tuple[str, ...],
        request_timeout_seconds: float = 10.0,
        heartbeat_interval_seconds: int = 30,
        refresh_margin_seconds: float = 30.0,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.interval, self.channels = interval, channels
        self._client_id, self._client_secret = client_id, client_secret
        self._timeout_ns = int(request_timeout_seconds * 1_000_000_000)
        self._heartbeat_interval = heartbeat_interval_seconds
        self._margin_ns = int(refresh_margin_seconds * 1_000_000_000)
        self._clock_ns = clock_ns
        self._transport: Transport | None = None
        self._pending: dict[int, PendingRequest] = {}
        self._epoch = self._next_id = 0
        self._ready = False
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_ns: int | None = None
        self._refresh_ns: int | None = None
        self._wake = asyncio.Event()
        self.state = SessionState.DISCONNECTED

    @property
    def ready_for_market(self) -> bool:
        return self._ready

    async def begin(self, transport: Transport) -> None:
        await self.close()
        self._epoch += 1
        self._transport = transport
        if self.interval == "raw":
            self.state = SessionState.AUTH_PENDING
            await self._send(
                "public/auth",
                {
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
        else:
            await self._subscribe()

    async def _send(
        self,
        method: str,
        params: dict[str, Any],
        *,
        channels: tuple[str, ...] = (),
        symbol: str | None = None,
    ) -> None:
        if self._transport is None:
            raise TransientConnectorError("Deribit connection is closed")
        self._next_id += 1
        request_id = self._next_id
        self._pending[request_id] = PendingRequest(
            request_id,
            self._epoch,
            method,
            self._clock_ns() + self._timeout_ns,
            channels,
            symbol,
        )
        self._wake.set()
        await self._transport.send(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            ).encode()
        )

    async def _subscribe(self) -> None:
        self.state = SessionState.SUBSCRIBE_PENDING
        await self._send(
            "public/subscribe", {"channels": list(self.channels)}, channels=self.channels
        )

    def _invalidate(self) -> None:
        self._ready = False
        self._access_token = self._refresh_token = None
        self._expires_ns = self._refresh_ns = None
        self._pending.clear()

    def _fatal(self, message: str) -> NoReturn:
        self._invalidate()
        self.state = SessionState.FAILED
        raise FatalConnectorError(message)

    def _transient(self, message: str) -> NoReturn:
        self._invalidate()
        self.state = SessionState.FAILED
        raise TransientConnectorError(message)

    def _accept_tokens(self, result: object) -> None:
        if not isinstance(result, dict):
            self._fatal("Deribit authentication response is invalid")
        access, refresh, seconds = (
            result.get("access_token"),
            result.get("refresh_token"),
            result.get("expires_in"),
        )
        if not (
            isinstance(access, str)
            and access.strip()
            and isinstance(refresh, str)
            and refresh.strip()
            and isinstance(seconds, (int, float))
            and not isinstance(seconds, bool)
            and math.isfinite(seconds)
            and seconds > 0
            and result.get("token_type", "bearer") == "bearer"
        ):
            self._fatal("Deribit authentication token metadata is invalid")
        self._access_token, self._refresh_token = access, refresh
        lifetime = int(seconds * 1_000_000_000)
        now = self._clock_ns()
        self._expires_ns = now + lifetime
        self._refresh_ns = now + lifetime - min(self._margin_ns, lifetime // 2)

    async def handle_control(self, msg: dict[str, Any]) -> bool:
        request_id = msg.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            return False
        pending = self._pending.pop(request_id, None)
        if pending is None or pending.connection_epoch != self._epoch:
            return False
        if "error" in msg:
            error = msg["error"]
            if isinstance(error, dict) and error.get("code") == 10028:
                self._transient("Deribit request was rate limited")
            self._fatal("Deribit control request was rejected")
        result = msg.get("result")
        if pending.method == "public/auth":
            self._accept_tokens(result)
            if self.state == SessionState.AUTH_PENDING:
                await self._subscribe()
        elif pending.method == "public/subscribe":
            if not (
                isinstance(result, list)
                and all(isinstance(c, str) for c in result)
                and set(pending.channels).issubset(result)
            ):
                self._fatal("Deribit subscription acknowledgement is incomplete")
            self._ready = True
            self.state = SessionState.SNAPSHOT_PENDING
        self._wake.set()
        return True

    async def tick(self) -> None:
        now = self._clock_ns()
        if any(p.deadline_monotonic_ns <= now for p in self._pending.values()):
            self._transient("Deribit control request timeout")

    async def _watchdog(self) -> NoReturn:
        while True:
            self._wake.clear()
            await self.tick()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.5)
            except TimeoutError:
                pass

    async def close(self) -> None:
        self._invalidate()
        self._transport = None
        self.state = SessionState.DISCONNECTED
        self._wake.set()
