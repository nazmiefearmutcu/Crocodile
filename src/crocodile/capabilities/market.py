"""Capabilities describing market structure and reference data.

Owns ``list-exchanges``, ``markets``, ``universe``, ``census``, ``open-interest``, ``depth``
and ``calendar`` — what exists to trade, where, when, and in what size, as opposed to what the
lake happens to hold about it.

Three of the seven reach a venue rather than the lake (``universe``, ``census`` and ``depth``;
``markets`` and ``calendar`` read the build), which makes this the batch where the
two questions below had to be answered once, out loud, instead of per declaration.

**Why every adapter here is synchronous, including the four whose work is not.**
``exchange_instruments``, ``top_symbols_by_volume``, ``market_census`` and
``DepthSource.snapshot`` are all ``async def``, while
:data:`~crocodile.core.capability.CapabilityFn` is one calling convention for three
surfaces — and the CLI projection is synchronous where the REST and MCP ones run on an
event loop. An ``async`` ``Impl.fn``
would therefore force every surface to branch on ``inspect.iscoroutinefunction`` — the
signature introspection that convention exists to refuse — and a surface that forgot would
not crash: it would hand the caller a coroutine object that never runs, which is a wrong
answer wearing a passing call. A capability that works on MCP and hangs on the CLI is worse
than one that is not declared, so the coroutine is driven to completion inside the adapter,
by :func:`~crocodile.core.capability.run_to_completion`, and what every surface gets back is
a value. That helper started here and moved to the machinery when a second batch wrote its
own bare ``asyncio.run`` and broke two surfaces out of three with it.

**Why none of them needs a live client on the context.**
:class:`~crocodile.core.capability.CapabilityContext` carries a ``Catalog`` and a
``Settings`` and no client, and nothing here wants one: ``universe`` and ``census``
construct their own ``ccxt``/``aiohttp`` sessions inside the coroutine and close them
again, and ``depth``'s equity half asks
:func:`~crocodile.equity.depth.select.select_depth_source` for a source rather than being
handed one. ``open-interest`` reads the lake through ``ctx.catalog``, exactly as
``slippage`` does, and ``depth``'s crypto half reads it through ``ctx.query``, which is
where a surface's ``readonly`` and ``row_limit`` are applied. The one thing that *is* read
from outside the context is the pair of Alpaca keys, and that read happens inside
``select_depth_source``; see :func:`depth`.

The equity halves of this family are where :data:`SPEC_METHODS
<crocodile.core.capability.SPEC_METHODS>` M2 and M3 landed, and the block at the foot of this
module is the record of what left and why. Every entry is gone: ``open-interest`` off M2,
``markets``, ``universe`` and ``census`` off M3 — all three answering out of
:mod:`crocodile.equity.reference.universe` — and ``depth`` off M8, which is the one the
ledger could not express because its missing half was the crypto one. ``list-exchanges``
never needed an entry; both markets could always answer it.

**Why the crypto halves refuse ``mic`` and ``country``.** Codemap 5.2 rule 3: a parameter a
capability does not understand must be refused or filtered, never ignored. A crypto venue has
neither an ISO 10383 MIC nor an ISO 3166 country, so ignoring either filter would return every
crypto venue for a request that asked for one market, a plausible wrong answer. The crypto
halves therefore raise :class:`~crocodile.core.errors.CapabilityUnavailable` naming each
dimension set, before any network or registry work. ``calendar_crypto`` refuses ``mic`` the
same way. Its answer for crypto is not a blank for the filter: every crypto venue trades every
calendar day, so the crypto calendar is the always-open one, and a ``mic`` it cannot honour is
refused rather than ignored.

A fifth question this batch had to answer, once M3 gave it two adapters per capability rather
than one: **an asymmetric capability resolves its own asset class and a symmetric one does
not.** ``crocodile.surfaces.dispatch.resolve_asset_class`` falls through to "there is only one
implementation, so there is nothing to decide" — and four capabilities here just stopped
having one. None of ``MarketsParams``, ``UniverseParams``, ``CensusParams``,
``OpenInterestParams`` or ``CalendarParams`` carries a ``symbol`` field, so nothing about a
request for them is evidence about a market, and callers name ``--asset-class`` explicitly
from here on (``calendar`` included: a bare ``mic`` field is not market evidence). That is
the documented step 3 of that function's order working as designed rather than a regression:
the alternative is defaulting to crypto, which would answer an equity request with a crypto
venue list and no error.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Final

import msgspec
import polars as pl

from crocodile.core.capability import (
    PENDING_SYMMETRY,
    AssetClass,
    Capability,
    CapabilityContext,
    Fallback,
    Impl,
    ReturnKind,
    declare,
    run_to_completion,
)
from crocodile.core.config import Settings
from crocodile.core.errors import CapabilityUnavailable
from crocodile.core.markets.calendar import (
    AlwaysOpenCalendar,
    CalendarQuality,
    ExchangeCalendar,
    calendar_for,
)
from crocodile.core.markets.errors import UnknownMarketError
from crocodile.core.markets.exchanges import ExchangeInfo, ExchangeRegistry
from crocodile.core.markets.grammar import GRAMMAR_SOURCES, load_grammar
from crocodile.core.markets.master import ListingMaster, ListingStatus, default_listings_path
from crocodile.core.schema.enums import SecurityType
from crocodile.core.schema.provenance import Provenance
from crocodile.core.schema.records import DepthProfile
from crocodile.core.util.time import now_ns
from crocodile.crypto import census as census_mod
from crocodile.crypto.analytics.oi_aggregator import aggregate_open_interest
from crocodile.crypto.depth import depth_from_book_snapshots
from crocodile.crypto.exchanges import factory
from crocodile.crypto.instruments.registry import Kind
from crocodile.crypto.instruments.universe import (
    exchange_instruments,
    filter_instruments,
    top_symbols_by_volume,
)
from crocodile.equity.analytics.oi_aggregator import aggregate_option_open_interest
from crocodile.equity.depth import alpaca_is_keyed, select_depth_source
from crocodile.equity.providers import factory as provider_factory
from crocodile.equity.reference import universe as reference
from crocodile.equity.universal.order import (
    SourceOrder,
    source_order,
    usage_class_of,
)
from crocodile.equity.universal.state import MarketCoverage, read_market_coverage

__all__ = [
    "CALENDAR",
    "CENSUS",
    "DEPTH",
    "LIST_EXCHANGES",
    "MARKETS",
    "OPEN_INTEREST",
    "UNIVERSE",
    "CalendarParams",
    "CensusParams",
    "DepthParams",
    "ListExchangesParams",
    "MarketsParams",
    "OpenInterestParams",
    "UniverseParams",
]

_END_OF_TIME: Final[int] = 2**63 - 1
"""The open right-hand end of a nanosecond range, as the largest ``local_ts`` there is.

