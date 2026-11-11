"""Gate: the packaged symbol grammars round-trip, cover operating MICs and match their evidence.

The three tables are ``data/grammar_yahoo_chart.csv`` (generated from the research matrix),
``data/grammar_google_finance.csv`` (generated) and ``data/grammar_tradingview.csv``
(curated, backed by the committed probe responses under
``tests/core/markets/fixtures/tradingview_probe``). The builders are loaded from
``scripts/build_markets_reference.py`` and must reproduce the committed CSVs exactly.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from crocodile.core.markets import _data
from crocodile.core.markets.errors import NoGrammarForMarket
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.grammar import (
    EVIDENCE_STATUSES,
    GRAMMAR_SOURCES,
    GrammarError,
    PrefixRow,
    SuffixRow,
    UnknownSourceSymbol,
    load_grammar,
)
from crocodile.core.markets.mic import MicRegistry
from crocodile.core.markets.symbols import listing_symbol

pytestmark = pytest.mark.usefixtures("network_forbidden")

_REPO = Path(__file__).resolve().parents[2]
_BUILD_SCRIPT = _REPO / "scripts" / "build_markets_reference.py"
_MATRIX = _REPO / "docs" / "superpowers" / "research" / "data" / "market-matrix-2026-11-08.json"
_PROBES = _REPO / "tests" / "core" / "markets" / "fixtures" / "tradingview_probe"

_TRADINGVIEW_MICS = frozenset(
    {
        "ASEX",
        "BIVA",
        "BMEX",
        "BVCA",
        "BVMF",
        "DIFX",
        "DSMD",
        "HSTC",
        "NEOE",
        "ROCO",
        "TGAT",
        "XADS",
        "XASX",
        "XBAH",
        "XBEL",
        "XBKK",
        "XBOG",
        "XBOM",
        "XBRN",
        "XBSE",
        "XBUD",
        "XBUE",
        "XBUL",
        "XCAI",
        "XCAS",
        "XCNQ",
        "XCOL",
        "XCSE",
        "XCYS",
        "XDFM",
        "XDHA",
        "XDUS",
        "XETR",
        "XFKA",
        "XFRA",
        "XHAM",
        "XHAN",
        "XHEL",
        "XHKG",
        "XICE",
        "XIDX",
        "XIST",
        "XJPX",
        "XKAR",
        "XKLS",
        "XKRX",
        "XLIM",
        "XLIT",
        "XLJU",
        "XLON",
        "XLUX",
        "XMEX",
        "XMIL",
        "XMUN",
        "XMUS",
        "XNAI",
        "XNAS",
        "XNGM",
        "XNGO",
        "XNSA",
        "XNSE",
        "XNYS",
        "XNZE",
        "XOSL",
        "XPHS",
        "XPRA",
        "XRIS",
        "XSAP",
        "XSAU",
        "XSES",
        "XSGO",
        "XSHE",
        "XSTC",
        "XSTO",
        "XSTU",
        "XSWX",
        "XTAI",
        "XTAL",
        "XTSE",
        "XTSX",
        "XTUN",
        "XWAR",
        "XWBO",
        "XZAG",
    }
)
_GOOGLE_MICS = frozenset({"XIST", "XNAS", "XNYS"})
_YAHOO_HEADER = ("mic", "suffix", "rule", "sample_local", "evidence")
_GOOGLE_HEADER = ("mic", "google_code", "sample_local", "evidence")

_ROWS: list[tuple[str, Any]] = [
    (source, row) for source in GRAMMAR_SOURCES for row in load_grammar(source).rows
]


def _row_id(source: str, row: Any) -> str:
    key = row.suffix or "unsuffixed" if isinstance(row, SuffixRow) else row.code
    return f"{source}-{row.mic}-{key}"


_IDS = [_row_id(source, row) for source, row in _ROWS]


def _segment_of(row: Any) -> str | None:
    """The segment a row's rule names, or ``None`` for a plain or class-share row."""
    if isinstance(row, SuffixRow) and row.is_segment:
        return row.rule.split(":", 1)[1]
    return None


def _spelling_is_unique(row: Any) -> bool:
    """A non-empty suffix or a TradingView code identifies its row without a hint."""
    return isinstance(row, PrefixRow) or bool(row.suffix)


