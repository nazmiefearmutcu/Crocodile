"""Read-only journal validation and ordinal lookup. Never skip corruption."""

from collections.abc import Iterator
from pathlib import Path

from crocodile.core.capture.format import FILE_HEADER, decode_record, record_length, validate_header
from crocodile.core.capture.manifest import SegmentInfo, checksum, load_manifest, segment_path
from crocodile.core.capture.schema import CaptureContext, JournalRecord, RecoveryReport
from crocodile.core.errors import CaptureIntegrityError


class CaptureReader:
    def __init__(self, root: Path, *, recover_active_tail: bool = False):
        self.root = Path(root)
        self.manifest = load_manifest(self.root)
        self.context: CaptureContext = self.manifest.context
        self._recover = recover_active_tail
        self._index: dict[int, tuple[Path, int]] = {}
        self._indexed = False
        self._report = RecoveryReport()

    def records(self) -> Iterator[JournalRecord]:
        self._index = {}
        self._indexed = False
        expected = 1
        discarded = 0
        segments: list[tuple[str, SegmentInfo | None]] = [(s.name, s) for s in self.manifest.segments]
        if self.manifest.active_name:
            segments.append((self.manifest.active_name, None))
        for name, info in segments:
            path = segment_path(self.root, name)
            try:
                if info is not None:
                    if path.stat().st_size != info.size_bytes or checksum(path) != info.sha256:
                        raise CaptureIntegrityError("Sealed segment manifest mismatch")
                first = expected
                with path.open("rb") as file:
                    header = file.read(len(FILE_HEADER))
                    validate_header(header)
                    while True:
                        offset = file.tell()
                        prefix = file.read(4)
                        if not prefix:
                            break
                        if len(prefix) < 4:
                            if info is None and self._recover:
                                discarded += len(prefix)
                                break
                            raise CaptureIntegrityError("Incomplete record length")
                        length = record_length(prefix)
                        remainder = file.read(length + 4)
                        if len(remainder) != length + 4:
                            if info is None and self._recover:
                                discarded += len(prefix) + len(remainder)
                                break
                            raise CaptureIntegrityError("Incomplete record tail")
                        record = decode_record(prefix + remainder)
                        if record.ordinal != expected:
                            raise CaptureIntegrityError("Non-contiguous journal ordinal")
                        self._index[expected] = (path, offset)
                        expected += 1
                        yield record
                if info is not None:
                    actual_first = first if first < expected else 0
                    actual_last = expected - 1 if first < expected else 0
                    if (actual_first, actual_last) != (info.first_ordinal, info.last_ordinal):
                        raise CaptureIntegrityError("Segment ordinal bounds mismatch")
            except OSError as exc:
                raise CaptureIntegrityError("Cannot read segment") from exc
        if self.manifest.fsynced_ordinal > expected - 1:
            raise CaptureIntegrityError("Durable watermark exceeds available records")
        self._report = RecoveryReport(recovered_records=expected - 1,
                                      discarded_tail_bytes=discarded)
        self._indexed = True

    def read_record(self, ordinal: int) -> JournalRecord:
        if not self._indexed:
            for _ in self.records():
                pass
        location = self._index.get(ordinal)
        if location is None:
            raise CaptureIntegrityError("Missing journal ordinal")
        path, offset = location
        try:
            with path.open("rb") as file:
                file.seek(offset)
                prefix = file.read(4)
                length = record_length(prefix)
                record = decode_record(prefix + file.read(length + 4))
        except OSError as exc:
            raise CaptureIntegrityError("Cannot resolve journal ordinal") from exc
        if record.ordinal != ordinal:
            raise CaptureIntegrityError("Journal index mismatch")
        return record

    def recovery_report(self) -> RecoveryReport:
        return self._report
