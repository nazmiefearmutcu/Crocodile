"""The equity instrument universe, resolved from three registries that disagree.

``SPEC_METHODS`` M3 — "Equity universe from SEC EDGAR x OpenFIGI x Tiingo, merged by
CoverageResolver" — names three sources and a merge machine, and the merge machine already
existed: :class:`~crocodile.core.coverage.resolver.CoverageResolver` arrived with the equity
fork and its ``fill_nulls`` strategy is exactly "start from the highest-priority row and take
each missing field from the next one down". What did not exist was anything to hand it. The
comment scheduling ``collect-market`` states the gap precisely: ``Provider.list_instruments``
describes the symbols it was handed rather than discovering any, so no equity source in the
tree enumerated a universe at all. This module is the enumeration.

**Why these three, and what each one is for.** They are not redundant, which is why the merge
is a merge rather than a fallback chain:

SEC EDGAR ``company_tickers.json``
    The registrant index. It is the only source of a CIK and the only source of a
    registrant-attested company name — both of which it therefore wins in the merge, the
    second only since ``REFERENCE_PRIORITY`` was corrected to put it above OpenFIGI — and it
    is keyless: one static file, no token, no pagination. It knows nothing about *where* a
    security trades, because a filer is not a listing.
Tiingo ``supported_tickers.zip``
    The listing index. It is the only source that names an exchange for every row and the
    only one that publishes a price currency, and its file is also keyless — the endpoint's
    own docstring in ``providers/tiingo/client.py`` says it "does not require an API token or
    count towards quota". Tiingo is the source M3 might have been expected to need a key for,
    and this is the one endpoint of theirs that does not, which is what keeps the whole
    method keyless.
OpenFIGI ``/v3/mapping``
    The identifier registry: FIGI, composite and share-class FIGI, an exchange code and a
    security type, per ticker. It is the only *per-symbol* source of the three, and its
    keyless tier is twenty-five requests a minute in batches of ten — so it cannot be run
    across a ninety-thousand-row universe and is deliberately applied to the slice a caller
    asked for instead. See :meth:`ReferenceEvidence.with_figi`.

**What that costs, and where it is reported.** A row nobody enriched with FIGI is attested by
two sources rather than three, and the ``reference_merge`` confidence formula divides by a
fixed three precisely so that this reads as 0.67 rather than as a full house. That is the
product promise being kept rather than dodged: a keyless path that degrades and says so beats
a hard requirement for a key.

**Why a** :class:`Listing` **rather than a bare** :class:`~crocodile.core.schema.records.
Instrument`. Two facts the resolution produces have no field on the canonical record. The
price currency is one — only Tiingo publishes it, and no equity record in the tree carries a
currency, so widening the canonical union for it would be a lake-wide schema change to hold a
value one of three sources reports. The attestation set is the other, and it is not record
data at all: it is evidence *about* the record, which is what ``prov_confidence`` already
encodes as a ratio. Keeping both beside the instrument rather than inside it means the record
written into a ``channel=instrument/`` partition stays the record the union declares.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Final

import polars as pl

from crocodile.core.config import Settings
from crocodile.core.coverage.resolver import CoverageResolver
from crocodile.core.schema.enums import AssetClass, SecurityType
from crocodile.core.schema.provenance import ProvenanceFields, provenance_fields
from crocodile.core.schema.records import Instrument
from crocodile.equity.providers.openfigi.client import OpenFigiClient
from crocodile.equity.providers.openfigi.models import FigiRecord, OpenFigiJob
from crocodile.equity.providers.sec_edgar.client import (
    SecCompanyTicker,
    SecEdgarClient,
    require_user_agent,
)
from crocodile.equity.providers.tiingo.client import TiingoClient, TiingoTicker

__all__ = [
    "REFERENCE_MERGE_BASIS",
    "REFERENCE_PRIORITY",
    "SOURCE_OPENFIGI",
    "SOURCE_SEC",
    "SOURCE_TIINGO",
    "TIINGO_US_EXCHANGES",
    "VOLUME_RANK_SQL",
    "Listing",
    "ReferenceEvidence",
    "fetch_bulk_evidence",
    "fetch_figi",
    "filter_listings",
    "instruments_from_figi",
    "instruments_from_sec",
    "instruments_from_tiingo",
    "parse_kinds",
    "rank_listings",
    "require_sec_user_agent",
    "volume_by_symbol",
]

SOURCE_SEC: Final = "sec_edgar"
SOURCE_TIINGO: Final = "tiingo"
SOURCE_OPENFIGI: Final = "openfigi"

log = logging.getLogger(__name__)

TIINGO_US_EXCHANGES: Final[Mapping[str, str | None]] = MappingProxyType(
    {
        # Code-derived: the first five labels map to the MIC that exchanges.csv names for them.
        "NASDAQ": "XNAS",
        "NYSE": "XNYS",
        "NYSE ARCA": "XNYS",  # ISO 10383 segment ARCX of XNYS
        "NYSE MKT": "XNYS",  # ISO 10383 segment XASE (NYSE American) of XNYS
        "BATS": "XCBO",  # ISO 10383 segment BATS (Cboe BZX) of XCBO
        # observed in supported_tickers.zip on 2026-11-11: 374 rows, all USD
        "AMEX": "XNYS",  # NYSE American under its older label, segment XASE of XNYS
        # US-only venue names, listed defensively: none of them can admit a non-US listing.
        "NYSE NAT": "XNYS",  # US venue, ISO segment XCIS
        "IEX": "IEXG",  # US venue
        # US OTC tiers. Their ISO operating MICs (OTCM, FINR) are not in exchanges.csv, so
        # they carry no MIC here, but the rows are kept rather than dropped.
        "OTC": None,
        "OTCBB": None,
        "OTCQB": None,
        "OTCQX": None,
        "OTCGREY": None,
        "OTCMKTS": None,
        "PINK": None,
        "EXPM": None,
        # observed in supported_tickers.zip on 2026-11-11: 389 rows, all USD
        "OTCCE": None,
        "NMFQS": None,  # Nasdaq mutual-fund quotation service, US
    }
)
"""Tiingo exchange label -> operating MIC, or None for a US tier with no exchanges.csv MIC.

