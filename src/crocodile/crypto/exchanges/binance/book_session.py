"""Pure Binance range validation and the asynchronous snapshot wrapper."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from typing import Any, Literal, NoReturn

import msgspec

from crocodile.core.capture.emitter import CaptureEmitter
from crocodile.core.capture.schema import ApplicationDecision, SnapshotRequest
from crocodile.core.capture.snapshots import CapturedSnapshot, SnapshotProvider
from crocodile.core.capture.writer import CaptureWriter
from crocodile.core.errors import CaptureError, TransientConnectorError
from crocodile.core.ingest.book_sync import OrderBookSync, SyncResult
from crocodile.core.schema.enums import AssetClass
from crocodile.core.schema.records import BookDelta, BookSnapshot, Record
from crocodile.core.sink.base import Sink
from crocodile.crypto.exchanges.binance.book import parse_rest_depth_snapshot
from crocodile.crypto.instruments.registry import InstrumentRegistry


class BinanceDepthEvent(msgspec.Struct, frozen=True, kw_only=True):
    symbol_raw: str
    source_ts_ns: int | None
    U: int
    u: int
    pu: int | None
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]


def json_depth(msg: dict[str, Any], *, timestamp_scale: int = 1_000_000) -> BinanceDepthEvent:
    data = msg.get("data", msg)
    return BinanceDepthEvent(
        symbol_raw=data["s"],
        source_ts_ns=int(data["E"]) * timestamp_scale if data.get("E") is not None else None,
        U=int(data["U"]),
        u=int(data["u"]),
        pu=int(data["pu"]) if data.get("pu") is not None else None,
        bids=tuple((float(p), float(q)) for p, q in data.get("b", [])),
        asks=tuple((float(p), float(q)) for p, q in data.get("a", [])),
    )


class BinanceBookState:
    def __init__(self, venue: Literal["spot", "futures"]):
        self.sync = OrderBookSync(venue)
        self.snapshot: BookSnapshot | None = None
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.valid = False

    def accept_snapshot(self, snapshot: BookSnapshot) -> tuple[Record, ...]:
        if snapshot.sequence_id is None:
            raise ValueError("Binance snapshot missing sequence_id")
        self.sync.set_snapshot(snapshot.sequence_id)
        self.snapshot = snapshot
        self.bids, self.asks = dict(snapshot.bids), dict(snapshot.asks)
        self.valid = True
        return (snapshot,)

    def accept_depth(
        self, event: BinanceDepthEvent, *, local_ts: int
    ) -> tuple[SyncResult, tuple[Record, ...]]:
        if not self.valid or self.snapshot is None:
            raise ValueError("Binance depth has no valid snapshot anchor")
        if event.symbol_raw != self.snapshot.symbol_raw or event.U > event.u:
            raise ValueError("Invalid Binance depth range or symbol")
        result = self.sync.feed(event.U, event.u, event.pu)
        if result == SyncResult.RESYNC:
            self.valid = False
            self.bids.clear()
            self.asks.clear()
        if result != SyncResult.APPLY:
            return result, ()
        for side, levels in ((self.bids, event.bids), (self.asks, event.asks)):
            for price, size in levels:
                if size == 0:
                    side.pop(price, None)
                else:
                    side[price] = size
        record = BookDelta(
            source=self.snapshot.source,
            symbol=self.snapshot.symbol,
            symbol_raw=event.symbol_raw,
            local_ts=local_ts,
            source_ts=event.source_ts_ns,
            asset_class=AssetClass.CRYPTO,
            bids=list(event.bids),
            asks=list(event.asks),
            seq_id=event.u,
            prev_seq_id=event.pu,
        )
        return result, (record,)


class BinanceBookSession:
    def __init__(
        self,
        venue: Literal["spot", "futures"],
        snapshot_provider: SnapshotProvider,
        out: Sink,
        registry: InstrumentRegistry,
        capture: CaptureWriter | None,
        *,
        source: str | None = None,
        stream_id: str = "s1",
        path: str | None = None,
        depth_limit: int = 1000,
        buffer_bytes: int = 32 << 20,
    ):
        self.venue, self.provider, self.out = venue, snapshot_provider, out
        self.registry, self.capture = registry, capture
        self.source = source or ("binance-spot" if venue == "spot" else "binance-usdm")
        self.stream_id = stream_id
        self.path = path or ("/api/v3/depth" if venue == "spot" else "/fapi/v1/depth")
        self.depth_limit, self.budget = depth_limit, buffer_bytes
        self.states: dict[str, BinanceBookState] = {}
        self.buffers: dict[str, deque[tuple[BinanceDepthEvent, int | None, int, int]]] = {}
        self.jobs: dict[str, asyncio.Task[None]] = {}
        self.requests: dict[str, str] = {}
        self._bytes = 0
        self._failure = asyncio.Event()
        self._error: Exception | None = None

    async def _emit(
        self,
        origin: int | None,
        records: tuple[Record, ...],
        *,
        action: Literal["apply", "drop", "gap", "snapshot"],
    ) -> None:
        if self.capture is not None:
            if origin is None:
                raise CaptureError("Book emission has no raw origin")
            await CaptureEmitter(self.capture, self.out).emit(
                ApplicationDecision(
                    origin_ordinal=origin,
                    record_indices=tuple(range(len(records))),
                    action=action,
                    reason="book_sync",
                ),
                records,
            )
        else:
            for record in records:
                await self.out.put(record)

    async def _apply(
        self, event: BinanceDepthEvent, origin: int | None, local_ts: int
    ) -> SyncResult:
        result, records = self.states[event.symbol_raw].accept_depth(event, local_ts=local_ts)
        action: Literal["apply", "drop", "gap", "snapshot"] = (
            "gap"
            if result == SyncResult.RESYNC
            else "drop"
            if result == SyncResult.DROP
            else "apply"
        )
        await self._emit(origin, records, action=action)
        return result

    async def accept_depth(
        self, event: BinanceDepthEvent, *, origin_ordinal: int | None, local_ts: int
    ) -> None:
        key = event.symbol_raw
        if (
            key not in self.jobs
            and key in self.states
            and self.states[key].valid
            and await self._apply(event, origin_ordinal, local_ts) != SyncResult.RESYNC
        ):
            return
        size = len(msgspec.msgpack.encode(event)) + 64
        if self._bytes + size > self.budget:
            raise CaptureError("Binance snapshot buffer byte overflow")
        self._bytes += size
        self.buffers.setdefault(key, deque()).append((event, origin_ordinal, local_ts, size))
        if key not in self.jobs:
            self.jobs[key] = asyncio.create_task(
                self._bootstrap(key), name=f"binance-snapshot-{key}"
            )
            self.jobs[key].add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )

    async def accept_snapshot(self, snapshot: CapturedSnapshot) -> None:
        key = self.requests.pop(snapshot.response.request_id)
        if snapshot.response.status != 200:
            raise TransientConnectorError("Snapshot HTTP status is not successful")
        canonical = parse_rest_depth_snapshot(
            json.loads(snapshot.response.payload),
            symbol_raw=key,
            venue=self.source,
            local_ts=snapshot.response.received_utc_ns,
            registry=self.registry,
        )
        state = self.states.setdefault(key, BinanceBookState(self.venue))
        records = state.accept_snapshot(canonical)
        await self._emit(snapshot.origin_ordinal, records, action="snapshot")

    async def _bootstrap(self, key: str) -> None:
        try:
            for _ in range(3):
                request_id = uuid.uuid4().hex
                self.requests[request_id] = key
                request = SnapshotRequest(
                    request_id=request_id,
                    stream_id=self.stream_id,
                    source=self.source,
                    symbol_raw=key,
                    path=self.path,
                    params={"symbol": key, "limit": self.depth_limit},
                )
                snapshot = await self.provider.fetch(request)
                await self.accept_snapshot(snapshot)
                buffer = self.buffers[key]
                while buffer:
                    event, origin, ts, size = buffer[0]
                    if await self._apply(event, origin, ts) == SyncResult.RESYNC:
                        break  # retain this range; a newer anchor must cover its gap
                    buffer.popleft()
                    self._bytes -= size
                if not buffer:
                    return
            raise TransientConnectorError("Snapshot anchors cannot bridge buffered depth gap")
        except Exception as exc:
            self._error = exc
            self._failure.set()
            raise
        finally:
            self.jobs.pop(key, None)

    async def drain(self) -> None:
        while self.jobs:
            await asyncio.gather(*tuple(self.jobs.values()))
        if self._error is not None:
            raise self._error

    async def wait_failure(self) -> NoReturn:
        await self._failure.wait()
        assert self._error is not None
        raise self._error

    async def reset(self, reason: str) -> None:
        tasks = tuple(self.jobs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()
        self.states.clear()
        self.buffers.clear()
        self.requests.clear()
        self._bytes = 0
