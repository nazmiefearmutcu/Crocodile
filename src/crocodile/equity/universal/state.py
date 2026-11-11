"""Durable watermarks for universal runs: what each source already covers, and the last EOD marker.

One SQLite file per data directory (``<data_dir>/state/universal.sqlite``). A watermark is
written only after the records it describes were flushed to the lake (the orchestrator's
checkpoint), so an interrupted backfill resumes from the stored coverage and refetches nothing
it already holds. Every write commits immediately.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import datetime
import logging
import sqlite3
from pathlib import Path
from typing import Final

import msgspec

from crocodile.core.errors import StoreError

logger = logging.getLogger(__name__)

STATE_SCHEMA_VERSION: Final = 1
NS_PER_DAY: Final = 86_400_000_000_000
_MEMORY: Final = ":memory:"
_DAILY_INTERVALS: Final = frozenset({"1d", ""})
_STATE_DIR: Final = "state"
_STATE_FILE: Final = "universal.sqlite"

_DDL: Final = (
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS coverage (
        source TEXT NOT NULL,
        symbol TEXT NOT NULL,
        channel TEXT NOT NULL,
        interval TEXT NOT NULL,
        mic TEXT NOT NULL,
        covered_from_ns INTEGER,
        covered_until_ns INTEGER,
        status TEXT NOT NULL,
        reason TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        updated_ns INTEGER NOT NULL,
        last_success_ns INTEGER,
        PRIMARY KEY (source, symbol, channel, interval)
    )""",
    """CREATE TABLE IF NOT EXISTS eod (
        source TEXT NOT NULL,
        mic TEXT NOT NULL,
        session TEXT NOT NULL,
        status TEXT NOT NULL,
        updated_ns INTEGER NOT NULL,
        PRIMARY KEY (source, mic)
    )""",
)


class Coverage(msgspec.Struct, frozen=True, kw_only=True):
    source: str
    symbol: str
    mic: str
    channel: str
    interval: str
    covered_from_ns: int | None
    covered_until_ns: int | None
    status: str
    reason: str | None
    attempts: int
    updated_ns: int
    last_success_ns: int | None


class EodMarker(msgspec.Struct, frozen=True, kw_only=True):
    session: datetime.date
    status: str
    updated_ns: int


