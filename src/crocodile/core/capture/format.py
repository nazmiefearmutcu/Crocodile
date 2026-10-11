"""Bounded, checksummed binary record framing."""

import struct
import zlib

import msgspec

from crocodile.core.capture.schema import JournalRecord
from crocodile.core.errors import CaptureIntegrityError

MAX_RECORD_BYTES = 16 << 20
FILE_HEADER = b"CRCLRAW\0" + struct.pack("<H", 1)
_decoder = msgspec.msgpack.Decoder(JournalRecord)


def validate_header(data: bytes) -> None:
    if len(data) != len(FILE_HEADER) or data[:8] != FILE_HEADER[:8]:
        raise CaptureIntegrityError("Invalid raw capture header")
    if data[8:] != FILE_HEADER[8:]:
        raise CaptureIntegrityError("Unsupported raw capture version")


def record_length(prefix: bytes, max_record_bytes: int = MAX_RECORD_BYTES) -> int:
    if len(prefix) != 4:
        raise CaptureIntegrityError("Incomplete record length")
    length, = struct.unpack("<I", prefix)
    if length == 0 or length > max_record_bytes:
        raise CaptureIntegrityError("Invalid record length")
    return int(length)


def encode_record(record: JournalRecord, *, max_record_bytes: int = MAX_RECORD_BYTES) -> bytes:
    body = msgspec.msgpack.encode(record)
    if not 0 < len(body) <= max_record_bytes:
        raise CaptureIntegrityError("Invalid record length")
    return struct.pack("<I", len(body)) + body + struct.pack("<I", zlib.crc32(body))


def decode_record(data: bytes, *, max_record_bytes: int = MAX_RECORD_BYTES) -> JournalRecord:
    length = record_length(data[:4], max_record_bytes)
    if len(data) != length + 8:
        raise CaptureIntegrityError("Incomplete or oversized record framing")
    body = data[4:-4]
    if struct.unpack("<I", data[-4:])[0] != zlib.crc32(body):
        raise CaptureIntegrityError("Record CRC mismatch")
    try:
        result = _decoder.decode(body)
    except msgspec.DecodeError as exc:
        raise CaptureIntegrityError("Invalid record schema") from exc
    if result.ordinal < 1:
        raise CaptureIntegrityError("Invalid record ordinal")
    return result
