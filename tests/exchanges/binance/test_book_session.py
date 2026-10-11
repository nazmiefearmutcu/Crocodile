import asyncio
import json

import pytest

from crocodile.core.capture.schema import SnapshotResponse
from crocodile.core.capture.snapshots import CapturedSnapshot
from crocodile.core.ingest.book_sync import OrderBookSync, SyncResult
from crocodile.core.sink.memory import MemorySink
from crocodile.crypto.exchanges.binance.book_session import BinanceBookSession, BinanceDepthEvent
from crocodile.crypto.instruments.registry import InstrumentRegistry


def event(U, u):
    return BinanceDepthEvent(
        symbol_raw="BTCUSDT", source_ts_ns=123, U=U, u=u, pu=None, bids=((10.0, 2.0),), asks=()
    )


def test_overlapping_spot_range_and_duplicate():
    sync = OrderBookSync(venue="spot")
    sync.set_snapshot(100)
    assert sync.feed(U=99, u=101, pu=None) == SyncResult.APPLY
    assert sync.feed(U=99, u=101, pu=None) == SyncResult.DROP
    assert sync.feed(U=100, u=103, pu=None) == SyncResult.APPLY
    assert sync.feed(U=105, u=106, pu=None) == SyncResult.RESYNC


@pytest.mark.asyncio
async def test_pending_snapshot_buffers_and_revalidates_every_range():
    ready = asyncio.Event()

    class Provider:
        async def fetch(self, request):
            await ready.wait()
            return CapturedSnapshot(
                response=SnapshotResponse(
                    request_id=request.request_id,
                    status=200,
                    payload=json.dumps(
                        {"lastUpdateId": 100, "bids": [[10, 1]], "asks": []}
                    ).encode(),
                    received_utc_ns=40,
                    received_monotonic_ns=50,
                ),
                origin_ordinal=None,
            )

    out = MemorySink()
    session = BinanceBookSession("spot", Provider(), out, InstrumentRegistry(), None)
    await session.accept_depth(event(99, 101), origin_ordinal=None, local_ts=11)
    await session.accept_depth(event(100, 101), origin_ordinal=None, local_ts=12)
    await session.accept_depth(event(102, 103), origin_ordinal=None, local_ts=13)
    assert out.records == []
    ready.set()
    await session.drain()
    assert [r.sequence_id if hasattr(r, "sequence_id") else r.seq_id for r in out.records] == [
        100,
        101,
        103,
    ]
    assert session.states["BTCUSDT"].bids == {10.0: 2.0}
    await session.reset("close")
