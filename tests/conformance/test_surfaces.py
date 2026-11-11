"""Gate 4 — every capability appears in all three surfaces, exactly once each.

This is the assertion the whole surface phase is measured by. Six hand-written stacks
drifted apart because nothing ever compared them; the projection is only worth anything if
something does.

Two things this file is careful about, both learned from earlier gates in this merge:

* It must not be **vacuously green**. The registry holds two capabilities, so a gate that
  only ever sees conforming input has not been shown to reject anything. Every comparison
  below is therefore also driven with a fixture — a capability registered mid-test, a
  surface made to forget a name — so the failing branch is exercised rather than assumed.
* It reads the **built artefacts**, not the registry. Asking each surface to re-derive its
  own name list from ``REGISTRY`` and then comparing the three answers proves only that
  ``REGISTRY`` equals itself. The CLI list comes off the Typer app's command table, the
  REST list off the router's routes, the MCP list off the ``tools/list`` payload.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import msgspec
import pytest

from crocodile.core import capability
from crocodile.core.capability import (
    REGISTRY,
    AssetClass,
    Capability,
    CapabilityContext,
    Impl,
    ReturnKind,
    declare,
)
from crocodile.core.schema.provenance import Provenance
from crocodile.surfaces import cli, dispatch, mcp, rest


class _Params(msgspec.Struct, frozen=True):
    symbol: str


def _fixture_impl(ctx: CapabilityContext, params: _Params) -> str:
    return params.symbol


@pytest.fixture
def _isolate() -> Iterator[None]:
    """The registry is module state and all three surfaces read it live."""
    registry = dict(REGISTRY)
    declared = set(capability._DECLARED_NAMES)
    try:
        yield
    finally:
        REGISTRY.clear()
        REGISTRY.update(registry)
        capability._DECLARED_NAMES.clear()
        capability._DECLARED_NAMES.update(declared)


def _fixture_capability(name: str, *, aliases: tuple[str, ...] = ()) -> Capability:
    impl = Impl(fn=_fixture_impl, prov=Provenance.NATIVE, basis="native")
    return Capability(
        name=name,
        summary="Registered by a test, projected by every surface.",
        params=_Params,
        returns=ReturnKind.SCALAR,
        aliases=aliases,
        impls={AssetClass.CRYPTO: impl, AssetClass.EQUITY: impl},
    )


SURFACES: dict[str, Callable[[], set[str]]] = {
    "cli": cli.command_names,
    "rest": lambda: {path.removeprefix(f"{rest.API_PREFIX}/") for path in rest.route_paths()},
    "mcp": mcp.tool_names,
}
"""The three projections, each reduced to the set of names it answers to."""


# ---------------------------------------------------------------------------
# Gate 4
# ---------------------------------------------------------------------------


def test_gate4_there_is_something_to_project() -> None:
    """An empty registry would make every assertion in this file vacuously true."""
    assert dispatch.wire_names(), "no capabilities declared; Gate 4 would prove nothing"


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_gate4_every_capability_appears_in_every_surface(surface: str) -> None:
    expected = set(dispatch.wire_names())
    actual = SURFACES[surface]()
    assert actual == expected, (
        f"{surface} is missing {sorted(expected - actual)} and invents "
        f"{sorted(actual - expected)}"
    )


def test_gate4_the_three_surfaces_agree_with_each_other() -> None:
    """Stated separately from the registry comparison because it is a different failure.

    Each surface could match a registry that changed under it — a capability imported by
    one process and not another — and still leave the three disagreeing. Comparing them to
    each other is what catches that, and it is the property the six legacy stacks lost.
    """
    projections = {name: build() for name, build in SURFACES.items()}
    distinct = {frozenset(names) for names in projections.values()}
    assert len(distinct) == 1, {name: sorted(names) for name, names in projections.items()}


def test_gate4_no_capability_is_implemented_more_than_once_per_surface() -> None:
    """One command, one route, one tool. A second is a second thing to keep in step."""
    commands = [command.name for command in cli.build_app().registered_commands]
    assert len(commands) == len(set(commands)), f"duplicate CLI commands: {sorted(commands)}"

    tools = [tool["name"] for tool in mcp.tool_definitions()]
    assert len(tools) == len(set(tools)), f"duplicate MCP tools: {sorted(tools)}"

    paths = [str(getattr(route, "path", "")) for route in rest.build_app().routes]
    api_paths = [path for path in paths if path.startswith(f"{rest.API_PREFIX}/")]
    assert len(api_paths) == len(set(api_paths)), f"duplicate REST routes: {sorted(api_paths)}"


def test_gate4_every_alias_resolves_to_its_capability_on_every_surface() -> None:
    """An alias is a redirect, so it must be present *and* point at one capability."""
    names = dispatch.wire_names()
    aliases = {alias: cap.name for cap in REGISTRY.values() for alias in cap.aliases}
    assert aliases, "no aliases declared; this gate would prove nothing"
    for alias, target in aliases.items():
        assert names[alias] == target
        for surface, build in SURFACES.items():
            assert alias in build(), f"{surface} does not answer to the retired spelling {alias!r}"


# ---------------------------------------------------------------------------
# The rejecting branches, driven rather than assumed
# ---------------------------------------------------------------------------


def test_a_capability_registered_mid_test_appears_in_all_three_surfaces(_isolate: None) -> None:
    """The accepting branch, and the strongest evidence these are projections.

    Nothing in ``cli.py``, ``rest.py`` or ``mcp.py`` mentions this capability, and no file
    was edited to add it. If a surface were a copy with a loop around it, this is the test
    that could not pass.
    """
    declare(_fixture_capability("fixture-projected", aliases=("fixture-old-spelling",)))
    for surface, build in SURFACES.items():
        names = build()
        assert "fixture-projected" in names, f"{surface} did not project a new capability"
        assert "fixture-old-spelling" in names, f"{surface} did not project its alias"


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_a_surface_that_drops_a_capability_fails_gate_4(
    surface: str, monkeypatch: pytest.MonkeyPatch, _isolate: None
) -> None:
    """Break one projection on purpose; the parity gate must name it.

    The drop is simulated at the surface's own accessor rather than by deleting a route,
    because what Gate 4 is defending against is a capability that stops being *reachable* —
    however that happens — and the accessor is what every reachability question goes
    through.
    """
    complete = SURFACES[surface]()
    victim = sorted(complete)[0]
    monkeypatch.setitem(SURFACES, surface, lambda: complete - {victim})

    with pytest.raises(AssertionError, match=victim):
        test_gate4_every_capability_appears_in_every_surface(surface)
    with pytest.raises(AssertionError):
        test_gate4_the_three_surfaces_agree_with_each_other()


def test_two_capabilities_claiming_one_wire_name_are_refused(_isolate: None) -> None:
    """The "implemented more than once" case that a per-surface count cannot see.

    Two declarations answering to one string is the original bug — two things behind one
    command — and it must not be resolvable by import order, so ``wire_names`` refuses
    rather than letting whichever loaded first win.
    """
    declare(_fixture_capability("fixture-one"))
    declare(_fixture_capability("fixture-two", aliases=("fixture-one",)))
    with pytest.raises(ValueError, match="fixture-one"):
        dispatch.wire_names()


def test_an_alias_shadowing_another_capabilitys_alias_is_refused(_isolate: None) -> None:
    declare(_fixture_capability("fixture-three", aliases=("fixture-shared",)))
    declare(_fixture_capability("fixture-four", aliases=("fixture-shared",)))
    with pytest.raises(ValueError, match="fixture-shared"):
        dispatch.wire_names()


def test_a_params_field_that_shadows_a_cli_option_is_refused(_isolate: None) -> None:
    """``--data-dir`` chooses the lake; a params field of that name would silently take it."""

    class _Shadowing(msgspec.Struct, frozen=True):
        data_dir: str

    impl = Impl(fn=_fixture_impl, prov=Provenance.NATIVE, basis="native")
    declare(
        Capability(
            name="fixture-shadow",
            summary="Its params collide with an option the CLI adds.",
            params=_Shadowing,
            returns=ReturnKind.SCALAR,
            impls={AssetClass.CRYPTO: impl, AssetClass.EQUITY: impl},
        )
    )
    with pytest.raises(ValueError, match="data_dir"):
        cli.build_app()


# ---------------------------------------------------------------------------
# `readonly` is a property of the surface, not a parameter
# ---------------------------------------------------------------------------


def test_no_params_struct_carries_a_sql_trust_flag() -> None:
    """A surface's trust level must not be something a caller can send.

    ``readonly`` arriving as a parameter is a network caller choosing whether to be
    guarded. It lives on the context, which only a surface constructs.
    """
    offenders = [
        f"{cap.name}.{field.name}"
        for cap in REGISTRY.values()
        for field in msgspec.structs.fields(cap.params)
        if field.name in {"readonly", "row_limit", "trusted", "unsafe"}
    ]
    assert not offenders, (
        f"trust settings in a params struct: {offenders}. They belong on "
        f"CapabilityContext, which the surface builds and the caller cannot."
    )


def test_the_context_carries_no_default_trust_level() -> None:
    """``build_context`` takes ``readonly`` and ``row_limit`` keyword-only and undefaulted.

    A default is how a fourth surface would inherit a posture nobody chose for it, which is
    the accident that gave one ``query`` capability three behaviours across six stacks.
    """
    import inspect

    signature = inspect.signature(dispatch.build_context)
    for name in ("readonly", "row_limit"):
        parameter = signature.parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameter.default is inspect.Parameter.empty, f"{name} has a default"


def test_the_calendar_is_served_on_get_because_every_parameter_is_a_scalar() -> None:
    """``start`` and ``end`` are strings, so no field is structured and REST may serve GET."""
    cap = dispatch.resolve("calendar")
    assert dispatch.structured_fields(cap) == frozenset()
    assert rest.methods_for(cap) == ["GET", "POST"]


def test_the_market_dimensions_are_published_on_every_surface() -> None:
    """``mic`` and ``country`` (and ``calendar``'s ``start``/``end``) reach MCP, the CLI and
    the REST document, so no surface can silently lack a parameter another one offers."""
    import inspect

    expected = {
        "markets": {"mic", "country"},
        "universe": {"mic", "country"},
        "calendar": {"start", "end", "mic"},
    }
    tools = {tool["name"]: tool for tool in mcp.tool_definitions()}
    commands = {command.name: command for command in cli.build_app().registered_commands}
    for name, names in expected.items():
        properties = set(tools[name]["inputSchema"]["properties"])
        assert names <= properties, (name, names - properties)
        cli_params = set(inspect.signature(commands[name].callback).parameters)
        assert names <= cli_params, (name, names - cli_params)
        rest_params = {param["name"] for param in rest._openapi_parameters(dispatch.resolve(name))}
        assert names <= rest_params, (name, names - rest_params)