class UniversalState:
    """The watermark and EOD-marker store. Not shared between event loops."""

    def __init__(self, path: Path | str = _MEMORY) -> None:
        self._path = str(path)
        if self._path != _MEMORY:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        if self._path != _MEMORY:
            self._conn.execute("PRAGMA journal_mode=WAL")
        try:
            self._init_schema()
        except BaseException:
            self._conn.close()
            raise

    @staticmethod
    def path_for(data_dir: Path) -> Path:
        return data_dir / _STATE_DIR / _STATE_FILE

    @classmethod
    def at(cls, data_dir: Path) -> UniversalState:
        return cls(cls.path_for(data_dir))

    def _init_schema(self) -> None:
        for statement in _DDL:
            self._conn.execute(statement)
        rows = self._conn.execute("SELECT version FROM schema_version").fetchall()
        if not rows:
            self._conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (STATE_SCHEMA_VERSION,)
            )
        else:
            stored = int(rows[0][0])
            if stored > STATE_SCHEMA_VERSION:
                raise RuntimeError(
                    f"universal state schema version {stored} is newer than this build's "
                    f"{STATE_SCHEMA_VERSION}; upgrade crocodile before reading it"
                )
        self._conn.commit()

    def coverage(self, source: str, symbol: str, channel: str, interval: str) -> Coverage | None:
        row = self._conn.execute(
            "SELECT mic, covered_from_ns, covered_until_ns, status, reason, attempts, updated_ns, "
            "last_success_ns FROM coverage WHERE source = ? AND symbol = ? AND channel = ? "
            "AND interval = ?",
            (source, symbol, channel, interval),
        ).fetchone()
        if row is None:
            return None
        return Coverage(
            source=source,
            symbol=symbol,
            mic=row[0],
            channel=channel,
            interval=interval,
            covered_from_ns=row[1],
            covered_until_ns=row[2],
            status=row[3],
            reason=row[4],
            attempts=int(row[5]),
            updated_ns=int(row[6]),
            last_success_ns=row[7],
        )

    def record_success(
        self,
        *,
        source: str,
        symbol: str,
        mic: str,
        channel: str,
        interval: str,
        covered_from_ns: int,
        covered_until_ns: int,
        at_ns: int,
    ) -> Coverage:
        current = self.coverage(source, symbol, channel, interval)
        if current is None or current.covered_from_ns is None or current.covered_until_ns is None:
            lo, hi = covered_from_ns, covered_until_ns
        elif interval in _DAILY_INTERVALS:
            lo, hi = _union_daily(
                current.covered_from_ns,
                current.covered_until_ns,
                covered_from_ns,
                covered_until_ns,
                symbol=symbol,
                channel=channel,
            )
        else:
            lo = min(current.covered_from_ns, covered_from_ns)
            hi = max(current.covered_until_ns, covered_until_ns)
        self._conn.execute(
            "INSERT INTO coverage (source, symbol, channel, interval, mic, covered_from_ns, "
            "covered_until_ns, status, reason, attempts, updated_ns, last_success_ns) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'ok', NULL, 0, ?, ?) "
            "ON CONFLICT (source, symbol, channel, interval) DO UPDATE SET "
            "mic = excluded.mic, covered_from_ns = excluded.covered_from_ns, "
            "covered_until_ns = excluded.covered_until_ns, status = 'ok', reason = NULL, "
            "updated_ns = excluded.updated_ns, last_success_ns = excluded.last_success_ns",
            (source, symbol, channel, interval, mic, lo, hi, at_ns, at_ns),
        )
        self._conn.commit()
        stored = self.coverage(source, symbol, channel, interval)
        if stored is None:  # pragma: no cover - the upsert above always leaves a row
            raise StoreError(f"coverage row for {symbol} {channel} vanished after its write")
        return stored

    def record_outcome(
        self,
        *,
        source: str,
        symbol: str,
        mic: str,
        channel: str,
        interval: str,
        status: str,
        reason: str | None,
        at_ns: int,
    ) -> None:
        self._conn.execute(
            "INSERT INTO coverage (source, symbol, channel, interval, mic, covered_from_ns, "
            "covered_until_ns, status, reason, attempts, updated_ns, last_success_ns) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, 1, ?, NULL) "
            "ON CONFLICT (source, symbol, channel, interval) DO UPDATE SET "
            "mic = excluded.mic, status = excluded.status, reason = excluded.reason, "
            "attempts = coverage.attempts + 1, updated_ns = excluded.updated_ns",
            (source, symbol, channel, interval, mic, status, reason, at_ns),
        )
        self._conn.commit()

    def eod_session(self, source: str, mic: str) -> datetime.date | None:
        marker = self.eod_marker(source, mic)
        return None if marker is None else marker.session

    def eod_marker(self, source: str, mic: str) -> EodMarker | None:
        row = self._conn.execute(
            "SELECT session, status, updated_ns FROM eod WHERE source = ? AND mic = ?",
            (source, mic),
        ).fetchone()
        if row is None:
            return None
        return EodMarker(
            session=datetime.date.fromisoformat(row[0]),
            status=row[1],
            updated_ns=int(row[2]),
        )

    def record_eod(
        self,
        *,
        source: str,
        mic: str,
        session: datetime.date,
        at_ns: int,
        status: str = "ok",
    ) -> None:
        self._conn.execute(
            "INSERT INTO eod (source, mic, session, status, updated_ns) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (source, mic) DO UPDATE SET session = excluded.session, "
            "status = excluded.status, updated_ns = excluded.updated_ns",
            (source, mic, session.isoformat(), status, at_ns),
        )
        self._conn.commit()

    def last_success_by_mic(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT mic, MAX(v) FROM ("
            "SELECT mic, last_success_ns AS v FROM coverage WHERE last_success_ns IS NOT NULL "
            "UNION ALL SELECT mic, updated_ns AS v FROM eod"
            ") GROUP BY mic"
        ).fetchall()
        return {str(mic): int(value) for mic, value in rows}

    def close(self) -> None:
        self._conn.close()


def _union_daily(
    stored_lo: int,
    stored_hi: int,
    new_lo: int,
    new_hi: int,
    *,
    symbol: str,
    channel: str,
) -> tuple[int, int]:
    """Merge a daily success into stored coverage when the ranges touch or overlap."""
    if new_lo <= stored_hi + NS_PER_DAY and new_hi >= stored_lo - NS_PER_DAY:
        return min(stored_lo, new_lo), max(stored_hi, new_hi)
    logger.warning(
        "disjoint success for %s %s: kept the stored range, the gap stays uncovered and "
        "will be refetched",
        symbol,
        channel,
    )
    return stored_lo, stored_hi


def missing_ranges(
    req_from: int,
    req_to: int,
    covered: tuple[int, int] | None,
    *,
    step: int = NS_PER_DAY,
) -> list[tuple[int, int]]:
    """The parts of ``[req_from, req_to]`` that lie outside ``covered``, ascending.

    The head ends one ``step`` before the covered start and the tail begins one ``step`` after
    the covered end, so a request that touches coverage is not refetched.
    """
    if req_to < req_from:
        return []
    if covered is None:
        return [(req_from, req_to)]
    lo, hi = covered
    gaps: list[tuple[int, int]] = []
    head_to = min(req_to, lo - step)
    if req_from <= head_to:
        gaps.append((req_from, head_to))
    tail_from = max(req_from, hi + step)
    if tail_from <= req_to:
        gaps.append((tail_from, req_to))
    return gaps
