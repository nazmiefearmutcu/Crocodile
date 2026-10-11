import pytest

from crocodile.core.capture.reader import CaptureReader
from crocodile.core.capture.schema import (
    ApplicationDecision,
    CaptureContext,
    MarketFrame,
    RegistryCheckpoint,
    SnapshotRequest,
    SnapshotResponse,
    StreamContext,
)
from crocodile.core.capture.snapshots import HttpSnapshotProvider, RecordedSnapshotProvider
from crocodile.core.capture.writer import CaptureWriter


@pytest.mark.asyncio
async def test_live_reader_records_depth_while_snapshot_is_pending(tmp_path):
    import asyncio
    import json

    from crocodile.core.capture.snapshots import CapturedSnapshot
    from crocodile.core.ingest.transport import FakeTransport
    from crocodile.core.sink.memory import MemorySink
    from crocodile.crypto.exchanges.binance.connector import BinanceConnector
    from crocodile.crypto.instruments.registry import InstrumentRegistry

    writer = await CaptureWriter.create(
        tmp_path,
        CaptureContext(
            capture_id="race",
            streams=(
                StreamContext(
                    stream_id="s1",
                    source="binance-spot",
                    market="spot",
                    symbols=("BTCUSDT",),
                    endpoint_id="binance-public",
                    decoder_id="binance-json",
                    decoder_version=1,
                ),
            ),
        ),
    )
    started, release = asyncio.Event(), asyncio.Event()

    class Provider:
        async def fetch(self, request):
            await writer.append(request)
            started.set()
            await release.wait()
            response = SnapshotResponse(
                request_id=request.request_id,
                status=200,
                payload=b'{"lastUpdateId":100,"bids":[],"asks":[]}',
                received_utc_ns=20,
                received_monotonic_ns=30,
            )
            ordinal = await writer.append(response)
            return CapturedSnapshot(response=response, origin_ordinal=ordinal)

    out = MemorySink()
    conn = BinanceConnector(["BTCUSDT"], ["book_delta"], out, InstrumentRegistry())
    conn.capture = writer
    conn.snapshot_provider = Provider()
    conn.transport = FakeTransport(
        [
            json.dumps(
                {"e": "depthUpdate", "s": "BTCUSDT", "U": u, "u": u, "b": [[10, u]], "a": []}
            ).encode()
            for u in (101, 102)
        ]
    )
    task = asyncio.create_task(conn.run(max_reconnects=0))
    try:
        await asyncio.wait_for(started.wait(), 0.5)
        await writer.flush()
        records = list(CaptureReader(tmp_path).records())
        assert isinstance(records[0].event, RegistryCheckpoint)
        assert len([r for r in records if isinstance(r.event, MarketFrame)]) == 2
        assert out.records == []
        release.set()
        await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await writer.close()
    decisions = [
        r.event
        for r in CaptureReader(tmp_path).records()
        if isinstance(r.event, ApplicationDecision)
    ]
    assert [d.action for d in decisions] == ["snapshot", "apply", "apply"]
    assert len(out.records) == 3


@pytest.mark.asyncio
async def test_snapshot_preserves_original_body_and_can_be_read_without_http(tmp_path):
    payload = b'{ "lastUpdateId": 100, "bids": [], "asks": [] }'

    class Response:
        status = 200

        async def read(self):
            return payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class HTTP:
        def get(self, url, **kwargs):
            assert url == "https://api.binance.com/api/v3/depth"
            return Response()

    writer = await CaptureWriter.create(tmp_path, CaptureContext(capture_id="test", streams=()))
    request = SnapshotRequest(
        request_id="req",
        stream_id="s1",
        source="binance-spot",
        symbol_raw="BTCUSDT",
        path="/api/v3/depth",
        params={"symbol": "BTCUSDT", "limit": 1000},
    )
    captured = await HttpSnapshotProvider(HTTP(), writer, base_url="https://api.binance.com").fetch(
        request
    )
    await writer.close()
    records = list(CaptureReader(tmp_path).records())
    assert records[0].event == request
    assert isinstance(records[1].event, SnapshotResponse)
    assert captured.response.payload == payload
    assert captured.origin_ordinal == 2
    assert await RecordedSnapshotProvider(CaptureReader(tmp_path)).fetch(request) == captured