Keys are the upper-cased, stripped labels, the form :func:`instruments_from_tiingo` compares.
The first five labels are code-derived. The rest are US-only venue names whose presence cannot
admit a non-US listing. A label Tiingo never emits costs nothing. A US label missing from this
table drops its rows, and the log line names it.
"""

REFERENCE_MERGE_BASIS: Final = "reference_merge"
"""The registered basis every row this module emits rests on."""

REFERENCE_PRIORITY: Final[tuple[str, str, str]] = (SOURCE_TIINGO, SOURCE_SEC, SOURCE_OPENFIGI)
"""Who wins a field two sources both publish, highest first.

The order is an argument about what each source *is*, not about which is nicest. Exactly
three fields are contested — ``name``, ``exchange`` and ``security_type`` — and the order is
the answer to those three and nothing else; ``cik``, the FIGIs, ``currency`` and
``listing_date`` each have one publisher, so ``fill_nulls`` hands them over whatever the
order is.

Tiingo first because its rows are per **listing**. The question this universe answers is
"what trades, where", and a Tiingo row is one ticker on one exchange in one currency — the
grain of the answer. Its exchange name is also the human one (``NASDAQ``, ``NYSE``) rather
than a code, which is what makes ``markets`` readable, and its ``asset_type`` is a listing's
kind rather than an instrument's.

SEC second because it publishes exactly one contested field and is the right answer for it.
Its rows are per **registrant** — one filer covers every share class it issues — which is
why it has no exchange and no security type at all, and why the one thing it does report is
the registrant's *legal name*. That is what
:attr:`crocodile.core.schema.records.Instrument.name` is documented to hold, in
``providers/sec_edgar/client.py``: "the only registrant-attested company name in the tree".
It still wins every ``cik`` for the same reason it always did, because nothing else has one.

OpenFIGI last because its identifiers are **assigned** rather than inferred: a FIGI is
issued by a registrar and cannot be derived from anything else here, so where it speaks at
all it is authoritative — and it is uncontested there, so last place costs it nothing. On
the three contested fields it is the fallback and should be: its ``exch_code`` is a code
(``UW``, ``UN``) rather than a venue name, and its ``name`` is a security description
(``APPLE INC``) rather than a filer's legal name. It is also the only source not run over
the whole universe — its keyless tier is twenty-five requests a minute — so a source that
covers a slice cannot be the source that decides the shape of rows outside it.

**This order used to be** ``(TIINGO, OPENFIGI, SEC)``, **and the prose above it has always
described this one.** Three places said OpenFIGI's name loses to SEC's — here, this module's
header, and ``SecCompanyTicker``'s docstring — while ``fill_nulls`` is first-non-null-wins
and OpenFIGI outranked SEC, so SEC's ``title`` was unreachable on any row OpenFIGI had
matched. The test named ``test_sec_still_wins_the_name_over_openfigis_security_description``
asserted ``name == "APPLE INC"`` under a comment conceding the opposite, which is how it
survived: a test can certify the inverse of its own name and still be green.

