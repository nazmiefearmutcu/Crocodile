"""Separate replay identities for Exchange and Advanced public protocols."""

from __future__ import annotations

from crocodile.core.capture.schema import StreamContext
from crocodile.core.errors import CaptureIntegrityError
from crocodile.crypto.exchanges.public_books import BookResult, Levels, PublicReplayDecoder
from crocodile.crypto.instruments.registry import InstrumentRegistry

from .advanced import CoinbaseAdvancedGate
from .normalize import normalize_message


class CoinbaseExchangeGate(Levels):
    def apply(self, msg: object, local_ts: int, registry: InstrumentRegistry) -> BookResult:
        if not isinstance(msg, dict):
            raise CaptureIntegrityError("Invalid Coinbase Exchange public message")
        kind = msg.get("type")
        symbol = msg.get("product_id")
        if kind == "l2update" and symbol not in self.ready:
            return BookResult("drop")
        records = tuple(normalize_message(msg, local_ts=local_ts, registry=registry))
        if kind == "snapshot":
            if not isinstance(symbol, str):
                raise CaptureIntegrityError("Coinbase snapshot symbol missing")
            self.ready.add(symbol)
            return BookResult("snapshot", records)
        return BookResult("apply" if records else "drop", records)


class CoinbaseReplayDecoder(PublicReplayDecoder):
    source = "coinbase"

    def __init__(self) -> None:
        self.gate = CoinbaseExchangeGate()

    def bind(self, stream: StreamContext) -> None:
        if (
            stream.endpoint_id not in {"coinbase-exchange", "coinbase-advanced"}
            or stream.market != "spot"
        ):
            raise CaptureIntegrityError("Coinbase replay endpoint mismatch")
        self.gate = (
            CoinbaseAdvancedGate()
            if stream.endpoint_id == "coinbase-advanced"
            else CoinbaseExchangeGate()
        )
        super().bind(stream)
