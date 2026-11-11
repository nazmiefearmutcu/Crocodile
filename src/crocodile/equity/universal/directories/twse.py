"""The TWSE company list, ``t187ap03_L.csv`` (task 12): the Taiwan Stock Exchange listed companies.

The file is a UTF-8 CSV (a BOM is optional) with a 33-column header in Chinese. Four columns are
used: the report date (ROC calendar, e.g. ``1151107`` for 2026-11-07), the company code (the local
symbol), the company name and the listing date (Gregorian ``YYYYMMDD``). Every row must carry the
same report date, or the download is mixed and refused. The report date, a calendar date with no
time, is read as UTC midnight of that date, the same rule as a daily bar. The English short name is
not persisted.

Licence: this is the one directory with a read licence. The TWSE open data is published under the
Open Government Data License, version 1.0, which permits redistribution with attribution, so the
usage class is ``REDISTRIBUTION`` and the attribution below is mandatory wherever the data goes.

Column headers are written as ``\\uXXXX`` escapes so the module stays ASCII.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from collections.abc import Mapping
from datetime import date
from typing import Final

from crocodile.core.markets.usage import UsageClass
from crocodile.core.schema.enums import SecurityType
from crocodile.equity.universal.base import daily_bar_ts_ns
from crocodile.equity.universal.directories.base import (
    DirectoryFormatError,
    DirectoryParse,
    DirectoryProvider,
    DirectoryRow,
    bytes_document,
)

__all__ = [
    "COL_COMPANY_CODE",
    "COL_COMPANY_NAME",
    "COL_ENGLISH_SHORT_NAME",
    "COL_LISTING_DATE",
    "COL_REPORT_DATE",
    "T187AP03_URL",
    "TWSE_COLUMN_COUNT",
    "TwseProvider",
    "parse_t187ap03",
    "roc_date",
]

SOURCE: Final = "twse"
T187AP03_URL: Final = "https://mopsfin.twse.com.tw/opendata/t187ap03_L.csv"
COL_REPORT_DATE: Final = "\u51fa\u8868\u65e5\u671f"  # report date
COL_COMPANY_CODE: Final = "\u516c\u53f8\u4ee3\u865f"  # company code
COL_COMPANY_NAME: Final = "\u516c\u53f8\u540d\u7a31"  # company name
COL_LISTING_DATE: Final = "\u4e0a\u5e02\u65e5\u671f"  # listing date
COL_ENGLISH_SHORT_NAME: Final = "\u82f1\u6587\u7c21\u7a31"  # English short name (not persisted)
TWSE_COLUMN_COUNT: Final = 33
_ROC_DATE: Final = re.compile(r"\d{6,7}")
_YYYYMMDD: Final = re.compile(r"\d{8}")
_ATTRIBUTION: Final = (
    "Source: Taiwan Stock Exchange, Market Observation Post System open data "
    "(t187ap03_L), Open Government Data License, version 1.0"
)


def roc_date(value: str) -> date:
    """A Republic of China calendar date (``1151107``: year 115 is 2026) as a Gregorian date."""
    text = value.strip()
    if _ROC_DATE.fullmatch(text) is None:
        raise ValueError(f"not a ROC date: {value!r}")
    year = int(text[:-4]) + 1911
    return date(year, int(text[-4:-2]), int(text[-2:]))


def _listing_date(value: str) -> str | None:
    if _YYYYMMDD.fullmatch(value) is None:
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:])).isoformat()
    except ValueError:
        return None


def parse_t187ap03(raw: bytes, *, document: str = T187AP03_URL) -> DirectoryParse:
    """The listed companies of ``t187ap03_L.csv``, with the one report date every row shares."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DirectoryFormatError(f"{document} is not UTF-8 text") from exc
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows:
        raise DirectoryFormatError(f"{document} is empty")
    header = tuple(cell.strip() for cell in rows[0])
    if len(header) != TWSE_COLUMN_COUNT:
        raise DirectoryFormatError(
            f"{document} has {len(header)} header columns, expected {TWSE_COLUMN_COUNT}"
        )
    for column in (COL_REPORT_DATE, COL_COMPANY_CODE, COL_COMPANY_NAME, COL_LISTING_DATE):
        if column not in header:
            raise DirectoryFormatError(f"{document} header lacks the column {column!r}")
    at_date = header.index(COL_REPORT_DATE)
    at_code = header.index(COL_COMPANY_CODE)
    at_name = header.index(COL_COMPANY_NAME)
    at_listed = header.index(COL_LISTING_DATE)
    skipped: Counter[str] = Counter()
    dropped: Counter[str] = Counter()
    report_dates: set[str] = set()
    parsed: list[DirectoryRow] = []
    for raw_row in rows[1:]:
        if not any(raw_row):
            continue
        if len(raw_row) != TWSE_COLUMN_COUNT:
            skipped["malformed_row"] += 1
            continue
        cells = [cell.strip() for cell in raw_row]
        report_dates.add(cells[at_date])
        listed = cells[at_listed]
        listing_date = _listing_date(listed)
        if listing_date is None and listed:
            dropped["listing_date"] += 1
        parsed.append(
            DirectoryRow(
                symbol_raw=cells[at_code],
                local_symbol=cells[at_code],
                mic="XTAI",
                segment_mic=None,
                name=cells[at_name] or None,
                security_type=SecurityType.UNKNOWN,
                listing_date=listing_date,
            )
        )
    if len(report_dates) > 1:
        raise DirectoryFormatError(f"{document} mixes report dates: {sorted(report_dates)}")
    as_of_ns: int | None = None
    if report_dates:
        (report,) = report_dates
        try:
            as_of_ns = daily_bar_ts_ns(roc_date(report))
        except ValueError as exc:
            raise DirectoryFormatError(f"{document} report date {report!r} is not a date") from exc
    return DirectoryParse(
        source=SOURCE,
        document=document,
        rows=tuple(parsed),
        as_of_ns=as_of_ns,
        skipped=dict(skipped),
        dropped_fields=dict(dropped),
    )


class TwseProvider(DirectoryProvider):
    """The TWSE listed companies, read as one snapshot of the Taiwan cash equity market."""

    name = "twse"
    host = "mopsfin.twse.com.tw"
    rest_url = "https://mopsfin.twse.com.tw"
    urls = (T187AP03_URL,)
    usage_class = UsageClass.REDISTRIBUTION
    supported_mics = frozenset({"XTAI"})
    attribution = _ATTRIBUTION

    @classmethod
    def parse_documents(cls, documents: Mapping[str, bytes]) -> tuple[DirectoryParse, ...]:
        return (parse_t187ap03(bytes_document(documents, T187AP03_URL), document=T187AP03_URL),)
