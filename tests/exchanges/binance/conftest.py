from pathlib import Path

import pytest


@pytest.fixture
def sbe_fixture():
    return lambda name: (Path(__file__).parent / "fixtures" / "sbe" / name).read_bytes()


@pytest.fixture
async def recorded_sbe_session(tmp_path, sbe_fixture):
    from crocodile.core.capture.reader import CaptureReader
    from crocodile.core.capture.schema import (
        CaptureContext,
        RawFrame,
        SnapshotResponse,
        StreamContext,
    )
    from crocodile.core.capture.snapshots import CapturedSnapshot
    from crocodile.core.capture.writer import CaptureWriter
    from crocodile.core.ingest.transport import FakeTransport
    from crocodile.core.sink.memory import MemorySink
    from crocodile.crypto.exchanges.binance.connector import BinanceConnector
    from crocodile.crypto.instruments.registry import InstrumentRegistry

    writer = await CaptureWriter.create(
        tmp_path,
        CaptureContext(
            capture_id="sbe",
            streams=(
                StreamContext(
                    stream_id="s1",
                    source="binance-spot",
                    market="spot",
                    symbols=("BTCUSDT",),
                    endpoint_id="binance-spot-sbe-ws",
                    decoder_id="binance-sbe",
                    decoder_version=1,
                    schema_id=1,
                    schema_version=0,
                ),
            ),
        ),
    )

    class Frames(FakeTransport):
        async def iter_frames(self):
            for i, name in enumerate(
                ("trades.bin", "best_bid_ask.bin", "depth_diff.bin", "depth_diff.bin"), 1
            ):
                yield RawFrame(
                    payload=sbe_fixture(name),
                    frame_kind="binary",
                    received_utc_ns=100 - i,
                    received_monotonic_ns=i * 1_000_000_000,
                    connection_id="c1",
                    receive_ordinal=i,
                )
            yield RawFrame(
                payload=b'{"id":1,"result":null}',
                frame_kind="text",
                received_utc_ns=90,
                received_monotonic_ns=4_000_000_001,
                connection_id="c1",
                receive_ordinal=5,
            )

    class Provider:
        async def fetch(self, request):
            await writer.append(request)
            response = SnapshotResponse(
                request_id=request.request_id,
                status=200,
                payload=b'{"lastUpdateId":100,"bids":[["30000.12","1.25"]],"asks":[]}',
                received_utc_ns=50,
                received_monotonic_ns=5_000_000_000,
            )
            ordinal = await writer.append(response)
            return CapturedSnapshot(response=response, origin_ordinal=ordinal)

    out = MemorySink()
    conn = BinanceConnector(
        ["BTCUSDT"],
        ["trade", "book_ticker", "book_delta"],
        out,
        InstrumentRegistry(),
        wire_format="sbe",
        api_key="SENTINEL_KEY",
        snapshot_provider=Provider(),
    )
    conn.capture, conn.transport = writer, Frames([])
    await conn.run(max_reconnects=0)
    summary = await writer.close()
    return CaptureReader(tmp_path), out.records, summary.stats
