import asyncio
import json

import pytest

from crocodile.core.capture.reader import CaptureReader
from crocodile.core.capture.schema import (
    CaptureContext,
    ControlMetadata,
    MarketFrame,
    StreamContext,
)
from crocodile.core.capture.writer import CaptureWriter
from crocodile.core.connector import Connector
from crocodile.core.errors import CaptureError
from crocodile.core.ingest.transport import FakeTransport
from crocodile.core.sink.memory import MemorySink
from crocodile.crypto.instruments.registry import InstrumentRegistry


@pytest.fixture
def capture_context():
    return CaptureContext(
        capture_id="test",
        streams=(
            StreamContext(
                stream_id="s1",
                source="deribit",
                market="derivatives",
                symbols=("BTC",),
                endpoint_id="deribit-public",
                decoder_id="deribit-json",
                decoder_version=1,
            ),
        ),
    )


class Harness(Connector):
    name = "deribit"
    ws_url = "wss://test"
    rest_url = "https://test"

    def __init__(self, writer):
        super().__init__(["BTC"], ["book_delta"], MemorySink(), InstrumentRegistry())
        self.capture = writer
        self.capture_stream_id = "s1"
        self.entered, self.release, self.ack = asyncio.Event(), asyncio.Event(), asyncio.Event()

    def normalize(self, msg, local_ts):
        return []

    async def list_instruments(self):
        return []

    async def _subscribe(self, transport):
        pass

    async def _handle_message(self, msg, local_ts):
        self.entered.set()
        await self.release.wait()

    async def _handle_control_message(self, msg, frame):
        if isinstance(msg, dict) and msg.get("id") == 7:
            self.ack.set()
        return True


MARKET = json.dumps(
    {"method": "subscription", "params": {"channel": "book.BTC.raw", "data": {"type": "snapshot"}}}
).encode()


@pytest.mark.asyncio
async def test_reader_captures_and_routes_control_while_processor_waits(tmp_path, capture_context):
    writer = await CaptureWriter.create(tmp_path, capture_context)
    conn = Harness(writer)
    conn.transport = FakeTransport([MARKET, b'{"id":7,"result":null}'])
    task = asyncio.create_task(conn.run(max_reconnects=0))
    try:
        await asyncio.wait_for(conn.entered.wait(), 0.5)
        await asyncio.wait_for(conn.ack.wait(), 0.5)
        await writer.flush()
        assert any(isinstance(r.event, MarketFrame) for r in CaptureReader(tmp_path).records())
        conn.release.set()
        await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await writer.close()


@pytest.mark.asyncio
async def test_unknown_mixed_frames_never_leak_secrets(tmp_path, capture_context, caplog):
    writer = await CaptureWriter.create(tmp_path, capture_context)
    conn = Harness(writer)
    secret = b"SENTINEL_PRIVATE_TOKEN"
    conn.transport = FakeTransport(
        [b'{"access_token":"' + secret + b'"}', b'{"access_token":"' + secret]
    )
    await conn.run(max_reconnects=0)
    await writer.close()
    assert conn._dlq.drain() == []
    assert secret.decode() not in caplog.text
    records = list(CaptureReader(tmp_path).records())
    assert (
        len(
            [
                r
                for r in records
                if isinstance(r.event, ControlMetadata) and r.event.kind != "connected"
            ]
        )
        == 2
    )
    assert not any(isinstance(r.event, MarketFrame) for r in records)
    assert all(secret not in p.read_bytes() for p in tmp_path.iterdir())


@pytest.mark.asyncio
async def test_capture_failure_is_fatal_without_reconnect_or_dlq(tmp_path, capture_context):
    writer = await CaptureWriter.create(tmp_path, capture_context, queue_bytes=1)
    conn = Harness(writer)
    conn.transport = FakeTransport([MARKET])
    with pytest.raises(CaptureError):
        await conn.run(max_reconnects=2)
    assert conn._dlq.drain() == []
    with pytest.raises(CaptureError):
        await writer.close()


@pytest.mark.asyncio
async def test_idle_reader_is_stopped_by_disk_failure(tmp_path, capture_context):
    writer = await CaptureWriter.create(tmp_path, capture_context)
    conn = Harness(writer)

    class Idle(FakeTransport):
        async def _iter(self):
            await asyncio.Event().wait()
            yield b""

    conn.transport = Idle([])
    task = asyncio.create_task(conn.run(max_reconnects=2))
    await asyncio.sleep(0)
    writer._fail(OSError("disk full"))
    with pytest.raises(CaptureError):
        await asyncio.wait_for(task, 0.5)
    with pytest.raises(CaptureError):
        await writer.close()


@pytest.mark.asyncio
async def test_public_malformed_frame_is_preserved(tmp_path, capture_context):
    writer = await CaptureWriter.create(tmp_path, capture_context)
    conn = Harness(writer)
    conn.name = "binance"
    conn.transport = FakeTransport([b"{malformed market"])
    await conn.run(max_reconnects=0)
    await writer.close()
    frames = [
        r.event for r in CaptureReader(tmp_path).records() if isinstance(r.event, MarketFrame)
    ]
    assert frames[0].frame.payload == b"{malformed market"
    assert len(conn._dlq.drain()) == 1


@pytest.mark.asyncio
async def test_processor_overflow_stops_required_capture(tmp_path, capture_context):
    writer = await CaptureWriter.create(tmp_path, capture_context)
    conn = Harness(writer)
    conn._processor_budget = 1
    conn.transport = FakeTransport([MARKET])
    with pytest.raises(CaptureError, match="processor"):
        await conn.run(max_reconnects=2)
    await writer.close()
    assert conn._dlq.drain() == []
