"""Pure replay of captured Deribit market decisions; no session or credentials."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from crocodile.core.capture.schema import (
    ApplicationDecision,
    CaptureEvent,
    ControlMetadata,
    JournalRecord,
    MarketFrame,
    StreamContext,
)
from crocodile.core.errors import CaptureIntegrityError
from crocodile.core.schema.records import Record
from crocodile.crypto.instruments.registry import InstrumentRegistry

from .book import DeribitBookGate, DeribitProtocolError, validate_book
from .normalize import normalize_message


class DeribitReplayDecoder:
    version = 1

    def __init__(self) -> None:
        self.gate = DeribitBookGate()
        self.interval = "100ms"

    def bind(self, stream: StreamContext) -> None:
        if stream.source != "deribit" or stream.endpoint_id not in (
            "deribit-raw-ws",
            "deribit-100ms-ws",
        ):
            raise CaptureIntegrityError("Deribit replay source/interval mismatch")
        self.interval = "raw" if stream.endpoint_id == "deribit-raw-ws" else "100ms"
        self.gate.reset()

    def _message(self, event: MarketFrame) -> dict[str, Any]:
        msg = json.loads(event.frame.payload)
        if not isinstance(msg, dict) or msg.get("method") != "subscription":
            raise CaptureIntegrityError("Deribit market notification is invalid")
        params = msg.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("channel"), str):
            raise CaptureIntegrityError("Deribit market channel is invalid")
        if not params["channel"].endswith("." + self.interval):
            raise CaptureIntegrityError("Deribit recorded channel interval mismatch")
        if params["channel"].startswith("book."):
            data = params.get("data")
            if not isinstance(data, dict):
                raise CaptureIntegrityError("Deribit book payload is invalid")
            validate_book(data)
        return msg

    def observe(self, event: CaptureEvent) -> None:
        if isinstance(event, ControlMetadata) and event.kind == "connected":
            self.gate.reset()
        elif isinstance(event, MarketFrame):
            try:
                self._message(event)
            except (ValueError, DeribitProtocolError) as exc:
                raise CaptureIntegrityError(
                    "Malformed captured Deribit market notification"
                ) from exc

    def apply(
        self, decision: ApplicationDecision, origin: JournalRecord, registry: InstrumentRegistry
    ) -> Iterable[Record]:
        if not isinstance(origin.event, MarketFrame):
            raise CaptureIntegrityError("Deribit decision must refer to a market frame")
        event = origin.event
        msg = self._message(event)
        if decision.reason == "not_ready":
            if decision.action != "drop":
                raise CaptureIntegrityError("Deribit admission decision is invalid")
            records: tuple[Record, ...] = ()
        elif msg["params"]["channel"].startswith("book."):
            action = self.gate.evaluate(msg["params"]["data"])
            if decision.reason != "deribit_book" or decision.action != action:
                raise CaptureIntegrityError("Deribit replay sequence decision mismatch")
            records = (
                tuple(normalize_message(msg, local_ts=event.observed_utc_ns, registry=registry))
                if action in ("snapshot", "apply")
                else ()
            )
        else:
            if decision.action != "apply":
                raise CaptureIntegrityError("Deribit non-book decision is invalid")
            records = tuple(
                normalize_message(msg, local_ts=event.observed_utc_ns, registry=registry)
            )
        if decision.record_indices != tuple(range(len(records))):
            raise CaptureIntegrityError("Deribit canonical emission indices mismatch")
        return records
