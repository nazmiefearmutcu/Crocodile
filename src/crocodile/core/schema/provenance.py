"""Provenance levels and the registry of confidence formulas.

Every record Crocodile writes declares how it came to exist. Where a value is
not reported natively by a venue, the method that produced it is named by
``prov_basis``, and that name is a key into this registry.

The registry exists to make one rule enforceable: a confidence number must be
a pure function of observable inputs, documented alongside its formula. Constants
chosen by feel are how honest provenance decays into decoration.

Confidence measures **sampling adequacy within a level**, not truthfulness
across levels. A synthetic profile built from a full session scores 1.0 because
it is fully sampled *as a synthetic profile*; ``prov`` is what says it is not a
real order book. The two fields answer different questions and neither
substitutes for the other. This is also why a confidence of 0.0 is not the same
claim as :attr:`Provenance.UNAVAILABLE`: an empty synthetic profile and an
absent one are distinguished by ``prov``, never by the number alone.

:func:`provenance_fields` is the only supported way to populate a record's four
``prov_*`` fields. Hand-assembling them bypasses the confidence formula, which
is exactly what this module exists to prevent.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import warnings
from collections.abc import Callable, Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Any, Final, NamedTuple

from crocodile.core.errors import ProvenanceError

__all__ = [
    "ConfidenceFn",
    "ConfidenceFormulaError",
    "ConfidenceInputError",
    "Provenance",
    "ProvenanceFields",
    "UnregisteredBasisError",
    "confidence_for",
    "describe",
    "level_for",
    "load_all_bases",
    "provenance_fields",
    "register_basis",
    "registered_bases",
    "trust_rank",
    "worst_provenance",
]


class Provenance(StrEnum):
    """How a record came to exist."""

    NATIVE = "native"
    """The venue reported this value directly."""

    DERIVED = "derived"
    """Computed from native records already in the lake."""

    SYNTHETIC = "synthetic"
    """Modelled from a different data class than the one being reported."""

    UNAVAILABLE = "unavailable"
    """Obtainable from no configured source. A typed hole, not a zero.

    This is a capability-envelope state, never a record state. A record's required fields
    are the observation itself — a ``Trade`` cannot exist without an id, a price and an
    amount — so an "unavailable record" could only be built by inventing them. A capability
    with nothing to return for an asset class returns an empty result set whose envelope
    carries this level; it never fabricates hole-records. :class:`CapabilityUnavailable` is
    the raised form of the same fact, and a conformance gate asserts no record type defaults
    ``prov`` to this value.
    """


ConfidenceFn = Callable[[Mapping[str, Any]], float]


class UnregisteredBasisError(ProvenanceError, LookupError):
    """A ``prov_basis`` was used that has no registered confidence formula."""


class ConfidenceInputError(ProvenanceError, ValueError):
    """Caller fault: the inputs handed to a confidence formula are missing or malformed."""


class ConfidenceFormulaError(ProvenanceError, RuntimeError):
    """Formula fault: a registered formula raised, or returned a value outside ``[0, 1]``.

    Distinct from :class:`ConfidenceInputError` on purpose. A caller that degrades to
    :attr:`Provenance.UNAVAILABLE` on bad input must not silently swallow a broken formula.
    """


class _Registered(NamedTuple):
    """One basis's registration, inserted into the registry as a single atomic value."""

    fn: ConfidenceFn
    level: Provenance
    inputs: tuple[str, ...]
    doc: str


class ProvenanceFields(NamedTuple):
    """The four-field provenance tail every record carries."""

    prov: Provenance
    prov_basis: str
    prov_confidence: float
    prov_inputs: list[str]


_REGISTRY: Final[dict[str, _Registered]] = {}

# One regular US trading session in 1-minute bars, scoped to the one basis that
# uses it: a crypto volume-at-price basis would have a 1440-minute day and no
# session at all, so this is not a shared-core constant.
_YAHOO_1M_VAP_SESSION_BARS: Final[int] = 390

# The two sides a top of book has. Scoped the same way: it is the denominator of
# one basis and a fact about quotes, not about depth in general.
_L1_QUOTED_SIDES: Final[int] = 2


def register_basis(
    basis: str, *, level: Provenance, inputs: Sequence[str], doc: str | None = None
) -> Callable[[ConfidenceFn], ConfidenceFn]:
    """Register the confidence formula for ``basis``.

    Args:
        basis: The ``prov_basis`` name this formula answers for.
        level: The provenance level records built on this basis carry.
        inputs: The data channels the method consumes (``["ohlcv"]``, ``[]``, ...). This is a
            property of the method rather than of any call site, which is why it is declared
            here; it becomes the record's ``prov_inputs``. Note this is a different thing
            from the ``inputs`` mapping passed to :func:`confidence_for`, which carries the
            observable measurements a specific call has to hand.
        doc: Optional description, preferred by :func:`describe` over the formula's
            docstring. The docstring is the normal path and the rule below still applies to
            it; this exists because ``python -OO`` strips docstrings, which would otherwise
            leave :func:`describe` empty and blank the warning body the REST and MCP
            surfaces must emit for every non-native record. A basis that supplies neither a
            docstring nor ``doc`` yields an empty description under ``-OO``, so ``-OO`` is
            not a supported mode for those surfaces.

    Raises:
        ValueError: if ``basis`` is already registered, or the formula has no docstring.
            The docstring is the only place a constant like ``native``'s 1.0 can be
            justified, so an undocumented formula is a rejected formula. The docstring
            half of that check is skipped under ``python -OO``, which strips docstrings at
            compile time and so leaves it nothing to inspect.
    """

    def decorate(fn: ConfidenceFn) -> ConfidenceFn:
        if basis in _REGISTRY:
            raise ValueError(f"provenance basis {basis!r} is already registered")
        docstring = (fn.__doc__ or "").strip()
        # Under -OO the interpreter strips docstrings at compile time, so this check has
        # nothing to inspect and would turn every registration into an import-time crash.
        # The rule is a development and CI discipline; neither runs with -OO.
        if sys.flags.optimize < 2 and not docstring:
            raise ValueError(
                f"confidence formula for {basis!r} has no docstring; "
                f"a confidence number must be documented alongside its formula"
            )
        _REGISTRY[basis] = _Registered(
            fn=fn,
            level=level,
            inputs=tuple(inputs),
            doc=(doc or "").strip() or docstring,
        )
        return fn

    return decorate


def _lookup(basis: str) -> _Registered:
    """Return the registration for ``basis``, or raise :class:`UnregisteredBasisError`."""
    try:
        return _REGISTRY[basis]
    except KeyError:
        raise UnregisteredBasisError(
            f"provenance basis {basis!r} has no registered confidence formula; "
            f"register one with @register_basis({basis!r}, level=..., inputs=[...])"
        ) from None


