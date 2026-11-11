"""The universal orchestrator: scope resolution, universe sync and resumable backfill.

Sources reach the orchestrator through three small protocols (``HistorySource``,
``UniverseSource``, ``SnapshotSource``) bound by ``bindings.py``; every test binds fakes. The
orchestrator never builds market records: sources build them through ``provenance_fields()``,
and the orchestrator routes, checks and writes them. Watermarks are queued on success and
committed only after the sink has flushed, so a stored watermark never claims rows that still
sit in a sink buffer.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

import msgspec

from crocodile.core.config import Settings
from crocodile.core.errors import TransientConnectorError
from crocodile.core.ingest.deadletter import DeadLetterQueue, dlq_db_path, report_drained_dlqs
from crocodile.core.markets.calendar import calendar_for
from crocodile.core.markets.currency import check_currency_consistent
from crocodile.core.markets.errors import (
    CalendarRangeError,
    CurrencyMismatchError,
    ListingConflictError,
    NoGrammarForMarket,
    SanctionedMarketError,
    UnknownMarketError,
)
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.master import (
    Listing,
    ListingMaster,
    ListingStatus,
    default_listings_path,
)
from crocodile.core.markets.mic import MicRegistry
from crocodile.core.markets.sanctions import (
    allowed_countries,
    is_sanctioned,
    require_not_sanctioned,
)
from crocodile.core.markets.symbols import is_listing_symbol, parse_listing_symbol
from crocodile.core.schema.records import OHLCV, CorporateAction, Record
from crocodile.core.sink.base import Sink
from crocodile.equity.providers.base import (
    TransientProviderError,
    UnsupportedMarketError,
    backoff_delays,
)
from crocodile.equity.universal.base import UniverseSnapshot, apply_universe
from crocodile.equity.universal.order import (
    NS_PER_DAY as ORDER_NS_PER_DAY,
)
from crocodile.equity.universal.order import (
    UNIVERSAL_ALIAS,
    SourceOrder,
    SourceOrderError,
    is_universal_source,
    source_order,
)
from crocodile.equity.universal.state import Coverage, UniversalState, missing_ranges

logger = logging.getLogger(__name__)

BACKFILL_CHANNELS: Final = frozenset({"ohlcv", "corp_action"})
EOD_DELAY: Final = datetime.timedelta(minutes=45)
DEFAULT_MAX_CONCURRENCY: Final = 4
EOD_FALLBACK_LIMIT: Final = 50
MAX_INTRADAY_LISTINGS: Final = 100
INTRADAY_INTERVAL_SECONDS: Final[Mapping[str, int]] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
}
DEFAULT_INTRADAY_INTERVAL: Final = "1m"
CHECKPOINT_EVERY: Final = 25
MAX_TRANSIENT_ATTEMPTS: Final = 3
EARLIEST_NS: Final = -2_208_988_800_000_000_000  # 1900-01-01 UTC: all history the source gives

NS_PER_DAY: Final = ORDER_NS_PER_DAY
_EPOCH_DATE: Final = datetime.date(1970, 1, 1)
_EPOCH_UTC: Final = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)
_DAILY_INTERVAL: Final = "1d"
_CORP_INTERVAL: Final = ""
_LISTING_SCOPE_MESSAGE: Final = "a scope needs mic, country, all_markets or symbols"


@dataclass(frozen=True, slots=True)
class Scope:
    mic: str | None = None
    country: str | None = None
    all_markets: bool = False
    symbols: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    mics: tuple[str, ...]
    listings: tuple[Listing, ...]
    explicit_listings: bool
    refused: tuple[tuple[str, str | None, str], ...]


class OutcomeStatus(StrEnum):
    OK = "ok"
    ALREADY_COVERED = "already_covered"
    NO_DATA = "no_data"
    UNSERVABLE = "unservable"
    SANCTIONED = "sanctioned"
    TRANSIENT_ERROR = "transient_error"
    FAILED = "failed"
    UNKNOWN_LISTING = "unknown_listing"
    NO_SOURCE = "no_source"
    NO_SESSION = "no_session"
    BUDGET_EXHAUSTED = "budget_exhausted"
    IDENTITY_CONFLICT = "identity_conflict"
    EMPTY_UNIVERSE = "empty_universe"


class Outcome(msgspec.Struct, frozen=True, kw_only=True):
    subject: str
    mic: str | None
    source: str | None
    channel: str | None
    status: OutcomeStatus
    reason: str | None = None
    records: int = 0


class RunReport(msgspec.Struct, frozen=True, kw_only=True):
    run_id: str
    kind: str
    started_ns: int
    finished_ns: int
    scope: Scope
    records_written: int
    dead_letters: int
    outcomes: tuple[Outcome, ...]
    report_path: str | None = None

    def by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            key = str(outcome.status)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def unservable_by_listing(self) -> dict[str, dict[str, str]]:
        found: dict[str, dict[str, str]] = {}
        for outcome in self.outcomes:
            if outcome.status is OutcomeStatus.UNSERVABLE and outcome.source is not None:
                found.setdefault(outcome.subject, {})[outcome.source] = outcome.reason or ""
        return found

    def market_outcomes(self, mic: str) -> tuple[Outcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.subject == mic)


class HistorySource(Protocol):
    name: str

    def serves(self, mic: str, channel: str) -> bool: ...

    def history(
        self, listing: Listing, channel: str, start_ns: int, end_ns: int, *, captured_ns: int
    ) -> AsyncIterator[Record]: ...

    def intraday(
        self, listing: Listing, interval: str, *, captured_ns: int
    ) -> AsyncIterator[Record]: ...


class UniverseSource(Protocol):
    name: str

    def units_for(self, mics: frozenset[str]) -> Mapping[str, frozenset[str]]: ...

    async def universe(self, unit: str, *, captured_ns: int) -> UniverseSnapshot: ...


@dataclass(frozen=True, slots=True)
class SnapshotBatch:
    records: Sequence[Record]
    attributed_mics: frozenset[str]


class SnapshotSource(Protocol):
    name: str

    def units_for(self, mics: frozenset[str]) -> Mapping[str, frozenset[str]]: ...

    async def snapshot(
        self, unit: str, sessions: Mapping[str, datetime.date], *, captured_ns: int
    ) -> SnapshotBatch: ...


@dataclass(frozen=True, slots=True)
class SourceBindings:
    history: Mapping[str, HistorySource] = field(default_factory=dict)
    universe: Mapping[str, UniverseSource] = field(default_factory=dict)
    snapshot: Mapping[str, SnapshotSource] = field(default_factory=dict)
    resources: tuple[object, ...] = ()


def listing_master_path(data_dir: Path) -> Path:
    return default_listings_path(data_dir)


def _midnight_ns(day: datetime.date) -> int:
    return (day - _EPOCH_DATE).days * NS_PER_DAY


def _session_of(source_ts: int) -> datetime.date:
    return _EPOCH_DATE + datetime.timedelta(days=source_ts // NS_PER_DAY)


def _ns_of(moment: datetime.datetime) -> int:
    delta = moment - _EPOCH_UTC
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def _check_shape(scope: Scope) -> None:
    named = [
        name
        for name, present in (
            ("mic", scope.mic is not None),
            ("country", scope.country is not None),
            ("all_markets", scope.all_markets),
        )
        if present
    ]
    if not named and not scope.symbols:
        raise ValueError(_LISTING_SCOPE_MESSAGE)
    if len(named) > 1:
        raise ValueError(f"a scope names one market, got {', '.join(named)}")
    if scope.all_markets and scope.symbols:
        raise ValueError("all_markets cannot be combined with symbols")


def _slice(
    scope: Scope, allow: Collection[str]
) -> tuple[list[str], list[tuple[str, str | None, str]]]:
    """The operating MICs a mic/country/all scope names, and the sanctioned ones refused."""
    if scope.mic is not None:
        operating = MicRegistry().operating(scope.mic.upper())
        ExchangeRegistry().get(operating)
        require_not_sanctioned(operating, allow=allow)
        return [operating], []
    if scope.country is not None:
        infos = ExchangeRegistry().by_country(scope.country.upper())
        if not infos:
            raise ValueError(f"no exchange is registered for country {scope.country!r}")
        return _open_and_refused([info.mic for info in infos], allow)
    if scope.all_markets:
        return _open_and_refused([info.mic for info in ExchangeRegistry().all()], allow)
    return [], []


def _open_and_refused(
    mics: Sequence[str], allow: Collection[str]
) -> tuple[list[str], list[tuple[str, str | None, str]]]:
    sanctioned = [mic for mic in mics if is_sanctioned(mic, allow=allow)]
    if mics and len(sanctioned) == len(mics):
        require_not_sanctioned(mics[0], allow=allow)
    open_mics = sorted(mic for mic in mics if mic not in sanctioned)
    refused: list[tuple[str, str | None, str]] = [
        (mic, mic, "sanctioned") for mic in sorted(sanctioned)
    ]
    return open_mics, refused


def _lookup(master: ListingMaster, raw: str) -> Listing | None:
    if ":" in raw and not is_listing_symbol(raw):
        source, _, rest = raw.partition(":")
        return master.resolve_alias(source.lower(), rest)
    return master.get(raw)


def resolve_scope(
    scope: Scope, master: ListingMaster, *, allow: Collection[str] = ()
) -> ResolvedScope:
    _check_shape(scope)
    open_mics, refused = _slice(scope, allow)
    scoped = frozenset(open_mics)
    listings: list[Listing] = []
    if scope.symbols:
        for raw in scope.symbols:
            listing = _lookup(master, raw)
            if listing is None:
                refused.append((raw, None, "unknown_listing"))
                continue
            require_not_sanctioned(listing.mic, allow=allow)
            if scoped and listing.mic not in scoped:
                raise ValueError(
                    f"{listing.symbol} trades on {listing.mic}, outside the named market"
                )
            if listing not in listings:
                listings.append(listing)
        if not listings:
            raise ValueError(
                "none of the named symbols is known to the listing master; run "
                "crocodile universe-sync for the scope first"
            )
        mics = tuple(sorted(scoped)) if scoped else tuple(sorted({item.mic for item in listings}))
    else:
        mics = tuple(sorted(scoped))
    return ResolvedScope(
        mics=mics,
        listings=tuple(listings),
        explicit_listings=bool(scope.symbols),
        refused=tuple(refused),
    )


def preflight_scope(settings: Settings, scope: Scope) -> ResolvedScope:
    """Registry-only check for the capabilities: never opens a file, never reads the master."""
    _check_shape(scope)
    allow = allowed_countries(settings)
    open_mics, refused = _slice(scope, allow)
    symbol_mics: set[str] = set()
    for raw in scope.symbols:
        if is_listing_symbol(raw):
            _local, mic = parse_listing_symbol(raw)
            if mic is None:
                continue
            require_not_sanctioned(mic, allow=allow)
            symbol_mics.add(mic)
    mics = tuple(sorted(open_mics)) if open_mics else tuple(sorted(symbol_mics))
    return ResolvedScope(
        mics=mics,
        listings=(),
        explicit_listings=bool(scope.symbols),
        refused=tuple(refused),
    )


def _validate_sources(sources: Sequence[str]) -> tuple[str, ...]:
    if not sources:
        raise ValueError("sources needs at least one entry: 'universal' or a universal source name")
    for name in sources:
        if name != UNIVERSAL_ALIAS and not is_universal_source(name):
            raise ValueError(
                f"{name!r} is not a universal source; native providers keep their own path "
                f"(sources are {UNIVERSAL_ALIAS!r} or a universal source name)"
            )
    return tuple(sources)


def _classify(exc: Exception) -> tuple[OutcomeStatus, str]:
    """The status and reason of one failed attempt (the table of spec section 3.10e)."""
    if isinstance(exc, SanctionedMarketError):
        return OutcomeStatus.SANCTIONED, str(exc)
    if isinstance(exc, (UnsupportedMarketError, NoGrammarForMarket, UnknownMarketError)):
        return OutcomeStatus.UNSERVABLE, type(exc).__name__
    reason = getattr(exc, "reason", None)
    if reason:
        return OutcomeStatus.UNSERVABLE, str(reason)
    if isinstance(exc, NotImplementedError):
        return OutcomeStatus.UNSERVABLE, "not_supported_by_source"
    if isinstance(exc, TimeoutError):
        return OutcomeStatus.UNSERVABLE, "timeout"
    if isinstance(exc, ListingConflictError):
        return OutcomeStatus.IDENTITY_CONFLICT, str(exc)
    if isinstance(exc, (TransientProviderError, TransientConnectorError)):
        return OutcomeStatus.TRANSIENT_ERROR, str(exc)
    return OutcomeStatus.FAILED, f"{type(exc).__name__}: {exc}"


_STOP_STATUSES: Final = frozenset(
    {
        OutcomeStatus.OK,
        OutcomeStatus.ALREADY_COVERED,
        OutcomeStatus.TRANSIENT_ERROR,
        OutcomeStatus.SANCTIONED,
    }
)
_STORED_STATUSES: Final = frozenset(
    {
        OutcomeStatus.NO_DATA,
        OutcomeStatus.UNSERVABLE,
        OutcomeStatus.FAILED,
        OutcomeStatus.TRANSIENT_ERROR,
        OutcomeStatus.SANCTIONED,
    }
)


@dataclass(slots=True)
class _Run:
    kind: str
    scope: Scope
    started_ns: int
    outcomes: list[Outcome] = field(default_factory=list)
    records: int = 0

    def add(self, outcome: Outcome) -> None:
        self.outcomes.append(outcome)


@dataclass(frozen=True, slots=True)
class _Pending:
    source: str
    symbol: str
    mic: str
    channel: str
    interval: str
    lo: int
    hi: int


def _run_id(kind: str, started_ns: int) -> str:
    return f"{kind}-{started_ns}"


def _interval_of(channel: str) -> str:
    return _DAILY_INTERVAL if channel == "ohlcv" else _CORP_INTERVAL


def _covered_tuple(coverage: Coverage | None) -> tuple[int, int] | None:
    if coverage is None or coverage.covered_from_ns is None or coverage.covered_until_ns is None:
        return None
    return coverage.covered_from_ns, coverage.covered_until_ns


def _group_by_coverage(
    state: UniversalState, name: str, symbol: str, channels: Sequence[str]
) -> list[list[str]]:
    """Channels grouped by their current coverage tuple: one history call per group."""
    groups: dict[tuple[int, int] | None, list[str]] = {}
    for channel in channels:
        covered = _covered_tuple(state.coverage(name, symbol, channel, _interval_of(channel)))
        groups.setdefault(covered, []).append(channel)
    return list(groups.values())


def _coverage_items(
    name: str,
    symbol: str,
    mic: str,
    group: Sequence[str],
    covered: tuple[int, int] | None,
    done: Sequence[tuple[int, int, int | None]],
) -> list[_Pending]:
    """The watermark ranges of the successful gaps of one group (head: start, tail: end)."""
    items: list[_Pending] = []
    for gap_from, gap_to, last_bar in done:
        if covered is not None and gap_to < covered[0]:
            lo, hi = gap_from, covered[1]
        else:
            if "ohlcv" in group:
                if last_bar is None:
                    continue
                end = last_bar
            else:
                end = gap_to
            lo = covered[0] if covered is not None else gap_from
            hi = end
        for channel in group:
            items.append(
                _Pending(
                    source=name,
                    symbol=symbol,
                    mic=mic,
                    channel=channel,
                    interval=_interval_of(channel),
                    lo=lo,
                    hi=hi,
                )
            )
    return items


class UniversalOrchestrator:
    name: str = "universal"

    def __init__(
        self,
        *,
        master: ListingMaster,
        state: UniversalState,
        sink: Sink,
        bindings: SourceBindings,
        order: SourceOrder | None = None,
        data_dir: Path | None = None,
        clock: Callable[[], datetime.datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        allow: Collection[str] = (),
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        eod_delay: datetime.timedelta = EOD_DELAY,
        eod_fallback_limit: int = EOD_FALLBACK_LIMIT,
        checkpoint_every: int = CHECKPOINT_EVERY,
        dlq: DeadLetterQueue | None = None,
        dlq_report_path: Path | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError(f"max_concurrency must be >= 1, got {max_concurrency}")
        self._master = master
        self._state = state
        self._sink = sink
        self._bindings = bindings
        self._order = order if order is not None else source_order()
        self._data_dir = data_dir
        self._clock: Callable[[], datetime.datetime] = (
            clock if clock is not None else lambda: datetime.datetime.now(datetime.UTC)
        )
        self._sleep = sleep
        self._allow: frozenset[str] = frozenset(allow)
        self._max_concurrency = max_concurrency
        self._eod_delay = eod_delay
        self._eod_fallback_limit = eod_fallback_limit
        self._checkpoint_every = checkpoint_every
        self._dlq_report_path = dlq_report_path
        if dlq is not None:
            self._dlq = dlq
        elif data_dir is not None:
            self._dlq = DeadLetterQueue(db_path=str(dlq_db_path(data_dir, "universal")))
        else:
            self._dlq = DeadLetterQueue()
        self._lock = asyncio.Lock()
        self._pending: list[_Pending] = []
        self._successes = 0
        self._closed = False

    def now(self) -> datetime.datetime:
        return self._clock()

    def now_ns(self) -> int:
        return _ns_of(self._clock())

    @property
    def bindings(self) -> SourceBindings:
        return self._bindings

    @property
    def order(self) -> SourceOrder:
        return self._order

    def resolve_scope(self, scope: Scope) -> ResolvedScope:
        return resolve_scope(scope, self._master, allow=self._allow)

    def _chain(
        self, mic: str, channel: str, sources: Sequence[str], mapping: Mapping[str, object]
    ) -> tuple[str, ...]:
        """The source names that may answer (mic, channel), in order, bound in ``mapping``."""
        names: list[str] = []
        for name in sources:
            if name == UNIVERSAL_ALIAS:
                try:
                    names.extend(self._order.order_for(mic, channel))
                except SourceOrderError:
                    continue
            else:
                names.append(name)
        return tuple(name for name in dict.fromkeys(names) if name in mapping)

    async def _checkpoint(self) -> None:
        """Flush the sink, then commit every queued watermark (flush-before-commit)."""
        async with self._lock:
            if not self._pending:
                return
            await self._sink.flush()
            for item in self._pending:
                self._state.record_success(
                    source=item.source,
                    symbol=item.symbol,
                    mic=item.mic,
                    channel=item.channel,
                    interval=item.interval,
                    covered_from_ns=item.lo,
                    covered_until_ns=item.hi,
                    at_ns=self.now_ns(),
                )
            self._pending.clear()

    async def _checkpoint_quietly(self) -> None:
        try:
            await self._checkpoint()
        except Exception:
            logger.exception("universal checkpoint failed; the next run refetches the queued range")

    async def _queue_success(self, item: _Pending) -> None:
        self._pending.append(item)
        self._successes += 1
        if self._successes % self._checkpoint_every == 0:
            await self._checkpoint()

    def _record_failure(
        self,
        *,
        source: str,
        symbol: str,
        mic: str,
        channel: str,
        interval: str,
        status: OutcomeStatus,
        reason: str | None,
    ) -> None:
        self._state.record_outcome(
            source=source,
            symbol=symbol,
            mic=mic,
            channel=channel,
            interval=interval,
            status=str(status),
            reason=reason,
            at_ns=self.now_ns(),
        )

    async def _finish(self, run: _Run) -> RunReport:
        """Checkpoint, drain the DLQs, persist the report and log one summary line."""
        await self._checkpoint_quietly()
        started = run.started_ns
        run_id = _run_id(run.kind, started)
        runs_dir = self._runs_dir()
        report_path: Path | None = None
        if runs_dir is not None:
            runs_dir.mkdir(parents=True, exist_ok=True)
            report_path = runs_dir / f"{run_id}.json"
        dlq_path = self._dlq_report_path
        if dlq_path is None and runs_dir is not None:
            dlq_path = runs_dir / f"{run_id}-dlq.json"
        drained = report_drained_dlqs(
            [self, *self._bindings.resources], report_path=dlq_path, data_dir=None
        )
        report = RunReport(
            run_id=run_id,
            kind=run.kind,
            started_ns=started,
            finished_ns=self.now_ns(),
            scope=run.scope,
            records_written=run.records,
            dead_letters=int(drained.get("count", 0)),
            outcomes=tuple(
                sorted(
                    run.outcomes,
                    key=lambda o: (o.subject, o.channel or "", o.source or ""),
                )
            ),
            report_path=None if report_path is None else str(report_path),
        )
        if report_path is not None:
            report_path.write_bytes(msgspec.json.encode(report))
        logger.info("universal %s %s: %s", run.kind, run_id, report.by_status())
        return report

    def _runs_dir(self) -> Path | None:
        if self._data_dir is None:
            return None
        return self._data_dir / "state" / "runs"

    async def _close_resource(self, resource: object) -> None:
        close = getattr(resource, "close", None)
        if close is None:
            return
        result = close()
        if asyncio.iscoroutine(result):
            await result

    async def aclose(self) -> None:
        """Final checkpoint and DLQ drain, then close resources, state and master (idempotent)."""
        if self._closed:
            return
        self._closed = True
        await self._checkpoint_quietly()
        runs_dir = self._runs_dir()
        report_path: Path | None = None
        if runs_dir is not None:
            runs_dir.mkdir(parents=True, exist_ok=True)
            report_path = runs_dir / f"close-{self.now_ns()}-dlq.json"
        try:
            report_drained_dlqs(
                [self, *self._bindings.resources],
                report_path=self._dlq_report_path or report_path,
                data_dir=None,
            )
        finally:
            for resource in self._bindings.resources:
                await self._close_resource(resource)
            self._state.close()
            self._master.close()

    def _refused_outcomes(
        self,
        run: _Run,
        refused: Sequence[tuple[str, str | None, str]],
        channels: Sequence[str],
    ) -> None:
        """Sanctioned and unknown subjects become outcomes without any source request."""
        for subject, mic, reason in refused:
            if reason == "sanctioned":
                status = OutcomeStatus.SANCTIONED
                message = (
                    "sanctioned: default-closed market "
                    "(CROCODILE_ALLOW_SANCTIONED_MARKETS opens it)"
                )
            else:
                status = OutcomeStatus.UNKNOWN_LISTING
                message = reason
            for channel in channels:
                run.add(
                    Outcome(
                        subject=subject,
                        mic=mic,
                        source=None,
                        channel=channel,
                        status=status,
                        reason=message,
                    )
                )

    async def _sync_unit(
        self,
        run: _Run,
        source: UniverseSource,
        unit: str,
        requested: frozenset[str],
    ) -> None:
        captured = self.now_ns()
        try:
            snapshot = await source.universe(unit, captured_ns=captured)
        except Exception as exc:
            status, reason = _classify(exc)
            if status is OutcomeStatus.FAILED:
                logger.warning("universe unit %s/%s failed", source.name, unit, exc_info=True)
            for mic in sorted(requested):
                run.add(
                    Outcome(
                        subject=mic,
                        mic=mic,
                        source=source.name,
                        channel="instrument",
                        status=status,
                        reason=reason,
                    )
                )
            return
        report = apply_universe(self._master, snapshot)
        for record in snapshot.records:
            await self._sink.put(record)
            run.records += 1
        notes = [f"{key}={value}" for key, value in sorted(snapshot.skipped.items())]
        if not snapshot.complete:
            notes.append("incomplete read: unseen marking skipped")
        note = "; ".join(notes) or None
        for mic in sorted(requested):
            seen = snapshot.seen.get(mic)
            if seen:
                run.add(
                    Outcome(
                        subject=mic,
                        mic=mic,
                        source=source.name,
                        channel="instrument",
                        status=OutcomeStatus.OK,
                        reason=note,
                        records=len(seen),
                    )
                )
            else:
                run.add(
                    Outcome(
                        subject=mic,
                        mic=mic,
                        source=source.name,
                        channel="instrument",
                        status=OutcomeStatus.EMPTY_UNIVERSE,
                        reason=note,
                    )
                )
        for conflict in report.conflicts:
            run.add(
                Outcome(
                    subject=source.name,
                    mic=None,
                    source=source.name,
                    channel="instrument",
                    status=OutcomeStatus.IDENTITY_CONFLICT,
                    reason=conflict,
                )
            )

    async def sync_universe(
        self, scope: Scope, *, sources: Sequence[str] = (UNIVERSAL_ALIAS,)
    ) -> RunReport:
        names = _validate_sources(sources)
        resolved = self.resolve_scope(scope)
        run = _Run(kind="sync_universe", scope=scope, started_ns=self.now_ns())
        self._refused_outcomes(run, resolved.refused, ("instrument",))
        mics = resolved.mics
        chains = {
            mic: self._chain(mic, "instrument", names, self._bindings.universe) for mic in mics
        }
        for mic in mics:
            if not chains[mic]:
                run.add(
                    Outcome(
                        subject=mic,
                        mic=mic,
                        source=None,
                        channel="instrument",
                        status=OutcomeStatus.NO_SOURCE,
                        reason="no universe source is bound for this market",
                    )
                )
        order: list[str] = []
        for mic in mics:
            for name in chains[mic]:
                if name not in order:
                    order.append(name)
        for name in order:
            source = self._bindings.universe[name]
            wanted = frozenset(mic for mic in mics if name in chains[mic])
            units = source.units_for(wanted)
            covered: set[str] = set()
            for unit in sorted(units):
                await self._sync_unit(run, source, unit, units[unit])
                covered.update(units[unit])
            for mic in sorted(wanted - covered):
                run.add(
                    Outcome(
                        subject=mic,
                        mic=mic,
                        source=name,
                        channel="instrument",
                        status=OutcomeStatus.NO_SOURCE,
                        reason="no unit enumerates this market",
                    )
                )
        return await self._finish(run)

    def _cap_ns(self, mic: str, now: datetime.datetime) -> int:
        """UTC midnight of the latest completed session of ``mic`` (yesterday's when unknown)."""
        try:
            session = calendar_for(mic).latest_completed_session(now, self._eod_delay)
        except CalendarRangeError:
            session = None
        if session is None:
            session = now.astimezone(datetime.UTC).date() - datetime.timedelta(days=1)
        return _midnight_ns(session)

    async def backfill(
        self,
        scope: Scope,
        channels: Sequence[str],
        *,
        start: datetime.date | None = None,
        end: datetime.date,
        sources: Sequence[str] = (UNIVERSAL_ALIAS,),
    ) -> RunReport:
        """Daily history and corporate actions, resumable from the stored coverage.

        Universal backfill writes daily history only: ``interval`` stays a crypto parameter.
        """
        names = _validate_sources(sources)
        wanted = tuple(dict.fromkeys(channels))
        if not wanted:
            raise ValueError("backfill needs at least one channel: ohlcv or corp_action")
        outside = [channel for channel in wanted if channel not in BACKFILL_CHANNELS]
        if outside:
            raise ValueError(
                f"backfill serves {sorted(BACKFILL_CHANNELS)}; {outside} is not one of them "
                "(instrument is written by crocodile universe-sync)"
            )
        if start is not None and start > end:
            raise ValueError(f"start {start} is after end {end}")
        resolved = self.resolve_scope(scope)
        listings: dict[str, Listing] = {}
        if resolved.explicit_listings:
            listings.update({listing.symbol: listing for listing in resolved.listings})
        else:
            for mic in resolved.mics:
                for listing in self._master.listings(mic=mic, status=ListingStatus.ACTIVE):
                    listings[listing.symbol] = listing
        refused_listings: list[Listing] = []
        for _subject, refused_mic, reason in resolved.refused:
            if reason == "sanctioned" and refused_mic is not None:
                refused_listings.extend(self._master.listings(mic=refused_mic))
        if not listings and not refused_listings:
            raise ValueError(
                "no listing is known for this scope; run crocodile universe-sync for it first"
            )
        run = _Run(kind="backfill", scope=scope, started_ns=self.now_ns())
        self._refused_outcomes(
            run,
            [entry for entry in resolved.refused if entry[2] != "sanctioned"],
            wanted,
        )
        for listing in refused_listings:
            for channel in wanted:
                run.add(
                    Outcome(
                        subject=listing.symbol,
                        mic=listing.mic,
                        source=None,
                        channel=channel,
                        status=OutcomeStatus.SANCTIONED,
                        reason=(
                            "sanctioned: default-closed market "
                            "(CROCODILE_ALLOW_SANCTIONED_MARKETS opens it)"
                        ),
                    )
                )
        window_from = EARLIEST_NS if start is None else _midnight_ns(start)
        window_to = _midnight_ns(end)
        semaphore = asyncio.Semaphore(self._max_concurrency)
        try:
            async with asyncio.TaskGroup() as group:
                for symbol in sorted(listings):
                    group.create_task(
                        self._backfill_unit(
                            run, semaphore, listings[symbol], wanted, names, window_from, window_to
                        )
                    )
        finally:
            await self._checkpoint_quietly()
        return await self._finish(run)

    async def _backfill_unit(
        self,
        run: _Run,
        semaphore: asyncio.Semaphore,
        listing: Listing,
        channels: tuple[str, ...],
        names: tuple[str, ...],
        window_from: int,
        window_to: int,
    ) -> None:
        async with semaphore:
            try:
                await self._backfill_listing(run, listing, channels, names, window_from, window_to)
            except Exception as exc:
                status, reason = _classify(exc)
                if status is OutcomeStatus.FAILED:
                    logger.warning("backfill of %s failed", listing.symbol, exc_info=True)
                for channel in channels:
                    run.add(
                        Outcome(
                            subject=listing.symbol,
                            mic=listing.mic,
                            source=None,
                            channel=channel,
                            status=status,
                            reason=reason,
                        )
                    )

    async def _backfill_listing(
        self,
        run: _Run,
        listing: Listing,
        channels: tuple[str, ...],
        names: tuple[str, ...],
        window_from: int,
        window_to: int,
    ) -> None:
        cap = self._cap_ns(listing.mic, self.now())
        to = min(window_to, cap)
        if to < window_from:
            for channel in channels:
                run.add(
                    Outcome(
                        subject=listing.symbol,
                        mic=listing.mic,
                        source=None,
                        channel=channel,
                        status=OutcomeStatus.ALREADY_COVERED,
                        reason="range ends after the latest completed session",
                    )
                )
            return
        chain = list(self._chain(listing.mic, "ohlcv", names, self._bindings.history))
        for name in self._chain(listing.mic, "corp_action", names, self._bindings.history):
            if name not in chain:
                chain.append(name)
        pending = list(channels)
        attempted: set[str] = set()
        for name in chain:
            if not pending:
                break
            source = self._bindings.history[name]
            serving = [channel for channel in pending if source.serves(listing.mic, channel)]
            for group in _group_by_coverage(self._state, name, listing.symbol, serving):
                statuses = await self._run_group(run, source, listing, group, window_from, to, cap)
                attempted.update(group)
                pending = [
                    channel
                    for channel in pending
                    if channel not in statuses or statuses[channel] not in _STOP_STATUSES
                ]
        for channel in pending:
            if channel not in attempted:
                run.add(
                    Outcome(
                        subject=listing.symbol,
                        mic=listing.mic,
                        source=None,
                        channel=channel,
                        status=OutcomeStatus.NO_SOURCE,
                        reason="no history source serves this market and channel",
                    )
                )

    async def _run_group(
        self,
        run: _Run,
        source: HistorySource,
        listing: Listing,
        group: list[str],
        window_from: int,
        to: int,
        cap: int,
    ) -> dict[str, OutcomeStatus]:
        """One source's attempt for the channels of one coverage group; returns their statuses."""
        name = source.name
        symbol = listing.symbol
        covered = _covered_tuple(
            self._state.coverage(name, symbol, group[0], _interval_of(group[0]))
        )
        gaps = missing_ranges(window_from, to, covered)
        if not gaps:
            for channel in group:
                run.add(
                    Outcome(
                        subject=symbol,
                        mic=listing.mic,
                        source=name,
                        channel=channel,
                        status=OutcomeStatus.ALREADY_COVERED,
                    )
                )
            return {channel: OutcomeStatus.ALREADY_COVERED for channel in group}
        fetch_channel = "ohlcv" if "ohlcv" in group else group[0]
        captured = self.now_ns()
        bars_total = 0
        actions_total = 0
        done: list[tuple[int, int, int | None]] = []
        failure: tuple[OutcomeStatus, str] | None = None
        for gap_from, gap_to in gaps:
            try:
                kept, bars, actions, last_bar = await self._fetch_gap(
                    source, listing, fetch_channel, gap_from, gap_to, group, captured, cap
                )
            except Exception as exc:
                failure = _classify(exc)
                if failure[0] is OutcomeStatus.FAILED:
                    logger.warning("history fetch %s %s failed", name, symbol, exc_info=True)
                break
            for record in kept:
                await self._sink.put(record)
                run.records += 1
            bars_total += bars
            actions_total += actions
            done.append((gap_from, gap_to, last_bar))
        first_fetch = covered is None
        wrote = bars_total if "ohlcv" in group else actions_total
        if not (first_fetch and wrote == 0):
            for item in _coverage_items(name, symbol, listing.mic, group, covered, done):
                await self._queue_success(item)
        if failure is not None:
            status, reason = failure
        elif first_fetch and wrote == 0:
            status, reason = OutcomeStatus.NO_DATA, None
        elif wrote == 0:
            status, reason = OutcomeStatus.OK, "no new sessions"
        else:
            status, reason = OutcomeStatus.OK, None
        statuses: dict[str, OutcomeStatus] = {}
        for channel in group:
            count = bars_total if channel == "ohlcv" else actions_total
            run.add(
                Outcome(
                    subject=symbol,
                    mic=listing.mic,
                    source=name,
                    channel=channel,
                    status=status,
                    reason=reason,
                    records=count,
                )
            )
            statuses[channel] = status
            if status in _STORED_STATUSES:
                self._record_failure(
                    source=name,
                    symbol=symbol,
                    mic=listing.mic,
                    channel=channel,
                    interval=_interval_of(channel),
                    status=status,
                    reason=reason,
                )
        return statuses

    async def _fetch_gap(
        self,
        source: HistorySource,
        listing: Listing,
        fetch_channel: str,
        gap_from: int,
        gap_to: int,
        group: list[str],
        captured: int,
        cap: int,
    ) -> tuple[list[Record], int, int, int | None]:
        """One gap with transient retries; returns the routed records and their counts."""
        attempt = 0
        while True:
            try:
                return await self._collect_gap(
                    source, listing, fetch_channel, gap_from, gap_to, group, captured, cap
                )
            except (TransientProviderError, TransientConnectorError):
                attempt += 1
                if attempt >= MAX_TRANSIENT_ATTEMPTS:
                    raise
                await self._sleep(backoff_delays(attempt - 1))

    async def _collect_gap(
        self,
        source: HistorySource,
        listing: Listing,
        fetch_channel: str,
        gap_from: int,
        gap_to: int,
        group: list[str],
        captured: int,
        cap: int,
    ) -> tuple[list[Record], int, int, int | None]:
        records = [
            record
            async for record in source.history(
                listing, fetch_channel, gap_from, gap_to, captured_ns=captured
            )
        ]
        end_bound = gap_to + NS_PER_DAY
        kept: list[Record] = []
        bars = 0
        actions = 0
        last_bar: int | None = None
        for record in records:
            if record.symbol != listing.symbol:
                logger.warning(
                    "dropped a record for %s while backfilling %s", record.symbol, listing.symbol
                )
                continue
            if isinstance(record, OHLCV):
                ts = record.source_ts
                if (
                    "ohlcv" not in group
                    or record.interval != _DAILY_INTERVAL
                    or ts is None
                    or not gap_from <= ts < end_bound
                    or ts > cap
                ):
                    continue
            elif isinstance(record, CorporateAction):
                if "corp_action" not in group:
                    continue
            else:
                continue
            try:
                check_currency_consistent(record.currency, record.price_scale, listing)
            except CurrencyMismatchError as exc:
                await self._dlq.put(
                    captured, msgspec.json.encode(record), "CurrencyMismatchError", str(exc)
                )
                continue
            kept.append(record)
            if isinstance(record, OHLCV):
                bars += 1
                if ts is not None and (last_bar is None or ts > last_bar):
                    last_bar = ts
            else:
                actions += 1
        return kept, bars, actions, last_bar
