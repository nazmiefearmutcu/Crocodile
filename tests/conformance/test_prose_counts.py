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

The section named Global equities asserts the README's ``## Global equities`` section and
the CHANGELOG's ``## [Unreleased]`` notes against the objects they describe. Every exchange,
calendar, source, usage class, command, column, path, member and count that those texts
state is read off the live code, so a sentence that names one that does not exist fails here.
"""

from __future__ import annotations

import ast
import csv
import dataclasses
import enum
import importlib
import itertools
import re
import shlex
import tomllib
from pathlib import Path
from typing import Final

import msgspec
import pytest

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
    11: "eleven",
    12: "twelve",
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
    """(capabilities the four port batches declared, total Phase 2 shipped).

    Phase 2's numbers are history, so they are read off the frozen census
    ``_REGISTRY_AS_SHIPPED`` rather than off the live registry: ``calendar`` is the first
    capability declared after Phase 2, and it must not falsify the sentences that describe
    Phase 2. The split is what the prose actually claims: the four batch modules ported a set
    off the legacy surfaces, and ``onchain`` added the two the surface-parity gate found after
    they closed. Computed from where each implementation's function lives, so moving a
    declaration between batches moves the number rather than falsifying a sentence.
    """
    from tests.conformance.test_gates import _REGISTRY_AS_SHIPPED

    load_all()
    missing = sorted(_REGISTRY_AS_SHIPPED - set(REGISTRY))
    assert not missing, f"Phase 2 capabilities that are no longer registered: {missing}"
    late = {
        name
        for name in _REGISTRY_AS_SHIPPED
        if all(impl.fn.__module__.endswith(".onchain") for impl in REGISTRY[name].impls.values())
    }
    return len(_REGISTRY_AS_SHIPPED) - len(late), len(_REGISTRY_AS_SHIPPED)


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
            f"the docstring calls the ledger empty while it holds {sorted(PENDING_SYMMETRY)}"
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
        f"{_NUMBER_WORDS[len(declared_here)]} of the {_NUMBER_WORDS[len(IRREDUCIBLE)]} entries of"
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
        f"make_provider's docs list {sorted(listed)}, _REGISTRY holds {sorted(factory._REGISTRY)}"
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


# ---------------------------------------------------------------------------
# The live registry size and the wire-name counts
# ---------------------------------------------------------------------------


def _flat(text: str) -> str:
    """``text`` with every run of whitespace collapsed to one space.

    Docstrings wrap, so a sentence may break across lines; the anchors match on the flat form.
    """
    return " ".join(text.split())


def _wire_counts() -> tuple[int, int]:
    """(wire names that must name an asset class, all wire names), read off the dispatcher."""
    from crocodile.surfaces import dispatch as dispatcher

    load_all()
    names = dispatcher.wire_names()
    explicit = sum(
        dispatcher.requires_explicit_asset_class(REGISTRY[name]) for name in names.values()
    )
    return explicit, len(names)


def test_the_batch_package_states_the_live_registry_size() -> None:
    """The package docstring names how many capabilities the registry holds right now."""
    load_all()
    doc = _flat(_module_docstring(_SRC / "capabilities" / "__init__.py"))
    assert f"the registry now holds {len(REGISTRY)}" in doc, (
        f"the registry holds {len(REGISTRY)} capabilities; the package docstring does not say so"
    )


def test_every_capability_added_after_phase_2_is_named_where_its_family_is_described() -> None:
    """A capability added after Phase 2 is named in the package docstring and in the
    docstring of the batch module its implementations live in."""
    from tests.conformance.test_gates import _ADDED_AFTER_PHASE_2

    load_all()
    package_doc = _flat(_module_docstring(_SRC / "capabilities" / "__init__.py"))
    for name in _ADDED_AFTER_PHASE_2:
        assert f"``{name}``" in package_doc, f"{name} is not named in the package docstring"
        for impl in REGISTRY[name].impls.values():
            module = impl.fn.__module__
            path = _SRC.parent / Path(*module.split(".")).with_suffix(".py")
            family_doc = _flat(_module_docstring(path))
            assert f"``{name}``" in family_doc, f"{name} is not named in {module}'s docstring"


def test_the_dispatcher_counts_the_wire_names_that_need_an_asset_class() -> None:
    explicit, total = _wire_counts()
    doc = _flat(
        _function_docstring(_SRC / "surfaces" / "dispatch.py", "requires_explicit_asset_class")
    )
    assert f"{explicit} of the {total} wire names" in doc, (
        f"the dispatcher has {explicit} of {total} wire names that need --asset-class; "
        "requires_explicit_asset_class's docstring says otherwise"
    )


def test_the_mcp_projection_counts_the_same_wire_names() -> None:
    explicit, _ = _wire_counts()
    doc = _flat(_function_docstring(_SRC / "surfaces" / "mcp.py", "_definition"))
    assert f"for the {explicit} wire names with two implementations" in doc, (
        f"the MCP projection requires asset_class on {explicit} wire names; _definition's "
        "docstring says otherwise"
    )


# ---------------------------------------------------------------------------
# Global equities: README, CHANGELOG and package metadata
# ---------------------------------------------------------------------------

_REPO: Final = _SRC.parents[1]
_README: Final = _REPO / "README.md"
_CHANGELOG: Final = _REPO / "CHANGELOG.md"
_FENCE: Final = "```"
_FORBIDDEN_CLAIMS: Final = (
    "global coverage",
    "all exchanges",
    "worldwide coverage",
    "real-time",
    "institutional",
    "complete",
)


def _markdown_section(path: Path, heading: str) -> str:
    """The text from *heading* to the next heading of the same or a higher level.

    Lines inside a fenced block are never headings, so a comment line inside a bash example
    does not end the section early.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    assert heading in lines, f"{path.name} has no line {heading!r}; the anchor is gone"
    start = lines.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    in_fence = False
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.startswith(_FENCE):
            in_fence = not in_fence
            continue
        hashes = len(line) - len(line.lstrip("#"))
        if not in_fence and 0 < hashes <= level and line[hashes : hashes + 1] == " ":
            return "\n".join(lines[start:index])
    return "\n".join(lines[start:])


