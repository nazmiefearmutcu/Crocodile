"""Pure L2 continuity gate shared by live collection and offline replay."""

from __future__ import annotations

import math
from typing import Any, Literal

from crocodile.core.errors import FatalConnectorError

BookAction = Literal["snapshot", "apply", "drop", "gap"]


class DeribitProtocolError(FatalConnectorError):
    pass


def validate_levels(rows: object) -> list[tuple[float, float]]:
    if not isinstance(rows, list):
        raise DeribitProtocolError("Deribit book levels must be arrays")
    levels: list[tuple[float, float]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 3 or row[0] not in ("new", "change", "delete"):
            raise DeribitProtocolError("Deribit book action is unsupported")
        _, price, amount = row
        if (
            isinstance(price, bool)
            or isinstance(amount, bool)
            or not isinstance(price, (int, float))
            or not isinstance(amount, (int, float))
            or not math.isfinite(price)
            or not math.isfinite(amount)
            or price <= 0
            or amount < 0
        ):
            raise DeribitProtocolError("Deribit book level values are invalid")
        levels.append((float(price), 0.0 if row[0] == "delete" else float(amount)))
    return levels


def validate_book(data: dict[str, Any]) -> None:
    change_id = data.get("change_id")
    if (
        not isinstance(data.get("instrument_name"), str)
        or not data["instrument_name"]
        or data.get("type") not in ("snapshot", "change")
        or not isinstance(change_id, int)
        or isinstance(change_id, bool)
        or change_id < 0
    ):
        raise DeribitProtocolError("Deribit book identity/type/sequence is invalid")
    if data["type"] == "change":
        prev = data.get("prev_change_id")
        if not isinstance(prev, int) or isinstance(prev, bool) or prev < 0:
            raise DeribitProtocolError("Deribit previous change ID is invalid")
    timestamp = data.get("timestamp")
    if (
        not isinstance(timestamp, int)
        or isinstance(timestamp, bool)
        or not 0 <= timestamp <= (2**63 - 1) // 1_000_000
    ):
        raise DeribitProtocolError("Deribit book timestamp is invalid")
    validate_levels(data.get("bids", []))
    validate_levels(data.get("asks", []))


class DeribitBookGate:
    def __init__(self) -> None:
        self._last: dict[str, int] = {}
        self.bids: dict[str, dict[float, float]] = {}
        self.asks: dict[str, dict[float, float]] = {}

    def reset(self, symbol_raw: str | None = None) -> None:
        if symbol_raw is None:
            self._last.clear()
            self.bids.clear()
            self.asks.clear()
        else:
            self._last.pop(symbol_raw, None)
            self.bids.pop(symbol_raw, None)
            self.asks.pop(symbol_raw, None)

    def last_change_id(self, symbol_raw: str) -> int | None:
        return self._last.get(symbol_raw)

    def evaluate(self, data: dict[str, Any]) -> BookAction:
        validate_book(data)
        symbol: str = data["instrument_name"]
        change_id: int = data["change_id"]
        last = self._last.get(symbol)
        if data["type"] == "snapshot":
            self.reset(symbol)
            action: BookAction = "snapshot"
        elif last is None or change_id <= last:
            return "drop"
        elif data["prev_change_id"] != last:
            self.reset(symbol)
            return "gap"
        else:
            action = "apply"
        for name, books in (("bids", self.bids), ("asks", self.asks)):
            book = books.setdefault(symbol, {})
            for price, amount in validate_levels(data.get(name, [])):
                if amount == 0:
                    book.pop(price, None)
                else:
                    book[price] = amount
        self._last[symbol] = change_id
        return action