The crypto CLI spelled this ``9999999999999999999``, which is larger than a signed 64-bit
integer and only ever worked because DuckDB widened the literal. ``local_ts`` is an
``int64`` on every record, so the largest representable timestamp is the honest spelling of
"no upper bound" and cannot be a value the comparison silently promotes.
"""


class ListExchangesParams(msgspec.Struct, frozen=True):
    """No parameters: the answer is the whole registry or nothing.

    All three legacy surfaces took none — ``crypcodile list-exchanges``,
    ``GET /api/v1/exchanges`` and MCP ``list_registered_exchanges`` are each a bare read of
    :func:`crocodile.crypto.exchanges.factory.list_exchanges`. Filtering lives on
    ``markets``, which is the capability that has a venue universe worth filtering.
    """


class MarketsParams(msgspec.Struct, frozen=True):
    """Parameters for ``markets``. One struct, and the equity half M3 landed inherits it."""

    search: str | None = None
    """Case-insensitive substring filter on the venue id. ``None`` means every venue."""

    native_only: bool = False
    """Keep only venues served by a hand-written connector."""

    ccxt_only: bool = False
    """Keep only venues served by the universal ccxt connector.

    Set together with ``native_only`` this yields the *overlap* — venues that are both,
    where a request routes to the native connector. The CLI's two independent ``if`` blocks
    printed nothing at all for that combination, which is an artefact of rendering two
    sections rather than an answer; two filters over one row set compose.
    """

    mic: str | None = None
    """ISO 10383 MIC of one exchange, operating or segment (a segment answers for its operating
    MIC). Equity only: the crypto half refuses it rather than ignore it."""

    country: str | None = None
    """ISO 3166-1 alpha-2 country code. Equity only, refused by the crypto half."""


class UniverseParams(msgspec.Struct, frozen=True):
    """Parameters for ``universe``. One struct, and the equity half M3 landed inherits it.

    ``--symbols-only`` is absent: printing bare symbols instead of a table is how a CLI
    renders this answer, not a different answer.
    """

    source: str | None = None
    """Venue id, native or ccxt. Required by the crypto half.

    On the equity half it names a reference-merge exchange label (the US reference universe)
    and is mutually exclusive with ``mic`` and ``country``, which select the listing master
    instead. ``source``, which is the lake's merged partition key and what the ops batch
    already called it. This struct said ``exchange`` — the crypto fork's word, which has no
    meaning on the equity half M3 landed, where a `source` is an exchange rather than a venue
    and a `quote` is the currency a listing is priced in.

    Ruling: ``UniverseParams.source`` becomes optional, so ``universe --asset-class equity
    --mic XIST`` works as spec 3.11 writes it. Cost if wrong: the CLI stops synthesising the
    positional for ``universe`` (``cli.positional_field`` only does so for exactly one
    required field, ``cli.py:128-146``), so ``crocodile universe binance`` must be spelled
    ``--source binance``; bringing it back needs a CLI rule, not a struct change."""

    top: int | None = None
    """Rank by traded volume and return the top N, instead of enumerating.

    The two halves rank on different evidence because the two markets publish different
    things. A crypto venue serves its own 24 h quote volume from ``fetch_tickers``; no equity
    exchange serves a free whole-market volume board at all, so the equity half ranks on the
    bars this lake has already stored — see
    :data:`~crocodile.equity.reference.universe.VOLUME_RANK_SQL`, which names that source and
    argues the statistic. Same question, same column, and each market answered with what it
    actually has rather than one of them answered with a guess.
    """

    quote: str | None = None
    """Quote-currency filter, e.g. ``USDT``. ``None`` means every quote.

    ``None`` rather than the ``"USDT"`` that
    :func:`~crocodile.crypto.instruments.universe.top_symbols_by_volume` defaults to, because
    that default is invisible to a caller who never named a quote: the CLI passed its own
    ``None`` straight through, and an unasked-for filter that silently empties a USD-quoted
    venue is the worse failure.
    """

    kinds: tuple[str, ...] = ()
    """Instrument kinds to keep. Empty means every kind.

    ``spot``/``perpetual``/``future``/``option`` for crypto and the
    :class:`~crocodile.core.schema.enums.SecurityType` names — ``CS``, ``ETF``, ``ADR`` and
    the rest — for equities. One field asking each market its own question, which is what
    ``source`` already does: a shared parameter schema is a shared set of *questions*, and a
    perpetual swap is not a thing an exchange lists. An unrecognised name raises on both
    sides, rather than quietly matching nothing.
    """

    limit: int = 50
    """Cap on enumerated rows. Ignored when ``top`` is set, which caps itself."""

    mic: str | None = None
    """ISO 10383 MIC of one exchange, operating or segment. Equity only: selects the listing
    master (``universe-sync`` builds it). Refused by the crypto half."""

    country: str | None = None
    """ISO 3166-1 alpha-2 country code. Equity only, selects the listing master. Refused by the
    crypto half."""


class CalendarParams(msgspec.Struct, frozen=True):
    """Parameters for ``calendar``. One struct, and the equity half inherits it.

    ``start`` and ``end`` are strings on purpose: a ``datetime.date`` field is not a scalar to
    the surface projection, which would make ``calendar`` POST-only on REST and unreachable by
    GET. The adapters parse them.
    """

    start: str
    """ISO date (YYYY-MM-DD), inclusive, in the exchange's own calendar."""

    end: str
    """ISO date (YYYY-MM-DD), inclusive, in the exchange's own calendar."""

    mic: str | None = None
    """Operating or segment MIC; required for equities. Refused by the crypto half, where every
    venue trades around the clock."""


class CensusParams(msgspec.Struct, frozen=True):
    """Parameters for ``census``. One struct, and the equity half M3 landed inherits it.

    The CLI's ``--output`` and ``--json`` are absent on purpose: they name files to write,
    which is a surface's business, and ``render_terminal`` / ``render_html`` are pure
    functions of the returned snapshot that a projection calls on the way to a screen.
    Neither reads the market, so neither belongs in the schema three surfaces publish.
    """

    venues: tuple[str, ...] = ()
    """Venues to deep-enumerate. Empty means the module's curated majors."""

    coin_pages: int = 1
    """Pages of 250 coins to sample for the movers board."""


class OpenInterestParams(msgspec.Struct, frozen=True):
    """Parameters for ``open-interest``. One struct, which the equity half inherits at M2.

    ``symbols`` is where three surfaces disagreed on paper and one implementation decided
    the matter. The CLI took ``--symbol``, singular, documented as a substring filter; REST
    took ``symbols``, plural; the MCP handler annotated ``str | list[str]`` while its
    published ``inputSchema`` said ``{"type": "string"}``. Read
    :func:`~crocodile.crypto.analytics.oi_aggregator.aggregate_open_interest` and the
    disagreement collapses: it has only ever had **one** semantic, and it is neither of the
    two the names suggest. Every element is a case-insensitive *literal substring* pattern
    and the elements are OR-ed, so a list of symbols was never a list of symbol identities —
    it was a list of patterns, and a lone string was the one-element case.

    Both plural spellings were therefore broken in opposite directions. REST handed its
    whole query string over as a single pattern, so ``?symbols=BTC,ETH`` matched no symbol
    at all and returned an empty board that reads exactly like an empty lake. MCP's schema
    made the list arm unreachable from the wire, so the only callers who could use it were
    in-process Python ones. A tuple of patterns is what the implementation always was: the
    CLI's ``--symbol`` becomes the one-element case, REST's comma string becomes a real
    split, and MCP's array becomes something a client can actually send.
    """

    symbols: tuple[str, ...] = ()
    """Case-insensitive literal substring patterns, OR-ed. Empty means every symbol."""

    start_ns: int = 0
    """Inclusive lower bound on ``local_ts``."""

    end_ns: int = _END_OF_TIME
    """Inclusive upper bound on ``local_ts``.

    Together these default to the whole range, which is the CLI's behaviour, and not to
    REST's ``start=0, end=0`` — that pair returns nothing for every lake, so a caller who
    omitted the range was told the market has no open interest rather than being asked for
    one.
    """


_DEPTH_WINDOW_NS: Final[int] = 60 * 1_000_000_000
"""How stale a stored book may be, by default, and still be reported as describing an instant.

A minute, and it is a default rather than a constant of the method — the caller overrides it,
because only the caller knows whether a minute-old ladder answers their question. It is the
denominator of ``book_snapshot_slice``'s freshness term, argued at that registration; the
value here is chosen to be wide enough that a lake collecting a liquid venue's book answers a
"depth now" request, and narrow enough that a quiet symbol's hour-old book is refused rather
than served as current.
"""


class DepthParams(msgspec.Struct, frozen=True):
    """Parameters for ``depth``. One struct, and both halves now read from it.

    Four of the six fields are consumed by one half and ignored by the other, which is what
    a shared struct across two markets costs and is not a defect: ``bins`` and ``method``
    were already in that position — the Alpaca L1 branch has one level per side and nothing
    to bin — and the two added for the crypto half are the same shape. The alternative is a
    struct per asset class, which is the thing the registry refuses, because identical
    parameter schemas are half of what "full API symmetry" means and two structs are two
    places for one question to drift apart.
    """

    symbol: str
    """The ticker to snapshot."""

    method: str = "uniform"
    """Volume-at-price binning method: ``uniform``/``typical``/``close``. Consumed only by
    the synthetic source; the L1 source has one level per side and nothing to bin."""

    bins: int = 40
    """Price buckets the synthetic ladder is built from."""

    top_n: int = 10
    """Levels returned per side. The one field both halves read: it shapes the ladder the
    equity source builds and the ladder the crypto slice cuts."""

    as_of_ns: int | None = None
    """The instant the crypto ladder should describe; ``None`` means the moment of the call.

    The crypto half's, because it is the half with a history to read. The equity half has no
    stored ladder to slice — it fetches a live quote or bins today's bars — so it answers at
    the instant it is called whatever this says, and that asymmetry is a property of the two
    markets' data rather than of this schema. It becomes meaningful for equities the day an
    equity provider streams a book into the lake, which is the same day
    ``crocodile.crypto.depth.depth_from_book_snapshots`` serves both.
    """

    max_age_ns: int = _DEPTH_WINDOW_NS
    """How old a stored book may be and still answer for ``as_of_ns``. Crypto's, likewise.

    Both a bound and a measurement: a snapshot older than this is not returned at all, and
    how much of the window the returned one used up is the freshness half of the confidence
    on the record. See ``book_snapshot_slice`` in
    :mod:`crocodile.core.schema.provenance`, including why a caller-declared window is the
    least bad denominator available here.
    """


def list_exchanges(ctx: CapabilityContext, params: ListExchangesParams) -> list[str]:
    """Return the crypto connectors this build registers. Never touches the lake.

    Distinct from ``catalog-exchanges``, which walks the ``source=`` partitions actually on
    disk. This one answers "what could I collect from", and the answer is a property of the
    build rather than of any market.
    """
    return factory.list_exchanges()


def list_providers(ctx: CapabilityContext, params: ListExchangesParams) -> list[str]:
    """Return the equity providers this build registers — the same question, other market.

    This is what makes ``list-exchanges`` symmetric today rather than scheduled. The two
    registries answer one question, "which sources can this build pull from", and the merge
    already ruled that a crypto venue and an equity vendor are the same kind of thing by
    collapsing ``exchange=`` and ``provider=`` into one ``source=`` partition key. The
    objection that a data vendor is not an exchange does not survive the crypto list either:
    it has carried ``coingecko``, an aggregator, since before the merge.
    """
    return provider_factory.list_providers()


