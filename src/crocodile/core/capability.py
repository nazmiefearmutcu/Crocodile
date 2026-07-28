"""One declaration per capability, and the registry the symmetry gate reads.

Crypcodile expressed a single capability list three times: 5 499 lines of Typer CLI,
3 273 of FastAPI routes and 2 600 of MCP tool handlers — roughly 11 400 lines saying
one thing. Adding a second asset class to that shape means six monoliths and a promise
of full API symmetry that nobody can keep by hand. So a capability is declared once,
here, and Phase 2 makes the three surfaces projections of this registry rather than
three hand-maintained copies of it.

The promise being mechanised is: every capability available for one asset class is
available for the other under the same name and parameter schema, and always returns
data — where no free native equity source exists, a derived or synthetic method supplies
it while saying so. :data:`REGISTRY` is what makes the first half checkable and
:attr:`Impl.basis` is what makes the second half machine-readable.

This module holds the *machinery* and no declarations. The declarations live in
:mod:`crocodile.capabilities`, one batch module per family, because four agents fill that
package in parallel and a single file would serialise them. Keeping the two apart also
keeps this module free of every analytics import a declaration drags in, which is what
lets :class:`CapabilityContext` defer ``Catalog`` to ``TYPE_CHECKING``.

The registry ships with real capabilities rather than empty on purpose: a symmetry gate
over an empty registry is vacuously green, which is the same as not having one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

import msgspec

from crocodile.core.schema.enums import AssetClass
from crocodile.core.schema.provenance import Provenance

if TYPE_CHECKING:  # pragma: no cover - annotations only
    import polars as pl

    from crocodile.core.config import Settings
    from crocodile.core.store.catalog import Catalog

__all__ = [
    "IRREDUCIBLE",
    "PENDING_SYMMETRY",
    "REGISTRY",
    "SHARED_IMPLEMENTATION",
    "SPEC_METHODS",
    "AssetClass",
    "Capability",
    "CapabilityContext",
    "CapabilityFn",
    "Fallback",
    "Impl",
    "ReturnKind",
    "declare",
    "register",
    "run_to_completion",
]


class ReturnKind(StrEnum):
    """The shape a capability returns, which is what a surface needs to render it.

    A CLI prints a ``TABLE`` as rows and a ``SCALAR`` as one line; REST pages the first
    and not the second; MCP has to decide whether the result is a content block or a
    subscription. Encoding it here means each surface derives that once instead of
    carrying its own per-endpoint knowledge of the answer.
    """

    TABLE = "table"
    """Zero or more rows. The common case; an empty result is still a table."""

    SCALAR = "scalar"
    """A single value or object, not a row set of length one."""

    STREAM = "stream"
    """An unbounded sequence a caller subscribes to rather than requests."""


@dataclass(frozen=True, slots=True)
class CapabilityContext:
    """Everything a surface supplies to an implementation that a user cannot.

    :attr:`Capability.params` describes what a *user* asks for. Everything else — which
    lake to read, which credentials are configured, which market the request resolved to,
    and how far a given surface is trusted — is the surface's to provide, and this is the
    one object it provides it in.

    Why not a :class:`msgspec.Struct`, which is the idiom of every other declaration in
    this file: a Struct is this codebase's *wire* type. The defining operation of that
    family is ``msgspec.json.encode``, Gate 2 already walks the registry calling
    ``msgspec.json.schema`` over struct types, and a context is the one object that must
    never cross a wire — it holds a live DuckDB connection and a :class:`Settings` whose
    ``__repr__`` was written specifically to redact the credentials msgspec would encode
    in the clear. ``Catalog`` is not encodable at all, so a Struct here would also mean a
    ``TypeError`` raised deep inside whichever surface first tried. A frozen dataclass is
    outside that family by construction.

    ``Catalog``, ``Settings`` and ``polars`` are imported under ``TYPE_CHECKING``. There
    is no import cycle today — checked: nothing under ``core.store`` or ``core.config``
    imports this module, and ``crocodile/__init__`` imports this module before either of
    them. They are deferred because this module is what every capability batch and all
    three surfaces import for their *declarations*, and a runtime import of ``Catalog``
    would make ``import crocodile.core.capability`` cost the whole DuckDB/Polars store
    layer for callers that only wanted to read :data:`REGISTRY`.
    """

    catalog: Catalog
    """The lake this invocation reads. Surfaces own its lifetime; implementations do not
    close it."""

    settings: Settings
    """The resolved environment. An implementation that needs a key asks this, never
    ``os.environ`` — that is the sixteen-scattered-reads problem
    :mod:`crocodile.core.config` exists to end."""

    asset_class: AssetClass
    """Which of :attr:`Capability.impls` was selected. Present so an implementation shared
    by both — ``apply_indicators`` is one — can tell which market it is serving without
    re-deriving it from the symbol."""

    readonly: bool = False
    """Whether raw SQL from this surface must pass :func:`assert_readonly_sql`.

    A property of the *surface*, never of a parameter: a local CLI and a public network
    endpoint do not deserve the same trust, and the same ``query`` capability shipped
    three different policies across the six legacy stacks precisely because each surface
    decided for itself at the call site. Read it through :meth:`query`, which is what
    makes the policy unforgettable rather than advisory.
    """

    row_limit: int | None = None
    """Cap on rows a raw-SQL read may return, or ``None`` for no cap.

    The other half of the legacy REST policy, and it belongs beside ``readonly`` for the
    same reason. Surfaces that set it are expected to publish it, so a truncated answer is
    a stated ceiling rather than a short result the caller reads as the whole lake.
    """

    def query(self, sql: str) -> pl.DataFrame:
        """Run ``sql`` against the lake under *this surface's* policy.

        The only way an implementation should reach :meth:`Catalog.query`. Calling the
        catalog directly compiles and runs, and silently ignores both fields above — which
        is exactly how the crypto CLI ended up with no SQL guard while REST and MCP each
        grew their own.
        """
        from crocodile.core.store.catalog import assert_readonly_sql

        if self.readonly:
            assert_readonly_sql(sql)
        if self.row_limit is not None:
            stripped = sql.strip().rstrip(";").strip()
            # Wrapping rather than truncating the frame: the point of the cap on a
            # network surface is to not materialise the rows in the first place.
            #
            # The newlines are load-bearing and are not formatting. SQL's ``--`` comment runs
            # to the end of the *line*, so folding the statement onto one line put everything
            # after it — the closing parenthesis, the alias, the LIMIT — inside the comment.
            # ``SELECT count(*) FROM trade -- how many`` therefore succeeded on the CLI, which
            # sets no cap and does not wrap, and answered ``400 Parser Error: syntax error at
            # end of input`` on REST and MCP: the caller blamed for SQL this surface broke,
            # on the one capability whose text is entirely theirs. A trailing block comment
            # (``/* … */``) never had the problem, which is what made it look like a parser
            # quirk rather than a wrapper bug.
            sql = f"SELECT * FROM (\n{stripped}\n) AS _q LIMIT {int(self.row_limit)}"
        return self.catalog.query(sql, readonly=self.readonly)


CapabilityFn = Callable[["CapabilityContext", Any], Any]
"""The one calling convention: ``fn(ctx, params)``.

