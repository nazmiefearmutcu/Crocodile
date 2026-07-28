"""The rules that keep the two symmetry ledgers honest, and stop either becoming a hiding place.

Phase 2 registered 49 capabilities — 47 ported by the four batch modules plus the two the
parity gate found at its exit — whose crypto halves worked and whose equity halves arrived
in Phase 3. Without somewhere honest to record that, the port had only dishonest options:
leave them out of the registry (and the surfaces stop being projections of it), or park
them on ``IRREDUCIBLE`` (and claim the market cannot have an equity analogue, which is
false and permanent).

``PENDING_SYMMETRY`` is the honest option, and it is only honest while these hold.

``IRREDUCIBLE``'s rules live here too, beside its twin's, which is the change an exit review
forced. ``PENDING_SYMMETRY`` had a hoarding gate and ``IRREDUCIBLE`` had none — and the
hazard the hoarding gate exists for is *stronger* on the permanent list, not weaker. The
review gave ``peg-deviation`` an equity implementation at runtime and ran every gate that
mentions ``IRREDUCIBLE``: all of them passed, over a capability that had just stopped being
irreducible. A stale entry there means a regression to asymmetry is invisible forever,
because the name was already excused forever.

That review then declared the hole closed, and a later referee showed it was not. Deleting
the equity half of ``iv-surface`` and writing the name onto ``IRREDUCIBLE`` in the same
breath passed all 778 conformance tests, and so did the same pair of edits over ``ofi``,
``census``, ``whale-alerts``, ``open-interest`` and ``liquidity-depth``. The gate the review
added fires on ``set(cap.impls) == {CRYPTO, EQUITY}`` *and* a name on ``IRREDUCIBLE``, which
is the state the reviewer produced by hand — adding an implementation to an already-excused
name. Deleting first and excusing second never enters it. The gate was written for the
experiment, not for the defect.

So the load moved off "which excuse was reached for" and onto two facts nothing can restate:

- **Every exemption ledger is censused.** ``PENDING_SYMMETRY`` had one and it worked — it is
  why the delete-then-*schedule* route was caught while delete-then-excuse was not — so all
  of them have one now, including the two this file adds, and a meta-gate fails if a ledger
  appears in the registry module without a census here. A gate shipped together with the
  exemption it suggests is the shape this codebase keeps re-finding.
- **The asymmetry frontier itself is pinned.** Which capabilities serve which asset classes
  is the property the ledgers exist to qualify, so it is asserted directly. That census does
  not care which ledger a deletion is laundered through, or whether it is laundered at all,
  or whether the ledger it uses had been invented yet.

A third ledger arrives with them, and for a defect of the same family: ``SHARED_IMPLEMENTATION``
records the capabilities where one function legitimately answers for both asset classes, so
that everywhere else two dict keys may not point at one callable. See its rules below.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping

import msgspec
import pytest

from crocodile.core import capability
from crocodile.core.capability import (
    IRREDUCIBLE,
    PENDING_SYMMETRY,
    REGISTRY,
    SHARED_IMPLEMENTATION,
    SPEC_METHODS,
    AssetClass,
    Capability,
    Impl,
    ReturnKind,
    register,
)
from crocodile.core.schema.provenance import Provenance


class _Params(msgspec.Struct, frozen=True):
    symbol: str


@pytest.fixture
def _isolate() -> Iterator[None]:
    """Registry and both ledgers are module state; a leak would order-couple gates."""
    registry, pending = dict(REGISTRY), dict(PENDING_SYMMETRY)
    irreducible = dict(IRREDUCIBLE)
    builtins = set(capability._DECLARED_NAMES)
    try:
        yield
    finally:
        REGISTRY.clear()
        REGISTRY.update(registry)
        PENDING_SYMMETRY.clear()
        PENDING_SYMMETRY.update(pending)
        IRREDUCIBLE.clear()
        IRREDUCIBLE.update(irreducible)
        capability._DECLARED_NAMES.clear()
        capability._DECLARED_NAMES.update(builtins)


def _crypto_only(name: str) -> Capability:
    return Capability(
        name=name,
        summary="Crypto today, equity in Phase 3.",
        params=_Params,
        returns=ReturnKind.TABLE,
        impls={AssetClass.CRYPTO: Impl(fn=len, prov=Provenance.NATIVE, basis="native")},
    )


def _symmetric(name: str) -> Capability:
    return Capability(
        name=name,
        summary="Both asset classes, today.",
        params=_Params,
        returns=ReturnKind.TABLE,
        impls={
            AssetClass.CRYPTO: Impl(fn=len, prov=Provenance.NATIVE, basis="native"),
            AssetClass.EQUITY: Impl(fn=len, prov=Provenance.NATIVE, basis="native"),
        },
    )


# ---------------------------------------------------------------------------
# The ledger's own rules
# ---------------------------------------------------------------------------


def test_every_entry_names_a_method_that_was_actually_specified() -> None:
    """A deadline pointing at a method nobody wrote down is a deadline nobody owns."""
    unknown = {
        name: method for name, method in PENDING_SYMMETRY.items() if method not in SPEC_METHODS
    }
    assert not unknown, (
        f"scheduled against methods that do not exist in design §9.1: {unknown}. "
        f"Known methods: {sorted(SPEC_METHODS)}"
    )


def test_no_capability_is_both_scheduled_and_irreducible() -> None:
    """They are opposite claims: "coming in Phase 3" and "can never exist"."""
    both = sorted(set(PENDING_SYMMETRY) & set(IRREDUCIBLE))
    assert not both, f"{both} claim both that an equity analogue is coming and that it cannot"


def test_the_ledger_only_holds_capabilities_that_exist() -> None:
    """An entry for an unregistered name is a deadline against nothing."""
    ghosts = sorted(set(PENDING_SYMMETRY) - set(REGISTRY))
    assert not ghosts, f"scheduled but never registered: {ghosts}"


def assert_asymmetry_is_scheduled(names: Iterable[str]) -> None:
    """The rule each capability batch files under: crypto-only means scheduled, with a method.

    Exported rather than written out per batch because the batches have run out of subject.
    Phase 3 emptied ``PENDING_SYMMETRY``, so every batch's "not symmetric" branch now has an
    empty name set — ``tests/capabilities/test_analytics.py`` parametrised 17 cases over a
    branch none of them took, and ``test_market.py`` looped over a dict it had just asserted
    was empty. Two shapes of the same thing: a rule whose only remaining subject is nothing.

    Deleting the rule is wrong — it is the rule that makes an unscheduled asymmetric
    capability a build failure, and Phase 4 will declare capabilities. Keeping it as a
    per-batch loop over an empty set is also wrong, because it reads as coverage. So it
    lives here as one function, and the self-tests below drive it with a fixture capability
    through ``_isolate``, which is how this module has tested every other empty-subject rule
    since the exit review.
    """
    for name in names:
        cap = REGISTRY.get(name)
        assert cap is not None, f"{name} is not registered, so nothing about it is asserted"
        assert set(cap.impls) == {AssetClass.CRYPTO}, (
            f"{name} was treated as crypto-only and implements {sorted(cap.impls)}; if the "
            f"equity half landed, move the name onto its batch's symmetric list in the same "
            f"commit"
        )
        method = PENDING_SYMMETRY.get(name)
        assert method in SPEC_METHODS, (
            f"{name} has only a crypto half and is scheduled against {method!r}, which is "
            f"not a method in design §9.1. Known methods: {sorted(SPEC_METHODS)}"
        )


def test_the_ledger_is_not_hoarding_capabilities_that_became_symmetric() -> None:
    """The same rule Phase 1 put on its dropped-names list.

    A name whose equity half has landed must leave, or the ledger drifts into a place
    where completed work goes to look unfinished — and, worse, where a *regression* to
    asymmetry would be invisible because the name was already excused.
    """
    settled = sorted(
        name
        for name in PENDING_SYMMETRY
        if (cap := REGISTRY.get(name)) is not None
        and set(cap.impls) == {AssetClass.CRYPTO, AssetClass.EQUITY}
    )
    assert not settled, f"these are symmetric now and no longer need scheduling: {settled}"


# ---------------------------------------------------------------------------
# The same two rules on IRREDUCIBLE, which had neither
# ---------------------------------------------------------------------------


def test_the_irreducible_list_only_holds_capabilities_that_exist() -> None:
    """An exemption for an unregistered name excuses nothing and hides a deletion.

    The mirror of :func:`test_the_ledger_only_holds_capabilities_that_exist`, and it is the
    same hazard the ``_INFRASTRUCTURE`` pin closes one file over: a ledger whose entries are
    only ever *checked against what is registered* reads a deleted capability as a satisfied
    rule. ``gas-tracker`` is the precedent — it sat here naming a capability nobody had
    declared, and being unregistered is exactly what let it sit.
    """
    ghosts = sorted(
        name
        for name in IRREDUCIBLE
        if name not in REGISTRY and not any(name in c.aliases for c in REGISTRY.values())
    )
    assert not ghosts, (
        f"excused as irreducible but not registered: {ghosts}. IRREDUCIBLE says an equity "
        f"analogue cannot exist for a capability this product serves; with nothing "
        f"registered under the name it says nothing at all, and it would go on saying "
        f"nothing after the crypto half was deleted too."
    )


def test_the_irreducible_list_is_not_hoarding_capabilities_that_became_symmetric() -> None:
    """The hoarding rule its twin has had all along, and needs more because it is permanent.

    ``PENDING_SYMMETRY`` entries are meant to leave; the review that added this found that
    ``IRREDUCIBLE`` entries had no way to. Giving ``peg-deviation`` an equity implementation
    at runtime left ``test_gate2_every_capability_is_symmetric``,
    ``test_gate2_irreducible_entries_carry_a_justification``,
    ``test_no_capability_is_both_scheduled_and_irreducible`` and the ledger's own hoarding
    test all green — because the first three ``continue`` past an excused name and the
    fourth reads the other ledger.

    A capability with both halves is a live disproof of the claim its entry makes. Leaving
    the entry means the next commit that *removes* the equity half is invisible, which is
    the regression the twin's docstring names — with the difference that this list never
    expires, so the invisibility does not either.
    """
    disproved = sorted(
        name
        for name in IRREDUCIBLE
        if (cap := REGISTRY.get(name)) is not None
        and set(cap.impls) == {AssetClass.CRYPTO, AssetClass.EQUITY}
    )
    assert not disproved, (
        f"{disproved} are implemented for both asset classes while IRREDUCIBLE claims no "
        f"equity analogue can exist. The implementation is the answer; delete the entry."
    )


def test_the_spec_methods_are_the_set_that_was_argued_for() -> None:
    """Pinning the set stops a new letter being invented to buy more time.

    Renamed from ``..._the_seven_the_design_committed_to``: it is eight. M8 was added
    when porting the surfaces found the design's unstated assumption — that a gap always
    runs equity-ward — and its counterexample, ``depth``, whose missing half is the crypto
    one. The count moving is exactly what this test exists to make someone justify, so
    the number lives here rather than in the name, where it read as a fact rather than
    as a thing being asserted.
    """
    assert sorted(SPEC_METHODS) == ["M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8"]
    for method, what in SPEC_METHODS.items():
        assert what.strip(), f"{method} carries no description"


# ---------------------------------------------------------------------------
# The mechanism, exercised — the ledger is empty today, so the branch it controls
# would otherwise never run under test. A gate whose only path is the one nothing
# takes is the vacuous-green shape this merge keeps finding.
# ---------------------------------------------------------------------------


def test_an_unscheduled_asymmetric_capability_fails_gate_2(_isolate: None) -> None:
    from tests.conformance.test_gates import test_gate2_every_capability_is_symmetric

    register(_crypto_only("fixture-unscheduled"))
    with pytest.raises(AssertionError, match="fixture-unscheduled"):
        test_gate2_every_capability_is_symmetric()


def test_a_scheduled_asymmetric_capability_passes_gate_2(_isolate: None) -> None:
    from tests.conformance.test_gates import test_gate2_every_capability_is_symmetric

    register(_crypto_only("fixture-scheduled"))
    PENDING_SYMMETRY["fixture-scheduled"] = "M1"
    test_gate2_every_capability_is_symmetric()


def test_scheduling_against_an_invented_method_is_caught(_isolate: None) -> None:
    PENDING_SYMMETRY["fixture-scheduled"] = "M9"
    with pytest.raises(AssertionError, match="do not exist"):
        test_every_entry_names_a_method_that_was_actually_specified()


def test_a_name_on_both_lists_is_caught(_isolate: None) -> None:
    """The fixture is any live IRREDUCIBLE name; it used to be `gas-tracker`.

    That entry was removed — it opened a Qt window, so it was a launcher rather than a
    capability, and an exemption list that covers one makes the list mean two things. The
    command itself has since been deleted along with the rest of the desktop layer, which
    is why the fixture cannot be a live name from that side any more.
    `mev-sandwich` stands in because it is what IRREDUCIBLE is actually for: it needs a
    public mempool, which is a property of the market and not of the schedule.
    """
    PENDING_SYMMETRY["mev-sandwich"] = "M1"
    with pytest.raises(AssertionError, match="mev-sandwich"):
        test_no_capability_is_both_scheduled_and_irreducible()


def test_a_settled_entry_left_behind_is_caught(_isolate: None) -> None:
    PENDING_SYMMETRY["indicators"] = "M1"
    with pytest.raises(AssertionError, match="indicators"):
        test_the_ledger_is_not_hoarding_capabilities_that_became_symmetric()


def test_a_ledger_entry_naming_nothing_registered_is_caught(_isolate: None) -> None:
    """The one ledger rule that had no self-test, and now has no real iterations either.

    Its twin on ``IRREDUCIBLE`` has had ``test_an_irreducible_entry_naming_nothing_
    registered_is_caught`` since the exit review; this side was left with a set difference
    over an empty ledger, which is ``set() - set(REGISTRY)`` — a comprehension that cannot
    produce an element. That is the same "green because it did not run" shape the rest of
    this section exists to close.
    """
    PENDING_SYMMETRY["fixture-never-declared"] = "M1"
    with pytest.raises(AssertionError, match="fixture-never-declared"):
        test_the_ledger_only_holds_capabilities_that_exist()


def test_an_unscheduled_asymmetric_capability_fails_its_batch_rule(_isolate: None) -> None:
    """:func:`assert_asymmetry_is_scheduled` over the state no batch can produce today."""
    register(_crypto_only("fixture-batch-unscheduled"))
    with pytest.raises(AssertionError, match="fixture-batch-unscheduled"):
        assert_asymmetry_is_scheduled(["fixture-batch-unscheduled"])


def test_a_scheduled_asymmetric_capability_satisfies_its_batch_rule(_isolate: None) -> None:
    register(_crypto_only("fixture-batch-scheduled"))
    PENDING_SYMMETRY["fixture-batch-scheduled"] = "M1"
    assert_asymmetry_is_scheduled(["fixture-batch-scheduled"])


def test_a_batch_rule_rejects_a_schedule_against_an_invented_method(_isolate: None) -> None:
    register(_crypto_only("fixture-batch-invented"))
    PENDING_SYMMETRY["fixture-batch-invented"] = "M9"
    with pytest.raises(AssertionError, match="M9"):
        assert_asymmetry_is_scheduled(["fixture-batch-invented"])


def test_a_batch_rule_rejects_a_name_that_grew_its_equity_half(_isolate: None) -> None:
    """The direction that catches a stale entry on a batch's crypto-only side.

    A capability whose equity half landed while the batch still lists it as asymmetric is
    the mirror of the hoarding failure the ledger's own rule catches, and it is the one a
    per-name loop could not report while there were no crypto-only names left to loop over.
    """
    REGISTRY["fixture-batch-grown"] = _symmetric("fixture-batch-grown")
    PENDING_SYMMETRY["fixture-batch-grown"] = "M1"
    with pytest.raises(AssertionError, match="fixture-batch-grown"):
        assert_asymmetry_is_scheduled(["fixture-batch-grown"])


def test_a_batch_rule_rejects_a_name_nothing_registered(_isolate: None) -> None:
    """A batch list that names a deleted capability asserts nothing about it, loudly."""
    with pytest.raises(AssertionError, match="fixture-batch-deleted"):
        assert_asymmetry_is_scheduled(["fixture-batch-deleted"])


def test_an_irreducible_capability_that_grew_an_equity_half_is_caught(_isolate: None) -> None:
    """The reviewer's experiment, run as a test rather than by hand.

    ``peg-deviation`` is the name they used and it is used here for the same reason: it is a
    live ``IRREDUCIBLE`` entry with a real crypto implementation, so nothing about the setup
    is contrived except the equity half.
    """
    REGISTRY["peg-deviation"] = _symmetric("peg-deviation")
    with pytest.raises(AssertionError, match="peg-deviation"):
        test_the_irreducible_list_is_not_hoarding_capabilities_that_became_symmetric()


def test_an_irreducible_entry_naming_nothing_registered_is_caught(_isolate: None) -> None:
    """What ``gas-tracker`` looked like while it sat here, and what a deletion looks like."""
    IRREDUCIBLE["fixture-deleted"] = "A justification for a capability nobody declared."
    with pytest.raises(AssertionError, match="fixture-deleted"):
        test_the_irreducible_list_only_holds_capabilities_that_exist()


def test_the_four_gates_the_review_ran_no_longer_all_pass(_isolate: None) -> None:
    """The experiment's actual shape: four gates run together, all four green.

    Kept as one test because the finding was about the *set* — each of the four had a
    defensible reason to pass, and the conclusion was only visible when they were run
    together. This asserts the set is no longer unanimous.
    """
    from tests.conformance.test_gates import (
        test_gate2_every_capability_is_symmetric,
        test_gate2_irreducible_entries_carry_a_justification,
    )

    REGISTRY["peg-deviation"] = _symmetric("peg-deviation")

    test_gate2_every_capability_is_symmetric()
    test_gate2_irreducible_entries_carry_a_justification()
    test_no_capability_is_both_scheduled_and_irreducible()
    test_the_ledger_is_not_hoarding_capabilities_that_became_symmetric()

    with pytest.raises(AssertionError, match="peg-deviation"):
        test_the_irreducible_list_is_not_hoarding_capabilities_that_became_symmetric()


# ---------------------------------------------------------------------------
# Phase 3's exit criterion, asserted here so it cannot be forgotten there.
# ---------------------------------------------------------------------------

_LEDGER_AS_SHIPPED: dict[str, str] = {}
"""The ledger is empty, and this pin is what makes the emptiness an assertion.