_MARKETS_SCHEMA: Final[Mapping[str, pl.DataType]] = {
    "venue": pl.String(),  # crypto: venue id; equity: operating MIC (same as mic)
    "native": pl.Boolean(),  # equity: _EQUITY_VENUE_IS_NATIVE
    "ccxt": pl.Boolean(),  # equity: _EQUITY_VENUE_IS_CCXT
    "mic": pl.String(),  # equity only (crypto rows: null)
    "name": pl.String(),  # ExchangeInfo.name
    "country": pl.String(),  # ISO 3166-1 alpha-2
    "timezone": pl.String(),  # ExchangeInfo.tz.key (IANA)
    "currency": pl.String(),  # ISO 4217 trading currency
    "quote_scale": pl.Int64(),  # 0 major unit, 2 minor unit (pence, cents, agorot)
    "calendar_quality": pl.String(),  # CalendarQuality member .value
    "sources": pl.List(pl.String()),  # equity: serving sources, in order-table rank
    "usage_classes": pl.List(pl.String()),  # equity: usage_class_of(sources[i]); None allowed
    "tier_research": pl.String(),  # "A".."D"
    "sanctioned": pl.Boolean(),  # ExchangeInfo.sanctioned (data fact, not the opt-in)
    "listings_known": pl.Int64(),  # equity: non-delisted listing rows (0 when none)
    "last_success": pl.Int64(),  # equity: latest universal success, epoch ns; null if none
}
"""One schema for both halves of ``markets``, with nulls where a half does not observe a column.

The sixteen columns are task 08's thirteen with ``usage_classes`` after ``sources`` and
``listings_known`` and ``last_success`` after ``sanctioned`` (spec 3.11, ruling 9). The three
local-state columns are counts and stamps read from this lake, not market observations, so no
lake channel is named in their basis.

Stated so an empty result is still a table with the columns a caller selects on. A null says
"this half does not observe it", not "this venue lacks it": the same argument
``_UNIVERSE_SCHEMA``'s docstring makes. Keeping one schema keeps the two halves projectable by
one renderer, and a later column cannot be left off one half by accident because crypto rows
start from ``dict.fromkeys(_MARKETS_SCHEMA)``.
"""


def _refuse_market_dimensions(
    capability: str, params: MarketsParams | UniverseParams | CalendarParams
) -> None:
    """Refuse ``mic`` and ``country`` on a crypto answer, before any network or registry work.

    A crypto venue has neither an ISO 10383 MIC nor an ISO 3166 country, so a crypto answer
    to a request that names one could only ignore the filter and return every venue, which is
    a plausible wrong answer (codemap 5.2 rule 3). The refusal names every dimension that was
    set.

    Raises:
        CapabilityUnavailable: naming each dimension set. REST maps it to 501.
    """
    named = [name for name in ("mic", "country") if getattr(params, name, None) is not None]
    if named:
        raise CapabilityUnavailable(
            capability,
            AssetClass.CRYPTO.value,
            reason=(
                f"{' and '.join(named)} slice equity exchanges (ISO 10383 MIC, ISO 3166 "
                "country); no crypto venue has either, so a crypto answer could only "
                "ignore the filter. Ask with --asset-class equity"
            ),
        )


def markets(ctx: CapabilityContext, params: MarketsParams) -> pl.DataFrame:
    """Every venue this build can reach, tagged by which connector serves it.

    One row per venue with two independent flags rather than the CLI's two printed
    sections. A venue in both is not two rows: a request for that name routes to the
    hand-written connector, which is what ``native`` being true means, and what the CLI
    rendered as ``(native override)``.

    ``list_all_exchanges()`` is the row source rather than the union of the other two calls,
    so the "total reachable" number the CLI printed and the rows it printed can no longer
    come from two different unions.

    Only the three tier columns are observed here; every exchange column is null (see
    ``_MARKETS_SCHEMA``). ``mic`` and ``country`` are refused, not ignored.
    """
    _refuse_market_dimensions("markets", params)
    native = set(factory.list_exchanges())
    ccxt = set(factory.list_ccxt_exchanges())
    needle = params.search.lower() if params.search is not None else None

    rows: list[dict[str, Any]] = []
    for venue in factory.list_all_exchanges():
        is_native, is_ccxt = venue in native, venue in ccxt
        if params.native_only and not is_native:
            continue
        if params.ccxt_only and not is_ccxt:
            continue
        if needle is not None and needle not in venue.lower():
            continue
        row: dict[str, Any] = dict.fromkeys(_MARKETS_SCHEMA)
        row.update(venue=venue, native=is_native, ccxt=is_ccxt)
        rows.append(row)
    return pl.DataFrame(rows, schema=_MARKETS_SCHEMA)


_UNIVERSE_SCHEMA: Final[Mapping[str, pl.DataType]] = {
    "symbol": pl.String(),
    "kind": pl.String(),
    "base": pl.String(),
    "quote": pl.String(),
    "rank": pl.Int64(),
}
"""One schema across both of ``universe``'s branches, with nulls where a branch cannot see.

The ranked branch reads ``fetch_tickers``, which returns symbols and volumes and no
instrument metadata; the enumerating branch reads ``load_markets``, which returns metadata
and no ranking. Emitting two different column sets under one capability would make the
result unrenderable by a projection that does not know which flag was passed, so both
branches fill the same five columns and a null says "this path does not observe it" rather
than "this venue does not have it".
"""


def _kinds(names: tuple[str, ...]) -> set[Kind] | None:
    """Turn the wire spelling of the kind filter into the enum, or ``None`` for no filter.

    Raises:
        ValueError: naming the bad kind. The CLI mapped this to exit 1; silently matching
            nothing would make a typo look like a venue with no perpetuals.
    """
    if not names:
        return None
    try:
        return {Kind(name.lower()) for name in names}
    except ValueError as exc:
        raise ValueError(
            f"unknown instrument kind in {list(names)}: {exc}; "
            f"valid kinds are {sorted(k.value for k in Kind)}"
        ) from exc


def universe(ctx: CapabilityContext, params: UniverseParams) -> pl.DataFrame:
    """Enumerate — or volume-rank — one venue's tradable set. Live, off the venue's own API.

    Reaches the network and not the lake: ``load_markets`` and ``fetch_tickers`` are what a
    venue publishes about itself, which is why this capability can answer for a symbol that
    has never been collected. The coroutine is driven here rather than returned; see
    :func:`~crocodile.core.capability.run_to_completion`.

    ``mic`` and ``country`` are refused before anything is reached. ``source`` is required:
    a crypto venue is named by its id, and the crypto half has no other way to scope.
    """
    _refuse_market_dimensions("universe", params)
    if params.source is None:
        raise ValueError("universe for crypto needs source=<venue id>, e.g. source=binance")
    # Bound once: mypy does not carry the narrowing of ``params.source`` into the lambdas.
    source = params.source
    kinds = _kinds(params.kinds)
    top = params.top
    if top is not None:
        symbols = run_to_completion(
            lambda: top_symbols_by_volume(source, top, quote=params.quote, kinds=kinds)
        )
        return pl.DataFrame(
            [
                {"symbol": symbol, "kind": None, "base": None, "quote": None, "rank": rank}
                for rank, symbol in enumerate(symbols, start=1)
            ],
            schema=_UNIVERSE_SCHEMA,
        )

    instruments = filter_instruments(
        run_to_completion(lambda: exchange_instruments(source)),
        kinds=kinds,
        quote=params.quote,
    )
    return pl.DataFrame(
        [
            {
                "symbol": inst.symbol_raw,
                "kind": inst.kind.value,
                "base": inst.base,
                "quote": inst.quote,
                "rank": None,
            }
            for inst in instruments[: params.limit]
        ],
        schema=_UNIVERSE_SCHEMA,
    )


def census(ctx: CapabilityContext, params: CensusParams) -> dict[str, Any]:
    """Snapshot the whole crypto market — venues, coins, DeFi — from keyless public APIs.

    The clock is read here rather than taken as a parameter. ``market_census`` takes
    ``generated_ns`` so that it never reads the clock itself and stays deterministic under
    test; that is an argument for the *function's* signature, not for a field in a schema
    three surfaces publish, since when a snapshot was taken is not something a user chooses.
    """
    return run_to_completion(
        lambda: census_mod.market_census(
            generated_ns=now_ns(),
            venues=list(params.venues) or None,
            coin_pages=params.coin_pages,
        )
    )


def open_interest(ctx: CapabilityContext, params: OpenInterestParams) -> pl.DataFrame:
    """Align stored open interest across venues with a forward fill. A pure argument shuffle.

    ``ctx.catalog`` rather than ``ctx.query``: the SQL here is fixed and internal to the
    aggregator, not a string a caller supplied, so there is nothing for the readonly guard
    to read. This is the same handoff ``slippage`` makes.
    """
    return aggregate_open_interest(
        ctx.catalog,
        list(params.symbols),
        params.start_ns,
        params.end_ns,
    )


