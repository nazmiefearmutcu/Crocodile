"""The ASX's listed companies file, ``ASXListedCompanies.csv`` (task 12).

The file opens with a preamble line, ``ASX listed companies as at <weekday> <month> <day> <time>
<zone> <year>``, which is the file's as-of instant. The preamble is read in Australia/Sydney; the
zone abbreviation picks the fold, AEDT or AEST, and must equal the zone the wall time falls in, or
the file is refused. A blank line, the header row and the rows follow. Quoted company names may
contain commas and keep their trailing dots as published.

Scope and terms: this module reads a file whose terms were not read in task 12, so the usage class
is ``INTERNAL`` (no redistribution). The GICS industry group is not persisted (no record field).
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Final
from zoneinfo import ZoneInfo

from crocodile.core.markets.usage import UsageClass
from crocodile.core.schema.enums import SecurityType
from crocodile.equity.universal.directories.base import (
    DirectoryFormatError,
    DirectoryParse,
    DirectoryProvider,
    DirectoryRow,
    text_document,
    utc_ns,
)

__all__ = ["ASX_HEADER", "ASX_LISTED_URL", "AsxProvider", "parse_as_at", "parse_asx_listed"]

SOURCE: Final = "asx"
ASX_LISTED_URL: Final = "https://www.asx.com.au/asx/research/ASXListedCompanies.csv"
ASX_HEADER: Final = ("Company name", "ASX code", "GICS industry group")
_SYDNEY: Final = ZoneInfo("Australia/Sydney")
_PREAMBLE: Final = re.compile(
    r"ASX listed companies as at \w{3} (\w{3}) (\d{1,2}) (\d{2}):(\d{2}):(\d{2}) (\w{3,4}) (\d{4})"
)
_MONTHS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "Jan": 1,
        "Feb": 2,
        "Mar": 3,
        "Apr": 4,
        "May": 5,
        "Jun": 6,
        "Jul": 7,
        "Aug": 8,
        "Sep": 9,
        "Oct": 10,
        "Nov": 11,
        "Dec": 12,
    }
)


def parse_as_at(line: str) -> int:
    """The preamble's instant as UTC epoch nanoseconds (Australia/Sydney, AEDT or AEST)."""
    match = _PREAMBLE.fullmatch(line.strip())
    if match is None:
        raise DirectoryFormatError("the ASX file does not open with its 'as at' preamble")
    month_name, day, hour, minute, second, abbreviation, year = match.groups()
    month = _MONTHS.get(month_name)
    if month is None:
        raise DirectoryFormatError(f"the ASX preamble month {month_name!r} is not a month")
    try:
        wall = datetime(int(year), month, int(day), int(hour), int(minute), int(second))
    except ValueError as exc:
        raise DirectoryFormatError("the ASX preamble is not a date") from exc
    for fold in (0, 1):
        local = wall.replace(tzinfo=_SYDNEY, fold=fold)
        if local.tzname() == abbreviation:
            return utc_ns(local)
    raise DirectoryFormatError(
        f"the ASX preamble says {abbreviation} but {wall} is not in that zone"
    )


def parse_asx_listed(text: str, *, document: str = ASX_LISTED_URL) -> DirectoryParse:
    """The ASX listed companies: line one is the as-of preamble, then the header and the rows."""
    lines = text.splitlines()
    if not lines:
        raise DirectoryFormatError(f"{document} is empty")
    as_of_ns = parse_as_at(lines[0])
    rows = [row for row in csv.reader(io.StringIO("\n".join(lines[1:]), newline="")) if any(row)]
    if not rows:
        raise DirectoryFormatError(f"{document} has no header row")
    header = tuple(cell.strip() for cell in rows[0])
    if header != ASX_HEADER:
        raise DirectoryFormatError(f"{document} header is not the documented one: {header}")
    skipped: Counter[str] = Counter()
    parsed: list[DirectoryRow] = []
    for raw in rows[1:]:
        if len(raw) != len(ASX_HEADER):
            skipped["malformed_row"] += 1
            continue
        name, code, _gics = (cell.strip() for cell in raw)
        parsed.append(
            DirectoryRow(
                symbol_raw=code,
                local_symbol=code,
                mic="XASX",
                segment_mic=None,
                name=name or None,
                security_type=SecurityType.UNKNOWN,
            )
        )
    return DirectoryParse(
        source=SOURCE,
        document=document,
        rows=tuple(parsed),
        as_of_ns=as_of_ns,
        skipped=dict(skipped),
        dropped_fields={},
    )


class AsxProvider(DirectoryProvider):
    """The ASX listed companies, read as one snapshot of the Australian cash equity market."""

    name = "asx"
    host = "www.asx.com.au"
    rest_url = "https://www.asx.com.au"
    urls = (ASX_LISTED_URL,)
    usage_class = UsageClass.INTERNAL
    supported_mics = frozenset({"XASX"})

    @classmethod
    def parse_documents(cls, documents: Mapping[str, bytes]) -> tuple[DirectoryParse, ...]:
        return (
            parse_asx_listed(text_document(documents, ASX_LISTED_URL), document=ASX_LISTED_URL),
        )
