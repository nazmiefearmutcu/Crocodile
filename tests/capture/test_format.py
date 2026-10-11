import struct
import zlib

import msgspec
import pytest

from crocodile.core.capture import format as fmt
from crocodile.core.capture.schema import JournalRecord, MarketFrame, RawFrame
from crocodile.core.errors import CaptureIntegrityError


def record(ordinal=1, payload=b"\xff\x00"):
    return JournalRecord(
        ordinal=ordinal,
        event=MarketFrame(
            stream_id="s1",
            frame=RawFrame(
                payload=payload,
                frame_kind="binary",
                received_utc_ns=100,
                received_monotonic_ns=7,
                connection_id="c1",
                receive_ordinal=ordinal,
            ),
            observed_utc_ns=101,
            observed_monotonic_ns=8,
        ),
    )


@pytest.mark.parametrize("payload", [b"", b"\xff\x00", b'{"broken', b'{"valid":1}'])
def test_raw_bytes_and_metadata_round_trip(payload):
    rec = record(payload=payload)
    encoded = fmt.encode_record(rec)
    body = msgspec.msgpack.encode(rec)
    assert encoded[:4] == struct.pack("<I", len(body))
    assert encoded[4:-4] == body
    assert encoded[-4:] == struct.pack("<I", zlib.crc32(body))
    assert fmt.decode_record(encoded) == rec


def test_record_length_is_checked_before_allocation():
    with pytest.raises(CaptureIntegrityError, match="length"):
        fmt.decode_record(struct.pack("<I", (16 << 20) + 1))


def test_middle_crc_corruption_fails():
    encoded = bytearray(fmt.encode_record(record()))
    encoded[10] ^= 1
    with pytest.raises(CaptureIntegrityError, match="CRC"):
        fmt.decode_record(bytes(encoded))


def test_unknown_format_version_fails():
    with pytest.raises(CaptureIntegrityError, match="version"):
        fmt.validate_header(b"CRCLRAW\0" + struct.pack("<H", 2))
