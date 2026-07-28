"""Counts and enumerations that are stated in prose, asserted against what the code holds.

Every claim in this file was wrong at some point, and each one was wrong for the same
reason: a sentence stated a number, the number moved, and nothing read the sentence again.
The merge falsified six of them at once — three docstrings saying the registry holds 48
capabilities when it holds 49, a connector factory enumerating nine of its ten exchanges, a
provider factory enumerating two of its five, an exemption list described as six names when
it is seven, and a ledger docstring that said "not empty, 21 entries" after Phase 3 emptied
it.

The repair for a wrong number is to write the right one. The repair for a number that goes
wrong unwatched is this file. A docstring that enumerates a registry is a gate somebody
declined to write, because the enumeration is derivable and the comparison is three lines;
what the prose adds is the *argument*, and the argument stays honest only while the list
beside it does.

Two rules kept here deliberately:

* **Assert against the live object, never against a literal.** A test that pins ``49`` in
  two files has moved the duplication rather than removed it. Every expectation below is
  computed from ``REGISTRY``, ``IRREDUCIBLE`` or a factory's ``_REGISTRY``.
* **A missing sentence fails too.** Each test locates its anchor phrase and fails if the
  anchor is gone, so rewording the paragraph out from under the gate is a red test rather
  than a silently vacuous one — the failure mode
  ``tests/conformance/test_gates.py`` documents for gates whose subject can empty.
"""

from __future__ import annotations

import ast
import itertools
import re
from pathlib import Path
from typing import Final

from crocodile.capabilities import load_all
from crocodile.core.capability import IRREDUCIBLE, PENDING_SYMMETRY, REGISTRY

_SRC: Final = Path(__file__).resolve().parents[2] / "src" / "crocodile"
_TESTS: Final = Path(__file__).resolve().parents[1]

_NUMBER_WORDS: Final[dict[int, str]] = {
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}


def _module_docstring(path: Path) -> str:
    """The module docstring, read off the file rather than by importing it.

    Reading the source keeps this test independent of import side effects — the batch
    modules mutate ``PENDING_SYMMETRY`` at import time, which is the very hazard one of
    these docstrings is about.
    """
    doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
    assert doc is not None, f"{path} has no module docstring"
    return doc


