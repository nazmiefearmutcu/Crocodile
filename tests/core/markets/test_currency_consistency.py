"""check_currency_consistent: a record's stated unit must agree with its listing's.

A record that says ``EUR`` for a Bulgarian listing, or ``GBP`` at major-unit scale for a
pence-quoted one, is a defect in the record, not a fact about the market. The check refuses
it so the caller can dead-letter the record (spec 3.7, section 5). A side that states nothing
is unknown and is never a mismatch.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from crocodile.core.markets.currency import check_currency_consistent
from crocodile.core.markets.errors import CurrencyMismatchError, MarketError
from crocodile.core.schema.enums import AssetClass
from crocodile.core.schema.records import OHLCV


@dataclass(frozen=True)
class _Listing:
    currency: str | None
    price_scale: int | None
    symbol: str = "VOD@XLON"


def test_a_record_that_agrees_with_its_listing_passes() -> None:
    assert check_currency_consistent("GBP", 2, _Listing("GBP", 2)) is None


@pytest.mark.parametrize(
    ("record_currency", "record_scale", "listing"),
    [
        (None, None, _Listing("GBP", 2)),
        (None, 2, _Listing("GBP", 2)),
        ("GBP", None, _Listing("GBP", 2)),
        ("GBP", 2, _Listing(None, None)),
    ],
    ids=["both-unstated", "currency-unstated", "scale-unstated", "listing-unstated"],
)
def test_an_unstated_side_is_never_a_mismatch(
    record_currency: str | None, record_scale: int | None, listing: _Listing
) -> None:
    assert check_currency_consistent(record_currency, record_scale, listing) is None


def test_a_different_currency_is_refused() -> None:
    """The Bulgarian changeover case from spec 3.7: the listing moved from BGN to EUR."""
    with pytest.raises(CurrencyMismatchError) as caught:
        check_currency_consistent("EUR", 0, _Listing("BGN", 0))
    assert "EUR" in str(caught.value)
    assert "BGN" in str(caught.value)


def test_a_different_price_scale_is_refused() -> None:
    with pytest.raises(CurrencyMismatchError, match="price_scale"):
        check_currency_consistent("GBP", 0, _Listing("GBP", 2))


def test_both_mismatches_are_named_in_one_refusal() -> None:
    with pytest.raises(CurrencyMismatchError) as caught:
        check_currency_consistent("EUR", 2, _Listing("GBP", 0))
    message = str(caught.value)
    assert "currency" in message
    assert "price_scale" in message


def test_the_refusal_is_a_market_value_error_for_the_dead_letter_path() -> None:
    assert issubclass(CurrencyMismatchError, ValueError)
    assert issubclass(CurrencyMismatchError, MarketError)


def test_the_refusal_names_the_listing_symbol_when_the_listing_has_one() -> None:
    with pytest.raises(CurrencyMismatchError, match="VOD@XLON"):
        check_currency_consistent("EUR", 0, _Listing("GBP", 2, symbol="VOD@XLON"))


def test_the_refusal_carries_both_units_as_attributes() -> None:
    with pytest.raises(CurrencyMismatchError) as caught:
        check_currency_consistent("EUR", 0, _Listing("BGN", 2, symbol="SOFIX@XBUL"))
    exc = caught.value
    assert exc.record_currency == "EUR"
    assert exc.record_scale == 0
    assert exc.listing_currency == "BGN"
    assert exc.listing_scale == 2
    assert exc.symbol == "SOFIX@XBUL"


def test_a_stage_b_bar_is_checked_with_its_own_fields() -> None:
    bar = OHLCV(
        source="yahoo_chart",
        symbol="VOD@XLON",
        symbol_raw="VOD.L",
        local_ts=1_793_577_600_000_000_000,
        asset_class=AssetClass.EQUITY,
        source_ts=None,
        interval="1d",
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=10.0,
        currency="GBP",
        price_scale=0,
    )
    with pytest.raises(CurrencyMismatchError):
        check_currency_consistent(bar.currency, bar.price_scale, _Listing("GBP", 2))
