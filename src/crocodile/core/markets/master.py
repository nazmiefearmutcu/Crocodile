"""Listing master: which listing a canonical symbol identifies, and what each source has seen.

Grain. A listing is an operating MIC plus a local symbol, the exchange's own spelling. The same
local symbol on two markets is two listings. An ISIN keys the instrument, never the listing,
because one ISIN has many listings across markets.

Canonical symbol. A listing on a US operating MIC keeps the bare ticker, and every other listing
is written LOCAL@MIC with the operating MIC. The canonical symbol is unique, so an observation
that would point it at a second listing raises ListingConflictError and writes nothing.

Ruling: a NULL currency means unknown and is never read as USD. Cost if wrong: every listing
without a stated currency is priced in the wrong unit, silently.

Ruling: delisted comes only from an authoritative directory. A source that stops reporting a
listing makes it unseen, and an unseen listing becomes active again when the source reports it.
Cost if wrong: one partial listing page would delist a market.

Ruling: an empty sweep is an outage, not a market with no listings. Cost if wrong: one empty
response from a source would flip every one of its listings to unseen.

Ruling: an issuer exists only when it has a legal entity identifier. Cost if wrong: two issuers
with the same name but different registrations would be merged on a guess.

Ruling: the master reads no clock and no environment; every instant arrives as an argument.
Cost if wrong: a replay of old captures would stamp today's time on yesterday's observations.
"""

import re
import sqlite3
import threading
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar, Final, Self

import msgspec

from crocodile.core.errors import StoreError
from crocodile.core.markets.currency import require_currency
from crocodile.core.markets.errors import ListingConflictError
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.mic import MicRegistry
from crocodile.core.markets.symbols import listing_symbol, validate_local_symbol
from crocodile.core.schema.enums import SecurityType

__all__ = [
    "LISTINGS_DB_NAME",
    "REFERENCE_DIR_NAME",
    "Listing",
    "ListingConflictError",
    "ListingEvent",
    "ListingEventKind",
    "ListingMaster",
    "ListingObservation",
    "ListingStatus",
    "SymbolAlias",
    "default_listings_path",
    "open_sqlite_readonly",
]

REFERENCE_DIR_NAME: Final = "reference"
LISTINGS_DB_NAME: Final = "listings.sqlite"

_MEMORY: Final = ":memory:"
_ISIN_SHAPE: Final = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
_UNKNOWN: Final = "unknown"
_BUSY_TIMEOUT_MS: Final = 30000

