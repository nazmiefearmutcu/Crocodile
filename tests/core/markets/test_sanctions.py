"""Default-closed sanctions policy: country by MIC, the allow-list and the setting."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest

from crocodile.core.config import Settings
from crocodile.core.errors import ConfigError
from crocodile.core.markets import sanctions as sanctions_module
from crocodile.core.markets.errors import (
    ReferenceDataError,
    SanctionedMarketError,
    UnknownMarketError,
)
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.mic import MicEntry, MicRegistry
from crocodile.core.markets.sanctions import (
    allowed_countries,
    is_sanctioned,
    parse_allowed_countries,
    require_not_sanctioned,
    sanctioned_countries,
)

pytestmark = pytest.mark.usefixtures("network_forbidden")

ALLOW_NAME = "CROCODILE_ALLOW_SANCTIONED_MARKETS"


@pytest.fixture
def sanctions_rows(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, str]]]:
    """Replace the packaged sanctions rows and clear the loader cache around the test."""
    sanctioned_countries.cache_clear()
    rows: list[dict[str, str]] = []

    def fake_read_csv(name: str, header: Sequence[str]) -> list[dict[str, str]]:
        assert name == "sanctions.csv"
        return list(rows)

    monkeypatch.setattr(sanctions_module, "read_csv", fake_read_csv)
    yield rows
    sanctioned_countries.cache_clear()


def _row(country: str) -> dict[str, str]:
    return {"country": country, "source": "https://example.test/programme", "note": "Test."}


@pytest.mark.parametrize("country", ["ru", "RUS"])
def test_a_malformed_country_in_sanctions_csv_is_refused(
    sanctions_rows: list[dict[str, str]], country: str
) -> None:
    sanctions_rows.append(_row(country))
    with pytest.raises(ReferenceDataError) as info:
        sanctioned_countries()
    text = str(info.value)
    assert "sanctions.csv" in text
    assert "line 2" in text
    assert repr(country) in text


def test_a_duplicate_country_in_sanctions_csv_is_refused(
    sanctions_rows: list[dict[str, str]],
) -> None:
    sanctions_rows.extend([_row("RU"), _row("RU")])
    with pytest.raises(ReferenceDataError) as info:
        sanctioned_countries()
    text = str(info.value)
    assert "sanctions.csv" in text
    assert "line 3" in text
    assert "duplicate country RU" in text


def test_a_refused_sanctions_load_is_not_cached(sanctions_rows: list[dict[str, str]]) -> None:
    sanctions_rows.append(_row("ru"))
    with pytest.raises(ReferenceDataError):
        sanctioned_countries()
    sanctions_rows.clear()
    sanctions_rows.append(_row("RU"))
    assert set(sanctioned_countries()) == {"RU"}


def _mic(mic: str, country: str) -> MicEntry:
    return MicEntry(
        mic=mic,
        operating_mic=mic,
        is_segment=False,
        name="Injected test market",
        category="XTDM",
        country=country,
        city="",
        website="",
        status="ACTIVE",
        comments="",
    )


def test_an_injected_registry_decides_the_country() -> None:
    # Neither code is packaged: KP has no MIC in ISO 10383, and KPXA/TRXA are test codes.
    assert "KPXA" not in MicRegistry()
    assert "TRXA" not in MicRegistry()
    mics = MicRegistry([_mic("KPXA", "KP"), _mic("TRXA", "TR")])

    assert is_sanctioned("KPXA", mics=mics) is True
    assert is_sanctioned("TRXA", mics=mics) is False
    with pytest.raises(SanctionedMarketError) as info:
        require_not_sanctioned("KPXA", mics=mics)
    assert info.value.country == "KP"
    assert require_not_sanctioned("TRXA", mics=mics) is None

    # The same codes are unknown to the packaged registry, so the injected table made the call.
    with pytest.raises(UnknownMarketError):
        is_sanctioned("KPXA")
    with pytest.raises(UnknownMarketError):
        require_not_sanctioned("TRXA")


def test_an_injected_registry_respects_the_allow_list() -> None:
    mics = MicRegistry([_mic("KPXA", "KP")])
    assert is_sanctioned("KPXA", allow={"KP"}, mics=mics) is False
    assert require_not_sanctioned("KPXA", allow=frozenset({"KP"}), mics=mics) is None


@pytest.mark.parametrize("mic", ["MISX", "SPBE", "RTSX", "XMIC", "BCSE", "XTEH", "IFBX", "XDSE"])
def test_comprehensive_programme_countries_are_closed_by_default(mic: str) -> None:
    assert is_sanctioned(mic) is True


@pytest.mark.parametrize("mic", ["XIST", "XNYS", "XLON", "BVCA", "XKHA", "XLSM"])
def test_other_markets_are_open(mic: str) -> None:
    assert is_sanctioned(mic) is False


def test_the_allow_list_opens_exactly_the_named_country() -> None:
    assert is_sanctioned("MISX", allow={"RU"}) is False
    assert is_sanctioned("XTEH", allow={"RU"}) is True


def test_require_not_sanctioned_names_the_country_source_and_opt_in() -> None:
    with pytest.raises(SanctionedMarketError) as info:
        require_not_sanctioned("MISX")
    err = info.value
    assert err.mic == "MISX"
    assert err.country == "RU"
    text = str(err)
    assert f"{ALLOW_NAME}=RU" in text
    assert "ofac.treasury.gov" in text
    assert "Not legal advice" in text


def test_require_not_sanctioned_passes_when_allowed() -> None:
    assert require_not_sanctioned("SPBE", allow=frozenset({"RU"})) is None


def test_require_not_sanctioned_passes_for_an_open_market() -> None:
    assert require_not_sanctioned("XIST") is None


def test_an_unparsed_allow_string_is_refused() -> None:
    with pytest.raises(TypeError, match="parse_allowed_countries"):
        is_sanctioned("MISX", allow="IRU")
    with pytest.raises(TypeError, match="parse_allowed_countries"):
        require_not_sanctioned("MISX", allow="IRU")


def test_unknown_mics_are_refused_rather_than_assumed_clean() -> None:
    with pytest.raises(UnknownMarketError):
        is_sanctioned("ZZZZ")
    with pytest.raises(UnknownMarketError):
        require_not_sanctioned("ZZZZ")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", frozenset()),
        (",", frozenset()),
        ("RU", frozenset({"RU"})),
        ("ru, by", frozenset({"RU", "BY"})),
        ("RU,RU", frozenset({"RU"})),
        (" KP ,SY,", frozenset({"KP", "SY"})),
    ],
)
def test_parse_allowed_countries_accepts_listed_codes(raw: str, expected: frozenset[str]) -> None:
    assert parse_allowed_countries(raw) == expected


@pytest.mark.parametrize("raw", ["RUS", "R", "R1", "RU;BY", "TR", "RS"])
def test_parse_allowed_countries_refuses_malformed_and_unlisted_codes(raw: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_allowed_countries(raw)
    assert ALLOW_NAME in str(info.value)


def test_allowed_countries_reads_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.from_env({ALLOW_NAME: "RU,BY"})
    assert allowed_countries(settings) == frozenset({"RU", "BY"})

    monkeypatch.setenv(ALLOW_NAME, "IR")
    assert allowed_countries() == frozenset({"IR"})

    monkeypatch.delenv(ALLOW_NAME, raising=False)
    assert allowed_countries() == frozenset()


def test_sanctioned_countries_loads_the_packaged_list() -> None:
    entries = sanctioned_countries()
    assert set(entries) == {"BY", "IR", "KP", "RU", "SY"}
    for country, entry in entries.items():
        assert entry.country == country
        assert entry.source.startswith("https://"), country
        assert entry.note, country


def test_exchanges_csv_and_the_policy_agree() -> None:
    for info in ExchangeRegistry().all():
        assert info.sanctioned == is_sanctioned(info.mic), info.mic
