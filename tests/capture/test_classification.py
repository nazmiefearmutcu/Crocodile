import pytest

from crocodile.core.capture.classification import classify_frame
from crocodile.core.capture.schema import RawFrame


@pytest.mark.parametrize("source", ["coinbase", "bybit"])
@pytest.mark.parametrize("kind,msg", [("text", None), ("text", []), ("binary", {})])
def test_json_venues_do_not_capture_unparsed_or_binary_frames(source, kind, msg):
    frame = RawFrame(
        payload=b'{"signature":unfinished',
        frame_kind=kind,
        received_utc_ns=1,
        received_monotonic_ns=1,
        connection_id="c1",
        receive_ordinal=1,
    )
    assert classify_frame(source, frame, msg) == "unknown"
