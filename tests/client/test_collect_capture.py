import asyncio

import pytest

from crocodile.core.capture.schema import CaptureContext, RegistryCheckpoint
from crocodile.core.capture.writer import CaptureWriter
from crocodile.core.errors import CaptureError, FatalConnectorError
from crocodile.core.sink.memory import MemorySink
from crocodile.crypto.client.collect import collect


class Stub:
    name = "test"

    def __init__(self, error=None, idle=False):
        self.error, self.idle = error, idle

    async def run(self, **kwargs):
        if self.error:
            raise self.error
        if self.idle:
            await asyncio.Event().wait()


class ClosingSink(MemorySink):
    closed = False

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("mode", ["sbe", "raw"])
async def test_explicit_fast_feed_failure_without_capture_is_not_success(mode):
    source = Stub(FatalConnectorError("Required feed authorization rejected"))
    source.wire_format = mode if mode == "sbe" else "json"
    source.interval = mode if mode == "raw" else "100ms"
    sink = ClosingSink()
    with pytest.raises(FatalConnectorError, match="authorization"):
        await asyncio.wait_for(collect([source, Stub(idle=True)], sink), 1)
    assert sink.closed


@pytest.mark.asyncio
async def test_close_fsync_failure_is_not_success(tmp_path, monkeypatch):
    writer = await CaptureWriter.create(tmp_path, CaptureContext(capture_id="test", streams=()))
    sink = ClosingSink()

    def fail():
        raise OSError("fsync failed")

    monkeypatch.setattr(writer, "_sync_disk", fail)
    await writer.append(RegistryCheckpoint(instruments=()))
    with pytest.raises(CaptureError):
        await collect([Stub()], sink, capture=writer)
    assert sink.closed
    assert writer.stats().fsynced_ordinal == 0


@pytest.mark.asyncio
async def test_required_capture_failure_propagates_after_closing_both_outputs(tmp_path):
    writer = await CaptureWriter.create(tmp_path, CaptureContext(capture_id="test", streams=()))
    sink = ClosingSink()
    await writer.append(RegistryCheckpoint(instruments=()))
    with pytest.raises(CaptureError, match="injected"):
        await collect([Stub(CaptureError("injected")), Stub(idle=True)], sink, capture=writer)
    assert sink.closed
    assert writer.stats().fsynced_records == 1


@pytest.mark.asyncio
async def test_fsync_failure_stops_idle_collect_and_does_not_report_success(tmp_path, monkeypatch):
    writer = await CaptureWriter.create(
        tmp_path, CaptureContext(capture_id="test", streams=()), fsync_interval_s=0.02
    )
    sink = ClosingSink()

    def failed_sync():
        raise OSError("disk full")

    monkeypatch.setattr(writer, "_sync_disk", failed_sync)
    await writer.append(RegistryCheckpoint(instruments=()))
    with pytest.raises(CaptureError):
        await asyncio.wait_for(collect([Stub(idle=True)], sink, capture=writer), 0.5)
    assert sink.closed
    assert writer.stats().fsynced_ordinal == 0


@pytest.mark.asyncio
async def test_cancellation_seals_capture_and_reraises(tmp_path):
    writer = await CaptureWriter.create(tmp_path, CaptureContext(capture_id="test", streams=()))
    sink = ClosingSink()
    await writer.append(RegistryCheckpoint(instruments=()))
    task = asyncio.create_task(collect([Stub(idle=True)], sink, capture=writer))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sink.closed
    assert writer.stats().fsynced_ordinal == 1
    assert list(tmp_path.glob("*.raw"))
