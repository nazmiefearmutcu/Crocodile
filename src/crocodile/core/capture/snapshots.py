"""Public depth snapshots, with their bytes captured before parsing."""

from __future__ import annotations

import time
from typing import Protocol

import aiohttp
import msgspec

from crocodile.core.capture.reader import CaptureReader
from crocodile.core.capture.schema import SnapshotRequest, SnapshotResponse
from crocodile.core.capture.writer import CaptureWriter
from crocodile.core.errors import CaptureIntegrityError, ConfigError, TransientConnectorError


class CapturedSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    response: SnapshotResponse
    origin_ordinal: int | None


class SnapshotProvider(Protocol):
    async def fetch(self, request: SnapshotRequest) -> CapturedSnapshot: ...


class HttpSnapshotProvider:
    def __init__(
        self, session: aiohttp.ClientSession, capture: CaptureWriter | None, *, base_url: str
    ):
        self.session, self.capture, self.base_url = session, capture, base_url

    async def fetch(self, request: SnapshotRequest) -> CapturedSnapshot:
        if (
            request.path not in {"/api/v3/depth", "/fapi/v1/depth", "/dapi/v1/depth"}
            or set(request.params) != {"symbol", "limit"}
            or not isinstance(request.params["symbol"], str)
            or not str(request.params["symbol"]).isalnum()
            or not isinstance(request.params["limit"], int)
            or not 1 <= request.params["limit"] <= 5000
        ):
            raise ConfigError("Only public depth snapshot requests may be captured")
        if self.capture is not None:
            await self.capture.append(request)
        async with self.session.get(
            self.base_url + request.path,
            params=request.params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            # Bounded reads: an unexpected HTTP response cannot grow RAM without limit.
            if hasattr(response, "content"):
                chunks, size = [], 0
                while chunk := await response.content.read(1 << 20):
                    size += len(chunk)
                    if size > 16 << 20:
                        raise TransientConnectorError("Depth response exceeds capture limit")
                    chunks.append(chunk)
                payload = b"".join(chunks)
            else:
                payload = await response.read()  # legacy scripted HTTP fixture
            result = SnapshotResponse(
                request_id=request.request_id,
                status=response.status,
                payload=payload,
                received_utc_ns=time.time_ns(),
                received_monotonic_ns=time.monotonic_ns(),
            )
        ordinal = await self.capture.append(result) if self.capture is not None else None
        if result.status != 200:
            raise TransientConnectorError("Public depth snapshot request failed")
        return CapturedSnapshot(response=result, origin_ordinal=ordinal)


class RecordedSnapshotProvider:
    def __init__(self, reader: CaptureReader):
        self.reader = reader

    async def fetch(self, request: SnapshotRequest) -> CapturedSnapshot:
        found = False
        for record in self.reader.records():
            if (
                isinstance(record.event, SnapshotRequest)
                and record.event.request_id == request.request_id
            ):
                if record.event != request or found:
                    raise CaptureIntegrityError("Snapshot request mismatch or duplicate")
                found = True
            if (
                isinstance(record.event, SnapshotResponse)
                and record.event.request_id == request.request_id
            ):
                if not found:
                    raise CaptureIntegrityError("Snapshot response without request")
                return CapturedSnapshot(response=record.event, origin_ordinal=record.ordinal)
        raise CaptureIntegrityError("Recorded snapshot is missing")
