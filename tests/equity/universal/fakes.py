"""Shared test doubles for the universal orchestrator (no ``test_`` prefix: not collected).

Every record, observation and snapshot is built in code; nothing here opens a socket. Task 15
imports these names, so they stay stable: ``FIXED_NOW``, ``make_observation``, ``make_snapshot``,
``seed_master``, ``daily_bar``, ``dividend``, ``instrument``, ``FakeHistorySource``,
``FakeUniverseSource``, ``FakeSnapshotSource``, ``RecordingSink``, ``make_orchestrator``.
"""

from __future__ import annotations

import asyncio
import datetime
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crocodile.core.markets.master import ListingMaster, ListingObservation
from crocodile.core.markets.symbols import parse_listing_symbol
from crocodile.core.schema.enums import AssetClass, CorpActionType
from crocodile.core.schema.provenance import provenance_fields
from crocodile.core.schema.records import OHLCV, CorporateAction, Instrument, Record
from crocodile.core.sink.base import Sink
from crocodile.equity.universal.base import UniverseSnapshot, daily_bar_ts_ns
from crocodile.equity.universal.orchestrator import (
    Listing,
    SnapshotBatch,
    SourceBindings,
    UniversalOrchestrator,
)
from crocodile.equity.universal.state import NS_PER_DAY, UniversalState

# A Friday after Borsa Istanbul's close plus 45 minutes: the default clock of every test.
FIXED_NOW: datetime.datetime = datetime.datetime(2024, 3, 8, 18, 0, tzinfo=datetime.UTC)
FIXED_NS: int = int(FIXED_NOW.timestamp()) * 1_000_000_000


def _tail() -> dict[str, Any]:
    fields = provenance_fields("native")
    return {
        "prov": fields.prov,
        "prov_basis": fields.prov_basis,
        "prov_confidence": fields.prov_confidence,
        "prov_inputs": fields.prov_inputs,
    }


def make_observation(
    source: str,
    raw: str,
    mic: str,
    local: str,
    *,
    observed_ns: int = FIXED_NS,
    currency: str | None = None,
    price_scale: int | None = None,
    name: str | None = None,
    extra_aliases: Iterable[tuple[str, str]] = (),
) -> ListingObservation:
    return ListingObservation(
        source=source,
        mic=mic,
        local_symbol=local,
        symbol_raw=raw,
        observed_ns=observed_ns,
        currency=currency,
        price_scale=price_scale,
        name=name,
        extra_aliases=tuple(extra_aliases),
    )


def make_snapshot(
    source: str,
    observations: Sequence[ListingObservation],
    records: Sequence[Record] = (),
    *,
    complete: bool = True,
    skipped: Mapping[str, int] | None = None,
    captured_ns: int = FIXED_NS,
) -> UniverseSnapshot:
    seen: dict[str, set[str]] = defaultdict(set)
    for observation in observations:
        seen[observation.mic].add(observation.local_symbol)
    return UniverseSnapshot(
        source=source,
        captured_at_ns=captured_ns,
        observations=tuple(observations),
        records=tuple(records),  # type: ignore[arg-type]
        seen={mic: frozenset(locals_) for mic, locals_ in seen.items()},
        complete=complete,
        skipped=dict(skipped or {}),
    )


def seed_master(master: ListingMaster, observations: Iterable[ListingObservation]) -> None:
    for observation in observations:
        master.upsert(observation)


def daily_bar(
    source: str,
    symbol: str,
    session: datetime.date,
    close: float,
    *,
    currency: str | None = None,
    price_scale: int | None = None,
    captured_ns: int = FIXED_NS,
) -> OHLCV:
    return OHLCV(
        source=source,
        symbol=symbol,
        symbol_raw=symbol,
        local_ts=captured_ns,
        asset_class=AssetClass.EQUITY,
        source_ts=daily_bar_ts_ns(session),
        interval="1d",
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1.0,
        currency=currency,
        price_scale=price_scale,
        **_tail(),
    )


