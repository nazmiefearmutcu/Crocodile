"""Research archive catalogues. Unverified schema is a stated result, never guessed."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

from .schema import ArchiveObject, ArchiveSchemaError

BYBIT_PORTAL = "https://www.bybit.com/en/derivative-activity/history-data"
ZENODO_RECORD = "https://zenodo.org/api/records/18184441"
_BYBIT_FILE = re.compile(r"(\d{4}-\d{2}-\d{2})_([A-Z0-9]+)_ob(\d+)\.data\.zip$")


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.links.extend(value for key, value in attrs if key == "href" and value is not None)


def parse_bybit_listing(html: str, *, category: str, symbol: str) -> tuple[ArchiveObject, ...]:
    if category not in {"linear", "inverse", "spot"} or not re.fullmatch(r"[A-Z0-9]+", symbol):
        raise ValueError("invalid Bybit category/symbol")
    parser = _Links()
    parser.feed(html)
    result = []
    for href in parser.links:
        matched = _BYBIT_FILE.fullmatch(href)
        if matched and matched[2] == symbol:
            result.append(
                ArchiveObject(
                    "bybit",
                    category,
                    symbol,
                    None,
                    matched[1],
                    href,
                    f"https://quote-saver.bycsi.com/orderbook/{category}/{symbol}/{href}",
                    "zip-unaudited",
                    int(matched[3]),
                    schema_status="unaudited",
                )
            )
    return tuple(sorted(result, key=lambda o: (o.source_date, o.filename)))


def parse_zenodo_record(payload: dict[str, Any]) -> tuple[ArchiveObject, ...]:
    """Only the explicitly reviewed Open Book record represents Hyperliquid research data."""
    if int(payload.get("id", 0)) != 18184441:
        raise ArchiveSchemaError(
            "unknown Zenodo dataset; venue provenance requires an explicit audit"
        )
    metadata = payload.get("metadata", {})
    license_value = metadata.get("license", {}).get("id")
    result = []
    for item in payload.get("files", []):
        result.append(
            ArchiveObject(
                "hyperliquid",
                "research-dataset",
                "published-dataset-scope",
                None,
                "published-metadata-scope",
                str(item["key"]),
                str(item["links"]["self"]),
                "research-unaudited",
                None,
                int(item["size"]),
                "unsupported_until_timestamp_and_opening_state_audit",
                license_value,
                str(item.get("checksum", "")) or None,
            )
        )
    return tuple(result)


def audit_archive_candidate(obj: ArchiveObject) -> dict[str, object]:
    """A filename is insufficient evidence of timestamps, opening state, or completeness."""
    return {
        "venue": obj.venue,
        "product": obj.product,
        "symbol": obj.symbol,
        "depth_from_filename": obj.depth,
        "schema_status": obj.schema_status,
        "opening_snapshot": "unknown",
        "source_time": "unknown",
        "sequence_continuity": "unknown",
        "license": obj.license,
        "supported_replay": obj.venue == "okx" and obj.format == "tar-ndjson",
    }