_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
    """
    CREATE TABLE IF NOT EXISTS issuers (
        issuer_id  INTEGER PRIMARY KEY,
        lei        TEXT UNIQUE,
        legal_name TEXT,
        country    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS instruments (
        instrument_id    INTEGER PRIMARY KEY,
        isin             TEXT UNIQUE,
        share_class_figi TEXT,
        security_type    TEXT,
        name             TEXT,
        issuer_id        INTEGER REFERENCES issuers (issuer_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS listings (
        listing_id    INTEGER PRIMARY KEY,
        symbol        TEXT NOT NULL UNIQUE,
        mic           TEXT NOT NULL,
        segment_mic   TEXT,
        local_symbol  TEXT NOT NULL,
        instrument_id INTEGER NOT NULL REFERENCES instruments (instrument_id),
        currency      TEXT,
        price_scale   INTEGER,
        status        TEXT NOT NULL CHECK (status IN ('active', 'unseen', 'delisted')),
        listed_date   TEXT,
        delisted_date TEXT,
        first_seen_ns INTEGER NOT NULL,
        last_seen_ns  INTEGER NOT NULL,
        UNIQUE (mic, local_symbol),
        CHECK ((currency IS NULL) = (price_scale IS NULL))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_listings_instrument ON listings (instrument_id)",
    """
    CREATE TABLE IF NOT EXISTS symbol_aliases (
        source        TEXT NOT NULL,
        symbol_raw    TEXT NOT NULL,
        valid_from_ns INTEGER NOT NULL,
        listing_id    INTEGER NOT NULL REFERENCES listings (listing_id),
        valid_to_ns   INTEGER,
        PRIMARY KEY (source, symbol_raw, valid_from_ns),
        CHECK (valid_to_ns IS NULL OR valid_to_ns > valid_from_ns)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_aliases_open_raw
        ON symbol_aliases (source, symbol_raw) WHERE valid_to_ns IS NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_aliases_open_listing
        ON symbol_aliases (listing_id, source) WHERE valid_to_ns IS NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS listing_events (
        listing_id  INTEGER NOT NULL REFERENCES listings (listing_id),
        kind        TEXT NOT NULL CHECK (
            kind IN ('currency_change', 'status_change', 'alias_change')
        ),
        old         TEXT,
        new         TEXT,
        observed_ns INTEGER NOT NULL,
        source      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_listing ON listing_events (listing_id)",
)

_SELECT_LISTINGS: Final = """
    SELECT l.listing_id, l.symbol, l.mic, l.segment_mic, l.local_symbol, l.instrument_id,
           l.currency, l.price_scale, l.status, l.listed_date, l.delisted_date,
           l.first_seen_ns, l.last_seen_ns, i.isin, i.name, i.security_type
    FROM listings l
    JOIN instruments i ON i.instrument_id = l.instrument_id
"""


def default_listings_path(data_dir: str | Path) -> Path:
    """Return ``<data_dir>/reference/listings.sqlite``. Never reads the environment."""
    return Path(data_dir) / REFERENCE_DIR_NAME / LISTINGS_DB_NAME


def open_sqlite_readonly(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open an existing SQLite file for reading without creating or changing any file.

    A WAL database read with plain ``mode=ro`` makes SQLite create -wal and -shm sidecars when no
    writer holds the file, and leaves them behind. So when no -wal file exists, no writer is
    active and the file is opened ``immutable=1``, which creates nothing. When a -wal file exists,
    a writer is or was active, the sidecars already exist, and ``mode=ro`` reads its rows.
    A missing file raises ``sqlite3.OperationalError``; ``mode=ro`` never creates one.
    """
    resolved = Path(path).resolve()
    wal = resolved.with_name(resolved.name + "-wal")
    mode = "?mode=ro" if wal.exists() else "?mode=ro&immutable=1"
    return sqlite3.connect(
        resolved.as_uri() + mode,
        uri=True,
        check_same_thread=check_same_thread,
    )


class ListingStatus(StrEnum):
    ACTIVE = "active"
    UNSEEN = "unseen"
    DELISTED = "delisted"


class ListingEventKind(StrEnum):
    CURRENCY_CHANGE = "currency_change"
    STATUS_CHANGE = "status_change"
    ALIAS_CHANGE = "alias_change"


class ListingObservation(msgspec.Struct, frozen=True, kw_only=True):
    source: str
    mic: str
    local_symbol: str
    symbol_raw: str
    observed_ns: int
    segment_mic: str | None = None
    currency: str | None = None
    price_scale: int | None = None
    status: ListingStatus = ListingStatus.ACTIVE
    isin: str | None = None
    share_class_figi: str | None = None
    security_type: SecurityType | None = None
    name: str | None = None
    issuer_lei: str | None = None
    issuer_name: str | None = None
    issuer_country: str | None = None
    listed_date: str | None = None
    delisted_date: str | None = None
    extra_aliases: tuple[tuple[str, str], ...] = ()


class Listing(msgspec.Struct, frozen=True, kw_only=True):
    listing_id: int
    symbol: str
    mic: str
    segment_mic: str | None
    local_symbol: str
    instrument_id: int
    currency: str | None
    price_scale: int | None
    status: ListingStatus
    listed_date: str | None
    delisted_date: str | None
    first_seen_ns: int
    last_seen_ns: int
    isin: str | None
    name: str | None
    security_type: SecurityType | None


class ListingEvent(msgspec.Struct, frozen=True, kw_only=True):
    kind: ListingEventKind
    old: str | None
    new: str | None
    observed_ns: int
    source: str


class SymbolAlias(msgspec.Struct, frozen=True, kw_only=True):
    source: str
    symbol_raw: str
    valid_from_ns: int
    valid_to_ns: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class _Plan:
    """A validated observation with its market resolved."""

    symbol: str
    operating: str
    segment_mic: str | None
    local: str
    currency: str | None
    price_scale: int | None
    isin: str | None
    listed_date: str | None
    delisted_date: str | None
    pairs: tuple[tuple[str, str], ...]


class ListingMaster:
    SCHEMA_VERSION: ClassVar[int] = 1

    def __init__(
        self,
        db_path: str | Path = _MEMORY,
        *,
        mics: MicRegistry | None = None,
        exchanges: ExchangeRegistry | None = None,
        readonly: bool = False,
    ) -> None:
        self._path = str(db_path)
        self._readonly = readonly
        self._mics = mics if mics is not None else MicRegistry()
        self._exchanges = exchanges if exchanges is not None else ExchangeRegistry()
        self._lock = threading.RLock()
        if readonly:
            self._open_readonly()
        else:
            self._open_writable()

    def _open_readonly(self) -> None:
        if self._path == _MEMORY or not Path(self._path).is_file():
            raise StoreError(
                f"listing master opened read-only needs an existing file, got {self._path}"
            )
        self._conn = open_sqlite_readonly(self._path, check_same_thread=False)
        try:
            self._configure()
            row = self._fetch_version_row()
            self._verify_version(row)
        except BaseException:
            self._conn.close()
            raise

    def _open_writable(self) -> None:
        if self._path != _MEMORY:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        try:
            self._configure()
            if self._path != _MEMORY:
                self._conn.execute("PRAGMA journal_mode = WAL")
            with self._write() as conn:
                for statement in _SCHEMA_STATEMENTS:
                    conn.execute(statement)
                row = conn.execute("SELECT version FROM schema_version").fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO schema_version (version) VALUES (?)",
                        (self.SCHEMA_VERSION,),
                    )
                else:
                    self._verify_version(row)
        except BaseException:
            self._conn.close()
            raise

    def _configure(self) -> None:
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")

    def _fetch_version_row(self) -> sqlite3.Row | None:
        try:
            row: sqlite3.Row | None = self._conn.execute(
                "SELECT version FROM schema_version"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            raise StoreError(f"listing master has no schema_version table: {self._path}") from exc
        return row

    def _verify_version(self, row: sqlite3.Row | None) -> None:
        if row is None:
            raise StoreError(f"listing master has no schema version recorded: {self._path}")
        stored = _int(row["version"])
        if stored != self.SCHEMA_VERSION:
            raise StoreError(
                f"listing master at {self._path} is schema version {stored}; "
                f"this build reads version {self.SCHEMA_VERSION}"
            )

    @classmethod
    def for_data_dir(
        cls,
        data_dir: str | Path,
        *,
        mics: MicRegistry | None = None,
        exchanges: ExchangeRegistry | None = None,
    ) -> Self:
        return cls(default_listings_path(data_dir), mics=mics, exchanges=exchanges)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One write transaction: BEGIN IMMEDIATE, COMMIT, or ROLLBACK on any exception."""
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _refuse_if_readonly(self) -> None:
        if self._readonly:
            raise StoreError(f"listing master opened read-only: {self._path}")

    # ----------------------------------------------------------------- writes

    def upsert(self, observation: ListingObservation) -> Listing:
        self._refuse_if_readonly()
        plan = self._plan(observation)
        with self._write() as conn:
            existing = _one_listing(conn, f"{_SELECT_LISTINGS} WHERE l.symbol = ?", (plan.symbol,))
            if existing is not None:
                if (existing.mic, existing.local_symbol) != (plan.operating, plan.local):
                    raise ListingConflictError(
                        plan.symbol,
                        f"{existing.local_symbol}@{existing.mic}",
                        f"{plan.local}@{plan.operating}",
                    )
                if plan.isin is not None and existing.isin not in (None, plan.isin):
                    raise ListingConflictError(
                        plan.symbol,
                        f"{plan.symbol} with ISIN {existing.isin}",
                        f"ISIN {plan.isin}",
                    )
            if existing is None:
                listing_id = _insert_listing(conn, plan, observation)
            else:
                _merge_listing(conn, existing, plan, observation)
                listing_id = existing.listing_id
            _write_aliases(conn, listing_id, plan.pairs, observation.observed_ns)
            result = _one_listing(conn, f"{_SELECT_LISTINGS} WHERE l.listing_id = ?", (listing_id,))
        if result is None:
            raise StoreError(f"listing {listing_id} vanished inside its own transaction")
        return result

    def mark_unseen(self, mic: str, source: str, seen: Iterable[str], at_ns: int) -> list[Listing]:
        self._refuse_if_readonly()
        if not source:
            raise ValueError("mark_unseen needs a non-empty source")
        if _not_a_nonnegative_int(at_ns):
            raise ValueError(f"at_ns must be an int >= 0, got {at_ns!r}")
        seen_locals = {unicodedata.normalize("NFC", local) for local in seen}
        if not seen_locals:
            raise ValueError(
                "Ruling: an empty sweep is an outage, not a market with no listings. "
                "Cost if wrong: one empty response flips every listing of the source to unseen."
            )
        operating = self._mics.operating(mic)
        changed: list[Listing] = []
        with self._write() as conn:
            candidates = _many_listings(
                conn,
                f"{_SELECT_LISTINGS} WHERE l.mic = ? AND l.status = ? AND l.last_seen_ns < ? "
                "AND EXISTS (SELECT 1 FROM symbol_aliases a WHERE a.listing_id = l.listing_id "
                "AND a.source = ? AND a.valid_to_ns IS NULL) ORDER BY l.symbol",
                (operating, ListingStatus.ACTIVE.value, at_ns, source),
            )
            for listing in candidates:
                if listing.local_symbol in seen_locals:
                    continue
                conn.execute(
                    "UPDATE listings SET status = ? WHERE listing_id = ?",
                    (ListingStatus.UNSEEN.value, listing.listing_id),
                )
                _write_event(
                    conn,
                    listing.listing_id,
                    ListingEventKind.STATUS_CHANGE,
                    old=ListingStatus.ACTIVE.value,
                    new=ListingStatus.UNSEEN.value,
                    observed_ns=at_ns,
                    source=source,
                )
                changed.append(msgspec.structs.replace(listing, status=ListingStatus.UNSEEN))
        return changed

    def _plan(self, obs: ListingObservation) -> _Plan:
        """Validate an observation and resolve its market. Writes nothing."""
        if not obs.source:
            raise ValueError("observation source must be non-empty")
        if not obs.symbol_raw:
            raise ValueError("observation symbol_raw must be non-empty")
        if _not_a_nonnegative_int(obs.observed_ns):
            raise ValueError(f"observed_ns must be an int >= 0, got {obs.observed_ns!r}")
        if obs.status not in (ListingStatus.ACTIVE, ListingStatus.DELISTED):
            raise ValueError(
                f"an observation may only report active or delisted, not {obs.status.value}"
            )
        currency, price_scale = _check_currency(obs.currency, obs.price_scale)
        if obs.isin is not None and not _isin_is_valid(obs.isin):
            raise ValueError(f"malformed ISIN {obs.isin!r}")
        listed = _check_date("listed_date", obs.listed_date)
        delisted = _check_date("delisted_date", obs.delisted_date)
        pairs = ((obs.source, obs.symbol_raw), *obs.extra_aliases)
        for source, raw in pairs:
            if not source or not raw:
                raise ValueError("every alias needs a non-empty source and symbol_raw")
        sources = [source for source, _ in pairs]
        if len(set(sources)) != len(sources):
            raise ValueError("one observation may name only one spelling per source")

        entry = self._mics.require_active(obs.mic)
        operating = entry.operating_mic
        if obs.segment_mic is not None:
            if self._mics.operating(obs.segment_mic) != operating:
                raise ValueError(f"segment {obs.segment_mic} does not belong to market {operating}")
            if entry.is_segment and obs.mic != obs.segment_mic:
                raise ValueError(
                    f"market {obs.mic} is a segment of {operating}, not {obs.segment_mic}"
                )
            segment: str | None = obs.segment_mic
        elif obs.mic != operating:
            segment = obs.mic
        else:
            segment = None
        self._exchanges.get(operating)
        local = validate_local_symbol(obs.local_symbol)
        symbol = listing_symbol(local, operating, mics=self._mics, exchanges=self._exchanges)
        return _Plan(
            symbol=symbol,
            operating=operating,
            segment_mic=segment,
            local=local,
            currency=currency,
            price_scale=price_scale,
            isin=obs.isin,
            listed_date=listed,
            delisted_date=delisted,
            pairs=pairs,
        )

    # ------------------------------------------------------------------ reads

    def get(self, symbol: str) -> Listing | None:
        return self._one(f"{_SELECT_LISTINGS} WHERE l.symbol = ?", (symbol,))

    def find(self, mic: str, local: str) -> Listing | None:
        operating = self._mics.operating(mic)
        normal = unicodedata.normalize("NFC", local)
        return self._one(
            f"{_SELECT_LISTINGS} WHERE l.mic = ? AND l.local_symbol = ?",
            (operating, normal),
        )

    def resolve_alias(self, source: str, raw: str, at_ns: int | None = None) -> Listing | None:
        join = "JOIN symbol_aliases a ON a.listing_id = l.listing_id WHERE a.source = ? "
        if at_ns is None:
            return self._one(
                f"{_SELECT_LISTINGS} {join} AND a.symbol_raw = ? AND a.valid_to_ns IS NULL",
                (source, raw),
            )
        return self._one(
            f"{_SELECT_LISTINGS} {join} AND a.symbol_raw = ? "
            "AND a.valid_from_ns <= ? AND (a.valid_to_ns IS NULL OR a.valid_to_ns > ?) "
            "ORDER BY a.valid_from_ns DESC LIMIT 1",
            (source, raw, at_ns, at_ns),
        )

    def by_isin(self, isin: str) -> list[Listing]:
        return self._many(f"{_SELECT_LISTINGS} WHERE i.isin = ? ORDER BY l.symbol", (isin,))

    def listings(
        self,
        mic: str | None = None,
        country: str | None = None,
        status: ListingStatus | None = None,
    ) -> list[Listing]:
        clauses: list[str] = []
        params: list[object] = []
        if mic is not None:
            clauses.append("l.mic = ?")
            params.append(self._mics.operating(mic))
        if country is not None:
            markets = [info.mic for info in self._exchanges.by_country(country)]
            if not markets:
                return []
            clauses.append(f"l.mic IN ({','.join('?' * len(markets))})")
            params.extend(markets)
        if status is not None:
            clauses.append("l.status = ?")
            params.append(status.value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._many(f"{_SELECT_LISTINGS}{where} ORDER BY l.symbol", tuple(params))

    def events(self, symbol: str) -> list[ListingEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT e.kind, e.old, e.new, e.observed_ns, e.source "
                "FROM listing_events e JOIN listings l ON l.listing_id = e.listing_id "
                "WHERE l.symbol = ? ORDER BY e.rowid",
                (symbol,),
            ).fetchall()
        return [
            ListingEvent(
                kind=ListingEventKind(_text(row["kind"])),
                old=_opt_text(row["old"]),
                new=_opt_text(row["new"]),
                observed_ns=_int(row["observed_ns"]),
                source=_text(row["source"]),
            )
            for row in rows
        ]

    def aliases(self, symbol: str) -> list[SymbolAlias]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT a.source, a.symbol_raw, a.valid_from_ns, a.valid_to_ns "
                "FROM symbol_aliases a JOIN listings l ON l.listing_id = a.listing_id "
                "WHERE l.symbol = ? ORDER BY a.valid_from_ns, a.source, a.symbol_raw",
                (symbol,),
            ).fetchall()
        return [
            SymbolAlias(
                source=_text(row["source"]),
                symbol_raw=_text(row["symbol_raw"]),
                valid_from_ns=_int(row["valid_from_ns"]),
                valid_to_ns=_opt_int(row["valid_to_ns"]),
            )
            for row in rows
        ]

    def _one(self, sql: str, params: Sequence[object]) -> Listing | None:
        with self._lock:
            return _one_listing(self._conn, sql, params)

    def _many(self, sql: str, params: Sequence[object]) -> list[Listing]:
        with self._lock:
            return _many_listings(self._conn, sql, params)


# ------------------------------------------------------------ write helpers


def _insert_listing(conn: sqlite3.Connection, plan: _Plan, obs: ListingObservation) -> int:
    instrument_id: int | None = None
    if plan.isin is not None:
        found = conn.execute(
            "SELECT instrument_id FROM instruments WHERE isin = ?", (plan.isin,)
        ).fetchone()
        if found is not None:
            instrument_id = _int(found["instrument_id"])
    if instrument_id is None:
        cursor = conn.execute("INSERT INTO instruments (isin) VALUES (?)", (plan.isin,))
        instrument_id = _rowid(cursor)
    _fill_from_observation(conn, instrument_id, obs)
    return _rowid(
        conn.execute(
            "INSERT INTO listings (symbol, mic, segment_mic, local_symbol, instrument_id, "
            "currency, price_scale, status, listed_date, delisted_date, first_seen_ns, "
            "last_seen_ns) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan.symbol,
                plan.operating,
                plan.segment_mic,
                plan.local,
                instrument_id,
                plan.currency,
                plan.price_scale,
                obs.status.value,
                plan.listed_date,
                plan.delisted_date,
                obs.observed_ns,
                obs.observed_ns,
            ),
        )
    )


def _merge_listing(
    conn: sqlite3.Connection,
    existing: Listing,
    plan: _Plan,
    obs: ListingObservation,
) -> None:
    newer = obs.observed_ns >= existing.last_seen_ns
    instrument_id = existing.instrument_id
    if plan.isin is not None and existing.isin is None:
        instrument_id = _adopt_isin(conn, existing.listing_id, instrument_id, plan.isin)
    _fill_from_observation(conn, instrument_id, obs)

    currency, price_scale = existing.currency, existing.price_scale
    if plan.currency is not None and plan.price_scale is not None:
        if existing.currency is None or existing.price_scale is None:
            currency, price_scale = plan.currency, plan.price_scale
        elif newer and (plan.currency, plan.price_scale) != (
            existing.currency,
            existing.price_scale,
        ):
            _write_event(
                conn,
                existing.listing_id,
                ListingEventKind.CURRENCY_CHANGE,
                old=f"{existing.currency}/{existing.price_scale}",
                new=f"{plan.currency}/{plan.price_scale}",
                observed_ns=obs.observed_ns,
                source=obs.source,
            )
            currency, price_scale = plan.currency, plan.price_scale

    status = existing.status
    if newer and obs.status is not existing.status:
        _write_event(
            conn,
            existing.listing_id,
            ListingEventKind.STATUS_CHANGE,
            old=existing.status.value,
            new=obs.status.value,
            observed_ns=obs.observed_ns,
            source=obs.source,
        )
        status = obs.status

    segment = existing.segment_mic
    if (
        plan.segment_mic is not None
        and plan.segment_mic != existing.segment_mic
        and (existing.segment_mic is None or newer)
    ):
        segment = plan.segment_mic

    listed = existing.listed_date if existing.listed_date is not None else plan.listed_date
    delisted = existing.delisted_date if existing.delisted_date is not None else plan.delisted_date
    conn.execute(
        "UPDATE listings SET instrument_id = ?, currency = ?, price_scale = ?, status = ?, "
        "segment_mic = ?, listed_date = ?, delisted_date = ?, first_seen_ns = ?, "
        "last_seen_ns = ? WHERE listing_id = ?",
        (
            instrument_id,
            currency,
            price_scale,
            status.value,
            segment,
            listed,
            delisted,
            min(existing.first_seen_ns, obs.observed_ns),
            max(existing.last_seen_ns, obs.observed_ns),
            existing.listing_id,
        ),
    )


def _adopt_isin(
    conn: sqlite3.Connection,
    listing_id: int,
    instrument_id: int,
    isin: str,
) -> int:
    """Give a listing's ISIN-less instrument the ISIN, merging into an existing holder if any."""
    found = conn.execute("SELECT instrument_id FROM instruments WHERE isin = ?", (isin,)).fetchone()
    if found is None:
        conn.execute(
            "UPDATE instruments SET isin = ? WHERE instrument_id = ?", (isin, instrument_id)
        )
        return instrument_id
    target = _int(found["instrument_id"])
    old = conn.execute(
        "SELECT name, share_class_figi, security_type, issuer_id FROM instruments "
        "WHERE instrument_id = ?",
        (instrument_id,),
    ).fetchone()
    if old is None:
        raise StoreError(f"instrument {instrument_id} is missing")
    _fill_instrument(
        conn,
        target,
        name=_opt_text(old["name"]),
        share_class_figi=_opt_text(old["share_class_figi"]),
        security_type=_opt_text(old["security_type"]),
        issuer_id=_opt_int(old["issuer_id"]),
    )
    conn.execute("UPDATE listings SET instrument_id = ? WHERE listing_id = ?", (target, listing_id))
    conn.execute(
        "DELETE FROM instruments WHERE instrument_id = ? "
        "AND NOT EXISTS (SELECT 1 FROM listings WHERE instrument_id = ?)",
        (instrument_id, instrument_id),
    )
    return target


def _fill_from_observation(
    conn: sqlite3.Connection,
    instrument_id: int,
    obs: ListingObservation,
) -> None:
    issuer_id = _issuer_for(conn, obs) if obs.issuer_lei else None
    _fill_instrument(
        conn,
        instrument_id,
        name=obs.name,
        share_class_figi=obs.share_class_figi,
        security_type=None if obs.security_type is None else obs.security_type.value,
        issuer_id=issuer_id,
    )


def _fill_instrument(
    conn: sqlite3.Connection,
    instrument_id: int,
    *,
    name: str | None,
    share_class_figi: str | None,
    security_type: str | None,
    issuer_id: int | None,
) -> None:
    """Fill-only: a non-null value replaces a NULL (or 'unknown' security type), never more."""
    conn.execute(
        "UPDATE instruments SET "
        "name = COALESCE(name, ?), "
        "share_class_figi = COALESCE(share_class_figi, ?), "
        "issuer_id = COALESCE(issuer_id, ?), "
        "security_type = CASE WHEN security_type IS NULL OR security_type = ? "
        "THEN COALESCE(?, security_type) ELSE security_type END "
        "WHERE instrument_id = ?",
        (name, share_class_figi, issuer_id, _UNKNOWN, security_type, instrument_id),
    )


def _issuer_for(conn: sqlite3.Connection, obs: ListingObservation) -> int:
    """Find or create the issuer of an LEI. Name and country fill only a NULL."""
    conn.execute("INSERT OR IGNORE INTO issuers (lei) VALUES (?)", (obs.issuer_lei,))
    row = conn.execute("SELECT issuer_id FROM issuers WHERE lei = ?", (obs.issuer_lei,)).fetchone()
    if row is None:
        raise StoreError(f"issuer {obs.issuer_lei} is missing after insert")
    issuer_id = _int(row["issuer_id"])
    conn.execute(
        "UPDATE issuers SET legal_name = COALESCE(legal_name, ?), "
        "country = COALESCE(country, ?) WHERE issuer_id = ?",
        (obs.issuer_name, obs.issuer_country, issuer_id),
    )
    return issuer_id


def _write_aliases(
    conn: sqlite3.Connection,
    listing_id: int,
    pairs: Sequence[tuple[str, str]],
    observed_ns: int,
) -> None:
    """Open validity ranges for each spelling, closing the ones a newer observation replaces."""
    for source, raw in pairs:
        holder = conn.execute(
            "SELECT listing_id, valid_from_ns FROM symbol_aliases "
            "WHERE source = ? AND symbol_raw = ? AND valid_to_ns IS NULL",
            (source, raw),
        ).fetchone()
        if holder is not None:
            holder_id = _int(holder["listing_id"])
            if holder_id == listing_id:
                continue
            if observed_ns <= _int(holder["valid_from_ns"]):
                continue
            conn.execute(
                "UPDATE symbol_aliases SET valid_to_ns = ? "
                "WHERE source = ? AND symbol_raw = ? AND valid_to_ns IS NULL",
                (observed_ns, source, raw),
            )
            _write_event(
                conn,
                holder_id,
                ListingEventKind.ALIAS_CHANGE,
                old=raw,
                new=None,
                observed_ns=observed_ns,
                source=source,
            )

        current = conn.execute(
            "SELECT symbol_raw, valid_from_ns FROM symbol_aliases "
            "WHERE listing_id = ? AND source = ? AND valid_to_ns IS NULL",
            (listing_id, source),
        ).fetchone()
        if current is not None and _text(current["symbol_raw"]) != raw:
            if observed_ns <= _int(current["valid_from_ns"]):
                continue
            previous = _text(current["symbol_raw"])
            conn.execute(
                "UPDATE symbol_aliases SET valid_to_ns = ? "
                "WHERE listing_id = ? AND source = ? AND valid_to_ns IS NULL",
                (observed_ns, listing_id, source),
            )
            _write_event(
                conn,
                listing_id,
                ListingEventKind.ALIAS_CHANGE,
                old=previous,
                new=raw,
                observed_ns=observed_ns,
                source=source,
            )

        conn.execute(
            "INSERT INTO symbol_aliases (source, symbol_raw, valid_from_ns, listing_id, "
            "valid_to_ns) VALUES (?, ?, ?, ?, NULL)",
            (source, raw, observed_ns, listing_id),
        )


def _write_event(
    conn: sqlite3.Connection,
    listing_id: int,
    kind: ListingEventKind,
    *,
    old: str | None,
    new: str | None,
    observed_ns: int,
    source: str,
) -> None:
    conn.execute(
        "INSERT INTO listing_events (listing_id, kind, old, new, observed_ns, source) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (listing_id, kind.value, old, new, observed_ns, source),
    )


# ------------------------------------------------------------ read helpers


def _one_listing(conn: sqlite3.Connection, sql: str, params: Sequence[object]) -> Listing | None:
    row = conn.execute(sql, tuple(params)).fetchone()
    return None if row is None else _listing_from_row(row)


def _many_listings(conn: sqlite3.Connection, sql: str, params: Sequence[object]) -> list[Listing]:
    return [_listing_from_row(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def _listing_from_row(row: sqlite3.Row) -> Listing:
    return Listing(
        listing_id=_int(row["listing_id"]),
        symbol=_text(row["symbol"]),
        mic=_text(row["mic"]),
        segment_mic=_opt_text(row["segment_mic"]),
        local_symbol=_text(row["local_symbol"]),
        instrument_id=_int(row["instrument_id"]),
        currency=_opt_text(row["currency"]),
        price_scale=_opt_int(row["price_scale"]),
        status=ListingStatus(_text(row["status"])),
        listed_date=_opt_text(row["listed_date"]),
        delisted_date=_opt_text(row["delisted_date"]),
        first_seen_ns=_int(row["first_seen_ns"]),
        last_seen_ns=_int(row["last_seen_ns"]),
        isin=_opt_text(row["isin"]),
        name=_opt_text(row["name"]),
        security_type=_security_type(_opt_text(row["security_type"])),
    )


def _security_type(value: str | None) -> SecurityType | None:
    if value is None:
        return None
    try:
        return SecurityType(value)
    except ValueError:
        return SecurityType.UNKNOWN


# ------------------------------------------------------------ validation


def _check_currency(currency: str | None, price_scale: int | None) -> tuple[str | None, int | None]:
    if (currency is None) != (price_scale is None):
        raise ValueError("currency and price_scale are both set or both None")
    if currency is None or price_scale is None:
        return None, None
    code = require_currency(currency)
    if _not_a_nonnegative_int(price_scale):
        raise ValueError(f"price_scale must be an int >= 0, got {price_scale!r}")
    return code, price_scale


def _check_date(label: str, value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) != 10:
        raise ValueError(f"{label} must be YYYY-MM-DD, got {value!r}")
    date.fromisoformat(value)
    return value


def _isin_is_valid(isin: str) -> bool:
    """ISO 6166 check: expand letters to base-36 digits, then Luhn over the whole string."""
    if len(isin) != 12 or _ISIN_SHAPE.fullmatch(isin) is None:
        return False
    digits = "".join(str(int(char, 36)) for char in isin[:-1]) + isin[-1]
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _not_a_nonnegative_int(value: object) -> bool:
    return isinstance(value, bool) or not isinstance(value, int) or value < 0


# ------------------------------------------------------------ row conversion


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise StoreError(f"listing master holds a non-text value: {value!r}")
    return value


def _opt_text(value: Any) -> str | None:
    return None if value is None else _text(value)


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StoreError(f"listing master holds a non-integer value: {value!r}")
    return value


def _opt_int(value: Any) -> int | None:
    return None if value is None else _int(value)


def _rowid(cursor: sqlite3.Cursor) -> int:
    rowid = cursor.lastrowid
    if rowid is None:
        raise StoreError("listing master insert returned no row id")
    return rowid