def dividend(
    source: str,
    symbol: str,
    ex_date: datetime.date,
    value: float,
    *,
    currency: str | None = "TRY",
    price_scale: int | None = 0,
    captured_ns: int = FIXED_NS,
) -> CorporateAction:
    return CorporateAction(
        source=source,
        symbol=symbol,
        symbol_raw=symbol,
        local_ts=captured_ns,
        asset_class=AssetClass.EQUITY,
        source_ts=daily_bar_ts_ns(ex_date),
        ex_date=ex_date.isoformat(),
        type=CorpActionType.DIVIDEND_CASH,
        value=value,
        currency=currency,
        price_scale=price_scale,
        **_tail(),
    )


def instrument(
    source: str,
    symbol: str,
    mic: str,
    *,
    name: str | None = None,
    captured_ns: int = FIXED_NS,
) -> Instrument:
    return Instrument(
        source=source,
        symbol=symbol,
        symbol_raw=symbol,
        local_ts=captured_ns,
        asset_class=AssetClass.EQUITY,
        source_ts=captured_ns,
        name=name,
        mic=mic,
        **_tail(),
    )


@dataclass
class FakeHistorySource:
    """A history source (yahoo_chart's role). One call yields bars and dividends for the symbol.

    ``bars`` and ``dividends`` map a canonical symbol to ``(session, close)`` and
    ``(ex_date, value)`` pairs. ``raises`` raises on every call for a symbol. ``gated`` symbols
    wait on ``gate`` before answering. ``calls`` logs ``(symbol, channel, start_ns, end_ns)``.
    """

    name: str = "yahoo_chart"
    mics: frozenset[str] = frozenset({"XIST"})
    channels: frozenset[str] = frozenset({"ohlcv", "corp_action"})
    bars: dict[str, list[tuple[datetime.date, float]]] = field(default_factory=dict)
    dividends: dict[str, list[tuple[datetime.date, float]]] = field(default_factory=dict)
    intraday_bars: dict[str, list[OHLCV]] = field(default_factory=dict)
    currency: dict[str, tuple[str | None, int | None]] = field(default_factory=dict)
    raises: dict[str, BaseException] = field(default_factory=dict)
    gated: set[str] = field(default_factory=set)
    gate: asyncio.Event | None = None
    delay: float = 0.0
    calls: list[tuple[str, str, int, int]] = field(default_factory=list)
    intraday_calls: list[tuple[str, str]] = field(default_factory=list)
    in_flight: int = 0
    max_in_flight: int = 0

    def serves(self, mic: str, channel: str) -> bool:
        return mic in self.mics and channel in self.channels

    def history(
        self, listing: Listing, channel: str, start_ns: int, end_ns: int, *, captured_ns: int
    ) -> AsyncIterator[Record]:
        return self._history(listing, channel, start_ns, end_ns, captured_ns)

    async def _history(
        self,
        listing: Listing,
        channel: str,
        start_ns: int,
        end_ns: int,
        captured_ns: int,
    ) -> AsyncIterator[Record]:
        symbol = listing.symbol
        self.calls.append((symbol, channel, start_ns, end_ns))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if symbol in self.gated and self.gate is not None:
                await self.gate.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            if symbol in self.raises:
                raise self.raises[symbol]
            currency, scale = self.currency.get(symbol, ("TRY", 0))
            for session, close in self.bars.get(symbol, []):
                ts = daily_bar_ts_ns(session)
                if start_ns <= ts < end_ns + NS_PER_DAY:
                    yield daily_bar(
                        self.name,
                        symbol,
                        session,
                        close,
                        currency=currency,
                        price_scale=scale,
                        captured_ns=captured_ns,
                    )
            for ex_date, value in self.dividends.get(symbol, []):
                ts = daily_bar_ts_ns(ex_date)
                if start_ns <= ts < end_ns + NS_PER_DAY:
                    yield dividend(self.name, symbol, ex_date, value, captured_ns=captured_ns)
        finally:
            self.in_flight -= 1

    def intraday(
        self, listing: Listing, interval: str, *, captured_ns: int
    ) -> AsyncIterator[Record]:
        return self._intraday(listing, interval)

    async def _intraday(self, listing: Listing, interval: str) -> AsyncIterator[Record]:
        self.intraday_calls.append((listing.symbol, interval))
        for bar in self.intraday_bars.get(listing.symbol, []):
            if bar.interval == interval:
                yield bar