def _global_equities() -> str:
    return _markdown_section(_README, "## Global equities")


def _unreleased() -> str:
    """The newest changelog section: ``[Unreleased]`` before a release, the release after it."""
    first = next(
        line
        for line in _CHANGELOG.read_text(encoding="utf-8").splitlines()
        if line.startswith("## [")
    )
    return _markdown_section(_CHANGELOG, first)


def _code_lines(section: str) -> list[str]:
    """Command lines in fenced blocks, backslash continuations joined and comments dropped."""
    commands: list[str] = []
    in_fence = False
    pending = ""
    for raw in section.splitlines():
        if raw.startswith(_FENCE):
            in_fence = not in_fence
            continue
        if not in_fence:
            continue
        line = raw.strip()
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        line = pending + line
        pending = ""
        if line and not line.startswith("#"):
            commands.append(line)
    return commands


def _table_after(section: str, heading: str) -> list[list[str]]:
    """The body rows of the first Markdown table after *heading*, header and rule dropped."""
    lines = section.splitlines()
    assert heading in lines, f"no line {heading!r} in the section"
    rows: list[list[str]] = []
    seen_header = False
    for line in lines[lines.index(heading) + 1 :]:
        if not line.startswith("|"):
            if seen_header:
                break
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not seen_header:
            seen_header = True
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        rows.append(cells)
    return rows


def _has_member(owner: type, name: str) -> bool:
    if isinstance(owner, enum.EnumMeta):
        return name in owner.__members__
    if issubclass(owner, msgspec.Struct):
        return name in {f.name for f in msgspec.structs.fields(owner)} or hasattr(owner, name)
    if dataclasses.is_dataclass(owner):
        return name in {f.name for f in dataclasses.fields(owner)} or hasattr(owner, name)
    return hasattr(owner, name) or name in getattr(owner, "__annotations__", {})


