import asyncio
import json

import msgspec
import pytest

from crocodile.core.capture.reader import CaptureReader
from crocodile.core.capture.replay import RawReplay
from crocodile.core.capture.schema import CaptureContext, MarketFrame, RawFrame, StreamContext
from crocodile.core.capture.writer import CaptureWriter
from crocodile.core.errors import FatalConnectorError
from crocodile.core.ingest.transport import FakeTransport
from crocodile.core.sink.memory import MemorySink
from crocodile.crypto.exchanges.deribit.connector import DeribitConnector
from crocodile.crypto.exchanges.deribit.replay import DeribitReplayDecoder
from crocodile.crypto.instruments.registry import InstrumentRegistry


def notification(kind, change_id, prev=None, symbol="BTC-PERPETUAL", bids=None):
    data = {
        "instrument_name": symbol,
        "type": kind,
        "change_id": change_id,
        "timestamp": 100,
        "bids": bids or [],
        "asks": [],
    }
    if prev is not None:
        data["prev_change_id"] = prev
    return {
        "jsonrpc": "2.0",
        "method": "subscription",
        "params": {"channel": f"book.{symbol}.raw", "data": data},
    }


class Scripted(FakeTransport):
    def __init__(self):
        super().__init__([])
        self.queue = asyncio.Queue()
        self.sent = []
        self.subscribe_count = 0

    async def send(self, payload):
        r = json.loads(payload)
        self.sent.append(r)
        method = r["method"]
        if method == "public/auth":
            self.queue.put_nowait(
                {
                    "id": r["id"],
                    "result": {
                        "access_token": "ACCESS_SENTINEL",
                        "refresh_token": "REFRESH_SENTINEL",
                        "expires_in": 900,
                        "token_type": "bearer",
                    },
                }
            )
        elif method == "public/subscribe":
            self.subscribe_count += 1
            if self.subscribe_count == 1:
                # Before-ack snapshot must stay dropped even if processor runs later.
                self.queue.put_nowait(notification("snapshot", 0, bids=[["new", 999, 1]]))
            self.queue.put_nowait({"id": r["id"], "result": r["params"]["channels"]})
            if self.subscribe_count == 1:
                for msg in (
                    notification("snapshot", 10, bids=[["new", 100, 3]]),
                    notification("change", 14, 10, bids=[["change", 100, 2]]),
                    notification("change", 14, 10),
                    notification("snapshot", 1, symbol="ETH-PERPETUAL"),
                    notification("change", 20, 18),
                    # Already queued in the old generation when the gap is discovered.
                    notification("snapshot", 99, bids=[["new", 999, 1]]),
                    notification("change", 2, 1, symbol="ETH-PERPETUAL"),
                ):
                    self.queue.put_nowait(msg)
            else:
                self.queue.put_nowait(notification("snapshot", 100, bids=[["new", 101, 7]]))
                self.queue.put_nowait(notification("change", 105, 100, bids=[["delete", 101, 0]]))
                self.queue.put_nowait(None)
        elif method == "public/unsubscribe":
            self.queue.put_nowait(notification("change", 21, 20))
            self.queue.put_nowait({"id": r["id"], "result": r["params"]["channels"]})
        elif method == "public/set_heartbeat":
            self.queue.put_nowait({"id": r["id"], "result": "ok"})

    async def iter_frames(self):
        ordinal = 0
        while (msg := await self.queue.get()) is not None:
            ordinal += 1
            yield RawFrame(
                payload=json.dumps(msg).encode(),
                frame_kind="text",
                received_utc_ns=1000 - ordinal,
                received_monotonic_ns=ordinal,
                connection_id="fixture",
                receive_ordinal=ordinal,
            )


@pytest.fixture
async def recorded_deribit(tmp_path):
    writer = await CaptureWriter.create(
        tmp_path,
        CaptureContext(
            capture_id="deribit",
            streams=(
                StreamContext(
                    stream_id="s1",
                    source="deribit",
                    market="derivatives",
                    symbols=("BTC-PERPETUAL", "ETH-PERPETUAL"),
                    endpoint_id="deribit-raw-ws",
                    decoder_id="deribit-json",
                    decoder_version=1,
                ),
            ),
        ),
    )
    sink = MemorySink()
    conn = DeribitConnector(
        ["BTC-PERPETUAL", "ETH-PERPETUAL"],
        ["book_delta"],
        sink,
        InstrumentRegistry(),
        interval="raw",
        client_id="ID_SENTINEL",
        client_secret="SECRET_SENTINEL",
    )
    conn.capture = writer
    conn.transport = Scripted()
    await asyncio.wait_for(conn.run(max_reconnects=0), 3)
    await writer.close()
    return CaptureReader(tmp_path), sink.records, conn


def test_raw_replay_matches_live_sequences_and_final_books(recorded_deribit, monkeypatch, caplog):
    reader, records, conn = recorded_deribit

    def forbidden(*args, **kwargs):
        raise AssertionError("Replay must not open a session or network")

    monkeypatch.setattr("aiohttp.ClientSession", forbidden)
    monkeypatch.setattr(
        "crocodile.crypto.exchanges.deribit.session.DeribitSession.begin", forbidden
    )
    replay = RawReplay(reader, {"deribit-json": DeribitReplayDecoder()})
    assert [msgspec.msgpack.encode(r) for r in replay.records()] == [
        msgspec.msgpack.encode(r) for r in records
    ]
    assert [getattr(r, "sequence_id", getattr(r, "seq_id", None)) for r in records] == [
        10,
        14,
        1,
        2,
        100,
        105,
    ]
    gate = replay.decoders["s1"].gate
    assert gate.bids["BTC-PERPETUAL"] == {}
    assert gate.last_change_id("ETH-PERPETUAL") == 2
    assert conn.transport.subscribe_count == 2
    assert conn._control._access_token is conn._control._refresh_token is None
    for path in reader.root.iterdir():
        assert b"SENTINEL" not in path.read_bytes()
    assert "SENTINEL" not in caplog.text
    assert conn._dlq.drain() == []
    assert sum(isinstance(r.event, MarketFrame) for r in reader.records()) == 11


def test_deribit_benchmark_counts_changes_separately(recorded_deribit):
    from scripts.benchmark_raw_capture import benchmark_capture

    reader, _, _ = recorded_deribit
    result = benchmark_capture(reader, repeats=2)
    assert result["new_depth_events"] == 3
    assert result["stale_or_duplicate_depth_frames"] == 1
    assert result["replay_records_total"] == 12


async def test_unknown_action_fails_without_dead_lettering_secret_data():
    transport = Scripted()
    original = transport.send

    async def malformed(payload):
        await original(payload)
        if json.loads(payload)["method"] == "public/subscribe":
            transport.queue.put_nowait(notification("snapshot", 999, bids=[["bad", 100, 1]]))

    transport.send = malformed
    conn = DeribitConnector(
        ["BTC-PERPETUAL"],
        ["book_delta"],
        MemorySink(),
        InstrumentRegistry(),
        interval="raw",
        client_id="id",
        client_secret="secret",
    )
    conn.transport = transport
    with pytest.raises(FatalConnectorError):
        await asyncio.wait_for(conn.run(max_reconnects=0), 3)
    assert conn._dlq.drain() == []
