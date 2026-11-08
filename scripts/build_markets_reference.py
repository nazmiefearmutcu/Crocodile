"""Deterministic generator for the packaged core/markets reference data.

Builds three generated files in ``src/crocodile/core/markets/data``:

* ``iso4217.csv``   derived from the SIX ISO 4217 list one XML (pinned copy);
* ``exchanges.csv`` derived from the research market matrix, the ISO 10383
  snapshot, ``sanctions.csv`` and the evidence-tagged ``curation.csv``;
* ``MANIFEST.json`` the hashes, row counts, sources and licence notes of every
  packaged data file.

``iso10383_mic.csv`` and ``sanctions.csv`` are inputs, not outputs: the first is
the verbatim ISO file and the second is hand-written. The script checks their
hashes but never rewrites them.

Usage, from the repository root, in the pinned ephemeral environment (the
project environment is never modified)::

    uv run --no-project --python 3.12 --with "exchange-calendars==4.13.2" \\
        --with "pandas==3.0.3" python scripts/build_markets_reference.py
    ... python scripts/build_markets_reference.py --check   # exits 0 when up to date

The script reads no clock and no network and writes LF, UTF-8, no BOM.
``exchange_calendars`` is imported inside ``main()`` only, so the builders can be
loaded without it.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import xml.etree.ElementTree as ET
import zoneinfo
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from types import ModuleType
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DATA_DIR = REPO / "src" / "crocodile" / "core" / "markets" / "data"
REF_DIR = REPO / "scripts" / "markets_reference"
MATRIX_PATH = REPO / "docs" / "superpowers" / "research" / "data" / "market-matrix-2026-11-08.json"
ISO_XML_PATH = REF_DIR / "iso4217_list_one.xml"
CURATION_PATH = REF_DIR / "curation.csv"
MIC_PATH = DATA_DIR / "iso10383_mic.csv"
SANCTIONS_PATH = DATA_DIR / "sanctions.csv"

AS_OF = "2026-11-08"
RETRIEVED = "2026-11-08"
MATRIX_REL = "docs/superpowers/research/data/market-matrix-2026-11-08.json"
CURATION_REL = "scripts/markets_reference/curation.csv"
MIC_SHA256 = "09a0d07b7b8348147453a2d8b18bc6638e3c5b16f749462582c9b1edf0fd0e38"
ISO4217_XML_SHA256 = "33139b438657d1cee116ba737807ea71d19d6de4b90f799a09c56f0cc6a1b0ff"
ISO4217_PUBLISHED = "2026-09-17"
MIC_LAST_UPDATE = "2026-10-26"
MIC_URL = "https://www.iso20022.org/sites/default/files/ISO10383_MIC/ISO10383_MIC.csv"
ISO4217_URL = (
    "https://www.six-group.com/dam/download/financial-information/data-center/"
    "iso-currrency/lists/list-one.xml"
)
ISO10383_LICENSE = (
    "Freely published by the ISO 10383 registration authority on iso20022.org; shipped "
    "unmodified with attribution; publisher terms not reviewed in this task."
)
ISO4217_LICENSE = (
    "List one published by SIX as ISO 4217 maintenance agency; only code, minor unit and "
    "name derived; terms not reviewed in this task."
)
EXCHANGES_LICENSE = (
    "Crocodile research matrix (this repository, Apache-2.0); weekend/close of library rows "
    "from exchange_calendars (Apache-2.0)."
)
SANCTIONS_LICENSE = "Curated from public sanctions programme pages; not legal advice."

MIC_HEADER = (
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
ISO4217_HEADER = ("code", "minor_unit", "name")
EXCHANGE_COLUMNS = (
    "mic",
    "name",
    "country",
    "timezone",
    "currency",
    "quote_scale",
    "weekend",
    "close_local",
    "xcal_code",
    "tier_research",
    "sanctioned",
    "notes",
)
SANCTIONS_HEADER = ("country", "source", "note")
CURATION_HEADER = ("mic", "field", "value", "evidence", "source", "note")

WEEKDAY_TOKENS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
DEFAULT_WEEKEND = frozenset({5, 6})

EXPECTED_EXCHANGE_ROWS = 172
EXPECTED_CURRENCY_ROWS = 165
EXPECTED_XCAL_ROWS = 64
EXPECTED_MIC_ROWS = 2889
EXPECTED_SANCTIONED_ROWS = 5
EXPECTED_SCALE_2 = frozenset({"XJSE", "XLON", "XTAE"})
EXPECTED_NA_CODES = frozenset(
    {"XAG", "XAU", "XBA", "XBB", "XBC", "XBD", "XDR", "XPD", "XPT", "XSU", "XTS", "XUA", "XXX"}
)
EXPECTED_FUND_CODES = frozenset(
    {"BOV", "CHE", "CHW", "CLF", "COU", "MXV", "USN", "UYI", "UYW", "XAD"}
)
MINOR_QUOTE_TOKENS = {"GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}
SANCTIONED_COUNTRIES = frozenset({"BY", "IR", "KP", "RU", "SY"})
TARGETED_COUNTRIES = frozenset({"VE", "MM", "SD", "LY"})
CURATION_FIELDS: dict[str, frozenset[str]] = {
    "weekend": frozenset({"primary", "secondary"}),
    "close_local": frozenset({"primary", "secondary"}),
    "timezone": frozenset({"library", "primary", "secondary"}),
    "currency": frozenset({"primary", "secondary", "unverified"}),
    "xcal_code": frozenset({"library"}),
}
LIBRARY_PROBE_WINDOW = ("2026-11-02", "2026-11-13")
INSTALL_HINT = (
    "exchange_calendars is required. Run from the repository root:\n"
    '  uv run --no-project --python 3.12 --with "exchange-calendars==4.13.2" '
    '--with "pandas==3.0.3" python scripts/build_markets_reference.py'
)


class BuildError(Exception):
    """An input or derived value violates the reference-data contract."""


@dataclass(frozen=True)
class CurationRow:
    value: str
    evidence: str
    source: str


@dataclass(frozen=True)
class CalendarFacts:
    weekend: frozenset[int]
    close_local: time


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fail(message: str) -> BuildError:
    return BuildError(message)


def decode_utf8(data: bytes, name: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise fail(f"{name} is not valid UTF-8: {exc}") from exc


def format_weekend(days: frozenset[int]) -> str:
    if not days or len(days) > 3 or not days <= frozenset(range(7)):
        raise fail(f"weekend must hold 1 to 3 weekdays, got {sorted(days)}")
    return ",".join(WEEKDAY_TOKENS[day] for day in sorted(days))


def parse_weekend(raw: str) -> frozenset[int]:
    tokens = raw.split(",")
    if len(tokens) != len(set(tokens)) or any(token not in WEEKDAY_TOKENS for token in tokens):
        raise fail(f"bad weekend value {raw!r}")
    days = frozenset(WEEKDAY_TOKENS.index(token) for token in tokens)
    if format_weekend(days) != raw:
        raise fail(f"weekend value {raw!r} is not in Monday-first canonical order")
    return days


def check_input_hash(path: Path, expected: str) -> bytes:
    data = path.read_bytes()
    actual = sha256_hex(data)
    if actual != expected:
        raise fail(f"{path.name}: sha256 {actual} differs from pinned {expected}")
    return data


def load_mic_snapshot(data: bytes) -> dict[str, dict[str, str]]:
    reader = csv.DictReader(io.StringIO(decode_utf8(data, MIC_PATH.name), newline=""))
    if tuple(reader.fieldnames or ()) != MIC_HEADER:
        raise fail("iso10383_mic.csv header differs from the ISO 10383 column list")
    by_mic: dict[str, dict[str, str]] = {}
    for row in reader:
        mic = row["MIC"]
        if mic in by_mic:
            raise fail(f"duplicate MIC {mic} in the ISO snapshot")
        by_mic[mic] = dict(row)
    if len(by_mic) != EXPECTED_MIC_ROWS:
        raise fail(f"ISO snapshot has {len(by_mic)} rows, expected {EXPECTED_MIC_ROWS}")
    return by_mic


def load_currencies(
    data: bytes,
) -> tuple[list[tuple[str, int, str]], frozenset[str], frozenset[str]]:
    """Return (sorted rows, excluded N.A. codes, kept fund codes) from list one."""
    root = ET.fromstring(data)
    if root.tag != "ISO_4217" or root.get("Pblshd") != ISO4217_PUBLISHED:
        raise fail("list-one.xml is not the expected ISO_4217 publication")
    seen: dict[str, tuple[int, str]] = {}
    excluded: set[str] = set()
    funds: set[str] = set()
    for entry in root.iter("CcyNtry"):
        code = (entry.findtext("Ccy") or "").strip()
        if not code:
            # Territories with no universal currency (for example Antarctica) carry no code.
            continue
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise fail(f"malformed currency code {code!r} in list one")
        minor_text = (entry.findtext("CcyMnrUnts") or "").strip()
        name_el = entry.find("CcyNm")
        if name_el is None:
            raise fail(f"{code} has no CcyNm element")
        name = " ".join((name_el.text or "").split())
        if not minor_text.isdigit():
            excluded.add(code)
            continue
        if name_el.get("IsFund") == "true":
            funds.add(code)
        minor = int(minor_text)
        if code in seen and seen[code] != (minor, name):
            raise fail(f"{code} appears with conflicting minor unit or name")
        seen[code] = (minor, name)
    if excluded & set(seen):
        raise fail("a currency code is both numeric and N.A. in list one")
    rows = [(code, seen[code][0], seen[code][1]) for code in sorted(seen)]
    if len(rows) != EXPECTED_CURRENCY_ROWS:
        raise fail(f"list one yields {len(rows)} currencies, expected {EXPECTED_CURRENCY_ROWS}")
    if frozenset(excluded) != EXPECTED_NA_CODES:
        raise fail(f"N.A. codes {sorted(excluded)} differ from the documented exclusions")
    if frozenset(funds) != EXPECTED_FUND_CODES:
        raise fail(f"fund codes {sorted(funds)} differ from the documented decision")
    return rows, frozenset(excluded), frozenset(funds)


def load_matrix(data: bytes) -> tuple[dict[str, Any], list[dict[str, Any]], tuple[str, ...]]:
    matrix: dict[str, Any] = json.loads(decode_utf8(data, MATRIX_PATH.name))
    if matrix["meta"]["as_of"] != AS_OF:
        raise fail("market matrix as_of differs from AS_OF")
    operating = [row for row in matrix["markets"] if row["mic_operating"]]
    if len(operating) != EXPECTED_EXCHANGE_ROWS:
        raise fail(f"matrix has {len(operating)} operating MIC rows, expected 172")
    mics = [row["mic_operating"] for row in operating]
    if len(set(mics)) != len(mics):
        raise fail("duplicate operating MIC in the matrix")
    without_mic = tuple(matrix["meta"]["counts"]["rows_without_mic"])
    if len(without_mic) != 4:
        raise fail("matrix rows_without_mic does not list the four documented names")
    return matrix, operating, without_mic


def load_sanctions(data: bytes) -> dict[str, dict[str, str]]:
    reader = csv.DictReader(io.StringIO(decode_utf8(data, SANCTIONS_PATH.name), newline=""))
    if tuple(reader.fieldnames or ()) != SANCTIONS_HEADER:
        raise fail("sanctions.csv header differs from country,source,note")
    rows: dict[str, dict[str, str]] = {}
    for row in reader:
        if row["country"] in rows:
            raise fail(f"duplicate sanctions country {row['country']}")
        rows[row["country"]] = dict(row)
    if set(rows) != SANCTIONED_COUNTRIES:
        raise fail(f"sanctions.csv countries {sorted(rows)} differ from the documented set")
    return rows


def load_curation(
    data: bytes,
    operating: frozenset[str],
    currencies: frozenset[str],
    xc: ModuleType,
) -> dict[tuple[str, str], CurationRow]:
    reader = csv.DictReader(io.StringIO(decode_utf8(data, CURATION_PATH.name), newline=""))
    if tuple(reader.fieldnames or ()) != CURATION_HEADER:
        raise fail("curation.csv header differs from the documented columns")
    curation: dict[tuple[str, str], CurationRow] = {}
    for row in reader:
        mic, field = row["mic"], row["field"]
        value, evidence, source = row["value"], row["evidence"], row["source"]
        if mic not in operating:
            raise fail(f"curation row names unknown MIC {mic}")
        if field not in CURATION_FIELDS:
            raise fail(f"curation field {field!r} is not allowed")
        if evidence not in CURATION_FIELDS[field]:
            raise fail(f"evidence {evidence!r} is not allowed for field {field}")
        if (mic, field) in curation:
            raise fail(f"duplicate curation row for ({mic}, {field})")
        if evidence != "unverified" and not source:
            raise fail(f"curation row ({mic}, {field}) has evidence but no source")
        _validate_curated_value(mic, field, value, currencies, xc)
        curation[(mic, field)] = CurationRow(value, evidence, source)
    return curation


def _validate_curated_value(
    mic: str, field: str, value: str, currencies: frozenset[str], xc: ModuleType
) -> None:
    if field == "weekend":
        parse_weekend(value)
    elif field == "close_local":
        if not re.fullmatch(r"\d{2}:\d{2}", value):
            raise fail(f"close_local {value!r} for {mic} is not HH:MM")
        time.fromisoformat(value)
    elif field == "timezone":
        if value not in zoneinfo.available_timezones():
            raise fail(f"timezone {value!r} for {mic} is unknown")
    elif field == "currency":
        if value not in currencies:
            raise fail(f"currency {value!r} for {mic} is not in ISO 4217")
    elif field == "xcal_code":
        try:
            alias = xc.resolve_alias(mic)
        except Exception as exc:
            raise fail(f"library has no alias for {mic}") from exc
        if alias != value:
            raise fail(f"resolve_alias({mic}) is {alias!r}, curation says {value!r}")


def library_facts(xc: ModuleType, code: str) -> CalendarFacts:
    """Weekend and regular close of a library calendar as of AS_OF."""
    calendar = xc.get_calendar(code, start=LIBRARY_PROBE_WINDOW[0], end=LIBRARY_PROBE_WINDOW[1])
    as_of = date.fromisoformat(AS_OF)
    mask: str = calendar.weekmask
    specials = getattr(calendar, "special_weekmasks", None) or ()
    covering = [
        special_mask
        for start, end, special_mask in specials
        if (start is None or start.date() <= as_of) and (end is None or end.date() >= as_of)
    ]
    if covering:
        mask = covering[-1]
    if len(mask) != 7 or set(mask) - {"0", "1"}:
        raise fail(f"{code} weekmask {mask!r} is malformed")
    weekend = frozenset(index for index, char in enumerate(mask) if char == "0")
    closes = [
        close for start, close in calendar.close_times if start is None or start.date() <= as_of
    ]
    if not closes:
        raise fail(f"{code} has no close time in force on {AS_OF}")
    return CalendarFacts(weekend=weekend, close_local=closes[-1])


def _time_text(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def build_exchange_rows(
    operating: list[dict[str, Any]],
    snapshot: dict[str, dict[str, str]],
    curation: dict[tuple[str, str], CurationRow],
    currencies: frozenset[str],
    sanctions: dict[str, dict[str, str]],
    xc: ModuleType,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for matrix_row in operating:
        mic = matrix_row["mic_operating"]
        upstream = snapshot.get(mic)
        if upstream is None:
            raise fail(f"{mic} is not in the ISO snapshot")
        if upstream["STATUS"] not in {"ACTIVE", "UPDATED"} or upstream["OPRT/SGMT"] != "OPRT":
            raise fail(f"{mic} is not an active operating MIC in the snapshot")
        country = matrix_row["iso2"]
        if country != upstream["ISO COUNTRY CODE (ISO 3166)"]:
            raise fail(f"{mic} country differs between matrix and snapshot")
        notes: list[str] = []

        # Currency and timezone, with curation only where the matrix is empty.
        currency = matrix_row["currency"]
        currency_source = ""
        if currency is None:
            row_currency = curation.get((mic, "currency"))
            if row_currency is None:
                raise fail(f"{mic} has no currency in the matrix or in curation")
            currency = row_currency.value
            currency_source = row_currency.evidence
        elif (mic, "currency") in curation:
            raise fail(f"{mic} has both a matrix currency and a curation override")
        if currency not in currencies:
            raise fail(f"{mic} currency {currency!r} is not in ISO 4217")

        timezone_name = matrix_row["timezone"]
        curated_tz = curation.get((mic, "timezone"))
        if timezone_name is None:
            if curated_tz is None:
                raise fail(f"{mic} has no timezone in the matrix or in curation")
            timezone_name = curated_tz.value
        elif curated_tz is not None:
            raise fail(f"{mic} has both a matrix timezone and a curation override")

        # Quote scale from the matrix quote-unit text, by its ASCII prefix.
        quote_unit = matrix_row["minor_unit_quote"]["quote_unit"]
        quote_token = ""
        if quote_unit == "major":
            quote_scale = 0
        else:
            token = (quote_unit or "").split(" ", 1)[0]
            if token not in MINOR_QUOTE_TOKENS:
                raise fail(f"{mic} quote unit {quote_unit!r} is not recognised")
            if MINOR_QUOTE_TOKENS[token] != currency:
                raise fail(f"{mic} quote token {token} does not match currency {currency}")
            if quote_unit.startswith(token + " ("):
                quote_scale = 2
                quote_token = "quote_unit:verified-minor"
            else:
                quote_scale = 0
                quote_token = "quote_unit:unverified"

        # Weekend and close, in the order the spec gives: library, curated, default.
        matrix_xcal = matrix_row["calendar_source"].get("exchange_calendars_4_13_2")
        curated_xcal = curation.get((mic, "xcal_code"))
        if matrix_xcal and curated_xcal is not None:
            raise fail(f"{mic} has both a matrix xcal_code and a curated one")
        if matrix_xcal:
            xcal_code: str | None = matrix_xcal
            xcal_token = "xcal:matrix"
        elif curated_xcal is not None:
            xcal_code = curated_xcal.value
            xcal_token = "xcal:alias"
        else:
            xcal_code = None
            xcal_token = ""

        if xcal_code:
            if (mic, "weekend") in curation or (mic, "close_local") in curation:
                raise fail(f"{mic} is a library row; curated weekend or close is refused")
            facts = library_facts(xc, xcal_code)
            weekend = facts.weekend
            close_text = _time_text(facts.close_local)
            notes.append("weekend:library")
            notes.append("close_local:library")
        else:
            curated_weekend = curation.get((mic, "weekend"))
            if curated_weekend is not None:
                weekend = parse_weekend(curated_weekend.value)
                notes.append(f"weekend:{curated_weekend.evidence}")
            else:
                weekend = DEFAULT_WEEKEND
                notes.append("weekend:unverified")
            curated_close = curation.get((mic, "close_local"))
            if curated_close is not None:
                close_text = curated_close.value
                notes.append(f"close_local:{curated_close.evidence}")
            else:
                close_text = ""
                notes.append("close_local:unknown")
        if xcal_token:
            notes.append(xcal_token)
        if quote_token:
            notes.append(quote_token)
        if curated_tz is not None:
            notes.append("timezone:curated")
        if currency_source:
            notes.append(
                "currency:unverified" if currency_source == "unverified" else "currency:curated"
            )

        sanctioned = country in SANCTIONED_COUNTRIES
        if sanctioned:
            notes.append("sanctions:default-closed")
        elif country in TARGETED_COUNTRIES:
            notes.append("sanctions:targeted")

        rows.append(
            {
                "mic": mic,
                "name": upstream["MARKET NAME-INSTITUTION DESCRIPTION"],
                "country": country,
                "timezone": timezone_name,
                "currency": currency,
                "quote_scale": str(quote_scale),
                "weekend": format_weekend(weekend),
                "close_local": close_text,
                "xcal_code": xcal_code or "",
                "tier_research": matrix_row["tier"],
                "sanctioned": "true" if sanctioned else "false",
                "notes": "; ".join(notes),
            }
        )
    rows.sort(key=lambda row: row["mic"])
    _check_expected_shape(rows)
    return rows


def _check_expected_shape(rows: list[dict[str, str]]) -> None:
    if len(rows) != EXPECTED_EXCHANGE_ROWS:
        raise fail(f"{len(rows)} exchange rows, expected {EXPECTED_EXCHANGE_ROWS}")
    xcal_rows = sum(1 for row in rows if row["xcal_code"])
    if xcal_rows != EXPECTED_XCAL_ROWS:
        raise fail(f"{xcal_rows} xcal_code rows, expected {EXPECTED_XCAL_ROWS}")
    scale_two = {row["mic"] for row in rows if row["quote_scale"] == "2"}
    if scale_two != EXPECTED_SCALE_2:
        raise fail(f"quote_scale 2 rows {sorted(scale_two)} differ from the documented set")
    sanctioned = {row["mic"] for row in rows if row["sanctioned"] == "true"}
    if len(sanctioned) != EXPECTED_SANCTIONED_ROWS:
        raise fail(f"{len(sanctioned)} sanctioned rows, expected {EXPECTED_SANCTIONED_ROWS}")


def _csv_bytes(
    header: tuple[str, ...], rows: list[dict[str, str]] | list[tuple[Any, ...]]
) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        if isinstance(row, dict):
            writer.writerow([row[column] for column in header])
        else:
            writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def build_outputs(xc: ModuleType) -> dict[str, bytes]:
    """Build every generated file, in memory, from the pinned inputs."""
    mic_bytes = check_input_hash(MIC_PATH, MIC_SHA256)
    xml_bytes = check_input_hash(ISO_XML_PATH, ISO4217_XML_SHA256)
    matrix_bytes = MATRIX_PATH.read_bytes()
    matrix_lf_sha = sha256_hex(matrix_bytes.replace(b"\r\n", b"\n"))
    sanctions_bytes = SANCTIONS_PATH.read_bytes()
    curation_bytes = CURATION_PATH.read_bytes()

    currency_rows, excluded_na, kept_funds = load_currencies(xml_bytes)
    currencies = frozenset(code for code, _, _ in currency_rows)
    snapshot = load_mic_snapshot(mic_bytes)
    _, operating, without_mic = load_matrix(matrix_bytes)
    sanctions = load_sanctions(sanctions_bytes)
    operating_mics = frozenset(row["mic_operating"] for row in operating)
    curation = load_curation(curation_bytes, operating_mics, currencies, xc)
    exchange_rows = build_exchange_rows(operating, snapshot, curation, currencies, sanctions, xc)

    iso4217_bytes = _csv_bytes(ISO4217_HEADER, [(c, m, n) for c, m, n in currency_rows])
    exchanges_bytes = _csv_bytes(EXCHANGE_COLUMNS, exchange_rows)
    sanctions_sha = sha256_hex(sanctions_bytes)
    manifest = _manifest(
        xc=xc,
        matrix_lf_sha=matrix_lf_sha,
        without_mic=without_mic,
        excluded_na=excluded_na,
        kept_funds=kept_funds,
        generated={
            "exchanges.csv": (exchanges_bytes, len(exchange_rows)),
            "iso4217.csv": (iso4217_bytes, len(currency_rows)),
        },
        mic_sha=sha256_hex(mic_bytes),
        sanctions_sha=sanctions_sha,
        curation_sha=sha256_hex(curation_bytes),
    )
    manifest_bytes = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    return {
        "iso4217.csv": iso4217_bytes,
        "exchanges.csv": exchanges_bytes,
        "MANIFEST.json": manifest_bytes,
    }


def _manifest(
    *,
    xc: ModuleType,
    matrix_lf_sha: str,
    without_mic: tuple[str, ...],
    excluded_na: frozenset[str],
    kept_funds: frozenset[str],
    generated: dict[str, tuple[bytes, int]],
    mic_sha: str,
    sanctions_sha: str,
    curation_sha: str,
) -> dict[str, Any]:
    exchanges_bytes, exchange_count = generated["exchanges.csv"]
    iso_bytes, iso_count = generated["iso4217.csv"]
    return {
        "as_of": AS_OF,
        "generator": "scripts/build_markets_reference.py",
        "files": {
            "exchanges.csv": {
                "sha256": sha256_hex(exchanges_bytes),
                "rows": exchange_count,
                "retrieved": RETRIEVED,
                "source": MATRIX_REL,
                "source_sha256_lf": matrix_lf_sha,
                "curation": CURATION_REL,
                "curation_sha256": curation_sha,
                "exchange_calendars_version": str(xc.__version__),
                "excluded_without_mic": list(without_mic),
                "license": EXCHANGES_LICENSE,
                "notes": (
                    "172 operating MICs from the research matrix, joined to the ISO 10383 "
                    "snapshot. Weekend and close of the 64 rows with xcal_code come from "
                    "exchange_calendars (XNAS->XNYS and XTSX->XTSE via the library alias "
                    "table); curated rows come from curation.csv; all other rows are "
                    "SAT,SUN with weekend:unverified. Sanctioned rows follow sanctions.csv "
                    "(default-closed). Quote scale 2 only for XJSE, XLON and XTAE."
                ),
            },
            "iso10383_mic.csv": {
                "sha256": mic_sha,
                "rows": EXPECTED_MIC_ROWS,
                "retrieved": RETRIEVED,
                "url": MIC_URL,
                "last_update": MIC_LAST_UPDATE,
                "license": ISO10383_LICENSE,
                "notes": "Verbatim, unmodified.",
            },
            "iso4217.csv": {
                "sha256": sha256_hex(iso_bytes),
                "rows": iso_count,
                "retrieved": RETRIEVED,
                "published": ISO4217_PUBLISHED,
                "url": ISO4217_URL,
                "source_sha256": ISO4217_XML_SHA256,
                "license": ISO4217_LICENSE,
                "notes": (
                    "Derived from list one. Excluded the "
                    f"{len(excluded_na)} codes with minor unit N.A.: "
                    f"{', '.join(sorted(excluded_na))}. Kept the {len(kept_funds)} fund codes "
                    f"(IsFund=true) because they denominate real instruments: "
                    f"{', '.join(sorted(kept_funds))}. Withdrawn codes BGN, HRK, ZWL and SLL "
                    "are absent because list one no longer carries them."
                ),
            },
            "sanctions.csv": {
                "sha256": sanctions_sha,
                "rows": EXPECTED_SANCTIONED_ROWS,
                "retrieved": RETRIEVED,
                "source": "curated",
                "license": SANCTIONS_LICENSE,
                "notes": (
                    "Default-closed countries (spec 3.1). Not legal advice. Targeted "
                    "programmes (VE, MM, SD, LY) are warned in exchanges.csv notes only."
                ),
            },
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="build in memory and compare with the files on disk; exit 1 on any difference",
    )
    args = parser.parse_args(argv)
    try:
        import exchange_calendars as xc
    except ImportError:
        print(INSTALL_HINT, file=sys.stderr)
        return 2
    try:
        outputs = build_outputs(xc)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.check:
        differing = [
            name
            for name, data in outputs.items()
            if not (DATA_DIR / name).is_file() or (DATA_DIR / name).read_bytes() != data
        ]
        if differing:
            print("differs: " + ", ".join(differing))
            return 1
        print("up to date: " + ", ".join(outputs))
        return 0
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name, data in outputs.items():
        (DATA_DIR / name).write_bytes(data)
        print(f"wrote {name} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