Three shapes were on the table and two of them cannot work.

*Call the underlying function with* ``**params``. No single ``params`` struct satisfies
the bodies that exist: ``apply_indicators(df, indicator, period)`` wants *data*,
``iv_surface(catalog, underlying, at_ns, …)`` wants a *dependency*, and neither of those
is something a user supplies. Widening ``params`` until it covers them would put the lake
handle in the schema the surfaces publish.

*Let each implementation declare the dependencies it wants and inject them by name.* This
works right up until a parameter is renamed, and then it fails **silently**: the projector
introspects signatures, so a drifted name degrades into a ``TypeError`` at call time, on
whichever of the three surfaces happens to call it first, and only for the asset class
whose implementation drifted. Divergence hiding under a shared name is the failure this
entire merge exists to end.

So: two positional parameters, the same two for every implementation, checked by a gate
rather than by convention. ``params`` is typed ``Any`` because each implementation narrows
it to its own struct, and a per-capability generic buys nothing a declaration-site
annotation does not already give.
"""


class Fallback(msgspec.Struct, frozen=True):
    """What an implementation degrades to when the deployment cannot reach its ceiling.

    :attr:`Impl.prov` is a ceiling and declaring the best branch is right. The consequence,
    left alone, is that a deployment which cannot take that branch announces the branch it
    did not take: ``depth``, ``slippage`` and ``liquidity-depth`` all declare
    ``DERIVED``/``alpaca_l1`` over ``select_depth_source``, which returns the **synthetic**
    Yahoo ladder whenever the Alpaca keys are unset. ``banner_for`` suppresses ``DERIVED``,
    so the default keyless deployment printed nothing on stderr, and REST and MCP named a
    method that never ran — while the records themselves came back stamped
    ``SYNTHETIC``/``yahoo_1m_vap``.

    Declaring the floor beside the ceiling is what lets a projection announce the branch that
    will actually answer without any surface knowing what a credential is. It is deliberately
    *not* a second ``prov``: the ceiling still says what the implementation can do at its
    best, and this says what it does when the deployment cannot let it.

    The alternative was to demote the three declarations to ``SYNTHETIC``/``yahoo_1m_vap``.
    That trades one false statement for another — a keyed deployment would then under-claim a
    real quoted ladder as modelled, and ``prov`` would stop meaning "ceiling" for three
    implementations and keep meaning it for the other eighty-eight.
    """

    prov: Provenance
    """The level this implementation actually reaches when :attr:`reachable` is false."""

    basis: str
    """The registered method that actually runs then. A key into the provenance registry,
    exactly as :attr:`Impl.basis` is, and held to the same rule: a basis with no registered
    confidence formula is a confidence number chosen by feel."""

    reachable: Callable[[Settings], bool]
    """Whether *this deployment* can take the ceiling's branch.

    A predicate over :class:`~crocodile.core.config.Settings` rather than a list of
    environment variable names, because the question is the implementation's and only the
    implementation knows how it is answered — ``select_depth_source`` accepts two spellings
    of the Alpaca keys and a surface must not learn either. It is expected to be the *same
    function the implementation calls*, which is what stops the announcement and the branch
    from being two readings of one fact.
    """


class Impl(msgspec.Struct, frozen=True):
    """How one asset class satisfies a capability."""

    fn: CapabilityFn
    """The callable that does the work, as ``fn(ctx, params)``. See :data:`CapabilityFn`.

    An existing analytics function is almost never this shape, and should not be bent into
    it — ``apply_indicators`` takes a frame because a frame is what it computes over. The
    repeatable move is a small adapter beside the declaration that turns the context and
    the params into that function's real arguments.
    """

    prov: Provenance
    """The best level this implementation can produce.

    A ceiling, not a per-call measurement: an implementation that models depth from bars
    is :attr:`Provenance.SYNTHETIC` on its best day. The per-record value comes from
    ``provenance_fields()``, never from a number written at a call site.
    """

    basis: str
    """The registered method this implementation's inputs rest on.

    A key into the provenance registry in :mod:`crocodile.core.schema.provenance`, which
    is what Gate 3 checks: a basis with no registered confidence formula is a confidence
    number chosen by feel waiting to happen. Note this names the *inputs* — ``indicators``
    declares ``native`` because both asset classes report OHLCV natively — while
    :attr:`prov` describes what the implementation hands back.
    """

    reads: tuple[str, ...] = ()
    """The lake channels this implementation reads, for the asset class it is declared under.

    The missing half of :attr:`basis`. A basis names the *method* the answer rests on and is
    checked against a registry of confidence formulas; nothing checked that the data the
    method needs could ever be in the lake. Eleven equity implementations read
    ``options_chain``, ``macro_series`` or ``insider``, no shipped equity ingest path could
    write any of them, and every gate stayed green: ``iv-surface``, ``open-interest``,
    ``perp-basis`` and ``whale-alerts`` returned zero rows on every lake this product can
    build, while ``spot-future-basis`` returned a row whose ``carry_pct`` was null under a
    ``prov_confidence`` of 0.667. ``tests/conformance/test_channel_writability.py`` is the
    gate this field exists for, and it fails when a name here is not a channel some
    registered provider for that asset class declares it can write.

    **Why a declaration and not a scan.** The alternative considered was deriving the
    read-set mechanically, by walking the SQL each implementation's module authors. It is
    the more attractive option — a derived fact cannot go stale — and it does not work at
    the precision this needs. ``spot-future-basis``'s equity implementation reads
    ``macro_series`` through two modules it does not name (``equity.analytics.carry`` calls
    into ``core.analytics.carry``); ``price_leg`` scans a channel held in a loop variable
    over a module constant, so the table name is not a literal at the call site at all; and
    ``core.analytics.volsurface`` reads ``options_chain`` on behalf of *both* asset classes
    from one module, which no per-module scan can attribute to one of them. A scan would go
    blind in exactly the places the indirection lives, which is where the defect lived. So
    the read-set is claimed by the author, and the *mechanical* half of the gate is turned
    on the sources instead: what a provider can write is derived from its own declaration,
    and a coarse module sweep backs the claim up where a scan can see (see that test's
    docstring for the division of labour).

    Empty means "reads no lake channel", which is true of ``collect``, ``backfill`` and of
    implementations that compute from their parameters — not "not declared". A capability
    that reads the lake and declares nothing is caught by the module sweep, not by this
    field.
    """

    fallback: Fallback | None = None
    """What this implementation degrades to where the ceiling is out of reach, if it does.

    ``None`` for the eighty-eight implementations whose answer does not depend on how the
    deployment is configured. See :class:`Fallback` for why the three that do need one.
    """


class Capability(msgspec.Struct, frozen=True):
    """One capability, declared once for both asset classes."""

    name: str
    """The wire name. The same string names the CLI command, the REST path segment and
    the MCP tool, so the three surfaces cannot drift apart on spelling."""

    summary: str
    """One line, reused as CLI help, OpenAPI summary and MCP tool description."""

    params: type[msgspec.Struct]
    """The parameter schema, shared by every asset class implementing this capability.

    One struct, not one per asset class: identical parameter schemas are half of what
    "full API symmetry" means, and two structs would let them diverge silently. Phase 2
    projects this to an MCP ``inputSchema`` via ``msgspec.json.schema``, to Typer options
    and to FastAPI query parameters.
    """

    returns: ReturnKind
    """See :class:`ReturnKind`."""

    impls: dict[AssetClass, Impl]
    """One entry per asset class this capability serves.

    Missing an asset class is a build failure unless the name appears in
    :data:`IRREDUCIBLE`.
    """

    cross_market: bool = False
    """Whether the answer is the same whichever implementation serves it.

    Thirteen capabilities read the lake *as a lake* rather than as a market: ``search``,
    ``query``, ``catalog-symbols``, ``catalog-exchanges`` and the rest of
    :mod:`crocodile.capabilities.catalog`, whose module never mentions ``ctx.asset_class``
    because there is nothing for it to mean. ``search?q=EQ&asset_class=crypto`` answered 200
    with three ``stooq:`` equity symbols and stamped ``provenance.asset_class: "crypto"`` —
    a value that selected nothing, echoed back as though it had.

    The envelope reads this and says ``"any"`` instead. That is the whole of what the flag
    does today: it does not change which implementation runs, because for these it cannot
    matter, and it deliberately does not try to make the parameter optional — that lives in
    ``surfaces.dispatch.resolve_asset_class``, which refuses when it cannot tell, and
    relaxing it there is a separate decision about a function three surfaces share.

    Declared rather than sniffed, and then *checked*: a conforming capability has one
    implementation function and one ``(prov, basis)`` across its asset classes, which is
    mechanically true of an answer that does not depend on the market, and the surfaces drive
    both classes and compare the rows. Sniffing the same condition would silently catch
    capabilities that share a function *and* read ``ctx.asset_class`` inside it —
    ``apply_indicators`` is one, and :class:`CapabilityContext` documents why it does.
    """

    aliases: tuple[str, ...] = ()
    """Retired spellings that must keep resolving to :attr:`name`.

    A capability has exactly one name, because the three surfaces derive their command,
    route and tool from it and a second name is a second thing to keep in step. But the
    forks shipped some capabilities under two, and a caller wired to the losing spelling
    is a caller the rename breaks. Listing it here is how the projection can keep
    answering on it while there is still only one declaration behind it.

    This is not a place to add a synonym somebody prefers. Every entry is a name that was
    already on the wire, and the ledger only shrinks — an alias leaves when the surfaces
    stop honouring it, which is a deprecation with its own decision.
    """


REGISTRY: Final[dict[str, Capability]] = {}
"""Every declared capability, keyed by name.