def _resolve_dotted(path: str) -> object:
    """Import the longest importable prefix of *path*, then read the rest as attributes.

    A last name that is a field of a class rather than a class attribute (a msgspec field,
    say) resolves to that name, because what is checked is that the member exists.
    """
    parts = path.removesuffix("()").split(".")
    for cut in range(len(parts), 0, -1):
        try:
            found: object = importlib.import_module(".".join(parts[:cut]))
        except ModuleNotFoundError:
            continue
        for position in range(cut, len(parts)):
            attribute = parts[position]
            if (
                isinstance(found, type)
                and position == len(parts) - 1
                and not hasattr(found, attribute)
                and _has_member(found, attribute)
            ):
                return attribute
            found = getattr(found, attribute)
        return found
    raise AssertionError(f"`{path}` does not resolve")


def _documented_classes() -> dict[str, type]:
    """The namespace C3 resolves ``Class.member`` tokens in.

    Public classes of the record and enum modules and of ``crocodile.core.markets`` and its
    submodules, plus the runtime classes the notes describe by name.
    """
    from crocodile.core.ingest.deadletter import DeadLetterQueue
    from crocodile.core.scheduler.coordinator import PullTask, ScheduledPullCoordinator
    from crocodile.core.schema import enums, records
    from crocodile.equity.providers.base import Provider
    from crocodile.equity.reference.master import SecurityMaster

    modules: list[object] = [records, enums]
    for name in (
        "",
        "mic",
        "currency",
        "exchanges",
        "calendar",
        "symbols",
        "grammar",
        "master",
        "sanctions",
        "usage",
    ):
        module_name = f"crocodile.core.markets.{name}" if name else "crocodile.core.markets"
        try:
            modules.append(importlib.import_module(module_name))
        except ModuleNotFoundError:
            continue
    classes: dict[str, type] = {}
    for module in modules:
        for name, value in vars(module).items():
            if (
                isinstance(value, type)
                and not name.startswith("_")
                and value.__module__.startswith("crocodile")
            ):
                classes.setdefault(name, value)
    for extra in (PullTask, ScheduledPullCoordinator, Provider, SecurityMaster, DeadLetterQueue):
        classes.setdefault(extra.__name__, extra)
    return classes


def _live_exchange_rows() -> list[object]:
    from crocodile.core.markets.exchanges import ExchangeRegistry

    return list(ExchangeRegistry().all())


def test_the_global_equities_section_counts_the_exchange_table() -> None:
    """The README's exchange and country counts are the exchange table's own counts.

    The README is where a reader learns how far the reference data reaches. A count typed
    once and never read again goes stale the day a row is added, and the sentence then
    understates or overstates coverage with nothing failing. Reading both numbers off
    ``ExchangeRegistry`` makes adding a row a change that must also touch the sentence.
    """
    rows = _live_exchange_rows()
    countries = len({row.country for row in rows})  # type: ignore[attr-defined]
    anchor = (
        f"`src/crocodile/core/markets/data/exchanges.csv` lists {len(rows)} exchanges "
        f"in {countries} countries"
    )
    assert anchor in _global_equities(), f"the Global equities section lacks {anchor!r}"


