import gzip
import hashlib
import io
import json
import tarfile
from decimal import Decimal

import pytest

from crocodile.crypto.history import (
    ArchiveManifest,
    ArchiveSchemaError,
    iter_okx_archive,
    replay_history,
)


def archive(tmp_path, rows, *, member="book.data", truncate=0, pax_headers=None):
    body = b"".join(json.dumps(row).encode() + b"\n" for row in rows)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as tar:
        info = tarfile.TarInfo(member)
        info.pax_headers = pax_headers or {}
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    compressed = gzip.compress(output.getvalue())
    if truncate:
        compressed = compressed[:-truncate]
    path = tmp_path / "archive.tar.gz"
    path.write_bytes(compressed)
    manifest = ArchiveManifest(
        venue="okx",
        product="SPOT",
        symbol="BTC-USDT",
        module=4,
        source_date="2026-10-07",
        filename=path.name,
        source_url="https://static.okx.com/a",
        sha256=hashlib.sha256(compressed).hexdigest(),
        bytes=len(compressed),
        format="tar-ndjson",
        depth=400,
        imported_at_ns=999,
    )
    return path, manifest


def row(action="snapshot", ts="1000", bids=None, asks=None):
    return {
        "instId": "BTC-USDT",
        "action": action,
        "ts": ts,
        "bids": bids if bids is not None else [["100", "2.000000000000000001", "1"]],
        "asks": asks if asks is not None else [["101", "3", "1"]],
    }


def test_source_time_decimal_absolute_delete_and_final_book_oracle(tmp_path):
    path, manifest = archive(
        tmp_path,
        [row(), row("update", "1010", [["100", "0", "0"], ["99", "4", "2"]], [["101", "2", "1"]])],
    )
    records = list(iter_okx_archive(path, manifest))
    assert records[0].decimal_bids[0][1] == Decimal("2.000000000000000001")
    assert records[0].record.local_ts == 999
    assert records[0].record.source_ts == 1_000_000_000
    assert records[0].record.sequence_id is None
    assert records[1].record.seq_id is None
    assert records[0].line == 1 and records[1].line == 2
    state = replay_history(records)
    assert state.bids == ((Decimal("99"), Decimal("4")),)
    assert state.asks == ((Decimal("101"), Decimal("2")),)
    assert state.continuity == "source-sequence-unavailable"
    assert state.source_start_ns == 1_000_000_000


@pytest.mark.parametrize(
    "rows,message",
    [
        ([], "empty"),
        ([row("update")], "snapshot"),
        ([row(), row("update", "999")], "time"),
        ([row(bids=[["100", "-1", "1"]])], "quantity"),
        ([{**row(), "instId": "ETH-USDT"}], "symbol"),
    ],
)
def test_unknown_or_invalid_history_cannot_be_complete_book(tmp_path, rows, message):
    path, manifest = archive(tmp_path, rows)
    with pytest.raises(ArchiveSchemaError, match=message):
        list(iter_okx_archive(path, manifest))


def test_tar_traversal_truncation_and_hash_are_rejected(tmp_path):
    path, manifest = archive(tmp_path, [row()], member="../escape.data")
    with pytest.raises(ArchiveSchemaError, match="member"):
        list(iter_okx_archive(path, manifest))
    path, manifest = archive(tmp_path, [row()], truncate=10)
    with pytest.raises(ArchiveSchemaError, match=r"truncat|gzip|tar"):
        list(iter_okx_archive(path, manifest))
    path, manifest = archive(tmp_path, [row()])
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ArchiveSchemaError, match="hash"):
        list(iter_okx_archive(path, manifest))


def test_time_jump_is_disclosed_as_possible_gap_not_sequence_evidence(tmp_path):
    path, manifest = archive(tmp_path, [row(), row("update", "3000", [], [])])
    state = replay_history(iter_okx_archive(path, manifest), gap_threshold_ns=1_000_000_000)
    assert state.possible_time_gaps == ((1_000_000_000, 3_000_000_000),)
    assert state.continuity == "source-sequence-unavailable"


def test_pax_metadata_is_bounded_before_tarfile_can_allocate_it(tmp_path, monkeypatch):
    path, manifest = archive(tmp_path, [row()], pax_headers={"comment": "x" * 1024**2})
    original = gzip.GzipFile.read
    requested = []

    def observed_read(source, size=-1):
        requested.append(size)
        return original(source, size)

    monkeypatch.setattr(gzip.GzipFile, "read", observed_read)
    with pytest.raises(ArchiveSchemaError, match="byte budget"):
        list(iter_okx_archive(path, manifest, max_uncompressed_bytes=1024))
    assert requested and max(requested) <= 1025


def test_extended_metadata_has_a_separate_resident_memory_bound(tmp_path, monkeypatch):
    path, manifest = archive(tmp_path, [row()], pax_headers={"comment": "x" * 1024**2})
    original = tarfile._Stream.read
    requested = []

    def observed_read(source, size):
        requested.append(size)
        return original(source, size)

    monkeypatch.setattr(tarfile._Stream, "read", observed_read)
    with pytest.raises(ArchiveSchemaError, match="extended metadata"):
        list(iter_okx_archive(path, manifest))
    assert requested and max(requested) <= 512


def test_small_standard_pax_metadata_remains_supported(tmp_path):
    path, manifest = archive(tmp_path, [row()], pax_headers={"comment": "ordinary header"})
    assert len(list(iter_okx_archive(path, manifest))) == 1
