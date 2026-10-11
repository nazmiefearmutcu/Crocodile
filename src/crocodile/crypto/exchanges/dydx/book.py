"""Indexer subscription counters are retained, never matching-engine IDs."""

from __future__ import annotations

from crocodile.crypto.exchanges.public_books import BookResult, Levels, PublicBookError, integer
from crocodile.crypto.instruments.registry import InstrumentRegistry


class DydxBookGate(Levels):
    def __init__(self) -> None:
        super().__init__()
        self.counters: dict[tuple[str, str, str], tuple[int, object]] = {}
        self.connections: dict[str, str] = {}

    def reset(self) -> None:
        super().reset()
        self.counters.clear()
        self.connections.clear()

    def apply(self, msg: object, local_ts: int, registry: InstrumentRegistry) -> BookResult:
        if self.invalid:
            return BookResult("drop")
        if not isinstance(msg, dict) or msg.get("channel") != "v4_orderbook":
            raise PublicBookError("dYdX unexpected channel")
        kind = msg.get("type")
        symbol = msg.get("id")
        connection = msg.get("connection_id")
        if (
            kind not in {"subscribed", "channel_data"}
            or not isinstance(symbol, str)
            or not isinstance(connection, str)
        ):
            raise PublicBookError("dYdX invalid unbatched envelope")
        key = connection, msg["channel"], symbol
        if (
            kind == "channel_data"
            and symbol in self.ready
            and self.connections[symbol] != connection
        ):
            return self.invalidate()
        if kind == "subscribed":
            self.connections[symbol] = connection
        mid = integer(msg.get("message_id"))
        previous = self.counters.get(key)
        if previous is not None:
            if mid == previous[0]:
                return BookResult("drop") if msg == previous[1] else self.invalidate()
            if mid != previous[0] + 1:
                return self.invalidate()
        self.counters[key] = mid, msg
        snapshot = kind == "subscribed"
        if not snapshot and symbol not in self.ready:
            return BookResult("drop")
        data = msg.get("contents")
        if not isinstance(data, dict):
            raise PublicBookError("dYdX missing orderbook contents")

        def rows(side: str) -> list[tuple[object, object]]:
            value = data.get(side, [])
            if not isinstance(value, list):
                raise PublicBookError("dYdX invalid price level list")
            result = []
            for row in value:
                if isinstance(row, dict):
                    result.append((row.get("price"), row.get("size")))
                elif isinstance(row, list) and len(row) == 2:
                    # Current indexer delta encoding uses [price, size] tuples.
                    result.append((row[0], row[1]))
                else:
                    raise PublicBookError("dYdX invalid price level")
            return result

        bids, asks = self.level_update(symbol, rows("bids"), rows("asks"), snapshot=snapshot)
        return self.emit(
            "dydx",
            symbol,
            bids,
            asks,
            snapshot=snapshot,
            local_ts=local_ts,
            source_ts=None,
            registry=registry,
        )