def confidence_for(basis: str, inputs: Mapping[str, Any]) -> float:
    """Return the confidence for ``basis`` given its observable ``inputs``.

    Raises:
        UnregisteredBasisError: if ``basis`` has no formula.
        ConfidenceInputError: if ``inputs`` is missing a key the formula needs, or holds a
            value of the wrong type. Caller fault.
        ConfidenceFormulaError: if the formula raised, or returned a value outside
            ``[0.0, 1.0]``. Formula fault.
    """
    registered = _lookup(basis)
    try:
        value = float(registered.fn(inputs))
    except ConfidenceInputError:
        raise
    except KeyError as exc:
        raise ConfidenceInputError(
            f"confidence formula for {basis!r} requires input {exc}; given: {sorted(inputs)}"
        ) from exc
    except Exception as exc:
        raise ConfidenceFormulaError(
            f"confidence formula for {basis!r} raised {type(exc).__name__}: {exc}"
        ) from exc
    if not 0.0 <= value <= 1.0:
        raise ConfidenceFormulaError(
            f"confidence formula for {basis!r} returned {value!r}, outside [0, 1]"
        )
    return value


def level_for(basis: str) -> Provenance:
    """Return the provenance level ``basis`` was registered under."""
    return _lookup(basis).level


def describe(basis: str) -> str:
    """Return the description of the formula registered for ``basis``.

    This is the ``doc`` passed to :func:`register_basis` if there was one, and the formula's
    docstring otherwise. The REST and MCP surfaces use it as the body of the warning they
    are required to emit whenever they serve a record whose ``prov`` is not
    :attr:`Provenance.NATIVE`. It is empty under ``python -OO`` for any basis that relied on
    its docstring, since ``-OO`` strips docstrings.
    """
    return _lookup(basis).doc


def registered_bases() -> frozenset[str]:
    """Every basis name registered *in this process so far*.

    This reflects only what has been imported. A conformance gate that compares call sites
    against this set must call :func:`load_all_bases` first, or a basis living in a module
    nothing happened to import reads as a false offender. That import is deliberately not
    done here: an accessor that imports the world is a trap.
    """
    return frozenset(_REGISTRY)


