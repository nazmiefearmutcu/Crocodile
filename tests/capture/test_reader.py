import hashlib

import msgspec
import pytest

from crocodile.core.capture.format import FILE_HEADER, encode_record
from crocodile.core.capture.reader import CaptureReader
from crocodile.core.capture.schema import CaptureContext, JournalRecord, RegistryCheckpoint
from crocodile.core.errors import CaptureIntegrityError


def capture(root, *, partial=False, payload=None):
    root.mkdir(exist_ok=True)
    records = [JournalRecord(ordinal=i, event=RegistryCheckpoint(instruments=())) for i in (1, 2)]
    data = FILE_HEADER + b"".join(encode_record(r) for r in records)
    if payload is not None:
        data = payload
    name = "00000001.partial" if partial else "00000001.raw"
    (root / name).write_bytes(data)
    segment = {
        "name": name,
        "size_bytes": len(data),
        "first_ordinal": 1,
        "last_ordinal": 2,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    manifest = {
        "version": 1,
        "context": msgspec.to_builtins(CaptureContext(capture_id="test", streams=())),
        "segments": [] if partial else [segment],
        "active_name": name if partial else None,
        "fsynced_ordinal": 0 if partial else 2,
    }
    (root / "manifest.json").write_bytes(msgspec.json.encode(manifest))
    return records, data


def test_manifest_segment_mismatch_fails(tmp_path):
    capture(tmp_path)
    (tmp_path / "00000001.raw").write_bytes(b"wrong")
    with pytest.raises(CaptureIntegrityError):
        list(CaptureReader(tmp_path).records())


def test_preceding_origin_lookup_does_not_restart_active_stream(tmp_path, monkeypatch):
    expected, _ = capture(tmp_path)
    reader = CaptureReader(tmp_path)
    iterator = reader.records()
    assert next(iterator) == expected[0]

    def forbidden():
        raise AssertionError("Origin lookup restarted journal")

    monkeypatch.setattr(reader, "records", forbidden)
    assert reader.read_record(1) == expected[0]
    assert next(iterator) == expected[1]


def test_every_tail_byte_truncation_recovers_complete_prefix(tmp_path):
    records, data = capture(tmp_path, partial=True)
    first_end = len(FILE_HEADER) + len(encode_record(records[0]))
    for end in range(len(FILE_HEADER), len(data) + 1):
        (tmp_path / "00000001.partial").write_bytes(data[:end])
        reader = CaptureReader(tmp_path, recover_active_tail=True)
        got = list(reader.records())
        assert got == records[: (0 if end < first_end else 1 if end < len(data) else 2)]
        assert reader.recovery_report().discarded_tail_bytes == end - (
            len(FILE_HEADER) if end < first_end else first_end if end < len(data) else len(data)
        )


def test_sealed_truncation_fails(tmp_path):
    _, data = capture(tmp_path)
    (tmp_path / "00000001.raw").write_bytes(data[:-1])
    with pytest.raises(CaptureIntegrityError):
        list(CaptureReader(tmp_path, recover_active_tail=True).records())


def test_partial_full_record_crc_error_is_not_recovery(tmp_path):
    _, data = capture(tmp_path, partial=True)
    corrupted = bytearray(data)
    corrupted[-1] ^= 1
    (tmp_path / "00000001.partial").write_bytes(corrupted)
    with pytest.raises(CaptureIntegrityError, match="CRC"):
        list(CaptureReader(tmp_path, recover_active_tail=True).records())


def test_reader_validates_ordinal_references_and_safe_paths(tmp_path):
    records, _ = capture(tmp_path)
    reader = CaptureReader(tmp_path)
    assert reader.read_record(2) == records[1]
    with pytest.raises(CaptureIntegrityError):
        reader.read_record(3)
    data = msgspec.json.decode((tmp_path / "manifest.json").read_bytes())
    data["segments"][0]["name"] = "../elsewhere.raw"
    (tmp_path / "manifest.json").write_bytes(msgspec.json.encode(data))
    with pytest.raises(CaptureIntegrityError):
        list(CaptureReader(tmp_path).records())
