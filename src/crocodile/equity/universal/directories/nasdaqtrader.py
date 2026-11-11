"""Nasdaq Trader's symbol directories: ``nasdaqlisted.txt`` and ``otherlisted.txt`` (task 12).

Both files are pipe-delimited, with a documented header line, one row per security and a footer
``File Creation Time: MMDDYYYYHH:MM`` that gives the file's own instant. A missing footer is a
truncated download and is refused. ``Test Issue`` rows are skipped and counted, and the ETF flag is
the only type the directory states, so every other row is ``UNKNOWN``.

The footer carries no zone. Nasdaq Trader is an Eastern-time operation, so the instant is read as
``America/New_York``: that reading is an assumption, and it is the only one in this module.

Scope and terms: this module reads files whose terms were not read in task 12, so the usage class
is ``INTERNAL`` (no redistribution).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Final
from zoneinfo import ZoneInfo

from crocodile.core.markets.usage import UsageClass
from crocodile.core.schema.enums import SecurityType
from crocodile.equity.providers.base import US_EQUITY_MICS
from crocodile.equity.universal.directories.base import (
    DirectoryFormatError,
    DirectoryParse,
    DirectoryProvider,
    DirectoryRow,
    text_document,
    utc_ns,
)

__all__ = [
    "FOOTER_PREFIX",
    "NASDAQLISTED_HEADER",
    "NASDAQLISTED_URL",
    "NASDAQ_MARKET_CATEGORY",
    "OTHERLISTED_EXCHANGE",
    "OTHERLISTED_HEADER",
    "OTHERLISTED_URL",
    "NasdaqTraderProvider",
    "parse_file_creation_time",
    "parse_nasdaqlisted",
    "parse_otherlisted",
]

SOURCE: Final = "nasdaqtrader"
NASDAQLISTED_URL: Final = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHERLISTED_URL: Final = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
NASDAQLISTED_HEADER: Final = (
    "Symbol",
    "Security Name",
    "Market Category",
    "Test Issue",
    "Financial Status",
    "Round Lot Size",
    "ETF",
    "NextShares",
)
OTHERLISTED_HEADER: Final = (
    "ACT Symbol",
    "Security Name",
    "Exchange",
    "CQS Symbol",
    "ETF",
    "Round Lot Size",
    "Test Issue",
    "NASDAQ Symbol",
)
NASDAQ_MARKET_CATEGORY: Final[Mapping[str, str]] = MappingProxyType(
    {"Q": "XNGS", "G": "XNMS", "S": "XNCM"}  # segments of XNAS
)
OTHERLISTED_EXCHANGE: Final[Mapping[str, tuple[str, str | None]]] = MappingProxyType(
    {
        "A": ("XNYS", "XASE"),  # NYSE American
        "N": ("XNYS", None),  # NYSE
        "P": ("XNYS", "ARCX"),  # NYSE Arca
        "Z": ("XCBO", "BATS"),  # Cboe BZX
        "V": ("IEXG", None),  # IEX
        "M": ("XNYS", "XCHI"),  # NYSE Texas
        "F": ("TXSE", None),  # Texas Stock Exchange: not in exchanges.csv, dropped and counted
    }
)
FOOTER_PREFIX: Final = "File Creation Time:"
_NEW_YORK: Final = ZoneInfo("America/New_York")
_FOOTER_STAMP: Final = re.compile(r"(\d{2})(\d{2})(\d{4})(\d{2}):(\d{2})")
_FIELDS: Final = 8


def parse_file_creation_time(line: str) -> int:
    """``File Creation Time: 1106202621:31||||||`` as UTC epoch nanoseconds.

    The stamp is MMDDYYYY HH:MM, read as America/New_York (see the module docstring).
    """
    if not line.startswith(FOOTER_PREFIX):
        raise DirectoryFormatError("the nasdaqtrader footer does not start with its prefix")
    stamp = line[len(FOOTER_PREFIX) :].split("|", 1)[0].strip()
    match = _FOOTER_STAMP.fullmatch(stamp)
    if match is None:
        raise DirectoryFormatError(f"the nasdaqtrader footer stamp {stamp!r} is not MMDDYYYYHH:MM")
    month, day, year, hour, minute = (int(group) for group in match.groups())
    try:
        local = datetime(year, month, day, hour, minute, tzinfo=_NEW_YORK)
    except ValueError as exc:
        raise DirectoryFormatError(
            f"the nasdaqtrader footer stamp {stamp!r} is not a date"
        ) from exc
    return utc_ns(local)


def _pipe_body(
    text: str, *, header: tuple[str, ...], document: str
) -> tuple[list[tuple[str, ...]], int, int]:
    """(data rows with stripped cells, footer instant, malformed-row count) of one file."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        raise DirectoryFormatError(f"{document} has no header and footer")
    cells = tuple(cell.strip() for cell in lines[0].split("|"))
    if cells != header:
        raise DirectoryFormatError(f"{document} header is not the documented one: {cells}")
    footer = lines[-1]
    if not footer.startswith(FOOTER_PREFIX):
        raise DirectoryFormatError(f"{document} has no footer: the download is truncated")
    as_of_ns = parse_file_creation_time(footer)
    rows: list[tuple[str, ...]] = []
    malformed = 0
    for line in lines[1:-1]:
        fields = line.split("|")
        if len(fields) != _FIELDS:
            malformed += 1
            continue
        rows.append(tuple(field.strip() for field in fields))
    return rows, as_of_ns, malformed