def open_interest_equities(ctx: CapabilityContext, params: OpenInterestParams) -> pl.DataFrame:
    """The same board, summed out of the option chain because no equity feed publishes it.

    A perpetual's venue states its open interest as one number and the crypto half reads
    it. A listed equity's is the sum over its option chain — Yahoo publishes
    ``openInterest`` per contract and nothing per underlying — so the aggregation is the
    equity half's own arithmetic, and it is the whole of the difference: both halves then
    hand their samples to the one forward-fill in
    :mod:`crocodile.core.analytics.open_interest` and return the same
    ``local_ts``/per-source/``total_oi`` frame.

    ``params.symbols`` keeps its meaning rather than acquiring an equity one. It is a tuple
    of case-insensitive literal substring patterns on both sides; what each side matches
    them against is the series it counts per, which is the perpetual's ``symbol`` there and
    the ``underlying`` here. A field that meant "pattern" for one asset class and "identity"
    for the other would be the divergence-under-one-name this registry exists to end, and
    ``OpenInterestParams``' docstring has the history of the three surfaces that each
    guessed differently.

    ``ctx.catalog`` rather than ``ctx.query``, for the reason :func:`open_interest` gives:
    the SQL is fixed and internal to the aggregator, not a string a caller supplied.
    """
    return aggregate_option_open_interest(
        ctx.catalog,
        list(params.symbols),
        params.start_ns,
        params.end_ns,
    )


def depth(ctx: CapabilityContext, params: DepthParams) -> DepthProfile:
    """A US-equity depth ladder, from real L1 when keyed and a modelled one when not.

    ``select_depth_source`` used to read ``ALPACA_API_KEY``/``ALPACA_API_SECRET`` off the
    process environment rather than from ``ctx.settings``, which was called a deviation from
    the rule :class:`~crocodile.core.capability.CapabilityContext` states and left alone
    because the switch was shared. The surface migration this docstring said would close it
    is what closed it: ``ctx.settings`` goes in, the switch still honours the bare spelling
    so nobody's keys go dark, and the *predicate* is exported as ``alpaca_is_keyed`` so the
    projection announcing this answer branches on the same call the implementation does.

    The declaration's ``prov`` is the *ceiling* — what this implementation produces on its
    best day, which is the keyed Alpaca L1 branch — and never a reading of today's
    environment. Which of the two branches actually ran is on the returned record's own
    tail, where ``provenance_fields`` measured it: ``alpaca_l1`` scores how much of the top
    of book was quoted, ``yahoo_1m_vap`` how much of a session the profile was binned out
    of. Declaring the keyless floor instead would understate a keyed deployment exactly as
    badly as declaring the ceiling overstates a keyless one, and only one of the two is
    fixed by a field that is documented as a maximum — which is why the floor is declared
    *beside* the ceiling, as :class:`~crocodile.core.capability.Fallback`, rather than
    replacing it. Before that existed the keyless deployment — the default one — announced
    nothing on the CLI and named ``alpaca_l1`` on REST and MCP while returning a ladder
    modelled from Yahoo 1m bars.
    """
    source = select_depth_source(
        bins=params.bins, top_n=params.top_n, method=params.method, settings=ctx.settings
    )
    return run_to_completion(lambda: source.snapshot(params.symbol))


def depth_crypto(ctx: CapabilityContext, params: DepthParams) -> DepthProfile:
    """A crypto depth ladder, sliced out of the book snapshots already in the lake.

    The half M8 names, and the one thing worth saying at the declaration site is what it is
    *not*. It is not the equity half's method pointed at a different market: nothing here is
    modelled, because a crypto venue streams the ladder and the sink has been storing it all
    along. It is not synchronous-over-async either — there is no coroutine, so no
    :func:`~crocodile.core.capability.run_to_completion` — because the lake is a local read
    and the network call the equity half makes has no counterpart here.

    ``ctx.query`` rather than ``ctx.catalog.query``, which is the other way round from
    :func:`open_interest` two functions up, and the difference is the ``WHERE`` clause.
    ``open_interest`` hands the aggregator a catalog because the statement is entirely
    internal to it and carries nothing a caller wrote. This one carries the caller's symbol,
    which arrives off a URL query string on REST and a tool argument on MCP — so the read
    goes through the bound method that applies this surface's ``readonly`` and ``row_limit``
    rather than around it. It is the method that is handed over rather than the whole
    context, so :func:`~crocodile.crypto.depth.depth_from_book_snapshots` can be exercised
    against a few rows with no lake on disk. ``ctx.asset_class`` goes with it for the reason
    the field exists: the surface has already resolved which market the request is in, and
    the reader has no crypto-specific line in it to re-derive that from.

    ``as_of_ns`` defaulting to :func:`~crocodile.core.util.time.now_ns` here rather than in
    the struct is deliberate. A default evaluated at import time would freeze the instant at
    process start, and a struct default cannot be a call; ``None`` means "when you are asked",
    and this is where being asked happens.
    """
    return depth_from_book_snapshots(
        ctx.query,
        params.symbol,
        asset_class=ctx.asset_class,
        as_of_ns=now_ns() if params.as_of_ns is None else params.as_of_ns,
        top_n=params.top_n,
        max_age_ns=params.max_age_ns,
    )


# ---------------------------------------------------------------------------
# M3 — the equity halves of `markets`, `universe` and `census`
# ---------------------------------------------------------------------------
# All three answer one question the crypto side gets from a venue's own API and equities
# have no single API for: what is listed, where, and in what. The resolution lives in
# `crocodile.equity.reference.universe`, which merges SEC EDGAR, Tiingo and OpenFIGI through
# `CoverageResolver`; the three adapters below are argument shuffling and framing over it.
#
# One difference from the crypto halves is worth naming rather than leaving to be found.
# Both halves of `markets` are now a pure function of this build: crypto reads its connector
# registry, and equity reads the bundled exchange table (`core/markets/data/exchanges.csv`)
# plus the providers' and grammars' MIC declarations, so neither touches the network. The
# equity halves of `universe` and `census` still reach the network for the US reference merge
# (their source branch) and read the listing master, a local file, for `mic`/`country`.

_FIGI_KEYLESS_BURST: Final[int] = 250
"""Tickers OpenFIGI's keyless tier maps before its own limiter starts pacing.

Twenty-five requests of ten jobs, both read off ``OpenFigiClient.__init__`` rather than
chosen here: that constructor sets ``capacity=25`` and ``_batch_size=10`` when it has no key.
Beyond the burst the limiter refills at twenty-five a minute, so a thousand-ticker slice
would take the better part of an hour — which is why enrichment stops at the burst instead of
turning a table request into a batch job.
"""

_FIGI_KEYED_BURST: Final[int] = 2500
"""The same arithmetic with a key: twenty-five requests of a hundred jobs.

The key buys throughput and no fields, so a slice past this line is not missing data that a
credential would have revealed — it is missing the third attestation, and
``reference_merge``'s confidence reports 0.67 for exactly that.
"""

_EQUITY_VENUE_IS_CCXT: Final[bool] = False
"""Whether a ccxt connector serves an equity venue. It does not, and that is a fact.

Spelled as a name so the column below is not a bare ``False`` a reader has to guess at. ccxt
is a cryptocurrency exchange library; it reaches no equity venue at any version, so
``markets --ccxt-only`` returning nothing for equities is an answer rather than an empty
result that reads like a broken filter.
"""

_EQUITY_VENUE_IS_NATIVE: Final[bool] = True
"""Whether a hand-written connector serves an equity venue. It does, for all of them.

The flag means "served by a hand-written connector rather than by a generic library", and
the equity side has only the hand-written tier — the hand-written providers ``list-exchanges``
returns. Constant for this asset class and not for the other, which is what the column is
for: it distinguishes two tiers, and equities have one.
"""


def _reference_evidence(ctx: CapabilityContext) -> reference.ReferenceEvidence:
    """Fetch the two whole-universe reference sources, stamped with one instant.

    The SEC contact string is validated before anything is built — see
    :func:`~crocodile.equity.reference.universe.require_sec_user_agent`, which argues why a
    missing one is a refusal rather than a default.

    The clock is read here for the reason :func:`census` reads it here: when a snapshot was
    taken is not something a user chooses, and the resolution underneath takes the instant as
    a parameter so it stays deterministic under test.
    """
    agent = reference.require_sec_user_agent(ctx.settings)
    return run_to_completion(
        lambda: reference.fetch_bulk_evidence(as_of_ns=now_ns(), sec_user_agent=agent)
    )


def _volume_evidence(ctx: CapabilityContext) -> dict[str, float]:
    """Read per-symbol traded volume out of the lake, or ``{}`` when the lake holds no bars."""
    return reference.volume_by_symbol(ctx.query, channels=ctx.catalog.list_channels())


def _figi_budget(settings: Settings) -> int:
    """How many tickers this deployment will enrich in one request, keyed or not."""
    return _FIGI_KEYED_BURST if settings.openfigi_api_key else _FIGI_KEYLESS_BURST