What that cost is worth stating, because it is the reason the code moved rather than the
prose. ``Instrument.name`` carried two different quantities depending on which capability
produced the row: an unenriched row from ``census`` or ``collect-market`` had SEC's legal
name, and the same ticker out of ``universe`` — enriched, because a caller asked for a slice
— had OpenFIGI's uppercase description. One ticker, two conventions, decided by a code path
rather than by anything anyone chose. Moving SEC above OpenFIGI changes the resolution of
``name`` and of nothing else, since the other two contested fields are Tiingo's where Tiingo
speaks and OpenFIGI's where it does not, exactly as before. A ticker SEC does not register —
a foreign issuer, some ETF share classes — still takes OpenFIGI's description, so the
fallback is kept rather than discarded.
"""

VOLUME_RANK_SQL: Final = (
    'SELECT symbol, avg(volume) AS mean_volume FROM "ohlcv" '
    "WHERE volume > 0 GROUP BY symbol ORDER BY mean_volume DESC"
)
"""The volume evidence the ``top`` slice ranks on: mean traded volume per stored bar.

**The data source, named because a ranking without one is a league table nobody can check:
the lake's own ``channel=ohlcv/`` partitions.** Not a vendor screener and not a live quote —
the bars this deployment has already collected, whatever they are. That has a real cost,
stated here rather than discovered: a lake with no bars cannot rank anything, so the ``top``
slice refuses instead of answering, and the refusal names ``collect`` and ``backfill``.

**Why the mean rather than the sum.** The sum ranks by how long a symbol has been collected,
not by how much it trades: a ticker backfilled over two years outranks a more liquid one
picked up yesterday, and the ranking silently becomes a report on the collection schedule.
The mean divides that out and needs no window constant to do it — the alternative, "sum over
the last N days", would have to invent N, which is the move the provenance registry refuses
one layer down.

``WHERE volume > 0`` because a zero-volume bar is not evidence of low turnover. Quote-derived
bars carry a *structural* zero — ``ohlcv_from_quotes`` argues exactly that as the reason its
basis is SYNTHETIC — so counting them would drag a symbol's mean toward zero in proportion to
how many of its bars were built from quotes rather than trades.

``ORDER BY`` inside the statement rather than after it, so that a surface's ``row_limit``
truncates the *bottom* of the ranking. :meth:`CapabilityContext.query
<crocodile.core.capability.CapabilityContext.query>` wraps a capped read as ``SELECT * FROM
(…) LIMIT n``; over an unordered aggregate that would be an arbitrary n symbols wearing the
word "top".
"""

_TIINGO_ASSET_TYPES: Final[Mapping[str, SecurityType]] = {
    "stock": SecurityType.CS,
    "etf": SecurityType.ETF,
}
"""Tiingo's ``assetType`` vocabulary, mapped to ours.

Tiingo publishes three values and only two of them have a member here: its third is
``Mutual Fund``, which :class:`~crocodile.core.schema.enums.SecurityType` has no member for
and which is not a listed security. It maps to ``UNKNOWN`` through the fallback rather than
being invented as a member, because adding an enum member is a change to what the whole
product believes exists and it would arrive here from one vendor's spelling.
"""

_FIGI_SECURITY_TYPES: Final[Mapping[str, SecurityType]] = {
    "common stock": SecurityType.CS,
    "etp": SecurityType.ETF,
    "reit": SecurityType.REIT,
    "ad": SecurityType.ADR,
    "adr": SecurityType.ADR,
    "preference": SecurityType.PFD,
    "preferred": SecurityType.PFD,
    "right": SecurityType.RIGHT,
    "unit": SecurityType.UNIT,
    "warrant": SecurityType.WARRANT,
}
"""OpenFIGI's ``securityType`` vocabulary, mapped to ours, lower-cased on both sides.

