"""Safe streaming OKX archive replay with exact Decimal payloads and source-time order."""

from __future__ import annotations

import gzip
import io
import json
import math
import tarfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Self, TypedDict

from crocodile.core.schema.enums import AssetClass
from crocodile.core.schema.records import BookDelta, BookSnapshot

from .download import sha256_file
from .schema import ArchiveManifest, ArchiveSchemaError

DecimalLevel = tuple[Decimal, Decimal]


class _SafeTarInfo(tarfile.TarInfo):
    @classmethod
    def frombuf(cls, buf: bytes, encoding: str, errors: str) -> Self:
        info = super().frombuf(buf, encoding, errors)
        if info.type == tarfile.GNUTYPE_SPARSE:
            raise ArchiveSchemaError("sparse tar metadata is unsupported")
        if (
            info.type
            in {
                tarfile.XHDTYPE,
                tarfile.XGLTYPE,
                tarfile.GNUTYPE_LONGNAME,
                tarfile.GNUTYPE_LONGLINK,
            }
            and info.size > 64 * 1024
        ):
            raise ArchiveSchemaError("tar extended metadata exceeds resident byte budget")
        return info


class _BudgetReader(io.RawIOBase):
    """Bound every decompressed read before tar headers or PAX metadata allocate."""

    def __init__(self, source: gzip.GzipFile, limit: int) -> None:
        self.source = source
        self.remaining = limit

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        request = min(size if size >= 0 else 64 * 1024, self.remaining + 1, 64 * 1024)
        data = self.source.read(request)
        if len(data) > self.remaining:
            raise ArchiveSchemaError("decompressed archive exceeds parser byte budget")
        self.remaining -= len(data)
        return data


class _BookFields(TypedDict):
    source: str
    symbol: str
    symbol_raw: str
    asset_class: AssetClass
    local_ts: int
    source_ts: int
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    record: BookSnapshot | BookDelta
    member: str
    line: int
    archive_sha256: str
    decimal_bids: tuple[DecimalLevel, ...]
    decimal_asks: tuple[DecimalLevel, ...]
    continuity: str = "source-sequence-unavailable"


@dataclass(frozen=True, slots=True)
class HistoryReplay:
    bids: tuple[DecimalLevel, ...]
    asks: tuple[DecimalLevel, ...]
    records: int
    source_start_ns: int
    source_end_ns: int
    depth: int
    continuity: str
    possible_time_gaps: tuple[tuple[int, int], ...]
    notes: tuple[str, ...] = (
        "Time jumps are possible coverage gaps; they do not prove missing exchange events.",
        "Depth is the advertised visible level limit, not a full exchange book claim.",
    )


def _levels(raw: Any, depth: int) -> tuple[DecimalLevel, ...]:
    if not isinstance(raw, list) or len(raw) > depth * 2:
        raise ArchiveSchemaError("invalid or excessive level array")
    result: list[DecimalLevel] = []
    prices: set[Decimal] = set()
    for level in raw:
        if (
            not isinstance(level, list)
            or len(level) != 3
            or any(not isinstance(v, str) for v in level)
        ):
            raise ArchiveSchemaError("expected audited [price, quantity, orderCount] string schema")
        try:
            price, quantity = Decimal(level[0]), Decimal(level[1])
            count = int(level[2])
        except (ValueError, InvalidOperation) as exc:
            raise ArchiveSchemaError("invalid decimal quantity/price or count") from exc
        if (
            not price.is_finite()
            or not quantity.is_finite()
            or price <= 0
            or quantity < 0
            or count < 0
            or not math.isfinite(float(price))
            or not math.isfinite(float(quantity))
        ):
            raise ArchiveSchemaError("invalid nonfinite/negative price or quantity")
        if price in prices:
            raise ArchiveSchemaError("duplicate price within one level array")
        prices.add(price)
        result.append((price, quantity))
    return tuple(result)


