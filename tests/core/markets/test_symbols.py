"""Canonical listing symbols: ``local@MIC`` with frozen US bare tickers."""

from __future__ import annotations

import pytest

from crocodile.core.markets.errors import (
    ExpiredMarketError,
    InvalidSymbolError,
    UnknownMarketError,
)
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.symbols import (
    is_listing_symbol,
    listing_symbol,
    parse_listing_symbol,
    validate_local_symbol,
)

pytestmark = pytest.mark.usefixtures("network_forbidden")

# Non-ASCII characters are built with chr() so this source file stays ASCII-only.
COMBINING_ACUTE = chr(0x0301)
E_ACUTE = chr(0x00E9)
ZERO_WIDTH_SPACE = chr(0x200B)
NO_BREAK_SPACE = chr(0x00A0)
BYTE_ORDER_MARK = chr(0xFEFF)
# json.loads turns the JSON escape \ud800 into this lone surrogate code point.
LONE_SURROGATE = chr(0xD800)

CAFE_DECOMPOSED = "Cafe" + COMBINING_ACUTE
CAFE_COMPOSED = "Caf" + E_ACUTE


@pytest.mark.parametrize(
    ("local", "mic", "expected"),
    [
        ("THYAO", "XIST", "THYAO@XIST"),
        ("7203", "XJPX", "7203@XJPX"),
        ("VOD", "XLON", "VOD@XLON"),
        ("CERA.N0000", "XCOL", "CERA.N0000@XCOL"),
        ("NOVO-B", "XCSE", "NOVO-B@XCSE"),
    ],
)
def test_non_us_listings_are_local_at_operating_mic(local: str, mic: str, expected: str) -> None:
    assert listing_symbol(local, mic) == expected


@pytest.mark.parametrize(
    ("local", "segment", "expected"),
    [
        ("7203", "XTKS", "7203@XJPX"),
        ("ITX", "XMAD", "ITX@BMEX"),
        ("THYAO", "XEQY", "THYAO@XIST"),
    ],
)
def test_segment_mics_resolve_to_the_operating_mic(local: str, segment: str, expected: str) -> None:
    assert listing_symbol(local, segment) == expected


@pytest.mark.parametrize(
    ("local", "mic"),
    [
        ("AAPL", "XNAS"),
        ("BRK.B", "XNYS"),
        ("AAPL", "XNGS"),
        ("SPY", "ARCX"),
        ("IBM", "XCBO"),
    ],
)
def test_us_listings_keep_bare_tickers(local: str, mic: str) -> None:
    assert listing_symbol(local, mic) == local


def test_one_local_symbol_on_two_exchanges_is_two_listings() -> None:
    shenzhen = listing_symbol("000001", "XSHE")
    shanghai = listing_symbol("000001", "XSHG")
    assert shenzhen == "000001@XSHE"
    assert shenzhen != shanghai


def test_local_symbols_are_nfc_normalised_and_keep_their_case() -> None:
    assert validate_local_symbol(CAFE_DECOMPOSED) == CAFE_COMPOSED
    assert listing_symbol("abc", "XIST") == "abc@XIST"
    assert listing_symbol(CAFE_DECOMPOSED, "XPAR") == f"{CAFE_COMPOSED}@XPAR"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "A@B",
        "A:B",
        "A B",
        "A\tB",
        "A\nB",
        "A\x00B",
        "A" + ZERO_WIDTH_SPACE + "B",
        "A" + NO_BREAK_SPACE + "B",
        "A" + BYTE_ORDER_MARK + "B",
        "A" + LONE_SURROGATE + "B",
    ],
)
def test_forbidden_characters_are_refused(bad: str) -> None:
    with pytest.raises(InvalidSymbolError) as info:
        validate_local_symbol(bad)
    assert info.value.reason
    with pytest.raises(InvalidSymbolError):
        listing_symbol(bad, "XIST")


def test_validate_local_symbol_refuses_a_non_string() -> None:
    with pytest.raises(TypeError):
        validate_local_symbol(7203)  # type: ignore[arg-type]


def test_markets_that_cannot_be_used_are_refused() -> None:
    with pytest.raises(UnknownMarketError) as unknown:
        listing_symbol("X", "ZZZZ")
    assert not isinstance(unknown.value, ExpiredMarketError)

    with pytest.raises(ExpiredMarketError):
        listing_symbol("X", "XTUR")

    # DRSP is an active ISO code that the exchange allow-list does not contain.
    with pytest.raises(UnknownMarketError):
        listing_symbol("X", "DRSP")


def test_sanctions_do_not_change_identity() -> None:
    assert listing_symbol("SBER", "MISX") == "SBER@MISX"


def test_parse_listing_symbol_splits_canonical_symbols() -> None:
    assert parse_listing_symbol("THYAO@XIST") == ("THYAO", "XIST")
    assert parse_listing_symbol("AAPL") == ("AAPL", None)
    assert parse_listing_symbol("BRK.B") == ("BRK.B", None)
    assert parse_listing_symbol("CERA.N0000@XCOL") == ("CERA.N0000", "XCOL")


@pytest.mark.parametrize(
    ("symbol", "error"),
    [
        ("THYAO@", InvalidSymbolError),
        ("@XIST", InvalidSymbolError),
        ("A@B@XIST", InvalidSymbolError),
        ("THYAO@xist", InvalidSymbolError),
        ("THYAO@XIS", InvalidSymbolError),
        ("7203@XTKS", InvalidSymbolError),
        ("AAPL@XNAS", InvalidSymbolError),
        (f"{CAFE_DECOMPOSED}@XPAR", InvalidSymbolError),
        (f"A{LONE_SURROGATE}B@XIST", InvalidSymbolError),
        ("X@ZZZZ", UnknownMarketError),
        ("X@XTUR", ExpiredMarketError),
        ("X@DRSP", UnknownMarketError),
    ],
)
def test_parse_listing_symbol_refuses_non_canonical_forms(symbol: str, error: type) -> None:
    with pytest.raises(error):
        parse_listing_symbol(symbol)


def test_is_listing_symbol_is_true_only_for_canonical_non_us_symbols() -> None:
    assert is_listing_symbol("THYAO@XIST") is True
    for symbol in (
        "AAPL",
        "AAPL@XNAS",
        "7203@XTKS",
        "X@ZZZZ",
        "binance:BTCUSDT",
        "",
    ):
        assert is_listing_symbol(symbol) is False, symbol


def test_every_exchange_round_trips_through_the_canonical_form() -> None:
    us_mics = {"IEXG", "XCBO", "XNAS", "XNYS"}
    rows = ExchangeRegistry().all()
    assert len(rows) == 172
    for info in rows:
        sym = listing_symbol("TEST-1.A", info.mic)
        if info.mic in us_mics:
            assert info.country == "US", info.mic
            assert sym == "TEST-1.A"
            assert parse_listing_symbol(sym) == ("TEST-1.A", None)
        else:
            assert parse_listing_symbol(sym) == ("TEST-1.A", info.mic), info.mic
            assert is_listing_symbol(sym), info.mic