Only the spellings that have a member. OpenFIGI's type list is long and mostly describes
instruments this enum does not model; anything outside this table becomes ``UNKNOWN``, which
is a member that exists precisely so an unmapped vendor string does not have to become a
guess.
"""


def _security_type(raw: str | None, table: Mapping[str, SecurityType]) -> SecurityType:
    """Map a vendor's type string through ``table``, defaulting to ``UNKNOWN``.

    ``UNKNOWN`` rather than ``None``: the field is optional on the record, and ``None`` there
    means *this source did not say*, which is a different claim from *this source said
    something we do not model*. ``fill_nulls`` acts on the first and not the second, so
    collapsing them would let an unmapped Tiingo string be silently overwritten by an
    unmapped OpenFIGI one and read as agreement.
    """
    if raw is None:
        return SecurityType.UNKNOWN
    return table.get(raw.strip().lower(), SecurityType.UNKNOWN)


def parse_kinds(names: Sequence[str]) -> set[SecurityType] | None:
    """Turn the wire spelling of the kind filter into the enum, or ``None`` for no filter.

    The equity counterpart of ``crocodile.capabilities.market._kinds``, and it raises on an
    unrecognised name for the same reason that one does: silently matching nothing would make
    a typo look like a market with no ETFs. Case-insensitive because the enum's values are
    upper-case tickers-of-a-kind (``CS``, ``ETF``) except ``unknown``, and asking a caller to
    remember which is which is asking them to get it wrong.

    Raises:
        ValueError: naming the bad kind and listing the valid ones.
    """
    if not names:
        return None
    by_value = {member.value.lower(): member for member in SecurityType}
    resolved: set[SecurityType] = set()
    for name in names:
        member = by_value.get(name.strip().lower())
        if member is None:
            raise ValueError(
                f"unknown instrument kind {name!r}; "
                f"valid kinds are {sorted(m.value for m in SecurityType)}"
            )
        resolved.add(member)
    return resolved


@dataclass(frozen=True, slots=True)
class Listing:
    """One resolved listing: the merged record, who attested it, and what it is priced in.

    See this module's docstring for why the last two are beside the record rather than on it.
    """

    instrument: Instrument
    """The merged reference record, carrying the ``reference_merge`` provenance tail."""

    sources: tuple[str, ...]
    """The reference sources that named this ticker, in :data:`REFERENCE_PRIORITY` order.

    The numerator of the row's own confidence, kept as the set rather than the count because
    ``census`` reports *which* registries agreed and a ratio cannot be un-divided.
    """

    currency: str | None
    """The currency the listing is priced in, or ``None`` if no source said.

    Tiingo's ``priceCurrency`` is the only source of this in the tree.
    """

    @property
    def symbol(self) -> str:
        """The ticker, which is the merge key and the canonical symbol for equities."""
        return self.instrument.symbol


@dataclass(frozen=True, slots=True)
class ReferenceEvidence:
    """What each source said, before anything was merged.

    Kept un-merged because the merge is not idempotent over its own output: re-running it
    with a fourth list would count the already-merged rows as one more attestation and report
    a two-source identity as a three-source one. Enrichment therefore adds a *source list*
    and re-merges from the evidence, which is what :meth:`with_figi` does.
    """

    as_of_ns: int
    """One instant for every row from every source.

    :meth:`CoverageResolver.resolve_records` groups by ``(symbol, local_ts)``, so three
    sources stamping their own clocks would produce three groups per ticker and merge
    nothing at all. Passing the instant in also keeps the whole pipeline deterministic under
    test, which is the argument ``market_census`` makes for ``generated_ns``.
    """

    by_source: Mapping[str, Sequence[Instrument]]
    """Each source's own rows, keyed by the name it partitions the lake under."""

    currency: Mapping[str, str]
    """Ticker to price currency, from whichever source published one."""

    def symbols(self) -> set[str]:
        """Every ticker any source named."""
        return {inst.symbol for rows in self.by_source.values() for inst in rows}

    def restricted(self, symbols: Iterable[str]) -> ReferenceEvidence:
        """Narrow every source's rows to ``symbols``, keeping the instant and the currencies."""
        keep = set(symbols)
        return replace(
            self,
            by_source={
                source: [inst for inst in rows if inst.symbol in keep]
                for source, rows in self.by_source.items()
            },
        )

    def with_figi(self, records: Mapping[str, Sequence[FigiRecord]]) -> ReferenceEvidence:
        """Add OpenFIGI's rows as a fourth list of evidence, then let the merge re-run."""
        merged = dict(self.by_source)
        merged[SOURCE_OPENFIGI] = instruments_from_figi(records, as_of_ns=self.as_of_ns)
        return replace(self, by_source=merged)

    def merged(self) -> list[Listing]:
        """Resolve every source's rows into one listing per ticker, sorted by ticker.

        The merge is :class:`~crocodile.core.coverage.resolver.CoverageResolver` under
        ``fill_nulls``, which is the strategy this data wants and ``priority`` is not: the
        three sources are complementary rather than competing, so taking the winner's row
        whole would throw away the CIK on every ticker Tiingo also lists — which is most of
        them.

        The tail is then restamped rather than inherited. ``fill_nulls`` fills fields that
        are ``None`` and every ``prov_*`` field has a non-null default, so the merged row
        would otherwise carry the highest-priority source's own claim — ``prov=NATIVE,
        prov_basis='native'``, the claim that a registrar published this row as it stands.
        It did not; this engine assembled it, and ``reference_merge`` is the basis that says
        so with a confidence that counts how many registrars agreed.

        A source outside :data:`REFERENCE_PRIORITY` is walked rather than skipped, after the
        ranked ones and in name order. Iterating the priority list alone would have made a
        fourth source's rows *vanish* — the silent-absence failure this whole merge exists to
        end — where walking it makes the fourth attestation reach ``reference_merge``, whose
        denominator is three, and raise. That is the loud form of "the formula's argument no
        longer holds", and it is the one somebody has to answer before the universe ships a
        confidence that means something different from what its docstring says.
        """
        ranked = [source for source in REFERENCE_PRIORITY if source in self.by_source]
        order = ranked + sorted(set(self.by_source) - set(REFERENCE_PRIORITY))
        resolver = CoverageResolver(REFERENCE_PRIORITY)
        rows = [inst for source in order for inst in self.by_source[source]]
        naming: dict[str, list[str]] = {}
        for source in order:
            for inst in self.by_source[source]:
                naming.setdefault(inst.symbol, []).append(source)

        listings: list[Listing] = []
        for record in resolver.resolve_records(rows, strategy="fill_nulls"):
            if not isinstance(record, Instrument):  # pragma: no cover - one record type in
                continue
            sources = tuple(dict.fromkeys(naming.get(record.symbol, ())))
            listings.append(
                Listing(
                    instrument=_restamped(record, n_sources=len(sources)),
                    sources=sources,
                    currency=self.currency.get(record.symbol),
                )
            )
        listings.sort(key=lambda listing: listing.symbol)
        return listings


