import json
from struct import pack_into

import msgspec
import pytest

from crocodile.core.capture.replay import RawReplay
from crocodile.core.errors import (
    CaptureIntegrityError,
    FatalConnectorError,
)
from crocodile.core.ingest.transport import FakeTransport
from crocodile.core.sink.memory import MemorySink
from crocodile.crypto.exchanges.binance.connector import BinanceConnector
from crocodile.crypto.exchanges.binance.replay import BinanceSbeReplayDecoder
from crocodile.crypto.exchanges.binance.sbe import UnsupportedSbeTemplate
from crocodile.crypto.instruments.registry import InstrumentRegistry


def test_binary_replay_matches_live_book_and_original_timestamps(
    recorded_sbe_session, network_forbidden
):
    reader, expected, _ = recorded_sbe_session
    replay = RawReplay(reader, {"binance-sbe": BinanceSbeReplayDecoder()})
    assert [msgspec.msgpack.encode(r) for r in replay.records()] == [
        msgspec.msgpack.encode(r) for r in expected
    ]
    state = replay.decoders["s1"].states["BTCUSDT"]
    assert state.bids == {} and state.asks == {30000.13: 2.0}
    assert all(b"SENTINEL_KEY" not in p.read_bytes() for p in reader.root.iterdir())


def test_bbo_and_duplicate_frames_do_not_inflate_new_depth_counter(recorded_sbe_session):
    from scripts.benchmark_raw_capture import benchmark_capture
    reader, _, stats = recorded_sbe_session
    result = benchmark_capture(reader, repeats=3)
    assert result["received_frames"] == stats.received_frames == 4
    assert result["new_depth_events"] == 1
    assert result["stale_or_duplicate_depth_frames"] == 1
    assert result["book_ticker_records"] == 1
    assert result["replay_records_total"] == 12


@pytest.mark.asyncio
async def test_server_shutdown_triggers_reconnect(monkeypatch):
    import crocodile.core.connector as base

    async def no_wait(*args):
        pass

    monkeypatch.setattr(base.asyncio, "sleep", no_wait)

    class Reconnecting(FakeTransport):
        count = 0

        async def connect(self):
            self.count += 1

        async def _iter(self):
            if self.count == 1:
                yield json.dumps({"e": "serverShutdown", "E": 10}).encode()

    conn = BinanceConnector(
        ["BTCUSDT"],
        ["trade"],
        MemorySink(),
        InstrumentRegistry(),
        wire_format="sbe",
        api_key="fixture-key",
    )
    conn.transport = Reconnecting([])
    await conn.run(max_reconnects=1)
    assert conn.transport.count == 2


@pytest.mark.asyncio
async def test_subscription_error_is_sanitized_fatal():
    conn = BinanceConnector(
        ["BTCUSDT"],
        ["trade"],
        MemorySink(),
        InstrumentRegistry(),
        wire_format="sbe",
        api_key="fixture-key",
    )
    conn.transport = FakeTransport([b'{"id":1,"error":{"code":2,"message":"SENTINEL_SECRET"}}'])
    with pytest.raises(FatalConnectorError) as error:
        await conn.run(max_reconnects=3)
    assert "SENTINEL_SECRET" not in str(error.value)
    assert conn._dlq.drain() == []


@pytest.mark.asyncio
async def test_unknown_binary_template_is_raw_captured_and_ends_continuity(tmp_path, sbe_fixture):
    from crocodile.core.capture.reader import CaptureReader
    from crocodile.core.capture.schema import CaptureContext, MarketFrame, StreamContext
    from crocodile.core.capture.writer import CaptureWriter

    payload = bytearray(sbe_fixture("depth_diff.bin"))
    pack_into("<H", payload, 2, 50000)
    writer = await CaptureWriter.create(
        tmp_path,
        CaptureContext(
            capture_id="bad",
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
    conn = BinanceConnector(
        ["BTCUSDT"],
        ["trade"],
        MemorySink(),
        InstrumentRegistry(),
        wire_format="sbe",
        api_key="fixture-key",
    )
    conn.capture, conn.transport = writer, FakeTransport([bytes(payload)])
    with pytest.raises(UnsupportedSbeTemplate):
        await conn.run(max_reconnects=0)
    await writer.close()
    reader = CaptureReader(tmp_path)
    frames = [r.event for r in reader.records() if isinstance(r.event, MarketFrame)]
    assert frames[0].frame.payload == bytes(payload)
    assert conn._dlq.drain() == []
    with pytest.raises(CaptureIntegrityError):
        list(RawReplay(reader, {"binance-sbe": BinanceSbeReplayDecoder()}).records())
