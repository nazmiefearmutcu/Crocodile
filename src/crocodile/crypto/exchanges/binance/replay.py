"""Pure JSON replay adapter; never constructs a connector or HTTP session."""

from __future__ import annotations

import json
from collections.abc import Iterable

from crocodile.core.capture.schema import (
    ApplicationDecision,
    CaptureEvent,
    JournalRecord,
    MarketFrame,
    SnapshotRequest,
    SnapshotResponse,
    StreamContext,
)
from crocodile.core.errors import CaptureIntegrityError
from crocodile.core.ingest.book_sync import SyncResult
from crocodile.core.schema.records import Record
from crocodile.crypto.exchanges.binance.book import parse_rest_depth_snapshot
from crocodile.crypto.exchanges.binance.book_session import BinanceBookState, json_depth
from crocodile.crypto.exchanges.binance.normalize import normalize_message
from crocodile.crypto.instruments.registry import InstrumentRegistry


class BinanceReplayDecoder:
    version = 1

    def __init__(self):
        self.source = "binance-spot"
        self.venue = "spot"
        self.states: dict[str, BinanceBookState] = {}
        self.requests: dict[str, SnapshotRequest] = {}

    def bind(self, stream: StreamContext) -> None:
        if stream.source not in {"binance-spot", "binance-usdm", "binance-coinm"}:
            raise CaptureIntegrityError("Binance decoder source mismatch")
        self.source = stream.source
        self.venue = "spot" if stream.market == "spot" else "futures"
        self.states.clear()
        self.requests.clear()

    def observe(self, event: CaptureEvent) -> None:
        if isinstance(event, SnapshotRequest):
            if event.source != self.source:
                raise CaptureIntegrityError("Snapshot source mismatch")
            self.requests[event.request_id] = event

    def _market_records(
        self, event: MarketFrame, decision: ApplicationDecision, registry: InstrumentRegistry
    ) -> tuple[Record, ...]:
        msg = json.loads(event.frame.payload)
        data = msg.get("data", msg)
        if decision.reason == "book_sync":
            if data.get("e") != "depthUpdate":
                raise CaptureIntegrityError("Book decision refers to another message")
            depth = json_depth(msg)
            state = self.states.get(depth.symbol_raw)
            if state is None or not state.valid:
                raise CaptureIntegrityError("Missing valid book snapshot anchor")
            result, records = state.accept_depth(depth, local_ts=event.observed_utc_ns)
            expected = "gap" if result == SyncResult.RESYNC else result.value
            if decision.action != expected:
                raise CaptureIntegrityError("Recorded depth decision disagrees with sequence")
            return records
        if decision.action != "apply":
            raise CaptureIntegrityError("Unexpected non-book application action")
        return tuple(
            normalize_message(
                msg, local_ts=event.observed_utc_ns, venue=self.source, registry=registry
            )
        )

    def apply(
        self, decision: ApplicationDecision, origin: JournalRecord, registry: InstrumentRegistry
    ) -> Iterable[Record]:
        event = origin.event
        if isinstance(event, SnapshotResponse):
            request = self.requests.pop(event.request_id, None)
            if request is None or event.status != 200 or decision.action != "snapshot":
                raise CaptureIntegrityError("Missing or invalid snapshot request/anchor")
            snapshot = parse_rest_depth_snapshot(
                json.loads(event.payload),
                symbol_raw=request.symbol_raw,
                venue=self.source,
                registry=registry,
                local_ts=event.received_utc_ns,
            )
            if snapshot.sequence_id is None:
                raise CaptureIntegrityError("Snapshot anchor has no sequence ID")
            state = self.states.setdefault(request.symbol_raw, BinanceBookState(self.venue))
            records = state.accept_snapshot(snapshot)
        elif isinstance(event, MarketFrame):
            records = self._market_records(event, decision, registry)
        else:
            raise CaptureIntegrityError("Invalid replay origin")
        indices = decision.record_indices
        if (
            len(set(indices)) != len(indices)
            or any(i < 0 or i >= len(records) for i in indices)
            or indices != tuple(range(len(records)))
        ):
            raise CaptureIntegrityError("Canonical emission indices mismatch")
        return records