@dataclass
class FakeUniverseSource:
    """A universe source (a directory or TradingView's universe role)."""

    name: str
    units: dict[str, frozenset[str]]
    snapshots: dict[str, UniverseSnapshot] = field(default_factory=dict)
    errors: dict[str, BaseException] = field(default_factory=dict)
    calls: list[tuple[str, int]] = field(default_factory=list)

    def units_for(self, mics: frozenset[str]) -> dict[str, frozenset[str]]:
        found: dict[str, frozenset[str]] = {}
        for unit, enumerated in self.units.items():
            requested = enumerated & mics
            if requested:
                found[unit] = requested
        return found

    async def universe(self, unit: str, *, captured_ns: int) -> UniverseSnapshot:
        self.calls.append((unit, captured_ns))
        if unit in self.errors:
            raise self.errors[unit]
        return self.snapshots[unit]


@dataclass
class FakeSnapshotSource:
    """A snapshot source (TradingView's EOD role). Returns ``SnapshotBatch`` per unit.

    ``bars`` maps a unit to its bars (canonical symbols). The fake keeps only bars whose MIC is
    passed with its session and whose session day matches it, as the binding contract says.
    ``unattributed`` lists MICs per unit that the snapshot cannot attribute (ruling 29).
    ``calls`` logs ``(unit, dict(sessions), captured_ns)``.
    """

    name: str
    units: dict[str, frozenset[str]]
    bars: dict[str, tuple[Record, ...]] = field(default_factory=dict)
    unattributed: dict[str, frozenset[str]] = field(default_factory=dict)
    errors: dict[str, BaseException] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, datetime.date], int]] = field(default_factory=list)

    def units_for(self, mics: frozenset[str]) -> dict[str, frozenset[str]]:
        found: dict[str, frozenset[str]] = {}
        for unit, enumerated in self.units.items():
            requested = enumerated & mics
            if requested:
                found[unit] = requested
        return found

    async def snapshot(
        self, unit: str, sessions: Mapping[str, datetime.date], *, captured_ns: int
    ) -> SnapshotBatch:
        self.calls.append((unit, dict(sessions), captured_ns))
        if unit in self.errors:
            raise self.errors[unit]
        skipped_mics = self.unattributed.get(unit, frozenset())
        attributed = frozenset(mic for mic in sessions if mic not in skipped_mics)
        kept: list[Record] = []
        for record in self.bars.get(unit, ()):
            _local, mic = parse_listing_symbol(record.symbol)
            if mic not in attributed:
                continue
            if getattr(record, "source_ts", None) != daily_bar_ts_ns(sessions[mic]):
                continue
            kept.append(record)
        return SnapshotBatch(records=tuple(kept), attributed_mics=attributed)


class RecordingSink(Sink):
    """Logs ``("put", symbol)`` and ``("flush",)`` events in order, and keeps the records."""

    def __init__(self, events: list[tuple[str, ...]] | None = None) -> None:
        self.events: list[tuple[str, ...]] = events if events is not None else []
        self.records: list[Record] = []

    async def put(self, record: Record) -> None:
        self.events.append(("put", record.symbol))
        self.records.append(record)

    async def flush(self) -> None:
        self.events.append(("flush",))


def make_orchestrator(
    tmp_path: Path,
    bindings: SourceBindings,
    *,
    clock: Callable[[], datetime.datetime] = lambda: FIXED_NOW,
    master: ListingMaster | None = None,
    state: UniversalState | None = None,
    sink: Sink | None = None,
    **kw: Any,
) -> UniversalOrchestrator:
    """An orchestrator over file-backed master and state under ``tmp_path`` (its data dir)."""
    kw.setdefault("data_dir", tmp_path)
    return UniversalOrchestrator(
        master=master if master is not None else ListingMaster.for_data_dir(tmp_path),
        state=state if state is not None else UniversalState.at(tmp_path),
        sink=sink if sink is not None else RecordingSink(),
        bindings=bindings,
        clock=clock,
        **kw,
    )
