"""Google Finance by listing market: candidates, currency and timezone come from the listing.

A listing symbol ``LOCAL@MIC`` is looked up in the Google grammar and its page is read in the
listing's currency and timezone. Offline only: every page is a canned HTML string.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from crocodile.core.markets.currency import require_currency
from crocodile.core.markets.errors import InvalidSymbolError, NoGrammarForMarket
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.grammar import load_grammar
from crocodile.core.schema.records import Fundamental, Trade
from crocodile.core.sink.memory import MemorySink
from crocodile.equity.providers.google_finance.connector import (
    _DOLLAR_SIGN_CURRENCIES,
    GoogleFinanceProvider,
    ListingContext,
    get_possible_google_symbols,
    listing_context,
    parse_val_and_unit,
)
from crocodile.equity.reference.registry import InstrumentRegistry

pytestmark = pytest.mark.usefixtures("network_forbidden")


def _session(html: str) -> MagicMock:
    """A session whose every request answers with ``html``."""
    response = MagicMock()
    response.status = 200
    response.text = AsyncMock(return_value=html)
    session = MagicMock()
    session.get.return_value.__aenter__ = AsyncMock(return_value=response)
    session.get.return_value.__aexit__ = AsyncMock(return_value=None)
    return session


def _provider(symbol: str, channels: list[str]) -> GoogleFinanceProvider:
    return GoogleFinanceProvider(
        symbols=[symbol],
        channels=channels,
        out=MemorySink(),
        registry=InstrumentRegistry(),
    )


def test_a_listing_symbol_is_looked_up_in_the_google_grammar():
    assert get_possible_google_symbols("THYAO@XIST") == ["THYAO:IST"]


def test_a_market_without_a_google_code_is_refused_rather_than_guessed():
    missing = sorted(
        {i.mic for i in ExchangeRegistry().all() if i.country != "US"}
        - load_grammar("google_finance").supported_mics()
    )
    assert missing
    with pytest.raises(NoGrammarForMarket):
        get_possible_google_symbols(f"ABC@{missing[0]}")
    # US listings are bare; a US MIC on a listing symbol is refused, not looked up.
    with pytest.raises(InvalidSymbolError):
        get_possible_google_symbols("AAPL@XNAS")


def test_bare_us_tickers_and_raw_symbols_keep_their_old_candidates():
    assert get_possible_google_symbols("AAPL")[:3] == [
        "AAPL:NASDAQ",
        "AAPL:NYSE",
        "AAPL:INDEXSP",
    ]
    assert get_possible_google_symbols("THYAO:IST") == ["THYAO:IST"]


def test_a_lower_case_raw_google_symbol_resolves_to_its_market():
    # The pre-change candidate builder upper-cased a raw ':' symbol, so 'thyao:ist' scraped
    # /quote/THYAO:IST. The market lookup must see the same code, not the raw spelling.
    assert get_possible_google_symbols("thyao:ist") == ["THYAO:IST"]
    istanbul = listing_context("thyao:ist")
    assert (istanbul.mic, istanbul.currency, str(istanbul.tz)) == (
        "XIST",
        "TRY",
        "Europe/Istanbul",
    )


async def test_scraping_a_lower_case_raw_google_symbol_requests_its_google_code():
    html = (
        "<html><body><div class='N6SYTe'>299.25</div>"
        "<div>As of Jul 3, 2026, 5:10 PM</div></body></html>"
    )
    provider = _provider("thyao:ist", ["trade"])
    session = _session(html)
    provider.session = session
    await provider._scrape_symbol("thyao:ist")
    assert session.get.call_count == 1
    assert session.get.call_args.args[0].endswith("/quote/THYAO:IST")


def test_listing_context_comes_from_the_listing_market():
    istanbul = listing_context("THYAO@XIST")
    assert isinstance(istanbul, ListingContext)
    assert (istanbul.mic, istanbul.currency, str(istanbul.tz)) == (
        "XIST",
        "TRY",
        "Europe/Istanbul",
    )
    tokyo = listing_context("7203@XJPX")
    assert (tokyo.mic, tokyo.currency, str(tokyo.tz)) == ("XJPX", "JPY", "Asia/Tokyo")
    for symbol in ("AAPL", "^SPX", "AAPL:NASDAQ"):
        ctx = listing_context(symbol)
        assert (ctx.mic, ctx.currency, str(ctx.tz)) == (None, "USD", "America/New_York")


def test_the_bare_us_context_agrees_with_every_us_exchange_row():
    rows = ExchangeRegistry().by_country("US")
    assert rows
    for info in rows:
        assert info.currency == "USD"
        assert str(info.tz) == "America/New_York"


def test_a_dollar_sign_resolves_to_the_listing_currency():
    assert parse_val_and_unit("$1,045.00", "Open", listing_currency="TWD") == (1045.0, "TWD")
    assert parse_val_and_unit("$1,045.00", "Open", listing_currency="AUD") == (1045.0, "AUD")


def test_a_dollar_sign_on_a_non_dollar_market_is_refused():
    assert parse_val_and_unit("$12.50", "Open", listing_currency="TRY") == (None, "unknown")


def test_an_unmarked_amount_is_in_the_listing_currency_not_usd():
    val, unit = parse_val_and_unit("412.97B", "Mkt. cap", listing_currency="TRY")
    assert val == pytest.approx(412.97e9)
    assert unit == "TRY"


def test_an_explicit_euro_or_pound_still_wins():
    assert parse_val_and_unit("€5.00", "Open", listing_currency="TRY") == (5.0, "EUR")


def test_every_dollar_sign_currency_is_an_iso_code():
    for code in _DOLLAR_SIGN_CURRENCIES:
        assert require_currency(code) == code


@pytest.mark.parametrize(
    ("symbol", "g_sym", "expected_ts"),
    [
        # 17:10 Tokyo, 17:10 New York (EDT): two zones 13 hours apart.
        ("7203@XJPX", "7203:TYO", 1_783_066_200_000_000_000),
        ("AAPL", "AAPL:NASDAQ", 1_783_113_000_000_000_000),
    ],
)
async def test_a_naive_as_of_time_is_read_in_the_listing_timezone(symbol, g_sym, expected_ts):
    html = (
        "<html><body><div class='N6SYTe'>3,000.00</div>"
        "<div>As of Jul 3, 2026, 5:10 PM</div></body></html>"
    )
    provider = _provider(symbol, ["trade"])
    provider.session = _session(html)
    records = await provider._scrape_with_g_sym(symbol, g_sym, 1)
    trades = [r for r in records or [] if isinstance(r, Trade)]
    assert len(trades) == 1
    assert trades[0].source_ts == expected_ts


async def test_fundamentals_on_a_listing_page_are_filed_in_the_listing_currency():
    html = (
        "<html><body>"
        "<div class='N6SYTe'>299.25</div>"
        "<div>As of Jul 3, 2026, 5:10 PM</div>"
        "<div><div class='SwQK7'>Open</div><div class='dO6ijd'>290.00</div></div>"
        "<div><div class='SwQK7'>Mkt. cap</div><div class='dO6ijd'>412.97B</div></div>"
        "<div><div class='SwQK7'>EPS</div><div class='dO6ijd'>$1.25</div></div>"
        "</body></html>"
    )
    provider = _provider("THYAO@XIST", ["trade", "fundamental"])
    provider.session = _session(html)
    records = await provider._scrape_with_g_sym("THYAO@XIST", "THYAO:IST", 1)
    records = records or []

    fundamentals = {r.tag: (r.val, r.unit) for r in records if isinstance(r, Fundamental)}
    assert set(fundamentals) == {"open", "market_cap"}
    assert fundamentals["open"] == (290.0, "TRY")
    assert fundamentals["market_cap"][0] == pytest.approx(412.97e9)
    assert fundamentals["market_cap"][1] == "TRY"

    trades = [r for r in records if isinstance(r, Trade)]
    assert len(trades) == 1
    assert trades[0].symbol == "THYAO@XIST"


async def test_scraping_a_listing_symbol_requests_its_google_code():
    html = (
        "<html><body><div class='N6SYTe'>299.25</div>"
        "<div>As of Jul 3, 2026, 5:10 PM</div></body></html>"
    )
    provider = _provider("THYAO@XIST", ["trade"])
    session = _session(html)
    provider.session = session
    await provider._scrape_symbol("THYAO@XIST")
    assert session.get.call_count == 1
    url = session.get.call_args.args[0]
    assert url.endswith("/quote/THYAO:IST")
    assert provider._resolved_symbol_cache["THYAO@XIST"] == "THYAO:IST"


async def test_a_listing_symbol_keeps_its_case_on_the_record():
    html = (
        "<html><body><div class='N6SYTe'>299.25</div>"
        "<div>As of Jul 3, 2026, 5:10 PM</div></body></html>"
    )
    provider = _provider("aBc@XIST", ["trade"])
    provider.session = _session(html)
    records = await provider._scrape_with_g_sym("aBc@XIST", "aBc:IST", 1)
    trades = [r for r in records or [] if isinstance(r, Trade)]
    assert len(trades) == 1
    assert trades[0].symbol == "aBc@XIST"