Populated when a batch module is imported, so it reflects only what has been imported so
far. Anything that treats it as the complete list — a surface projection, a symmetry gate
— calls :func:`crocodile.capabilities.load_all` first, on exactly the discipline
:func:`crocodile.core.schema.provenance.registered_bases` documents for bases. Importing
the world from here instead would make reading a dict pull in every analytics dependency
in the tree.
"""


IRREDUCIBLE: Final[dict[str, str]] = {
    # `gas-tracker` was here and is not a capability at all. It takes no parameters,
    # returns nothing, observes nothing, and belongs to no asset class: it opened a Qt
    # window and entered an event loop, so it could not reach REST or MCP even in
    # principle. Its justification was an argument about gas *data*, which `gas-vol`
    # already carries. The evidence was `migrate-lake`, which is the same kind of thing —
    # infrastructure declined on purpose, argued for in `capabilities.ops.UNDECLARED` —
    # and was never on this list. An exemption that covers a non-capability makes the list
    # mean two things: "no equity analogue can exist" and "not a capability at all". Only
    # the first is checkable against a market.
    #
    # The original evidence was `flowmap`, a Qt launcher of exactly the same shape that
    # also never appeared here. Both commands have since been deleted along with the rest
    # of the desktop layer, so the pair no longer exists to point at — the argument is
    # unchanged and the surviving example is `migrate-lake`.
    "gas-vol": "Correlates price volatility with gas prices; gas is chain-native.",
    "mev-sandwich": "Requires a public mempool and atomic transaction ordering.",
    "sequencer-latency": "Measures an L2 sequencer; equities have no sequencer.",
    "peg-deviation": "Stablecoin peg mechanics; no equity instrument behaves this way.",
    "lending-stress": "On-chain lending-pool utilisation and liquidation thresholds.",
    # The two below arrived at Phase 2's exit rather than from the port. The surface-parity
    # gate found three wire names — `get_onchain_price` and `get_base_market_data` on both
    # forks' MCP servers, and equity's `GET /api/v1/market-data` — that the 47 ported
    # capabilities did not serve, so they were declared rather than exempted; see
    # `crocodile.capabilities.onchain`.
    "onchain-price": "An AMM pool's price is a function of two pooled reserves; no equity "
    "instrument is priced that way, and there is no chain to read one off.",
    "base-market-data": "The same pool, with its swap volume. Same argument: the volume is "
    "the pool's own swap log, which has no equity analogue either.",
}
"""The only way a capability escapes the symmetry gate, and the bar for getting on it.

