"""MicRegistry behaviour against the packaged ISO 10383 snapshot."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest

from crocodile.core.markets import mic
from crocodile.core.markets.errors import ExpiredMarketError, ReferenceDataError, UnknownMarketError
from crocodile.core.markets.mic import MicEntry, MicRegistry

pytestmark = pytest.mark.usefixtures("network_forbidden")


def test_get_returns_the_registry_row() -> None:
    row = MicRegistry().get("XIST")
    assert row.mic == "XIST"
    assert row.operating_mic == "XIST"
    assert row.is_segment is False
    assert row.country == "TR"
    assert row.category == "NSPD"
    assert row.status == "ACTIVE"
    assert row.city == "ISTANBUL"
    assert row.is_active is True


def test_operating_maps_segments_to_their_operating_mic() -> None:
    registry = MicRegistry()
    expected = {
        "XEQY": "XIST",
        "XTKS": "XJPX",
        "XMAD": "BMEX",
        "XNGS": "XNAS",
        "ARCX": "XNYS",
        "XIST": "XIST",
    }
    for segment, operating in expected.items():
        assert registry.operating(segment) == operating, segment
    assert registry.get("XEQY").is_segment is True


def test_require_active_rejects_unknown_codes() -> None:
    with pytest.raises(UnknownMarketError) as caught:
        MicRegistry().require_active("ZZZZ")
    assert "ZZZZ" in str(caught.value)


def test_require_active_rejects_expired_codes_with_the_registry_redirect() -> None:
    with pytest.raises(ExpiredMarketError) as caught:
        MicRegistry().require_active("XTUR")
    err = caught.value
    assert "XIST" in str(err)
    assert err.replacement == "XIST"
    assert err.expiry_date == date(2013, 8, 26)
    assert err.comments == "MIC TO USE IS XIST"


def test_updated_status_counts_as_active() -> None:
    assert MicRegistry().require_active("INSE").status == "UPDATED"


def test_get_does_not_raise_for_expired_entries() -> None:
    row = MicRegistry().get("XTUR")
    assert row.status == "EXPIRED"
    assert row.is_active is False


def test_lookups_are_case_sensitive_and_suggest_the_upper_case_code() -> None:
    with pytest.raises(UnknownMarketError) as caught:
        MicRegistry().get("xist")
    assert "XIST" in str(caught.value)
    assert "xist" not in MicRegistry()


def test_the_packaged_snapshot_loads_lazily_and_once() -> None:
    mic._packaged_index.cache_clear()
    registry = MicRegistry()
    assert mic._packaged_index.cache_info().currsize == 0
    registry.get("XIST")
    registry.require_active("XIST")
    MicRegistry().get("XIST")
    info = mic._packaged_index.cache_info()
    assert info.misses == 1
    assert info.currsize == 1


def test_an_injected_table_replaces_the_snapshot() -> None:
    entry = MicEntry(
        mic="AAAA",
        operating_mic="AAAA",
        is_segment=False,
        name="Injected Exchange",
        category="RMKT",
        country="ZZ",
        city="Nowhere",
        website="",
        status="ACTIVE",
        comments="",
    )
    registry = MicRegistry([entry])
    assert registry.get("AAAA") == entry
    assert len(registry) == 1
    assert "XIST" not in registry
    with pytest.raises(UnknownMarketError):
        registry.get("XIST")


def test_entries_are_sorted_by_mic() -> None:
    mics = [entry.mic for entry in MicRegistry().entries()]
    assert mics == sorted(mics)
    assert len(mics) == 2889


def test_a_duplicate_mic_in_the_source_is_a_reference_data_error() -> None:
    row = {
        "MIC": "AAAA",
        "OPERATING MIC": "AAAA",
        "OPRT/SGMT": "OPRT",
        "MARKET NAME-INSTITUTION DESCRIPTION": "Dup",
        "MARKET CATEGORY CODE": "RMKT",
        "ISO COUNTRY CODE (ISO 3166)": "ZZ",
        "CITY": "Nowhere",
        "WEBSITE": "",
        "STATUS": "ACTIVE",
        "EXPIRY DATE": "",
        "COMMENTS": "",
    }
    with pytest.raises(ReferenceDataError):
        mic.parse_mic_rows([row, dict(row)])


def _mic_row(mic_code: str = "XIST", **overrides: str) -> dict[str, str]:
    row = {column: "" for column in mic.MIC_HEADER}
    row.update(
        {
            "MIC": mic_code,
            "OPERATING MIC": mic_code,
            "OPRT/SGMT": "OPRT",
            "MARKET NAME-INSTITUTION DESCRIPTION": "BORSA ISTANBUL",
            "MARKET CATEGORY CODE": "NSPD",
            "ISO COUNTRY CODE (ISO 3166)": "TR",
            "STATUS": "ACTIVE",
        }
    )
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("20130826", date(2013, 8, 26)), ("", None)],
    ids=["a-date", "empty"],
)
def test_expiry_reads_yyyymmdd_or_nothing(raw: str, expected: date | None) -> None:
    assert mic._expiry(raw, "XTUR") == expected


@pytest.mark.parametrize(
    "raw",
    ["2013-08-26", "2013082", "abcdefgh", "20131301", "20130230"],
    ids=["dashed", "seven-digits", "letters", "month-13", "no-such-day"],
)
def test_expiry_refuses_a_value_that_is_not_a_yyyymmdd_date(raw: str) -> None:
    with pytest.raises(ReferenceDataError) as caught:
        mic._expiry(raw, "XTUR")
    assert "XTUR" in str(caught.value)


def test_parse_mic_rows_refuses_a_duplicate_mic() -> None:
    with pytest.raises(ReferenceDataError) as caught:
        mic.parse_mic_rows([_mic_row("XIST"), _mic_row("XIST")])
    assert "duplicate MIC XIST" in str(caught.value)


def test_parse_mic_rows_refuses_a_malformed_expiry_date() -> None:
    with pytest.raises(ReferenceDataError):
        mic.parse_mic_rows([_mic_row("XTUR", **{"EXPIRY DATE": "2013-08-26"})])


@pytest.fixture
def fresh_mic_index() -> Iterator[None]:
    mic._packaged_index.cache_clear()
    yield
    mic._packaged_index.cache_clear()


def test_the_packaged_mic_loader_refuses_a_duplicate_row(
    fresh_mic_index: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [_mic_row("XIST"), _mic_row("XIST")]
    monkeypatch.setattr(mic, "read_csv", lambda name, header: rows)
    with pytest.raises(ReferenceDataError):
        mic._packaged_index()