def _enriched(
    ctx: CapabilityContext,
    evidence: reference.ReferenceEvidence,
    listings: list[reference.Listing],
) -> list[reference.Listing]:
    """Add OpenFIGI's identifiers to the slice being returned, if it fits in one burst.

    Enrichment is applied *after* the slice is chosen rather than across the universe, which
    is the whole reason the third source can be in the method at all: OpenFIGI is per-symbol,
    so enriching ninety thousand tickers to return fifty of them would be ninety thousand
    tickers of rate limit spent on a table nobody asked for.

    A slice larger than the budget is returned un-enriched rather than partially enriched.
    Half a slice at three attestations and half at two would make ``prov_confidence`` a
    report on where a batch boundary fell, and a caller comparing two rows would read a
    difference in coverage that is really a difference in position.

    The original order is restored afterwards because the merge sorts by ticker and the
    caller may have asked for a ranking.
    """
    symbols = [listing.symbol for listing in listings]
    if not symbols or len(symbols) > _figi_budget(ctx.settings):
        return listings
    matches = run_to_completion(
        lambda: reference.fetch_figi(symbols, api_key=ctx.settings.openfigi_api_key)
    )
    if not matches:
        return listings
    position = {symbol: index for index, symbol in enumerate(symbols)}
    enriched = evidence.restricted(symbols).with_figi(matches).merged()
    enriched.sort(key=lambda listing: position.get(listing.symbol, len(position)))
    return enriched


def _require_mic(raw: str) -> str:
    """The operating MIC of ``raw``, which must be an ``exchanges.csv`` row.

    Surfaces may upper-case input; the core registries do not guess, so the adapter does.
    ``UnknownMarketError`` is a ``LookupError`` and would surface as a 500, so it is re-raised
    as the ``ValueError`` every surface maps to 400. The registry's message is kept, which is
    how an expired code names its replacement.
    """
    try:
        return ExchangeRegistry().resolve(raw.strip().upper()).mic
    except UnknownMarketError as exc:
        raise ValueError(f"mic {raw!r}: {exc}") from exc


def _require_country(raw: str) -> str:
    """The upper-case ISO 3166 alpha-2 code of ``raw``, which must name at least one exchange."""
    code = raw.strip().upper()
    try:
        found = ExchangeRegistry().by_country(code)
    except ValueError as exc:
        raise ValueError(f"country {raw!r} is not an ISO 3166-1 alpha-2 code") from exc
    if not found:
        raise ValueError(
            f"exchanges.csv lists no exchange in country {code!r}; "
            "`markets --asset-class equity` lists the countries it covers"
        )
    return code


def _market_scope(mic: str | None, country: str | None) -> tuple[str | None, str | None]:
    """Validate the two market dimensions and check that they agree when both are set."""
    mic_code = None if mic is None else _require_mic(mic)
    country_code = None if country is None else _require_country(country)
    if mic_code is not None and country_code is not None:
        mic_country = ExchangeRegistry().get(mic_code).country
        if mic_country != country_code:
            raise ValueError(
                f"mic {mic_code!r} is in country {mic_country!r}, not {country_code!r}"
            )
    return mic_code, country_code


def _calendar_quality(info: ExchangeInfo) -> str:
    """The calendar quality ``calendar_for`` answers, so the two can never disagree.

    Measured at task 08 (Step 0): reading the quality of all 172 calendars takes about 0.04 s on
    this host, far under the 1.5 s the brief allows, so the row asks ``calendar_for`` itself
    rather than re-deriving the rule from ``xcal_code``.
    """
    return calendar_for(info.mic).quality.value


def _grammar_mics() -> dict[str, frozenset[str]]:
    """The MICs each grammar table serves, for the registered sources only.

    Only registered providers count: a grammar table for a source nothing can collect from is
    not coverage.
    """
    registered = set(provider_factory.list_providers())
    return {
        source: load_grammar(source).supported_mics()
        for source in GRAMMAR_SOURCES
        if source in registered
    }


def _serving_sources(mic: str, grammar_mics: Mapping[str, frozenset[str]]) -> list[str]:
    """The registered equity sources that serve ``mic``, by declaration or by grammar table."""
    serving: list[str] = []
    for name in sorted(provider_factory.list_providers()):
        declared = provider_factory.supported_mics(name)
        in_grammar = mic in grammar_mics.get(name, frozenset())
        if in_grammar or (declared is not None and mic in declared):
            serving.append(name)
    return serving


_RANKED_CHANNELS: Final[tuple[str, ...]] = ("ohlcv", "instrument", "corp_action")


def _ranked_sources(mic: str, serving: list[str], order: SourceOrder) -> list[str]:
    """``serving`` in the order table's rank: first appearance in the ohlcv, instrument, then
    corp_action chain of ``mic``; sources in none of the chains follow in name order.

    The ohlcv chain is the order ``resolve_daily_bars`` applies, so the first name here is the
    source a reader asks first. A broken table raises from ``source_order`` before this runs.
    """
    rank: dict[str, int] = {}
    for channel in _RANKED_CHANNELS:
        for name in order.order_for(mic, channel):
            rank.setdefault(name, len(rank))
    unranked = len(rank)
    return sorted(serving, key=lambda name: (rank.get(name, unranked), name))


def _exchange_row(
    info: ExchangeInfo,
    grammar_mics: Mapping[str, frozenset[str]],
    order: SourceOrder,
    coverage: MarketCoverage,
) -> dict[str, Any]:
    """One ``markets`` row for an allow-listed exchange, with every column of the schema."""
    sources = _ranked_sources(info.mic, _serving_sources(info.mic, grammar_mics), order)
    return {
        "venue": info.mic,
        "native": _EQUITY_VENUE_IS_NATIVE,
        "ccxt": _EQUITY_VENUE_IS_CCXT,
        "mic": info.mic,
        "name": info.name,
        "country": info.country,
        "timezone": info.tz.key,
        "currency": info.currency,
        "quote_scale": info.quote_scale,
        "calendar_quality": _calendar_quality(info),
        "sources": sources,
        "usage_classes": [usage_class_of(name) for name in sources],
        "tier_research": info.tier_research,
        "sanctioned": info.sanctioned,
        "listings_known": coverage.listings_known(info.mic),
        "last_success": coverage.last_success_ns(info.mic),
    }


def markets_equities(ctx: CapabilityContext, params: MarketsParams) -> pl.DataFrame:
    """Every allow-listed exchange, from the bundled exchange table, with its reference facts.

    The row source is ``exchanges.csv`` (through :class:`ExchangeRegistry`) plus the providers'
    and grammars' MIC declarations. Nothing here contacts SEC or any network, so no SEC contact
    string is needed: the table is packaged data, read rather than reconstructed, which is what
    ``native`` scores. Before this change the row source was the merged reference universe, so
    a clean lake had no exchanges to list.

    ``mic`` selects one exchange (a segment answers for its operating exchange), ``country`` the
    exchanges of one country, and ``search`` is a case-insensitive substring of the MIC or the
    name. Bad input raises ``ValueError`` naming the input: an unknown or expired MIC (the
    registry's replacement is kept), a malformed or exchange-less country, and a MIC and a
    country that disagree.

    The two tier flags are constant for this asset class and that is an answer rather than
    padding; see :data:`_EQUITY_VENUE_IS_NATIVE` and :data:`_EQUITY_VENUE_IS_CCXT`. Both filters
    are applied structurally so that the day either constant stops being constant the filters
    follow it. ``--native-only`` keeps everything and ``--ccxt-only`` keeps nothing.

    ``sources`` lists the serving sources in rank order: first appearance in the ``ohlcv``
    chain (the order ``resolve_daily_bars`` applies), then ``instrument``, then
    ``corp_action``; the rest follow by name. ``usage_classes`` is aligned with it. The three
    local-state columns are read through :func:`read_market_coverage`, which never creates or
    writes a file: ``listings_known`` is 0 when no master exists and null when it is unreadable,
    and ``last_success`` is null when the state holds no success for the MIC or is unreadable.
    """
    mic, country = _market_scope(params.mic, params.country)
    # Both tier filters are applied structurally rather than one being left implicit, so that
    # the day either constant stops being constant the filters follow it instead of quietly
    # disagreeing with the column they read.
    if (params.ccxt_only and not _EQUITY_VENUE_IS_CCXT) or (
        params.native_only and not _EQUITY_VENUE_IS_NATIVE
    ):
        return pl.DataFrame([], schema=_MARKETS_SCHEMA)

    registry = ExchangeRegistry()
    infos = (registry.get(mic),) if mic is not None else registry.all()
    needle = params.search.lower() if params.search is not None else None
    grammar_mics = _grammar_mics()
    order = source_order()
    coverage = read_market_coverage(ctx.settings.data_dir)
    rows: list[dict[str, Any]] = []
    for info in infos:
        if country is not None and info.country != country:
            continue
        if needle is not None and not (needle in info.mic.lower() or needle in info.name.lower()):
            continue
        rows.append(_exchange_row(info, grammar_mics, order, coverage))
    return pl.DataFrame(rows, schema=_MARKETS_SCHEMA)


