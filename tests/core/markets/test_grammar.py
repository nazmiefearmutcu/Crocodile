"""Symbol grammars on in-memory rows: no packaged table is read here.

Every test builds a small grammar from ``SuffixRow`` or ``PrefixRow`` values, so the
behaviour is pinned independently of the content of ``grammar_*.csv``. The table gates
live in ``tests/conformance/test_symbol_grammar.py``.
"""

from __future__ import annotations

import copy
import pickle
from typing import Any

import pytest

from crocodile.core.errors import CrocodileError
from crocodile.core.markets import errors as market_errors
from crocodile.core.markets.errors import MarketError, NoGrammarForMarket, ReferenceDataError
from crocodile.core.markets.grammar import (
    AmbiguousSourceSymbol,
    GrammarError,
    PrefixGrammar,
    PrefixRow,
    SuffixGrammar,
    SuffixRow,
    SymbolGrammar,
    UnknownSourceSymbol,
)

pytestmark = pytest.mark.usefixtures("network_forbidden")

EVIDENCE = "verified:2026-11-08"


def _suffix(rows: list[SuffixRow], separator: str = ".") -> SuffixGrammar:
    return SuffixGrammar("yahoo_chart", rows, separator=separator)


def _xist() -> SuffixRow:
    return SuffixRow(mic="XIST", suffix=".IS", rule="none", sample_local="THYAO", evidence=EVIDENCE)


def _us_pair() -> list[SuffixRow]:
    return [
        SuffixRow(
            mic="XNAS", suffix="", rule="us_class_dash", sample_local="AAPL", evidence=EVIDENCE
        ),
        SuffixRow(
            mic="XNYS", suffix="", rule="us_class_dash", sample_local="AAPL", evidence=EVIDENCE
        ),
    ]


def _xkrx() -> list[SuffixRow]:
    return [
        SuffixRow(mic="XKRX", suffix=".KS", rule="none", sample_local="005930", evidence=EVIDENCE),
        SuffixRow(
            mic="XKRX",
            suffix=".KQ",
            rule="segment:XKOS",
            sample_local="247540",
            evidence=EVIDENCE,
        ),
    ]


def test_a_suffix_grammar_appends_and_strips_the_market_suffix() -> None:
    grammar = _suffix([_xist()])
    assert grammar.to_source("THYAO", "XIST") == "THYAO.IS"
    assert grammar.from_source("THYAO.IS") == ("THYAO", "XIST")


def test_to_source_refuses_a_market_with_no_row() -> None:
    grammar = _suffix([_xist()])
    with pytest.raises(NoGrammarForMarket) as caught:
        grammar.to_source("IBM", "XNYS")
    assert caught.value.source == "yahoo_chart"
    assert caught.value.mic == "XNYS"


def test_from_source_refuses_an_unknown_suffix() -> None:
    grammar = _suffix([_xist()])
    with pytest.raises(UnknownSourceSymbol) as caught:
        grammar.from_source("THYAO.ZZ")
    assert caught.value.raw == "THYAO.ZZ"
    assert isinstance(caught.value, NoGrammarForMarket)
    assert isinstance(caught.value, LookupError)
    # Matching is case-sensitive: a lower-case suffix is not normalised into ".IS".
    with pytest.raises(UnknownSourceSymbol):
        grammar.from_source("thyao.is")


def test_from_source_refuses_an_empty_or_malformed_local_part() -> None:
    grammar = _suffix([_xist()])
    for raw in ("", ".IS", "A B.IS"):
        with pytest.raises(GrammarError):
            grammar.from_source(raw)


def test_unsuffixed_symbols_shared_by_two_markets_need_a_mic_hint() -> None:
    grammar = _suffix(_us_pair())
    with pytest.raises(AmbiguousSourceSymbol) as caught:
        grammar.from_source("IBM")
    assert caught.value.candidates == ("XNAS", "XNYS")
    assert grammar.from_source("IBM", mic="XNYS") == ("IBM", "XNYS")


def test_a_mic_hint_that_contradicts_the_suffix_is_refused() -> None:
    grammar = _suffix([_xist()])
    with pytest.raises(GrammarError):
        grammar.from_source("THYAO.IS", mic="XNAS")


def test_us_class_shares_are_spelled_with_a_dash_on_yahoo() -> None:
    grammar = _suffix(_us_pair())
    assert grammar.to_source("BRK.B", "XNYS") == "BRK-B"
    assert grammar.from_source("BRK-B", mic="XNYS") == ("BRK.B", "XNYS")


