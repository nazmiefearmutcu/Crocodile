"""A source that serves one market is refused for another, and the refusal is declared.

``Provider.supported_mics`` is the market-scope counterpart of ``supported_channels``: ``None``
means "not declared", never "every market". The seven US-only sources declare
:data:`US_EQUITY_MICS`; the three multi-market sources stay undeclared, and the reasons are
recorded below so that an exemption without an argument cannot pass unnoticed.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.sink.memory import MemorySink
from crocodile.equity.providers import factory
from crocodile.equity.providers.base import (
    US_EQUITY_MICS,
    PullProvider,
    UnsupportedMarketError,
    require_supported_mic,
)
from crocodile.equity.providers.factory import _REGISTRY
from crocodile.equity.providers.finra.connector import FinraProvider
from crocodile.equity.providers.google_finance.connector import GoogleFinanceProvider
from crocodile.equity.providers.tiingo.connector import TiingoProvider
from crocodile.equity.reference.registry import InstrumentRegistry

pytestmark = pytest.mark.usefixtures("network_forbidden")

_US_ONLY = ("alpaca", "finnhub", "finra", "msn_money", "sec_edgar", "tiingo", "treasury")
_UNDECLARED: dict[str, str] = {
    "google_finance": "multi-market through grammar_google_finance.csv; a MIC without a row is "
    "refused by NoGrammarForMarket at candidate time, not by a static set",
    "stooq": "routes by its own suffix/ZIP heuristic (stooq/connector.py:165-183); no MIC grammar "
    "exists for it yet, so a declared set would be a guess",
    "yahoo": "legacy yfinance client takes Yahoo's own suffixed symbols (THYAO.IS) for any market; "
    "the MIC-aware Yahoo source is the universal yahoo_chart",
}


class _XistOnly(PullProvider):
    """Test-local source whose declared scope holds no US market."""

    name = "xist_only"
    supported_channels: ClassVar[frozenset[str]] = frozenset({"ohlcv"})
    supported_mics: ClassVar[frozenset[str] | None] = frozenset({"XIST"})


def _sink() -> MemorySink:
    return MemorySink()


def _registry() -> InstrumentRegistry:
    return InstrumentRegistry()


def test_us_equity_mics_are_the_us_rows_of_exchanges_csv():
    assert US_EQUITY_MICS == {i.mic for i in ExchangeRegistry().by_country("US")}


@pytest.mark.parametrize("name", _US_ONLY)
def test_each_us_only_source_declares_the_us_market_scope(name):
    assert factory.supported_mics(name) == US_EQUITY_MICS


def test_a_provider_that_declares_no_market_scope_says_why():
    undeclared = {name for name, cls in _REGISTRY.items() if cls.supported_mics is None}
    assert undeclared == set(_UNDECLARED)
    assert all(reason.strip() for reason in _UNDECLARED.values())


def test_every_declared_mic_is_an_operating_mic_the_exchange_table_knows():
    known = {i.mic for i in ExchangeRegistry().all()}
    for name, cls in _REGISTRY.items():
        declared = cls.supported_mics or frozenset()
        assert declared <= known, (name, declared - known)


def test_require_supported_mic_refuses_a_market_outside_the_scope():
    with pytest.raises(UnsupportedMarketError, match="XIST"):
        require_supported_mic(TiingoProvider, "XIST")
    assert require_supported_mic(TiingoProvider, "XNAS") is None
    assert issubclass(UnsupportedMarketError, ValueError)


def test_require_supported_mic_accepts_any_market_for_an_undeclared_provider():
    assert require_supported_mic(GoogleFinanceProvider, "XIST") is None


def test_supported_mics_is_none_for_an_unknown_provider_name():
    assert factory.supported_mics("nope") is None


def test_a_us_only_provider_drops_a_listing_on_another_market(caplog):
    with caplog.at_level("WARNING"):
        provider = FinraProvider(
            symbols=["AAPL", "THYAO@XIST"],
            channels=["short_volume"],
            out=_sink(),
            registry=_registry(),
        )
    assert provider.symbols == ["AAPL"]
    assert "XIST" in caplog.text


def test_a_us_only_provider_with_nothing_servable_is_refused():
    with pytest.raises(UnsupportedMarketError):
        FinraProvider(
            symbols=["THYAO@XIST"],
            channels=["short_volume"],
            out=_sink(),
            registry=_registry(),
        )


def test_a_malformed_listing_symbol_is_unservable_rather_than_a_crash(caplog):
    with caplog.at_level("WARNING"):
        provider = FinraProvider(
            symbols=["AAPL", "AAPL@XNAS"],
            channels=["short_volume"],
            out=_sink(),
            registry=_registry(),
        )
    assert provider.symbols == ["AAPL"]
    assert "bare ticker" in caplog.text
    with pytest.raises(UnsupportedMarketError):
        FinraProvider(
            symbols=["AAPL@XNAS"],
            channels=["short_volume"],
            out=_sink(),
            registry=_registry(),
        )


def test_a_bare_symbol_is_refused_by_a_provider_that_serves_no_us_market():
    with pytest.raises(UnsupportedMarketError):
        _XistOnly(symbols=["AAPL"], channels=["ohlcv"], out=_sink(), registry=_registry())
    kept = _XistOnly(
        symbols=["THYAO@XIST", "AAPL"], channels=["ohlcv"], out=_sink(), registry=_registry()
    )
    assert kept.symbols == ["THYAO@XIST"]


def test_an_undeclared_provider_keeps_every_symbol():
    provider = GoogleFinanceProvider(
        symbols=["THYAO@XIST", "AAPL"],
        channels=["trade"],
        out=_sink(),
        registry=_registry(),
    )
    assert provider.symbols == ["THYAO@XIST", "AAPL"]


def test_an_empty_symbol_list_is_not_refused_by_a_declared_provider():
    provider = FinraProvider(
        symbols=[], channels=["short_volume"], out=_sink(), registry=_registry()
    )
    assert provider.symbols == []
