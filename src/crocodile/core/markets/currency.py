"""ISO 4217 currencies, quote units and exact major-unit conversion.

The packaged list ``iso4217.csv`` is derived from SIX list one (2026-09-17) by
``scripts/build_markets_reference.py``. It holds 165 codes:

* the 13 codes whose minor unit is ``N.A.`` are excluded (XAG, XAU, XBA, XBB,
  XBC, XBD, XDR, XPD, XPT, XSU, XTS, XUA, XXX): precious metals, bond-market
  units, SDR, the ADB unit, the testing code and "no currency". No listing is
  priced in them, so ``require_currency`` rejects them;
* the 10 fund codes flagged ``IsFund`` are kept (BOV, CHE, CHW, CLF, COU, MXV,
  USN, UYI, UYW, XAD) because they denominate real instruments, for example
  Chilean UF bonds in CLF with minor unit 4;
* withdrawn codes are absent because list one no longer carries them: BGN
  (Bulgaria is EUR), HRK, ZWL and SLL.

``parse_quote_unit`` reads the price scale of a quote. It is case-sensitive and
never strips or guesses. ``KWF``, a Kuwaiti-fils spelling some sources use, is not
accepted: adding it changes the specification and is a decision for the user.
"""

from __future__ import annotations

import functools
import math
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Final, Protocol

import msgspec

from crocodile.core.markets._data import read_csv
from crocodile.core.markets.errors import (
    CurrencyMismatchError,
    ReferenceDataError,
    UnknownCurrencyError,
)

__all__ = [
    "ISO4217_COLUMNS",
    "ISO4217_FILE",
    "MINOR_UNIT_QUOTES",
    "Currency",
    "ListingUnit",
    "QuoteUnit",
    "check_currency_consistent",
    "currencies",
    "minor_unit",
    "parse_quote_unit",
    "require_currency",
    "to_major",
]

ISO4217_FILE: Final = "iso4217.csv"
ISO4217_COLUMNS: Final = ("code", "minor_unit", "name")
MINOR_UNIT_QUOTES: Final[Mapping[str, tuple[str, int]]] = MappingProxyType(
    {
        "GBp": ("GBP", 2),
        "GBX": ("GBP", 2),
        "ZAc": ("ZAR", 2),
        "ZAC": ("ZAR", 2),
        "ILA": ("ILS", 2),
    }
)
_CODE_RE: Final = re.compile(r"[A-Z]{3}")


class Currency(msgspec.Struct, frozen=True, kw_only=True):
    """One ISO 4217 currency: its code, its ISO minor unit and its name."""

    code: str
    minor_unit: int
    name: str


class QuoteUnit(msgspec.Struct, frozen=True):
    """How a quote is expressed: its currency and the scale of the quoted price.

    ``price_scale`` is the scale of the quote, not the ISO minor unit. A price
    quoted in pence has ``price_scale`` 2 and currency GBP.
    """

    currency: str
    price_scale: int


@functools.cache
def _table() -> Mapping[str, Currency]:
    table: dict[str, Currency] = {}
    for line, row in enumerate(read_csv(ISO4217_FILE, ISO4217_COLUMNS), start=2):
        code = row["code"]
        if not _CODE_RE.fullmatch(code):
            raise ReferenceDataError(ISO4217_FILE, f"line {line}: code {code!r} is not ISO 4217")
        if not row["minor_unit"].isdigit():
            raise ReferenceDataError(ISO4217_FILE, f"line {line}: minor_unit is not a number")
        if code in table:
            raise ReferenceDataError(ISO4217_FILE, f"line {line}: duplicate code {code}")
        table[code] = Currency(code=code, minor_unit=int(row["minor_unit"]), name=row["name"])
    return MappingProxyType(table)


def currencies() -> Mapping[str, Currency]:
    """Every accepted ISO 4217 currency, keyed by code. Loaded once."""
    return _table()


def require_currency(code: str) -> str:
    """Return ``code`` when it is an accepted ISO 4217 code; refuse anything else."""
    if not isinstance(code, str) or code not in _table():
        raise UnknownCurrencyError(str(code), "not an accepted ISO 4217 code")
    return code


