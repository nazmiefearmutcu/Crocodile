from crocodile.core.capture.schema import StreamContext
from crocodile.core.errors import CaptureIntegrityError
from crocodile.crypto.exchanges.public_books import PublicReplayDecoder

from .book import BitfinexBookGate


class BitfinexReplayDecoder(PublicReplayDecoder):
    source = "bitfinex"

    def __init__(self) -> None:
        self.gate = BitfinexBookGate()

    def bind(self, stream: StreamContext) -> None:
        if stream.endpoint_id != "bitfinex-r0-f0-250" or stream.market != "spot":
            raise CaptureIntegrityError("Bitfinex raw replay endpoint mismatch")
        super().bind(stream)