def iter_okx_archive(
    path: Path,
    manifest: ArchiveManifest,
    *,
    max_uncompressed_bytes: int = 16 * 1024**3,
    max_line_bytes: int = 4 * 1024**2,
    max_records: int = 20_000_000,
) -> Iterator[HistoryRecord]:
    """Never extract files. Validate hash, gzip EOF, schema, opening anchor and time order.

    Consumers must exhaust the iterator to complete the audit. Partial iteration is a
    preview and cannot certify the tail. Sequence fields remain None: file order is not
    a matching-engine sequence. Decimal levels preserve the original numeric content;
    canonical float levels are provided only as the existing Crocodile compatibility view.
    """
    path = Path(path)
    if (
        manifest.venue != "okx"
        or manifest.format != "tar-ndjson"
        or manifest.depth not in {400, 5000}
    ):
        raise ArchiveSchemaError(
            "unsupported archive schema; legacy CSV requires independent audit"
        )
    if max_uncompressed_bytes <= 0 or max_line_bytes <= 0 or max_records <= 0:
        raise ValueError("archive parser budgets must be positive")
    if path.stat().st_size != manifest.bytes or sha256_file(path) != manifest.sha256:
        raise ArchiveSchemaError("archive size/hash differs from verified manifest")
    depth = manifest.depth
    count = 0
    previous_ts: int | None = None
    total_members = 0
    try:
        with gzip.open(path, "rb") as gz:
            bounded = _BudgetReader(gz, max_uncompressed_bytes)
            with tarfile.open(fileobj=bounded, mode="r|", tarinfo=_SafeTarInfo) as tar:
                for member in tar:
                    name = PurePosixPath(member.name)
                    if (
                        name.is_absolute()
                        or ".." in name.parts
                        or "\\" in member.name
                        or ":" in member.name
                        or not member.isfile()
                        or not member.name.endswith(".data")
                    ):
                        raise ArchiveSchemaError("unsafe or unsupported tar member")
                    total_members += member.size
                    if total_members > max_uncompressed_bytes:
                        raise ArchiveSchemaError("tar member sizes exceed uncompressed byte budget")
                    source = tar.extractfile(member)
                    if source is None:
                        raise ArchiveSchemaError("tar member cannot be streamed")
                    line_number = 0
                    while line := source.readline(max_line_bytes + 1):
                        line_number += 1
                        if len(line) > max_line_bytes:
                            raise ArchiveSchemaError("NDJSON line exceeds parser budget")
                        if not line.strip():
                            raise ArchiveSchemaError("empty NDJSON record")
                        try:
                            row = json.loads(line)
                        except (ValueError, UnicodeError) as exc:
                            raise ArchiveSchemaError("truncated or malformed NDJSON") from exc
                        if not isinstance(row, dict) or set(row) != {
                            "instId",
                            "action",
                            "ts",
                            "asks",
                            "bids",
                        }:
                            raise ArchiveSchemaError("unsupported OKX archive record schema")
                        if row["instId"] != manifest.symbol:
                            raise ArchiveSchemaError("archive symbol differs from manifest")
                        if row["action"] not in {"snapshot", "update"}:
                            raise ArchiveSchemaError("unsupported book action")
                        if count == 0 and row["action"] != "snapshot":
                            raise ArchiveSchemaError("archive requires an opening snapshot")
                        try:
                            if not isinstance(row["ts"], str) or not row["ts"].isdigit():
                                raise ValueError("timestamp must be a millisecond integer string")
                            stamp = int(row["ts"]) * 1_000_000
                        except (ValueError, TypeError) as exc:
                            raise ArchiveSchemaError("invalid source time") from exc
                        if previous_ts is not None and stamp < previous_ts:
                            raise ArchiveSchemaError("archive source time moves backwards")
                        previous_ts = stamp
                        bids, asks = _levels(row["bids"], depth), _levels(row["asks"], depth)
                        common: _BookFields = dict(
                            source="okx",
                            symbol=f"okx:{manifest.symbol}",
                            symbol_raw=manifest.symbol,
                            asset_class=AssetClass.CRYPTO,
                            local_ts=manifest.imported_at_ns,
                            source_ts=stamp,
                            bids=[(float(p), float(q)) for p, q in bids],
                            asks=[(float(p), float(q)) for p, q in asks],
                        )
                        if row["action"] == "snapshot":
                            if len(bids) > depth or len(asks) > depth:
                                raise ArchiveSchemaError("snapshot exceeds advertised depth")
                            record: BookSnapshot | BookDelta = BookSnapshot(
                                **common, depth=depth, sequence_id=None
                            )
                        else:
                            record = BookDelta(**common, seq_id=None, prev_seq_id=None)
                        count += 1
                        if count > max_records:
                            raise ArchiveSchemaError("archive parser budget exhausted")
                        yield HistoryRecord(
                            record, member.name, line_number, manifest.sha256, bids, asks
                        )
            # Consume gzip EOF/footer even if tar stopped at its end blocks.
            while bounded.read(64 * 1024):
                pass
    except (EOFError, OSError, tarfile.TarError) as exc:
        raise ArchiveSchemaError("truncated or corrupt gzip/tar archive") from exc
    if count == 0:
        raise ArchiveSchemaError("empty orderbook archive")


def replay_history(
    records: Iterable[HistoryRecord], *, gap_threshold_ns: int | None = None
) -> HistoryReplay:
    bids: dict[Decimal, Decimal] = {}
    asks: dict[Decimal, Decimal] = {}
    count = 0
    start = previous = 0
    depth = 0
    identity: tuple[str, str] | None = None
    gaps = []
    if gap_threshold_ns is not None and gap_threshold_ns <= 0:
        raise ValueError("gap threshold must be positive")
    for item in records:
        record = item.record
        if identity is None:
            identity = (record.source, record.symbol)
        elif identity != (record.source, record.symbol):
            raise ArchiveSchemaError("replay cannot mix source/symbol books")
        if record.source_ts is None:
            raise ArchiveSchemaError("archive replay requires historical source time")
        if count and record.source_ts < previous:
            raise ArchiveSchemaError("archive replay source time moves backwards")
        if isinstance(record, BookSnapshot):
            bids.clear()
            asks.clear()
            depth = record.depth
        elif count == 0:
            raise ArchiveSchemaError("replay requires an opening snapshot")
        if count == 0:
            start = record.source_ts
        elif gap_threshold_ns is not None and record.source_ts - previous > gap_threshold_ns:
            gaps.append((previous, record.source_ts))
        for levels, state in ((item.decimal_bids, bids), (item.decimal_asks, asks)):
            for price, quantity in levels:
                if quantity == 0:
                    state.pop(price, None)
                else:
                    state[price] = quantity
        # The archive describes only its depth limit; distant retained levels are unknown.
        bids = dict(sorted(bids.items(), reverse=True)[:depth])
        asks = dict(sorted(asks.items())[:depth])
        previous = record.source_ts
        count += 1
    if not count:
        raise ArchiveSchemaError("empty orderbook replay")
    return HistoryReplay(
        tuple(sorted(bids.items(), reverse=True)),
        tuple(sorted(asks.items())),
        count,
        start,
        previous,
        depth,
        "source-sequence-unavailable",
        tuple(gaps),
    )