Phase 2 closed with 21 entries. Phase 3 emptied it one method at a time, each name leaving
in the same commit that gave its capability the implementation it was missing: the options
family and ``open-interest`` off M1 and M2; ``whale-alerts``, ``smart-money`` and
``label-transfers`` off M4, which left together because the method that closes them is one
piece of work; the five spread capabilities off M5; ``liquidity-depth`` and ``chaos-score``
off M6 and ``ofi`` off M7; ``depth`` off M8, the one whose missing half ran crypto-ward; and
last ``markets``, ``universe``, ``census`` and ``collect-market`` off M3, all four of which
waited on one piece of reference data and left when it arrived.

That one-commit rule is the whole mechanism: delete a name from a batch module alone and
this pin names a capability that is no longer scheduled; delete it from here alone and a
schedule survives that nothing records.
:func:`test_the_ledger_holds_exactly_the_entries_it_was_pinned_to_hold` fails in both
directions, so a half-done deletion is as red as an undeclared addition.

``PENDING_SYMMETRY``'s own docstring says "the count only ever moving in those two
directions is the property worth watching", and until an exit review looked, nothing was
watching. Two things made that hard to see. The declaration in ``core/capability.py`` reads
``PENDING_SYMMETRY = {}``, so the module a reviewer opens shows an empty dict while three
batch modules assembled 21 live entries into it at import time. And
:func:`test_phase_3_exit_the_ledger_must_be_empty` skipped whenever the ledger was
non-empty, so it was green at 21 and would have been green at 48. That skip was then
conditioned on this pin — which fixed the growth case and left one hole open at the far
end, because an empty ledger equals an empty pin and the test would have skipped at the
finish line too. See that function.