def _listing_master(ctx: CapabilityContext) -> ListingMaster:
    """Open the lake's listing master read-only, or refuse. Never creates one.

    Task 04's read-write constructor creates the parent directory and the file, which would
    make a read capability write to the lake root from REST and MCP. Even on an existing file
    a read-write open runs ``PRAGMA journal_mode``, the DDL and possibly a ``schema_version``
    insert, and a plain ``?mode=ro`` open leaves ``-wal``/``-shm`` behind. ``readonly=True``
    does neither (interfaces ruling 27). This is the monkeypatch seam the tests use.
    """
    path = default_listings_path(ctx.settings.data_dir)
    if not path.exists():
        raise ValueError(
            f"no listing master at {path}; run `crocodile universe-sync` (with --mic or "
            "--country) to build one"
        )
    return ListingMaster(path, readonly=True)


def _universe_from_listing_master(ctx: CapabilityContext, params: UniverseParams) -> pl.DataFrame:
    """One MIC or one country out of the listing master, under the same filters and ranking.

    Delisted listings are dropped; unseen ones are kept, because an unseen listing is absence of
    evidence rather than evidence of absence. ``top`` ranks by the lake's stored bars, exactly as
    the legacy branch does, and ``limit`` caps the enumeration. The full slice is returned: the
    surface ceiling is applied by ``dispatch.payload``, which must see every row to set
    ``truncated``.
    """
    mic, country = _market_scope(params.mic, params.country)
    kinds = reference.parse_kinds(params.kinds)
    currency = params.quote.strip().upper() if params.quote else None
    scope = f"mic={mic!r}" if mic is not None else f"country={country!r}"
    with _listing_master(ctx) as master:
        found = master.listings(mic=mic, country=country)
    if not found:
        raise ValueError(
            f"the listing master holds no listing for {scope}; run `crocodile universe-sync` "
            "for that market first"
        )
    kept = [
        listing
        for listing in found
        if listing.status is not ListingStatus.DELISTED
        and (kinds is None or listing.security_type in kinds)
        and (currency is None or (listing.currency or "").upper() == currency)
    ]
    if params.top is not None:
        volumes = _volume_evidence(ctx)
        if not volumes:
            raise ValueError(
                "ranking equities by traded volume reads this lake's own stored ohlcv bars "
                "and it holds none, so there is nothing to rank; collect or backfill the "
                "ohlcv channel first, or drop --top to enumerate the universe instead"
            )
        selected = sorted(kept, key=lambda item: (-volumes.get(item.symbol, 0.0), item.symbol))
        selected = selected[: params.top]
        ranks: list[int | None] = list(range(1, len(selected) + 1))
    else:
        selected = kept[: params.limit]
        ranks = [None] * len(selected)
    return pl.DataFrame(
        [
            {
                "symbol": listing.symbol,
                "kind": listing.security_type.value
                if listing.security_type is not None
                else None,
                "base": listing.symbol,
                "quote": listing.currency,
                "rank": rank,
            }
            for listing, rank in zip(selected, ranks, strict=True)
        ],
        schema=_UNIVERSE_SCHEMA,
    )


def universe_equities(ctx: CapabilityContext, params: UniverseParams) -> pl.DataFrame:
    """Enumerate — or volume-rank — one equity venue's listed set, from merged reference data.

    ``source`` is an exchange rather than a data vendor, which is what makes this the same
    capability as its crypto twin: ``markets`` lists venues and ``universe`` enumerates one of
    them, on both sides. Naming a *provider* here would have made the pair incoherent —
    ``list-exchanges`` is already the capability that lists equity vendors.

    The five columns are the crypto schema and mean the same things. ``base`` and ``quote``
    decompose the instrument into what you acquire and what you pay with: on Binance
    ``BTCUSDT`` that is BTC and USDT, and on NASDAQ ``AAPL`` priced in dollars it is AAPL and
    USD. The decomposition is trivial for a security and it is still the same decomposition,
    which is why the column is filled rather than nulled. ``kind`` carries a
    :class:`~crocodile.core.schema.enums.SecurityType` where the crypto half carries a
    ``Kind`` — the same column asking each market its own question, exactly as ``source``
    does.

    Two scopes, and exactly one may be named. ``source`` takes the US reference merge above.
    ``mic`` or ``country`` takes the listing master (``universe-sync`` builds it): delisted
    listings are dropped, unseen ones kept, and kinds, quote, limit and top are honoured the same
    way. Naming both scopes, or neither, is a ``ValueError``.

    ``rank`` is filled only on the ``top`` branch, which is the crypto contract: the
    enumerating branch returns the universe in ticker order and observes no ranking, and a
    null says the path did not observe it rather than that the venue has none.
    """
    if params.mic is not None or params.country is not None:
        if params.source is not None:
            raise ValueError(
                "universe takes one scope: source (the US reference merge) or mic/country "
                "(the listing master), not both"
            )
        return _universe_from_listing_master(ctx, params)
    if params.source is None:
        raise ValueError(
            "universe for equities needs a scope: source=<exchange label> for the US reference "
            "merge, or mic=/country= for the listing master"
        )
    # Bound once: mypy does not carry the narrowing of ``params.source`` into the call below.
    source = params.source
    kinds = reference.parse_kinds(params.kinds)
    evidence = _reference_evidence(ctx)
    listings = reference.filter_listings(
        evidence.merged(), exchange=source, kinds=kinds, currency=params.quote
    )

    if params.top is not None:
        volumes = _volume_evidence(ctx)
        if not volumes:
            raise ValueError(
                "ranking equities by traded volume reads this lake's own stored ohlcv bars "
                "and it holds none, so there is nothing to rank; collect or backfill the "
                "ohlcv channel first, or drop --top to enumerate the universe instead"
            )
        selected = reference.rank_listings(listings, volumes)[: params.top]
        ranks: list[int | None] = list(range(1, len(selected) + 1))
    else:
        selected = listings[: params.limit]
        ranks = [None] * len(selected)

    return pl.DataFrame(
        [
            {
                "symbol": listing.symbol,
                "kind": listing.instrument.security_type.value
                if listing.instrument.security_type is not None
                else None,
                "base": listing.symbol,
                "quote": listing.currency,
                "rank": rank,
            }
            for listing, rank in zip(_enriched(ctx, evidence, selected), ranks, strict=True)
        ],
        schema=_UNIVERSE_SCHEMA,
    )


def census_equities(ctx: CapabilityContext, params: CensusParams) -> dict[str, Any]:
    """Snapshot the whole equity market — venues, listings, attestation — from keyless sources.

    The crypto census counts a venue universe and a coin universe. This counts a venue
    universe and a *listing* universe, and adds the one number the crypto side has no
    question for: how many registries agreed about each identity. That is not decoration.
    The crypto census enumerates one authority per venue and can only report what it found;
    this one enumerates three overlapping authorities, so "how much of the market do all
    three agree exists" is a real measurement of the market's own reference data, and it is
    the number that says whether a universe is trustworthy enough to trade off.

    ``connectors`` mirrors the crypto block field for field so a projection can render either
    snapshot, with ``ccxt_count`` a structural zero for the reason
    :data:`_EQUITY_VENUE_IS_CCXT` gives.

    ``venues`` accepts the same filter the crypto half does: naming venues restricts the
    count to them. ``coin_pages`` has no equity meaning and is ignored, on the argument
    ``CollectParams`` makes for ``dlq_report_path`` — an optional parameter costs a caller
    who omits it nothing, and there is no coin universe here to page through.

    Nothing here is enriched with OpenFIGI. A census counts the whole universe and OpenFIGI
    is per-symbol, so every row is attested by the two bulk sources and the ``two_sources``
    bucket is where the mass sits; a deployment that wants three-source counts is asking for
    a ninety-thousand-ticker mapping job, which is not a capability call.
    """
    listings = _reference_evidence(ctx).merged()
    wanted = {venue.strip().upper() for venue in params.venues}
    if wanted:
        listings = [
            listing
            for listing in listings
            if (listing.instrument.exchange or "").upper() in wanted
        ]

    kind_keys = [member.value for member in SecurityType]
    by_venue: dict[str, dict[str, int]] = {}
    for listing in listings:
        venue = listing.instrument.exchange or ""
        counts = by_venue.setdefault(venue, dict.fromkeys(kind_keys, 0))
        kind = listing.instrument.security_type or SecurityType.UNKNOWN
        counts[kind.value] += 1

    # Sorted on the counts rather than on the built rows: `markets` is an ``int`` here and an
    # ``object`` once it is a value in a heterogeneous dict, and a key function that has to
    # coerce it back is a key function that would silently accept a string.
    ordered = sorted(by_venue.items(), key=lambda item: (-sum(item[1].values()), item[0]))
    rows: list[dict[str, Any]] = [
        {"exchange": venue, "markets": sum(counts.values()), **counts} for venue, counts in ordered
    ]
    providers = provider_factory.list_providers()

    return {
        "generated_ns": now_ns(),
        "connectors": {
            "native": sorted(providers),
            "native_count": len(providers),
            "ccxt_count": 0,
            "total_reachable": len(providers),
        },
        "venues": {
            "enumerated": len(ordered),
            "total_markets": sum(sum(counts.values()) for _, counts in ordered),
            "by_kind": {key: sum(counts[key] for _, counts in ordered) for key in kind_keys},
            "rows": rows,
        },
        "securities": {
            "resolved": len(listings),
            "by_source": {
                source: sum(1 for listing in listings if source in listing.sources)
                for source in reference.REFERENCE_PRIORITY
            },
            "attested_by": {
                "one_source": sum(1 for listing in listings if len(listing.sources) == 1),
                "two_sources": sum(1 for listing in listings if len(listing.sources) == 2),
                "three_sources": sum(1 for listing in listings if len(listing.sources) == 3),
            },
            "with_cik": sum(1 for listing in listings if listing.instrument.cik),
            "with_figi": sum(1 for listing in listings if listing.instrument.figi),
        },
    }


