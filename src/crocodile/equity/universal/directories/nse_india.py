"""NSE India's ``EQUITY_L.csv``: the equity listings of the National Stock Exchange (task 12).

The file states no date, so every record carries no source timestamp. It carries the ISIN and the
listing date of each security: an ISIN that fails its check digit is dropped to ``None`` and
counted, and an unparseable listing date is dropped and counted, never guessed. Local symbols keep
``&`` and ``-`` as published (``M&M``, ``BAJAJ-AUTO``).

The SERIES column is validated against the equity series (EQ, BE, BZ). A row in another series is
skipped and counted: this module makes no guess about other segments. The series of a kept row is
carried on the row only; it is not persisted.

Scope and terms: this module reads a file whose terms were not read in task 12, so the usage class
is ``INTERNAL`` (no redistribution).
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from collections.abc import Mapping
from datetime import date
from types import MappingProxyType
from typing import Final

from crocodile.core.markets.usage import UsageClass
from crocodile.core.schema.enums import SecurityType
from crocodile.equity.universal.base import is_valid_isin
from crocodile.equity.universal.directories.base import (
    DirectoryFormatError,
    DirectoryParse,
    DirectoryProvider,
    DirectoryRow,
    text_document,
)

__all__ = [
    "EQUITY_L_HEADER",
    "EQUITY_L_URL",
    "EQUITY_SERIES",
    "NseIndiaProvider",
    "parse_equity_l",
    "parse_listing_date",
]

SOURCE: Final = "nse_india"
EQUITY_L_URL: Final = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
EQUITY_L_HEADER: Final = (
    "SYMBOL",
    "NAME OF COMPANY",
    "SERIES",
    "DATE OF LISTING",
    "PAID UP VALUE",
    "MARKET LOT",
    "ISIN NUMBER",
    "FACE VALUE",
)
EQUITY_SERIES: Final = frozenset({"EQ", "BE", "BZ"})
_MONTHS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "JAN": 1,
        "FEB": 2,
        "MAR": 3,
        "APR": 4,
        "MAY": 5,
        "JUN": 6,
        "JUL": 7,
        "AUG": 8,
        "SEP": 9,
        "OCT": 10,
        "NOV": 11,
        "DEC": 12,
    }
)
_LISTING_DATE: Final = re.compile(r"(\d{2})-([A-Z]{3})-(\d{4})")


def parse_listing_date(value: str) -> str | None:
    """``06-OCT-2008`` as ``2008-10-06``; anything else, or a day that does not exist, is None."""
    match = _LISTING_DATE.fullmatch(value.strip().upper())
    if match is None:
        return None
    day_text, month_name, year_text = match.groups()
    month = _MONTHS.get(month_name)
    if month is None:
        return None
    try:
        listed = date(int(year_text), month, int(day_text))
    except ValueError:
        return None
    return listed.isoformat()


def parse_equity_l(text: str, *, document: str = EQUITY_L_URL) -> DirectoryParse:
    """The equity listings of ``EQUITY_L.csv``; the header must be the documented one."""
    rows = [row for row in csv.reader(io.StringIO(text, newline="")) if any(row)]
    if not rows:
        raise DirectoryFormatError(f"{document} is empty")
    header = tuple(cell.strip() for cell in rows[0])
    if header != EQUITY_L_HEADER:
        raise DirectoryFormatError(f"{document} header is not the documented one: {header}")
    skipped: Counter[str] = Counter()
    dropped: Counter[str] = Counter()
    parsed: list[DirectoryRow] = []
    for raw in rows[1:]:
        if len(raw) != len(EQUITY_L_HEADER):
            skipped["malformed_row"] += 1
            continue
        symbol, name, series, listed, _paid, _lot, isin_text, _face = (cell.strip() for cell in raw)
        if series not in EQUITY_SERIES:
            skipped[f"series:{series}"] += 1
            continue
        isin: str | None = isin_text if is_valid_isin(isin_text) else None
        if isin is None and isin_text:
            dropped["isin"] += 1
        listing_date = parse_listing_date(listed)
        if listing_date is None and listed:
            dropped["listing_date"] += 1
        parsed.append(
            DirectoryRow(
                symbol_raw=symbol,
                local_symbol=symbol,
                mic="XNSE",
                segment_mic=None,
                name=name or None,
                security_type=SecurityType.UNKNOWN,
                isin=isin,
                listing_date=listing_date,
                series=series,
            )
        )
    return DirectoryParse(
        source=SOURCE,
        document=document,
        rows=tuple(parsed),
        as_of_ns=None,
        skipped=dict(skipped),
        dropped_fields=dict(dropped),
    )


class NseIndiaProvider(DirectoryProvider):
    """The NSE equity listings, read as one snapshot of the Indian cash equity market."""

    name = "nse_india"
    host = "archives.nseindia.com"
    rest_url = "https://archives.nseindia.com"
    urls = (EQUITY_L_URL,)
    usage_class = UsageClass.INTERNAL
    supported_mics = frozenset({"XNSE"})

    @classmethod
    def parse_documents(cls, documents: Mapping[str, bytes]) -> tuple[DirectoryParse, ...]:
        return (parse_equity_l(text_document(documents, EQUITY_L_URL), document=EQUITY_L_URL),)
