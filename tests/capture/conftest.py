import pytest

from crocodile.core.capture.schema import CaptureContext, StreamContext


@pytest.fixture
def capture_context():
    return CaptureContext(capture_id="fixture", streams=(StreamContext(
        stream_id="s1", source="binance-spot", market="spot", symbols=("BTCUSDT",),
        endpoint_id="binance-spot-ws", decoder_id="binance-json", decoder_version=1),))
