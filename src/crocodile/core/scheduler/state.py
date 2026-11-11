"""State management for the scheduled pull coordinator.

Besides the last successful run, the store keeps how often each (task, logical date) has
failed and the gap entries written when the coordinator gave up on a date (spec 3.9). Both
survive a restart, so the failure budget of a date is not reset by restarting the process.
"""

import datetime
import os
from abc import ABC, abstractmethod
from typing import Literal

import msgspec

# "no_data" when the task's calendar answers the logical date through the weekday fallback
# (spec 3.8: a missing day there may simply be a holiday), otherwise "gap".
GapKind = Literal["gap", "no_data"]


class FailureRecord(msgspec.Struct, frozen=True):
    """How often one logical date of one task has failed, and the last reason."""

    count: int
    last_failure: datetime.datetime  # aware
    last_error: str  # f"{type(exc).__name__}: {exc}"


class GapEntry(msgspec.Struct, frozen=True):
    """A logical date the scheduler gave up on and stepped past (spec 3.9)."""

    task: str
    logical_date: datetime.date
    reason: str
    failures: int
    recorded_at: datetime.datetime  # aware
    kind: GapKind = "gap"


class TaskStateRecord(msgspec.Struct):
    """Serialization model for task state."""

    last_run_timestamp: str | None = None  # ISO format datetime
    last_run_date: str | None = None  # ISO format date
    # Keyed by the ISO logical date. A state file written before failures were persisted
    # decodes with both fields empty.
    failures: dict[str, FailureRecord] = msgspec.field(default_factory=dict)
    gaps: list[GapEntry] = msgspec.field(default_factory=list)


class SchedulerStateStore(ABC):
    """Abstract base class for storing scheduler execution state.

    A store written outside this repository must implement every abstract method; the
    failure and gap methods were added with per-exchange calendars (spec 3.9).
    """

    @abstractmethod
    def get_last_run(self, task_name: str) -> tuple[datetime.datetime | None, datetime.date | None]:
        """Retrieve (last_run_timestamp, last_run_date) for a task."""

    @abstractmethod
    def update_last_run(
        self, task_name: str, run_time: datetime.datetime, run_date: datetime.date
    ) -> None:
        """Persist the last run details for a task."""

    @abstractmethod
    def get_failure(self, task_name: str, logical_date: datetime.date) -> FailureRecord | None:
        """The failure record of one logical date of one task, or None."""

    @abstractmethod
    def record_failure(
        self,
        task_name: str,
        logical_date: datetime.date,
        at: datetime.datetime,
        error: str,
    ) -> FailureRecord:
        """Count one more failure of a logical date and return the incremented record."""

    @abstractmethod
    def clear_failure(self, task_name: str, logical_date: datetime.date) -> None:
        """Forget the failures of a logical date (after it succeeded or became a gap)."""

    @abstractmethod
    def record_gap(self, entry: GapEntry) -> None:
        """Persist a gap entry."""

    @abstractmethod
    def gaps(self, task_name: str | None = None) -> list[GapEntry]:
        """Gap entries for one task, or for every task; sorted by (task, logical date)."""


def _sorted_gaps(entries: list[GapEntry]) -> list[GapEntry]:
    return sorted(entries, key=lambda g: (g.task, g.logical_date, g.recorded_at))


class InMemoryStateStore(SchedulerStateStore):
    """In-memory implementation of state store, useful for tests/ephemeral runs."""

    def __init__(self) -> None:
        self._states: dict[str, tuple[datetime.datetime, datetime.date]] = {}
        self._failures: dict[tuple[str, datetime.date], FailureRecord] = {}
        self._gaps: list[GapEntry] = []

    def get_last_run(self, task_name: str) -> tuple[datetime.datetime | None, datetime.date | None]:
        if task_name in self._states:
            return self._states[task_name]
        return None, None

    def update_last_run(
        self, task_name: str, run_time: datetime.datetime, run_date: datetime.date
    ) -> None:
        self._states[task_name] = (run_time, run_date)

    def get_failure(self, task_name: str, logical_date: datetime.date) -> FailureRecord | None:
        return self._failures.get((task_name, logical_date))

    def record_failure(
        self,
        task_name: str,
        logical_date: datetime.date,
        at: datetime.datetime,
        error: str,
    ) -> FailureRecord:
        previous = self._failures.get((task_name, logical_date))
        count = 1 if previous is None else previous.count + 1
        record = FailureRecord(count=count, last_failure=at, last_error=error)
        self._failures[(task_name, logical_date)] = record
        return record

    def clear_failure(self, task_name: str, logical_date: datetime.date) -> None:
        self._failures.pop((task_name, logical_date), None)

    def record_gap(self, entry: GapEntry) -> None:
        self._gaps.append(entry)

    def gaps(self, task_name: str | None = None) -> list[GapEntry]:
        selected = [g for g in self._gaps if task_name is None or g.task == task_name]
        return _sorted_gaps(selected)


