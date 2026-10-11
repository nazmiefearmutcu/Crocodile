"""Bybit fast snapshots and independently anchored full-depth state."""

from __future__ import annotations

from typing import Any

from crocodile.crypto.exchanges.public_books import BookResult, Levels, PublicBookError, integer
from crocodile.crypto.instruments.registry import InstrumentRegistry

from .normalize import normalize_message


class BybitBookGate(Levels):
    def __init__(self, mode: str = "fast", depth: int = 50) -> None:
        if mode not in {"fast", "full"} or depth not in {1, 25, 50, 100, 200, 1000}:
            raise PublicBookError("Bybit mode/depth is not supported")
        super().__init__()
        self.mode, self.depth = mode, depth
        self.last: dict[str, tuple[int, object]] = {}
        self.last_seq: dict[str, int] = {}

    def reset(self) -> None:
        super().reset()
        self.last.clear()
        self.last_seq.clear()

    def anchor(
        self, data: dict[str, Any], local_ts: int, registry: InstrumentRegistry
    ) -> BookResult:
        symbol = data.get("s")
        if not isinstance(symbol, str):
            raise PublicBookError("Bybit full snapshot symbol missing")
        u = integer(data.get("u"))
        self.last_seq[symbol] = integer(data.get("seq"))
        bids, asks = self.level_update(symbol, data.get("b", []), data.get("a", []), snapshot=True)
        self.last[symbol] = u, data
        self.invalid = False
        return self.emit(
            "bybit",
            symbol,
            bids,
            asks,
            snapshot=True,
            local_ts=local_ts,
            source_ts=integer(data.get("cts", data.get("ts"))) * 1_000_000,
            registry=registry,
        )

    def apply(self, msg: object, local_ts: int, registry: InstrumentRegistry) -> BookResult:
        if not isinstance(msg, dict):
            raise PublicBookError("Bybit malformed public envelope")
        topic = msg.get("topic", "")
        if not isinstance(topic, str):
            raise PublicBookError("Bybit invalid public topic")
        if not topic.startswith("orderbook."):
            records = tuple(normalize_message(msg, local_ts=local_ts, registry=registry))
            return BookResult("apply" if records else "drop", records)
        if self.invalid:
            return BookResult("drop")
        expected = "full" if self.mode == "full" else str(self.depth)
        parts = topic.split(".", 2)
        data = msg.get("data")
        if (
            len(parts) != 3
            or parts[1] != expected
            or not isinstance(data, dict)
            or data.get("s") != parts[2]
        ):
            raise PublicBookError("Bybit book topic/context mismatch")
        symbol = data["s"]
        kind = msg.get("type")
        if kind not in {"snapshot", "delta"}:
            raise PublicBookError("Bybit book type invalid")
        snapshot = kind == "snapshot"
        u = integer(data.get("u"))
        seq = integer(data.get("seq"))
        if self.mode == "full":
            if snapshot:
                raise PublicBookError("Bybit full stream must contain deltas only")
            if symbol not in self.ready:
                return BookResult("drop")
            last = self.last[symbol][0]
            if seq < self.last_seq[symbol]:
                return BookResult("drop")
            if u == 1 or u > last + 1:
                return self.invalidate()
            if u <= last:
                return BookResult("drop")
        else:
            if u == 1 and not snapshot:
                return self.invalidate()
            if not snapshot and symbol not in self.ready:
                return BookResult("drop")
            previous = self.last.get(symbol)
            if previous is not None and not snapshot:
                if u < previous[0]:
                    return BookResult("drop")
                if u == previous[0]:
                    return BookResult("drop") if msg == previous[1] else self.invalidate()
            # Fast-channel u is incremental, not documented as consecutive.
        bids, asks = self.level_update(
            symbol, data.get("b", []), data.get("a", []), snapshot=snapshot
        )
        self.last[symbol] = u, msg
        self.last_seq[symbol] = seq
        return self.emit(
            "bybit",
            symbol,
            bids,
            asks,
            snapshot=snapshot,
            local_ts=local_ts,
            source_ts=integer(msg.get("cts", msg.get("ts"))) * 1_000_000,
            registry=registry,
            prune=None if self.mode == "full" else self.depth,
        )
