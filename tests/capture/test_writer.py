import asyncio
import threading

import pytest

from crocodile.core.capture import writer as mod
from crocodile.core.capture.reader import CaptureReader
from crocodile.core.capture.schema import RegistryCheckpoint
from crocodile.core.errors import CaptureError


async def test_flush_advances_only_durable_watermark(tmp_path, capture_context):
    writer = await mod.CaptureWriter.create(tmp_path, capture_context)
    assert await writer.append(RegistryCheckpoint(instruments=())) == 1
    assert await writer.flush() == 1
    assert writer.stats().fsynced_ordinal == 1
    summary = await writer.close()
    assert summary.stats.fsynced_records == 1
    assert [r.ordinal for r in CaptureReader(tmp_path).records()] == [1]


async def test_queue_overflow_is_sticky_failure(tmp_path, capture_context):
    writer = await mod.CaptureWriter.create(tmp_path, capture_context, queue_bytes=1)
    with pytest.raises(CaptureError):
        await writer.append(RegistryCheckpoint(instruments=()))
    with pytest.raises(CaptureError):
        await writer.flush()
    with pytest.raises(CaptureError):
        await writer.close()
    assert writer.stats().failed
    assert writer.stats().fsynced_ordinal == 0


async def test_fsync_error_wakes_failure_waiters(tmp_path, capture_context, monkeypatch):
    writer = await mod.CaptureWriter.create(tmp_path, capture_context)
    waiter = asyncio.create_task(writer.wait_failure())
    await writer.append(RegistryCheckpoint(instruments=()))
    monkeypatch.setattr(mod.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(CaptureError):
        await writer.flush()
    with pytest.raises(CaptureError):
        await asyncio.wait_for(waiter, 2)
    assert writer.stats().fsynced_ordinal == 0
    with pytest.raises(CaptureError):
        await writer.close()


async def test_idle_capture_fsyncs_within_interval(tmp_path, capture_context):
    writer = await mod.CaptureWriter.create(tmp_path, capture_context, fsync_interval_s=0.02)
    await writer.append(RegistryCheckpoint(instruments=()))
    for _ in range(100):
        if writer.stats().fsynced_ordinal == 1:
            break
        await asyncio.sleep(0.005)
    assert writer.stats().fsynced_ordinal == 1
    await writer.close()


async def test_rotation_never_splits_records(tmp_path, capture_context):
    writer = await mod.CaptureWriter.create(tmp_path, capture_context, segment_bytes=60)
    for _ in range(3):
        await writer.append(RegistryCheckpoint(instruments=()))
    summary = await writer.close()
    assert summary.sealed_segment_count == 3
    assert [r.ordinal for r in CaptureReader(tmp_path).records()] == [1, 2, 3]
    assert not list(tmp_path.glob("*.partial"))


async def test_queue_budget_includes_envelope_and_inflight_bytes(
    tmp_path, capture_context, monkeypatch
):
    writer = await mod.CaptureWriter.create(tmp_path, capture_context, queue_bytes=60)
    entered, release = threading.Event(), threading.Event()
    actual = mod.write_all

    def blocked(file, payload):
        entered.set()
        assert release.wait(2)
        actual(file, payload)

    monkeypatch.setattr(mod, "write_all", blocked)
    await writer.append(RegistryCheckpoint(instruments=()))
    assert await asyncio.to_thread(entered.wait, 2)
    assert writer.stats().queued_bytes > 0
    try:
        with pytest.raises(CaptureError):
            await writer.append(RegistryCheckpoint(instruments=()))
    finally:
        release.set()
        with pytest.raises(CaptureError):
            await writer.close()


async def test_short_write_failure_does_not_advance_watermark(
    tmp_path, capture_context, monkeypatch
):
    writer = await mod.CaptureWriter.create(tmp_path, capture_context)

    def broken(file, payload):
        file.write(payload[:2])
        raise OSError("short write")

    monkeypatch.setattr(mod, "write_all", broken)
    await writer.append(RegistryCheckpoint(instruments=()))
    with pytest.raises(CaptureError):
        await writer.flush()
    assert writer.stats().written_ordinal == 0
    assert writer.stats().fsynced_ordinal == 0
    with pytest.raises(CaptureError):
        await writer.close()
    assert list(CaptureReader(tmp_path, recover_active_tail=True).records()) == []


async def test_cancelled_close_drains_capture(tmp_path, capture_context):
    writer = await mod.CaptureWriter.create(tmp_path, capture_context)
    await writer.append(RegistryCheckpoint(instruments=()))
    task = asyncio.create_task(writer.close())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await writer.close()).stats.fsynced_records == 1
    assert len(list(CaptureReader(tmp_path).records())) == 1