def load_all_bases() -> None:
    """Import every ``crocodile`` submodule so that every ``@register_basis`` has run.

    Call this explicitly before treating :func:`registered_bases` as the complete set.
    A submodule that fails to import — a missing optional extra, most likely — is skipped
    rather than allowed to crash the caller, since a gate must still be able to run on a
    partial install. The cost is that a basis inside such a module stays invisible. Any
    other failure is warned about rather than swallowed, so a genuinely broken module is
    not mistaken for an absent optional dependency.
    """
    import crocodile

    for module in pkgutil.walk_packages(crocodile.__path__, prefix=f"{crocodile.__name__}."):
        try:
            # The names come from walk_packages over the package's own __path__, so no
            # caller-supplied value reaches this import; and a dynamic import is the entire
            # point of the function, so there is no literal form to prefer.
            # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
            importlib.import_module(module.name)
        except ImportError:
            continue
        except Exception as exc:
            warnings.warn(
                f"crocodile: could not load provenance bases from {module.name!r}: "
                f"{type(exc).__name__}: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            continue


def provenance_fields(basis: str, inputs: Mapping[str, Any] | None = None) -> ProvenanceFields:
    """Build a record's provenance tail from a registered basis.

    This is the only supported way to populate the four ``prov_*`` fields.
    Hand-assembling them bypasses the confidence formula, which is exactly what
    the registry exists to prevent.

    ``prov`` and ``prov_inputs`` come from the registration; ``prov_confidence`` is
    computed from ``inputs`` by the registered formula.

    Raises:
        UnregisteredBasisError: if ``basis`` has no formula.
        ConfidenceInputError: if ``inputs`` does not satisfy the formula.
        ConfidenceFormulaError: if the formula misbehaves.
    """
    registered = _lookup(basis)
    return ProvenanceFields(
        prov=registered.level,
        prov_basis=basis,
        prov_confidence=confidence_for(basis, inputs if inputs is not None else {}),
        prov_inputs=list(registered.inputs),
    )


def _require_int(inputs: Mapping[str, Any], key: str) -> int:
    """Read ``key`` from ``inputs`` as an ``int``, refusing to coerce.

    A definitional count arriving as ``3.9`` or ``"390"`` is a caller bug, and coercing it
    would silently turn a malformed input into a plausible-looking confidence.
    """
    try:
        value = inputs[key]
    except KeyError:
        raise ConfidenceInputError(
            f"missing required input {key!r}; given: {sorted(inputs)}"
        ) from None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfidenceInputError(
            f"input {key!r} must be an int, got {type(value).__name__}: {value!r}"
        )
    return value


@register_basis("native", level=Provenance.NATIVE, inputs=[])
def _native(_: Mapping[str, Any]) -> float:
    """A venue-reported value is certain by definition.

    The constant 1.0 is not a judgement about the venue's accuracy; it says the value was
    fully sampled at its own level, having been read rather than reconstructed.
    """
    return 1.0


@register_basis("unavailable", level=Provenance.UNAVAILABLE, inputs=[])
def _unavailable(_: Mapping[str, Any]) -> float:
    """A hole carries no information, so it is fully unsampled at any level."""
    return 0.0


@register_basis("yahoo_1m_vap", level=Provenance.SYNTHETIC, inputs=["ohlcv"])
def _yahoo_1m_vap(inputs: Mapping[str, Any]) -> float:
    """Volume-at-price depth: confidence is the session coverage of the profile.

    ``min(n / 390, 1.0)``, where ``n`` counts volume-bearing 1-minute bars and 390 is one
    full regular US session in such bars. The number reads as how many bars the profile
    holds against that reference: 1.0 means at least as many volume-bearing bars as one
    full session contains. That is not the same as having covered a session — 390 bars
    drawn from three partial sessions also scores 1.0. A profile built from three bars is
    not a book.

    Saturating at a full session says the profile is as well sampled as this method can
    make it — not that it has become a real order book. That claim is ``prov``'s job, and
    it stays :attr:`Provenance.SYNTHETIC` at every value.
    """
    n = _require_int(inputs, "n_volume_bars")
    if n < 0:
        raise ConfidenceInputError(f"input 'n_volume_bars' must be non-negative, got {n}")
    return min(n / _YAHOO_1M_VAP_SESSION_BARS, 1.0)


@register_basis("alpaca_l1", level=Provenance.DERIVED, inputs=["quote"])
def _alpaca_l1(inputs: Mapping[str, Any]) -> float:
    """Top of book reshaped as a depth profile: confidence is how much of the top is quoted.

    ``n_quoted_sides / 2``, where ``n_quoted_sides`` counts the sides Alpaca's latest-quote
    endpoint returned a price for. It is the only thing about this method that varies: the
    source has exactly one level per side and no notion of a requested depth, so two sides is
    the whole of what one call can observe. A one-sided quote describes half a top of book,
    an empty one describes none, and both are states the endpoint really returns.

    :attr:`Provenance.DERIVED` rather than :attr:`Provenance.NATIVE`: every number in the
    profile was reported by the venue, but the venue reported a *quote*. Nothing here is
    modelled — which is what separates it from ``yahoo_1m_vap``, where traded volume stands
    in for resting size — so it is not :attr:`Provenance.SYNTHETIC` either, and
    ``DepthProfile.is_synthetic`` reads ``False`` for it exactly as the equity fork's
    hand-written ``is_synthetic=False`` did.

    Saturating at both sides says the profile is as well sampled as this method can make it,
    not that one price level has become a book. That claim is ``prov``'s, and it stays
    ``DERIVED`` at every value.
    """
    n = _require_int(inputs, "n_quoted_sides")
    if not 0 <= n <= _L1_QUOTED_SIDES:
        raise ConfidenceInputError(
            f"input 'n_quoted_sides' must be between 0 and {_L1_QUOTED_SIDES}, got {n}"
        )
    return n / _L1_QUOTED_SIDES


def _aggregate_of_an_undeclared_stream(_: Mapping[str, Any]) -> float:
    """Shared body for the two bases whose input stream declares no size: 1.0, argued.

    A bar is a *function* of the records handed to the resampler — every print in the
    bucket is in the open, high, low, close and volume, and none of them is estimated.
    What the bar might be missing is records the caller never supplied, and a resampler
    cannot observe a stream it was not given.

    The obvious counter-argument, and why it does not land here. ``high`` and ``low`` are
    order statistics over the supplied sample, so a one-quote minute yields
    ``open == high == low == close`` with ``volume = 0.0`` and still scores 1.0 — which is
    precisely the claim ``yahoo_1m_vap`` refuses for a three-bar profile. The difference is
    the denominator, not the sparsity. ``yahoo_1m_vap`` divides by 390 because a regular US
    session *contains* 390 one-minute bars whether or not any were fetched: the reference
    exists independently of the input, so "how much of it did we get" is answerable. A trade
    or quote stream has no such reference. How many prints a minute should hold is not a
    property of the market, it is whatever traded, and dividing by a number invented to
    stand for it would be the constant this registry exists to refuse, one indirection
    deeper.

    That is an argument for refusing to measure, not a claim that the sparsity does not
    matter — so the sparsity is reported where it can be reported honestly, as a count
    rather than a ratio: the bar carries ``num_trades``, which is the sample size, and for
    a quote bar it is the quote count. ``ohlcv_from_ohlcv`` is the case where the
    denominator *does* exist, because every input bar declares its own width, and that one
    is measured. The contrast is the argument: measure when the input declares a
    denominator, refuse to invent one when it does not.

    The claim that the bar was not reported by a venue is ``prov``'s, and it is the level
    each registration below states — not this number.
    """
    return 1.0


register_basis(
    "ohlcv_from_trades",
    level=Provenance.DERIVED,
    inputs=["trade"],
    doc=(
        "Bars aggregated from trade prints. DERIVED, not NATIVE: the venue reported the "
        "prints, not the bar. Confidence is a constant 1.0, and the argument for it — "
        "including why it is not the claim yahoo_1m_vap refuses — is in "
        "_aggregate_of_an_undeclared_stream."
    ),
)(_aggregate_of_an_undeclared_stream)

register_basis(
    "ohlcv_from_quotes",
    level=Provenance.SYNTHETIC,
    inputs=["quote"],
    doc=(
        "Bars whose prices are quotes. SYNTHETIC rather than DERIVED because a quote is "
        "a different data class from the traded prices a bar reports: nothing here was "
        "ever transacted, and `volume` is a structural 0.0 rather than a measured one — "
        "quotes carry no size that belongs in a bar. Confidence is a constant 1.0 on the "
        "argument in _aggregate_of_an_undeclared_stream; the sample size a one-quote "
        "bucket has is on the record, as num_trades."
    ),
)(_aggregate_of_an_undeclared_stream)


@register_basis("ohlcv_from_ohlcv", level=Provenance.DERIVED, inputs=["ohlcv"])
def _ohlcv_from_ohlcv(inputs: Mapping[str, Any]) -> float:
    """Re-bucketed bars: extent times adequacy, both against the tradeable window.

    ``min(covered_ns / tradeable_ns, 1.0) * min(sampled_ns / tradeable_ns, 1.0)``, where

    * ``covered_ns`` is the **union** of the input bars' declared spans inside the
      emitted bucket — *extent*, how much of the window has any input at all;
    * ``sampled_ns`` is that same union with each instant weighted by the best
      ``prov_confidence`` covering it — *adequacy*, how well the covered part was
      sampled;
    * ``tradeable_ns`` is how much of the bucket the market could fill.

    **Why the denominator is not the bucket's width.** It was, and a *complete* regular
    US session — 390 one-minute bars, every one of them at confidence 1.0 — re-bucketed
    to 1d scored 0.2708, because 390 minutes of a 1440-minute calendar day is all a
    complete session can ever be. A consumer thresholding ``>= 0.5`` dropped every
    complete equity daily bar there is. ``yahoo_1m_vap`` in this same registry already
    divides by 390 for exactly this market and exactly this reason; wall-clock was never
    the reference, it was the reference for a market that never closes. So the caller
    declares the tradeable window, because only the caller knows the calendar. Today the
    only caller is the equity resampler and it passes regular sessions
    (``crocodile.equity.resample.ohlcv._tradeable_ns``); a market that never closes would
    pass the bucket width unchanged, which is what the old formula assumed of every
    market. Over-declaring the window — a holiday nobody modelled — lowers the score and
    never raises it, which is the safe direction for a number consumers filter on and the
    reason a full calendar is not a prerequisite for measuring at all.

    **Why the two terms are separate, and multiplied.** They used to be one: the width of
    each input was multiplied by its own confidence and the products summed, so 390 bars
    at 0.5 and 195 bars at 1.0 produced the identical number from different states. They
    are not the same state. Half-sampled inputs across a whole session still observed
    every minute's own high and low; a missing half-session observed nothing there, and
    the day's high may be absent from the bar entirely. A gap therefore fails both tests
    — the bar does not span the window, *and* the instants it does not span contribute no
    sampling — while dilution fails only the second. Multiplying charges it twice, which
    is the asymmetry the two states differ by: 390 at 0.5 scores 0.5, 195 at 1.0 scores
    0.25.

    **Why a union and not a sum.** ``covered_ns`` is a union of intervals because a lake
    spanning the migration holds the same day under two channel tags, and a summed width
    counted it twice — *raising* the confidence of a bar because one of its inputs was
    duplicated. A duplicate is the one thing that must never make a derivation look better
    sampled. Overlapping inputs contribute their instant once, at the best confidence
    covering it.

    Saturating at 1.0 in each term says the bucket is as covered, and as well sampled, as
    this method can make it. A caller re-bucketing wide bars into narrower ones passes
    more coverage than the bucket holds, and that is a full bucket, not an over-full one.
    The claim that the bar was not reported by a venue is ``prov``'s, and the emitted bar
    carries the worst level among its inputs (see :func:`worst_provenance`), so
    re-bucketing synthetic bars yields synthetic ones rather than laundering them.
    """
    covered_ns = _require_int(inputs, "covered_ns")
    sampled_ns = _require_int(inputs, "sampled_ns")
    tradeable_ns = _require_int(inputs, "tradeable_ns")
    if tradeable_ns <= 0:
        raise ConfidenceInputError(f"input 'tradeable_ns' must be positive, got {tradeable_ns}")
    if covered_ns < 0:
        raise ConfidenceInputError(f"input 'covered_ns' must be non-negative, got {covered_ns}")
    if sampled_ns < 0:
        raise ConfidenceInputError(f"input 'sampled_ns' must be non-negative, got {sampled_ns}")
    if sampled_ns > covered_ns:
        raise ConfidenceInputError(
            f"input 'sampled_ns' ({sampled_ns}) exceeds 'covered_ns' ({covered_ns}); "
            f"an instant cannot be sampled better than it is covered"
        )
    extent = min(covered_ns / tradeable_ns, 1.0)
    adequacy = min(sampled_ns / tradeable_ns, 1.0)
    return extent * adequacy


@register_basis("caller_supplied", level=Provenance.NATIVE, inputs=[])
def _caller_supplied(_: Mapping[str, Any]) -> float:
    """Inputs the caller handed in rather than the lake produced: 1.0, by definition.

    Eight capabilities compute over numbers that arrive in their ``params`` — ``gas-vol``,
    ``mev-sandwich``, ``lending-stress``, ``chaos-score``, ``funding-predict``,
    ``smart-money``, ``label-transfers`` and ``peg-deviation``'s pure mode. Two porting
    agents reached for ``native`` for want of anything better and both said in writing
    that it claims more than the code can check: ``native`` means *a venue reported this*,
    and no venue was involved.

    What is true instead is narrower and worth stating. A pure function is exact over
    whatever it was handed, so there is no sampling loss *inside* this engine to grade —
    which is what ``prov_confidence`` measures. The sampling story of the inputs belongs
    to whoever produced them and is not ours to assert; a lower number here would be this
    engine grading a stranger's data, which it cannot observe and must not guess at.

    So the level stays :attr:`Provenance.NATIVE` and the value stays 1.0, and the honesty
    lives in the basis *name*: ``WHERE prov_basis = 'caller_supplied'`` separates these
    from anything the lake produced, which ``native`` could not.
    """
    return 1.0


@register_basis("scraped_last_price", level=Provenance.SYNTHETIC, inputs=[])
def _scraped_last_price(_: Mapping[str, Any]) -> float:
    """A last price lifted off a web page, reported as a trade: 0.0, by definition.

    ``google_finance`` scrapes one number — the last traded price — and the record it
    fills is a :class:`~crocodile.core.schema.records.Trade`, which cannot exist without a
    quantity. The page publishes no per-print size at any time, for any symbol, so the
    measurement that makes a trade a trade is unsampled at every call. There is nothing
    that varies, which is why this is a constant and why it is declared as one rather than
    left to look like a formula.

    0.0 is the same reading ``unavailable`` carries: no sampling evidence. It is not the
    claim that the price is wrong — the price was really the last trade — and ``prov``
    stays :attr:`Provenance.SYNTHETIC` to say the record's shape is modelled rather than
    observed. What this refuses to be is the 1.0 the header defaults to, which said the
    venue reported a one-share print directly and left a consumer filtering on
    ``prov != NATIVE`` silent.
    """
    return 0.0


_AMM_LADDER_LEVELS: Final[int] = 5
"""Levels a side of the reconstructed AMM ladder asks for. The denominator of one basis
and a property of that reconstruction, not of depth in general — scoped like
``_YAHOO_1M_VAP_SESSION_BARS`` above."""


@register_basis("amm_tick_curve", level=Provenance.SYNTHETIC, inputs=[])
def _amm_tick_curve(inputs: Mapping[str, Any]) -> float:
    """A concentrated-liquidity curve reshaped as a book: how much of the ladder it fills.

    ``n_levels / 5``, where ``n_levels`` counts the price levels the pool's active
    liquidity actually supports a non-zero size at, out of the five a side asks for.

    :attr:`Provenance.SYNTHETIC` and not :attr:`Provenance.DERIVED`, which is the whole
    point of the entry. Every number is computed from real chain state — the active tick,
    the liquidity in range, the token decimals — but liquidity in range is what the pool
    *could* fill, not orders anyone placed. That is a different data class from the resting
    depth a :class:`~crocodile.core.schema.records.BookSnapshot` reports, which is the line
    ``SYNTHETIC`` draws and the line ``alpaca_l1`` stays the other side of: a top of book
    reshaped into a profile is still quotes somebody posted.

    The count is the honest observable because the reconstruction thins out. It assumes
    the active liquidity holds across five tick-spacings either side of the current tick,
    and where it does not the level computes to nothing — so the levels that survive are
    the levels the curve has evidence for. A pool with liquidity at one spacing scores
    0.2, which is the sparse profile ``yahoo_1m_vap`` refuses to call full, measured the
    same way.

    The predecessor of that count was ``max(size, 0.0001)``, which floored every empty
    level into a dust order: ``SELECT min(bid_sz) … WHERE source='base_onchain'`` returned
    0.0001 and ``WHERE bid_sz > 0`` — "is there liquidity here" — answered yes for a
    drained pool. A level with no size is not a level.

    Saturating at five says the ladder is as filled as this method builds it, not that the
    curve has become a book; ``prov`` stays ``SYNTHETIC`` at every value.
    """
    n = _require_int(inputs, "n_levels")
    if n < 0:
        raise ConfidenceInputError(f"input 'n_levels' must be non-negative, got {n}")
    return min(n / _AMM_LADDER_LEVELS, 1.0)


_REFERENCE_SOURCES: Final[int] = 3
"""Sources the equity reference merge consults: SEC EDGAR, OpenFIGI and Tiingo.

The denominator of one basis and a property of that method — scoped exactly like
``_YAHOO_1M_VAP_SESSION_BARS`` above, and for the same reason: a crypto venue universe
comes off one venue's own market list and has no such triple to agree with.
"""


@register_basis("reference_merge", level=Provenance.DERIVED, inputs=["instrument"])
def _reference_merge(inputs: Mapping[str, Any]) -> float:
    """A listed security's identity assembled from three registries: how many of them named it.

    ``n_sources / 3``, where ``n_sources`` counts the reference sources that returned a row
    for this ticker out of the three ``SPEC_METHODS`` M3 names — SEC EDGAR's
    ``company_tickers.json``, Tiingo's supported-tickers file, and OpenFIGI's mapping
    endpoint. A ticker all three name is an identity three independent registrars agree
    exists; one only Tiingo lists is a listing nobody else has confirmed, and the number
    says which of the two you are holding.

    **Why the denominator is a fixed three rather than "sources this run consulted".** The
    second reading is the tempting one and it is the ``yahoo_1m_vap`` mistake one axis over:
    it would score a run that only managed SEC EDGAR at 1.0 for every ticker SEC lists,
    which reports a single-registrar row as fully attested. The method *declares* three
    sources, so a session contains three attestations whether or not any given run fetched
    them — the same argument that divides by 390 because a regular US session contains 390
    one-minute bars whether or not any were fetched. This matters in practice rather than in
    principle: OpenFIGI's keyless tier is twenty-five requests a minute in batches of ten, so
    enrichment is bounded by the slice a caller asked for rather than run across a
    ninety-thousand-row universe, and the rows outside that slice really are attested twice
    and score 0.67. Degrading and saying so is the product promise; degrading and reporting
    1.0 is the thing this registry exists to refuse.

    :attr:`Provenance.DERIVED` and not :attr:`Provenance.NATIVE`. Every field on a merged
    row was published by one of the three — the CIK by SEC, the listing venue by Tiingo, the
    FIGI by OpenFIGI — so nothing here is modelled, which is the line
    :attr:`Provenance.SYNTHETIC` is the other side of. But no single source published the
    *row*: the identity that joins a registrant to a listing to a global identifier is this
    engine's, assembled by ``CoverageResolver`` under a stated priority, and NATIVE would
    claim a registrar reported it that way.

    Saturating at three says the identity is as well attested as this method can make it, not
    that it has become a venue's own instrument record. That claim is ``prov``'s, and it
    stays ``DERIVED`` at every value.
    """
    n = _require_int(inputs, "n_sources")
    if not 0 <= n <= _REFERENCE_SOURCES:
        raise ConfidenceInputError(
            f"input 'n_sources' must be between 0 and {_REFERENCE_SOURCES}, got {n}"
        )
    return n / _REFERENCE_SOURCES


_SESSION_SNAPSHOT_WITNESSES: Final[int] = 2
"""Independent witnesses the session-snapshot method can have for which session an undated
scanner row describes: the exchange calendar, and the row's own update stamp. The denominator
of one basis, scoped like ``_REFERENCE_SOURCES``."""


@register_basis("session_snapshot", level=Provenance.DERIVED, inputs=["ohlcv"])
def _session_snapshot(inputs: Mapping[str, Any]) -> float:
    """A scanner's undated daily bar, attributed to a session: how many witnesses agree on which.

    ``n_session_witnesses / 2``. TradingView's scanner answers with the current state of a
    listing's latest session - open, high, low, last, volume - and no date. This method dates
    it. The calendar names the latest session whose close plus the settle delay has passed, and
    a row without a stamp captured before the next session opens can only describe that
    session: that is the calendar witness, and a bar is never built without a completed session
    from it. Where the source stamps the row and the stamp falls inside that session, that is
    the second witness, and the stamp then replaces the next-open guard (spec 3.10b). The
    denominator is the two witnesses the method declares, which exist whether or not a given
    source publishes a stamp - the ``reference_merge`` argument for a fixed three. A bar
    attributed by the calendar alone scores 0.5 because the calendar's own failure - a holiday or
    half-day it does not know - is invisible to it, which is exactly what the stamp would catch.

    DERIVED, not NATIVE: every number was published by the source, but the session the bar
    belongs to is this engine's attribution, the step NATIVE would claim the source made. Not
    SYNTHETIC: nothing is modelled. Rows the method cannot attribute - zero volume, identical to
    the previous snapshot with no stamp, a stamp on another day, an unstamped row captured once
    the next session has opened - are not built at all rather than scored low; the score grades
    the bars that exist.
    """
    n = _require_int(inputs, "n_session_witnesses")
    if not 0 <= n <= _SESSION_SNAPSHOT_WITNESSES:
        raise ConfidenceInputError(
            f"input 'n_session_witnesses' must be between 0 and "
            f"{_SESSION_SNAPSHOT_WITNESSES}, got {n}"
        )
    return n / _SESSION_SNAPSHOT_WITNESSES


@register_basis("farcaster_cast_search", level=Provenance.SYNTHETIC, inputs=[])
def _farcaster_cast_search(_: Mapping[str, Any]) -> float:
    """Social metrics modelled from a page of casts: 0.0, by definition.

    Neynar's cast-search endpoint returns casts. :class:`FarcasterCorrelation` requires a
    24-hour mention count, a developer-activity score and a trending rank, and the endpoint
    publishes none of the three — they are counted, scored from author bios and ranked by
    the adapter. Every measurement on the record is modelled, so there is no sampling
    evidence to grade and nothing about the method that varies, which is why this is a
    constant and why it is declared as one.

    0.0 is the reading ``unavailable`` and ``scraped_last_price`` carry, and for the same
    reason: not that the numbers are wrong, but that no part of them was sampled. The
    header default this replaces said Farcaster published a trending rank directly, and
    left a consumer filtering ``prov != NATIVE`` silent on a record with nothing measured
    on it at all.

    What this does not fix, and cannot from here: ``mentions_24h`` names a window the
    query does not request, so a count over an untimed search is filed under a
    twenty-four-hour field. That is a schema question rather than a provenance one.
    """
    return 0.0


@register_basis("book_snapshot_slice", level=Provenance.DERIVED, inputs=["book_snapshot"])
def _book_snapshot_slice(inputs: Mapping[str, Any]) -> float:
    """A ladder cut out of a stored venue book: how full the ladder is, times how fresh it is.

    ``min(n_levels / n_requested, 1.0) * (1 - age_ns / window_ns)``, where

    * ``n_levels`` counts the levels the stored snapshot actually put into the ladder, both
      sides together, after dead levels are dropped and the requested depth is applied;
    * ``n_requested`` is the ladder the caller asked for, ``2 * top_n``;
    * ``age_ns`` is how far *before* the instant the slice claims to describe the snapshot
      it was cut from was observed;
    * ``window_ns`` is how stale the caller said a book may be and still describe that
      instant.

    **Why neither neighbouring basis covers this, which is why it exists.** ``native`` says
    a venue reported this value. The venue reported *a book*, at its own instant and to
    whatever depth it was subscribed at; what a slice hands back is that book re-cut to a
    requested depth and re-stamped at a requested instant, and neither edit is the venue's.
    ``book_resample`` is the nearer miss and misses in the opposite direction: it is a
    declared constant because a boundary capture is exact at the instant it is stamped — the
    resampler *chooses* the boundary and flushes it before applying the record that crosses
    it, so nothing is left over to score. A slice chooses no instant. The caller names one
    and the lake holds whatever it holds at or before it, so the two deficiencies that are
    structurally impossible in a resampled capture are precisely the two that vary here.

    **Why the fill term does not contradict ``book_resample``'s note on truncation.** That
    entry argues, correctly, that ``top_n`` truncation is not a deficiency: a caller asking
    for five levels and receiving the best five has been answered. It holds the whole
    reconstructed book, so it can always answer. This does not: the truncation that matters
    happened upstream, at whatever depth the collector subscribed the venue's book at, and it
    is already in the Parquet file. Asking for twenty-five levels and receiving the ten the
    lake stored is not an answer trimmed to the request, it is a ladder the lake could not
    fill. So the two agree rather than disagree — this term is 1.0 in exactly the case
    ``book_resample`` describes, and below it only when the request outruns the store.

    **Why staleness is scored here and declined there.** ``book_resample`` declines it
    because a quiet interval is not an unsampled one: if no update arrives between two
    boundaries the book genuinely did not change, and there is no reference for how often a
    book ought to tick. That argument is about a *chosen* boundary. Here the record claims an
    instant the caller chose and was built from an observation made before it, and the gap
    between the two is not the market being quiet — it is this answer describing one moment
    with another moment's book.

    **Where the denominator comes from, and the one uncomfortable thing about it.**
    ``window_ns`` is declared by the caller, on ``ohlcv_from_ohlcv``'s precedent: only the
    caller knows what "still describes this instant" means for what they are doing, a minute
    for a risk report and a second for an execution decision. The precedent does not carry
    cleanly, and saying so is cheaper than pretending it does. Over-declaring
    ``tradeable_ns`` *lowers* that score, which is the safe direction; over-declaring
    ``window_ns`` raises this one, so a caller who widens their tolerance grades the same
    stale book higher. It is taken anyway because both alternatives are worse. The natural
    structural reference — the interval to the *next* stored snapshot — cannot be read
    without looking past the instant this record claims to describe, which
    :mod:`crocodile.core.resample.book` forbids and which this basis exists downstream of. A
    fixed constant would be a number invented to stand for how often a book ought to tick,
    which is the move ``_aggregate_of_an_undeclared_stream`` refuses one indirection out. The
    mitigation is that the number is read *relative to a claim the caller made*: widening the
    window weakens the claim as fast as it raises the score, and the age is recoverable from
    the emitted record by anyone who wants to re-grade it against a window of their own.

    That last clause was false for as long as it stood here, and it is the whole of what
    makes this denominator tolerable, so it is worth saying exactly what carries it.
    ``DepthProfile.local_ts`` is the *requested* instant — the slice re-stamps it — and
    ``source_ts`` is the venue's own clock, a different number wherever both exist and
    ``None`` for every Coinbase book. Neither is the stamp ``age_ns`` was measured from.
    ``DepthProfile.snapshot_ts`` is, and ``age_ns = local_ts - snapshot_ts`` reads off the
    row. Until that field existed a caller passing ``--max-age-ns 86400e9`` against an
    hour-old book got ``prov_confidence=0.958`` and no way to find the hour.

    **Why the terms multiply.** They are independent ways the same answer fails to be the
    book at the instant asked for — a ladder can be short, stale, or both — and a slice that
    is both is worse than either, which is what a product says and a mean does not. It is the
    same reasoning ``ohlcv_from_ohlcv`` gives for multiplying extent by adequacy, applied to
    depth and to time instead of to two halves of one window.

    **What is deliberately not an input.** Whether the stored snapshot was the venue's whole
    book or a truncated top-N is not observable from the record: ``BookSnapshot`` carries the
    levels it carries and no flag saying whether anything was cut, and a collector that
    subscribed twenty levels writes exactly what a venue that publishes twenty writes.
    Scoring it would mean guessing, and the fill term already measures the only consequence
    either case has — a stored ten asked for twenty-five scores 0.4 whichever of the two
    trimmed it.

    :attr:`Provenance.DERIVED`, the same level ``alpaca_l1`` and ``book_resample`` carry and
    the same one the equity half of ``depth`` declares. Every price and size in the ladder
    was published by the venue in a real book, so nothing here is modelled — which is what
    keeps it out of :attr:`Provenance.SYNTHETIC`, where ``yahoo_1m_vap`` sits because traded
    volume stands in for resting size and ``amm_tick_curve`` sits because liquidity in range
    stands in for orders anyone placed. Saturating the fill term at the requested depth says
    the ladder is as full as the store can make it, not that a slice has become a venue
    product; that claim is ``prov``'s, and it stays ``DERIVED`` at every value.
    """
    n_levels = _require_int(inputs, "n_levels")
    n_requested = _require_int(inputs, "n_requested")
    age_ns = _require_int(inputs, "age_ns")
    window_ns = _require_int(inputs, "window_ns")
    if n_levels < 0:
        raise ConfidenceInputError(f"input 'n_levels' must be non-negative, got {n_levels}")
    if n_requested <= 0:
        raise ConfidenceInputError(f"input 'n_requested' must be positive, got {n_requested}")
    if window_ns <= 0:
        raise ConfidenceInputError(f"input 'window_ns' must be positive, got {window_ns}")
    if age_ns < 0:
        raise ConfidenceInputError(
            f"input 'age_ns' must be non-negative, got {age_ns}; a snapshot stamped after "
            f"the instant the slice claims to describe is lookahead rather than staleness, "
            f"and there is no confidence at which it is a legal record"
        )
    if age_ns > window_ns:
        raise ConfidenceInputError(
            f"input 'age_ns' ({age_ns}) exceeds 'window_ns' ({window_ns}); a snapshot older "
            f"than the window the caller declared should not have been admitted at all"
        )
    fill = min(n_levels / n_requested, 1.0)
    freshness = 1.0 - age_ns / window_ns
    return fill * freshness
@register_basis(
    "treasury_carry",
    level=Provenance.DERIVED,
    inputs=["trade", "ohlcv", "index_value", "corp_action", "options_chain", "macro_series"],
)
def _treasury_carry(inputs: Mapping[str, Any]) -> float:
    """A spread carried to an annual rate net of a published yield: how fresh the yield is.

    ``max(1 - yield_age_ns / horizon_ns, 0)``, where

    * ``yield_age_ns`` is how long before the price pair the Treasury quote being
      subtracted was published;
    * ``horizon_ns`` is the period the carry is annualised over — time to expiry for a
      dated future, time to the next dividend event for a holding cost.

    **What this used to have, and why it does not.** It was
    ``(n_price_legs + freshness) / 3``, three legs averaged, "two of them binary, one of
    them graded". The two binary ones never moved. ``n_price_legs`` had no call site: it
    was a keyword-only parameter of ``_carry_confidence`` with a default of ``2`` that all
    three carry functions took, because a row of any of them cannot be built without both
    price legs — the join that produces it drops anything unpaired. So the term was the
    constant 2 and the score had a floor of 0.667 that no data could push it below. A
    ``spot-future-basis`` row with ``risk_free_rate=None`` and ``carry_pct=None`` — no
    financing leg at all, which is the whole of what this basis measures — reported 0.667
    and passed the ``>= 0.5`` consumer filter this module cites elsewhere as the reason
    ``ohlcv_from_ohlcv`` changed denominators. It also made two different rows
    indistinguishable: one price leg with a perfect yield and two price legs with no yield
    both scored 0.667.

    That is precisely what ``book_resample`` was demoted for one screen down: "a constant
    spelled as a division is a constant in the one place no call-site gate looks". The
    difference is that ``book_resample`` had nothing left to measure and became a declared
    constant, while this still has the term that was doing all the work. So the constant
    terms are removed rather than the formula, and what is left says what it measures: a
    carry is a spread minus a financing rate, both price legs are structurally present in
    any emitted row, and the only thing that varies is how much of the priced period the
    subtracted yield actually speaks to. An absent yield is now 0.0 rather than 0.667,
    which is the honest reading of a carry with no financing leg.

    Counting the legs for real was the alternative and it measures nothing: there is no
    reachable state in which a row exists with fewer than two. It would also have been
    wrong where it was loudest — ``equity_funding_apr`` passed ``n_price_legs=2`` for a row
    that is a dividend and a share price and has no derivative leg at all.

    **Why the horizon is the denominator, and why nothing else could be.** The temptation
    is to divide the yield's age by the Treasury's publication interval — one business day
    — which would say a same-session quote is perfect and a Monday quote used on Wednesday
    is two-thirds stale. That number is about the *feed*, and this registry has refused
    that shape before: ``_aggregate_of_an_undeclared_stream`` declines to invent a
    denominator for how often a stream ought to tick. The rate here is not being reported,
    it is being *applied across a horizon*, and the horizon is declared by the caller from
    the contract's own expiry. A quote published a day before a 90-day carry misses a
    ninetieth of what it is being used to price; the same quote against a two-day carry
    misses half of it. Same staleness, different consequence, and the consequence is what
    a consumer filtering on this number needs.

    Saturating at zero rather than going negative says a yield older than the whole
    horizon speaks to none of the period being priced — the same information content as no
    yield at all, which is why an absent leg is passed in as ``yield_age_ns ==
    horizon_ns`` rather than given a second term of its own. Absence and total staleness
    are the same claim here, and a second term would let them disagree.

    :attr:`Provenance.DERIVED` and not :attr:`Provenance.SYNTHETIC`. Every number that
    goes in was reported by somebody: the derivative's price by its venue, the cash leg by
    its source, and the par yield by the US Treasury. Nothing is modelled from a different
    data class, which is the line ``yahoo_1m_vap`` and ``amm_tick_curve`` sit the other
    side of. Not :attr:`Provenance.NATIVE` either, and this is the whole reason the basis
    exists: no venue publishes an equity carry, and the header default would have said one
    did.

    The honest caveat, stated because the basis name invites it: a Treasury par yield is a
    *proxy* for the rate an actual carry trade finances at, which is a repo rate and is
    not published keylessly. The emitted rows carry ``risk_free_rate``,
    ``risk_free_tenor_days`` and ``risk_free_date`` beside the carry, so which published
    number was subtracted is on the row rather than in this docstring.
    """
    yield_age_ns = _require_int(inputs, "yield_age_ns")
    horizon_ns = _require_int(inputs, "horizon_ns")
    if yield_age_ns < 0:
        raise ConfidenceInputError(
            f"input 'yield_age_ns' must be non-negative, got {yield_age_ns}; a yield "
            f"published after the price pair it is subtracted from is not a staleness"
        )
    if horizon_ns <= 0:
        raise ConfidenceInputError(f"input 'horizon_ns' must be positive, got {horizon_ns}")
    return max(1.0 - yield_age_ns / horizon_ns, 0.0)


_TRUST_ORDER: Final[tuple[Provenance, ...]] = (
    Provenance.NATIVE,
    Provenance.DERIVED,
    Provenance.SYNTHETIC,
    Provenance.UNAVAILABLE,
)
"""The levels from most to least trustworthy, which is what makes "worst" well defined."""


def trust_rank(level: Provenance) -> int:
    """Return ``level``'s position in the trust order — higher is less trustworthy.

    The same ordering :func:`worst_provenance` reduces over, exposed for the callers that
    have to express "the worst of these" somewhere a Python ``max()`` cannot reach: a
    Polars aggregation over a bar frame takes a numeric column, not a list of enums. A
    second copy of the order written out at such a call site is a second place for it to
    disagree with this one.
    """
    return _TRUST_ORDER.index(level)


def worst_provenance(levels: Iterable[Provenance]) -> Provenance:
    """Return the least trustworthy level in ``levels``.

    A derivation can never be more trustworthy than its worst input, and the level a basis
    registers is a ceiling rather than a measurement — ``Impl.prov`` says the same thing
    about implementations. Without this, re-bucketing quote-derived bars produced
    ``prov=derived`` over prices that were never transacted, and a caller filtering
    ``WHERE prov != 'synthetic'`` got them back with nothing to notice.

    Raises:
        ValueError: if ``levels`` is empty. There is no worst of nothing, and returning
            NATIVE for it would be the laundering this function exists to stop.
    """
    materialised = list(levels)
    if not materialised:
        raise ValueError("worst_provenance() needs at least one level; there is no worst of none")
    return max(materialised, key=_TRUST_ORDER.index)


_FORM4_NOTIONAL_BOXES: Final[int] = 2
"""The two Form 4 Table I boxes a USD notional needs: shares and price per share.

Scoped to one basis like ``_YAHOO_1M_VAP_SESSION_BARS`` and ``_L1_QUOTED_SIDES`` above.
It is a fact about the form, not about disclosure in general — a 13F information table
has no price box at all, which is why it is scored on something else entirely.
"""

_FORM_13F_DISCLOSURE_WINDOW_DAYS: Final[int] = 45
"""Days Rule 13f-1(a) allows between the quarter a table describes and its filing.

Scoped the same way. It is the one denominator in this registry that a regulator wrote
down rather than a market produced, which is the whole reason ``sec_13f_hr`` is allowed
to measure a lag where ``book_resample`` refused to.
"""


@register_basis("sec_form4", level=Provenance.NATIVE, inputs=["insider"])
def _sec_form4(inputs: Mapping[str, Any]) -> float:
    """An insider's reported transaction: how much of the notional the line actually states.

    ``n_reported_amounts / 2``, where the two are Table I's ``transactionShares`` and
    ``transactionPricePerShare``. Both are independently omissible and both are genuinely
    omitted: a gift (code ``G``) and an award (code ``A``) carry shares and no price, and a
    holding-only line carries neither. ``whale-alerts`` thresholds on a USD notional, so a
    line stating shares and no price is half of what the measurement it feeds requires — the
    same shape as ``alpaca_l1``'s ``n_quoted_sides / 2``, and the same kind of denominator:
    the form has those two boxes whether or not a filer fills them.

    :attr:`Provenance.NATIVE`, not :attr:`Provenance.DERIVED`. Section 16(a) makes the
    *insider* the reporter and the SEC the publisher, so the shares, the price and the date
    are read off a filed document rather than reconstructed from anything. The confidence
    grades sampling **within** that level, which is what lets a natively-reported line score
    0.5 without any suggestion that the numbers on it are modelled.

    **Why the two-business-day rule is not what is scored here, when it is what
    ``sec_13f_hr`` scores.** Rule 16a-3(g) gives an insider two business days, and it is
    tempting to read a late Form 4 as a worse sample. It is not, because the record carries
    ``transaction_date``: the event's own instant is *on the row*, so a consumer asking "what
    moved on the third" gets the right answer from a filing that landed on the tenth. Lateness
    costs a reader latency, not sampling. A 13F carries no date for anything it reports, which
    is exactly why the lag is a sampling deficiency there and only a delay here.
    """
    n = _require_int(inputs, "n_reported_amounts")
    if not 0 <= n <= _FORM4_NOTIONAL_BOXES:
        raise ConfidenceInputError(
            f"input 'n_reported_amounts' must be between 0 and {_FORM4_NOTIONAL_BOXES}, got {n}"
        )
    return n / _FORM4_NOTIONAL_BOXES


@register_basis("sec_13f_hr", level=Provenance.NATIVE, inputs=["holding_13f"])
def _sec_13f_hr(inputs: Mapping[str, Any]) -> float:
    """A quarter-end position: how much of the statutory 45 days it was withheld for.

    ``max(1 - disclosure_lag_days / 45, 0.0)``, where ``disclosure_lag_days`` counts calendar
    days from the quarter end the table describes to the day the filing became public. A table
    filed the day after quarter end was invisible for one of the days Rule 13f-1 permits it to
    be invisible and scores 44/45; one filed at the deadline scores 0.0, and an amendment
    landing later clamps there.

    **Why this is a sampling measure and not a complaint about lateness.**
    ``_book_resample`` in this same registry declines to score staleness, and its two reasons
    are the right test to apply. The first: "absence of updates is not absence of sampling" —
    a quiet book genuinely did not change. That leg fails here. A withheld 13F describes a
    portfolio that *existed* through the whole interval and that no one outside the manager
    could observe; the position is not quiet, it is hidden, and an interval in which a real
    fact was unobservable is unsampled by any reading of the word. The second: scoring it
    "would need a reference for how often a book ought to tick, and no such reference
    exists". That leg fails too, and more plainly — Rule 13f-1(a) wrote the reference down.
    45 days is not a denominator invented to make a constant look measured; it is the window
    a regulator set, and it exists whether or not any filer uses it, in the way a regular US
    session contains 390 one-minute bars whether or not ``yahoo_1m_vap`` fetched any.

    **Why the lag and not the form's own boxes**, which is what ``sec_form4`` scores. An
    information table's Column 5 value and Column 4 share count are mandatory and are
    present on essentially every row; there is nothing partial about them to grade. What is
    partial is the *time* the row speaks for: the table states a position at one instant and
    publishes no date for a single one of the trades that built it, so a quarter of activity
    arrives as one undated number. That is the deficiency worth measuring, and the lag is the
    part of it that is observable per record.

    0.0 at the deadline is the reading ``unavailable`` and ``scraped_last_price`` carry and
    means what it means there: no sampling evidence — here, none *for the present*. It is not
    a claim that the position is false. The claim that a filer really reported these numbers
    is ``prov``'s, and it stays :attr:`Provenance.NATIVE` at every value.
    """
    lag = _require_int(inputs, "disclosure_lag_days")
    if lag < 0:
        raise ConfidenceInputError(f"input 'disclosure_lag_days' must be non-negative, got {lag}")
    return max(1.0 - lag / _FORM_13F_DISCLOSURE_WINDOW_DAYS, 0.0)


@register_basis("book_resample", level=Provenance.DERIVED, inputs=["book_snapshot", "book_delta"])
def _book_resample(_: Mapping[str, Any]) -> float:
    """Resampled book depth: 1.0, because the capture is exact at the instant it is stamped.

    The reconstruction has nothing estimated in it. The engine replays absolute levels
    from a real venue snapshot and raises ``BookGap`` rather than guessing across a
    sequence break, so a capture at boundary *B* is the book as the stream reported it at
    *B*, level for level.

    **This was a formula, and the formula is why it stopped being one.** It read
    ``max(1 - lookahead_ns / interval_ns, 0.0)``, and its whole observable was the crypto
    resampler's own boundary rule: that rule applied the boundary-crossing record *before*
    capturing, so a snapshot stamped 10:00:00 could contain an update from 10:00:00.200 and
    scored itself 0.8 on the way into the lake. The two resamplers have been collapsed onto
    the ordering that flushes boundaries below a record *before* applying it
    (``crocodile.core.resample.book``), which makes ``lookahead_ns`` structurally zero at
    every capture — so the formula could only ever return 1.0. A number that cannot move is
    a constant, and a constant spelled as a division is a constant in the one place no
    call-site gate looks. It is declared here instead, and
    ``tests/conformance/test_gates.py::CONSTANT_BY_DEFINITION`` carries the same argument
    where Gate 3c can check it.

    Nothing was silently dropped in the trade. What used to be scored is now refused:
    ``_capture_snapshot`` raises :class:`~crocodile.core.errors.ProvenanceError` if it is
    ever handed a book holding an update stamped after the boundary. Emitting a biased bar
    at 0.0 is how they reached the lake; not building the record is the loud form.

    **The tempting replacement, and why it is not taken.** Staleness — how long before the
    boundary the last update landed — is observable and does vary. It is not a sampling
    deficiency: if no update arrives between two boundaries the book genuinely did not
    change, and absence of updates is not absence of sampling. Scoring it would need a
    reference for how often a book *ought* to tick, and no such reference exists; it would
    be a denominator invented to make a constant look measured, which is the move
    ``_aggregate_of_an_undeclared_stream`` refuses one indirection out.

    ``top_n`` truncation is not a deficiency either. A caller asking for five levels and
    receiving the best five has been answered, not under-sampled; the emitted ``depth``
    says how many levels the record carries.

    1.0 is a claim about sampling within this level, not about the record being a venue
    product. That claim is ``prov``'s, and it stays :attr:`Provenance.DERIVED`.
    """
    return 1.0