def _security_type(etf_flag: str) -> SecurityType:
    return SecurityType.ETF if etf_flag == "Y" else SecurityType.UNKNOWN


def parse_nasdaqlisted(text: str, *, document: str = NASDAQLISTED_URL) -> DirectoryParse:
    """The Nasdaq-listed securities: every row is XNAS, with its market category as segment."""
    rows, as_of_ns, malformed = _pipe_body(text, header=NASDAQLISTED_HEADER, document=document)
    skipped: Counter[str] = Counter()
    if malformed:
        skipped["malformed_row"] += malformed
    parsed: list[DirectoryRow] = []
    for symbol, name, category, test_issue, _status, _lot, etf_flag, _next in rows:
        if test_issue == "Y":
            skipped["test_issue"] += 1
            continue
        segment = NASDAQ_MARKET_CATEGORY.get(category)
        if segment is None:
            skipped[f"unknown_market_category:{category}"] += 1
            continue
        parsed.append(
            DirectoryRow(
                symbol_raw=symbol,
                local_symbol=symbol,
                mic="XNAS",
                segment_mic=segment,
                name=name or None,
                security_type=_security_type(etf_flag),
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


def parse_otherlisted(text: str, *, document: str = OTHERLISTED_URL) -> DirectoryParse:
    """The other exchanges' securities: the exchange code gives the operating MIC and segment."""
    rows, as_of_ns, malformed = _pipe_body(text, header=OTHERLISTED_HEADER, document=document)
    skipped: Counter[str] = Counter()
    if malformed:
        skipped["malformed_row"] += malformed
    parsed: list[DirectoryRow] = []
    for act_symbol, name, code, _cqs, etf_flag, _lot, test_issue, _nasdaq in rows:
        if test_issue == "Y":
            skipped["test_issue"] += 1
            continue
        mapping = OTHERLISTED_EXCHANGE.get(code)
        if mapping is None:
            skipped[f"unknown_exchange_code:{code}"] += 1
            continue
        mic, segment = mapping
        parsed.append(
            DirectoryRow(
                symbol_raw=act_symbol,
                local_symbol=act_symbol,
                mic=mic,
                segment_mic=segment,
                name=name or None,
                security_type=_security_type(etf_flag),
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


class NasdaqTraderProvider(DirectoryProvider):
    """Nasdaq Trader's two symbol directories, read as one snapshot of the US listings."""

    name = "nasdaqtrader"
    host = "www.nasdaqtrader.com"
    rest_url = "https://www.nasdaqtrader.com"
    urls = (NASDAQLISTED_URL, OTHERLISTED_URL)
    usage_class = UsageClass.INTERNAL
    supported_mics = US_EQUITY_MICS

    @classmethod
    def parse_documents(cls, documents: Mapping[str, bytes]) -> tuple[DirectoryParse, ...]:
        return (
            parse_nasdaqlisted(
                text_document(documents, NASDAQLISTED_URL), document=NASDAQLISTED_URL
            ),
            parse_otherlisted(text_document(documents, OTHERLISTED_URL), document=OTHERLISTED_URL),
        )