No count is written into this docstring, on purpose. Several agents emptied this dict in
parallel and a sentence naming a number is a merge conflict that resolves to a lie — this
paragraph said "It is 16", then "It is 10", then "It is 7" across three merges, each true
for exactly one commit. The number that matters is the length of the literal above, which
is what the tests read.

Pinned here rather than beside the declaration for the reason the batches write it here:
``core/capability.py`` is shared and this is a test's assertion about a fact, not a fact.
"""


def test_the_ledger_holds_exactly_the_entries_it_was_pinned_to_hold() -> None:
    """A schedule that can grow silently is an exemption list with better manners.

    Both directions are failures worth a diff. A new entry is a capability whose equity half
    somebody decided not to write, which is a decision and not a detail; a departed entry is
    Phase 3 delivering, which is the outcome the ledger exists to reach. Neither should
    happen without this list changing in the same commit.
    """
    unpinned = sorted(set(PENDING_SYMMETRY) - set(_LEDGER_AS_SHIPPED))
    added = {name: PENDING_SYMMETRY[name] for name in unpinned}
    assert not added, (
        f"scheduled since Phase 2 closed and unrecorded here: {added}. Deferring an equity "
        f"half is allowed and deferring it quietly is not — record it, with the method."
    )

    settled = sorted(set(_LEDGER_AS_SHIPPED) - set(PENDING_SYMMETRY))
    assert not settled, (
        f"{settled} left PENDING_SYMMETRY; if their equity halves landed, delete them here "
        f"too so the remaining count keeps meaning what it says."
    )

    remapped = {
        name: (was, PENDING_SYMMETRY[name])
        for name, was in _LEDGER_AS_SHIPPED.items()
        if name in PENDING_SYMMETRY and PENDING_SYMMETRY[name] != was
    }
    assert not remapped, (
        f"rescheduled against a different method: {remapped}. A method is the plan that "
        f"closes the gap; swapping it is a re-plan, not a correction."
    )


def test_phase_3_exit_the_ledger_must_be_empty() -> None:
    """The exit criterion, asserted — and the skip that would have swallowed it, removed.

    This test has now been wrong twice in the same direction, which is why the history is
    kept rather than the fix alone.

    The version Phase 2 shipped skipped on ``if PENDING_SYMMETRY``. That made it unfailable:
    green at 21 entries and equally green at 48, so the one test named for Phase 3's exit
    criterion could not report the ledger moving *away* from it. An exit review caught that
    and conditioned the skip on ``PENDING_SYMMETRY == _LEDGER_AS_SHIPPED``, so the only
    silencing state was one a reviewer had agreed to.

    That closed the growth case and left the far end open, and the far end is here. An empty
    ledger equals an empty pin, so ``{} == {}`` is true and the conditioned skip fires at the
    finish line — the test would have abstained on exactly the state it was written to
    assert, reporting "0 capabilities still scheduled" as a skip rather than as a pass. The
    same shape both times: a gate that is green because it did not run.

    So there is no skip. While the ledger is non-empty and matches its pin, that is a *fail*
    with a legible message, which is the honest report of "Phase 3 is not finished" — a
    schedule is not a reason for its own deadline to stay quiet. What retires the schedule is
    an assertion that has always been able to fail.
    """
    assert not PENDING_SYMMETRY, (
        f"{len(PENDING_SYMMETRY)} capabilities are still scheduled: "
        f"{', '.join(f'{k}→{v}' for k, v in sorted(PENDING_SYMMETRY.items()))}. "
        f"Phase 3 ends when this dict is empty; each name leaves in the commit that gives "
        f"its capability the implementation it was missing, and leaves _LEDGER_AS_SHIPPED "
        f"in the same one."
    )


# ---------------------------------------------------------------------------
# The three assertions above, driven through states the live ledger cannot reach.
# ---------------------------------------------------------------------------
# `PENDING_SYMMETRY` and `_LEDGER_AS_SHIPPED` are both empty, so two of the three
# comparisons in the census above read `{} - {}` and the third reads `{}.items()`.
# Only the growth branch can fire from live state, which means the two branches
# that report Phase 3 *delivering* — a name leaving, a name rescheduled — have never
# run. That is the vacuous-green shape the mechanism section below the ledger's own
# rules already answers for the gate-2 branch, applied to the census.


def test_a_departed_entry_the_pin_still_holds_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """What a half-done deletion looks like: the batch module dropped it, the pin did not."""
    monkeypatch.setitem(_LEDGER_AS_SHIPPED, "fixture-departed", "M1")
    with pytest.raises(AssertionError, match="fixture-departed"):
        test_the_ledger_holds_exactly_the_entries_it_was_pinned_to_hold()


def test_a_rescheduled_entry_is_caught(
    _isolate: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half-done edit: the name stayed, the method under it changed."""
    monkeypatch.setitem(_LEDGER_AS_SHIPPED, "fixture-remapped", "M1")
    PENDING_SYMMETRY["fixture-remapped"] = "M2"
    with pytest.raises(AssertionError, match="rescheduled against a different method"):
        test_the_ledger_holds_exactly_the_entries_it_was_pinned_to_hold()


