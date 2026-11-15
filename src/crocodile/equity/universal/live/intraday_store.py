"""Single-writer SQLite minutes with atomic content-addressed raw activation.

Raw data is durable before the SQLite transaction references it. A crash between
those steps leaves only an unreferenced file, never a partial visible revision.
All public methods serialize on one lock; errors roll back the entire import.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from types import TracebackType

from .event_time_bars import parse_file, reduce_minute
from .intraday_models import (
    CONTRACTS,
    BarEnvelope,
    FileReference,
    IngestResult,
    MinuteKey,
    Observation,
    file_minute_ns,
)


class StoreError(RuntimeError):
    """A failed operation did not activate a partial file/bar revision."""


def _serialize(value: Observation | BarEnvelope) -> str:
    return json.dumps(asdict(value), default=str, sort_keys=True, separators=(",", ":"))


def _observation(payload: str) -> Observation:
    data = json.loads(payload)
    for field in ("price", "quantity"):
        data[field] = Decimal(data[field]) if data[field] is not None else None
    data["quality_reasons"] = tuple(data["quality_reasons"])
    return Observation(**data)


def _bar(payload: str) -> BarEnvelope:
    data = json.loads(payload)
    for field in ("open", "high", "low", "close", "volume", "vwap"):
        data[field] = Decimal(data[field]) if data[field] is not None else None
    data["quality_reasons"] = tuple(data["quality_reasons"])
    data["files"] = tuple(FileReference(**item) for item in data["files"])
    return BarEnvelope(**data)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS active_files (
    name TEXT PRIMARY KEY, sha TEXT NOT NULL, raw_path TEXT NOT NULL,
    captured_ns INTEGER NOT NULL, row_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    name TEXT NOT NULL, sha TEXT NOT NULL, raw_path TEXT NOT NULL,
    captured_ns INTEGER NOT NULL, PRIMARY KEY(name,sha)
);
CREATE TABLE IF NOT EXISTS observations (
    file_name TEXT NOT NULL, row_number INTEGER NOT NULL, isin TEXT NOT NULL,
    minute_ns INTEGER NOT NULL, venue TEXT NOT NULL, txid TEXT NOT NULL,
    payload_hash TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(file_name,row_number)
);
CREATE INDEX IF NOT EXISTS observations_minute ON observations(isin,minute_ns);
CREATE INDEX IF NOT EXISTS observations_trade_key ON observations(venue,isin,txid);
CREATE TABLE IF NOT EXISTS conflicts (
    venue TEXT NOT NULL, isin TEXT NOT NULL, txid TEXT NOT NULL,
    PRIMARY KEY(venue,isin,txid)
);
CREATE TABLE IF NOT EXISTS bars (
    isin TEXT NOT NULL, minute_ns INTEGER NOT NULL, revision INTEGER NOT NULL,
    payload TEXT, PRIMARY KEY(isin,minute_ns)
);
"""


