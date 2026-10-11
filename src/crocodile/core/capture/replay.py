"""Offline replay follows journal decisions, rather than sorting UTC clocks."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Iterable, Iterator, Mapping
from typing import Literal, Protocol

from crocodile.core.capture.reader import CaptureReader
from crocodile.core.capture.schema import (
    ApplicationDecision,
    CaptureEvent,
    ControlMetadata,
    JournalRecord,
    MarketFrame,
    RegistryCheckpoint,
    ReplaySummary,
    SnapshotRequest,
    SnapshotResponse,
    StreamContext,
)
from crocodile.core.errors import CaptureIntegrityError
from crocodile.core.schema.records import Record
from crocodile.core.sink.base import Sink
from crocodile.crypto.instruments.registry import InstrumentRegistry


class ReplayDecoder(Protocol):
    version: int

    def bind(self, stream: StreamContext) -> None: ...
    def observe(self, event: CaptureEvent) -> None: ...
    def apply(
        self, decision: ApplicationDecision, origin: JournalRecord, registry: InstrumentRegistry
    ) -> Iterable[Record]: ...


class RawReplay:
    def __init__(self, reader: CaptureReader, decoders: Mapping[str, ReplayDecoder]):
        self.reader, self._templates = reader, decoders
        self.decoders: dict[str, ReplayDecoder] = {}
        self.last_ordinal = 0

    def _walk(self) -> Iterator[tuple[JournalRecord, tuple[Record, ...]]]:
        self.decoders = {}
        self.last_ordinal = 0
        for stream in self.reader.context.streams:
            if stream.stream_id in self.decoders:
                raise CaptureIntegrityError("Duplicate stream identity")
            template = self._templates.get(stream.decoder_id)
            if template is None or stream.decoder_version != template.version:
                raise CaptureIntegrityError("Unsupported decoder id/version")
            decoder = copy.deepcopy(template)
            decoder.bind(stream)
            self.decoders[stream.stream_id] = decoder
        registry = InstrumentRegistry()
        requests: dict[str, str] = {}
        for record in self.reader.records():
            self.last_ordinal = record.ordinal
            event = record.event
            emitted: tuple[Record, ...] = ()
            if isinstance(event, RegistryCheckpoint):
                registry = InstrumentRegistry()
                for instrument in event.instruments:
                    registry.add(instrument)
                for decoder in self.decoders.values():
                    decoder.observe(event)
            elif isinstance(event, SnapshotRequest):
                if event.request_id in requests or event.stream_id not in self.decoders:
                    raise CaptureIntegrityError("Duplicate request or unknown snapshot stream")
                requests[event.request_id] = event.stream_id
                self.decoders[event.stream_id].observe(event)
            elif isinstance(event, (MarketFrame, ControlMetadata)):
                if event.stream_id not in self.decoders:
                    raise CaptureIntegrityError("Unknown market/control stream")
                self.decoders[event.stream_id].observe(event)
            elif isinstance(event, SnapshotResponse):
                if event.request_id not in requests:
                    raise CaptureIntegrityError("Snapshot response has no preceding request")
            elif isinstance(event, ApplicationDecision):
                if not 0 < event.origin_ordinal < record.ordinal:
                    raise CaptureIntegrityError("Decision references future or missing origin")
                origin = self.reader.read_record(event.origin_ordinal)
                if isinstance(origin.event, MarketFrame):
                    stream_id = origin.event.stream_id
                elif isinstance(origin.event, SnapshotResponse):
                    stream_id = requests.get(origin.event.request_id, "")
                else:
                    raise CaptureIntegrityError("Decision references an invalid origin type")
                if stream_id not in self.decoders:
                    raise CaptureIntegrityError("Decision source is unavailable")
                try:
                    emitted = tuple(self.decoders[stream_id].apply(event, origin, registry))
                except CaptureIntegrityError:
                    raise
                except Exception as exc:
                    raise CaptureIntegrityError(
                        "Recorded application decision cannot be replayed"
                    ) from exc
            yield record, emitted

    def records(self) -> Iterator[Record]:
        for _, records in self._walk():
            yield from records

    async def run(
        self, out: Sink, *, pacing: Literal["fast", "original"] = "fast"
    ) -> ReplaySummary:
        if pacing not in ("fast", "original"):
            raise ValueError("Unknown raw replay pacing")
        previous: int | None = None
        count = 0
        for journal, records in self._walk():
            if pacing == "original":
                event = journal.event
                timestamp = None
                if isinstance(event, MarketFrame):
                    timestamp = event.frame.received_monotonic_ns
                    if timestamp is None:
                        raise CaptureIntegrityError("Original pacing needs observed receive clocks")
                elif isinstance(event, SnapshotResponse):
                    timestamp = event.received_monotonic_ns
                if timestamp is not None:
                    if previous is not None:
                        if timestamp < previous:
                            raise CaptureIntegrityError(
                                "Original pacing monotonic clock moved backwards"
                            )
                        await asyncio.sleep((timestamp - previous) / 1_000_000_000)
                    previous = timestamp
            for record in records:
                await out.put(record)
                count += 1
        return ReplaySummary(emitted_records=count, last_ordinal=self.last_ordinal)