def test_an_unpinned_new_entry_is_caught(_isolate: None) -> None:
    """The branch that could already fire, pinned so a rewrite cannot lose it."""
    PENDING_SYMMETRY["fixture-unpinned"] = "M1"
    with pytest.raises(AssertionError, match="fixture-unpinned"):
        test_the_ledger_holds_exactly_the_entries_it_was_pinned_to_hold()


# ---------------------------------------------------------------------------
# Every exemption ledger is censused, and the ledgers are discovered rather than listed
# ---------------------------------------------------------------------------


_IRREDUCIBLE_AS_SHIPPED: dict[str, str] = {
    "gas-vol": "Correlates price volatility with gas prices; gas is chain-native.",
    "mev-sandwich": "Requires a public mempool and atomic transaction ordering.",
    "sequencer-latency": "Measures an L2 sequencer; equities have no sequencer.",
    "peg-deviation": "Stablecoin peg mechanics; no equity instrument behaves this way.",
    "lending-stress": "On-chain lending-pool utilisation and liquidation thresholds.",
    "onchain-price": "An AMM pool's price is a function of two pooled reserves; no equity "
    "instrument is priced that way, and there is no chain to read one off.",
    "base-market-data": "The same pool, with its swap volume. Same argument: the volume is "
    "the pool's own swap log, which has no equity analogue either.",
}
"""The seven permanent exemptions, verbatim, so that an eighth cannot arrive quietly.

This is the census ``IRREDUCIBLE`` did not have, and its absence is the whole of the
critical finding. ``PENDING_SYMMETRY`` has had ``_LEDGER_AS_SHIPPED`` since Phase 2, which is
exactly why a referee deleting an equity half and *scheduling* the name was caught in two
tests while the same deletion *excused* on this list passed all 778. Three greps found the
asymmetry: ``IRREDUCIBLE`` was mentioned in tests only by three hand-written negative pins in
``tests/capabilities/test_ops.py``, none of which is a count.

The justifications are pinned as text and not merely as keys. A permanent exemption is
carried by its argument — which is what
:func:`~tests.conformance.test_capability.test_every_irreducible_justification_names_a_market_property`
grades and what a reviewer reads — so an entry whose argument is rewritten is a different
entry, and the diff is the only place anyone will ever see it. The same reasoning
``_LEDGER_AS_SHIPPED`` gives for pinning the *method* beside the name.

No count is written here, for the reason its twin gives: a sentence naming a number is a
merge conflict that resolves to a lie. The number that matters is the length of the literal.
"""


