from crocodile.core.capture.schema import StreamContext
from crocodile.core.errors import CaptureIntegrityError
from crocodile.crypto.exchanges.public_books import PublicReplayDecoder

from .book import DydxBookGate


class DydxReplayDecoder(PublicReplayDecoder):
    source = "dydx"

    def __init__(self) -> None:
        self.gate = DydxBookGate()

    def bind(self, stream: StreamContext) -> None:
        if stream.endpoint_id != "dydx-v4-unbatched" or stream.market != "perpetual":
            raise CaptureIntegrityError("dYdX replay endpoint mismatch")
        super().bind(stream)
