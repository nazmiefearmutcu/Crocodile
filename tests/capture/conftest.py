import pytest

from crocodile.core.capture.schema import CaptureContext, StreamContext


@pytest.fixture
def capture_context():
    return CaptureContext(
        capture_id="fixture",
        streams=(
            StreamContext(
                stream_id="s1",
                source="binance-spot",
                market="spot",
                symbols=("BTCUSDT",),
                endpoint_id="binance-spot-ws",
                decoder_id="binance-json",
                decoder_version=1,
            ),
        ),
    )


@pytest.fixture
async def recorded_json_session(tmp_path, capture_context):
    import json

    from crocodile.core.capture.reader import CaptureReader
    from crocodile.core.capture.schema import SnapshotResponse
    from crocodile.core.capture.snapshots import CapturedSnapshot
    from crocodile.core.capture.writer import CaptureWriter
    from crocodile.core.ingest.transport import FakeTransport
    from crocodile.core.sink.memory import MemorySink
    from crocodile.crypto.exchanges.binance.connector import BinanceConnector
    from crocodile.crypto.instruments.registry import Instrument, InstrumentRegistry, Kind

    writer = await CaptureWriter.create(tmp_path, capture_context)
    registry = InstrumentRegistry()
    registry.add(
        Instrument(
            "binance-spot:BTCUSDT",
            "binance-spot",
            "BTCUSDT",
            Kind.SPOT,
            "BTC",
            "USDT",
            tick_size=0.01,
        )
    )

    class Provider:
        def __init__(self):
            self.ids = iter((100, 200))

        async def fetch(self, request):
            await writer.append(request)
            response = SnapshotResponse(
                request_id=request.request_id,
                status=200,
                payload=json.dumps(
                    {"lastUpdateId": next(self.ids), "bids": [[10, 1]], "asks": [[11, 2]]}
                ).encode(),
                received_utc_ns=50,
                received_monotonic_ns=60,
            )
            ordinal = await writer.append(response)
            return CapturedSnapshot(response=response, origin_ordinal=ordinal)

    out = MemorySink()
    conn = BinanceConnector(["BTCUSDT"], ["book_delta", "trade"], out, registry)
    conn.capture, conn.snapshot_provider = writer, Provider()
    conn.transport = FakeTransport(
        [
            json.dumps(
                {
                    "e": "depthUpdate",
                    "s": "BTCUSDT",
                    "E": 123,
                    "U": U,
                    "u": u,
                    "b": [[10, u]],
                    "a": [[11, 0]],
                }
            ).encode()
            for U, u in ((99, 101), (99, 101), (102, 103), (200, 210), (201, 205))
        ]
    )
    await conn.run(max_reconnects=0)
    await writer.close()
    return CaptureReader(tmp_path), out.records