def _restamped(record: Instrument, *, n_sources: int) -> Instrument:
    """Rebuild ``record`` with the merge's own provenance tail.

    Spelled as a construction rather than as ``msgspec.structs.replace`` so that the record
    this module emits is visible to the conformance scanner that reads canonical-record calls
    looking for a missing ``prov=``. A replace call is invisible to it, and a derivation that
    the gate cannot see is the shape ``core.resample.book`` shipped in.

    ``source`` is left as the resolver left it — the highest-priority registry that named the
    ticker. That is a true statement about which row the merge started from, and it is not a
    claim that the row came from there whole: ``prov_basis`` says otherwise on the same
    record, and it is the field a consumer filters on.
    """
    tail = provenance_fields(REFERENCE_MERGE_BASIS, {"n_sources": n_sources})
    return Instrument(
        source=record.source,
        symbol=record.symbol,
        symbol_raw=record.symbol_raw,
        source_ts=record.source_ts,
        local_ts=record.local_ts,
        asset_class=AssetClass.EQUITY,
        prov=tail.prov,
        prov_basis=tail.prov_basis,
        prov_confidence=tail.prov_confidence,
        prov_inputs=tail.prov_inputs,
        name=record.name,
        cik=record.cik,
        figi=record.figi,
        composite_figi=record.composite_figi,
        share_class_figi=record.share_class_figi,
        cusip=record.cusip,
        exchange=record.exchange,
        security_type=record.security_type,
        sic=record.sic,
        shares_outstanding=record.shares_outstanding,
        listing_date=record.listing_date,
        status=record.status,
    )


def _read_from_a_registry() -> ProvenanceFields:
    """The tail a pre-merge row carries: ``native``, and this is the one place it is honest.

    A row built by one of the three functions below is one registry's own statement about one
    ticker — SEC's index really does say that AAPL files under CIK 320193 — so it was read
    rather than reconstructed, which is exactly what ``native``'s formula scores 1.0 for.
    The derivation is the *merge*, and :meth:`ReferenceEvidence.merged` is where the tail
    stops saying this.

    Built fresh per call rather than held as a module constant so no two records share one
    ``prov_inputs`` list; ``native`` declares no inputs, so the list is empty today and the
    sharing hazard is the one ``_Header`` documents rather than a live bug.
    """
    return provenance_fields("native")