The value is the *argument* for why no equity analogue can exist — a property of the
market, not of the schedule. "Not built yet" is not a valid reason and neither is "no
free data source": the product promise is that a derived or synthetic method supplies
the data while saying so, so an absent source is a reason to declare a
:attr:`Provenance.SYNTHETIC` implementation, not to claim irreducibility. Adding a name
here silences a build failure, which is exactly why the justification is mandatory and
exactly why an empty one is itself a build failure.

Being permanent is what makes a *stale* entry here worse than a stale one on
:data:`PENDING_SYMMETRY`, and until an exit review looked, only the latter had a gate
saying so. Two rules in ``tests/conformance/test_pending_symmetry.py`` now apply to both
lists: an entry must name a registered capability, and a capability implemented for both
asset classes has disproved its own entry and the entry has to go. Without the second, an
equity half could land and then be deleted again with nothing raising, because the name was
excused forever.
"""


SHARED_IMPLEMENTATION: Final[dict[str, str]] = {
    # One lake. These read `ctx.catalog`, which holds both asset classes in one set of
    # partitions, so the asset class is a *filter* inside one query rather than a different
    # store to open. A second copy of any of them would be a second copy of the same SQL.
    "catalog": "Describes the lake's own tables; there is one lake.",
    "catalog-channels": "Lists the channels the lake partitions by; there is one lake.",
    "catalog-dates": "Lists the dates the lake holds; there is one lake.",
    "catalog-exchanges": "Lists the venues the lake holds rows for; there is one lake.",
    "catalog-inventory": "Counts rows per partition of one lake.",
    "catalog-scan": "Walks one lake's partition tree.",
    "catalog-stats": "Aggregates one lake's row counts and byte sizes.",
    "catalog-summary": "Summarises one lake's contents.",
    "catalog-symbols": "Lists the symbols one lake holds rows for.",
    "data-coverage": "Reports which dates one lake holds for a symbol.",
    "export": "Writes rows out of one lake in a requested file format.",
    "query": "Runs the caller's SQL against one lake.",
    "replay": "Re-emits stored rows from one lake in timestamp order.",
    "resample": "Re-buckets stored rows of one lake into a coarser interval.",
    "resolve-symbols": "Maps a symbol spelling onto the rows one lake stores it under.",
    "search": "Matches a needle against the symbols one lake holds.",
    "indicators": "Arithmetic over OHLCV columns, which both asset classes store in the "
    "same channel of the same lake with the same column names; the indicator does not "
    "know or need to know which market produced the bars.",
    # Reads no lake at all. `params` carries the whole input, so there is no store to
    # differ about — the same argument as `caller_supplied`'s in the provenance registry.
    "funding-predict": "A projection over rates the caller supplies in `params`; it opens "
    "no store, so there is nothing for a second implementation to read differently.",
}
"""Capabilities where one function legitimately serves both asset classes, and why.

