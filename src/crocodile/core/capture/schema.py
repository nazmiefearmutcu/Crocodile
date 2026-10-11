"""Versioned raw events. Credentials never belong in these structures."""

from typing import Literal

import msgspec

from crocodile.crypto.instruments.registry import Instrument


class RawFrame(msgspec.Struct, frozen=True, kw_only=True):
    payload: bytes
    frame_kind: Literal["text", "binary", "unknown"]
    received_utc_ns: int | None
    received_monotonic_ns: int | None
    connection_id: str | None
    receive_ordinal: int | None


class StreamContext(msgspec.Struct, frozen=True, kw_only=True):
    stream_id: str
    source: str
    market: str
    symbols: tuple[str, ...]
    endpoint_id: str
    decoder_id: str
    decoder_version: int
    schema_id: int | None = None
    schema_version: int | None = None


class CaptureContext(msgspec.Struct, frozen=True, kw_only=True):
    capture_id: str
    streams: tuple[StreamContext, ...]


class MarketFrame(msgspec.Struct, tag="market", frozen=True, kw_only=True):
    stream_id: str
    frame: RawFrame
    observed_utc_ns: int
    observed_monotonic_ns: int


class SnapshotRequest(msgspec.Struct, tag="snapshot_request", frozen=True, kw_only=True):
    request_id: str
    stream_id: str
    source: str
    symbol_raw: str
    path: str
    params: dict[str, str | int]


class SnapshotResponse(msgspec.Struct, tag="snapshot_response", frozen=True, kw_only=True):
    request_id: str
    status: int
    payload: bytes
    received_utc_ns: int
    received_monotonic_ns: int


class RegistryCheckpoint(msgspec.Struct, tag="registry", frozen=True, kw_only=True):
    instruments: tuple[Instrument, ...]


class ApplicationDecision(msgspec.Struct, tag="decision", frozen=True, kw_only=True):
    origin_ordinal: int
    record_indices: tuple[int, ...]
    action: Literal["apply", "drop", "snapshot", "gap"]
    reason: str


class ControlMetadata(msgspec.Struct, tag="control", frozen=True, kw_only=True):
    stream_id: str
    kind: str
    connection_id: str | None
    request_id: str | None
    status: str
    observed_utc_ns: int


CaptureEvent = (MarketFrame | SnapshotRequest | SnapshotResponse | RegistryCheckpoint
                | ApplicationDecision | ControlMetadata)


class JournalRecord(msgspec.Struct, frozen=True, kw_only=True):
    ordinal: int
    event: CaptureEvent


class CaptureStats(msgspec.Struct, frozen=True, kw_only=True):
    received_frames: int = 0
    queued_records: int = 0
    written_records: int = 0
    fsynced_records: int = 0
    queued_ordinal: int = 0
    written_ordinal: int = 0
    fsynced_ordinal: int = 0
    queued_bytes: int = 0
    peak_queued_bytes: int = 0
    durable_lag_ns: int = 0
    failed: bool = False


class CaptureSummary(msgspec.Struct, frozen=True, kw_only=True):
    context: CaptureContext
    stats: CaptureStats
    sealed_segment_count: int


class RecoveryReport(msgspec.Struct, frozen=True, kw_only=True):
    recovered_records: int = 0
    discarded_tail_bytes: int = 0


class ReplaySummary(msgspec.Struct, frozen=True, kw_only=True):
    emitted_records: int
    last_ordinal: int
