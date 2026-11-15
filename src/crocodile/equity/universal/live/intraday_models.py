"""Exact, immutable public models for the independent Eurex minute source.

Quantities are the publisher's raw quantity field, not independently reconciled
contract volume. No model here changes the core OHLCV wire schema.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Literal

MINUTE_NS = 60_000_000_000
SOURCE = "eurex_delayed_posttrade"
_TIMESTAMP = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z")
_FILE = re.compile(r"DEUR-posttrade-(\d{4}-\d{2}-\d{2}T\d{2}_\d{2})\.json\.gz")


def parse_utc_ns(value: str) -> int:
    """Parse UTC source timestamps without float or microsecond truncation."""
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        raise ValueError("unsupported UTC timestamp")
    stamp = datetime.strptime(match[1], "%Y-%m-%dT%H:%M:%S")
    return calendar.timegm(stamp.timetuple()) * 1_000_000_000 + int((match[2] or "").ljust(9, "0"))


def file_minute_ns(file_name: str) -> int:
    """Validate a minute file name and return publication partition time only."""
    match = _FILE.fullmatch(file_name)
    if match is None:
        raise ValueError("unsupported minute file name")
    return parse_utc_ns(match[1].replace("_", ":") + ":00Z")


@dataclass(frozen=True, slots=True)
class Contract:
    isin: str
    display_symbol: str
    product: str
    maturity: str
    price_unit: str
    price_notation: int
    mapping_url: str
    mic: str = "XEUR"
    evidence_date: date = date(2026, 11, 15)
    # Product mapping is verified for this maturity, not a perpetual alias.
    valid_from: date = date(2026, 11, 13)
    valid_until: date = date(2026, 12, 31)


CONTRACTS = MappingProxyType(
    {
        "DE000F0GDWD2": Contract(
            "DE000F0GDWD2",
            "FDAXZ2026",
            "FDAX",
            "2026-12",
            "index_points",
            1,
            "https://live.deutsche-boerse.com/future/fdax-dec26-index-futures",
        ),
        "DE000F2HLQG4": Contract(
            "DE000F2HLQG4",
            "FGBLZ2026",
            "FGBL",
            "2026-12",
            "percent_of_par",
            2,
            "https://www.boerse-stuttgart.de/de-de/produkte/future/"
            "de000f2hlqg4-euro-bund-future-12-2026-eurex/",
        ),
    }
)


@dataclass(frozen=True, slots=True, order=True)
class MinuteKey:
    isin: str
    minute_start_ns: int


@dataclass(frozen=True, slots=True, order=True)
class FileReference:
    file_name: str
    sha256: str


@dataclass(frozen=True, slots=True)
class Observation:
    isin: str
    venue: str
    transaction_id: str
    event_time_ns: int
    publication_time_ns: int
    captured_ns: int
    price: Decimal | None
    quantity: Decimal | None
    price_notation: int | None
    price_currency: str
    trading_system: str
    flags: str
    file_name: str
    file_sha256: str
    row_number: int
    payload_sha256: str
    quality_reasons: tuple[str, ...] = ()
    source: str = SOURCE
    origin: Literal["native"] = "native"
    aggressor: Literal["unknown"] = "unknown"

    @property
    def trade_key(self) -> tuple[str, str, str]:
        return self.venue, self.isin, self.transaction_id

    @property
    def minute_start_ns(self) -> int:
        return self.event_time_ns // MINUTE_NS * MINUTE_NS


@dataclass(frozen=True, slots=True)
class BarEnvelope:
    isin: str
    minute_start_ns: int
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal
    trade_count: int
    vwap: Decimal | None
    revision: int
    quality: Literal["observed", "incomplete"]
    quality_reasons: tuple[str, ...]
    excluded_count: int
    files: tuple[FileReference, ...]
    latest_event_ns: int
    latest_publication_ns: int
    captured_ns: int
    price_unit: str
    source: str = SOURCE
    origin: Literal["derived"] = "derived"
    input_origin: Literal["native"] = "native"
    quantity_unit: str = "publisher_quantity"
    declared_delay_s: int = 900


@dataclass(frozen=True, slots=True)
class IngestResult:
    file_name: str
    sha256: str
    changed: bool
    indexed_rows: int
    changed_bars: tuple[BarEnvelope, ...] = ()
    removed_minutes: tuple[MinuteKey, ...] = ()


@dataclass(frozen=True, slots=True)
class CoverageGap:
    """Missing publication partitions; never a claim about no-trade minutes."""

    start_ns: int
    end_ns: int
    reason: str = "missing_manifest_interval"


@dataclass(frozen=True, slots=True)
class RefreshStatus:
    status: Literal["ready", "partial", "error", "backoff", "storage_full"]
    ingested_files: int
    pending_files: int
    completed_files: int
    checked_ns: int
    last_success_ns: int | None
    latest_event_ns: int | None
    manifest_gaps: tuple[CoverageGap, ...]
    changed_bars: tuple[BarEnvelope, ...] = ()
    removed_minutes: tuple[MinuteKey, ...] = ()
    error_code: str | None = None
    retry_after_s: float | None = None
    declared_delay_s: int = 900
    market_status: Literal["unknown"] = "unknown"