def _function_docstring(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            doc = ast.get_docstring(node)
            assert doc is not None, f"{path}::{name} has no docstring"
            return doc
    raise AssertionError(f"{path} has no function named {name}")


def _assignment_docstring(path: Path, target: str) -> str:
    """The string literal immediately following ``target = ...`` at module level.

    This is how attribute docstrings are written, and ``ast.get_docstring`` does not
    reach them.
    """
    body = ast.parse(path.read_text(encoding="utf-8")).body
    for previous, node in itertools.pairwise(body):
        names = (
            [previous.target]
            if isinstance(previous, ast.AnnAssign)
            else getattr(previous, "targets", [])
        )
        if not any(isinstance(n, ast.Name) and n.id == target for n in names):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            value = node.value.value
            assert isinstance(value, str), f"{path}: {target}'s docstring is not a string"
            return value
    raise AssertionError(f"{path} has no attribute docstring for {target}")


def _backticked(text: str) -> list[str]:
    """Every ``double-backticked`` token in *text*, in order."""
    return re.findall(r"``([A-Za-z0-9_\-]+)``", text)


def _ported_and_registered() -> tuple[int, int]:
    """(capabilities the four port batches declared, total registered).

    The split is what the prose actually claims: the four batch modules ported a set off
    the legacy surfaces, and ``onchain`` added the two the surface-parity gate found after
    they closed. Computed from where each implementation's function lives, so moving a
    declaration between batches moves the number rather than falsifying a sentence.
    """
    load_all()
    late = {
        name
        for name, cap in REGISTRY.items()
        if all(impl.fn.__module__.endswith(".onchain") for impl in cap.impls.values())
    }
    return len(REGISTRY) - len(late), len(REGISTRY)


# ---------------------------------------------------------------------------
# How many capabilities the registry holds
# ---------------------------------------------------------------------------


def test_the_pending_symmetry_docstring_counts_the_registry_correctly() -> None:
    """It said 48 while the registry held 49, in the docstring that owns the ledger."""
    ported, total = _ported_and_registered()
    doc = _assignment_docstring(_SRC / "core" / "capability.py", "PENDING_SYMMETRY")
    assert f"Phase 2 put {total} capabilities into" in doc, (
        f"the registry holds {total} capabilities; PENDING_SYMMETRY's docstring does not "
        f"say so. First paragraph as written:\n{doc.split(chr(10) + chr(10))[1]}"
    )
    assert f"{ported} ported off the legacy surfaces" in doc, (
        f"{ported} capabilities were ported by the four batch modules and "
        f"{total - ported} arrived later; the docstring's split does not match."
    )


def test_the_batch_package_counts_what_it_was_asked_to_port() -> None:
    """``capabilities/__init__.py`` argued for four parallel modules over a count of 48."""
    ported, total = _ported_and_registered()
    doc = _module_docstring(_SRC / "capabilities" / "__init__.py")
    assert f"Porting {ported} capabilities off the six legacy surfaces" in doc, (
        f"the four batch modules declare {ported} capabilities; the package docstring "
        f"says otherwise."
    )
    assert f"the {total} names" in doc, f"the registry holds {total} names"


def test_the_symmetry_gate_file_counts_the_registry_correctly() -> None:
    """The ledger's own test file opened on the same wrong number as the ledger."""
    _, total = _ported_and_registered()
    doc = _module_docstring(_TESTS / "conformance" / "test_pending_symmetry.py")
    assert f"Phase 2 registered {total} capabilities" in doc


# ---------------------------------------------------------------------------
# The hazard that made the ledger docstring stale twice
# ---------------------------------------------------------------------------


def test_the_ledger_docstring_states_no_count_of_its_own_contents() -> None:
    """The paragraph that fixed "Empty today" became "not empty, 21 entries" and rotted.

    ``PENDING_SYMMETRY`` is declared ``{}`` and filled by three batch modules at import
    time, so the declaration site can never show the truth and any sentence there that
    names a number is a hostage to the next commit. The rule this pins is therefore not
    "state the right count" — it is *state no count*, and let ``_LEDGER_AS_SHIPPED`` and
    ``test_phase_3_exit_the_ledger_must_be_empty`` carry it.

    Both directions of the failure are covered: the docstring may not claim emptiness
    while the dict has entries, and may not name a size at all.
    """
    doc = _assignment_docstring(_SRC / "core" / "capability.py", "PENDING_SYMMETRY")
    hazard = doc.split("**The ``{}`` above is a declaration")
    assert len(hazard) == 2, (
        "the paragraph warning that the declaration site cannot show the runtime value is "
        "gone. It is the reason this docstring went stale twice; deleting it is how it "
        "goes stale a third time."
    )
    claimed = re.findall(
        r"\b(?:ships with|holds|contains|now has|is)\s+(\d+)\s+entries\b", hazard[1]
    )
    assert not claimed, (
        f"the hazard paragraph asserts a present-tense count ({claimed}); it cannot know "
        f"one. The ledger currently holds {len(PENDING_SYMMETRY)} entries and nothing in "
        f"this module can see that. Past-tense history is fine — 'filled to 21 and "
        f"emptied again' is what makes the hazard concrete — but a claim about now is the "
        f"defect this test exists for."
    )
    for pointer in ("_LEDGER_AS_SHIPPED", "test_phase_3_exit_the_ledger_must_be_empty"):
        assert pointer in doc, (
            f"the docstring no longer says which test checks the count ({pointer}); "
            f"without that the next reader has a warning and nowhere to look"
        )
    if PENDING_SYMMETRY:
        assert "Empty today" not in doc, (
            f"the docstring calls the ledger empty while it holds "
            f"{sorted(PENDING_SYMMETRY)}"
        )


# ---------------------------------------------------------------------------
# The exemption list
# ---------------------------------------------------------------------------


def test_the_ops_batch_counts_the_exemption_list_correctly() -> None:
    """It said "five of the six entries" after ``gas-tracker`` left and two arrived."""
    load_all()
    declared_here = sorted(
        name
        for name, cap in REGISTRY.items()
        if name in IRREDUCIBLE
        and all(impl.fn.__module__.endswith(".ops") for impl in cap.impls.values())
    )
    doc = _module_docstring(_SRC / "capabilities" / "ops.py")
    expected = (
        f"{_NUMBER_WORDS[len(declared_here)]} of the "
        f"{_NUMBER_WORDS[len(IRREDUCIBLE)]} entries of"
    )
    assert expected in doc, (
        f"this batch declares {len(declared_here)} of IRREDUCIBLE's {len(IRREDUCIBLE)} "
        f"names ({declared_here}); the docstring says something else."
    )
    for name in declared_here:
        assert f"``{name}``" in doc, f"{name} is exempted and declared here but unnamed"


def test_the_operator_module_counts_the_commands_it_registers() -> None:
    """It said "These seven" for a table that now holds five.

    ``surfaces/operate.py`` is the one place the hand-written command table is described in
    prose rather than derived, and its docstring opened by counting it — which made the
    sentence a hostage to the next command added or removed. Deleting the two PyQt6
    launchers, ``flowmap`` and ``gas-tracker``, is what collected on that: seven became
    five and nothing in the tree read the sentence again.

    Against ``build_app()`` rather than against a literal, per this module's first rule; a
    reworded paragraph fails on the missing anchor rather than passing vacuously, per its
    second.
    """
    from crocodile.surfaces import operate

    count = len(operate.build_app().registered_commands)
    doc = _module_docstring(_SRC / "surfaces" / "operate.py")
    assert f"These {_NUMBER_WORDS[count]} answer questions" in doc, (
        f"`operate.build_app()` registers {count} hand-written commands "
        f"({sorted(c.name or '' for c in operate.build_app().registered_commands)}); the "
        f"module docstring does not say so. First paragraph as written:\n"
        f"{doc.split(chr(10) + chr(10))[1]}"
    )


def test_gas_tracker_is_described_as_removed_rather_than_present() -> None:
    """Its argument outlived its entry; the argument may not still describe it as live.

    ``tests/capabilities/test_ops.py`` already pins the *state* — absent from every
    ledger but ``UNDECLARED``. What was left uncorrected was the prose that argued for
    that state, which still read "is on IRREDUCIBLE ... the entry is left in place".
    """
    assert "gas-tracker" not in IRREDUCIBLE
    doc = _function_docstring(
        _SRC / "capabilities" / "ops.py", "_why_gas_tracker_is_not_a_capability"
    )
    assert "**was** on" in doc, (
        "the argument still describes gas-tracker as being on IRREDUCIBLE; it is not"
    )
    assert "The entry is left in place" not in doc, (
        "the argument still says the entry stands; the coordinator removed it"
    )


# ---------------------------------------------------------------------------
# Factory enumerations
# ---------------------------------------------------------------------------


def test_the_connector_factory_docstring_lists_every_native_exchange() -> None:
    """It listed nine of ten — ``coingecko`` was missing and read as complete."""
    from crocodile.crypto.exchanges import factory

    path = _SRC / "crypto" / "exchanges" / "factory.py"
    doc = _module_docstring(path)
    _, _, tail = doc.partition("Valid exchange names:")
    assert tail, "the 'Valid exchange names:' enumeration is gone from the module docstring"
    listed = set(_backticked(tail.split("That list is")[0]))
    missing = sorted(set(factory._REGISTRY) - listed)
    extra = sorted(listed - set(factory._REGISTRY))
    assert not missing and not extra, (
        f"module docstring enumeration disagrees with _REGISTRY: missing {missing}, "
        f"named-but-unregistered {extra}"
    )

    param_doc = _function_docstring(path, "make_connector")
    _, _, param_tail = param_doc.partition("Valid values:")
    param_listed = set(_backticked(param_tail.split("Any other name")[0]))
    assert param_listed == set(factory._REGISTRY), (
        f"make_connector's parameter docs list {sorted(param_listed)}, "
        f"_REGISTRY holds {sorted(factory._REGISTRY)}"
    )


def test_the_provider_factory_docstring_lists_every_registered_provider() -> None:
    """It listed two of five, which reads as the whole set and is not."""
    from crocodile.equity.providers import factory

    doc = _function_docstring(_SRC / "equity" / "providers" / "factory.py", "make_provider")
    _, _, tail = doc.partition("Valid values:")
    assert tail, "the 'Valid values:' enumeration is gone from make_provider's docs"
    listed = set(_backticked(tail.split("—")[0]))
    assert listed == set(factory._REGISTRY), (
        f"make_provider's docs list {sorted(listed)}, _REGISTRY holds "
        f"{sorted(factory._REGISTRY)}"
    )


def test_the_treasury_client_does_not_claim_to_be_outside_the_registry() -> None:
    """This gate outlived its own subject, which is the thing it was written to catch.

    It pinned two ordinals — treasury calling itself "the fourth" plain client and "a fifth
    entry" the factory would gain — because *that* prose had been off by one: ``openfigi``
    is a plain client too, and the factory already held five providers. The ordinals were
    the argument's arithmetic, and "this is the same shape as its siblings" is only
    checkable if the siblings are counted.

    Then the argument itself was measured and found wrong: a client outside
    ``providers.factory._REGISTRY`` is a client no shipped command can reach, and eleven
    equity capabilities read channels only those clients write. Treasury is a registered
    provider now, so it makes no ordinal claim and there is no ordinal to check — the gate
    failed with a ``KeyError`` rather than an assertion, which is what a gate looks like
    when the sentence it guards has been deleted instead of edited.

    So it now pins what is true after the move, in the same shape: the registry is what
    decides who is reachable, ``openfigi`` is the one package deliberately outside it
    because it enriches a universe rather than writing a channel, and treasury's docstring
    must not still describe itself as a sibling of the excluded set.
    """
    from crocodile.equity.providers import factory

    providers_dir = _SRC / "equity" / "providers"
    packages = {
        child.name
        for child in providers_dir.iterdir()
        if child.is_dir() and (child / "__init__.py").exists()
    }
    outside = sorted(packages - set(factory._REGISTRY))
    assert outside == ["openfigi"], (
        f"packages outside providers.factory._REGISTRY are {outside}. A source outside it "
        f"cannot be reached by collect, collect-market or backfill, so anything new here is "
        f"a capability whose inputs nothing can write — say why in this test or register it."
    )

    doc = _module_docstring(providers_dir / "treasury" / "client.py")
    assert "This is the fourth" not in doc and "This is the fifth" not in doc, (
        "treasury still counts itself among the plain clients; it is a registered provider"
    )
    assert "TreasuryProvider" in doc or "_REGISTRY" in doc, (
        "treasury's module docstring should say it is reachable through the provider "
        "registry, since that is the fact its old argument got wrong"
    )