class JSONFileStateStore(SchedulerStateStore):
    """JSON file-based implementation of state store for persistence across restarts."""

    def __init__(self, filepath: str) -> None:
        self.filepath = filepath
        self._cache: dict[str, TaskStateRecord] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.filepath):
            self._cache = {}
            return
        try:
            with open(self.filepath, "rb") as f:
                data = f.read()
                if not data:
                    self._cache = {}
                    return
                self._cache = msgspec.json.decode(data, type=dict[str, TaskStateRecord])
        except Exception:
            import logging

            logger = logging.getLogger(__name__)
            logger.exception(f"Failed to load state store from {self.filepath}. Creating a backup.")
            try:
                bak_filepath = self.filepath + ".bak"
                if os.path.exists(self.filepath):
                    os.replace(self.filepath, bak_filepath)
            except Exception:
                logger.exception("Failed to backup corrupted state file.")
            # Fallback to empty if decoding fails or file is corrupted
            self._cache = {}

    def _save(self) -> None:
        # Ensure parent directory exists
        os.makedirs(os.path.dirname(os.path.abspath(self.filepath)), exist_ok=True)
        temp_filepath = self.filepath + ".tmp"
        with open(temp_filepath, "wb") as f:
            f.write(msgspec.json.encode(self._cache))
        os.replace(temp_filepath, self.filepath)

    def _record(self, task_name: str) -> TaskStateRecord:
        """The record of a task, created empty when the task has none yet."""
        record = self._cache.get(task_name)
        if record is None:
            record = TaskStateRecord()
            self._cache[task_name] = record
        return record

    def get_last_run(self, task_name: str) -> tuple[datetime.datetime | None, datetime.date | None]:
        record = self._cache.get(task_name)
        if not record:
            return None, None

        ts = None
        dt = None
        if record.last_run_timestamp:
            try:
                ts = datetime.datetime.fromisoformat(record.last_run_timestamp)
            except ValueError:
                pass
        if record.last_run_date:
            try:
                dt = datetime.date.fromisoformat(record.last_run_date)
            except ValueError:
                pass

        return ts, dt

    def update_last_run(
        self, task_name: str, run_time: datetime.datetime, run_date: datetime.date
    ) -> None:
        # Only the run fields change: failures and gaps of the task are kept.
        record = self._record(task_name)
        record.last_run_timestamp = run_time.isoformat()
        record.last_run_date = run_date.isoformat()
        self._save()

    def get_failure(self, task_name: str, logical_date: datetime.date) -> FailureRecord | None:
        record = self._cache.get(task_name)
        if record is None:
            return None
        return record.failures.get(logical_date.isoformat())

    def record_failure(
        self,
        task_name: str,
        logical_date: datetime.date,
        at: datetime.datetime,
        error: str,
    ) -> FailureRecord:
        record = self._record(task_name)
        key = logical_date.isoformat()
        previous = record.failures.get(key)
        count = 1 if previous is None else previous.count + 1
        updated = FailureRecord(count=count, last_failure=at, last_error=error)
        record.failures[key] = updated
        self._save()
        return updated

    def clear_failure(self, task_name: str, logical_date: datetime.date) -> None:
        record = self._cache.get(task_name)
        if record is None:
            return
        if record.failures.pop(logical_date.isoformat(), None) is not None:
            self._save()

    def record_gap(self, entry: GapEntry) -> None:
        self._record(entry.task).gaps.append(entry)
        self._save()

    def gaps(self, task_name: str | None = None) -> list[GapEntry]:
        if task_name is None:
            collected = [g for record in self._cache.values() for g in record.gaps]
        else:
            record = self._cache.get(task_name)
            collected = list(record.gaps) if record is not None else []
        return _sorted_gaps(collected)