_SHARED_IMPLEMENTATION_AS_SHIPPED: dict[str, str] = dict(SHARED_IMPLEMENTATION)
"""The one ledger pinned by copying rather than by transcription, and why that is not a hole.

Every other census in this file restates its ledger, so that a source edit and a test edit
have to meet in review. This one cannot, and pretending otherwise would be the more
dangerous of the two options.

``SHARED_IMPLEMENTATION`` is *derived from* the registry it excuses: an entry is legitimate
exactly when the capability's two implementations really are one function, which
:func:`test_no_shared_implementation_entry_is_stale` checks against ``REGISTRY`` on every
run. A transcribed copy would therefore pin the same fact twice and go stale in a way that
says nothing — the argument that matters is checked against the code, not against a second
copy of the code.

What a copy still buys is the meta-gate: this ledger is *in* :data:`_CENSUSES`, so
:func:`test_every_ledger_in_the_registry_module_is_censused` counts it, and adding a fourth
exemption dict to ``core/capability.py`` without a census here fails. Growth of this list is
caught where growth of this list is actually dangerous — by
:func:`test_no_shared_implementation_entry_is_stale`, which refuses an entry for a capability
whose two implementations are distinct, and by the asymmetry census, which refuses the
deletion an entry here might otherwise be reached for.
"""


_SPEC_METHODS_AS_SHIPPED: dict[str, str] = {
    "M1": "Lift volsurface into core; equity chain from Yahoo, IV solved from mid if absent.",
    "M2": "Aggregate the Yahoo option chain's open_interest per underlying.",
    "M3": "Equity universe from SEC EDGAR x OpenFIGI x Tiingo, merged by CoverageResolver.",
    "M4": "Form 4 insider transactions plus a new SEC EDGAR 13F-HR parser.",
    "M5": "carry generalizes funding-apr; new keyless `treasury` provider for the risk-free leg.",
    "M6": "Equity depth from the synthetic VAP ladder, upgraded by Alpaca L1 when keyed.",
    "M7": "Order-flow imbalance derived from L1 quote changes.",
    "M8": "Crypto depth from stored book snapshots — the ladder equity models, crypto reports.",
}
"""The eight methods, with their text.

:func:`test_the_spec_methods_are_the_set_that_was_argued_for` already pins the key set and
that each description is non-empty, which is the half that stops a ninth letter being
invented. This pins the other half. A method is what ``PENDING_SYMMETRY`` schedules against,
so silently widening one — "M6: equity depth from the VAP ladder" becoming "M6: equity depth,
somehow" — would let a name keep its deadline while the plan under it changed, which is the
remap ``_LEDGER_AS_SHIPPED`` refuses one field over.
"""


_CENSUSES: dict[str, Mapping[str, str]] = {
    "IRREDUCIBLE": _IRREDUCIBLE_AS_SHIPPED,
    "PENDING_SYMMETRY": _LEDGER_AS_SHIPPED,
    "SHARED_IMPLEMENTATION": _SHARED_IMPLEMENTATION_AS_SHIPPED,
    "SPEC_METHODS": _SPEC_METHODS_AS_SHIPPED,
}
"""Every name-to-argument ledger in the registry module, and what it was pinned to hold.

Keyed by the constant's own name so :func:`test_every_ledger_in_the_registry_module_is_censused`
can compare this against what the module actually declares rather than against a list
somebody remembered to update.
"""


def _ledgers_in_the_registry_module() -> dict[str, dict[str, str]]:
    """Every ``NAME: dict[str, str]`` constant :mod:`crocodile.core.capability` declares.

    Discovered rather than enumerated, which is the point of the meta-gate: a list of
    ledgers to census is itself a list that can go stale, and the finding this file answers
    is precisely a gate that watched the ledgers somebody thought of.

    ``REGISTRY`` is excluded because its values are capabilities rather than arguments — it
    is the subject the ledgers make claims about, not a claim. It has a census of its own in
    ``test_gates.py`` (``_REGISTRY_AS_SHIPPED``), which is the assertion that a *capability*
    cannot leave; these are the assertions that an *excuse* cannot arrive.
    """
    found: dict[str, dict[str, str]] = {}
    for name, value in vars(capability).items():
        if name.startswith("_") or not name.isupper() or not isinstance(value, dict):
            continue
        if all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            found[name] = value
    return found


def _census_offences(
    live: Mapping[str, str], pinned: Mapping[str, str]
) -> tuple[list[str], list[str], dict[str, tuple[str, str]]]:
    """Split a ledger against its pin into arrivals, departures and rewrites."""
    added = sorted(set(live) - set(pinned))
    removed = sorted(set(pinned) - set(live))
    reworded = {
        name: (pinned[name], live[name])
        for name in sorted(set(pinned) & set(live))
        if pinned[name] != live[name]
    }
    return added, removed, reworded


