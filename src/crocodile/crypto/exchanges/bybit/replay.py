from __future__ import annotations

import json
from collections.abc import Iterable
from decimal import Decimal

from crocodile.core.capture.schema import (
    ApplicationDecision,
    CaptureEvent,
    JournalRecord,
    SnapshotRequest,
    SnapshotResponse,
    StreamContext,
)
from crocodile.core.errors import CaptureIntegrityError
from crocodile.core.schema.records import Record
from crocodile.crypto.exchanges.public_books import PublicReplayDecoder
from crocodile.crypto.instruments.registry import InstrumentRegistry

from .book import BybitBookGate


class BybitReplayDecoder(PublicReplayDecoder):
    source = "bybit"
    gate: BybitBookGate

    def __init__(self) -> None:
        self.gate = BybitBookGate()

    def bind(self, stream: StreamContext) -> None:
        try:
            source, category, mode, depth = stream.endpoint_id.split("-")
            if (
                source != "bybit"
                or category not in {"spot", "linear", "inverse", "option"}
                or mode not in {"fast", "full"}
                or stream.market != category
                or int(depth) not in ({25, 100} if category == "option" else {1, 50, 200, 1000})
                or (mode == "full" and category == "option")
            ):
                raise ValueError
            self.gate = BybitBookGate(mode=mode, depth=int(depth))
        except ValueError:
            raise CaptureIntegrityError("Bybit replay endpoint mismatch") from None
        super().bind(stream)

    def observe(self, event: CaptureEvent) -> None:
        if isinstance(event, SnapshotRequest):
            if (
                self.gate.mode != "full"
                or event.source != "bybit"
                or event.path != "/v5/market/full_orderbook"
                or event.symbol_raw not in self.stream.symbols
                or event.params != {"category": self.stream.market, "symbol": event.symbol_raw}
            ):
                raise CaptureIntegrityError("Bybit replay snapshot request mismatch")
        super().observe(event)

    def apply(
        self, decision: ApplicationDecision, origin: JournalRecord, registry: InstrumentRegistry
    ) -> Iterable[Record]:
        if isinstance(origin.event, SnapshotResponse):
            if (
                self.gate.mode != "full"
                or decision.reason != "bybit_full_anchor"
                or decision.action != "snapshot"
            ):
                raise CaptureIntegrityError("Bybit full snapshot decision mismatch")
            data = json.loads(origin.event.payload, parse_float=Decimal)
            if data.get("retCode") != 0 or origin.event.status != 200:
                raise CaptureIntegrityError("Bybit replay REST anchor rejected")
            if data["result"].get("s") not in self.stream.symbols:
                raise CaptureIntegrityError("Bybit replay anchor symbol mismatch")
            result = self.gate.anchor(data["result"], origin.event.received_utc_ns, registry)
            if decision.record_indices != tuple(range(len(result.records))):
                raise CaptureIntegrityError("Bybit full anchor indices mismatch")
            return result.records
        return super().apply(decision, origin, registry)