def instruments_from_sec(rows: Sequence[SecCompanyTicker], *, as_of_ns: int) -> list[Instrument]:
    """Build one record per SEC registrant row: a CIK, a ticker and a legal name.

    No exchange, and none is invented. A filer is not a listing — SEC's index says which
    company reports under which ticker, not where the ticker trades — so ``exchange`` stays
    ``None`` and Tiingo fills it during the merge.

    ``source_ts`` is ``None`` because ``company_tickers.json`` carries no publication
    timestamp. Saying so explicitly is what a required field with no default is for; the same
    is true of the other two sources and for the same reason.
    """
    built: list[Instrument] = []
    for row in rows:
        tail = _read_from_a_registry()
        built.append(
            Instrument(
                source=SOURCE_SEC,
                symbol=row.ticker,
                symbol_raw=row.ticker,
                source_ts=None,
                local_ts=as_of_ns,
                asset_class=AssetClass.EQUITY,
                prov=tail.prov,
                prov_basis=tail.prov_basis,
                prov_confidence=tail.prov_confidence,
                prov_inputs=tail.prov_inputs,
                name=row.title,
                cik=f"{row.cik:010d}",
            )
        )
    return built


def instruments_from_tiingo(rows: Sequence[TiingoTicker], *, as_of_ns: int) -> list[Instrument]:
    """Build one record per Tiingo listing row: a ticker, its exchange and its type.

    Rows with no exchange are dropped. Tiingo's archive carries entries whose ``exchange``
    column is empty, and the whole reason this source is first in the priority order is that
    it names the venue; a row that does not is a row with nothing this source is for.

    ``listing_date`` takes Tiingo's ``startDate``, which is the first date Tiingo has data
    for rather than the date of the listing. That is close enough to be worth carrying and
    far enough to be worth saying: it is a data-coverage bound, and a caller reading it as an
    IPO date will be wrong for anything that listed before Tiingo's history begins.

    ``status`` stays ``None`` even though ``endDate`` is right there and tempting. Turning a
    coverage end into "delisted" needs a cutoff — how stale is stale — and that cutoff would
    be a constant invented here to make a judgement look measured. The universe therefore
    enumerates what the registries name, delisted rows included, and the ``top`` slice sheds
    them on evidence instead: a delisted ticker has no recent traded volume, so it sorts
    where it belongs rather than where a guessed cutoff put it.

    The universe is US-scoped (spec 3.6). A row whose exchange label is not a key of
    :data:`TIINGO_US_EXCHANGES` is skipped and counted per label, and one INFO line names the
    counts. Non-US listings belong to the listing master, not to this universe.
    """
    built: list[Instrument] = []
    skipped: Counter[str] = Counter()
    for row in rows:
        ticker = row.ticker.strip().upper()
        exchange = row.exchange.strip().upper()
        if not ticker or not exchange:
            continue
        if exchange not in TIINGO_US_EXCHANGES:
            skipped[exchange] += 1
            continue
        tail = _read_from_a_registry()
        built.append(
            Instrument(
                source=SOURCE_TIINGO,
                symbol=ticker,
                symbol_raw=ticker,
                source_ts=None,
                local_ts=as_of_ns,
                asset_class=AssetClass.EQUITY,
                prov=tail.prov,
                prov_basis=tail.prov_basis,
                prov_confidence=tail.prov_confidence,
                prov_inputs=tail.prov_inputs,
                exchange=exchange,
                security_type=_security_type(row.asset_type, _TIINGO_ASSET_TYPES),
                listing_date=row.start_date or None,
            )
        )
    if skipped:
        log.info(
            "tiingo universe: skipped %d row(s) on non-US exchange labels (%s); "
            "SecurityMaster and this universe are US-scoped",
            sum(skipped.values()),
            ", ".join(f"{label}={count}" for label, count in sorted(skipped.items())),
        )
    return built