def test_the_global_equities_section_counts_library_calendars() -> None:
    """The README's library-calendar count is taken from the table and tied to ``calendar_for``.

    The section says how many exchanges get a holiday-aware calendar from the
    ``exchange_calendars`` library and how many fall back to weekdays. The count is taken from
    the ``xcal_code`` column, which is what ``calendar_for`` consults. Two spot checks then
    confirm that a row with a code answers ``LIBRARY`` and a row without one answers
    ``WEEKDAY_FALLBACK``, so the count is tied to behaviour and not only to data. Building every
    calendar here would pay the library import many times over.
    """
    from crocodile.core.markets.calendar import CalendarQuality, calendar_for

    rows = _live_exchange_rows()
    with_code = [row for row in rows if row.xcal_code]  # type: ignore[attr-defined]
    without_code = [row for row in rows if not row.xcal_code]  # type: ignore[attr-defined]
    assert with_code and without_code, "the exchange table has no mix of library rows"
    first_with = with_code[0].mic  # type: ignore[attr-defined]
    first_without = without_code[0].mic  # type: ignore[attr-defined]
    assert calendar_for(first_with).quality is CalendarQuality.LIBRARY
    assert calendar_for(first_without).quality is CalendarQuality.WEEKDAY_FALLBACK
    anchor = f"{len(with_code)} of them have a library calendar"
    assert anchor in _global_equities(), f"the Global equities section lacks {anchor!r}"


def test_the_global_equities_section_names_exactly_the_default_sanctioned_countries() -> None:
    """The sanctions line in the README names exactly the countries the data file closes.

    Sanctioned-by-default is a legal boundary, so a reader must be able to see it, and a
    README that lists three countries while the data closes five is the worst error this
    section could make. The set is compared in both directions against the ``country``
    column of ``sanctions.csv``.
    """
    data = _SRC / "core" / "markets" / "data" / "sanctions.csv"
    with data.open(encoding="utf-8", newline="") as handle:
        listed = {row["country"] for row in csv.DictReader(handle)}
    lines = [
        line
        for line in _global_equities().splitlines()
        if line.startswith("Sanctioned by default:")
    ]
    assert len(lines) == 1, "the section needs exactly one 'Sanctioned by default:' line"
    named = set(re.findall(r"`([A-Z]{2})`", lines[0]))
    assert named == listed, f"README names {sorted(named)}; sanctions.csv closes {sorted(listed)}"


def test_the_sources_table_matches_the_registered_universal_sources() -> None:
    """The sources table lists every universal source the registry holds, with its usage class.

    A source a reader cannot find in the table is a source whose terms nobody reviewed in
    print, and a row for a source that is not registered documents a feature that does not
    run. Both directions are compared, name to usage class, against the ``usage_class`` each
    registered universal class declares.
    """
    from crocodile.equity.providers import factory

    registered = {
        name: getattr(cls.usage_class, "value", cls.usage_class)
        for name, cls in factory._REGISTRY.items()
        if getattr(cls, "__module__", "").startswith("crocodile.equity.universal")
    }
    assert registered, "no universal source is registered; the table would be vacuous"
    documented: dict[str, str] = {}
    for cells in _table_after(_global_equities(), "### Sources and usage classes"):
        assert len(cells) == 3, f"a sources row does not have three cells: {cells}"
        documented[cells[0].strip("`")] = cells[-1].strip("`")
    assert documented == registered


def test_the_global_equities_section_explains_every_usage_class() -> None:
    """Every usage class the code defines is named, in backticks, in the global equities section.

    A reader who meets ``internal`` in the sources table must find what it means in the same
    section. Each member's value is searched for as a backticked token, so a class added to
    ``UsageClass`` without an explanation fails here.
    """
    from crocodile.core.markets.usage import UsageClass

    members = list(UsageClass)
    assert members, "UsageClass has no members"
    section = _global_equities()
    missing = [member.value for member in members if f"`{member.value}`" not in section]
    assert not missing, f"usage classes the section never names: {missing}"


