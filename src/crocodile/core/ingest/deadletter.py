"""Dead-letter queue for unparseable / normalize-failed frames.

Items land here from ``Connector.run()`` so the supervised loop can continue.
On collect stop, callers drain the queue and optionally write a JSON report.

Why ``put`` is sync-but-awaitable
---------------------------------
The two forks shipped the same idea over different plumbing, and both spellings
are pinned by tests that cannot both be satisfied by a coroutine:

* Crypto's ``DeadLetterQueue.put`` was ``async``. ``crocodile.core.connector``
  awaits it from inside the supervised read loop, and ``tests/ingest/
  test_deadletter.py`` / ``tests/exchanges/test_base.py`` call
  ``await dlq.put(...)`` throughout.
* Equity's was synchronous, guarded by a ``threading.Lock``, and optionally
  backed by a SQLite file so dead letters survived a crash. ``tests/equity/
  test_ingest.py`` calls ``dlq.put(...)`` bare and then asserts on ``drain()``
  in the same synchronous test function.

Making ``put`` ``async`` loses the equity tests (the bare call returns an
un-awaited coroutine, nothing is enqueued, and ``drain()`` sees zero items).
Making it plain-sync loses the crypto call sites (``await None`` is a
``TypeError``). So ``put`` does its work synchronously and returns an
already-completed awaitable: the bare call enqueues and discards a cheap
sentinel, and ``await`` on it resolves immediately to ``None``. One class,
both contracts, no ``coroutine was never awaited`` warning either way.

The durable store and the ``max_size`` floor are equity's, kept because they
are real capability: ``db_path=None`` (the default, and what every crypto call
site uses) leaves the hot path a bare ``deque.append`` with no I/O, so crypto
behaviour is byte-for-byte what it was. Only a caller that opts into
``db_path`` pays the blocking SQLite write, which is why that write is not
offloaded to a thread — the fork it comes from ran it inline too, and moving it
would change the durability guarantee (a row is on disk before ``put``
returns).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from collections import deque
from collections.abc import Awaitable, Generator, Sequence
from pathlib import Path
from typing import Any, Final

import msgspec

log = logging.getLogger(__name__)

# Default filename when writing under data_dir without an explicit path.
DEFAULT_DLQ_REPORT_NAME = "dlq_report.json"

# Directory under the lake root that holds one durable dead-letter file per source (spec 3.9).
DLQ_DIR_NAME: Final = "dlq"
_SOURCE_NAME = re.compile(r"[a-z0-9][a-z0-9_]*")


def dlq_db_path(data_dir: Path | str, source: str) -> Path:
    """``<data_dir>/dlq/<source>.sqlite``: one durable dead-letter file per source per lake.

    Composed like ``default_listings_path`` and the report file of ``report_drained_dlqs``;
    it never reads the environment. ``ValueError`` unless ``source`` is a provider name
    (``^[a-z0-9][a-z0-9_]*$``), so a name can never step outside the directory.
    """
    if not _SOURCE_NAME.fullmatch(source):
        raise ValueError(
            f"dead-letter source {source!r} must match [a-z0-9][a-z0-9_]* (a provider name)"
        )
    return Path(data_dir) / DLQ_DIR_NAME / f"{source}.sqlite"


class DeadLetter(msgspec.Struct, frozen=True):
    local_ts: int
    raw: bytes
    error_type: str
    traceback: str


class _Enqueued:
    """Already-complete awaitable returned by :meth:`DeadLetterQueue.put`.

    Exists so one ``put`` satisfies both fork's call conventions; see the module
    docstring. Awaiting it never suspends, and dropping it costs nothing and
    warns about nothing.
    """

    __slots__ = ()

    def __await__(self) -> Generator[Any, None, None]:
        yield from ()


_ENQUEUED = _Enqueued()


class DeadLetterQueue:
    """Bounded FIFO of dead letters, optionally mirrored into SQLite.

    Args:
        max_size: Maximum retained letters; the oldest is evicted past it.
        db_path: When given, letters are also appended to a SQLite file at this
            path and reloaded from it on construction, so a crashed collect run
            does not lose its dead letters. ``None`` keeps the queue purely
            in-memory.

    Raises:
        ValueError: If *max_size* is below 1. A zero-size queue silently
            discards everything it is handed, which is the one behaviour a
            dead-letter queue must never have.
    """

    def __init__(self, max_size: int = 10_000, db_path: str | None = None) -> None:
        if max_size < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size}")
        self._dq: deque[DeadLetter] = deque(maxlen=max_size)
        self.db_path = db_path
        self._lock = threading.Lock()
        if self.db_path is not None:
            self._init_db()

    def __len__(self) -> int:
        return len(self._dq)

    # ------------------------------------------------------------------
    # Durable store
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        assert self.db_path is not None
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        """Create the table if absent and reload the newest ``max_size`` rows."""
        if self.db_path is None:
            return
        try:
            # A durable queue under a lake that has no dlq/ directory yet creates it here;
            # without this the connect below fails and the queue silently stays in memory.
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS dead_letters (
                        local_ts INTEGER,
                        raw BLOB,
                        error_type TEXT,
                        traceback TEXT
                    )
                    """
                )
                # No index on rowid: SQLite refuses to name it in CREATE INDEX ("no such
                # column: rowid"), which aborted every reload and left durable queues empty.
                # rowid is the table's implicit key, so ORDER BY rowid needs no index.
                conn.commit()
                maxlen = self._dq.maxlen
                limit = maxlen if maxlen is not None else -1
                cursor = conn.execute(
                    "SELECT local_ts, raw, error_type, traceback "
                    "FROM dead_letters ORDER BY rowid DESC LIMIT ?",
                    (limit,),
                )
                rows = cursor.fetchall()
                for local_ts, raw, error_type, traceback in reversed(rows):
                    self._dq.append(
                        DeadLetter(
                            local_ts=local_ts,
                            raw=raw,
                            error_type=error_type,
                            traceback=traceback,
                        )
                    )
                self._trim_db(conn, maxlen)
                conn.commit()
        except Exception as exc:
            # A broken durable store must not take the collect loop with it.
            log.error("Failed to initialize or load from SQLite dead letter queue: %s", exc)

    @staticmethod
    def _trim_db(conn: sqlite3.Connection, maxlen: int | None) -> None:
        """Delete the oldest rows so the file honours the same cap as the deque."""
        if maxlen is None:
            return
        count = conn.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0]
        excess = count - maxlen
        if excess > 0:
            conn.execute(
                "DELETE FROM dead_letters WHERE rowid IN ("
                "SELECT rowid FROM dead_letters ORDER BY rowid ASC LIMIT ?"
                ")",
                (excess,),
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def put(self, local_ts: int, raw: bytes, error_type: str, traceback: str) -> Awaitable[None]:
        """Enqueue one dead letter, evicting the oldest if the queue is full.

        The work is done before this returns. The return value is an
        already-complete awaitable purely so ``await dlq.put(...)`` — how every
        crypto call site spells it — keeps working; discarding it is equally
        correct. See the module docstring.
        """
        with self._lock:
            maxlen = self._dq.maxlen
            if maxlen is not None and len(self._dq) >= maxlen and self._dq:
                oldest = self._dq[0]
                log.warning(
                    "DeadLetterQueue overflow. Evicting oldest dead letter: "
                    "local_ts=%d, error_type=%s",
                    oldest.local_ts,
                    oldest.error_type,
                )

            self._dq.append(
                DeadLetter(local_ts=local_ts, raw=raw, error_type=error_type, traceback=traceback)
            )

            if self.db_path is not None:
                try:
                    with self._connect() as conn:
                        conn.execute(
                            "INSERT INTO dead_letters "
                            "(local_ts, raw, error_type, traceback) VALUES (?, ?, ?, ?)",
                            (local_ts, raw, error_type, traceback),
                        )
                        self._trim_db(conn, maxlen)
                        conn.commit()
                except Exception as exc:
                    log.error("Failed to write dead letter to SQLite: %s", exc)

        return _ENQUEUED

    def drain(self) -> list[DeadLetter]:
        """Return all queued items and clear the queue.

        When a durable store is configured it is cleared first: if that fails,
        the in-memory queue is left intact so the letters are not lost from both
        places at once, and the next drain replays them rather than the caller
        seeing them twice.
        """
        with self._lock:
            items = list(self._dq)
            if self.db_path is not None:
                try:
                    with self._connect() as conn:
                        conn.execute("DELETE FROM dead_letters")
                        conn.commit()
                except Exception as exc:
                    log.error(
                        "Failed to clear dead letters from SQLite: %s "
                        "(memory drain aborted to avoid duplicate replay)",
                        exc,
                    )
                    return items
            self._dq.clear()
            return items


def dead_letter_to_dict(item: DeadLetter, *, connector: str | None = None) -> dict[str, Any]:
    """Serialize one dead letter for JSON report output."""
    out: dict[str, Any] = {
        "local_ts": item.local_ts,
        "raw": item.raw.decode("utf-8", errors="replace"),
        "error_type": item.error_type,
        "traceback": item.traceback,
    }
    if connector is not None:
        out["connector"] = connector
    return out


def build_dlq_report(
    entries: Sequence[tuple[str, DeadLetter]] | Sequence[DeadLetter],
) -> dict[str, Any]:
    """Build a JSON-serializable DLQ report.

    *entries* may be bare ``DeadLetter`` items or ``(connector_name, item)`` pairs.
    """
    items: list[dict[str, Any]] = []
    by_error: dict[str, int] = {}
    for entry in entries:
        if isinstance(entry, DeadLetter):
            d = dead_letter_to_dict(entry)
        else:
            name, letter = entry
            d = dead_letter_to_dict(letter, connector=name)
        items.append(d)
        et = d["error_type"]
        by_error[et] = by_error.get(et, 0) + 1
    return {
        "count": len(items),
        "by_error_type": by_error,
        "items": items,
    }


def write_dlq_report(path: Path | str, report: dict[str, Any]) -> Path:
    """Write *report* as JSON to *path*. Creates parent directories as needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return p


def drain_connector_dlqs(
    connectors: Sequence[Any],
) -> list[tuple[str, DeadLetter]]:
    """Drain ``_dlq`` from each connector; return ``(name, item)`` pairs."""
    out: list[tuple[str, DeadLetter]] = []
    for conn in connectors:
        dlq = getattr(conn, "_dlq", None)
        if dlq is None or not hasattr(dlq, "drain"):
            continue
        name = str(getattr(conn, "name", type(conn).__name__))
        for item in dlq.drain():
            out.append((name, item))
    return out


def report_drained_dlqs(
    connectors: Sequence[Any],
    *,
    report_path: Path | str | None = None,
    data_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Drain all connector DLQs; write a JSON report if non-empty.

    Resolution for the report file when *count > 0*:
    1. Explicit *report_path* if given
    2. Else ``{data_dir}/dlq_report.json`` if *data_dir* is given
    3. Else no file is written (summary only via return value / log)

    Returns the report dict (``count == 0`` and empty items when nothing drained).
    """
    entries = drain_connector_dlqs(connectors)
    report = build_dlq_report(entries)
    count = report["count"]
    if count == 0:
        return report

    dest: Path | None = None
    if report_path is not None:
        dest = Path(report_path)
    elif data_dir is not None:
        dest = Path(data_dir) / DEFAULT_DLQ_REPORT_NAME

    if dest is not None:
        written = write_dlq_report(dest, report)
        report["path"] = str(written)
        log.warning(
            "DLQ drained %d item(s); report written to %s",
            count,
            written,
        )
    else:
        log.warning(
            "DLQ drained %d item(s) (no report path/data_dir; not written to disk)",
            count,
        )
    return report
