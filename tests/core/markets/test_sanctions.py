"""Default-closed sanctions policy: country by MIC, the allow-list and the setting."""

from __future__ import annotations

import pytest

from crocodile.core.config import Settings
from crocodile.core.errors import ConfigError
from crocodile.core.markets.errors import SanctionedMarketError, UnknownMarketError
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.sanctions import (
    allowed_countries,
    is_sanctioned,
    parse_allowed_countries,
    require_not_sanctioned,
    sanctioned_countries,
)

pytestmark = pytest.mark.usefixtures("network_forbidden")

ALLOW_NAME = "CROCODILE_ALLOW_SANCTIONED_MARKETS"


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
