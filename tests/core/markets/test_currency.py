"""Currency registry, quote-unit parsing and exact major-unit conversion."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest

from crocodile.core.markets import currency
from crocodile.core.markets.currency import (
    QuoteUnit,
    currencies,
    minor_unit,
    parse_quote_unit,
    require_currency,
    to_major,
)
from crocodile.core.markets.errors import ReferenceDataError, UnknownCurrencyError

pytestmark = pytest.mark.usefixtures("network_forbidden")


def test_require_currency_accepts_iso_codes() -> None:
    for code in ("EUR", "TRY", "USD", "JPY", "CLF"):
        assert require_currency(code) == code


@pytest.mark.parametrize("bad", ["BGN", "eur", "XAU", "XXX", "XTS", "", "EURO"])
def test_require_currency_rejects_withdrawn_lowercase_and_non_currency_codes(bad: str) -> None:
    with pytest.raises(UnknownCurrencyError):
        require_currency(bad)


def test_minor_unit_reads_iso4217() -> None:
    assert minor_unit("JPY") == 0
    assert minor_unit("EUR") == 2
    assert minor_unit("KWD") == 3
    assert minor_unit("CLF") == 4


def test_currencies_is_a_cached_mapping_of_165_entries() -> None:
    table = currencies()
    assert len(table) == 165
    assert table["EUR"].minor_unit == 2
    assert table["EUR"].name == "Euro"
    assert currencies() is table


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("GBp", QuoteUnit("GBP", 2)),
        ("GBX", QuoteUnit("GBP", 2)),
        ("ZAc", QuoteUnit("ZAR", 2)),
        ("ZAC", QuoteUnit("ZAR", 2)),
        ("ILA", QuoteUnit("ILS", 2)),
        ("TRY", QuoteUnit("TRY", 0)),
        ("USD", QuoteUnit("USD", 0)),
        ("JPY", QuoteUnit("JPY", 0)),
        ("EUR", QuoteUnit("EUR", 0)),
    ],
)
def test_parse_quote_unit_table(raw: str, expected: QuoteUnit) -> None:
    assert parse_quote_unit(raw) == expected


@pytest.mark.parametrize(
    "raw", ["gbp", "gbx", "Gbp", "zac", "ila", "KWF", "GBP ", "", "XAU", "BGN"]
)
def test_parse_quote_unit_refuses_to_guess(raw: str) -> None:
    with pytest.raises(UnknownCurrencyError):
        parse_quote_unit(raw)


def test_to_major_is_exact() -> None:
    assert to_major(Decimal("1234.5"), 2) == Decimal("12.345")
    assert to_major(1234.5, 2) == Decimal("12.345")
    assert to_major(7, 0) == Decimal("7")
    assert to_major("0.1", 2) == Decimal("0.001")
    assert str(to_major(Decimal("123456789012345678901234567890"), 2)) == (
        "1234567890123456789012345678.90"
    )


def test_to_major_uses_the_shortest_float_repr_not_the_binary_expansion() -> None:
    assert to_major(0.1, 1) == Decimal("0.01")
    assert str(to_major(0.1, 1)) == "0.01"


@pytest.mark.parametrize(
    ("value", "scale", "error"),
    [
        (True, 2, TypeError),
        (1, -1, ValueError),
        (1, 1.5, ValueError),
        (float("nan"), 2, ValueError),
        (float("inf"), 2, ValueError),
        ("abc", 2, ValueError),
    ],
)
def test_to_major_rejects_bad_input(value: object, scale: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        to_major(value, scale)  # type: ignore[arg-type]


def test_to_major_rejects_a_non_finite_decimal_string() -> None:
    with pytest.raises(ValueError):
        to_major("NaN", 2)


@pytest.fixture
def fresh_currency_table() -> Iterator[None]:
    """Each loader test starts from an empty cache and leaves none behind."""
    currency._table.cache_clear()
    yield
    currency._table.cache_clear()


@pytest.mark.parametrize(
    "rows",
    [
        [{"code": "eur", "minor_unit": "2", "name": "Euro"}],
        [{"code": "EUR", "minor_unit": "two", "name": "Euro"}],
        [
            {"code": "EUR", "minor_unit": "2", "name": "Euro"},
            {"code": "EUR", "minor_unit": "2", "name": "Euro"},
        ],
    ],
    ids=["lowercase-code", "non-numeric-minor-unit", "duplicate-code"],
)
def test_the_iso4217_loader_refuses_a_bad_row(
    fresh_currency_table: None, monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, str]]
) -> None:
    monkeypatch.setattr(currency, "read_csv", lambda name, header: rows)
    with pytest.raises(ReferenceDataError):
        currency._table()