class IntradayStore:
    """Persistent two-contract store. Query intervals are [start_ns, end_ns).

    `before_ns` is exclusive and results are always ascending. No-data minutes
    are not fabricated. A removed minute is omitted from queries and returned
    explicitly in `IngestResult.removed_minutes` for live cache invalidation.
    """

    def __init__(self, root: Path, raw_budget_bytes: int = 2 * 1024**3) -> None:
        if raw_budget_bytes < 0:
            raise ValueError("raw_budget_bytes must be nonnegative")
        self.root = Path(root)
        self.raw_budget_bytes = raw_budget_bytes
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self._raw_root = self.root / "raw"
        self._raw_root.mkdir(exist_ok=True)
        self._connection = sqlite3.connect(self.root / "intraday.sqlite3", check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.executescript(_SCHEMA)
        self._closed = False

    def __enter__(self) -> IntradayStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _write_raw(self, raw: bytes, digest: str) -> Path:
        path = self._raw_root / (digest + ".json.gz")
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise StoreError("corrupt_raw_file")
            return path
        occupied = sum(p.stat().st_size for p in self._raw_root.iterdir() if p.is_file())
        if occupied + len(raw) > self.raw_budget_bytes:
            raise StoreError("storage_full")
        temporary = self._raw_root / ("." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def ingest_file(
        self,
        file_name: str,
        raw_gzip: bytes,
        *,
        captured_ns: int | None = None,
    ) -> IngestResult:
        file_minute_ns(file_name)
        captured = time.time_ns() if captured_ns is None else captured_ns
        digest = hashlib.sha256(raw_gzip).hexdigest()
        # Validation never mutates visible state and runs before budget/disk writes.
        rows = parse_file(file_name, raw_gzip, captured_ns=captured)
        with self._lock:
            try:
                current = self._connection.execute(
                    "SELECT sha,row_count,raw_path FROM active_files WHERE name=?",
                    (file_name,),
                ).fetchone()
                if current is not None and current[0] == digest:
                    # Verify durable evidence too; do not silently bless a corrupted archive.
                    if hashlib.sha256(Path(current[2]).read_bytes()).hexdigest() != digest:
                        raise StoreError("corrupt_raw_file")
                    return IngestResult(file_name, digest, False, current[1])
                raw_path = self._write_raw(raw_gzip, digest)
                changed: list[BarEnvelope] = []
                removed: list[MinuteKey] = []
                with self._connection:
                    self._connection.execute("BEGIN IMMEDIATE")
                    # Only changed transaction keys can create/resolve global ambiguity.
                    keys = {r.trade_key for r in rows}
                    keys.update(
                        self._connection.execute(
                            "SELECT venue,isin,txid FROM observations WHERE file_name=?",
                            (file_name,),
                        ).fetchall()
                    )
                    affected = {MinuteKey(r.isin, r.minute_start_ns) for r in rows}
                    affected.update(self._key_minutes(keys))
                    self._connection.execute(
                        "DELETE FROM observations WHERE file_name=?", (file_name,)
                    )
                    self._connection.executemany(
                        "INSERT INTO observations VALUES(?,?,?,?,?,?,?,?)",
                        [
                            (
                                r.file_name,
                                r.row_number,
                                r.isin,
                                r.minute_start_ns,
                                r.venue,
                                r.transaction_id,
                                r.payload_sha256,
                                _serialize(r),
                            )
                            for r in rows
                        ],
                    )
                    affected.update(self._key_minutes(keys))
                    for key in keys:
                        self._connection.execute(
                            "DELETE FROM conflicts WHERE venue=? AND isin=? AND txid=?",
                            key,
                        )
                        count = self._connection.execute(
                            "SELECT COUNT(DISTINCT payload_hash) FROM observations "
                            "WHERE venue=? AND isin=? AND txid=?",
                            key,
                        ).fetchone()[0]
                        if count > 1:
                            self._connection.execute("INSERT INTO conflicts VALUES(?,?,?)", key)
                    self._connection.execute(
                        "INSERT OR IGNORE INTO versions VALUES(?,?,?,?)",
                        (file_name, digest, str(raw_path), captured),
                    )
                    self._connection.execute(
                        "INSERT OR REPLACE INTO active_files VALUES(?,?,?,?,?)",
                        (file_name, digest, str(raw_path), captured, len(rows)),
                    )
                    for minute in sorted(affected):
                        bar = self._recompute(minute)
                        if bar is not None:
                            changed.append(bar)
                        else:
                            removed.append(minute)
                return IngestResult(
                    file_name, digest, True, len(rows), tuple(changed), tuple(removed)
                )
            except (OSError, sqlite3.Error) as error:
                raise StoreError("storage_operation_failed") from error

    def _key_minutes(self, keys: set[tuple[str, str, str]]) -> set[MinuteKey]:
        minutes: set[MinuteKey] = set()
        for key in keys:
            minutes.update(
                MinuteKey(*row)
                for row in self._connection.execute(
                    "SELECT DISTINCT isin,minute_ns FROM observations "
                    "WHERE venue=? AND isin=? AND txid=?",
                    key,
                )
            )
        return minutes

    def _recompute(self, minute: MinuteKey) -> BarEnvelope | None:
        args = (minute.isin, minute.minute_start_ns)
        previous = self._connection.execute(
            "SELECT revision FROM bars WHERE isin=? AND minute_ns=?",
            args,
        ).fetchone()
        revision = previous[0] + 1 if previous else 1
        rows = [
            _observation(r[0])
            for r in self._connection.execute(
                "SELECT payload FROM observations WHERE isin=? AND minute_ns=?",
                args,
            )
        ]
        if rows:
            conflicts = frozenset(
                self._connection.execute(
                    "SELECT DISTINCT c.venue,c.isin,c.txid FROM conflicts c "
                    "JOIN observations o USING(venue,isin,txid) WHERE o.isin=? AND o.minute_ns=?",
                    args,
                )
            )
            result = reduce_minute(*args, rows, ambiguous_keys=conflicts, revision=revision)
            payload = _serialize(result)
        else:
            result, payload = None, None
        self._connection.execute(
            "INSERT OR REPLACE INTO bars VALUES(?,?,?,?)",
            (*args, revision, payload),
        )
        return result

    def query_bars(
        self,
        isin: str,
        *,
        before_ns: int | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
        limit: int = 500,
    ) -> tuple[BarEnvelope, ...]:
        if isin not in CONTRACTS:
            raise ValueError("unsupported contract ISIN")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise ValueError("limit must be 1..10000")
        clauses = ["isin=?", "payload IS NOT NULL"]
        values: list[str | int] = [isin]
        for bound, operation in ((before_ns, "<"), (start_ns, ">="), (end_ns, "<")):
            if bound is not None:
                if not isinstance(bound, int) or isinstance(bound, bool):
                    raise ValueError("time bounds must be integer nanoseconds")
                clauses.append("minute_ns" + operation + "?")
                values.append(bound)
        # A backward page (``before_ns`` without ``start_ns``) is the newest ``limit`` minutes
        # below the cursor; every page is returned oldest first.
        backward = before_ns is not None and start_ns is None
        order = (
            " ORDER BY minute_ns DESC LIMIT ?" if backward else " ORDER BY minute_ns ASC LIMIT ?"
        )
        with self._lock:
            try:
                result = self._connection.execute(
                    "SELECT payload FROM bars WHERE " + " AND ".join(clauses) + order,
                    (*values, limit),
                ).fetchall()
                if backward:
                    result.reverse()
                return tuple(_bar(item[0]) for item in result)
            except (sqlite3.Error, ValueError, TypeError, KeyError) as error:
                raise StoreError("storage_query_failed") from error

    def completed_files(self) -> tuple[str, ...]:
        with self._lock:
            try:
                return tuple(
                    row[0]
                    for row in self._connection.execute(
                        "SELECT name FROM active_files ORDER BY name",
                    )
                )
            except sqlite3.Error as error:
                raise StoreError("storage_query_failed") from error

    def observation_count(self) -> int:
        with self._lock:
            try:
                return int(
                    self._connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
                )
            except sqlite3.Error as error:
                raise StoreError("storage_query_failed") from error

    def latest_event_ns(self) -> int | None:
        with self._lock:
            try:
                # Read only the newest event minute for each supported contract.
                latest = []
                for isin in CONTRACTS:
                    row = self._connection.execute(
                        "SELECT payload FROM bars WHERE isin=? AND payload IS NOT NULL "
                        "ORDER BY minute_ns DESC LIMIT 1",
                        (isin,),
                    ).fetchone()
                    if row:
                        latest.append(_bar(row[0]).latest_event_ns)
                return max(latest) if latest else None
            except (sqlite3.Error, ValueError, TypeError, KeyError) as error:
                raise StoreError("storage_query_failed") from error
