"""Decimal book primitives shared by public protocols and their offline decoders.

Canonical book rows intentionally carry no sequence when source counters span
other channels. The original frame and protocol gate retain those counters.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol, cast

from crocodile.core.capture.classification import contains_credentials
from crocodile.core.capture.schema import (
    ApplicationDecision,
    CaptureEvent,
    ControlMetadata,
    JournalRecord,
    MarketFrame,
    RawFrame,
    StreamContext,
)
from crocodile.core.connector import Connector
from crocodile.core.errors import (
    CaptureIntegrityError,
    FatalConnectorError,
    TransientConnectorError,
)
from crocodile.core.schema.enums import AssetClass
from crocodile.core.schema.provenance import (
    ConfidenceInputError,
    Provenance,
    provenance_fields,
    register_basis,
)
from crocodile.core.schema.records import BookDelta, BookSnapshot, Record
from crocodile.crypto.instruments.registry import InstrumentRegistry


class PublicBookError(FatalConnectorError):
    """Malformed or contradictory public protocol; never fabricate a transition."""


@register_basis("visible_l3_sum", level=Provenance.DERIVED, inputs=["raw_l3"])
def _visible_l3_sum(inputs: Mapping[str, Any]) -> float:
    """One for a synchronized visible subscription, zero after loss of continuity.

    This boolean is observed by the protocol gate; it does not grade hidden or
    unsubscribed liquidity. Missing or non-boolean synchronization is refused.
    """
    synchronized = inputs["synchronized"]
    if not isinstance(synchronized, bool):
        raise ConfidenceInputError("visible_l3_sum requires boolean synchronized")
    return float(synchronized)


def decimal(value: object, *, positive: bool = False) -> Decimal:
    try:
        if isinstance(value, bool):
            raise InvalidOperation
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PublicBookError("Invalid public price or quantity") from None
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise PublicBookError("Invalid public price or quantity")
    return result


def integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PublicBookError("Invalid public counter")
    return value


def iso_ns(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PublicBookError("Invalid public timestamp")
    match = re.fullmatch(r"(.+?)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)", value)
    if not match:
        raise PublicBookError("Invalid public timestamp")
    try:
        dt = datetime.fromisoformat(match[1] + match[3].replace("Z", "+00:00"))
        elapsed = dt - datetime(1970, 1, 1, tzinfo=UTC)
        return (elapsed.days * 86400 + elapsed.seconds) * 1_000_000_000 + int(
            (match[2] or "").ljust(9, "0")
        )
    except ValueError:
        raise PublicBookError("Invalid public timestamp") from None


@dataclass(frozen=True)
class BookResult:
    action: Literal["apply", "drop", "snapshot", "gap"]
    records: tuple[Record, ...] = ()


class PublicGate(Protocol):
    ready: set[str]

    def reset(self) -> None: ...
    def apply(self, msg: object, local_ts: int, registry: InstrumentRegistry) -> BookResult: ...


class MappedPublicGate(PublicGate, Protocol):
    def map_channel(self, data: dict[str, str | int]) -> None: ...


class Levels:
    def __init__(self) -> None:
        self.bids: dict[str, dict[Decimal, Decimal]] = {}
        self.asks: dict[str, dict[Decimal, Decimal]] = {}
        self.ready: set[str] = set()
        self.invalid = False

    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.ready.clear()
        self.invalid = False

    def invalidate(self) -> BookResult:
        self.ready.clear()
        self.invalid = True
        return BookResult("gap")

    def emit(
        self,
        source: str,
        symbol: str,
        bids: dict[Decimal, Decimal],
        asks: dict[Decimal, Decimal],
        *,
        snapshot: bool,
        local_ts: int,
        source_ts: int | None,
        registry: InstrumentRegistry,
        derived: bool = False,
        prune: int | None = None,
    ) -> BookResult:
        previous_bids = self.bids.get(symbol, {})
        previous_asks = self.asks.get(symbol, {})
        if prune is not None:
            bids = dict(sorted(bids.items(), reverse=True)[:prune])
            asks = dict(sorted(asks.items())[:prune])
        self.bids[symbol], self.asks[symbol] = bids, asks
        self.ready.add(symbol)

        def rows(
            current: dict[Decimal, Decimal], previous: dict[Decimal, Decimal], reverse: bool
        ) -> list[tuple[float, float]]:
            keys = (
                set(current)
                if snapshot
                else {
                    price
                    for price in set(current) | set(previous)
                    if current.get(price, Decimal(0)) != previous.get(price, Decimal(0))
                }
            )
            return [
                (float(p), float(current.get(p, Decimal(0)))) for p in sorted(keys, reverse=reverse)
            ]

        bid_rows = rows(bids, previous_bids, True)
        ask_rows = rows(asks, previous_asks, False)
        if not snapshot and not bid_rows and not ask_rows:
            return BookResult("drop")
        instrument = registry.get_raw(source, symbol)
        fields: dict[str, Any] = dict(
            source=source,
            symbol=instrument.canonical if instrument else f"{source}:{symbol}",
            symbol_raw=symbol,
            local_ts=local_ts,
            source_ts=source_ts,
            asset_class=AssetClass.CRYPTO,
            bids=bid_rows,
            asks=ask_rows,
        )
        if derived:
            fields.update(provenance_fields("visible_l3_sum", {"synchronized": True})._asdict())
        record = (
            BookSnapshot(depth=len(bid_rows) + len(ask_rows), **fields)
            if snapshot
            else BookDelta(**fields)
        )
        return BookResult("snapshot" if snapshot else "apply", (record,))

    def level_update(
        self,
        symbol: str,
        bids: Iterable[tuple[object, object]],
        asks: Iterable[tuple[object, object]],
        *,
        snapshot: bool,
    ) -> tuple[dict[Decimal, Decimal], dict[Decimal, Decimal]]:
        sides: list[dict[Decimal, Decimal]] = (
            [{}, {}]
            if snapshot
            else [dict(self.bids.get(symbol, {})), dict(self.asks.get(symbol, {}))]
        )
        for target, rows in zip(sides, (bids, asks), strict=True):
            for price, qty in rows:
                p, q = decimal(price, positive=True), decimal(qty)
                if q:
                    target[p] = q
                else:
                    target.pop(p, None)
        return sides[0], sides[1]


class GatedConnector(Connector):
    required_feed = True
    gate: PublicGate
    market: str
    capture_endpoint_id: str
    _counts: dict[str, int]

    def _decode_frame(self, frame: RawFrame) -> object:
        try:
            return json.loads(frame.payload, parse_float=Decimal)
        except (ValueError, UnicodeError):
            raise PublicBookError("Malformed public market frame") from None

    def normalize(self, msg: object, local_ts: int) -> Iterable[Record]:
        return self._apply_public(msg, local_ts).records

    def _apply_public(self, msg: object, local_ts: int) -> BookResult:
        try:
            result = self.gate.apply(msg, local_ts, self.registry)
        except (KeyError, TypeError, AttributeError, IndexError, ValueError):
            raise PublicBookError("Malformed public protocol fields") from None
        if any(r.symbol_raw not in self.symbols for r in result.records):
            raise PublicBookError("Public record symbol is outside the subscription")
        return result

    async def _prepare_connection(self) -> None:
        self.gate.reset()

    async def _handle_message(self, msg: object, local_ts: int) -> None:
        result = self._apply_public(msg, local_ts)
        self._count(result)
        await self._emit_records(list(result.records), action=result.action, reason="public_book")
        if result.action == "gap":
            raise TransientConnectorError(
                "Public book continuity failed; reconnect snapshot required"
            )

    def _count(self, result: BookResult, *, raw_frame: bool = True) -> None:
        if not hasattr(self, "_counts"):
            self._counts = {
                "raw_market_frames": 0,
                "changed_book_revisions": 0,
                "snapshots": 0,
                "gaps": 0,
                "unchanged_or_control_frames": 0,
            }
        self._counts["raw_market_frames"] += int(raw_frame)
        changed = sum(isinstance(r, BookDelta) for r in result.records)
        snapshots = sum(isinstance(r, BookSnapshot) for r in result.records)
        self._counts["changed_book_revisions"] += changed
        self._counts["snapshots"] += snapshots
        self._counts["gaps"] += int(result.action == "gap")
        self._counts["unchanged_or_control_frames"] += int(
            raw_frame and not changed and not snapshots and result.action != "gap"
        )

    def telemetry(self) -> dict[str, object]:
        counts = dict(getattr(self, "_counts", {}))
        return {
            **counts,
            "endpoint_id": self.capture_endpoint_id,
            "market": self.market,
            "quantity_unit": getattr(self, "quantity_unit", "native"),
            "coverage": getattr(self, "coverage", {}),
        }

    async def _drain_market(self) -> None:
        if not set(self.symbols) <= self.gate.ready:
            raise TransientConnectorError("Public feed ended before every book snapshot arrived")


class PublicReplayDecoder:
    version = 1
    source = ""
    gate: PublicGate

    def bind(self, stream: StreamContext) -> None:
        if stream.source != self.source:
            raise CaptureIntegrityError("Public replay source mismatch")
        self.stream = stream
        self.gate.reset()

    def observe(self, event: CaptureEvent) -> None:
        if isinstance(event, ControlMetadata):
            if event.kind == "connected":
                self.gate.reset()
            elif event.public_data is not None and self.source == "bitfinex":
                if event.public_data.get("symbol") not in self.stream.symbols:
                    raise CaptureIntegrityError("Public mapping symbol is outside the capture")
                cast(MappedPublicGate, self.gate).map_channel(event.public_data)
        elif isinstance(event, MarketFrame):
            try:
                message = json.loads(event.frame.payload, parse_float=Decimal)
            except (ValueError, UnicodeError):
                raise CaptureIntegrityError("Malformed public replay frame") from None
            if contains_credentials(message):
                raise CaptureIntegrityError("Credentials in a public replay frame")

    def apply(
        self, decision: ApplicationDecision, origin: JournalRecord, registry: InstrumentRegistry
    ) -> Iterable[Record]:
        if not isinstance(origin.event, MarketFrame):
            raise CaptureIntegrityError("Public replay decision needs a market origin")
        result = self.gate.apply(
            json.loads(origin.event.frame.payload, parse_float=Decimal),
            origin.event.observed_utc_ns,
            registry,
        )
        if any(r.symbol_raw not in self.stream.symbols for r in result.records):
            raise CaptureIntegrityError("Public replay symbol is outside the capture")
        if (
            decision.reason != "public_book"
            or decision.action != result.action
            or decision.record_indices != tuple(range(len(result.records)))
        ):
            raise CaptureIntegrityError("Public replay application decision mismatch")
        return result.records