def _load_build_script() -> ModuleType:
    was_loaded = "exchange_calendars" in sys.modules
    spec = importlib.util.spec_from_file_location("build_markets_reference", _BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not was_loaded:
        assert "exchange_calendars" not in sys.modules
    return module


@pytest.mark.parametrize(("source", "row"), _ROWS, ids=_IDS)
def test_every_row_round_trips(source: str, row: Any) -> None:
    grammar = load_grammar(source)
    segment = _segment_of(row)
    spelled = grammar.to_source(row.sample_local, row.mic, segment_mic=segment)
    assert grammar.from_source(spelled, mic=segment or row.mic) == (row.sample_local, row.mic)
    if _spelling_is_unique(row):
        assert grammar.from_source(spelled) == (row.sample_local, row.mic)


@pytest.mark.parametrize(("source", "row"), _ROWS, ids=_IDS)
def test_every_table_mic_is_an_operating_mic_in_exchanges_csv(source: str, row: Any) -> None:
    assert ExchangeRegistry().get(row.mic).mic == row.mic
    assert MicRegistry().operating(row.mic) == row.mic


_SEGMENT_ROWS = [(source, row) for source, row in _ROWS if _segment_of(row) is not None]


@pytest.mark.parametrize(
    ("source", "row"),
    _SEGMENT_ROWS,
    ids=[_row_id(source, row) for source, row in _SEGMENT_ROWS],
)
def test_every_segment_rule_names_a_segment_of_its_row_mic(source: str, row: Any) -> None:
    segment = _segment_of(row)
    assert segment is not None
    assert MicRegistry().operating(segment) == row.mic
    assert segment != row.mic


def test_each_mic_has_exactly_one_primary_spelling() -> None:
    yahoo = Counter(row.mic for row in load_grammar("yahoo_chart").rows if not _segment_of(row))
    assert set(yahoo) == {row.mic for row in load_grammar("yahoo_chart").rows}
    assert set(yahoo.values()) == {1}
    tradingview = Counter(row.mic for row in load_grammar("tradingview").rows if row.primary)
    assert set(tradingview) == {row.mic for row in load_grammar("tradingview").rows}
    assert set(tradingview.values()) == {1}
    google = Counter(row.mic for row in load_grammar("google_finance").rows)
    assert set(google.values()) == {1}


@pytest.mark.parametrize(("source", "row"), _ROWS, ids=_IDS)
def test_every_sample_local_is_a_valid_listing_local(source: str, row: Any) -> None:
    listing_symbol(row.sample_local, row.mic)


@pytest.mark.parametrize(("source", "row"), _ROWS, ids=_IDS)
def test_every_evidence_cell_names_a_known_status_and_a_date(source: str, row: Any) -> None:
    status, _, day = row.evidence.partition(":")
    assert status in EVIDENCE_STATUSES
    # TradingView rows: the 2026-11-08 probes, or the 2026-11-11 market sweep (task 11b)
    dates = {date(2026, 11, 8)}
    if source == "tradingview":
        dates.add(date(2026, 11, 11))
    assert date.fromisoformat(day) in dates


@pytest.mark.parametrize("source", GRAMMAR_SOURCES)
def test_an_unknown_market_has_no_grammar(source: str) -> None:
    grammar = load_grammar(source)
    with pytest.raises(NoGrammarForMarket):
        grammar.to_source("X", "ZZZZ")
    supported = grammar.supported_mics()
    missing = next(info.mic for info in ExchangeRegistry().all() if info.mic not in supported)
    with pytest.raises(NoGrammarForMarket):
        grammar.to_source("X", missing)


@pytest.mark.parametrize(
    ("source", "raw"),
    [
        ("yahoo_chart", "THYAO.ZZ"),
        ("google_finance", "THYAO:ZZZ"),
        ("google_finance", ".INX:INDEXSP"),
        ("tradingview", "ZZZ:THYAO"),
    ],
)
def test_an_unknown_suffix_or_code_is_refused(source: str, raw: str) -> None:
    with pytest.raises(UnknownSourceSymbol):
        load_grammar(source).from_source(raw)


def test_unsuffixed_yahoo_symbols_are_ambiguous_between_us_markets() -> None:
    from crocodile.core.markets.grammar import AmbiguousSourceSymbol

    with pytest.raises(AmbiguousSourceSymbol) as caught:
        load_grammar("yahoo_chart").from_source("AAPL")
    assert caught.value.candidates == ("XNAS", "XNYS")


def test_the_yahoo_and_google_tables_are_reproducible_from_the_matrix() -> None:
    build = _load_build_script()
    matrix = json.loads(_MATRIX.read_text(encoding="utf-8"))
    mics = {info.mic for info in ExchangeRegistry().all()}
    yahoo_rows = build.grammar_yahoo_chart_rows(matrix, mics)
    google_rows = build.grammar_google_finance_rows(matrix, mics)
    assert yahoo_rows == _data.read_csv("grammar_yahoo_chart.csv", _YAHOO_HEADER)
    assert google_rows == _data.read_csv("grammar_google_finance.csv", _GOOGLE_HEADER)


def test_tradingview_rows_are_backed_by_the_committed_probes() -> None:
    currencies = {info.mic: info.currency for info in ExchangeRegistry().all()}
    for row in load_grammar("tradingview").rows:
        path = _PROBES / f"{row.market}.json"
        assert path.is_file(), path
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["totalCount"] > 0, row.market
        backed = any(
            item["s"] == f"{row.code}:{row.sample_local}"
            and item["d"][0] == row.sample_local
            and item["d"][2] == currencies[row.mic]
            and item["d"][3] == row.code
            for item in payload["data"]
        )
        assert backed, f"{row.code}:{row.sample_local} is not in {path.name}"


def test_the_tables_hold_the_documented_markets() -> None:
    assert load_grammar("tradingview").supported_mics() == _TRADINGVIEW_MICS
    assert load_grammar("google_finance").supported_mics() == _GOOGLE_MICS


def test_load_grammar_refuses_an_unknown_source() -> None:
    with pytest.raises(GrammarError):
        load_grammar("stooq")
    assert load_grammar("yahoo_chart") is load_grammar("yahoo_chart")