The defect this exists to make visible has shipped in this codebase before. ``slippage``
declared an equity half that was the crypto function bound twice: the declaration advertised
a ``yahoo_1m_vap`` ladder while the bound function read ``book_snapshot``, which no equity
provider writes, so every equity call raised and the symmetry gate reported a symmetric
capability. ``set(cap.impls) == {CRYPTO, EQUITY}`` — which is all Gate 2 asks — is satisfied
by two dict keys pointing at one callable, and a referee re-introduced exactly that against
``census`` and passed the entire 3 300-test suite.

So the gate asks for ``fn`` distinctness, and this is what tells it when sharing is the
answer rather than the bug. It is a declaration and not a heuristic on purpose: the two
states are indistinguishable from the outside. ``basis`` was the obvious guess and is wrong —
``iv-surface`` declares ``native`` on both sides, so does ``ofi``, so does ``indicators``,
and a gate keyed on "the two bases differ" would wave a rebind of the first two straight
through while flagging the third. Whether one implementation can serve two markets is a
claim about the capability, and a claim has to be made somewhere a reviewer reads.

Two arguments qualify, and both are about there being **one thing to read**:

- *One lake.* The ``catalog-*`` family and its neighbours answer about storage, not about a
  market. Both asset classes live in one set of partitions, so the asset class narrows a
  query rather than choosing a store, and :attr:`CapabilityContext.asset_class` is there for
  exactly that narrowing.
