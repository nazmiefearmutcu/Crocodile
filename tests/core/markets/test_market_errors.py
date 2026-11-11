"""Behaviour of the core/markets error family.

Every class is built from positional parameters and must survive pickle, copy
and deepcopy, because surfaces and multiprocessing carry exceptions across
process boundaries.
"""

from __future__ import annotations

import copy
import pickle
from datetime import date
from typing import Any

import pytest

from crocodile.core.errors import CrocodileError, StoreError
from crocodile.core.markets.errors import (
    CalendarRangeError,
    CurrencyMismatchError,
    ExpiredMarketError,
    InvalidSymbolError,
    ListingConflictError,
    MarketError,
    NoGrammarForMarket,
    NotASessionError,
    ReferenceDataError,
    SanctionedMarketError,
    UnknownCurrencyError,
    UnknownMarketError,
)

pytestmark = pytest.mark.usefixtures("network_forbidden")

ALL_ERRORS: tuple[type[MarketError], ...] = (
    MarketError,
    ReferenceDataError,
    UnknownMarketError,
    ExpiredMarketError,
    UnknownCurrencyError,
    NoGrammarForMarket,
    SanctionedMarketError,
    ListingConflictError,
    CalendarRangeError,
    InvalidSymbolError,
    NotASessionError,
    CurrencyMismatchError,
)

ONE_OF_EACH: tuple[MarketError, ...] = (
    MarketError("general failure"),
    ReferenceDataError("exchanges.csv", "line 3: quote_scale must be 0 or 2"),
    UnknownMarketError("ZZZZ", "not in the snapshot"),
    ExpiredMarketError("XTUR", "MIC TO USE IS XIST", "XIST", date(2013, 8, 26)),
    UnknownCurrencyError("GBp", "quote units are case-sensitive"),
    NoGrammarForMarket("yahoo_chart", "XIST"),
    SanctionedMarketError("BCSE", "BY", "default-closed"),
    ListingConflictError("AAPL", "XNAS:AAPL", "XLON:AAPL"),
    CalendarRangeError("XIST", date(2030, 1, 1), date(2013, 1, 1), date(2026, 12, 31)),
    InvalidSymbolError("AAPL US", "contains a space"),
    NotASessionError("XIST", date(2026, 11, 8)),
    CurrencyMismatchError("EUR", 0, "BGN", 2, "SOFIX@XBUL"),
)


def test_every_market_error_descends_from_market_error_and_the_root() -> None:
    for cls in ALL_ERRORS:
        assert issubclass(cls, MarketError), cls
        assert issubclass(cls, CrocodileError), cls


def test_there_are_exactly_twelve_market_error_classes() -> None:
    from crocodile.core.markets import errors

    assert len(ALL_ERRORS) == 12
    assert errors.__all__ == sorted(errors.__all__)
    assert set(errors.__all__) == {cls.__name__ for cls in ALL_ERRORS}


def test_listing_conflict_error_is_also_a_store_error() -> None:
    assert issubclass(ListingConflictError, StoreError)
    err = ListingConflictError("AAPL", "XNAS:AAPL", "XLON:AAPL")
    assert isinstance(err, StoreError)
    assert isinstance(err, CrocodileError)


def test_lookup_and_value_errors_keep_their_builtin_bases() -> None:
    for cls in (UnknownMarketError, ExpiredMarketError, NoGrammarForMarket):
        assert issubclass(cls, LookupError), cls
    for cls in (
        UnknownCurrencyError,
        CalendarRangeError,
        InvalidSymbolError,
        NotASessionError,
        CurrencyMismatchError,
    ):
        assert issubclass(cls, ValueError), cls


def test_an_expired_market_is_an_unknown_market() -> None:
    err = ExpiredMarketError("XTUR", "MIC TO USE IS XIST", "XIST", date(2013, 8, 26))
    assert isinstance(err, UnknownMarketError)
    assert isinstance(err, LookupError)
    try:
        raise err
    except UnknownMarketError as caught:
        assert caught.mic == "XTUR"


@pytest.mark.parametrize("err", ONE_OF_EACH, ids=lambda e: type(e).__name__)
def test_every_market_error_survives_pickle_copy_and_deepcopy(err: MarketError) -> None:
    # nosemgrep: python.lang.security.deserialization.pickle.avoid-pickle
    restored_pickle = pickle.loads(pickle.dumps(err))
    restored_copy = copy.copy(err)
    restored_deep = copy.deepcopy(err)
    for restored in (restored_pickle, restored_copy, restored_deep):
        assert type(restored) is type(err)
        assert str(restored) == str(err)
        assert _state(restored) == _state(err)
        assert isinstance(restored, CrocodileError)


def _state(err: BaseException) -> dict[str, Any]:
    return dict(vars(err))


def test_messages_name_their_subject() -> None:
    expired = str(ExpiredMarketError("XTUR", "MIC TO USE IS XIST", "XIST", date(2013, 8, 26)))
    for fragment in ("XTUR", "MIC TO USE IS XIST", "use XIST"):
        assert fragment in expired, fragment
    assert "GBp" in str(UnknownCurrencyError("GBp"))
    mismatch = str(CurrencyMismatchError("EUR", 0, "BGN", 2, "SOFIX@XBUL"))
    for fragment in ("EUR", "BGN", "SOFIX@XBUL"):
        assert fragment in mismatch, fragment
    no_grammar = str(NoGrammarForMarket("yahoo_chart", "XIST"))
    assert "yahoo_chart" in no_grammar
    assert "XIST" in no_grammar


def test_listing_conflict_message_is_the_documented_sentence() -> None:
    err = ListingConflictError("AAPL", "XNAS:AAPL", "XLON:AAPL")
    assert str(err) == (
        "canonical symbol 'AAPL' already identifies XNAS:AAPL; refusing to point it at XLON:AAPL"
    )
