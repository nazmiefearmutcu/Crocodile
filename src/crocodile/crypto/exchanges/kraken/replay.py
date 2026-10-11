from crocodile.core.capture.schema import StreamContext
from crocodile.core.errors import CaptureIntegrityError
from crocodile.crypto.exchanges.public_books import PublicReplayDecoder

from .book import KrakenBookGate


class KrakenReplayDecoder(PublicReplayDecoder):
    source = "kraken"

    def __init__(self) -> None:
        self.gate = KrakenBookGate()

    def bind(self, stream: StreamContext) -> None:
        try:
            source, mode, depth = stream.endpoint_id.split("-")
            if source != "kraken" or mode not in {"book", "level3"} or stream.market != "spot":
                raise ValueError
            self.gate = KrakenBookGate(mode=mode, depth=int(depth))
        except ValueError:
            raise CaptureIntegrityError("Kraken replay endpoint mismatch") from None
        super().bind(stream)