def instruments_from_figi(
    records: Mapping[str, Sequence[FigiRecord]], *, as_of_ns: int
) -> list[Instrument]:
    """Build one record per ticker OpenFIGI matched, from the first match it returned.

    OpenFIGI answers a ticker mapping with every FIGI that matches it, which for a US equity
    is the composite plus one per venue it trades on. The first is taken and the rest are
    dropped rather than emitted as extra rows: this universe is keyed on ticker, so N rows
    for one ticker would collapse in the merge anyway, and which of them survived would be
    decided by grouping order rather than by anything anyone chose. The composite and
    share-class FIGIs that *are* worth having come off the same record as their own fields.
    """
    built: list[Instrument] = []
    for ticker, matches in records.items():
        if not matches:
            continue
        match = matches[0]
        symbol = ticker.strip().upper()
        tail = _read_from_a_registry()
        built.append(
            Instrument(
                source=SOURCE_OPENFIGI,
                symbol=symbol,
                symbol_raw=symbol,
                source_ts=None,
                local_ts=as_of_ns,
                asset_class=AssetClass.EQUITY,
                prov=tail.prov,
                prov_basis=tail.prov_basis,
                prov_confidence=tail.prov_confidence,
                prov_inputs=tail.prov_inputs,
                name=match.name,
                figi=match.figi,
                composite_figi=match.composite_figi,
                share_class_figi=match.share_class_figi,
                exchange=match.exch_code,
                security_type=_security_type(match.security_type, _FIGI_SECURITY_TYPES),
            )
        )
    return built


def filter_listings(
    listings: Sequence[Listing],
    *,
    exchange: str | None = None,
    kinds: set[SecurityType] | None = None,
    currency: str | None = None,
) -> list[Listing]:
    """Narrow a resolved universe. Every filter is case-insensitive and ``None`` means "any".

    ``exchange`` matches the resolved venue exactly rather than as a substring, which is the
    opposite of ``markets``' ``search`` and deliberately so: ``search`` is a lookup aid over
    a list a human is reading, while this decides which market a request is *about*, and a
    substring there would quietly fold ``NASDAQ`` into anything containing it.
    """
    wanted_exchange = exchange.strip().upper() if exchange else None
    wanted_currency = currency.strip().upper() if currency else None
    kept: list[Listing] = []
    for listing in listings:
        instrument = listing.instrument
        if wanted_exchange is not None and (instrument.exchange or "").upper() != wanted_exchange:
            continue
        if kinds is not None and instrument.security_type not in kinds:
            continue
        if wanted_currency is not None and (listing.currency or "").upper() != wanted_currency:
            continue
        kept.append(listing)
    return kept


def rank_listings(listings: Sequence[Listing], volumes: Mapping[str, float]) -> list[Listing]:
    """Order a universe by traded volume, most traded first, ties broken by ticker.

    A ticker with no entry in ``volumes`` scores zero and sorts after every ticker that has
    one — it is not dropped. The distinction matters: absent evidence is not evidence of
    absence, and a symbol this lake has never collected is a symbol nothing is known about
    rather than a symbol nobody trades. The caller decides whether an unranked tail is
    acceptable; ``universe --top`` decides it is not and refuses when there is no evidence at
    all, while the ``all`` slice takes the order as a better-than-alphabetical default.
    """
    return sorted(listings, key=lambda listing: (-volumes.get(listing.symbol, 0.0), listing.symbol))


def volume_by_symbol(
    query: Callable[[str], pl.DataFrame], *, channels: Sequence[str]
) -> dict[str, float]:
    """Read the volume evidence out of the lake through the caller's own query policy.

    ``query`` is :meth:`CapabilityContext.query
    <crocodile.core.capability.CapabilityContext.query>` and not a ``Catalog``. Taking the
    bound method rather than the catalog is what keeps this function inside the surface's
    readonly and row-limit policy without knowing anything about surfaces — calling
    ``catalog.query`` here would silently ignore both, which is the failure that method's
    docstring was written about. Taking a callable rather than the context is what keeps this
    module out of the capability layer: a reference resolver that imported
    ``CapabilityContext`` would be a domain module depending on the registry that calls it.

    ``channels`` is what the lake actually holds, from ``Catalog.list_channels`` — a
    filesystem walk over the hive layout, not a lake read, so consulting it routes around no
    policy. It is asked first because a ``SELECT`` against a channel this lake has never held
    raises out of DuckDB complaining about a missing view, and "this deployment has not
    collected any bars" is a state rather than an error. An empty answer is the same state
    reached the other way and is returned the same way.
    """
    if "ohlcv" not in channels:
        return {}
    frame = query(VOLUME_RANK_SQL)
    if frame.height == 0:
        return {}
    return {
        str(row["symbol"]): float(row["mean_volume"] or 0.0)
        for row in frame.iter_rows(named=True)
        if row["symbol"] is not None
    }


