"""Archive identities and coverage, separate from exchange event identities."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from urllib.parse import urlsplit, urlunsplit


class DownloadError(ValueError):
    """A bounded archive transfer could not be verified."""


class ExpiredArchiveURL(DownloadError):
    """Refresh the official catalogue rather than reusing an expiring URL."""


class ArchiveSchemaError(ValueError):
    """An archive cannot safely be interpreted as the advertised L2 schema."""


def public_url_identity(url: str) -> str:
    """Signed query strings are transient delivery credentials, never identity."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def safe_filename(filename: str) -> None:
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10))
    if (
        not filename
        or filename in {".", ".."}
        or filename.endswith((".", " "))
        or filename.split(".", 1)[0].upper() in reserved
        or any(ord(c) < 32 or c in '/\\:<>"|?*' for c in filename)
    ):
        raise ValueError("archive filename must be a plain basename")


@dataclass(frozen=True, slots=True)
class ArchiveRequest:
    symbol: str
    product: str
    module: int
    start_date: date
    end_date: date
    aggregation: str = "daily"
    family: str | None = None

    def __post_init__(self) -> None:
        if self.product not in {"SPOT", "SWAP", "FUTURES", "OPTION"}:
            raise ValueError("unknown OKX instrument type")
        if self.module not in {4, 5}:
            raise ValueError("supported OKX modules are 4 (400) and 5 (5000)")
        if self.start_date > self.end_date or self.aggregation not in {"daily", "monthly"}:
            raise ValueError("invalid archive date range or aggregation")
        if not self.symbol or not all(
            c.isascii() and (c.isalnum() or c == "-") for c in self.symbol
        ):
            raise ValueError("invalid instrument symbol")
        if self.product in {"FUTURES", "OPTION"} and self.family is None:
            raise ValueError("FUTURES/OPTION catalogue requests require explicit family")

    @property
    def selector(self) -> str:
        if self.product == "SPOT":
            return self.symbol
        return self.family or self.symbol.removesuffix("-SWAP")


@dataclass(frozen=True, slots=True)
class ArchiveObject:
    venue: str
    product: str
    symbol: str
    module: int | None
    source_date: str
    filename: str
    url: str = field(repr=False)
    format: str
    depth: int | None
    declared_bytes: int | None = None
    schema_status: str = "unaudited"
    license: str | None = None
    published_checksum: str | None = None

    def __post_init__(self) -> None:
        safe_filename(self.filename)

    @property
    def identity(self) -> str:
        parts = (
            self.venue,
            self.product,
            self.symbol,
            self.module,
            self.source_date,
            self.filename,
            public_url_identity(self.url),
        )
        return hashlib.sha256(json.dumps(parts, ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ArchiveListing:
    request: ArchiveRequest
    objects: tuple[ArchiveObject, ...]
    missing_dates: tuple[str, ...]
    checked_at_ns: int
    notes: tuple[str, ...] = (
        "Unavailable dates are holes, not empty books.",
        "Catalogue coverage is nominal; replay audits actual timestamps and opening state.",
    )


@dataclass(frozen=True, slots=True)
class ArchiveManifest:
    venue: str
    product: str
    symbol: str
    module: int | None
    source_date: str
    filename: str
    source_url: str
    sha256: str
    bytes: int
    format: str
    depth: int | None
    imported_at_ns: int
    continuity: str = "source-sequence-unavailable"
    schema_version: int = 1
    decoder_version: str = "okx-archive-ndjson-v1"
    timestamp_precision_ns: int | None = 1_000_000
    coverage: str = "nominal-source-date; actual coverage requires replay audit"
    license: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