def test_every_command_in_the_global_equities_section_exists_with_those_options() -> None:
    """Every command in the Run it block exists, and every option it uses exists on that command.

    The block is the reader's copy-paste path, so a flag renamed in the operator surface must
    fail here rather than in a shell. Each command must also carry every option it marks as
    required (``backfill`` needs its channel and its two bounds). ``checked`` must be at least
    five, so an emptied block fails rather than passing vacuously.
    """
    import click
    import typer

    from crocodile.surfaces.entrypoint import build_app

    group = typer.main.get_command(build_app())
    checked = 0
    for line in _code_lines(_markdown_section(_README, "### Run it")):
        tokens = shlex.split(line)
        assert "crocodile" in tokens, f"a Run it line does not invoke crocodile: {line!r}"
        tokens = tokens[tokens.index("crocodile") + 1 :]
        name = tokens[0]
        assert name in group.commands, f"`crocodile {name}` is not a command"
        command = group.commands[name]
        known = {"--help"}
        required: set[str] = set()
        for param in command.params:
            if isinstance(param, click.Argument):
                continue
            known |= set(param.opts) | set(getattr(param, "secondary_opts", ()))
            if param.required:
                required |= set(param.opts)
        used = {token.split("=", 1)[0] for token in tokens[1:] if token.startswith("--")}
        assert used <= known, f"`crocodile {name}` has no option {sorted(used - known)}"
        assert required <= used, f"`crocodile {name}` lacks required {sorted(required - used)}"
        checked += 1
    assert checked >= 5, f"only {checked} commands checked; the block is empty"


@pytest.mark.parametrize("which", ["readme", "changelog"])
def test_every_path_module_and_variable_the_global_notes_name_exists(which: str) -> None:
    """Every module, path and environment variable the global notes name exists in the tree.

    The notes point readers at code (``crocodile.core.markets.currency.to_major``), at files
    (``src/crocodile/core/markets/data/sanctions.csv``) and at settings
    (``CROCODILE_ALLOW_SANCTIONED_MARKETS``). Each name is resolved against the live package,
    the filesystem or the settings reader, so a rename leaves a red test instead of a dead
    pointer. Placeholders such as ``<data_dir>/...`` are not names and are skipped.
    """
    from crocodile.core.config import Settings

    section = _global_equities() if which == "readme" else _unreleased()
    source_text = "\n".join(path.read_text(encoding="utf-8") for path in _SRC.rglob("*.py"))
    settings_names = set(Settings.known_names())
    checked = 0
    for token in re.findall(r"`([^`\n]+)`", section):
        if token.startswith("<") or " " in token:
            continue
        if token.startswith(("src/", "scripts/", "docs/", "tests/")):
            assert (_REPO / token).exists(), f"`{token}` does not exist"
            checked += 1
        elif token.startswith("crocodile."):
            _resolve_dotted(token)
            checked += 1
        elif token.startswith("CROCODILE_"):
            name = token.split("=", 1)[0]
            assert name in settings_names or name in source_text, f"`{name}` is not read anywhere"
            checked += 1
    assert checked > 0, f"the {which} section names no path, module or variable"