def _assert_censused(ledger: str) -> None:
    """Assert one ledger holds exactly what it was pinned to hold, in all three directions."""
    live = _ledgers_in_the_registry_module()[ledger]
    pinned = _CENSUSES[ledger]
    added, removed, reworded = _census_offences(live, pinned)

    assert not added, (
        f"{added} arrived on {ledger} and are unrecorded in tests/conformance/"
        f"test_pending_symmetry.py. Excusing a capability is allowed and excusing it quietly "
        f"is not: a referee deleted the equity half of iv-surface, wrote the name onto "
        f"IRREDUCIBLE with a fabricated justification, and 778 conformance tests stayed "
        f"green. Add the entry here, in the same commit, where the diff shows what stopped "
        f"being symmetric and what was written down instead."
    )
    assert not removed, (
        f"{removed} left {ledger} and are still pinned here. If the exemption was repaid — "
        f"the implementation landed, the method shipped — delete it here too, so the "
        f"remaining list keeps meaning what it says."
    )
    assert not reworded, (
        f"the argument changed under these {ledger} entries: {reworded}. An exemption is "
        f"carried by its argument, so rewriting one is a new claim and not an edit."
    )


@pytest.mark.parametrize("ledger", sorted(_CENSUSES))
def test_every_ledger_holds_exactly_the_entries_it_was_pinned_to_hold(ledger: str) -> None:
    """The mechanism ``PENDING_SYMMETRY`` had and the other ledgers did not.

    An exemption list that can grow silently is a place to put things, and the referee's
    experiment is what that costs: ``IRREDUCIBLE`` gained an entry, a capability lost an
    implementation, and every gate that reads either one had a defensible reason to pass.
    The delete-then-*schedule* spelling of the same experiment was caught, and it was caught
    here — by ``_LEDGER_AS_SHIPPED``, the one census that existed.

    Pinning is deliberately dumb. It grades no prose and understands no argument; it asserts
    that a human edited two files in one commit, which is the one thing a gate can check
    about a claim only a human can evaluate.
    """
    _assert_censused(ledger)


def test_every_ledger_in_the_registry_module_is_censused() -> None:
    """The gate that makes the next ledger arrive censused rather than arrive unwatched.

    Two phases ago eleven branches each added a gate and each verified its own by breaking
    it; the lesson recorded from that is that per-gate verification measures "can this gate
    fail" and never "what do these gates together miss". This is one answer in that second
    shape. Every gate above watches a ledger somebody thought to watch — and the finding
    that reopened this file was a ledger nobody had.

    So the ledgers are discovered from the module and compared against the censused set. A
    fourth exemption dict added to ``core/capability.py`` fails here until it is pinned,
    which is the failure a reviewer can act on rather than a silence they cannot see. The
    census this file adds for ``SHARED_IMPLEMENTATION`` — a ledger written in the same commit
    as the gate that reads it — is exactly the shape this test exists to make mandatory.
    """
    discovered = sorted(_ledgers_in_the_registry_module())
    censused = sorted(_CENSUSES)
    assert discovered == censused, (
        f"crocodile.core.capability declares {discovered} as name-to-argument ledgers and "
        f"this file censuses {censused}. Every ledger is an exemption somebody can widen, so "
        f"every ledger is pinned; add the missing one to _CENSUSES with its entries."
    )


def test_an_entry_arriving_on_the_irreducible_list_is_caught(_isolate: None) -> None:
    """The referee's experiment, second half, run as a test.

    The first half — deleting the equity implementation — is caught by the asymmetry census
    below. This is the half that made the deletion *invisible*, and on its own it is the
    thing worth failing: a name arriving on the permanent exemption list is a claim that the
    market cannot support a capability this product served yesterday.
    """
    IRREDUCIBLE["iv-surface"] = "No equity analogue can exist; this is chain-native."
    with pytest.raises(AssertionError, match="iv-surface"):
        _assert_censused("IRREDUCIBLE")


def test_an_entry_leaving_the_irreducible_list_is_caught(_isolate: None) -> None:
    """Departures fail too, so the census cannot be satisfied by deleting the pin's subject."""
    del IRREDUCIBLE["peg-deviation"]
    with pytest.raises(AssertionError, match="peg-deviation"):
        _assert_censused("IRREDUCIBLE")


def test_a_rewritten_irreducible_justification_is_caught(_isolate: None) -> None:
    """The argument is the exemption; swapping it silently is how a bar erodes."""
    IRREDUCIBLE["mev-sandwich"] = "Chain-native; there is no equity version of this."
    with pytest.raises(AssertionError, match="the argument changed"):
        _assert_censused("IRREDUCIBLE")


def test_a_ledger_with_no_census_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fourth exemption dict, added to the registry module and pinned nowhere."""
    monkeypatch.setattr(
        capability, "FIXTURE_EXEMPTIONS", {"fixture-cap": "because"}, raising=False
    )
    with pytest.raises(AssertionError, match="FIXTURE_EXEMPTIONS"):
        test_every_ledger_in_the_registry_module_is_censused()


# ---------------------------------------------------------------------------
# The frontier itself: which capabilities serve which asset classes
# ---------------------------------------------------------------------------


_ASYMMETRIC_AS_SHIPPED: dict[str, tuple[str, ...]] = {
    "base-market-data": (AssetClass.CRYPTO,),
    "gas-vol": (AssetClass.CRYPTO,),
    "lending-stress": (AssetClass.CRYPTO,),
    "mev-sandwich": (AssetClass.CRYPTO,),
    "onchain-price": (AssetClass.CRYPTO,),
    "peg-deviation": (AssetClass.CRYPTO,),
    "sequencer-latency": (AssetClass.CRYPTO,),
}
"""Every capability that does not serve both asset classes, and which one it serves.

Everything else in ``REGISTRY`` is symmetric, asserted rather than assumed: a name absent
from this mapping and asymmetric at runtime is a failure, so the pin covers all 49 without
listing 49.

**Why this exists beside the ledger censuses rather than instead of them.** They watch the
excuses; this watches the fact. Each of the last two reviews closed a route into
``IRREDUCIBLE`` and the next referee found another one, because a gate keyed on *how* a
deletion was justified has to be re-written for every new justification — and the codebase's
own recorded lesson is that per-gate verification cannot measure what a set of gates misses.
An equity half cannot leave without this changing, whichever ledger the name is written onto,
whether it is written onto one at all, and whether that ledger had been invented yet.

**Why asset classes and not implementation identity.** The two questions are different and
both are asked: this says ``iv-surface`` still serves equities, and
:func:`test_each_asset_class_is_served_by_its_own_implementation` says the thing serving them
is not the crypto function bound twice. ``slippage`` shipped the second defect while
satisfying the first, which is why one census cannot answer both.