- *No lake.* ``funding-predict`` takes its inputs from ``params`` and opens nothing, which is
  the same argument :func:`crocodile.core.schema.provenance` registers as ``caller_supplied``.

What does **not** qualify is "the crypto one happens to run": that is the ``slippage``
failure, and it is the one this list must never be used to excuse. A capability that reads
market data has two markets' data to read, and one function cannot read both unless it was
written to — in which case it belongs here with that written down.

Membership is censused in ``tests/conformance/test_pending_symmetry.py`` for the reason every
ledger here is: an exemption list nobody counts is a place to put things. That census is not
an afterthought — an earlier review added a gate together with the exemption the gate itself
suggested, and the exemption is what the next deletion was laundered through.
"""


SPEC_METHODS: Final[dict[str, str]] = {
    "M1": "Lift volsurface into core; equity chain from Yahoo, IV solved from mid if absent.",
    "M2": "Aggregate the Yahoo option chain's open_interest per underlying.",
    "M3": "Equity universe from SEC EDGAR x OpenFIGI x Tiingo, merged by CoverageResolver.",
    "M4": "Form 4 insider transactions plus a new SEC EDGAR 13F-HR parser.",
    "M5": "carry generalizes funding-apr; new keyless `treasury` provider for the risk-free leg.",
    "M6": "Equity depth from the synthetic VAP ladder, upgraded by Alpaca L1 when keyed.",
    "M7": "Order-flow imbalance derived from L1 quote changes.",
    "M8": "Crypto depth from stored book snapshots — the ladder equity models, crypto reports.",
}
"""The methods that close a declared gap, by their spec ids.