def require_sec_user_agent(settings: Settings) -> str:
    """Return the contact string SEC requires, or refuse to invent one.

    This is the contract :attr:`Settings.sec_user_agent <crocodile.core.config.Settings>`
    hands to "whichever task wires the EDGAR client", and the equity universe is that task.
    The name is kept because two adapters call it and because the *timing* is what this
    module contributes: it is called here rather than inside :func:`fetch_bulk_evidence` so
    an adapter can refuse before it builds anything, which is what lets ``collect-market``
    fail on configuration before a subscription exists rather than on first await.

    What it no longer contributes is the check. It used to hold a second, weaker copy —
    ``if not settings.sec_user_agent``, with no ``.strip()`` — and since nothing routed
    through :meth:`SecEdgarClient.from_settings`, the weaker copy was the one every caller
    reached. The two disagreed on whitespace, so a blank-but-present value passed here and
    failed sixty seconds later inside ``begin()`` with three spaces in the header. One
    contract has one gate; :func:`~crocodile.equity.providers.sec_edgar.client.require_user_agent`
    is it, and it lives in the module that writes the header because the import only runs
    one way — this module reaches for ``SecEdgarClient``, not the reverse.

    Raises:
        ConfigError: naming the variable and what belongs in it.
    """
    return require_user_agent(settings)


async def fetch_bulk_evidence(
    *,
    as_of_ns: int,
    sec_user_agent: str | None = None,
    sec: SecEdgarClient | None = None,
    tiingo: TiingoClient | None = None,
) -> ReferenceEvidence:
    """Fetch the two whole-universe sources and return their rows, un-merged.

    Both clients are injectable and neither is constructed when one is supplied, which is what
    makes this testable without a network: the tests hand in objects whose two methods return
    fixture rows. A client this function built is also closed by it, and one the caller
    supplied is not — the caller owns what it opened, which is why ``sec_user_agent`` and
    ``sec`` are two parameters rather than one. An adapter that validated configuration up
    front and then handed the built client across an ``await`` boundary would own an
    ``aiohttp`` session opened inside somebody else's event loop and closed by nobody.

    OpenFIGI is deliberately absent. It is per-symbol and rate-limited to twenty-five requests
    a minute without a key, so running it here would make every call to ``universe`` a
    multi-hour job; :meth:`ReferenceEvidence.with_figi` applies it to the slice that is
    actually going to be returned.
    """
    own_sec = sec is None
    own_tiingo = tiingo is None
    sec_client = (
        sec
        if sec is not None
        else (
            SecEdgarClient(user_agent=sec_user_agent)
            if sec_user_agent
            else SecEdgarClient()
        )
    )
    tiingo_client = tiingo if tiingo is not None else TiingoClient()
    try:
        sec_rows = await sec_client.fetch_company_tickers()
        tiingo_rows = await tiingo_client.download_supported_tickers()
    finally:
        if own_sec:
            await sec_client.close()
        if own_tiingo:
            await tiingo_client.close()

    return ReferenceEvidence(
        as_of_ns=as_of_ns,
        by_source={
            SOURCE_SEC: instruments_from_sec(sec_rows, as_of_ns=as_of_ns),
            SOURCE_TIINGO: instruments_from_tiingo(tiingo_rows, as_of_ns=as_of_ns),
        },
        currency={
            row.ticker.upper(): row.price_currency.strip().upper()
            for row in tiingo_rows
            if row.price_currency.strip()
            and row.exchange.strip().upper() in TIINGO_US_EXCHANGES
        },
    )


async def fetch_figi(
    symbols: Sequence[str],
    *,
    api_key: str | None = None,
    client: OpenFigiClient | None = None,
) -> dict[str, list[FigiRecord]]:
    """Map ``symbols`` to FIGI records, batched and rate-limited by the client itself.

    Returns a mapping rather than a list so a ticker OpenFIGI had nothing for is an absent
    key rather than an empty row that the merge would count as an attestation.

    ``api_key`` is threaded through rather than read here: the client picks a batch size of a
    hundred and a rate of twenty-five requests per six seconds when it has one, and ten and
    twenty-five per minute when it does not. Which of the two a deployment gets is a fact
    about its configuration, and configuration is read in exactly one place —
    :mod:`crocodile.core.config` — which is why this parameter exists instead of an
    ``os.environ`` lookup.
    """
    if not symbols:
        return {}
    figi = client if client is not None else OpenFigiClient(api_key=api_key)
    jobs = [OpenFigiJob(id_type="TICKER", id_value=symbol) for symbol in symbols]
    try:
        results = await figi.map_jobs(jobs)
    finally:
        if client is None:
            await figi.close()
    return {
        symbol: list(matches)
        for symbol, matches in zip(symbols, results, strict=True)
        if matches
    }
