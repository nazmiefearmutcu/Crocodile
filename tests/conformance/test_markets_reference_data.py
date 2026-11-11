"""Gate: the packaged core/markets reference data is what its manifest says it is.

The gate reads the package data through ``importlib.resources`` and imports no
``crocodile.core.markets`` module, so it stays green on its own while the loaders
are still being written. Every read passes ``encoding="utf-8"``.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import zoneinfo
from collections import Counter
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

DATA = files("crocodile.core.markets") / "data"
_REPO = Path(__file__).resolve().parents[2]
_BUILD_SCRIPT = _REPO / "scripts" / "build_markets_reference.py"

MIC_SNAPSHOT_SHA256 = "09a0d07b7b8348147453a2d8b18bc6638e3c5b16f749462582c9b1edf0fd0e38"
MIC_FILE_HEADER = (
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
EXCHANGE_HEADER = (
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
WEEKDAY_ORDER = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
EXCLUDED_WITHOUT_MIC = (
    "Ethiopian Securities Exchange (ESX)",
    "BVMAC regional market (Libreville, unverified) and Douala Stock Exchange (Cameroon)",
    "Royal Securities Exchange of Bhutan (RSEB)",
    "Yangon Stock Exchange (YSX)",
)
WITHDRAWN_OR_NON_CURRENCY = (
    "BGN",
    "HRK",
    "ZWL",
    "SLL",
    "XAG",
    "XAU",
    "XBA",
    "XBB",
    "XBC",
    "XBD",
    "XDR",
    "XPD",
    "XPT",
    "XSU",
    "XTS",
    "XUA",
    "XXX",
)


def _bytes(name: str) -> bytes:
    return (DATA / name).read_bytes()


def _text(name: str) -> str:
    return _bytes(name).decode("utf-8")


def _manifest() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_text("MANIFEST.json"))
    return data


def _csv_rows(name: str) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(_text(name), newline=""))
    header = tuple(reader.fieldnames or ())
    rows: list[dict[str, str]] = [dict(row) for row in reader]
    return header, rows


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _exchange_rows() -> dict[str, dict[str, str]]:
    _, rows = _csv_rows("exchanges.csv")
    return {row["mic"]: row for row in rows}


def _notes(row: dict[str, str]) -> list[str]:
    return [token for token in row["notes"].split("; ") if token]


def test_the_data_directory_holds_exactly_the_manifest_files() -> None:
    names = {entry.name for entry in DATA.iterdir() if entry.is_file()}
    expected = set(_manifest()["files"]) | {"MANIFEST.json"}
    assert names == expected


def test_every_data_file_matches_its_manifest_sha256() -> None:
    files = _manifest()["files"]
    assert set(files) == {
        "exchanges.csv",
        "grammar_google_finance.csv",
        "grammar_tradingview.csv",
        "grammar_yahoo_chart.csv",
        "iso10383_mic.csv",
        "iso4217.csv",
        "sanctions.csv",
    }
    for name, entry in files.items():
        assert _sha256(_bytes(name)) == entry["sha256"], name


def test_data_files_are_utf8_lf_without_bom() -> None:
    for name in ("iso4217.csv", "exchanges.csv", "sanctions.csv", "MANIFEST.json"):
        raw = _bytes(name)
        assert not raw.startswith(b"\xef\xbb\xbf"), name
        assert b"\r" not in raw, name
        raw.decode("utf-8")
        assert raw.endswith(b"\n"), name


def test_the_iso_snapshot_keeps_its_published_crlf_and_has_no_bom() -> None:
    raw = _bytes("iso10383_mic.csv")
    assert not raw.startswith(b"\xef\xbb\xbf")
    raw.decode("utf-8")
    assert raw.count(b"\r\n") == 2890
    assert raw.count(b"\n") == raw.count(b"\r\n")


def test_the_mic_snapshot_is_the_verbatim_iso_file() -> None:
    raw = _bytes("iso10383_mic.csv")
    assert _sha256(raw) == MIC_SNAPSHOT_SHA256
    header, rows = _csv_rows("iso10383_mic.csv")
    assert header == MIC_FILE_HEADER
    assert len(rows) == 2889
    counts = Counter(row["STATUS"] for row in rows)
    assert counts == Counter({"ACTIVE": 2313, "EXPIRED": 574, "UPDATED": 2})


def test_iso4217_has_the_documented_shape() -> None:
    header, rows = _csv_rows("iso4217.csv")
    assert header == ISO4217_HEADER
    assert len(rows) == 165
    codes = [row["code"] for row in rows]
    assert len(set(codes)) == len(codes)
    assert codes == sorted(codes)
    for code in codes:
        assert re.fullmatch(r"[A-Z]{3}", code), code
    assert {row["minor_unit"] for row in rows} <= {"0", "2", "3", "4"}
    by_code = {row["code"]: row["minor_unit"] for row in rows}
    assert by_code["JPY"] == "0"
    assert by_code["EUR"] == "2"
    assert by_code["KWD"] == "3"
    assert by_code["CLF"] == "4"
    for code in ("EUR", "TRY", "GBP", "ZAR", "ILS"):
        assert code in by_code, code


def test_iso4217_excludes_withdrawn_and_non_currency_codes() -> None:
    _, rows = _csv_rows("iso4217.csv")
    codes = {row["code"] for row in rows}
    for code in WITHDRAWN_OR_NON_CURRENCY:
        assert code not in codes, code


def test_exchanges_csv_has_the_spec_columns_in_order() -> None:
    header, _ = _csv_rows("exchanges.csv")
    assert header == EXCHANGE_HEADER


def test_exchanges_csv_has_172_unique_sorted_operating_mics() -> None:
    _, rows = _csv_rows("exchanges.csv")
    mics = [row["mic"] for row in rows]
    assert len(mics) == 172
    assert len(set(mics)) == 172
    assert mics == sorted(mics)


def test_every_exchange_mic_is_active_and_operating_in_the_snapshot() -> None:
    _, snapshot = _csv_rows("iso10383_mic.csv")
    by_mic = {row["MIC"]: row for row in snapshot}
    for mic in _exchange_rows():
        row = by_mic[mic]
        assert row["STATUS"] in {"ACTIVE", "UPDATED"}, mic
        assert row["OPRT/SGMT"] == "OPRT", mic


def test_exchange_country_and_name_agree_with_the_snapshot() -> None:
    _, snapshot = _csv_rows("iso10383_mic.csv")
    by_mic = {row["MIC"]: row for row in snapshot}
    for mic, row in _exchange_rows().items():
        upstream = by_mic[mic]
        assert row["country"] == upstream["ISO COUNTRY CODE (ISO 3166)"], mic
        assert row["name"] == upstream["MARKET NAME-INSTITUTION DESCRIPTION"], mic


def test_every_exchange_timezone_exists_in_zoneinfo() -> None:
    available = zoneinfo.available_timezones()
    for mic, row in _exchange_rows().items():
        key = row["timezone"]
        assert key in available, (mic, key)
        zoneinfo.ZoneInfo(key)


def test_every_exchange_currency_is_in_iso4217() -> None:
    _, currencies = _csv_rows("iso4217.csv")
    known = {row["code"] for row in currencies}
    for mic, row in _exchange_rows().items():
        assert row["currency"] in known, (mic, row["currency"])


def test_quote_scale_is_0_or_2_and_2_only_where_a_minor_unit_quote_was_verified() -> None:
    rows = _exchange_rows()
    for mic, row in rows.items():
        assert row["quote_scale"] in {"0", "2"}, mic
    assert {mic for mic, row in rows.items() if row["quote_scale"] == "2"} == {
        "XJSE",
        "XLON",
        "XTAE",
    }
    assert "quote_unit:unverified" in _notes(rows["AQSE"])


def test_weekend_close_and_tier_fields_are_well_formed() -> None:
    for mic, row in _exchange_rows().items():
        days = row["weekend"].split(",")
        assert 1 <= len(days) <= 3, mic
        assert all(day in WEEKDAY_ORDER for day in days), mic
        positions = [WEEKDAY_ORDER.index(day) for day in days]
        assert positions == sorted(set(positions)), mic
        assert row["close_local"] == "" or re.fullmatch(r"\d{2}:\d{2}", row["close_local"]), mic
        assert row["tier_research"] in {"A", "B", "C", "D"}, mic
        assert row["sanctioned"] in {"true", "false"}, mic


def test_every_row_states_where_its_weekend_and_close_came_from() -> None:
    for mic, row in _exchange_rows().items():
        notes = _notes(row)
        weekend_tokens = [token for token in notes if token.startswith("weekend:")]
        close_tokens = [token for token in notes if token.startswith("close_local:")]
        assert len(weekend_tokens) == 1, mic
        assert len(close_tokens) == 1, mic
        has_library = bool(row["xcal_code"])
        assert ("weekend:library" in notes) == has_library, mic
        assert ("close_local:library" in notes) == has_library, mic
        if "weekend:unverified" in notes:
            assert row["weekend"] == "SAT,SUN", mic
        assert ("close_local:unknown" in notes) == (row["close_local"] == ""), mic


def test_spot_values_match_the_measured_reference() -> None:
    rows = _exchange_rows()
    assert rows["XSAU"]["weekend"] == "FRI,SAT"
    assert "weekend:library" in _notes(rows["XSAU"])
    assert rows["XCAI"]["weekend"] == "FRI,SAT"
    assert "weekend:secondary" in _notes(rows["XCAI"])
    assert rows["XTAE"]["weekend"] == "SAT,SUN"
    assert "weekend:library" in _notes(rows["XTAE"])
    assert rows["XIST"]["close_local"] == "18:00"
    assert rows["XJPX"]["xcal_code"] == "XTKS"
    assert rows["BMEX"]["xcal_code"] == "XMAD"
    assert rows["MISX"]["xcal_code"] == "XMOS"
    assert rows["XNAS"]["xcal_code"] == "XNYS"
    assert "xcal:alias" in _notes(rows["XNAS"])
    assert rows["XTSX"]["xcal_code"] == "XTSE"
    assert "xcal:alias" in _notes(rows["XTSX"])
    assert rows["AIXK"]["timezone"] == "Asia/Almaty"
    assert "timezone:curated" in _notes(rows["AIXK"])
    assert rows["XBUL"]["currency"] == "EUR"
    assert sum(1 for row in rows.values() if row["xcal_code"]) == 68


def test_sanctions_csv_lists_exactly_the_comprehensive_programme_countries() -> None:
    header, rows = _csv_rows("sanctions.csv")
    assert header == SANCTIONS_HEADER
    assert {row["country"] for row in rows} == {"BY", "IR", "KP", "RU", "SY"}
    for row in rows:
        assert row["source"].startswith("https://"), row["country"]
        assert "Not legal advice" in row["note"], row["country"]


def test_the_sanctioned_column_follows_sanctions_csv() -> None:
    rows = _exchange_rows()
    flagged = {mic for mic, row in rows.items() if row["sanctioned"] == "true"}
    assert flagged == {"BCSE", "MISX", "SPBE", "XDSE", "XTEH"}
    for mic in flagged:
        assert "sanctions:default-closed" in _notes(rows[mic]), mic


def test_targeted_sanctions_are_warned_in_notes_only() -> None:
    rows = _exchange_rows()
    for mic in ("BVCA", "XKHA", "XLSM"):
        assert rows[mic]["sanctioned"] == "false", mic
        assert "sanctions:targeted" in _notes(rows[mic]), mic


SUNDAY_TO_THURSDAY_MICS = ("DSMD", "XKUW", "XBAH", "XAMM", "XCHG", "XPAE", "XKHA")
SIBLING_ALIASES = {"BJSE": "XSHG", "XCNQ": "XTSE", "XNGM": "XSTO", "XSHE": "XSHG"}


@pytest.mark.parametrize("mic", SUNDAY_TO_THURSDAY_MICS)
def test_markets_that_trade_sunday_to_thursday_have_a_friday_saturday_weekend(mic: str) -> None:
    row = _exchange_rows()[mic]
    assert row["weekend"] == "FRI,SAT", mic
    notes = _notes(row)
    assert "weekend:primary" in notes or "weekend:secondary" in notes, mic


def test_tehran_trades_saturday_to_wednesday() -> None:
    row = _exchange_rows()["XTEH"]
    assert row["weekend"] == "THU,FRI"
    notes = _notes(row)
    assert "weekend:primary" in notes or "weekend:secondary" in notes


@pytest.mark.parametrize(("mic", "library"), sorted(SIBLING_ALIASES.items()))
def test_sibling_venues_use_the_library_calendar_they_share(mic: str, library: str) -> None:
    row = _exchange_rows()[mic]
    assert row["xcal_code"] == library, mic
    notes = _notes(row)
    assert "xcal:sibling" in notes, mic
    assert "weekend:library" in notes and "close_local:library" in notes, mic


def test_the_manifest_lists_the_four_exchanges_without_a_mic() -> None:
    entry = _manifest()["files"]["exchanges.csv"]
    assert tuple(entry["excluded_without_mic"]) == EXCLUDED_WITHOUT_MIC


def test_every_manifest_entry_records_hash_rows_date_and_licence() -> None:
    files = _manifest()["files"]
    assert _manifest()["as_of"] == "2026-11-08"
    expected_rows = {
        "exchanges.csv": 172,
        "grammar_google_finance.csv": 3,
        "grammar_tradingview.csv": 13,
        "grammar_yahoo_chart.csv": 55,
        "iso10383_mic.csv": 2889,
        "iso4217.csv": 165,
        "sanctions.csv": 5,
    }
    for name, entry in files.items():
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]), name
        assert entry["rows"] == expected_rows[name], name
        _, rows = _csv_rows(name)
        assert len(rows) == entry["rows"], name
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["retrieved"]), name
        assert entry["license"], name
        assert "url" in entry or "source" in entry, name


@pytest.mark.parametrize("name", ["exchanges.csv", "iso4217.csv", "sanctions.csv"])
def test_generated_and_hand_written_csv_headers_are_exact(name: str) -> None:
    expected = {
        "exchanges.csv": EXCHANGE_HEADER,
        "iso4217.csv": ISO4217_HEADER,
        "sanctions.csv": SANCTIONS_HEADER,
    }[name]
    header, _ = _csv_rows(name)
    assert header == expected


_LOAD_SCRIPT_CODE = (
    "import importlib.util, sys\n"
    "spec = importlib.util.spec_from_file_location('build_markets_reference', sys.argv[1])\n"
    "module = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(module)\n"
    "assert callable(module.main)\n"
    "print('exchange_calendars' in sys.modules)\n"
)


def test_the_build_script_loads_with_the_standard_importlib_recipe() -> None:
    # The recipe of task 05's gate: spec_from_file_location, module_from_spec and
    # exec_module, with no sys.modules registration. A fresh interpreter keeps this
    # test from depending on what earlier tests imported.
    result = subprocess.run(
        [sys.executable, "-c", _LOAD_SCRIPT_CODE, str(_BUILD_SCRIPT)],
        cwd=_REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", "loading the script must not import exchange_calendars"


def test_the_build_script_is_loadable_in_process_without_sys_modules() -> None:
    spec = importlib.util.spec_from_file_location("_probe_build_markets_reference", _BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.main)
    assert "_probe_build_markets_reference" not in sys.modules
