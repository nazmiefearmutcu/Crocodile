"""Source symbol grammars: how a data source spells a listing, and the way back.

A source writes the same listing in its own spelling. Yahoo appends a market suffix
(``THYAO.IS``), Google Finance appends ``:CODE`` (``THYAO:IST``) and TradingView
prefixes the exchange code (``BIST:THYAO``). A grammar maps such a spelling to
``(local, operating MIC)`` and back. The rows of ``data/grammar_<source>.csv`` say which
markets a source covers. An unknown market, suffix or code is refused with
``NoGrammarForMarket`` (or its subclass ``UnknownSourceSymbol``) and never guessed.

Segments. A row may name an ISO 10383 segment in its rule (``segment:XKOS``). Yahoo spells
KOSDAQ ``.KQ`` and KOSPI ``.KS``, but both trade on the operating MIC ``XKRX``: the
``segment_mic`` argument of ``to_source`` selects the spelling of a segment. Every MIC
returned here is an operating MIC.

Class shares. Yahoo writes ``BRK-B`` where the exchange writes ``BRK.B``. The
``us_class_dash`` rule maps the dot to a dash and back, and refuses a local that already
holds a dash, so the mapping stays reversible.

This module imports only the standard library and ``crocodile.core.markets`` errors and
packaged-data loader. It never reads the filesystem except through ``_data.read_csv``.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Final, Protocol, runtime_checkable

from crocodile.core.markets._data import read_csv
from crocodile.core.markets.errors import MarketError, NoGrammarForMarket, ReferenceDataError

__all__ = [
    "EVIDENCE_STATUSES",
    "GRAMMAR_SOURCES",
    "AmbiguousSourceSymbol",
    "GrammarError",
    "NoGrammarForMarket",
    "PrefixGrammar",
    "PrefixRow",
    "SuffixGrammar",
    "SuffixRow",
    "SymbolGrammar",
    "UnknownSourceSymbol",
    "load_grammar",
]

EVIDENCE_STATUSES: Final = frozenset({"verified", "status_only", "connector"})
"""verified    = HTTP 200 and data observed for the sample symbol (matrix status or probe file);
status_only = HTTP 200 observed, payload not read (matrix status);
connector   = inherited from a pre-existing Crocodile connector's hard-coded candidates,
              not re-probed (Google US rows)."""

GRAMMAR_SOURCES: Final = ("google_finance", "tradingview", "yahoo_chart")

_MIC_RE: Final = re.compile(r"[A-Z0-9]{4}")
_CODE_RE: Final = re.compile(r"[A-Z0-9_]+")
_MARKET_RE: Final = re.compile(r"[a-z][a-z0-9_]*")
_SUFFIX_BODY_RE: Final = re.compile(r"[A-Z0-9]{1,16}")
_GOOGLE_CODE_RE: Final = re.compile(r"[A-Z0-9]{1,16}")
_RULE_RE: Final = re.compile(r"none|us_class_dash|segment:[A-Z0-9]{4}")
_EVIDENCE_RE: Final = re.compile(r"(?:verified|status_only|connector):(\d{4}-\d{2}-\d{2})")
_NO_RULE: Final = "none"
_US_RULE: Final = "us_class_dash"
_SEGMENT_PREFIX: Final = "segment:"
_REFUSED_CATEGORIES: Final = frozenset({"Zs", "Zl", "Zp", "Cc", "Cf", "Cs"})
_REFUSED_CHARACTERS: Final = frozenset({"@", ":"})

_YAHOO_FILE: Final = "grammar_yahoo_chart.csv"
_YAHOO_HEADER: Final = ("mic", "suffix", "rule", "sample_local", "evidence")
_GOOGLE_FILE: Final = "grammar_google_finance.csv"
_GOOGLE_HEADER: Final = ("mic", "google_code", "sample_local", "evidence")
_TRADINGVIEW_FILE: Final = "grammar_tradingview.csv"
_TRADINGVIEW_HEADER: Final = ("tv_code", "mic", "primary", "market", "sample_local", "evidence")


class GrammarError(MarketError, ValueError):
    """A source symbol is malformed, or contradicts the market it was said to belong to."""

    def __init__(self, source: str, detail: str) -> None:
        self.source = source
        self.detail = detail
        MarketError.__init__(self, source, detail)

    def __str__(self) -> str:
        return f"source {self.source!r} symbol grammar: {self.detail}"


class UnknownSourceSymbol(NoGrammarForMarket):
    """``from_source`` met a suffix or code that no row covers.

    An unknown suffix is ``NoGrammarForMarket`` (spec 5). No market could be determined,
    so ``mic`` is the empty string.
    """

    def __init__(self, source: str, raw: str) -> None:
        self.source = source
        self.mic = ""
        self.raw = raw
        MarketError.__init__(self, source, raw)

    def __str__(self) -> str:
        return f"source {self.source!r} has no symbol grammar for {self.raw!r}"


class AmbiguousSourceSymbol(GrammarError):
    """``from_source`` matched rows of several markets and no ``mic=`` hint chose one."""

    def __init__(self, source: str, raw: str, candidates: tuple[str, ...]) -> None:
        self.source = source
        self.raw = raw
        self.candidates = candidates
        self.detail = f"{raw!r} is shared by {', '.join(candidates)}; pass mic= to choose"
        MarketError.__init__(self, source, raw, candidates)

    def __str__(self) -> str:
        return f"source {self.source!r} symbol {self.raw!r} is ambiguous: {self.detail}"


@runtime_checkable
class SymbolGrammar(Protocol):
    """The common surface of the suffix and prefix grammars."""

    @property
    def source(self) -> str: ...

    def to_source(self, local: str, mic: str, *, segment_mic: str | None = None) -> str: ...

    def from_source(self, raw: str, *, mic: str | None = None) -> tuple[str, str]: ...

    def supported_mics(self) -> frozenset[str]: ...


@dataclass(frozen=True, slots=True)
class SuffixRow:
    """One market of a suffix grammar. ``suffix`` is appended verbatim, or empty."""

    mic: str
    suffix: str
    rule: str
    sample_local: str
    evidence: str

    @property
    def is_segment(self) -> bool:
        """True when the rule names an ISO 10383 segment of ``mic``."""
        return self.rule.startswith(_SEGMENT_PREFIX)


@dataclass(frozen=True, slots=True)
class PrefixRow:
    """One exchange code of a prefix grammar; ``primary`` marks the code written for a MIC."""

    code: str
    mic: str
    primary: bool
    market: str
    sample_local: str
    evidence: str


def _local_problem(local: str) -> str | None:
    """Describe why ``local`` is not a usable local part, or return ``None``."""
    if not local:
        return "the local part is empty"
    for char in local:
        if char in _REFUSED_CHARACTERS:
            return f"the local part holds {char!r}"
        if unicodedata.category(char) in _REFUSED_CATEGORIES or char.isspace():
            return f"the local part holds whitespace or a control character ({char!r})"
    return None


def _require_local(source: str, local: str) -> None:
    problem = _local_problem(local)
    if problem is not None:
        raise GrammarError(source, problem)


def _is_iso_date(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _check_common(resource: str, mic: str, sample_local: str, evidence: str) -> None:
    """Validate the fields every row shares. Any violation raises ``ReferenceDataError``."""
    if not _MIC_RE.fullmatch(mic):
        raise ReferenceDataError(resource, f"mic {mic!r} is not four upper-case letters or digits")
    problem = _local_problem(sample_local)
    if problem is not None:
        raise ReferenceDataError(resource, f"sample_local {sample_local!r}: {problem}")
    match = _EVIDENCE_RE.fullmatch(evidence)
    if match is None or not _is_iso_date(match.group(1)):
        raise ReferenceDataError(resource, f"evidence {evidence!r} is not <status>:<YYYY-MM-DD>")


class SuffixGrammar:
    """Suffix spelling: Yahoo appends ``.IS`` after the local part, Google appends ``:IST``."""

    def __init__(self, source: str, rows: Iterable[SuffixRow], *, separator: str) -> None:
        self._source = source
        self._separator = separator
        table = tuple(rows)
        resource = f"grammar_{source}.csv"
        by_suffix: dict[str, SuffixRow] = {}
        primary: dict[str, SuffixRow] = {}
        segments: dict[str, SuffixRow] = {}
        for row in table:
            _check_common(resource, row.mic, row.sample_local, row.evidence)
            if row.suffix:
                body = row.suffix[len(separator) :]
                if not row.suffix.startswith(separator) or not _SUFFIX_BODY_RE.fullmatch(body):
                    raise ReferenceDataError(
                        resource,
                        f"suffix {row.suffix!r} for {row.mic} is not {separator!r} followed by "
                        "1 to 16 upper-case letters or digits",
                    )
                if row.suffix in by_suffix:
                    raise ReferenceDataError(resource, f"suffix {row.suffix!r} is on two rows")
                by_suffix[row.suffix] = row
            if not _RULE_RE.fullmatch(row.rule):
                raise ReferenceDataError(resource, f"rule {row.rule!r} for {row.mic} is unknown")
            if row.is_segment:
                segment = row.rule[len(_SEGMENT_PREFIX) :]
                if segment == row.mic:
                    raise ReferenceDataError(resource, f"{row.mic} names itself as its segment")
                if segment in segments:
                    raise ReferenceDataError(resource, f"segment {segment} is on two rows")
                segments[segment] = row
            elif row.mic in primary:
                raise ReferenceDataError(resource, f"{row.mic} has two non-segment rows")
            else:
                primary[row.mic] = row
        missing = sorted({row.mic for row in table} - set(primary))
        if missing:
            raise ReferenceDataError(resource, f"{missing[0]} has no non-segment row")
        self._rows = table
        self._by_suffix = by_suffix
        self._primary = primary
        self._segments = segments
        self._unsuffixed = tuple(row for row in table if not row.suffix)

    @property
    def source(self) -> str:
        return self._source

    @property
    def separator(self) -> str:
        return self._separator

    @property
    def rows(self) -> tuple[SuffixRow, ...]:
        return self._rows

    def supported_mics(self) -> frozenset[str]:
        return frozenset(row.mic for row in self._rows)

    def to_source(self, local: str, mic: str, *, segment_mic: str | None = None) -> str:
        """Spell ``local`` on operating ``mic`` the way this source writes it."""
        _require_local(self._source, local)
        row = self._writing_row(mic, segment_mic)
        spelled = local
        if row.rule == _US_RULE:
            if "-" in local:
                raise GrammarError(
                    self._source,
                    f"{local!r} already holds a dash, so the class-share spelling would not "
                    "be reversible",
                )
            spelled = local.replace(".", "-")
        return spelled + row.suffix

    def from_source(self, raw: str, *, mic: str | None = None) -> tuple[str, str]:
        """Read ``raw`` back to ``(local, operating MIC)``. Matching is case-sensitive."""
        if not raw:
            raise GrammarError(self._source, "the symbol is empty")
        cut = raw.rfind(self._separator)
        if cut == 0:
            raise GrammarError(self._source, f"{raw!r} has no local part before the suffix")
        if cut > 0:
            row = self._by_suffix.get(raw[cut:])
            if row is None:
                raise UnknownSourceSymbol(self._source, raw)
            stem = raw[:cut]
            candidates: tuple[SuffixRow, ...] = (row,)
        else:
            if not self._unsuffixed:
                raise UnknownSourceSymbol(self._source, raw)
            stem = raw
            candidates = self._unsuffixed
        _require_local(self._source, stem)
        chosen = self._choose(raw, candidates, mic)
        local = stem.replace("-", ".") if chosen.rule == _US_RULE else stem
        return local, chosen.mic

    def _writing_row(self, mic: str, segment_mic: str | None) -> SuffixRow:
        if segment_mic is not None and segment_mic in self._segments:
            segment_row = self._segments[segment_mic]
            if segment_row.mic != mic:
                raise GrammarError(
                    self._source, f"segment {segment_mic} belongs to {segment_row.mic}, not {mic}"
                )
            return segment_row
        primary_row = self._primary.get(mic)
        if primary_row is None:
            raise NoGrammarForMarket(self._source, mic)
        return primary_row

    def _choose(self, raw: str, candidates: tuple[SuffixRow, ...], mic: str | None) -> SuffixRow:
        if mic is None:
            mics = sorted({row.mic for row in candidates})
            if len(mics) > 1:
                raise AmbiguousSourceSymbol(self._source, raw, tuple(mics))
        else:
            kept = tuple(
                row for row in candidates if row.mic == mic or row.rule == _SEGMENT_PREFIX + mic
            )
            if not kept:
                raise GrammarError(self._source, f"{raw!r} does not belong to market {mic}")
            candidates = kept
            for row in candidates:
                if row.rule == _SEGMENT_PREFIX + mic:
                    return row
        for row in candidates:
            if not row.is_segment:
                return row
        return candidates[0]


class PrefixGrammar:
    """Prefix spelling: TradingView writes ``CODE:LOCAL`` with one code per exchange."""

    def __init__(self, source: str, rows: Iterable[PrefixRow]) -> None:
        self._source = source
        table = tuple(rows)
        resource = f"grammar_{source}.csv"
        by_code: dict[str, PrefixRow] = {}
        primary: dict[str, PrefixRow] = {}
        for row in table:
            if not _CODE_RE.fullmatch(row.code):
                raise ReferenceDataError(resource, f"code {row.code!r} is not upper-case")
            _check_common(resource, row.mic, row.sample_local, row.evidence)
            if not _MARKET_RE.fullmatch(row.market):
                raise ReferenceDataError(resource, f"market {row.market!r} is not a name")
            if row.code in by_code:
                raise ReferenceDataError(resource, f"code {row.code} is on two rows")
            by_code[row.code] = row
            if row.primary:
                if row.mic in primary:
                    raise ReferenceDataError(resource, f"{row.mic} has two primary codes")
                primary[row.mic] = row
        missing = sorted({row.mic for row in table} - set(primary))
        if missing:
            raise ReferenceDataError(resource, f"{missing[0]} has no primary code")
        self._rows = table
        self._by_code = by_code
        self._primary = primary

    @property
    def source(self) -> str:
        return self._source

    @property
    def rows(self) -> tuple[PrefixRow, ...]:
        return self._rows

    def supported_mics(self) -> frozenset[str]:
        return frozenset(row.mic for row in self._rows)

    def codes(self) -> frozenset[str]:
        """Every exchange code, primary or not."""
        return frozenset(self._by_code)

    def markets(self) -> frozenset[str]:
        return frozenset(row.market for row in self._rows)

    def market_for(self, mic: str) -> str:
        """The TradingView scanner market of ``mic``, from its primary row."""
        row = self._primary.get(mic)
        if row is None:
            raise NoGrammarForMarket(self._source, mic)
        return row.market

    def mics_for_market(self, market: str) -> frozenset[str]:
        return frozenset(row.mic for row in self._rows if row.market == market)

    def to_source(self, local: str, mic: str, *, segment_mic: str | None = None) -> str:
        """Spell ``local`` with the primary code of ``mic``. Segments are not modelled here."""
        _require_local(self._source, local)
        row = self._primary.get(mic)
        if row is None:
            raise NoGrammarForMarket(self._source, mic)
        return f"{row.code}:{local}"

    def from_source(self, raw: str, *, mic: str | None = None) -> tuple[str, str]:
        """Read ``CODE:LOCAL`` back to ``(local, operating MIC)``. Every code is accepted."""
        if not raw:
            raise GrammarError(self._source, "the symbol is empty")
        cut = raw.find(":")
        if cut == -1:
            raise GrammarError(self._source, f"{raw!r} has no ':' between code and local part")
        code, local = raw[:cut], raw[cut + 1 :]
        if not code:
            raise GrammarError(self._source, f"{raw!r} has no exchange code")
        _require_local(self._source, local)
        row = self._by_code.get(code)
        if row is None:
            raise UnknownSourceSymbol(self._source, raw)
        if mic is not None and mic != row.mic:
            raise GrammarError(self._source, f"{raw!r} is on {row.mic}, not {mic}")
        return local, row.mic


@functools.cache
def load_grammar(source: str) -> SuffixGrammar | PrefixGrammar:
    """Build the grammar of ``source`` from its packaged table, once per process."""
    if source == "yahoo_chart":
        yahoo_rows = [
            SuffixRow(
                mic=row["mic"],
                suffix=row["suffix"],
                rule=row["rule"],
                sample_local=row["sample_local"],
                evidence=row["evidence"],
            )
            for row in read_csv(_YAHOO_FILE, _YAHOO_HEADER)
        ]
        return SuffixGrammar(source, yahoo_rows, separator=".")
    if source == "google_finance":
        google_rows: list[SuffixRow] = []
        for row in read_csv(_GOOGLE_FILE, _GOOGLE_HEADER):
            code = row["google_code"]
            if not _GOOGLE_CODE_RE.fullmatch(code):
                raise ReferenceDataError(_GOOGLE_FILE, f"google_code {code!r} is malformed")
            google_rows.append(
                SuffixRow(
                    mic=row["mic"],
                    suffix=":" + code,
                    rule=_NO_RULE,
                    sample_local=row["sample_local"],
                    evidence=row["evidence"],
                )
            )
        return SuffixGrammar(source, google_rows, separator=":")
    if source == "tradingview":
        tv_rows = [
            PrefixRow(
                code=row["tv_code"],
                mic=row["mic"],
                primary=_flag(row["primary"]),
                market=row["market"],
                sample_local=row["sample_local"],
                evidence=row["evidence"],
            )
            for row in read_csv(_TRADINGVIEW_FILE, _TRADINGVIEW_HEADER)
        ]
        return PrefixGrammar(source, tv_rows)
    raise GrammarError(source, f"known: {', '.join(GRAMMAR_SOURCES)}")


def _flag(value: str) -> bool:
    if value == "1":
        return True
    if value == "0":
        return False
    raise ReferenceDataError(_TRADINGVIEW_FILE, f"primary {value!r} must be 1 or 0")
