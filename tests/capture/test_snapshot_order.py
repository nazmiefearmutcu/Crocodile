import asyncio

from crocodile.core.capture.reader import CaptureReader
from crocodile.core.capture.replay import RawReplay
from crocodile.core.capture.schema import MarketFrame, RawFrame, SnapshotRequest, SnapshotResponse
from crocodile.core.capture.snapshots import HttpSnapshotProvider
from crocodile.core.capture.writer import CaptureWriter
from crocodile.core.sink.memory import MemorySink
from crocodile.crypto.exchanges.binance.replay import BinanceReplayDecoder


async def test_snapshot_is_journaled_before_yielding_http_cleanup(
    tmp_path, capture_context, monkeypatch
):
    writer = await CaptureWriter.create(tmp_path, capture_context)
    clock = [10]
    monkeypatch.setattr("crocodile.core.capture.snapshots.time.monotonic_ns", lambda: clock[0])

    class Response:
        status = 200

        async def read(self):
            return b'{"lastUpdateId":100,"bids":[],"asks":[]}'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await asyncio.sleep(0)
            clock[0] = 20
            await writer.append(
                MarketFrame(
                    stream_id="s1",
                    frame=RawFrame(
                        payload=b"{}",
                        frame_kind="text",
                        received_utc_ns=100,
                        received_monotonic_ns=20,
                        connection_id="c1",
                        receive_ordinal=1,
                    ),
                    observed_utc_ns=100,
                    observed_monotonic_ns=20,
                )
            )

    class HTTP:
        def get(self, *args, **kwargs):
            return Response()

    request = SnapshotRequest(
        request_id="r1",
        stream_id="s1",
        source="binance-spot",
        symbol_raw="BTCUSDT",
        path="/api/v3/depth",
        params={"symbol": "BTCUSDT", "limit": 1000},
    )
    await HttpSnapshotProvider(HTTP(), writer, base_url="https://api.binance.com").fetch(request)
    await writer.close()
    reader = CaptureReader(tmp_path)
    events = [r.event for r in reader.records()]
    assert isinstance(events[1], SnapshotResponse)
    assert isinstance(events[2], MarketFrame)
    sleeps = []

    async def record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("crocodile.core.capture.replay.asyncio.sleep", record_sleep)
    await RawReplay(reader, {"binance-json": BinanceReplayDecoder()}).run(
        MemorySink(), pacing="original"
    )
    assert sleeps == [10 / 1_000_000_000]