def test_the_markets_columns_the_readme_lists_are_the_equity_report_columns(
    network_forbidden: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``Columns:`` line of the README is the column list the equity markets report returns.

    A reader who selects a column named in the README must find it in the output, in the order
    the README gives. The report is produced offline against an empty lake, which is the path
    the README's ``markets`` example takes on a fresh checkout.
    """
    from crocodile.core.config import Settings
    from crocodile.core.schema.enums import AssetClass
    from crocodile.core.store.catalog import Catalog
    from crocodile.surfaces import dispatch

    load_all()
    monkeypatch.setenv("CROCODILE_DATA_DIR", str(tmp_path))
    cap = REGISTRY["markets"]
    params = dispatch.build_params(cap, {"mic": "XIST"})
    with Catalog(tmp_path) as catalog:
        ctx = dispatch.build_context(
            catalog,
            AssetClass.EQUITY,
            settings=Settings.from_env(),
            readonly=True,
            row_limit=None,
        )
        result = dispatch.drive(dispatch.invoke(cap, ctx, params), row_limit=None)
    if hasattr(result, "columns"):
        columns = list(result.columns)
    else:
        columns = list(dispatch.payload(cap, result)["rows"][0])
    columns_line = next(
        (line for line in _global_equities().splitlines() if line.startswith("Columns:")),
        None,
    )
    assert columns_line is not None, "the Global equities section has no 'Columns:' line"
    assert re.findall(r"`([^`]+)`", columns_line) == columns


def test_the_changelog_opens_with_unreleased_notes_in_the_house_sections() -> None:
    """The first release heading is ``[Unreleased]`` or the package version, in house sections.

    The changelog is read top-down by people deciding what goes into the next release, so the
    unreleased notes come first and their headings are the four the project uses. A heading
    added or reordered without a decision fails here.
    """
    headings = [
        line
        for line in _CHANGELOG.read_text(encoding="utf-8").splitlines()
        if line.startswith("## [")
    ]
    version = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    assert headings, "the changelog has no release heading"
    first = headings[0]
    assert first == "## [Unreleased]" or re.fullmatch(
        rf"## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}", first
    ), f"first release heading {first!r} is neither [Unreleased] nor the package version {version}"
    sections = [line[4:] for line in _unreleased().splitlines() if line.startswith("### ")]
    assert sections == ["Added", "Changed", "Fixed", "Compatibility and limits"]


def test_every_class_member_the_unreleased_notes_name_exists() -> None:
    """Every ``Class.member`` token in the unreleased notes names a real field or member.

    The notes name new record fields, enum members and methods in backticks. A field that was
    renamed or never written would leave a changelog describing a schema the code does not
    have. Owners outside the documented namespace are skipped, and ``checked`` must be positive
    so the test cannot pass on notes that name nothing.
    """
    classes = _documented_classes()
    checked = 0
    for owner, member in re.findall(
        r"`([A-Z][A-Za-z0-9]+)\.([A-Za-z_][A-Za-z0-9_]*)`", _unreleased()
    ):
        target = classes.get(owner)
        if target is None:
            continue
        assert _has_member(target, member), f"`{owner}.{member}` does not exist"
        checked += 1
    assert checked > 0, "the unreleased notes name no Class.member token"


def test_the_unreleased_notes_count_exchanges_and_name_only_registered_bases() -> None:
    """The unreleased notes state the live exchange counts and name only registered bases.

    The changelog repeats the exchange count and the library-calendar count, so a change to the
    table moves the changelog too. The provenance line names the bases a lake row can carry; a
    name that is not registered would describe a provenance value no writer emits.
    """
    from crocodile.core.schema import provenance

    rows = _live_exchange_rows()
    total = len(rows)
    with_code = sum(1 for row in rows if row.xcal_code)  # type: ignore[attr-defined]
    text = _unreleased()
    counts = {int(match) for match in re.findall(r"\b(\d+) exchanges\b", text)}
    assert counts, "the unreleased notes state no exchange count"
    assert counts == {total}, f"the notes say {sorted(counts)} exchanges; the table holds {total}"
    pairs = {
        (int(a), int(b))
        for a, b in re.findall(r"\b(\d+) of the (\d+) exchanges have a library calendar", text)
    }
    assert pairs, "the unreleased notes state no library-calendar count"
    expected = {(with_code, total)}
    assert pairs == expected, f"the notes say {pairs}; the table says {expected}"
    provenance.load_all_bases()
    registered = provenance.registered_bases()
    line = next(line for line in text.splitlines() if "Provenance bases" in line)
    named = re.findall(r"`([a-z_]+)`", line)
    assert named, "the provenance line names no basis"
    assert set(named) <= registered, f"unregistered bases named: {sorted(set(named) - registered)}"


def test_the_one_line_descriptions_do_not_confine_equities_to_the_us() -> None:
    """The package description, package docstring and landing page drop "US equities".

    Those three lines are what a reader meets first (``help(crocodile)``, the package index,
    the server's root page), and they are the lines this release changed. Each must avoid the
    phrase, and the description and keywords must avoid the over-claims in ``_FORBIDDEN_CLAIMS``.
    The exchange table must also hold at least one non-US exchange, because a description that
    widens its claim has to be true of the data it describes.
    """
    import crocodile
    from crocodile.core.markets.exchanges import ExchangeRegistry

    project = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    server = (_SRC / "surfaces" / "server.py").read_text(encoding="utf-8")
    lines = {
        "project.description": project["description"],
        "crocodile.__doc__ line 1": (crocodile.__doc__ or "").splitlines()[0],
        "src/crocodile/surfaces/server.py": server,
    }
    for where, text in lines.items():
        assert "US equities" not in text, f"{where} still says 'US equities'"
    description = project["description"].lower()
    keywords = " ".join(project["keywords"]).lower()
    for claim in _FORBIDDEN_CLAIMS:
        assert claim not in description, f"description claims {claim!r}"
        assert claim not in keywords, f"keywords claim {claim!r}"
    assert any(row.country != "US" for row in ExchangeRegistry().all())  # type: ignore[attr-defined]


def test_the_keywords_this_release_adds_each_name_something_in_the_tree() -> None:
    """Each keyword this release adds has evidence in the tree: data, a dependency or a source.

    ``pyproject.toml`` says its keywords name things that exist today, so a keyword added without
    evidence is the aspirational term the comment forbids. ISO 10383 data sits beside the code,
    ``exchange-calendars`` is a declared dependency, and Yahoo and TradingView are both
    registered sources.
    """
    from crocodile.equity.providers import factory

    project = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    keywords = project["keywords"]
    for keyword in ("iso-10383", "exchange-calendars", "yahoo-finance", "tradingview"):
        assert keyword in keywords, f"keyword {keyword!r} is missing"
    data = _SRC / "core" / "markets" / "data"
    assert any(path.name.startswith("iso10383") for path in data.iterdir())
    assert any(
        dep.lower().startswith(("exchange_calendars", "exchange-calendars"))
        for dep in project["dependencies"]
    )
    assert "yahoo" in factory._REGISTRY and "yahoo_chart" in factory._REGISTRY
    assert "tradingview" in factory._REGISTRY


def test_the_registry_census_docstring_counts_its_own_set() -> None:
    """The Phase 2 census docstring states the size of the set it documents.

    ``_REGISTRY_AS_SHIPPED`` is the frozen Phase 2 census, and its docstring's count is
    history that later capabilities do not move. This pins the sentence to the set's own
    size, so an edit to the set that the sentence does not follow fails here. The set stays
    at 49 in this release, so this test passes at once; it is kept as a pin.
    """
    gates = _TESTS / "conformance" / "test_gates.py"
    tree = ast.parse(gates.read_text(encoding="utf-8"))
    size: int | None = None
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_REGISTRY_AS_SHIPPED"
        ):
            assert isinstance(node.value, ast.Call), "_REGISTRY_AS_SHIPPED is not a frozenset call"
            size = len(node.value.args[0].elts)  # type: ignore[attr-defined]
    assert size is not None, "test_gates.py has no _REGISTRY_AS_SHIPPED"
    doc = _assignment_docstring(gates, "_REGISTRY_AS_SHIPPED")
    assert f"The {size} capabilities" in doc, f"the census docstring does not say {size}"


def test_the_measurement_paragraph_states_the_scripts_own_bounds() -> None:
    """The measurement paragraph states the country count and per-endpoint cap the script uses.

    The README must not describe the live measurement with a looser bound than the script
    enforces. The count and the cap are imported from the script and written as words, so a
    change to either constant fails here until the paragraph follows it. The script is Task
    17's deliverable; if it is absent the test skips and says so.
    """
    script = _REPO / "scripts" / "measure_global_equity.py"
    if not script.exists():
        pytest.skip("scripts/measure_global_equity.py is absent; it is Task 17's deliverable")
    from scripts.measure_global_equity import DEFAULT_COUNTRIES, PER_ENDPOINT_CAP

    section = _markdown_section(_README, "### What is measured and what is not")
    countries = f"for {_NUMBER_WORDS[len(DEFAULT_COUNTRIES)]} countries"
    cap = f"at most {_NUMBER_WORDS[PER_ENDPOINT_CAP]} requests per endpoint"
    assert countries in section, f"the measurement paragraph lacks {countries!r}"
    assert cap in section, f"the measurement paragraph lacks {cap!r}"