# ---------------------------------------------------------------------------
# calendar
# ---------------------------------------------------------------------------

_CALENDAR_SCHEMA: Final[Mapping[str, pl.DataType]] = {
    "mic": pl.String(),  # equity: operating MIC; crypto: null
    "date": pl.Date(),  # session date in the exchange's own calendar
    "open_utc": pl.Datetime("us", "UTC"),  # null on WEEKDAY_FALLBACK rows (open unknown)
    "close_utc": pl.Datetime("us", "UTC"),  # null on WEEKDAY_FALLBACK rows without a known close
    "quality": pl.String(),  # CalendarQuality member .value
}
"""One schema for both halves of ``calendar``, nulls where a half does not observe a column.

The same argument as ``_MARKETS_SCHEMA``: a null says "this half does not observe it", and the
two halves project through one renderer.
"""

_CALENDAR_MAX_DAYS: Final[int] = 36_600
"""Longest range one request may span, about a century. No holiday table reaches that far, so
the cap only refuses a typo such as year 20260 that would otherwise materialise millions of
always-open rows."""


def _iso_date(field: str, value: str) -> date:
    """``value`` as a date, or a ``ValueError`` that names the field it came from."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"calendar: {field} {value!r} is not an ISO date (YYYY-MM-DD)") from exc


def _calendar_dates(params: CalendarParams) -> tuple[date, date]:
    """Both bounds of the request, validated: ISO dates, ordered, and inside the span cap.

    Raises:
        ValueError: naming the field that is malformed, the inverted range, or the day count
            and the cap when the span is longer than ``_CALENDAR_MAX_DAYS``.
    """
    start = _iso_date("start", params.start)
    end = _iso_date("end", params.end)
    if end < start:
        raise ValueError(f"calendar: end {params.end!r} is before start {params.start!r}")
    days = (end - start).days + 1
    if days > _CALENDAR_MAX_DAYS:
        raise ValueError(
            f"calendar: the range spans {days} days, more than the {_CALENDAR_MAX_DAYS}-day cap"
        )
    return start, end


def _calendar_frame(
    calendar: ExchangeCalendar, start: date, end: date, *, mic: str | None, close_known: bool
) -> pl.DataFrame:
    """One row per session of ``calendar`` in ``[start, end]``, with the quality of each row.

    Ruling: a row's quality is ``calendar.quality_for(d)``, not ``calendar.quality``, because a
    library MIC past its bound answers by the weekday fallback (XSHG, XBOM and XSES after
    2026-12-31 with exchange_calendars 4.13.2). For every quality except ``WEEKDAY_FALLBACK``
    both times come from the calendar. On a ``WEEKDAY_FALLBACK`` row the open is unknown and is
    emitted as null, never the placeholder local midnight the protocol reports so that the
    scheduler stays total; the close is emitted only when ``exchanges.csv`` carries one
    (``close_known``), never the 23:00 placeholder. Cost if wrong: a reader of a fallback row
    sees nulls where a placeholder used to be, and no hours are invented for a venue.
    """
    rows: list[dict[str, Any]] = []
    for d in calendar.sessions(start, end):
        quality = calendar.quality_for(d)
        if quality is CalendarQuality.WEEKDAY_FALLBACK:
            opened = None
            closed = calendar.session_close_utc(d) if close_known else None
        else:
            opened = calendar.session_open_utc(d)
            closed = calendar.session_close_utc(d)
        rows.append(
            {
                "mic": mic,
                "date": d,
                "open_utc": opened,
                "close_utc": closed,
                "quality": quality.value,
            }
        )
    return pl.DataFrame(rows, schema=_CALENDAR_SCHEMA)


def calendar_crypto(ctx: CapabilityContext, params: CalendarParams) -> pl.DataFrame:
    """Every day is a session and every session is open from 00:00 UTC to 00:00 UTC next day.

    ``mic`` stays null: ``AlwaysOpenCalendar``'s own ``mic`` is a free label (``"24/7"``), not an
    ISO 10383 code, so writing it into the column would present a label as a market.
    """
    _refuse_market_dimensions("calendar", params)
    start, end = _calendar_dates(params)
    return _calendar_frame(AlwaysOpenCalendar(), start, end, mic=None, close_known=True)


def calendar_equities(ctx: CapabilityContext, params: CalendarParams) -> pl.DataFrame:
    """The sessions of one exchange, by its calendar, with open and close in UTC.

    The MIC is resolved to its operating exchange first, so a segment answers with the
    operating MIC in the ``mic`` column. The close is known when ``exchanges.csv`` carries a
    ``close_local``; a fallback row without one says so by a null close.
    """
    if params.mic is None:
        raise ValueError(
            "calendar for equities needs mic=<MIC>; `markets --asset-class equity` lists them"
        )
    operating = _require_mic(params.mic)
    start, end = _calendar_dates(params)
    close_known = ExchangeRegistry().get(operating).close_local is not None
    return _calendar_frame(
        calendar_for(operating), start, end, mic=operating, close_known=close_known
    )


LIST_EXCHANGES = declare(
    Capability(
        name="list-exchanges",
        summary="Registered source connectors this build can collect from. No lake, no network.",
        params=ListExchangesParams,
        returns=ReturnKind.TABLE,
        impls={
            # NATIVE for a list of connector names reads oddly until you read the formula:
            # `native` scores 1.0 because a value was "read rather than reconstructed", and
            # nothing about a registry is modelled. DERIVED would claim it was computed
            # from records in the lake, which is the one thing this capability promises not
            # to touch.
            AssetClass.CRYPTO: Impl(fn=list_exchanges, prov=Provenance.NATIVE, basis="native"),
            AssetClass.EQUITY: Impl(fn=list_providers, prov=Provenance.NATIVE, basis="native"),
        },
    )
)


MARKETS = declare(
    Capability(
        name="markets",
        summary="Every venue reachable from this build, native connector or ccxt.",
        params=MarketsParams,
        returns=ReturnKind.TABLE,
        impls={
            AssetClass.CRYPTO: Impl(fn=markets, prov=Provenance.NATIVE, basis="native"),
            # NATIVE on both sides. Both halves now read this build's own declarations: the
            # crypto connector registry on one side, and the bundled exchange table
            # (`core/markets/data/exchanges.csv`) plus the providers' and grammars' MIC
            # declarations on the other, read rather than reconstructed, which is what
            # `native` scores (`core/schema/provenance.py:324-331`). The equity half no longer
            # merges three registries, so `reference_merge` would name a method that does not
            # run.
            AssetClass.EQUITY: Impl(fn=markets_equities, prov=Provenance.NATIVE, basis="native"),
        },
    )
)


UNIVERSE = declare(
    Capability(
        name="universe",
        summary="Enumerate or volume-rank one venue's whole tradable instrument set.",
        params=UniverseParams,
        returns=ReturnKind.TABLE,
        impls={
            # The rows are the venue's own market list and its own 24 h volumes, fetched
            # live. Ranking sorts them; a sort adds no modelling, so the ceiling stays where
            # the venue put it.
            AssetClass.CRYPTO: Impl(fn=universe, prov=Provenance.NATIVE, basis="native"),
            # The equity rows are nobody's published market list — they are one assembled
            # from three registries under a stated priority, which is what `reference_merge`
            # names and why the ceiling is DERIVED rather than the NATIVE its twin reaches.
            # The listing-master branch (``mic``/``country``) is also a merged reference view:
            # observations from several sources are reconciled per (mic, local_symbol). So the
            # ceiling and the basis stand, and spec 3.13 adds no basis for it (a new one would
            # widen scope).
            # The ranked branch does not lower it further: ranking sorts rows by evidence
            # already in the lake and adds no modelling, exactly as on the crypto side.
            AssetClass.EQUITY: Impl(
                fn=universe_equities,
                prov=Provenance.DERIVED,
                basis="reference_merge",
                # The rows come from the three registries over the network; the lake is
                # read only to rank them, and only for `--top`. `volume_by_symbol` asks
                # `Catalog.list_channels` first and returns an empty ranking when the
                # lake holds no bars, so this is a channel the answer improves with
                # rather than one it requires — and it is still a channel this
                # implementation reads, which is what this field records.
                reads=("ohlcv",),
            ),
        },
    )
)


CENSUS = declare(
    Capability(
        name="census",
        summary="Live whole-market snapshot: venue market counts, coin universe, DeFi TVL.",
        params=CensusParams,
        returns=ReturnKind.SCALAR,
        impls={
            # DERIVED where `universe` is NATIVE, and the difference is real: this reports
            # counts, sums and dominance ratios computed across venues and endpoints, not
            # the rows any one of them published. Its inputs are all natively reported,
            # which is what `basis` names — the same split `indicators` makes.
            AssetClass.CRYPTO: Impl(fn=census, prov=Provenance.DERIVED, basis="native"),
            # DERIVED on both sides, and the `basis` is where they differ. Crypto's inputs
            # are natively reported — one venue's own market list, CoinGecko's own totals —
            # so it names `native`. This one counts a universe that had to be resolved
            # before it could be counted, so its inputs rest on the merge and it says so.
            AssetClass.EQUITY: Impl(
                fn=census_equities, prov=Provenance.DERIVED, basis="reference_merge"
            ),
        },
    )
)


OPEN_INTEREST = declare(
    Capability(
        name="open-interest",
        summary="Open interest aligned across venues with forward fill, from stored records.",
        params=OpenInterestParams,
        returns=ReturnKind.TABLE,
        impls={
            AssetClass.CRYPTO: Impl(
                fn=open_interest,
                prov=Provenance.DERIVED,
                basis="native",
                reads=("open_interest",),
            ),
            # DERIVED and `native` on both sides, and for one argument rather than for
            # symmetry's sake. The inputs are venue- and provider-reported open interest —
            # a perpetual's own figure on one side, a contract's own figure on the other —
            # so `native` names what is behind them; the alignment, the forward fill and,
            # on this side, the sum over a chain are this implementation's work, which is
            # what makes the board DERIVED rather than the NATIVE its inputs are. That the
            # equity number is a sum where the crypto one is a reading changes the
            # arithmetic and not the provenance: a sum of reported values is still not a
            # model of anything, which is the line SYNTHETIC is on the far side of.
            AssetClass.EQUITY: Impl(
                fn=open_interest_equities,
                prov=Provenance.DERIVED,
                basis="native",
                # Not `open_interest`. The equity figure is the per-contract open
                # interest carried on an `options_chain` row, summed over the chain —
                # which is why binding `fn=open_interest` here would have read a channel
                # no equity provider writes, the defect this batch already recorded once
                # and then repeated one channel over: `options_chain` was equally
                # unwritable until `yahoo` joined the provider registry.
                reads=("options_chain",),
            ),
        },
    )
)


DEPTH = declare(
    Capability(
        name="depth",
        summary="Market-depth ladder around a reference price, real L1 when keyed.",
        params=DepthParams,
        returns=ReturnKind.SCALAR,
        impls={
            # The ceiling, argued in `depth`: `alpaca_l1` registers as DERIVED — every
            # number in the profile was quoted by the venue, but the venue quoted a top of
            # book rather than a ladder — and it is the best of the two branches
            # `select_depth_source` can take.
            #
            # The *other* branch is what a keyless deployment gets, and until `fallback`
            # existed nothing said so: the ladder came back stamped SYNTHETIC/`yahoo_1m_vap`
            # while the surfaces published DERIVED/`alpaca_l1` and the CLI — where
            # `banner_for` suppresses DERIVED — printed nothing at all. This is the one
            # hand-written banner the projection was generalised from, silent in the default
            # deployment. `alpaca_is_keyed` is the predicate `select_depth_source` itself
            # branches on, so the announcement cannot disagree with the branch.
            AssetClass.EQUITY: Impl(
                fn=depth,
                prov=Provenance.DERIVED,
                basis="alpaca_l1",
                # Nothing: `select_depth_source` fetches from Alpaca or models a ladder
                # from Yahoo bars, and neither branch touches the lake.
                reads=(),
                fallback=Fallback(
                    prov=Provenance.SYNTHETIC,
                    basis="yahoo_1m_vap",
                    reachable=alpaca_is_keyed,
                ),
            ),
            # DERIVED on both halves, and the two arrive at it from opposite directions.
            # Equity's best branch is a real quote reshaped into a ladder; crypto's is a
            # real ladder re-cut and re-stamped. Neither is NATIVE — no venue published
            # *this* record — and neither is SYNTHETIC, because nothing in either is
            # modelled. `book_snapshot_slice` is where the second of those is argued.
            AssetClass.CRYPTO: Impl(
                fn=depth_crypto,
                prov=Provenance.DERIVED,
                basis="book_snapshot_slice",
                reads=("book_snapshot",),
            ),
        },
    )
)


CALENDAR = declare(
    Capability(
        name="calendar",
        summary="Trading sessions of one exchange by MIC, in UTC; crypto never closes.",
        params=CalendarParams,
        returns=ReturnKind.TABLE,
        impls={
            # The crypto rows are a pure function of the caller's date range under a market that
            # never closes, which is `caller_supplied`'s argument (`core/schema/provenance.py`,
            # the basis whose inputs are the caller's own parameters). The equity rows are
            # computed from published exchange schedules as the calendar library encodes them,
            # or from the exchange table's weekend and close columns: those inputs are read
            # rather than modelled, so they are `native`, and the computed table is `DERIVED`.
            # The per-row `quality` column says which encoding answered, and a WEEKDAY_FALLBACK
            # row knows no holidays. Both halves are within the trust-rank gate
            # (`tests/conformance/test_capability.py`).
            AssetClass.CRYPTO: Impl(
                fn=calendar_crypto, prov=Provenance.DERIVED, basis="caller_supplied"
            ),
            AssetClass.EQUITY: Impl(fn=calendar_equities, prov=Provenance.DERIVED, basis="native"),
        },
    )
)


# ---------------------------------------------------------------------------
# The schedule for the halves that are missing
# ---------------------------------------------------------------------------
# Filled here rather than in `core/capability.py` for the reason the batch package exists:
# four agents write these in parallel, and one file would serialise them behind one set of
# merge conflicts. `declare()` and this assignment are both idempotent, so a module body
# that runs twice leaves the same ledger.
#
# `markets`, `universe` and `census` were here against M3 and have left, which is the ledger
# working rather than the ledger shrinking: all three fell out of one piece of reference data,
# so they were scheduled together and they closed together. What closed them is
# `crocodile.equity.reference.universe` — SEC EDGAR x OpenFIGI x Tiingo merged by
# `CoverageResolver`, which is M3 word for word. The matching lines in
# `tests/conformance/test_pending_symmetry.py::_LEDGER_AS_SHIPPED` went in the same commit,
# because a gate there asserts the two agree and a schedule only means something while
# somebody has to look at both halves of a departure.
PENDING_SYMMETRY.update(
    {
        # This block is empty, and every paragraph in it is written in the past tense on
        # purpose: it is the record of what the ledger held and how each entry left, kept
        # because the arguments outlived the entries. Anything still scheduled would appear
        # as a live mapping below, not as a sentence.
        #
        # An equity venue list and an equity instrument universe both fell out of the same
        # reference data, so both waited on the same method — see M3 at the end of this
        # block, which is what closed them and the census with them.
        # `open-interest` was here against M2 and is repaid: the equity half sums the
        # stored Yahoo chain's per-contract `openInterest` per underlying, which is what
        # M2 specified, and both halves now widen their samples through the one function
        # in `core.analytics.open_interest` so the two boards are the same table.
        # `depth` was here too, mapped to M6, and it is the entry this block should be read
        # backwards from. It was the one whose *direction* the ledger could not express:
        # every method in SPEC_METHODS closes an equity gap, and depth's missing half was
        # the crypto one, because the equity half — the synthetic VAP ladder upgraded by
        # Alpaca L1, which this module declares — is what already shipped. M6 describes
        # that equity half, so M6 was already spent and could never have closed this. The
        # entry was scheduled against it anyway, because IRREDUCIBLE would have claimed a
        # crypto order book cannot exist and leaving the capability undeclared is the
        # silent absence the registry exists to end. It was recorded as honest about being
        # asymmetric and wrong about which direction, on the argument that Phase 3's exit
        # gate would force somebody to look at it. It did.
        #
        # M8 is what looking at it produced, and closing the entry meant deleting it rather
        # than remapping it to M8: the ledger's own gate refuses a re-map, on the ground
        # that a method is the plan that closes a gap and swapping one is a re-plan rather
        # than a correction. A capability with both halves needs no schedule at all, so the
        # clean resolution is the entry's absence, here and in `_LEDGER_AS_SHIPPED`.
        #
        # What it actually took was the missing confidence formula, which is the part the
        # old note named and could not supply: `book_snapshot_slice` in
        # `core/schema/provenance.py`, measuring how much of the requested ladder the store
        # held and how much of the caller's staleness tolerance the answer used up. `native`
        # would have claimed the venue published this ladder and `book_resample` measures a
        # boundary lookahead a slice has none of — both true when it was written, and
        # neither an argument for inventing a number.
        #
        # M3 closed the last three — `markets`, `universe` and `census` — and with them
        # this module's ledger. All three answer out of
        # `crocodile.equity.reference.universe`, which merges SEC EDGAR, Tiingo and
        # OpenFIGI through `CoverageResolver`, because no equity venue ships a connector
        # here and the only thing that knows NASDAQ exists is the reference data. So the
        # equity halves reach the network where the crypto halves read the build, and the
        # two are symmetric in what they answer rather than in what they cost.
        #
        # The update below therefore adds nothing. It is kept, with its notes, because
        # this block is the record of what this batch owed and how each debt was paid;
        # deleting it would leave the ledger's own gates asserting over a silence.
    }
)