def minor_unit(code: str) -> int:
    """The ISO minor unit of ``code``, for example JPY 0, EUR 2, KWD 3, CLF 4."""
    return _table()[require_currency(code)].minor_unit


def parse_quote_unit(raw: str) -> QuoteUnit:
    """Read a quote-unit spelling exactly. Minor-unit spellings first, then ISO codes."""
    if raw in MINOR_UNIT_QUOTES:
        currency, scale = MINOR_UNIT_QUOTES[raw]
        return QuoteUnit(currency, scale)
    if raw in _table():
        return QuoteUnit(raw, 0)
    raise UnknownCurrencyError(
        raw,
        "quote units are case-sensitive; accepted: GBp, GBX, ZAc, ZAC, ILA or an ISO 4217 code",
    )


def to_major(value: Decimal | int | float | str, price_scale: int) -> Decimal:
    """Convert a quote at ``price_scale`` into major units, exactly and without context rounding.

    Floats go through their shortest round-trip ``repr`` rather than the binary
    expansion. The result is the same number with its exponent shifted by
    ``price_scale``, so no precision is lost however long the value is.
    """
    if isinstance(value, bool):
        raise TypeError("a boolean is not a price")
    if isinstance(price_scale, bool) or not isinstance(price_scale, int):
        raise ValueError(f"price_scale must be an int, got {price_scale!r}")
    if price_scale < 0:
        raise ValueError(f"price_scale must be non-negative, got {price_scale}")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"price {value!r} is not finite")
        number = Decimal(repr(value))
    elif isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, str):
        try:
            number = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"price {value!r} is not a number") from exc
    else:
        raise TypeError(f"unsupported price type {type(value).__name__}")
    if not number.is_finite():
        raise ValueError(f"price {value!r} is not finite")
    sign, digits, exponent = number.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError(f"price {value!r} is not finite")
    return Decimal((sign, digits, exponent - price_scale))


class ListingUnit(Protocol):
    """What :func:`check_currency_consistent` reads off a listing.

    Structural, so ``master.Listing`` (spec §3.6) satisfies it without this module importing the
    listing master, and a test can pass any frozen dataclass with the two attributes. Read-only
    properties rather than plain attributes, so frozen dataclasses and frozen msgspec structs
    match under mypy strict.
    """

    @property
    def currency(self) -> str | None: ...

    @property
    def price_scale(self) -> int | None: ...


def check_currency_consistent(
    record_currency: str | None,
    record_scale: int | None,
    listing: ListingUnit,
) -> None:
    """Refuse a record whose stated unit contradicts its listing's.

    Each dimension is compared only when both sides state it. ``None`` on either side means
    unknown: it is never a mismatch, and it is never read as USD or as scale 0. Codes are
    compared exactly, because ISO 4217 codes are upper case and normalising ``"gbp"`` here would
    hide the defect that produced it. Minor-unit spellings (``GBp``, ``ZAc``, ``ILA``) must have
    gone through ``parse_quote_unit`` before a record is built.

    Call it as ``check_currency_consistent(record.currency, record.price_scale, listing)``.

    Raises:
        CurrencyMismatchError: if both sides state a currency and they differ, or both state a
            price scale and they differ. The message names both values and, when the listing
            object has a ``symbol`` attribute, that symbol. The caller writes the record to its
            dead-letter queue instead of the sink.
    """
    listing_currency = listing.currency
    listing_scale = listing.price_scale
    currency_differs = (
        record_currency is not None
        and listing_currency is not None
        and record_currency != listing_currency
    )
    scale_differs = (
        record_scale is not None and listing_scale is not None and record_scale != listing_scale
    )
    if not (currency_differs or scale_differs):
        return
    symbol = getattr(listing, "symbol", None)
    raise CurrencyMismatchError(
        record_currency,
        record_scale,
        listing_currency,
        listing_scale,
        None if symbol is None else str(symbol),
    )