Here so that :data:`PENDING_SYMMETRY` can only point at a plan that was actually written
down. A deadline that names a method nobody specified is a deadline nobody owns.

M1 to M7 are design §9.1's, and they share an assumption the design never states: that the
gap always runs equity-ward. Porting the surfaces found the counterexample. ``depth``
existed only for equities — M6 *is* its equity half, already built — so the half it was
missing was the **crypto** one, and the ledger had no vocabulary for that direction.
M8 is that vocabulary. It was not a new promise so much as an admission: crypto emits
``BookSnapshot`` records natively, so the ladder equities have to model was one crypto
already reported, and the only reason it was absent is that nobody had written it.

M8's confidence formula did not exist when M8 was written, and that was the whole of the
difficulty: a ladder sliced out of a stored venue book is neither ``native`` — the venue
published a book, at its own instant and to its own depth, not this ladder — nor
``book_resample``, which is a declared constant precisely because a resampler picks its own
boundary and so has no staleness and no unfillable request to score. The method named the
plan and the formula arrived with the implementation, which is the order the registry
requires. It is ``book_snapshot_slice`` in :mod:`crocodile.core.schema.provenance` — how
much of the requested ladder the store actually held, times how much of the caller's
declared staleness tolerance the answer used up — and with it ``depth`` became symmetric and
left :data:`PENDING_SYMMETRY` entirely rather than being remapped from M6 to M8. The ledger
forbids that remap on purpose: a method is the plan that closes a gap, so swapping one is a
re-plan rather than a correction, and a capability with both halves needs no schedule at
all.
"""


PENDING_SYMMETRY: Final[dict[str, str]] = {}
"""Capabilities that are asymmetric *on schedule*, mapped to the method that closes them.

Phase 2 put 49 capabilities into :data:`REGISTRY` — 47 ported off the legacy surfaces by
the four batch modules, plus the two the parity gate found at its exit — with their crypto
halves working and their equity halves Phase 3 work. That leaves the symmetry gate three
possible futures and two of them are lies:

- **Do not register them until both halves exist.** The registry stops describing the
  product, and the surfaces cannot be projections of it, which is the whole of Phase 2.
- **Put them on** :data:`IRREDUCIBLE`. That mapping means *no equity analogue can exist* —
  a claim about the market. Using it for "not built yet" is exactly the scheduling excuse
  its justifications are tested against, and once a name is there nothing ever makes it
  leave.
- **Say so, with a deadline.** This.

The rules, each enforced by a gate in ``tests/conformance/test_pending_symmetry.py``:

1. every value names a method in :data:`SPEC_METHODS`;
2. a name may not be here and on :data:`IRREDUCIBLE` — those are opposite claims;
3. every name here must be registered and must actually be asymmetric, so the ledger
   cannot quietly hold names that no longer need it;
4. it must be **empty** at Phase 3's exit, which is what makes it a schedule rather than a
   second exemption list.

**The ``{}`` above is a declaration, not the runtime value, and no sentence here states
what the runtime value is.** :mod:`crocodile.capabilities.analytics`,
:mod:`~crocodile.capabilities.market` and :mod:`~crocodile.capabilities.ops` each call
``update()`` on this dict at import time, because four batches editing one dict in this
shared module is four merge conflicts in one file. So the declaration site shows ``{}``
whatever the ledger actually holds, and a reader who takes it at face value is wrong by
exactly the number of entries the batches added.

That gap has now produced the same defect twice, in opposite directions. "Empty today"
survived here for a whole phase while the batches filled the ledger to 21 entries. The
correction stated that count in the present tense — and then outlived the entries: Phase 3
discharged M1 through M8, every one of them left, and a paragraph written to stop a stale
count had become one. A sentence naming a number this module cannot see will go stale on
whichever side the ledger next moves, so this one names none.

