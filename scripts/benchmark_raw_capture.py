"""Read-only fixture/replay throughput; never a live exchange cadence measurement."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from crocodile.core.capture.reader import CaptureReader
from crocodile.core.capture.replay import RawReplay, ReplayDecoder
from crocodile.core.capture.schema import ApplicationDecision, SnapshotRequest
from crocodile.core.schema.records import BookDelta, BookTicker
from crocodile.crypto.exchanges.binance.replay import BinanceReplayDecoder, BinanceSbeReplayDecoder
from crocodile.crypto.exchanges.deribit.replay import DeribitReplayDecoder


def benchmark_capture(reader: CaptureReader, *, repeats: int = 1) -> dict[str, int | float | str]:
    if repeats < 1:
        raise ValueError("repeats must be positive")
    decoders: dict[str, ReplayDecoder] = {
        "binance-json": BinanceReplayDecoder(),
        "binance-sbe": BinanceSbeReplayDecoder(),
        "deribit-json": DeribitReplayDecoder(),
    }
    limits = []
    stale = journal_count = 0
    for record in reader.records():
        journal_count += 1
        if isinstance(record.event, SnapshotRequest):
            limits.append(int(record.event.params["limit"]))
        if (
            isinstance(record.event, ApplicationDecision)
            and record.event.reason in ("book_sync", "deribit_book")
            and record.event.action == "drop"
        ):
            stale += 1
    start = time.perf_counter()
    emitted = depth = bbo = 0
    for iteration in range(repeats):
        for canonical in RawReplay(reader, decoders).records():
            emitted += 1
            if iteration == 0:
                depth += isinstance(canonical, BookDelta)
                bbo += isinstance(canonical, BookTicker)
    elapsed = time.perf_counter() - start
    stats = reader.manifest.stats
    result: dict[str, int | float | str] = {
        "measurement": "offline replay; not live cadence",
        "capture_stats_scope": "seal-time snapshot" if stats else "unavailable in older manifest",
        "journal_records": journal_count,
        "canonical_records_per_pass": emitted // repeats,
        "replay_records_total": emitted,
        "replay_seconds": elapsed,
        "replay_records_per_second": emitted / elapsed if elapsed else 0.0,
        "new_depth_events": depth,
        "stale_or_duplicate_depth_frames": stale,
        "book_ticker_records": bbo,
        "snapshot_limit_per_side": max(limits) if limits else "not REST anchored",
        "book_coverage": (
            "REST seed plus observed changed levels; not entire market liquidity"
            if limits
            else "venue snapshot plus continuous accepted L2 changes"
        ),
        "precision": "raw bytes; SBE int64 mantissa/signed exponent; canonical float64",
        "fsynced_records": reader.manifest.fsynced_ordinal,
    }
    for name in (
        "received_frames",
        "queued_records",
        "written_records",
        "peak_queued_bytes",
        "durable_lag_ns",
        "peak_durable_lag_ns",
        "durable_lag_overruns",
    ):
        result[name] = getattr(stats, name) if stats else "unavailable"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_dir", type=Path)
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    print(
        json.dumps(
            benchmark_capture(CaptureReader(args.capture_dir), repeats=args.repeats), indent=2
        )
    )


if __name__ == "__main__":
    main()