def test_the_class_dash_rule_refuses_a_local_that_already_has_a_dash() -> None:
    grammar = _suffix(_us_pair())
    with pytest.raises(GrammarError):
        grammar.to_source("BRK-B", "XNYS")


def test_a_segment_row_serves_only_its_segment() -> None:
    grammar = _suffix(_xkrx())
    assert grammar.to_source("247540", "XKRX", segment_mic="XKOS") == "247540.KQ"
    assert grammar.to_source("005930", "XKRX") == "005930.KS"
    # A segment with no row of its own (XKON) falls back to the primary row.
    assert grammar.to_source("005930", "XKRX", segment_mic="XKON") == "005930.KS"
    assert grammar.from_source("247540.KQ") == ("247540", "XKRX")
    assert grammar.from_source("247540.KQ", mic="XKOS") == ("247540", "XKRX")


def test_a_segment_hint_must_belong_to_the_row_market() -> None:
    grammar = _suffix([*_xkrx(), _xist()])
    with pytest.raises(GrammarError):
        grammar.to_source("1", "XIST", segment_mic="XKOS")


_BAD_SUFFIX_TABLES: dict[str, list[SuffixRow]] = {
    "duplicate_suffix": [
        _xist(),
        SuffixRow(mic="XAMS", suffix=".IS", rule="none", sample_local="ASML", evidence=EVIDENCE),
    ],
    "two_primaries_for_one_mic": [
        _xist(),
        SuffixRow(mic="XIST", suffix=".TR", rule="none", sample_local="THYAO", evidence=EVIDENCE),
    ],
    "segment_only_mic": [
        SuffixRow(
            mic="XKRX",
            suffix=".KQ",
            rule="segment:XKOS",
            sample_local="247540",
            evidence=EVIDENCE,
        ),
    ],
    "unknown_rule": [
        SuffixRow(mic="XIST", suffix=".IS", rule="dash", sample_local="THYAO", evidence=EVIDENCE),
    ],
    "unknown_evidence_status": [
        SuffixRow(
            mic="XIST",
            suffix=".IS",
            rule="none",
            sample_local="THYAO",
            evidence="seen:2026-11-08",
        ),
    ],
    "lower_case_mic": [
        SuffixRow(mic="xist", suffix=".IS", rule="none", sample_local="THYAO", evidence=EVIDENCE),
    ],
}


@pytest.mark.parametrize("name", sorted(_BAD_SUFFIX_TABLES))
def test_suffix_tables_refuse_ambiguity_and_missing_primaries(name: str) -> None:
    with pytest.raises(ReferenceDataError):
        _suffix(_BAD_SUFFIX_TABLES[name])


def test_a_prefix_grammar_round_trips_code_and_local() -> None:
    grammar = PrefixGrammar(
        "tradingview",
        [
            PrefixRow(
                code="BIST",
                mic="XIST",
                primary=True,
                market="turkey",
                sample_local="THYAO",
                evidence=EVIDENCE,
            ),
            PrefixRow(
                code="CSELK",
                mic="XCOL",
                primary=True,
                market="srilanka",
                sample_local="CERA.N0000",
                evidence=EVIDENCE,
            ),
        ],
    )
    assert grammar.to_source("THYAO", "XIST") == "BIST:THYAO"
    assert grammar.from_source("BIST:THYAO") == ("THYAO", "XIST")
    assert grammar.to_source("CERA.N0000", "XCOL") == "CSELK:CERA.N0000"
    assert grammar.from_source("CSELK:CERA.N0000") == ("CERA.N0000", "XCOL")


def _hstc_grammar() -> PrefixGrammar:
    return PrefixGrammar(
        "tradingview",
        [
            PrefixRow(
                code="HNX",
                mic="HSTC",
                primary=True,
                market="vietnam",
                sample_local="SHS",
                evidence=EVIDENCE,
            ),
            PrefixRow(
                code="UPCOM",
                mic="HSTC",
                primary=False,
                market="vietnam",
                sample_local="LKW",
                evidence=EVIDENCE,
            ),
        ],
    )


def test_a_prefix_grammar_writes_the_primary_code_and_reads_every_code() -> None:
    grammar = _hstc_grammar()
    assert grammar.to_source("SHS", "HSTC") == "HNX:SHS"
    assert grammar.from_source("UPCOM:LKW") == ("LKW", "HSTC")


