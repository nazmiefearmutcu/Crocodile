"""Capabilities computed from stored records.

Owns ``basis``, ``funding-apr``, ``iv-surface``, ``vol-skew``, ``term-structure``,
``risk-reversal``, ``slippage``, ``ofi``, ``indicators``, ``liquidity-depth`` and the rest
of the analytics family — anything whose inputs are rows already in the lake.

Every declaration here shows the same adapter pattern: the analytics function keeps the
signature its own domain wants, and a module-level function named after the capability
turns ``(ctx, params)`` into that signature's arguments. Do not reshape an analytics
function to fit the registry — ``apply_indicators`` takes a frame because a frame is what
it computes over, and a version of it that took a
:class:`~crocodile.core.capability.CapabilityContext` would be a worse function that is
also harder to test.

The adapters are module-level and named, not lambdas or partials, so that a stack trace and
the calling-convention gate both point at something with a file and a line number. Six
analytics functions already carry the name their capability needs — ``perp_basis``,
``spot_future_basis``, ``funding_apr``, ``iv_surface``, ``term_structure``, ``vol_skew`` —
so those are imported under a ``_``-prefixed alias and the bare name belongs to the adapter.
The rule is one-directional: the adapter is what the registry sees, so the adapter gets the
capability's spelling.

One params struct per capability, never one shared by two. Identical schemas are what
:attr:`Capability.params` asks of the two *asset classes*; sharing a struct across two
capabilities would additionally couple their schedules, so that a field Phase 3 needs on
``term-structure`` alone could only be added by splitting a type two declarations point at.
``IvSurfaceParams`` and ``TermStructureParams`` are field-for-field identical today and
still separate for that reason.

Where the three legacy surfaces disagreed about a parameter, the struct's docstring records
which surface was followed and why. Those disagreements are the substance of the port: a
default is a decision six files made six times, and collapsing them into one declaration
means making it once, in the open.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

import msgspec
import polars as pl

from crocodile.core.analytics.indicators import apply_indicators
from crocodile.core.analytics.slippage import estimate_slippage, slippage_over_levels
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
from crocodile.core.resample.ohlcv import resample_ohlcv
from crocodile.core.schema.provenance import Provenance
from crocodile.crypto.analytics.basis import (
    perp_basis as _perp_basis,
)
from crocodile.crypto.analytics.basis import (
    spot_future_basis as _spot_future_basis,
)
from crocodile.crypto.analytics.basis import (
    spot_perp_basis,
)
from crocodile.crypto.analytics.funding import funding_apr as _funding_apr
from crocodile.crypto.analytics.funding_prediction import predict_next_funding
from crocodile.crypto.analytics.liquidity_depth import calculate_block_liquidity_depth
from crocodile.crypto.analytics.ofi import calculate_ofi
from crocodile.crypto.analytics.risk import calculate_chaos_score
from crocodile.crypto.analytics.smart_money import normalize_watchlist, summarize_smart_money
from crocodile.crypto.analytics.volsurface import (
    iv_surface as _iv_surface,
)
from crocodile.crypto.analytics.volsurface import (
    risk_reversal_butterfly,
)
from crocodile.crypto.analytics.volsurface import (
    term_structure as _term_structure,
)
from crocodile.crypto.analytics.volsurface import (
    vol_skew as _vol_skew,
)
from crocodile.crypto.analytics.whale import track_whale_alerts
from crocodile.equity.analytics.carry import (
    equity_basis,
    equity_forward_basis,
    equity_funding_apr,
    equity_spot_future_carry,
)
from crocodile.equity.analytics.chaos import (
    chaos_score_equities as _chaos_score_equities,
)
from crocodile.equity.analytics.filings import (
    filing_flows,
    label_filing_parties,
    resolve_watched_filers,
    track_filing_whales,
)
from crocodile.equity.analytics.liquidity_depth import liquidity_depth_from_profile
from crocodile.equity.analytics.ofi import calculate_quote_ofi
from crocodile.equity.analytics.volsurface import (
    iv_surface as _equity_iv_surface,
)
from crocodile.equity.analytics.volsurface import (
    term_structure as _equity_term_structure,
)
from crocodile.equity.analytics.volsurface import (
    vol_skew as _equity_vol_skew,
)
from crocodile.equity.depth import alpaca_is_keyed, select_depth_source

__all__ = [
    "BASIS",
    "CHAOS_SCORE",
    "FUNDING_APR",
    "FUNDING_PREDICT",
    "INDICATORS",
    "IV_SURFACE",
    "LABEL_TRANSFERS",
    "LIQUIDITY_DEPTH",
    "OFI",
    "PERP_BASIS",
    "RISK_REVERSAL",
    "SLIPPAGE",
    "SMART_MONEY",
    "SPOT_FUTURE_BASIS",
    "TERM_STRUCTURE",
    "VOL_SKEW",
    "WHALE_ALERTS",
    "BasisParams",
    "ChaosScoreParams",
    "FundingAprParams",
    "FundingPredictParams",
    "IndicatorParams",
    "IvSurfaceParams",
    "LabelTransfersParams",
    "LiquidityDepthParams",
    "OfiParams",
    "PerpBasisParams",
    "RiskReversalParams",
    "SlippageParams",
    "SmartMoneyParams",
    "SpotFutureBasisParams",
    "TermStructureParams",
    "VolSkewParams",
    "WhaleAlertsParams",
]


# ---------------------------------------------------------------------------
# Parameter schemas
# ---------------------------------------------------------------------------


class IndicatorParams(msgspec.Struct, frozen=True):
    """Parameters for ``indicators``, identical for both asset classes."""

    symbol: str
    start_ns: int
    end_ns: int
    interval: str = "1d"
    indicator: str | None = None
    period: int = 14
    fill_empty: bool = False
    """Insert a zero-volume bar for every bucket in the range that saw no trades.

    ``False``, which is what the equity CLI's ``--fill-empty`` defaulted to
    (``equity/legacy/cli.py:1641-1647@4a0f84c``, whose help warned it "can explode wide
    date ranges"). The port dropped the flag and hardcoded ``True`` in the adapter, on the
    argument that a 14-period SMA otherwise spans different amounts of wall-clock time on a
    thin symbol than on a busy one. That argument is real and it is not this field's to
    settle: a caller who wants an even time grid asks for one, and a caller who does not
    gets the arithmetic the legacy command gave them.

    What the silent flip changed, measured on a 40-bar equity series with a 60-minute
    session gap: 40 rows became 121, the last bar's RSI moved 49.573 → 55.732, and the
    largest per-bar RSI difference was 36.56. A period is a count of *bars*, so inserting
    bars redefines every window in the result.

    And with no field to ask for ``False``, there was no way back on any of the three
    surfaces — including for the second-order case, where a symbol the lake has never seen
    stopped answering "no data found" and started answering with fabricated bars carrying
    ``num_trades=0``, ``volume=0.0`` and the header's default ``prov_confidence=1.0``.
    """


class SlippageParams(msgspec.Struct, frozen=True):
    """Parameters for ``slippage``, identical for both asset classes.

    ``size_unit`` is the crypto half of a collision: the crypto implementation took
    ``size: float | str`` plus a unit and could walk the book denominated in either asset,
    the equity one took a bare ``float``. One struct has to cover both, so the unit was
    either equity-ignored or crypto-lost, and it was equity-ignored — an optional parameter
    costs a caller that omits it nothing, while dropping it deletes a measured, tested book
    walk. Left unset, the walk is by quantity, which is what sizing in shares means.

    It is no longer equity-*ignored*, because the walk is now one function over whichever
    ladder the asset class brings — see
    :func:`~crocodile.core.analytics.slippage.slippage_over_levels`. Sizing an equity order
    in dollars was the use the original note said the parameter was being kept for; keeping
    it turned out to cost nothing and it now works.
    """

    symbol: str
    side: str
    size: float | str
    size_unit: str | None = None


class BasisParams(msgspec.Struct, frozen=True):
    """Parameters for ``basis`` — the spot leg and the perpetual leg, plus a window.

    Both symbols are required and named rather than positional, which is what settles the
    argument-order flip between the legacy routes. ``GET /basis`` passes ``(spot, perp)``
    (api_server.py:2010) and ``GET /spot-future-basis`` passes ``(future, spot)``
    (:2095), so "the spot leg" is argument one in one route and argument two in the other.
    A struct has no argument two; the adapter binds each field to the parameter that
    means it, and a caller that swapped the two legs on the wire could only do so by
    swapping two labelled fields.

    ``start_ns``/``end_ns`` are required, following ``IndicatorParams`` rather than either
    surface. The CLI resolves an omitted range to the whole lake (cli.py:3197) and REST
    defaults both to ``0`` (:1985), which returns nothing; neither is a default worth
    freezing into the one schema all three surfaces will publish.

    ``perp_symbol`` names *the derivative leg*, and for equities that is a future rather
    than a perpetual. The field keeps the crypto spelling because one capability has one
    parameter schema — that is half of what full API symmetry means, and a second struct
    is what would let the two drift. Renaming it to something market-neutral was the
    alternative and is worse than it looks: the name is on the wire in three surfaces
    already, so a rename is a breaking change to every caller in exchange for a word.
    What the leg is for each asset class is documented rather than encoded.
    """

    spot_symbol: str
    perp_symbol: str
    start_ns: int
    end_ns: int


class PerpBasisParams(msgspec.Struct, frozen=True):
    """Parameters for ``perp-basis`` — one perpetual contract, mark against index.

    The single symbol is ``symbol``, not ``perp_symbol``. Two of three surfaces spell it
    for the leg (``--perp`` at cli.py:3035, ``perp_symbol`` at mcp_server.py:1528) and REST
    spells it ``symbol`` (:2027); the tie is broken by the registry rather than by the
    legacy majority, because every other single-symbol capability here — ``indicators``,
    ``slippage``, ``funding-apr``, ``ofi``, ``whale-alerts``, ``liquidity-depth`` — calls
    its one symbol ``symbol``, and a schema that renames the same field per capability is
    the drift the single registry exists to stop. The leg is already named by the
    capability.

    The single symbol is also the constraint that decided what the equity half computes.
    Crypto gets away with one because ``derivative_ticker`` carries a mark and an index on
    one record; no equity source in this tree writes one, so the equity half reads the
    derivative side off the symbol's own option chain by put-call parity and the reference
    side off its own cash price series. Both are keyed on this one symbol, which is why
    the schema did not have to change — see
    :func:`~crocodile.equity.analytics.carry.equity_forward_basis` for the argument.
    """

    symbol: str
    start_ns: int
    end_ns: int


class SpotFutureBasisParams(msgspec.Struct, frozen=True):
    """Parameters for ``spot-future-basis`` — a dated future against spot.

    ``expiry_ns`` is exposed even though ``GET /spot-future-basis`` never accepted it
    (api_server.py:2067). It is on the CLI as ``--expiry`` (cli.py:3041) and on the MCP
    tool as an optional ``expiry_ns`` (mcp_server.py:1611), and it is not cosmetic: with it
    the result gains an ``annualized_pct`` column, without it that column does not exist.
    Two surfaces pinned a parameter that changes the answer's shape and one forgot it, so
    the REST projection gains it. Absent, the behaviour is the REST route's exactly.
    """

    future_symbol: str
    spot_symbol: str
    start_ns: int
    end_ns: int
    expiry_ns: int | None = None


class FundingAprParams(msgspec.Struct, frozen=True):
    """Parameters for ``funding-apr`` — one perpetual symbol over a window."""

    symbol: str
    start_ns: int
    end_ns: int


class FundingPredictParams(msgspec.Struct, frozen=True):
    """Parameters for ``funding-predict`` — a rate history and a fallback window.

    ``rates`` is a sequence of floats, which is the MCP tool's shape
    (mcp_server.py:1749) and neither of the other two. The CLI takes ``--rates`` as a
    comma-separated string or ``--file`` as a path to a CSV/JSON (cli.py:4823) and REST
    takes ``rates`` as a comma-separated query string (api_server.py:2699); both then parse
    to exactly this list. Comma-splitting is how you put an array in a query string and
    reading a CSV is how you put one in a shell — encodings of the parameter, not the
    parameter — so they stay in the projections and the registry takes the numbers.

    ``window_size`` is 5 on all three (cli.py:4844, api_server.py:2700, mcp_server.py:1757),
    so there is nothing to decide.

    ``target_col`` exists on ``predict_next_funding`` and is exposed by no surface, so it is
    not here; see ``IvSurfaceParams`` for the same rule applied to ``fit_method``.
    """

    rates: tuple[float, ...]
    window_size: int = 5


_RATE_DESCRIPTION: Final = (
    "Annual risk-free rate as a decimal fraction (0.0546 is 5.46 %), CONTINUOUSLY "
    "compounded. Not the same convention as the `risk_free_rate` column that "
    "`spot-future-basis` and `funding-apr` emit: that is a US Treasury par yield, which is "
    "simple/bond-equivalent. Convert with r_cont = ln(1 + r_simple)."
)
"""Help text for the options family's ``rate``, published on all three surfaces.

The generated ``--rate`` shipped with no help at all, and the field is where two live
conventions meet. M1 declares this one **continuous** — ``equity/analytics/options.py``
discounts with ``exp(-r*t)`` and ``crypto/analytics/volsurface.py`` says so — while M5
declares **simple**: ``core/analytics/carry.py``'s header states "Simple, not compounded,
throughout" and ``equity/analytics/carry.py`` discounts with ``1 + r*days/365``. The only
number this engine *publishes* for the rate is M5's, on the wire as ``risk_free_rate``, and
it is the simple one.

There is no percent bug — both sides are decimal fractions. The hazard is entirely that an
operator reads a rate out of one capability and passes it into another, which is a
one-line pipeline and produces a plausible wrong answer: 15 bp of discount factor at one
year, 62 bp at two, and a one-year ATM call priced off a simple 5.46 % and inverted as
continuous solves to 18.5 bp less vol than it was priced at. Nothing on either row would
say so, so the field says it instead — which is why this is a wire-visible description
rather than a docstring: a docstring is not where an operator building that pipeline is
looking.

Neither convention is changed. Each is correct for its own family and each is argued where
it is declared; the Black-Scholes family is continuous everywhere it is written down, and
the Treasury publishes a bond-equivalent yield that the carry arithmetic consumes unchanged
so that both legs of a carry sit on one clock. Converting either to match the other would
move stored numbers to remove a hazard that naming it removes.
"""

_Rate = Annotated[float, msgspec.Meta(description=_RATE_DESCRIPTION)]
"""The options family's rate field, one spelling shared by four params structs."""


class IvSurfaceParams(msgspec.Struct, frozen=True):
    """Parameters for ``iv-surface`` — one underlying at one instant.

    ``rate`` defaults to 0.0 on all three surfaces (cli.py:3377, api_server.py:2308,
    mcp_server.py:1421), so it carries that default here.

    ``fit_method`` is **not** here, and this is the rule for the whole options family.
    ``iv_surface`` and ``vol_skew`` both accept ``fit_method="sabr"`` and neither the CLI,
    REST nor MCP ever offered it — a port that added it would be inventing public API under
    cover of a migration, and there is no legacy behaviour to check the new option against.
    It would also be asymmetric within its own family: ``term_structure`` does not take the
    argument at all, so a shared surface option would be honoured by two capabilities and
    silently ignored by the third. The implementations keep their own default.
    """

    underlying: str
    at_ns: int
    rate: _Rate = 0.0


class TermStructureParams(msgspec.Struct, frozen=True):
    """Parameters for ``term-structure`` — the ATM slice of the same snapshot.

    Field-for-field identical to :class:`IvSurfaceParams`, and deliberately a separate type;
    the module docstring has the argument.
    """

    underlying: str
    at_ns: int
    rate: _Rate = 0.0


class VolSkewParams(msgspec.Struct, frozen=True):
    """Parameters for ``vol-skew`` — one underlying, one expiry, one instant."""

    underlying: str
    expiry_ns: int
    at_ns: int
    rate: _Rate = 0.0


class RiskReversalParams(msgspec.Struct, frozen=True):
    """Parameters for ``risk-reversal`` — a vol skew plus the delta to read it at.

    ``target_delta`` is 0.25 on all three surfaces (cli.py:3757, api_server.py:2435,
    mcp_server.py:1510), which is the 25-delta convention the capability is named after.
    """

    underlying: str
    expiry_ns: int
    at_ns: int
    rate: _Rate = 0.0
    target_delta: float = 0.25


class OfiParams(msgspec.Struct, frozen=True):
    """Parameters for ``ofi`` — one symbol, a window, and a bin width.

    ``interval`` defaults to ``1m``. The three surfaces look like three answers and are
    not: the CLI's ``None`` (cli.py:3921) is a sentinel, and both branches behind it resolve
    to ``1m`` — the non-interactive fallback at :3939 and the prompt's default at :3960.
    REST publishes ``1m`` twice, as the signature default and as the empty-string coalesce
    (api_server.py:2177, :2198). Only MCP declines to choose, marking it required
    (mcp_server.py:1369). So one value is written five times across two surfaces and the
    third asks the caller for a number nobody has to think about; ``1m`` wins.

    That ``indicators`` defaults to ``1d`` is not a contradiction. OFI bins top-of-book
    revisions, where a day-wide bin sums a day of churn into one number, and ``indicators``
    bins trades into candles, where a minute-wide bin is a different chart. The default
    belongs to the measurement, not to the word ``interval``.

    "Top-of-book revisions" is now literally what both halves consume, and it is why the
    default survived Phase 3 without a second thought: crypto differences consecutive
    ``book_snapshot`` rows and equity differences consecutive ``quote`` rows, and a minute of
    quote churn is the same kind of quantity as a minute of book churn. A parameter whose
    meaning changed per asset class would be the divergence one shared struct exists to stop.
    """

    symbol: str
    start_ns: int
    end_ns: int
    interval: str = "1m"


class WhaleAlertsParams(msgspec.Struct, frozen=True):
    """Parameters for ``whale-alerts`` — trades and liquidations above a USD notional.

    ``min_usd`` defaults to 100 000. This is the one parameter in the batch where the three
    surfaces genuinely disagree: the CLI resolves it to ``100000.0`` in both of its branches
    (cli.py:4144 non-interactive, :4165 as the prompt's default), REST defaults it to ``0.0``
    (api_server.py:2225) and MCP requires it (mcp_server.py:1400).

    The CLI's value wins because it is the only one any surface *chose*. REST's ``0.0`` is
    what a FastAPI query parameter looks like when nobody decided — and it is not a harmless
    zero: ``min_usd=0`` admits every print in the range, so ``GET /whale-alerts`` with no
    threshold returns the entire tape under a name that promises whales. MCP's "required"
    is the honest version of the same abstention, but a capability whose whole subject is a
    threshold should carry the threshold the product means.

    This does change ``GET /whale-alerts?symbol=X`` with no ``min_usd``: it stops returning
    every trade and starts returning whales. A caller who wanted the tape asks for ``trade``
    rows; a caller who wanted whales was being lied to.

    The same 100 000 serves equities, where the rows are reported Form 4 transactions rather
    than prints. It is not a translated threshold — a dollar of disclosed insider selling and
    a dollar of perpetual notional are both a dollar — but it does select differently: a
    six-figure open-market purchase by an officer is a common filing where a six-figure
    print is a common trade, so the equity result set is sparser at the same setting. That is
    the market differing, not the parameter.

    ``start_ns``/``end_ns`` bound the *transaction* date for equities rather than the
    ingestion instant, which is a difference in what the window means and is argued at
    :func:`~crocodile.equity.analytics.filings.track_filing_whales`: a decade of filings
    arrives in one fetch carrying one ``local_ts``, so a window over that would answer with
    all of history or none of it.
    """

    symbol: str
    start_ns: int
    end_ns: int
    min_usd: float = 100_000.0


class LiquidityDepthParams(msgspec.Struct, frozen=True):
    """Parameters for ``liquidity-depth`` — one symbol, and nothing else.

    No time range, on any of the three surfaces (cli.py:4012, api_server.py:2489,
    mcp_server.py:1668), because ``calculate_block_liquidity_depth`` scans from zero to now
    and buckets by ``sequence_id`` rather than by time. Adding a window here would be a
    parameter the implementation cannot honour.

    The equity half cannot honour one either, for the opposite reason: it snapshots a live
    ladder rather than scanning stored ones, so there is one observation and it is now. Two
    implementations that cannot take a window, for two different reasons, is still one
    schema — which is what kept this struct at a single field through Phase 3.
    """

    symbol: str


class SmartMoneyParams(msgspec.Struct, frozen=True):
    """Parameters for ``smart-money`` — transfer rows and the addresses to watch.

    Both are data, not paths. The CLI takes ``--transfers`` and ``--watchlist`` as files
    (cli.py:4601, :4608) and REST and MCP take the rows and the map inline
    (api_server.py:2930, mcp_server.py:1948); loading a CSV is how a shell passes a table,
    so it stays in the CLI projection, the same call the ``rates`` field of
    :class:`FundingPredictParams` makes.

    ``watchlist`` keeps the union the REST body already declared (api_server.py:2927):
    ``normalize_watchlist`` accepts an ``addr -> label`` map, a bare address list, and the
    nested ``{"watchlist": ...}`` / ``{"labels": ...}`` / ``{"addresses": [...]}`` shapes,
    and narrowing it here would reject documents the legacy endpoint accepted.

    **What a row is, for equities.** One struct serves both asset classes, and this is the
    field where the two markets put different documents into one name. A crypto row is a
    transfer: a sender, a recipient, a USD value. An equity row is a *filing* row as the
    ``insider`` and ``holding_13f`` channels store it — a Form 4 transaction, keyed on
    ``insider_name``/``insider_cik``, or a 13F position, keyed on
    ``manager_name``/``manager_cik``. Both may be handed in together and the implementation
    routes each on the field that identifies it, so no third parameter naming the form is
    needed; a parameter like that would be a schema difference between the asset classes,
    which is the one thing :attr:`Capability.params` exists to prevent.

    The name ``transfers`` is kept for equities even though a filing is not a transfer. A
    renamed field is a different wire schema, and identical parameter schemas are half of
    what this registry means by symmetry — so the honest place to say "these are filings" is
    this docstring, not a second spelling of the field.

    An equity ``watchlist`` may key on either handle a filing carries: the CIK
    (``{"0001051401": "Apple CEO"}``) or the reported name
    (``{"COOK TIMOTHY D": "Apple CEO"}``). Both are matched case-insensitively and both
    resolve onto the same filer.

    **One quarter of 13F rows nets to nothing, on purpose.** An information table states a
    position, and a flow is the difference between two of them; the earliest table for a
    given filer and CUSIP is a baseline, not a purchase. A caller who supplies a single
    quarter gets no institutional flow because none was observed.
    """

    transfers: tuple[dict[str, Any], ...]
    watchlist: dict[str, Any] | list[Any]


class LabelTransfersParams(msgspec.Struct, frozen=True):
    """Parameters for ``label-transfers`` — rows, a watchlist, and two optional filters.

    ``known_only`` is ``False`` and ``min_usd`` is ``None`` on all three surfaces
    (cli.py:4686/:4679, api_server.py:3003/:3004, mcp_server.py:910/:911), so neither needs
    deciding. ``min_usd=None`` is not ``min_usd=0.0``: ``None`` skips the filter entirely
    while ``0.0`` runs it and drops every row whose ``usd_value`` is missing or unparseable,
    which is a different result on the same input.

    ``transfers`` and ``watchlist`` mean for equities exactly what they mean on
    :class:`SmartMoneyParams`, which has the argument for keeping the crypto spelling over
    filing rows.

    That ``min_usd=0.0`` distinction bites harder here than on the crypto side, because
    Form 4 states no price for a gift or an award: those rows have no notional at all, so
    ``0.0`` removes a whole class of real filings that ``None`` keeps. It is the same rule
    applied to a source that exercises it more often, not a different one.
    """

    transfers: tuple[dict[str, Any], ...]
    watchlist: dict[str, Any] | list[Any]
    known_only: bool = False
    min_usd: float | None = None


class ChaosScoreParams(msgspec.Struct, frozen=True):
    """Parameters for ``chaos-score`` — the four readings the index is made of.

    All four are required, which is MCP's schema (mcp_server.py:1805) and not the CLI's or
    REST's; both of those default every one of them to ``0.0`` (cli.py:4406, :4413, :4420,
    :4427; api_server.py:2568). Those defaults are what Typer and FastAPI need in order to
    make an option optional, and the value they chose is the worst possible one here: the
    four inputs *are* the computation, and all-zeros scores exactly 0.0 — "perfectly calm",
    reported for a market nobody looked at. That is the zero-standing-in-for-a-hole this
    package's provenance rules exist to refuse, one layer up.

    So ``crocodile chaos-score`` with no options stops printing 0.0 and starts saying which
    reading it is missing.

    **The field names are the crypto half's and two of them mean something else for
    equities.** One capability has one parameter struct, which is half of what full API
    symmetry means, so renaming a field per asset class was never available; what the equity
    half does instead is re-specify the subject and keep the slot. ``stablecoin_deviation``
    is read as the fraction by which a price has departed from the LULD reference its price
    band is measured against, and ``sequencer_delay`` as consolidated-tape latency in
    seconds. The other two are unchanged in kind. Each equity reading's unit — and the
    argument for why neither re-specification disproves an ``IRREDUCIBLE`` entry — is in
    :mod:`crocodile.equity.analytics.chaos`, and the equity result repeats it per term so a
    caller does not have to go there to read their own answer.
    """

    volatility: float
    stablecoin_deviation: float
    orderbook_imbalance: float
    sequencer_delay: float


# ---------------------------------------------------------------------------
# Adapters — (ctx, params) in, the analytics function's own signature out
# ---------------------------------------------------------------------------


def indicators(ctx: CapabilityContext, params: IndicatorParams) -> pl.DataFrame:
    """Resample the symbol's trades into bars, then append the requested indicators.

    The query is the crypto CLI's ``indicators`` command end to end, via
    ``CrypcodileClient.get_indicators``: resample, sort by ``bar``, then compute.

    ``fill_empty`` was hardcoded ``True`` here, on the argument that a quiet hour is
    otherwise absent from the series and a 14-period SMA therefore spans a different amount
    of wall-clock time on a thin symbol than on a busy one. The argument stands; the
    hardcoding does not, because it was also the equity command's flag and its default was
    ``False``. Silently flipping a default is not the same decision as offering one: it
    changed the arithmetic for every caller (max ΔRSI 36.56 over a session gap) and left
    nobody able to ask for the other answer, on any of the three surfaces. It now comes
    from :attr:`IndicatorParams.fill_empty`, which carries the measurement.

    An empty frame is passed through to :func:`apply_indicators` rather than returned
    early, so an unknown ``indicator`` name is still rejected on a lake with no data. The
    early return looked equivalent and made a typo depend on whether the symbol had trades.
    """
    bars = resample_ohlcv(
        ctx.catalog,
        params.symbol,
        params.start_ns,
        params.end_ns,
        params.interval,
        fill_empty=params.fill_empty,
    )
    if not bars.is_empty():
        bars = bars.sort("bar")
    return apply_indicators(bars, params.indicator, params.period)


def slippage(ctx: CapabilityContext, params: SlippageParams) -> pl.DataFrame:
    """Walk the stored crypto book for the requested size. A pure argument shuffle."""
    return estimate_slippage(
        ctx.catalog,
        params.symbol,
        params.side,
        params.size,
        params.size_unit,
    )


def slippage_equities(ctx: CapabilityContext, params: SlippageParams) -> pl.DataFrame:
    """Walk the equity depth ladder for the requested size.

    A separate adapter, because ``fn=slippage`` was bound for both asset classes and that
    was the whole defect. :func:`estimate_slippage` reads ``book_snapshot``, and no equity
    provider in this tree emits one — so every equity call raised ``ValueError: No book
    snapshots found`` while the declaration published ``prov_basis: "yahoo_1m_vap"``, a
    basis naming a ladder the code path never opened. A declared basis for an unreachable
    branch is worse than a missing one: it reads as measured.

    The ladder that basis names is the one ``depth`` already serves —
    :func:`~crocodile.equity.depth.select_depth_source`, Alpaca L1 when keyed and the
    synthetic Yahoo VAP profile when not — so the two capabilities now read one book and
    the walk over it is :func:`~crocodile.core.analytics.slippage.slippage_over_levels`,
    shared with crypto. Which of the two branches ran is on the profile's own tail, where
    it was measured; the declaration states the ceiling, and it states the same ceiling
    ``depth`` does, because there is only one book to have a ceiling over.

    Like ``depth``, this reaches the network rather than the lake, so it is driven through
    :func:`~crocodile.core.capability.run_to_completion` — the caller may or may not own an
    event loop, and the same declaration has to answer on all three surfaces.
    """
    source = select_depth_source(settings=ctx.settings)
    profile = run_to_completion(lambda: source.snapshot(params.symbol))
    return slippage_over_levels(
        params.symbol,
        params.side,
        params.size,
        list(profile.bids),
        list(profile.asks),
        size_unit=params.size_unit,
    )


def basis(ctx: CapabilityContext, params: BasisParams) -> pl.DataFrame:
    """ASOF-join the perpetual's mark price onto the nearest prior spot print."""
    return spot_perp_basis(
        ctx.catalog,
        params.spot_symbol,
        params.perp_symbol,
        params.start_ns,
        params.end_ns,
    )


def basis_equities(ctx: CapabilityContext, params: BasisParams) -> pl.DataFrame:
    """ASOF-join the equity derivative leg onto the nearest prior cash print.

    A separate adapter rather than ``fn=basis`` for both, on the argument
    :func:`slippage_equities` records: ``spot_perp_basis`` reads ``derivative_ticker``,
    and nothing under ``equity/providers`` writes one, so a shared binding would publish a
    declaration for a code path an equity lake cannot reach. The equity legs are price
    series in ``trade``, ``ohlcv`` or ``index_value``, which is why
    :func:`~crocodile.equity.analytics.carry.price_leg` exists and why it is not in
    ``core``: the fallback order is a fact about equity's channels, not about the
    measurement.

    ``perp_symbol`` names the derivative leg here. The struct is shared by both asset
    classes and one capability has one parameter schema, so the field keeps the spelling
    the crypto half gave it; what it means for equities is in
    :class:`BasisParams`'s own docstring rather than in a second struct.
    """
    return equity_basis(
        ctx.catalog,
        params.spot_symbol,
        params.perp_symbol,
        params.start_ns,
        params.end_ns,
    )


def perp_basis(ctx: CapabilityContext, params: PerpBasisParams) -> pl.DataFrame:
    """Mark price against index price, off the perpetual's own ticker channel."""
    return _perp_basis(ctx.catalog, params.symbol, params.start_ns, params.end_ns)


def perp_basis_equities(ctx: CapabilityContext, params: PerpBasisParams) -> pl.DataFrame:
    """The option market's forward for the symbol against its cash price, per snapshot.

    The equity reading of "one symbol, a derivative price and its reference on the same
    observation". Crypto gets both off one ``derivative_ticker`` row; equity gets the
    derivative side from put-call parity on its own option chain and the reference side
    from its own cash price series, which is the only single-symbol construction an equity
    lake can actually serve. The argument in full, including why the discount factor is
    what puts this capability on M5 rather than on M1 with the options family, is in
    :func:`~crocodile.equity.analytics.carry.equity_forward_basis`.
    """
    return equity_forward_basis(ctx.catalog, params.symbol, params.start_ns, params.end_ns)


def spot_future_basis(ctx: CapabilityContext, params: SpotFutureBasisParams) -> pl.DataFrame:
    """ASOF-join spot onto the future's prints, annualising when an expiry is given."""
    return _spot_future_basis(
        ctx.catalog,
        params.future_symbol,
        params.spot_symbol,
        params.start_ns,
        params.end_ns,
        params.expiry_ns,
    )


def spot_future_basis_equities(
    ctx: CapabilityContext, params: SpotFutureBasisParams
) -> pl.DataFrame:
    """The dated equity future against spot, annualised and net of the Treasury yield.

    The capability M5 is named for. Everything up to ``annualized_pct`` is the crypto
    half's answer in the crypto half's columns; ``carry_pct`` is that number minus what
    the money costs over the same horizon, which is the term a perpetual quotes directly
    as funding and a future does not quote at all.
    """
    return equity_spot_future_carry(
        ctx.catalog,
        params.future_symbol,
        params.spot_symbol,
        params.start_ns,
        params.end_ns,
        params.expiry_ns,
    )


def funding_apr(ctx: CapabilityContext, params: FundingAprParams) -> pl.DataFrame:
    """Per-settlement funding APR and the running sum. A pure argument shuffle."""
    return _funding_apr(ctx.catalog, params.symbol, params.start_ns, params.end_ns)


def funding_apr_equities(ctx: CapabilityContext, params: FundingAprParams) -> pl.DataFrame:
    """Per-dividend cost of carry for an equity position, in the crypto frame's columns.

    A perpetual settles financing in cash and logs it; an equity position pays financing
    and receives dividends, and only the second half is published as an event. So the row
    is the dividend, the period is the gap since the previous one, and the annualisation
    is the identical :func:`~crocodile.core.analytics.carry.apr_from_rate` the crypto half
    calls — with hours-to-the-next-payment where crypto passes the settlement interval.
    ``risk_free_apr`` and ``carry_apr`` close the other half.
    """
    return equity_funding_apr(ctx.catalog, params.symbol, params.start_ns, params.end_ns)


def funding_predict(ctx: CapabilityContext, params: FundingPredictParams) -> dict[str, Any]:
    """Forecast the next settlement's funding rate from a supplied rate history.

    ``ctx`` is unused because this capability reads nothing: the history arrives in
    ``params`` and the model is offline. It still takes the context, because a projector
    that had to know which implementations want one would be introspecting signatures
    again — the failure :data:`~crocodile.core.capability.CapabilityFn` rules out.

    ``predict_next_funding`` wants a list or tuple; ``rates`` is already a tuple, so this is
    the argument shuffle it looks like and not a conversion.
    """
    return predict_next_funding(params.rates, window_size=params.window_size)


def iv_surface(ctx: CapabilityContext, params: IvSurfaceParams) -> pl.DataFrame:
    """The implied-vol cross-section at one instant, latest row per strike/expiry/type."""
    return _iv_surface(ctx.catalog, params.underlying, params.at_ns, params.rate)


def iv_surface_equities(ctx: CapabilityContext, params: IvSurfaceParams) -> pl.DataFrame:
    """The same cross-section over a US equity chain, solved against the spot with BSM.

    A separate adapter rather than a second asset class bound to :func:`iv_surface`, and
    the reason is the one ``slippage_equities`` learned the hard way: the two halves do not
    read the same numbers off the row. The crypto solver inverts ``mark_price``, which no
    equity feed publishes, so binding one function for both would have produced a surface
    of ``source="unavailable"`` for every equity contract Yahoo did not hand a vol for —
    a hole that reads exactly like an empty lake, under a declaration claiming otherwise.

    Everything after that choice is shared: this and its crypto twin call one
    :mod:`crocodile.core.analytics.volsurface`, differing only in the
    :class:`~crocodile.core.analytics.volsurface.OptionsModel` they pass it, so the two
    frames cannot disagree about columns, dtypes, the snapshot rule or which strike is ATM.

    Reads the lake, like its twin, and so needs neither a network call nor
    ``run_to_completion``.
    """
    return _equity_iv_surface(ctx.catalog, params.underlying, params.at_ns, params.rate)


def term_structure(ctx: CapabilityContext, params: TermStructureParams) -> pl.DataFrame:
    """ATM implied vol per expiry — the surface read down its ATM column."""
    return _term_structure(ctx.catalog, params.underlying, params.at_ns, params.rate)


def term_structure_equities(ctx: CapabilityContext, params: TermStructureParams) -> pl.DataFrame:
    """The equity ATM column of the same surface. See :func:`iv_surface_equities`."""
    return _equity_term_structure(ctx.catalog, params.underlying, params.at_ns, params.rate)


def vol_skew(ctx: CapabilityContext, params: VolSkewParams) -> pl.DataFrame:
    """Per-strike implied vol and delta for a single expiry."""
    return _vol_skew(
        ctx.catalog,
        params.underlying,
        params.expiry_ns,
        params.at_ns,
        params.rate,
    )


def vol_skew_equities(ctx: CapabilityContext, params: VolSkewParams) -> pl.DataFrame:
    """One equity expiry's strikes, with the Black-Scholes-Merton delta beside each vol."""
    return _equity_vol_skew(
        ctx.catalog,
        params.underlying,
        params.expiry_ns,
        params.at_ns,
        params.rate,
    )


def risk_reversal(ctx: CapabilityContext, params: RiskReversalParams) -> dict[str, float | None]:
    """Solve the skew for one expiry, then read the risk reversal and butterfly off it.

    Two calls, because ``risk_reversal_butterfly`` takes the skew frame rather than a
    catalog — all three legacy surfaces make the same pair (cli.py:3789,
    api_server.py:2463, mcp_server.py:645), and none of them caches the intermediate.

    The empty skew is short-circuited rather than passed through. ``risk_reversal_butterfly``
    returns ``(None, None)`` for an empty frame anyway, so this is the legacy guard kept for
    its cost rather than its result: without it every no-data call still walks the frame's
    column set.

    Returns the two measurements and not the request that produced them. The REST route
    echoes ``underlying``, ``expiry_ns``, ``at`` and both parameters back into its body
    (api_server.py:2478) and the MCP tool does not (mcp_server.py:649); echoing is a
    surface's answer to "what did I ask for", and a surface already knows.
    """
    skew = _vol_skew(
        ctx.catalog,
        params.underlying,
        params.expiry_ns,
        params.at_ns,
        params.rate,
    )
    if skew.is_empty():
        return {"risk_reversal": None, "butterfly": None}
    rr, bf = risk_reversal_butterfly(skew, target_delta=params.target_delta)
    return {"risk_reversal": rr, "butterfly": bf}


def risk_reversal_equities(
    ctx: CapabilityContext, params: RiskReversalParams
) -> dict[str, float | None]:
    """The same two numbers off the equity skew, and the same short-circuit.

    ``risk_reversal_butterfly`` is not duplicated for the equity half and could not
    usefully be: by the time it runs the market has been reduced to strikes, vols and
    deltas, and subtracting a put's vol from a call's is the same sentence in both. Only
    the skew underneath it is asset-class-specific, which is the one call that differs
    between this and :func:`risk_reversal`.
    """
    skew = _equity_vol_skew(
        ctx.catalog,
        params.underlying,
        params.expiry_ns,
        params.at_ns,
        params.rate,
    )
    if skew.is_empty():
        return {"risk_reversal": None, "butterfly": None}
    rr, bf = risk_reversal_butterfly(skew, target_delta=params.target_delta)
    return {"risk_reversal": rr, "butterfly": bf}


def ofi(ctx: CapabilityContext, params: OfiParams) -> pl.DataFrame:
    """Order-flow imbalance per time bin, from consecutive top-of-book snapshots."""
    return calculate_ofi(
        ctx.catalog,
        params.symbol,
        params.start_ns,
        params.end_ns,
        params.interval,
    )


def ofi_equities(ctx: CapabilityContext, params: OfiParams) -> pl.DataFrame:
    """The same imbalance, differenced over stored L1 quotes instead of book snapshots.

    A separate adapter because the two asset classes read different channels — ``quote``
    against ``book_snapshot`` — and for no other reason: the statistic itself is one
    function, :func:`~crocodile.core.analytics.ofi.ofi_increment`, which both halves call.
    Binding ``fn=ofi`` for equities as well would have been the ``slippage`` defect exactly:
    one object reading a channel no equity provider writes, under a declaration naming a
    basis its code path could not reach.

    The four returned columns are the crypto half's four, so a caller who asked for ``ofi``
    does not have to know which market answered in order to read the answer.
    """
    return calculate_quote_ofi(
        ctx.catalog,
        params.symbol,
        params.start_ns,
        params.end_ns,
        params.interval,
    )


def liquidity_depth(ctx: CapabilityContext, params: LiquidityDepthParams) -> pl.DataFrame:
    """Cumulative bid and ask size within 1, 2 and 5 percent of mid, per book sequence."""
    return calculate_block_liquidity_depth(ctx.catalog, params.symbol)


_LADDER_BINS: Final[int] = 40
"""Price buckets the equity ladder is built from, and the number of levels kept per side.

The synthetic source's own defaults are 40 bins and ``top_n=10``, which ``depth`` and
``slippage`` both take, and which this capability cannot. Those two report the top of a
ladder and a walk from the touch, so ten levels is an answer; a band sum is *cumulative*,
so a ladder truncated before the band edge under-reports the very quantity being asked for,
silently and by an amount that depends on how wide the session's range happened to be. On a
day whose range is 2 %, forty buckets span 2 % and ten of them span about half a percent —
so the default would have truncated even the 1 % band. Asking for every bucket the profile
has is what makes the sums cover the whole ladder.

Ignored by the Alpaca L1 branch, which has one level per side and nothing to bin; see
:func:`~crocodile.equity.depth.select.select_depth_source`.
"""


def liquidity_depth_equities(
    ctx: CapabilityContext, params: LiquidityDepthParams
) -> pl.DataFrame:
    """Band depth over the equity ladder, from real L1 when keyed and a modelled one when not.

    This is what M6 was the binding constraint for. Cumulative size within a percent band is
    meaningless without both sides of a book, and equities had no book at all until
    :func:`~crocodile.equity.depth.select_depth_source` existed; the ladder it produces is
    the one ``depth`` and ``slippage`` already serve, so the three capabilities now read one
    book rather than three.

    The declaration's ``prov`` is the ceiling — ``DERIVED``/``alpaca_l1``, the same pair
    ``depth`` and ``slippage`` declare, because there is one book to have a ceiling over.
    Which of the two branches actually ran is not asserted here: it is on the profile's own
    tail, measured by ``provenance_fields`` when the ladder was built, and
    :func:`~crocodile.equity.analytics.liquidity_depth.liquidity_depth_from_profile` carries
    those four fields onto the row. A frame of sizes with no tail would leave a caller unable
    to tell resting quotes from traded volume standing in for them.

    Reaches the network rather than the lake, like ``depth`` and ``slippage``, so the
    coroutine is driven by :func:`~crocodile.core.capability.run_to_completion` — the caller
    may or may not own an event loop and one declaration has to answer on all three surfaces.
    """
    source = select_depth_source(
        bins=_LADDER_BINS, top_n=_LADDER_BINS, settings=ctx.settings
    )
    profile = run_to_completion(lambda: source.snapshot(params.symbol))
    return liquidity_depth_from_profile(profile)


def whale_alerts(ctx: CapabilityContext, params: WhaleAlertsParams) -> pl.DataFrame:
    """Trades and liquidations whose notional clears the threshold. An argument shuffle."""
    return track_whale_alerts(
        ctx.catalog,
        params.symbol,
        params.start_ns,
        params.end_ns,
        params.min_usd,
    )


def smart_money(ctx: CapabilityContext, params: SmartMoneyParams) -> list[dict[str, Any]]:
    """Net flow, volume and last activity per watched address, over supplied transfers.

    Normalising the watchlist first is what lets the three document shapes REST accepted
    keep working; ``summarize_smart_money`` alone treats a mapping as ``addr -> label`` and
    would read ``{"addresses": [...]}`` as a single address named ``addresses``.

    The legacy handlers return early on an empty watchlist (api_server.py:2977,
    mcp_server.py:866). That branch is dropped rather than copied: an empty address set
    matches no transfer, so the tracker yields no rows by construction, and a guard whose
    only effect is to reach the same empty list sooner is a second place for the two answers
    to differ.
    """
    return summarize_smart_money(params.transfers, normalize_watchlist(params.watchlist))


def label_transfers(ctx: CapabilityContext, params: LabelTransfersParams) -> list[dict[str, Any]]:
    """Annotate supplied transfer rows with watchlist labels, optionally filtering first.

    ``filter_transfers_by_usd`` runs before labelling, which is the order all three surfaces
    use (cli.py:4726, api_server.py:3059, mcp_server.py:946). It is kept because it is what
    they did and not because it changes anything: both steps are row-wise maps over disjoint
    keys — the filter reads ``usd_value`` and its aliases, the labeller reads ``from`` and
    ``to`` — so swapping them is unobservable. Stated because it looks like it should
    matter, and a reader who assumed it did would go looking for the case where it bites.

    ``known_only`` is genuinely ordered: it reads ``is_known``, which does not exist until
    the labeller has written it.

    The helpers are imported on demand. Their module keeps Web3 as a type-only import
    and loads ``eth_abi`` only when decoding RPC logs, so these offline transforms work
    with the base installation.
    """
    from crocodile.crypto.analytics.whale_transfers import (
        filter_transfers_by_usd,
        label_transfer_addresses,
    )

    rows: list[dict[str, Any]] = list(params.transfers)
    if params.min_usd is not None:
        rows = filter_transfers_by_usd(rows, params.min_usd)
    labelled = label_transfer_addresses(rows, normalize_watchlist(params.watchlist))
    if params.known_only:
        return [row for row in labelled if row.get("is_known")]
    return labelled


def whale_alerts_filings(ctx: CapabilityContext, params: WhaleAlertsParams) -> pl.DataFrame:
    """Reported insider transactions above the threshold — the equity form of a whale print.

    The crypto half asks the tape who moved size. US equities have no tape for that: the
    only public record of a large, attributable trade in a listed stock is a *disclosure*,
    and Section 16(a) is the one that produces it within days rather than quarters. So the
    equity implementation reads Form 4 out of the ``insider`` channel and thresholds the
    notional it reports, which is the same question — what cleared this size, which way, and
    when — answered from the only source that answers it.

    **13F-HR is deliberately not read here**, even though M4 adds its parser. An information
    table reports a *position* at quarter end, with no side and no date for any of the
    trades behind it, so a row of it cannot fill ``side`` or ``timestamp`` without inventing
    both. Reporting a $2bn holding as a whale alert would answer "who holds size" under a
    name that promises "who moved it". The 13F half of M4 lands in ``smart-money`` instead,
    where the difference between two consecutive tables *is* a flow — see
    :func:`~crocodile.equity.analytics.filings.filing_flows`.

    ``ctx.catalog`` rather than ``ctx.query``, which is the handoff ``open-interest``
    documents in :mod:`crocodile.capabilities.market` and the one the crypto half of this
    capability already makes: the SQL is fixed and internal to the analytics function, so
    there is no caller-supplied string to vet, and a surface row cap would turn a threshold
    query into a silently partial answer.
    """
    return track_filing_whales(
        ctx.catalog,
        params.symbol,
        params.start_ns,
        params.end_ns,
        params.min_usd,
    )


def smart_money_filings(ctx: CapabilityContext, params: SmartMoneyParams) -> list[dict[str, Any]]:
    """Net flow, volume and last activity per watched filer, over supplied filing rows.

    Reaches ``summarize_smart_money`` — the same tracker the crypto half runs — rather than
    reimplementing the netting, because the arithmetic was never the crypto-specific part.
    What is crypto-specific is the *shape* of an input row, and
    :func:`~crocodile.equity.analytics.filings.filing_flows` is the whole of the
    translation: an insider acquisition becomes value moving from the issuer to the filer, a
    disposition the reverse, and a change in a 13F position between two consecutive
    quarter-ends becomes a flow of the difference. A single quarter of holdings yields no
    flow at all, which is the true answer — a change cannot be observed before the first
    observation — and that function's docstring argues it.

    ``resolve_watched_filers`` sits between the watchlist and the tracker because a filing
    names its party twice, by CIK and by name, and a watchlist is entitled to use either.
    Handing the tracker the raw watchlist would silently match one spelling and miss the
    other, which is the asymmetry that shape of bug always has: it looks like the filer did
    nothing.
    """
    rows = list(params.transfers)
    watchlist = normalize_watchlist(params.watchlist)
    return summarize_smart_money(filing_flows(rows), resolve_watched_filers(rows, watchlist))


def label_transfers_filings(
    ctx: CapabilityContext, params: LabelTransfersParams
) -> list[dict[str, Any]]:
    """Annotate filing rows with watchlist labels — the join equity serves better than crypto.

    Same order as the crypto half, and for the same reason: filter by notional, then label,
    then drop the unknowns if asked. ``known_only`` is the one step that is genuinely
    ordered, because it reads ``is_known``, which does not exist until the labeller has
    written it.

    ``filter_transfers_by_usd`` is reused unchanged and needs no adaptation: it already reads
    ``usd_value``, ``amount`` and ``value`` in that order, and a filing row states ``value``.
    A Form 4 gift, which reports shares and no price, therefore has no notional and lands
    below every threshold rather than above the zero one — the same treatment a transfer with
    an unparseable ``usd_value`` gets. The helper is imported on demand and requires no
    on-chain dependencies for this offline transform.

    What differs from the crypto half is the label an *unmatched* party gets, and this is the
    claim the ledger comment above ``PENDING_SYMMETRY`` made when it said a CIK is already a
    label. On the crypto side an address off the watchlist gets ``""``, because a hex string
    is all there is. A filing arrives carrying the filer's own reported name and a CIK that
    never changes spelling, so every row comes back labelled and a watchlist is an
    *enrichment* rather than the only thing standing between the caller and an opaque
    string. ``is_known`` keeps meaning "matched the watchlist" and nothing wider, or
    ``known_only`` would quietly stop filtering.
    """
    from crocodile.crypto.analytics.whale_transfers import filter_transfers_by_usd

    rows: list[dict[str, Any]] = list(params.transfers)
    if params.min_usd is not None:
        rows = filter_transfers_by_usd(rows, params.min_usd)
    labelled = label_filing_parties(rows, normalize_watchlist(params.watchlist))
    if params.known_only:
        return [row for row in labelled if row.get("is_known")]
    return labelled


def chaos_score(ctx: CapabilityContext, params: ChaosScoreParams) -> float:
    """Blend four stress readings into one soft-thresholded index in ``[0, 100]``.

    Returns the score alone. Both REST and MCP echo the four inputs back beside it
    (api_server.py:2600, mcp_server.py:801); a surface that was handed the inputs does not
    need the capability to hand them back, and a scalar capability that returns a
    five-field object is not a scalar.
    """
    return calculate_chaos_score(
        volatility=params.volatility,
        stablecoin_deviation=params.stablecoin_deviation,
        orderbook_imbalance=params.orderbook_imbalance,
        sequencer_delay=params.sequencer_delay,
    )


def chaos_score_equities(ctx: CapabilityContext, params: ChaosScoreParams) -> dict[str, Any]:
    """The same index over the equity readings of the same four slots, plus its weights.

    Two of the four fields name phenomena this registry calls irreducible, so their equity
    readings are re-specified rather than ported —
    ``stablecoin_deviation`` becomes departure from the LULD reference price and
    ``sequencer_delay`` becomes consolidated-tape latency. The argument for each, and for
    why neither disproves an ``IRREDUCIBLE`` entry, is in
    :mod:`crocodile.equity.analytics.chaos`; it is there rather than here because it is a
    claim about the market, not about this adapter.

    Returns an object where the crypto half returns a bare float, and the difference is the
    weighting. Both halves weigh four readings at 0.25 and produce the same number for the
    same finite inputs — pinned by a test, because an equality asserted in prose is an
    equality that drifts. They part on a reading that is not a finite number: the crypto half
    scores a NaN volatility, deviation or delay as 0.0 and a NaN imbalance as 1.0, which is
    two opposite inventions from one absence, while this half drops the term and divides the
    index between the ones that were read. That matters more here than it would there
    because this tree's equity analytics *return* NaN as their "not enough data" answer, so a
    caller piping ``calculate_realized_volatility`` into this capability is the expected path
    rather than an edge case. A data-dependent weight has to be published to be checkable,
    which is why it is in the result; a constant one has nothing to say.
    """
    return _chaos_score_equities(
        volatility=params.volatility,
        stablecoin_deviation=params.stablecoin_deviation,
        orderbook_imbalance=params.orderbook_imbalance,
        sequencer_delay=params.sequencer_delay,
    )


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


SLIPPAGE = declare(
    Capability(
        name="slippage",
        summary="Expected execution price and slippage for a size, against the stored book.",
        params=SlippageParams,
        returns=ReturnKind.SCALAR,
        # One capability, two wire names. `slippage` is the crypto CLI command, the crypto
        # REST GET route and (as `estimate_slippage`) the MCP tool; `simulate-price-impact`
        # is a REST POST on both sides and the only spelling equity ever exposed. The name
        # is `slippage` because it names the measurement rather than an action performed on
        # a UI, and because one name here becomes a command, a path segment and a tool name
        # at once — an imperative reads wrong as two of those three.
        #
        # That equity exposes only the other spelling is not evidence for it: equity has no
        # CLI and no MCP at all, so the "shared" name is an artefact of equity having almost
        # nothing rather than of the name being the better one.
        aliases=("simulate-price-impact",),
        impls={
            AssetClass.CRYPTO: Impl(
                fn=slippage,
                prov=Provenance.DERIVED,
                basis="native",
                reads=("book_snapshot",),
            ),
            # `DERIVED`/`alpaca_l1`, which is what `market.py` declares for `depth` over
            # exactly this book. The two used to disagree: this entry said SYNTHETIC /
            # `yahoo_1m_vap` and argued it as the deliberate *floor* — "declaring the keyed
            # ceiling would let a keyless deployment report a level it never reaches" —
            # while `depth` said DERIVED / `alpaca_l1` and argued the ceiling. One book, one
            # `select_depth_source`, two batches, opposite ends of one range.
            #
            # `Impl.prov` is documented as a ceiling, so the floor argument was answering a
            # question the field does not ask, and its own worry is already handled where it
            # belongs: which branch ran is on the returned profile's tail, measured by
            # `provenance_fields`, not promised here.
            #
            # The basis was additionally false rather than merely pessimistic. `fn=slippage`
            # was the same object for both classes and read `book_snapshot`, which no equity
            # provider writes, so `yahoo_1m_vap` named a path that could not execute.
            #
            # The floor the deleted argument was reaching for now has somewhere to live that
            # does not fight the ceiling: `fallback` says what a *keyless* deployment gets,
            # so the surfaces announce SYNTHETIC/`yahoo_1m_vap` when that is what will run
            # and DERIVED/`alpaca_l1` when the keys are set. Before it existed the default
            # deployment named a method that never ran and, on the CLI, said nothing at all.
            AssetClass.EQUITY: Impl(
                fn=slippage_equities,
                prov=Provenance.DERIVED,
                basis="alpaca_l1",
                # Nothing. `select_depth_source` fetches a book from Alpaca or models
                # one from Yahoo bars; neither reaches the lake. An empty read-set is a
                # claim — "this answer needs no stored channel" — and it is the claim
                # that makes the crypto half's `book_snapshot` visible as the one the
                # equity half cannot make.
                reads=(),
                fallback=Fallback(
                    prov=Provenance.SYNTHETIC,
                    basis="yahoo_1m_vap",
                    reachable=alpaca_is_keyed,
                ),
            ),
        },
    )
)


INDICATORS = declare(
    Capability(
        name="indicators",
        summary="Moving averages, RSI, MACD and Bollinger bands over stored OHLCV.",
        params=IndicatorParams,
        returns=ReturnKind.TABLE,
        impls={
            # One function serves both: its input is OHLCV, which both asset classes
            # produce natively. This is the walking skeleton that keeps the symmetry gate
            # honest before the real work of Phase 3 — a gate whose only subject is a
            # capability contrived to satisfy it proves nothing.
            AssetClass.CRYPTO: Impl(
                fn=indicators, prov=Provenance.DERIVED, basis="native", reads=("trade",)
            ),
            AssetClass.EQUITY: Impl(
                fn=indicators, prov=Provenance.DERIVED, basis="native", reads=("trade",)
            ),
        },
    )
)


# The three basis capabilities stay three. The CLI is one `basis` command that infers its
# mode from which of --perp/--future/--spot were given (cli.py:3012), and REST and MCP are
# three endpoints each; collapsing to one capability would mean adopting the CLI's shape,
# which costs more than it reads like. One capability has one params struct, and the three
# modes do not share one: the legs differ in arity and in name, `expiry_ns` is meaningful in
# exactly one of them, and the returned columns differ (`spot_price`/`perp_price` versus
# `mark_price`/`index_price` versus `future_price`/`spot_price` plus `annualized_pct`). A
# merged struct would publish four optional symbol fields with no combination marked
# required, and the rule for which combinations are legal would live in the adapter as a
# re-implementation of `_basis_mode` — an inference the MCP inputSchema could not express
# and the OpenAPI schema could not either. Mode inference is a convenience a CLI projection
# can keep offering over three capabilities; it is not part of the contract.


BASIS = declare(
    Capability(
        name="basis",
        summary="Spot against perpetual mark, ASOF-joined onto the nearest prior print.",
        params=BasisParams,
        returns=ReturnKind.TABLE,
        impls={
            # Both legs are venue-reported: perpetual marks off `derivative_ticker`, spot
            # off `trade` with `book_snapshot` mid as the documented fallback. The join and
            # the two ratios are this implementation's own work, which is what makes the
            # result DERIVED rather than the NATIVE its inputs are.
            AssetClass.CRYPTO: Impl(
                fn=basis,
                prov=Provenance.DERIVED,
                basis="native",
                reads=("derivative_ticker", "trade", "book_snapshot"),
            ),
            # The same claim, verbatim, for the equity half: both legs are price series a
            # source published — `trade` prints, `ohlcv` closes or an `index_value` level —
            # and the ASOF join and the two ratios are the implementation's own work. This
            # is the one of the four M5 capabilities that needs no risk-free leg, because
            # `basis` takes no expiry on either asset class and so has no horizon to
            # finance over; `treasury_carry` would claim a third leg this measurement does
            # not have.
            AssetClass.EQUITY: Impl(
                fn=basis_equities,
                prov=Provenance.DERIVED,
                basis="native",
                # `price_leg`'s three channels, best first. They are read through a
                # loop over a module constant rather than named at the call site, which
                # is one of the two reasons this field is authored rather than scanned.
                reads=("trade", "ohlcv", "index_value"),
            ),
        },
    )
)


PERP_BASIS = declare(
    Capability(
        name="perp-basis",
        summary="Derivative price against its index price, per update, from one symbol.",
        params=PerpBasisParams,
        returns=ReturnKind.TABLE,
        impls={
            AssetClass.CRYPTO: Impl(
                fn=perp_basis,
                prov=Provenance.DERIVED,
                basis="native",
                reads=("derivative_ticker",),
            ),
            # `treasury_carry` and not `native`, because the equity forward is not read off
            # a record: put-call parity discounts the call/put difference at a published
            # yield, so a third input joins the two option marks and the cash price. The
            # confidence formula is a function of how stale that yield is against the
            # option's own horizon, which is the observable that varies here — the two
            # price legs are always both present in an emitted row, since a row cannot be
            # built without them.
            AssetClass.EQUITY: Impl(
                fn=perp_basis_equities,
                prov=Provenance.DERIVED,
                basis="treasury_carry",
                reads=(
                    "options_chain",
                    "macro_series",
                    "trade",
                    "ohlcv",
                    "index_value",
                ),
            ),
        },
    )
)


SPOT_FUTURE_BASIS = declare(
    Capability(
        name="spot-future-basis",
        summary="Dated future against spot, annualised when a contract expiry is given.",
        params=SpotFutureBasisParams,
        returns=ReturnKind.TABLE,
        impls={
            AssetClass.CRYPTO: Impl(
                fn=spot_future_basis,
                prov=Provenance.DERIVED,
                basis="native",
                reads=("trade",),
            ),
            # DERIVED is still the ceiling: every number in is reported by somebody — the
            # future's prints by its venue, the cash leg by its source, the par yield by
            # the US Treasury — and the annualisation and the subtraction are arithmetic
            # over the three. Nothing is modelled from a different data class, which is
            # what would make it SYNTHETIC. The basis moves off `native` because a venue
            # reported none of the *carry*: `native` would say one had.
            AssetClass.EQUITY: Impl(
                fn=spot_future_basis_equities,
                prov=Provenance.DERIVED,
                basis="treasury_carry",
                reads=("macro_series", "trade", "ohlcv", "index_value"),
            ),
        },
    )
)


FUNDING_APR = declare(
    Capability(
        name="funding-apr",
        summary="Per-period financing cost of a position, annualised and accumulated.",
        params=FundingAprParams,
        returns=ReturnKind.TABLE,
        impls={
            AssetClass.CRYPTO: Impl(
                fn=funding_apr, prov=Provenance.DERIVED, basis="native", reads=("funding",)
            ),
            # Same basis as the two above and for the same reason: the dividend leg is
            # published (`corp_action`, from three providers), the price it is expressed
            # against is published, and the financing leg is the Treasury curve. What is
            # not published is their combination, which is what the capability returns.
            AssetClass.EQUITY: Impl(
                fn=funding_apr_equities,
                prov=Provenance.DERIVED,
                basis="treasury_carry",
                reads=(
                    "corp_action",
                    "macro_series",
                    "trade",
                    "ohlcv",
                    "index_value",
                ),
            ),
        },
    )
)


FUNDING_PREDICT = declare(
    Capability(
        name="funding-predict",
        summary="Next-period funding rate forecast from a supplied rate history.",
        params=FundingPredictParams,
        returns=ReturnKind.SCALAR,
        impls={
            # SYNTHETIC, and it is the only reasonable ceiling: the value reported is a
            # settlement that has not happened, so nothing observed it at any level. The
            # method — XGBoost when the `ml` extra is installed, a rolling mean otherwise —
            # is named in the returned object's `method` field, where it varies per call.
            #
            # `caller_supplied` names where the *inputs* came from, and this used to say
            # `native` under a paragraph explaining that it was the batch's weakest basis:
            # the rates arrive in `params` rather than out of the lake, so `native` claimed
            # "funding rates as a venue settles them" — which is what the parameter
            # documents and not something this capability can check. The registered basis
            # that says it properly now exists and names this capability in its own
            # docstring, so the caveat is spent and the declaration carries the claim.
            AssetClass.CRYPTO: Impl(
                fn=funding_predict, prov=Provenance.SYNTHETIC, basis="caller_supplied"
            ),
            # One function serves both, which is the `indicators` pattern and not the
            # `fn=slippage` defect. The distinction is whether the shared body can reach
            # data the asset class has: `slippage` read `book_snapshot`, which no equity
            # provider writes, while this reads nothing at all — the history arrives in
            # `params` and the model is offline, so there is no lake, no channel and no
            # venue for the two halves to differ over. A second adapter here would be two
            # spellings of one call with nothing to distinguish them but a name.
            #
            # The ledger scheduled this against M5 because "there is nothing for its
            # equity half to predict until `carry` exists", and that is now discharged
            # rather than dismissed: the series it forecasts is the one `funding-apr`
            # produces, and `funding-apr` has an equity half in this same commit. Both
            # series are per-period financing rates in the same sign convention, which is
            # what makes one forecaster correct for both — the crypto period is a
            # settlement interval and the equity period is the gap to the next dividend,
            # and a rolling mean over a sequence does not know or need the difference.
            AssetClass.EQUITY: Impl(
                fn=funding_predict, prov=Provenance.SYNTHETIC, basis="caller_supplied"
            ),
        },
    )
)


IV_SURFACE = declare(
    Capability(
        name="iv-surface",
        summary="Implied-volatility cross-section for an underlying at one instant.",
        params=IvSurfaceParams,
        returns=ReturnKind.TABLE,
        impls={
            # DERIVED is the ceiling. On its best day every row's `iv` is the venue's own
            # `mark_iv` and the surface is those points arranged into a cross-section — the
            # venue reported the points, not the surface, which is the same argument
            # `ohlcv_from_trades` makes for a bar. It is not NATIVE for that reason and not
            # SYNTHETIC either, because the fallback path inverts Black-76 on a venue mark
            # price rather than modelling a price from some other data class. Which of the
            # three each row actually took is on the row, in its `source` column.
            AssetClass.CRYPTO: Impl(
                fn=iv_surface,
                prov=Provenance.DERIVED,
                basis="native",
                reads=("options_chain",),
            ),
            # The equity half lands on the same two words, by the same argument rather than
            # by symmetry. On its best day every `iv` is Yahoo's own `impliedVolatility`,
            # carried on the row as `mark_iv` — the feed supplied the points and this
            # arranged them into a cross-section, which is DERIVED and not NATIVE. The
            # fallback inverts Black-Scholes-Merton on the *mid of a published bid and
            # ask*, so it is a venue quote being solved rather than a price modelled out of
            # some other data class, which is what keeps it the near side of SYNTHETIC.
            # `native` names the inputs and it is right for both branches: an IV Yahoo
            # published and a two-sided quote Yahoo published are both things a venue said.
            # Which branch a row took is on the row, in the same `source` column with the
            # same three words. What is *not* claimed here is the model: the solve is
            # European over an American contract at a zero dividend yield, and
            # `equity/analytics/volsurface.py` states both and what they cost, because
            # neither is a sampling deficiency and `prov_confidence` would be the wrong
            # place to report them.
            AssetClass.EQUITY: Impl(
                fn=iv_surface_equities,
                prov=Provenance.DERIVED,
                basis="native",
                # Read through `core.analytics.volsurface`, which serves both asset
                # classes from one module and so cannot be attributed to either by any
                # per-module scan. That blind spot is the other reason this field is
                # authored, and it is where the defect sat: until `yahoo` joined the
                # provider registry, no equity ingest path wrote this channel and this
                # capability returned zero rows on every lake the product could build.
                reads=("options_chain",),
            ),
        },
    )
)


TERM_STRUCTURE = declare(
    Capability(
        name="term-structure",
        summary="ATM implied volatility per expiry for an underlying at one instant.",
        params=TermStructureParams,
        returns=ReturnKind.TABLE,
        impls={
            AssetClass.CRYPTO: Impl(
                fn=term_structure,
                prov=Provenance.DERIVED,
                basis="native",
                reads=("options_chain",),
            ),
            AssetClass.EQUITY: Impl(
                fn=term_structure_equities,
                prov=Provenance.DERIVED,
                basis="native",
                reads=("options_chain",),
            ),
        },
    )
)


VOL_SKEW = declare(
    Capability(
        name="vol-skew",
        summary="Per-strike implied volatility and delta for a single option expiry.",
        params=VolSkewParams,
        returns=ReturnKind.TABLE,
        impls={
            AssetClass.CRYPTO: Impl(
                fn=vol_skew,
                prov=Provenance.DERIVED,
                basis="native",
                reads=("options_chain",),
            ),
            AssetClass.EQUITY: Impl(
                fn=vol_skew_equities,
                prov=Provenance.DERIVED,
                basis="native",
                reads=("options_chain",),
            ),
        },
    )
)


RISK_REVERSAL = declare(
    Capability(
        name="risk-reversal",
        summary="Risk reversal and butterfly at a target delta, from one expiry's skew.",
        params=RiskReversalParams,
        returns=ReturnKind.SCALAR,
        impls={
            AssetClass.CRYPTO: Impl(
                fn=risk_reversal,
                prov=Provenance.DERIVED,
                basis="native",
                reads=("options_chain",),
            ),
            AssetClass.EQUITY: Impl(
                fn=risk_reversal_equities,
                prov=Provenance.DERIVED,
                basis="native",
                reads=("options_chain",),
            ),
        },
    )
)


OFI = declare(
    Capability(
        name="ofi",
        summary="Order-flow imbalance per time bin, from consecutive book snapshots.",
        params=OfiParams,
        returns=ReturnKind.TABLE,
        impls={
            AssetClass.CRYPTO: Impl(
                fn=ofi,
                prov=Provenance.DERIVED,
                basis="native",
                reads=("book_snapshot",),
            ),
            # The same pair as crypto, and for the same reason: every number the increment
            # consumes — two prices and two sizes — was published by the provider on the
            # `quote` channel, which is what `native` names, and differencing consecutive
            # ones into an imbalance is this implementation's own work, which is what makes
            # the result DERIVED. The split `indicators` makes, one channel over.
            AssetClass.EQUITY: Impl(
                fn=ofi_equities,
                prov=Provenance.DERIVED,
                basis="native",
                reads=("quote",),
            ),
        },
    )
)


LIQUIDITY_DEPTH = declare(
    Capability(
        name="liquidity-depth",
        # "per book sequence" was true while only crypto implemented this, and it named the
        # crypto row key — `sequence_id` — in the one sentence all three surfaces publish as
        # help. The equity half has no sequence to be per, so the summary now says what both
        # halves do and each frame names its own row key in its own column.
        summary="Bid and ask size within 1, 2 and 5 percent of the reference price.",
        params=LiquidityDepthParams,
        returns=ReturnKind.TABLE,
        impls={
            AssetClass.CRYPTO: Impl(
                fn=liquidity_depth,
                prov=Provenance.DERIVED,
                basis="native",
                reads=("book_snapshot",),
            ),
            # `DERIVED`/`alpaca_l1`, the pair `depth` and `slippage` already declare over
            # this same `select_depth_source` ladder. `Impl.prov` is a ceiling, so it names
            # the best branch the switch can take rather than the branch a keyless
            # deployment gets; the branch that actually ran is measured on the returned
            # row's own `prov_*` columns, carried across from the profile.
            #
            # Not `native`, which is what the crypto half declares: crypto's inputs are a
            # venue's own published book, and neither equity branch is that. L1 is a quote
            # reshaped into a ladder and the keyless branch is traded volume standing in for
            # resting size — the distinction `alpaca_l1` and `yahoo_1m_vap` are registered
            # to keep, and which a shared `native` would erase.
            AssetClass.EQUITY: Impl(
                fn=liquidity_depth_equities,
                prov=Provenance.DERIVED,
                basis="alpaca_l1",
                reads=(),
                # The third of the three implementations over this one switch, declaring the
                # same pair of branches. "The branch that actually ran is measured on the
                # returned row's own `prov_*` columns" was true and was not enough: a caller
                # reads the envelope before the rows, and the envelope named the branch that
                # did not run.
                fallback=Fallback(
                    prov=Provenance.SYNTHETIC,
                    basis="yahoo_1m_vap",
                    reachable=alpaca_is_keyed,
                ),
            ),
        },
    )
)


WHALE_ALERTS = declare(
    Capability(
        name="whale-alerts",
        summary="Trades and liquidations whose notional clears a USD threshold.",
        params=WhaleAlertsParams,
        returns=ReturnKind.TABLE,
        impls={
            AssetClass.CRYPTO: Impl(
                fn=whale_alerts,
                prov=Provenance.DERIVED,
                basis="native",
                reads=("trade", "liquidation"),
            ),
            # DERIVED over `sec_form4`, which is the same relationship the crypto entry has
            # to `native`: the venue — here the filer, through EDGAR — reported the shares,
            # the price and the date, and the threshold and the notional are this
            # implementation's own arithmetic over them.
            #
            # The basis is `sec_form4` and not `native` because `native`'s registered
            # confidence is a flat 1.0 "by definition", and a Form 4 line is not always
            # fully reported: `transactionPricePerShare` is blank on a gift and on an award,
            # and this capability thresholds on exactly the notional that box is half of. A
            # row that could not be priced would otherwise reach the lake claiming the same
            # sampling adequacy as one that could.
            AssetClass.EQUITY: Impl(
                fn=whale_alerts_filings,
                prov=Provenance.DERIVED,
                basis="sec_form4",
                reads=("insider",),
            ),
        },
    )
)


SMART_MONEY = declare(
    Capability(
        name="smart-money",
        summary="Net flow, volume and last activity per watched address, from transfers.",
        params=SmartMoneyParams,
        returns=ReturnKind.TABLE,
        impls={
            # DERIVED: every output field is a sum or a max over the transfers supplied, and
            # nothing here is modelled. `caller_supplied` for the reason `funding-predict`
            # gives — the rows arrive in `params`, so no channel this capability read is
            # behind them and nothing here can grade how they were sampled.
            AssetClass.CRYPTO: Impl(
                fn=smart_money, prov=Provenance.DERIVED, basis="caller_supplied"
            ),
            # The identical pair, and identical for a reason rather than by copying. The
            # rows arrive in `params` for equities exactly as they do for crypto — that is
            # what the shared params struct means — so nothing this implementation reads
            # came out of the lake, and `caller_supplied` is the basis that says so without
            # claiming a venue reported it. Every output field is still a sum or a max, so
            # the level is DERIVED.
            #
            # Note this is *not* `sec_form4`, even though the rows will usually have come
            # from that parser: a basis names where this implementation's inputs came from,
            # and this one cannot check that the dicts it was handed are filings at all.
            AssetClass.EQUITY: Impl(
                fn=smart_money_filings, prov=Provenance.DERIVED, basis="caller_supplied"
            ),
        },
    )
)


LABEL_TRANSFERS = declare(
    Capability(
        name="label-transfers",
        summary="Annotate transfer rows with watchlist labels, optionally filtered by USD.",
        params=LabelTransfersParams,
        returns=ReturnKind.TABLE,
        impls={
            AssetClass.CRYPTO: Impl(
                fn=label_transfers, prov=Provenance.DERIVED, basis="caller_supplied"
            ),
            # `caller_supplied` for the reason `smart-money`'s equity entry gives. This is
            # also the capability the M4 ledger entry singled out as the one equity serves
            # *better*, and the difference is in the answer rather than in this declaration:
            # a filing row carries a CIK and a reported name, so every row comes back
            # labelled instead of only the watchlisted ones.
            AssetClass.EQUITY: Impl(
                fn=label_transfers_filings, prov=Provenance.DERIVED, basis="caller_supplied"
            ),
        },
    )
)


CHAOS_SCORE = declare(
    Capability(
        name="chaos-score",
        summary="Composite market-stress index in [0, 100] from four supplied readings.",
        params=ChaosScoreParams,
        returns=ReturnKind.SCALAR,
        impls={
            # SYNTHETIC: the reported quantity is an index, and no market publishes one.
            # Its four terms are soft-thresholded and averaged, so the number is modelled
            # from a different data class than any of them — which is the definition, and
            # also why a chaos score is not comparable with anything but another chaos
            # score. `caller_supplied` because all four terms are typed in by the caller:
            # this is the capability whose shipped answer read `{"prov": "synthetic",
            # "prov_basis": "native", "method": "A venue-reported value is certain by
            # definition."}` over four numbers no venue ever saw.
            AssetClass.CRYPTO: Impl(
                fn=chaos_score, prov=Provenance.SYNTHETIC, basis="caller_supplied"
            ),
            # The same pair, and both halves of it for the same reasons. SYNTHETIC because
            # an index is modelled from a different data class than any of its terms, and no
            # market publishes one; `caller_supplied` because all four readings arrive in
            # `params`. Re-specifying two of the four terms for equities changes what the
            # numbers mean and changes neither of those facts — a re-specified term is still
            # a number the caller typed in, and a composite of four of them is still an index
            # nobody quotes.
            AssetClass.EQUITY: Impl(
                fn=chaos_score_equities, prov=Provenance.SYNTHETIC, basis="caller_supplied"
            ),
        },
    )
)


# ---------------------------------------------------------------------------
# The schedule for this batch's equity halves
# ---------------------------------------------------------------------------

PENDING_SYMMETRY.update(
    {
        # This block is empty. What follows is the record of what it held and how each
        # entry left, in the past tense throughout, kept because the arguments turned out
        # to be the design. A capability still on the schedule would show up as a live
        # mapping here, not as a paragraph.
        #
        # M1 was here — the options family, all four of it — and is repaid. The prediction
        # the entry made held: `term-structure`, `vol-skew` and `risk-reversal` are
        # `iv-surface` read three ways, and once the Yahoo chain carried a spot and the
        # surface moved to `core.analytics.volsurface` they landed together, three adapters
        # of two lines each. The one thing the entry did not foresee is that the equity
        # chain needed the *underlying price* before any of it worked — Yahoo's option
        # payload carries it in the quote block beside the strikes, and without it every
        # moneyness is NaN and no mid can be inverted.
        #
        # M5 is discharged and its five names are gone from here too. The prediction the
        # entries made held: all four spread capabilities were the same trade priced four
        # ways, equity had the legs, and what it was missing was the rate that turns a
        # spread into a carry. `crocodile.equity.providers.treasury` is that rate,
        # `crocodile.core.analytics.carry` is the arithmetic both asset classes now share,
        # and `crocodile.equity.analytics.carry` is where the four equity halves read
        # their legs. Two of the five needed an argument the ledger did not anticipate:
        # `perp-basis`, whose one-symbol schema no equity *record* can satisfy — nothing
        # under `equity/providers` writes a `derivative_ticker` — and which is served
        # instead by put-call parity on the symbol's own option chain, which is what makes
        # the discount factor load-bearing rather than decorative; and `funding-apr`,
        # whose equity period is the gap between dividend events rather than a settlement
        # interval. `funding-predict` needed neither, for the reason its declaration
        # gives.
        # M7 — order-flow imbalance from L1 quote changes, which is this measurement's
        # equity form: the crypto implementation differences consecutive top-of-book
        # snapshots, and an equity quote stream is the same two prices and two sizes.
        # M6 — depth. The crypto implementation reads a real ladder out of `book_snapshot`;
        # equity had no ladder until the synthetic VAP profile and its Alpaca L1 upgrade
        # were built, and cumulative size within a percent band is meaningless without one.
        # Both exist now — `crocodile.equity.depth.select` — which is what discharged this.
        # M4 is closed and its three names have left this ledger. The argument that stood
        # here is kept because it turned out to be the whole design and not a plan: the
        # equity form of "who is moving size" is a filing rather than a transfer, so
        # `whale-alerts` reads Form 4 as large reported transactions, `smart-money` nets
        # per-filer flow — from dated insider lines, and from the difference between two
        # consecutive 13F information tables, since one table is a position and not a flow
        # — and `label-transfers` is the same watchlist join against named filers. The last
        # of the three is where the claim that a CIK is already a label was cashed: every
        # equity row comes back labelled, because the filing carries the filer's own name
        # and a stable id, where an unwatched Ethereum address carries neither.
        # M6, and this is the batch's one uncomfortable mapping — no single spec method
        # covers a composite. Of the four terms, volatility is computable from equity bars
        # today, and `stablecoin_deviation` and `sequencer_delay` name phenomena this same
        # registry calls IRREDUCIBLE, so their equity readings have to be re-specified
        # rather than ported. What blocks an equity chaos score *mechanically* is the one
        # term neither available nor re-definable without new data: order-book imbalance
        # needs both sides' resting size, which is precisely what M6 delivers. M6 is named
        # because it is the binding constraint, not because it is the whole of the work.
        #
        # M7 closed `ofi` and M6 closed `liquidity-depth` and `chaos-score`; their
        # entries are gone rather than annotated, which is what the ledger's hoarding
        # rule asks for. The three arguments those entries carried now live where the
        # work does: `ofi_equities` and `liquidity_depth_equities` above, and
        # `crocodile.equity.analytics.chaos` for the composite, whose two re-specified
        # terms needed more room than a ledger comment. That leaves this batch owing
        # nothing: all fifteen names it scheduled in Phase 2 have been repaid, and the
        # call below updates the ledger with nothing because there is nothing left.
    }
)