The seven names here are the seven on ``IRREDUCIBLE``, and that is a fact rather than a
definition — the two lists are asserted to agree in
:func:`test_the_asymmetry_census_and_the_exemption_ledgers_describe_the_same_seven` rather
than one being derived from the other, because deriving it would make this census read its
subject's excuse and inherit whatever that excuse permits.
"""


def _live_asymmetry() -> dict[str, tuple[str, ...]]:
    return {
        name: tuple(sorted(cap.impls))
        for name, cap in REGISTRY.items()
        if set(cap.impls) != {AssetClass.CRYPTO, AssetClass.EQUITY}
    }


def test_no_capability_changes_which_asset_classes_it_serves() -> None:
    """A deletion that reaches for any excuse, and one that reaches for none, both fail here.

    The referee's report, in one table: deleting the equity half of ``iv-surface`` failed 4
    tests; deleting it *and* excusing it on ``IRREDUCIBLE`` failed none, over ``iv-surface``,
    ``ofi``, ``census``, ``whale-alerts``, ``open-interest`` and ``liquidity-depth`` alike.
    The four that fired for the bare deletion all read ``PENDING_SYMMETRY`` or Gate 2, and
    every one of them is written to fall silent once a name is excused — which is correct
    behaviour for a gate about scheduling and is the reason none of them can be the gate
    about *shape*.

    ``load_all()`` is called for the same reason Gate 2's registry census calls it: this
    reads ``REGISTRY`` as though it were complete, and it is only complete once every batch
    module has been imported.
    """
    from crocodile.capabilities import load_all

    load_all()
    live = _live_asymmetry()

    lost = {
        name: served for name, served in live.items() if name not in _ASYMMETRIC_AS_SHIPPED
    }
    assert not lost, (
        f"{lost} serve fewer asset classes than they shipped with. A capability that stops "
        f"answering for a market is the deletion this whole ledger exists to make visible, "
        f"and no entry on IRREDUCIBLE, PENDING_SYMMETRY or any list invented later makes it "
        f"a smaller change. Restore the implementation, or record the retirement here in the "
        f"same commit."
    )

    gained = sorted(set(_ASYMMETRIC_AS_SHIPPED) - set(live))
    assert not gained, (
        f"{gained} are symmetric now and still pinned as asymmetric. That is work landing: "
        f"delete them here, and delete whatever ledger entry excused them, which is the "
        f"claim their implementation has just disproved."
    )

    changed = {
        name: (was, live[name])
        for name, was in _ASYMMETRIC_AS_SHIPPED.items()
        if name in live and live[name] != was
    }
    assert not changed, (
        f"these serve a different asset class than they were pinned to: {changed}. Swapping "
        f"which market a capability answers for is not a refactor."
    )


def test_the_asymmetry_census_and_the_exemption_ledgers_describe_the_same_seven() -> None:
    """Two independently maintained lists of the same seven names, asserted to agree.

    Gate 2 says an asymmetric capability must be excused or scheduled;
    :func:`test_the_irreducible_list_only_holds_capabilities_that_exist` says an excuse must
    name something registered. Neither says the two sets are the *same* set, and the gap
    between them is where a deletion sits while its excuse is being written. Asserting the
    agreement is what makes the two censuses one fact from two directions rather than two
    facts that can drift.
    """
    excused = set(IRREDUCIBLE) | set(PENDING_SYMMETRY)
    pinned = set(_ASYMMETRIC_AS_SHIPPED)
    assert pinned == excused, (
        f"the asymmetry census holds {sorted(pinned)} and the exemption ledgers hold "
        f"{sorted(excused)}. Every asymmetric capability is excused or scheduled and every "
        f"excused or scheduled name is asymmetric; a difference either way is a name that "
        f"has an argument and no gap, or a gap and no argument."
    )


def test_deleting_an_equity_half_is_caught_even_when_it_is_excused(_isolate: None) -> None:
    """The referee's experiment end to end, over the capability it was first run on.

    Both edits, in the order that passed: the implementation goes, then the name is written
    onto ``IRREDUCIBLE`` with the justification the referee fabricated — which the old
    six-word blacklist accepted, because it names no schedule.
    """
    cap = REGISTRY["iv-surface"]
    REGISTRY["iv-surface"] = msgspec.structs.replace(
        cap, impls={AssetClass.CRYPTO: cap.impls[AssetClass.CRYPTO]}
    )
    IRREDUCIBLE["iv-surface"] = "No equity analogue can exist; this is chain-native."

    with pytest.raises(AssertionError, match="iv-surface"):
        test_no_capability_changes_which_asset_classes_it_serves()


def test_a_capability_that_grew_the_half_it_was_pinned_without_is_caught(_isolate: None) -> None:
    """The other direction, which is work landing rather than work vanishing.

    It fails too, and it should: an implementation arriving is the moment its exemption
    stops being true, and the entry has to leave in the same commit or the *next* deletion
    is invisible again.
    """
    REGISTRY["peg-deviation"] = _symmetric("peg-deviation")
    with pytest.raises(AssertionError, match="peg-deviation"):
        test_no_capability_changes_which_asset_classes_it_serves()


# ---------------------------------------------------------------------------
# One function cannot serve two markets
# ---------------------------------------------------------------------------
# `set(cap.impls) == {CRYPTO, EQUITY}` is two dict keys. Nothing above it asks what
# the keys point at, so binding the crypto function under both is a symmetric
# declaration by every measure any gate takes. This codebase has the defect on
# record — `slippage` shipped an equity half that read `book_snapshot`, which no
# equity provider writes — and a referee re-introduced it against `census` and passed
# 3 300 tests. What caught the two capabilities it did not pass were hand-written
# per-capability assertions in a batch module's own test file, which is coverage by
# whoever happened to write one.


def test_each_asset_class_is_served_by_its_own_implementation() -> None:
    """Two markets, two implementations — unless the declaration argues otherwise.

    The exception is real and is why this reads a ledger instead of banning sharing
    outright: seventeen capabilities answer about the lake rather than about a market, and
    ``funding-predict`` reads no store at all. For those, one function is the honest
    declaration and a second copy would be a second copy of the same SQL.

    What the gate must not do is *guess* which case it is looking at, and the obvious guess
    fails. ``basis`` differing between the two implementations sounds like the signature of
    two real halves, but ``iv-surface``, ``ofi``, ``term-structure``, ``vol-skew``,
    ``risk-reversal``, ``open-interest``, ``basis``, ``backfill``, ``collect`` and
    ``list-exchanges`` all declare ``native`` on both sides — every one of them would have
    been waved through — while ``indicators``, which legitimately shares, declares ``native``
    on both sides too. The distinguisher is a claim about the capability, so it is written
    where claims are written: :data:`~crocodile.core.capability.SHARED_IMPLEMENTATION`.
    """
    from crocodile.capabilities import load_all

    load_all()
    offenders: dict[str, str] = {}
    for name, cap in sorted(REGISTRY.items()):
        if set(cap.impls) != {AssetClass.CRYPTO, AssetClass.EQUITY}:
            continue
        crypto, equity = cap.impls[AssetClass.CRYPTO], cap.impls[AssetClass.EQUITY]
        if crypto.fn is not equity.fn:
            continue
        if not SHARED_IMPLEMENTATION.get(name, "").strip():
            offenders[name] = f"{crypto.fn.__module__}.{crypto.fn.__qualname__}"

    assert not offenders, (
        f"{offenders} declare both asset classes and bind one function to both. That is a "
        f"symmetric declaration over an implementation that can only serve one market — the "
        f"shape slippage shipped, where the equity half read a book_snapshot no equity "
        f"provider writes and every equity call raised while the registry read as symmetric. "
        f"Write the missing implementation, or argue on SHARED_IMPLEMENTATION why one "
        f"function is the answer here."
    )


def test_no_shared_implementation_entry_is_stale() -> None:
    """An entry has to keep being true, or it is an exemption waiting for a rebind.

    Three ways it stops being true and all three fail: the capability is gone, the
    capability stopped serving both asset classes, or the two implementations are distinct
    functions now — at which point the entry excuses a sharing that is not happening, and
    would go on excusing it after somebody rebound them.
    """
    from crocodile.capabilities import load_all

    load_all()
    offenders: dict[str, str] = {}
    for name, why in sorted(SHARED_IMPLEMENTATION.items()):
        cap = REGISTRY.get(name)
        if cap is None:
            offenders[name] = "names no registered capability"
        elif set(cap.impls) != {AssetClass.CRYPTO, AssetClass.EQUITY}:
            offenders[name] = f"serves only {sorted(cap.impls)}"
        elif cap.impls[AssetClass.CRYPTO].fn is not cap.impls[AssetClass.EQUITY].fn:
            offenders[name] = "has two distinct implementations and needs no exemption"
        elif not why.strip():
            offenders[name] = "carries no argument"
    assert not offenders, f"stale SHARED_IMPLEMENTATION entries: {offenders}"


def _two_functions(name: str) -> Capability:
    """A capability whose two asset classes are served by genuinely different callables."""
    return Capability(
        name=name,
        summary="Two markets, two implementations.",
        params=_Params,
        returns=ReturnKind.TABLE,
        impls={
            AssetClass.CRYPTO: Impl(fn=len, prov=Provenance.NATIVE, basis="native"),
            AssetClass.EQUITY: Impl(fn=sorted, prov=Provenance.NATIVE, basis="native"),
        },
    )


def test_rebinding_the_crypto_function_as_the_equity_one_is_caught(_isolate: None) -> None:
    """The referee's second experiment, over one of the six names nothing covered.

    ``census`` is used because it is the name the referee ran the full suite against: 3 300
    passed, 7 skipped, 0 failed. ``markets``, ``universe``, ``depth``, ``liquidity-depth``
    and ``chaos-score`` were the other five with no per-capability assertion of their own.
    """
    cap = REGISTRY["census"]
    REGISTRY["census"] = msgspec.structs.replace(
        cap,
        impls={
            **cap.impls,
            AssetClass.EQUITY: msgspec.structs.replace(
                cap.impls[AssetClass.EQUITY], fn=cap.impls[AssetClass.CRYPTO].fn
            ),
        },
    )
    with pytest.raises(AssertionError, match="census"):
        test_each_asset_class_is_served_by_its_own_implementation()


def test_a_legitimately_shared_implementation_is_left_alone() -> None:
    """The gate is only worth having if it does not fire on the seventeen that share on purpose.

    ``query`` stands for them: one lake, one SQL path, and an entry that says so.
    """
    assert REGISTRY["query"].impls[AssetClass.CRYPTO].fn is (
        REGISTRY["query"].impls[AssetClass.EQUITY].fn
    )
    test_each_asset_class_is_served_by_its_own_implementation()


def test_the_rebind_cannot_be_laundered_by_widening_the_shared_list(
    _isolate: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The move this whole task exists to answer, applied to the gate it just added.

    Every gate here suggests its own exemption, and the finding two phases ago was that
    eleven branches each added one and the twelfth deletion went straight into it. So the
    rebind gate ships with the census already closed around it: excusing ``census`` on
    ``SHARED_IMPLEMENTATION`` silences the rebind gate exactly as intended, and then the
    census fails on the entry that silenced it.
    """
    cap = REGISTRY["census"]
    REGISTRY["census"] = msgspec.structs.replace(
        cap,
        impls={
            **cap.impls,
            AssetClass.EQUITY: msgspec.structs.replace(
                cap.impls[AssetClass.EQUITY], fn=cap.impls[AssetClass.CRYPTO].fn
            ),
        },
    )
    monkeypatch.setitem(SHARED_IMPLEMENTATION, "census", "One lake, honest.")

    test_each_asset_class_is_served_by_its_own_implementation()
    with pytest.raises(AssertionError, match="census"):
        _assert_censused("SHARED_IMPLEMENTATION")


def test_an_entry_for_a_capability_that_no_longer_shares_is_caught(
    _isolate: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repaid exemption has to leave, on the rule both other ledgers already carry.

    Writing the second implementation is the outcome this list exists to make someone
    choose against. Leaving the entry behind afterwards means the *next* rebind of that
    capability is excused before it happens, which is the hoarding hazard
    ``IRREDUCIBLE``'s twin has had a gate for since Phase 2.
    """
    REGISTRY["fixture-unshared"] = _two_functions("fixture-unshared")
    monkeypatch.setitem(SHARED_IMPLEMENTATION, "fixture-unshared", "One lake.")
    with pytest.raises(AssertionError, match="needs no exemption"):
        test_no_shared_implementation_entry_is_stale()


def test_an_entry_naming_nothing_registered_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``gas-tracker`` shape again: an exemption outliving the thing it excused."""
    monkeypatch.setitem(SHARED_IMPLEMENTATION, "fixture-deleted", "One lake.")
    with pytest.raises(AssertionError, match="names no registered capability"):
        test_no_shared_implementation_entry_is_stale()