def test_a_prefix_grammar_refuses_unknown_codes_and_malformed_symbols() -> None:
    grammar = _hstc_grammar()
    with pytest.raises(UnknownSourceSymbol):
        grammar.from_source("NOPE:X")
    for raw in ("THYAO", "HNX:", ":SHS", "HNX:A:B"):
        with pytest.raises(GrammarError):
            grammar.from_source(raw)
    with pytest.raises(GrammarError):
        grammar.from_source("HNX:SHS", mic="XIST")


_BAD_PREFIX_TABLES: dict[str, list[PrefixRow]] = {
    "zero_primaries": [
        PrefixRow(
            code="BIST",
            mic="XIST",
            primary=False,
            market="turkey",
            sample_local="THYAO",
            evidence=EVIDENCE,
        ),
    ],
    "two_primaries": [
        PrefixRow(
            code="BIST",
            mic="XIST",
            primary=True,
            market="turkey",
            sample_local="THYAO",
            evidence=EVIDENCE,
        ),
        PrefixRow(
            code="XIST",
            mic="XIST",
            primary=True,
            market="turkey",
            sample_local="THYAO",
            evidence=EVIDENCE,
        ),
    ],
    "duplicate_code": [
        PrefixRow(
            code="BIST",
            mic="XIST",
            primary=True,
            market="turkey",
            sample_local="THYAO",
            evidence=EVIDENCE,
        ),
        PrefixRow(
            code="BIST",
            mic="XAMS",
            primary=True,
            market="netherlands",
            sample_local="ASML",
            evidence=EVIDENCE,
        ),
    ],
}


@pytest.mark.parametrize("name", sorted(_BAD_PREFIX_TABLES))
def test_prefix_tables_need_exactly_one_primary_per_mic(name: str) -> None:
    with pytest.raises(ReferenceDataError):
        PrefixGrammar("tradingview", _BAD_PREFIX_TABLES[name])


def test_the_market_lookups_follow_the_rows() -> None:
    grammar = PrefixGrammar(
        "tradingview",
        [
            PrefixRow(
                code="BIST",
                mic="XIST",
                primary=True,
                market="turkey",
                sample_local="THYAO",
                evidence=EVIDENCE,
            ),
            *_hstc_grammar().rows,
        ],
    )
    assert grammar.market_for("XIST") == "turkey"
    assert grammar.market_for("HSTC") == "vietnam"
    assert grammar.mics_for_market("vietnam") == frozenset({"HSTC"})
    assert grammar.markets() == frozenset({"turkey", "vietnam"})
    assert grammar.codes() == frozenset({"BIST", "HNX", "UPCOM"})
    assert grammar.supported_mics() == frozenset({"XIST", "HSTC"})
    with pytest.raises(NoGrammarForMarket) as caught:
        grammar.market_for("XNYS")
    assert caught.value.mic == "XNYS"


def test_grammar_errors_stay_inside_the_market_hierarchy() -> None:
    error = GrammarError("yahoo_chart", "bad")
    assert isinstance(error, MarketError)
    assert isinstance(error, CrocodileError)
    assert isinstance(error, ValueError)
    assert isinstance(
        AmbiguousSourceSymbol("yahoo_chart", "IBM", ("XNAS", "XNYS")),
        GrammarError,
    )
    unknown = UnknownSourceSymbol("yahoo_chart", "THYAO.ZZ")
    assert isinstance(unknown, NoGrammarForMarket)
    assert isinstance(unknown, LookupError)
    from crocodile.core.markets import grammar

    assert grammar.NoGrammarForMarket is market_errors.NoGrammarForMarket


_ERROR_SAMPLES: list[Any] = [
    GrammarError("yahoo_chart", "bad symbol"),
    UnknownSourceSymbol("yahoo_chart", "THYAO.ZZ"),
    AmbiguousSourceSymbol("yahoo_chart", "IBM", ("XNAS", "XNYS")),
]


@pytest.mark.parametrize("error", _ERROR_SAMPLES, ids=lambda err: type(err).__name__)
def test_grammar_errors_survive_pickle_copy_and_deepcopy(error: Any) -> None:
    # nosemgrep: python.lang.security.deserialization.pickle.avoid-pickle
    restored_pickle = pickle.loads(pickle.dumps(error))
    restored_copy = copy.copy(error)
    restored_deep = copy.deepcopy(error)
    for restored in (restored_pickle, restored_copy, restored_deep):
        assert type(restored) is type(error)
        assert str(restored) == str(error)
        assert vars(restored) == vars(error)


def test_both_grammars_satisfy_the_protocol() -> None:
    assert isinstance(_suffix([_xist()]), SymbolGrammar)
    assert isinstance(_hstc_grammar(), SymbolGrammar)
