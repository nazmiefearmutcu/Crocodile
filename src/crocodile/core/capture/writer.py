"""A single ordered disk writer with bounded admission and explicit durability."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import BinaryIO, NoReturn

from crocodile.core.capture.format import FILE_HEADER, encode_record
from crocodile.core.capture.manifest import CaptureManifest, SegmentInfo, checksum, save_manifest
from crocodile.core.capture.schema import (
    CaptureContext,
    CaptureEvent,
    CaptureStats,
    CaptureSummary,
    JournalRecord,
    MarketFrame,
)
from crocodile.core.errors import CaptureError, ConfigError

log = logging.getLogger(__name__)


def write_all(file: BinaryIO, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        count = file.write(view)
        if count is None or count <= 0:
            raise OSError("Raw capture short write made no progress")
        view = view[count:]


class CaptureWriter:
    def __init__(self, root: Path, context: CaptureContext, *, queue_bytes: int,
                 segment_bytes: int, max_record_bytes: int, fsync_interval_s: float):
        self.root, self.context = root, context
        self._budget, self._segment_target = queue_bytes, segment_bytes
        self._max_record, self._interval = max_record_bytes, fsync_interval_s
        self._queue: asyncio.Queue[tuple[int, bytes] | asyncio.Future[int] | None] = asyncio.Queue()
        self._next = self._written = self._fsynced = self._received = 0
        self._bytes = self._peak_bytes = 0
        self._admitted: deque[tuple[int, int]] = deque()
        self._manifest = CaptureManifest(context=context)
        self._file: BinaryIO | None = None
        self._size = self._segment_first = 0
        self._last_sync = time.monotonic()
        self._error: CaptureError | None = None
        self._failure = asyncio.Event()
        self._waiters: set[asyncio.Future[int]] = set()
        self._closing = False
        self._close_task: asyncio.Task[CaptureSummary] | None = None
        self._task: asyncio.Task[None] | None = None

    @classmethod
    async def create(cls, root: Path, context: CaptureContext, *, queue_bytes: int = 32 << 20,
                     segment_bytes: int = 256 << 20, max_record_bytes: int = 16 << 20,
                     fsync_interval_s: float = 1.0) -> CaptureWriter:
        if (queue_bytes < 1 or segment_bytes <= len(FILE_HEADER) or max_record_bytes < 1
                or max_record_bytes > 16 << 20 or not 0 < fsync_interval_s <= 1):
            raise ConfigError("Invalid raw capture byte budgets or fsync interval")
        writer = cls(Path(root), context, queue_bytes=queue_bytes, segment_bytes=segment_bytes,
                     max_record_bytes=max_record_bytes, fsync_interval_s=fsync_interval_s)
        try:
            await asyncio.to_thread(writer._initialize)
        except Exception as exc:
            if writer._file is not None:
                writer._file.close()
            raise CaptureError("Cannot initialize required raw capture") from exc
        writer._task = asyncio.create_task(writer._run(), name="raw-capture-writer")
        return writer

    def _initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if any(self.root.iterdir()):
            raise CaptureError("Capture directory is not empty")
        self._open_segment()

    def _open_segment(self) -> None:
        name = f"{len(self._manifest.segments) + 1:08d}.partial"
        self._file = (self.root / name).open("xb", buffering=0)
        write_all(self._file, FILE_HEADER)
        self._file.flush()
        os.fsync(self._file.fileno())
        self._size, self._segment_first = len(FILE_HEADER), 0
        self._manifest = CaptureManifest(context=self.context, segments=self._manifest.segments,
                                         active_name=name, fsynced_ordinal=self._fsynced)
        save_manifest(self.root, self._manifest)

    def _check(self) -> None:
        if self._error is not None:
            raise self._error

    def _fail(self, exc: BaseException) -> None:
        if self._error is None:
            self._error = CaptureError("Required raw capture failed")
            self._error.__cause__ = exc
            self._failure.set()
            for waiter in tuple(self._waiters):
                if not waiter.done():
                    waiter.set_exception(self._error)
            self._queue.put_nowait(None)

    async def append(self, event: CaptureEvent) -> int:
        self._check()
        if self._closing:
            raise CaptureError("Raw capture is closing")
        if isinstance(event, MarketFrame):
            self._received += 1
        try:
            payload = encode_record(JournalRecord(ordinal=self._next + 1, event=event),
                                    max_record_bytes=self._max_record)
            if self._bytes + len(payload) > self._budget:
                raise CaptureError("Raw capture byte queue overflow")
        except Exception as exc:
            self._fail(exc)
            self._check()
            raise AssertionError("unreachable") from exc
        self._next += 1
        self._bytes += len(payload)
        self._peak_bytes = max(self._peak_bytes, self._bytes)
        self._admitted.append((self._next, time.monotonic_ns()))
        self._queue.put_nowait((self._next, payload))
        return self._next

    async def flush(self) -> int:
        self._check()
        if self._closing:
            await self.close()
            return self._fsynced
        future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._waiters.add(future)
        self._queue.put_nowait(future)
        try:
            return await future
        finally:
            self._waiters.discard(future)

    def _sync_disk(self) -> None:
        if self._file is None:
            return
        self._file.flush()
        os.fsync(self._file.fileno())
        manifest = CaptureManifest(context=self.context, segments=self._manifest.segments,
                                   active_name=self._manifest.active_name,
                                   fsynced_ordinal=self._written)
        save_manifest(self.root, manifest)
        self._manifest = manifest

    async def _sync(self) -> None:
        if self._written == self._fsynced:
            self._last_sync = time.monotonic()
            return
        if self.stats().durable_lag_ns > int(self._interval * 1_000_000_000):
            log.warning("Raw capture durable lag exceeded configured fsync interval")
        await asyncio.to_thread(self._sync_disk)
        self._fsynced = self._written
        while self._admitted and self._admitted[0][0] <= self._fsynced:
            self._admitted.popleft()
        self._last_sync = time.monotonic()

    def _write(self, ordinal: int, payload: bytes) -> None:
        if self._file is None:
            self._open_segment()
        assert self._file is not None
        write_all(self._file, payload)
        self._size += len(payload)
        if self._segment_first == 0:
            self._segment_first = ordinal

    def _seal_disk(self) -> None:
        assert self._file is not None and self._manifest.active_name is not None
        self._file.close()
        self._file = None
        partial = self.root / self._manifest.active_name
        sealed = partial.with_suffix(".raw")
        # Publish a second name before the manifest switches: a process crash
        # always leaves the previous active name or the new sealed name readable.
        os.link(partial, sealed)
        info = SegmentInfo(name=sealed.name, size_bytes=self._size,
                           first_ordinal=self._segment_first,
                           last_ordinal=self._written if self._segment_first else 0,
                           sha256=checksum(sealed))
        self._manifest = CaptureManifest(context=self.context,
            segments=(*self._manifest.segments, info), fsynced_ordinal=self._fsynced)
        save_manifest(self.root, self._manifest)
        partial.unlink()

    async def _seal(self) -> None:
        await self._sync()
        await asyncio.to_thread(self._seal_disk)

    async def _run(self) -> None:
        try:
            while self._error is None:
                remaining = max(.001, self._interval - (time.monotonic() - self._last_sync))
                try:
                    item = await asyncio.wait_for(self._queue.get(), remaining)
                except TimeoutError:
                    await self._sync()
                    continue
                if item is None:
                    await self._seal()
                    return
                if isinstance(item, asyncio.Future):
                    await self._sync()
                    if not item.done():
                        item.set_result(self._fsynced)
                    continue
                ordinal, payload = item
                if self._segment_first and self._size + len(payload) > self._segment_target:
                    await self._seal()
                await asyncio.to_thread(self._write, ordinal, payload)
                self._written = ordinal
                self._bytes -= len(payload)
                if time.monotonic() - self._last_sync >= self._interval:
                    await self._sync()
        except Exception as exc:
            self._fail(exc)
        finally:
            if self._file is not None:
                await asyncio.to_thread(self._file.close)
                self._file = None

    async def _finish(self) -> CaptureSummary:
        self._closing = True
        self._queue.put_nowait(None)
        assert self._task is not None
        await self._task
        self._check()
        return CaptureSummary(context=self.context, stats=self.stats(),
                              sealed_segment_count=len(self._manifest.segments))

    async def close(self) -> CaptureSummary:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._finish(), name="raw-capture-close")
        try:
            return await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await asyncio.shield(self._close_task)
            raise

    async def wait_failure(self) -> NoReturn:
        await self._failure.wait()
        assert self._error is not None
        raise self._error

    def stats(self) -> CaptureStats:
        lag = time.monotonic_ns() - self._admitted[0][1] if self._admitted else 0
        return CaptureStats(received_frames=self._received, queued_records=self._next,
            written_records=self._written, fsynced_records=self._fsynced,
            queued_ordinal=self._next, written_ordinal=self._written,
            fsynced_ordinal=self._fsynced, queued_bytes=self._bytes,
            peak_queued_bytes=self._peak_bytes, durable_lag_ns=max(0, lag),
            failed=self._error is not None)