What checks the number instead: ``_LEDGER_AS_SHIPPED`` in
``tests/conformance/test_pending_symmetry.py`` pins the contents entry by entry, and
``test_phase_3_exit_the_ledger_must_be_empty`` asserts the exit criterion against the live
dict. Between them the count is measured on every run.

The hazard this paragraph exists for does *not* go away while the ledger is empty — it is
dormant, which is worse, because the next reader sees ``{}`` and a docstring that agrees
with it. The moment a batch module schedules a capability again, this line still reads
``{}``, and the only things that will notice are the two tests above.
"""


def run_to_completion[T](make: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Drive ``make()``'s coroutine to a value, from a caller that may or may not have a loop.

    :data:`CapabilityFn` is synchronous and some implementations are not, so this is the one
    bridge between the two — and it lives here, in the machinery, because writing it twice is
    how the second copy came to be wrong. The first copy is
    ``crocodile.capabilities.market``'s; the second was ``capabilities.onchain``'s ``_run``,
    a bare ``asyncio.run`` under a comment asserting that "both network surfaces call
    ``dispatch.invoke`` from a worker thread rather than from the event loop". Neither does.
    ``onchain-price`` worked on the CLI, returned 500 on REST and raised ``RuntimeError:
    asyncio.run() cannot be called from a running event loop`` on MCP — two surfaces out of
    three, from one line, for a whole phase.

    The two callers this has to serve are the reason it is not a bare ``asyncio.run``. A CLI
    projection has no running loop, so ``asyncio.run`` is exactly right. A REST or MCP
    projection *is* a running loop, and ``asyncio.run`` raises ``RuntimeError`` there —
    which makes the same declaration work on one surface and fail on another, the asymmetry
    this registry exists to end.

    So a running loop is detected and the coroutine gets its own loop on a worker thread.
    That blocks the calling loop for the duration, which is a real cost and is stated rather
    than hidden: an async surface that does not want to pay it calls the adapter through
    ``asyncio.to_thread``, which puts it on a thread with no loop and takes the first
    branch. It cannot deadlock — every coroutine reaching this builds its own
    ``aiohttp``/``ccxt``/``web3`` session inside the new loop and awaits nothing belonging to
    the outer one.

    ``make`` is a factory rather than a coroutine so the object is created on the thread that
    will await it, and so a failure before submission cannot leave a "coroutine was never
    awaited" warning behind instead of an error.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(make())

    def _in_its_own_loop() -> T:
        return asyncio.run(make())

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_in_its_own_loop).result()


def register(cap: Capability) -> Capability:
    """Add ``cap`` to :data:`REGISTRY`.

    Returns the capability so a declaration can be bound to a name at the call site.

    Raises:
        ValueError: if the name is already taken. Two modules claiming one name is how
            the three surfaces would end up projecting different things under the same
            command, which is the failure this registry exists to prevent.
    """
    if cap.name in REGISTRY:
        raise ValueError(f"capability {cap.name!r} is already registered")
    REGISTRY[cap.name] = cap
    return cap


_DECLARED_NAMES: Final[set[str]] = set()


def declare(cap: Capability) -> Capability:
    """Register ``cap`` from a batch module, tolerating that module's body running twice.

    This is what the four batch modules in :mod:`crocodile.capabilities` call.
    :func:`register` is strict, and it has to be. But a batch module registers at import
    time, and ``load_all_bases()`` walks the whole package catching ``Exception`` into a
    ``RuntimeWarning`` — so a ``ValueError`` raised from a re-executed module body would
    not fail loudly, it would degrade into a registry quietly missing whatever came after
    it. Re-declaring a name the *same* declaring path already owns therefore replaces it
    instead of raising, while a *different* module claiming an existing name still goes
    through :func:`register` and still fails hard. That distinction is the whole point: an
    idempotent re-import is a mechanical fact, two modules claiming one name is a bug.
    """
    if cap.name in _DECLARED_NAMES:
        REGISTRY[cap.name] = cap
        return cap
    _DECLARED_NAMES.add(cap.name)
    return register(cap)
