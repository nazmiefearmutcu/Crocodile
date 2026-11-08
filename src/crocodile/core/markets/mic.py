"""ISO 10383 market identifiers: the packaged snapshot and ``MicRegistry``.

The snapshot is the verbatim ISO file ``iso10383_mic.csv``. It is parsed on first
use by a module-level cached loader that every ``MicRegistry`` shares. Lookups are
exact: MIC codes are upper case, and ``get("xist")`` raises ``UnknownMarketError``
whose detail names ``XIST`` when that code exists. Core does not guess.

``UPDATED`` counts as active. Expired codes stay visible through ``get`` but
``require_active`` refuses them with ``ExpiredMarketError``, which names the
replacement the registry comments state (only when that code exists).
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable, Mapping
from datetime import date
from types import MappingProxyType
from typing import Final

import msgspec

from crocodile.core.markets._data import read_csv
from crocodile.core.markets.errors import (
    ExpiredMarketError,
    ReferenceDataError,
    UnknownMarketError,
)

__all__ = [
    "ACTIVE_STATUSES",
    "MIC_FILE",
    "MIC_HEADER",
    "MicEntry",
    "MicRegistry",
    "parse_mic_rows",
]

MIC_FILE: Final = "iso10383_mic.csv"
MIC_HEADER: Final = (
    "MIC",
    "OPERATING MIC",
    "OPRT/SGMT",
    "MARKET NAME-INSTITUTION DESCRIPTION",
    "LEGAL ENTITY NAME",
    "LEI",
    "MARKET CATEGORY CODE",
    "ACRONYM",
    "ISO COUNTRY CODE (ISO 3166)",
    "CITY",
    "WEBSITE",
    "STATUS",
    "CREATION DATE",
    "LAST UPDATE DATE",
    "LAST VALIDATION DATE",
    "EXPIRY DATE",
    "COMMENTS",
)
ACTIVE_STATUSES: Final = frozenset({"ACTIVE", "UPDATED"})
_REPLACEMENT_PATTERNS: Final = (
    re.compile(r"\bMIC TO USE IS ([A-Z0-9]{4})\b"),
    re.compile(r"\b([A-Z0-9]{4}) IS THE MIC TO USE\b"),
    re.compile(r"\bREPLACED BY ([A-Z0-9]{4})\b"),
)


class MicEntry(msgspec.Struct, frozen=True, kw_only=True):
    """One row of the ISO 10383 snapshot, with the fields the platform uses."""

    mic: str
    operating_mic: str
    is_segment: bool
    name: str
    category: str
    country: str
    city: str
    website: str
    status: str
    comments: str
    expiry_date: date | None = None

    @property
    def is_active(self) -> bool:
        """True for ACTIVE and UPDATED rows."""
        return self.status in ACTIVE_STATUSES


def _expiry(raw: str, mic: str) -> date | None:
    if not raw:
        return None
    if not re.fullmatch(r"\d{8}", raw):
        raise ReferenceDataError(MIC_FILE, f"{mic}: EXPIRY DATE {raw!r} is not YYYYMMDD")
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError as exc:
        raise ReferenceDataError(MIC_FILE, f"{mic}: EXPIRY DATE {raw!r} is not a date") from exc


def parse_mic_rows(rows: Iterable[Mapping[str, str]]) -> tuple[MicEntry, ...]:
    """Turn ISO snapshot rows (keyed by the ISO column names) into sorted entries."""
    entries: dict[str, MicEntry] = {}
    for row in rows:
        mic = row["MIC"]
        if mic in entries:
            raise ReferenceDataError(MIC_FILE, f"duplicate MIC {mic}")
        entries[mic] = MicEntry(
            mic=mic,
            operating_mic=row["OPERATING MIC"],
            is_segment=row["OPRT/SGMT"] == "SGMT",
            name=row["MARKET NAME-INSTITUTION DESCRIPTION"],
            category=row["MARKET CATEGORY CODE"],
            country=row["ISO COUNTRY CODE (ISO 3166)"],
            city=row["CITY"],
            website=row["WEBSITE"],
            status=row["STATUS"],
            comments=row["COMMENTS"],
            expiry_date=_expiry(row["EXPIRY DATE"], mic),
        )
    return tuple(entries[mic] for mic in sorted(entries))


@functools.cache
def _packaged_index() -> Mapping[str, MicEntry]:
    """The packaged snapshot keyed by MIC. Loaded once, shared by every registry."""
    entries = parse_mic_rows(read_csv(MIC_FILE, MIC_HEADER))
    return MappingProxyType({entry.mic: entry for entry in entries})


def _index(entries: Iterable[MicEntry]) -> Mapping[str, MicEntry]:
    table: dict[str, MicEntry] = {}
    for entry in entries:
        if entry.mic in table:
            raise ReferenceDataError(MIC_FILE, f"duplicate MIC {entry.mic}")
        table[entry.mic] = entry
    return MappingProxyType(table)


def _replacement(comments: str, table: Mapping[str, MicEntry]) -> str | None:
    for pattern in _REPLACEMENT_PATTERNS:
        match = pattern.search(comments)
        if match is not None and match.group(1) in table:
            return match.group(1)
    return None


class MicRegistry:
    """Exact lookups over ISO 10383 operating MICs and their segments.

    With no argument the registry reads the packaged snapshot, lazily and once.
    Pass ``entries`` to inject a table, for example in tests.
    """

    def __init__(self, entries: Iterable[MicEntry] | None = None) -> None:
        self._injected: Mapping[str, MicEntry] | None = None if entries is None else _index(entries)

    def _table(self) -> Mapping[str, MicEntry]:
        return self._injected if self._injected is not None else _packaged_index()

    def get(self, mic: str) -> MicEntry:
        """Return the row for ``mic`` in any status. Absent codes raise."""
        table = self._table()
        entry = table.get(mic)
        if entry is None:
            detail = ""
            upper = mic.upper()
            if upper != mic and upper in table:
                detail = f"did you mean {upper!r}? MIC codes are upper case"
            raise UnknownMarketError(mic, detail)
        return entry

    def require_active(self, mic: str) -> MicEntry:
        """Return the row for an ACTIVE or UPDATED ``mic``; refuse anything else."""
        table = self._table()
        entry = self.get(mic)
        if not entry.is_active:
            raise ExpiredMarketError(
                mic,
                entry.comments,
                _replacement(entry.comments, table),
                entry.expiry_date,
            )
        return entry

    def operating(self, mic: str) -> str:
        """Return the operating MIC of an active ``mic`` (a segment maps to its operator)."""
        return self.require_active(mic).operating_mic

    def entries(self) -> tuple[MicEntry, ...]:
        """Every row, sorted by MIC."""
        table = self._table()
        return tuple(table[mic] for mic in sorted(table))

    def __contains__(self, mic: object) -> bool:
        return isinstance(mic, str) and mic in self._table()

    def __len__(self) -> int:
        return len(self._table())
