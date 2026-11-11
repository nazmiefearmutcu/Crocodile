"""A canonical symbol says which market it is in, however the parameter is spelled.

``resolve_asset_class`` reads the source out of ``source:RAW`` and asks the two source
registries which market claims it — evidence rather than a guess, and the reason a caller
does not have to name the asset class twice. The surfaces then handed it exactly one field,
the one literally named ``symbol``, and six two-implementation capabilities spell it
``symbols``: ``catalog-scan``, ``resolve-symbols``, ``replay``, ``export``, ``backfill`` and
``collect``.

So ``?symbol=deribit:BTC-PERPETUAL`` answered 200 and ``?symbols=deribit:BTC-PERPETUAL``
answered 400 *cannot tell which market*, for the same symbol, on the same lake. Loud, but it
made six capabilities unreachable without an out-of-band parameter the symbol already
determines.

It then happened again, silently, and the second time is why the last section of this file
exists. ``basis`` and ``spot-future-basis`` spell their legs ``spot_symbol``,
``perp_symbol`` and ``future_symbol``. While they were crypto-only they resolved by "there
is only one implementation"; the moment they grew equity halves — in a branch that touches
neither the dispatcher nor this file — that step stopped applying and both went unreachable
on CLI, REST and MCP at once. The list below was the *evidence* for the first fix and could
not notice the second: it names six capabilities by hand and nothing re-derives it.

So the population is derived from the registry instead, and the answer a capability gives is
checked rather than assumed.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

import msgspec
import pytest
from typer.testing import CliRunner

from crocodile.capabilities import load_all
from crocodile.core import capability
from crocodile.core.capability import (
    IRREDUCIBLE,
    REGISTRY,
    AssetClass,
    Capability,
    CapabilityContext,
    Impl,
    ReturnKind,
    declare,
)
from crocodile.core.config import Settings
from crocodile.core.errors import CapabilityUnavailable
from crocodile.core.schema.provenance import Provenance
from crocodile.surfaces import cli, dispatch, mcp, rest
from tests.surfaces.conftest import END_NS, START_NS, SYMBOL

_EQUITY_SYMBOL = "stooq:AAPL"
_CLAIMED_BY_BOTH = "alpaca:AAPL"
"""``alpaca`` is registered as a crypto exchange *and* as an equity provider."""


def _settings(lake: pathlib.Path) -> Settings:
    return Settings(data_dir=lake)


def _client(lake: pathlib.Path):  # starlette's TestClient, imported lazily
    from starlette.testclient import TestClient

    return TestClient(rest.build_app(settings=_settings(lake)), raise_server_exceptions=False)


_ARGUMENTS: dict[str, dict[str, Any]] = {
    "catalog-scan": {"channel": "trade", "symbols": SYMBOL, "start_ns": START_NS,
                     "end_ns": END_NS},
    "resolve-symbols": {"symbols": SYMBOL},
    "replay": {"channels": "trade", "symbols": SYMBOL, "start_ns": START_NS, "end_ns": END_NS},
    "export": {"channel": "trade", "symbols": SYMBOL, "start_ns": START_NS, "end_ns": END_NS,
               "dest": "/dev/null"},
    "backfill": {"source": "deribit", "channel": "trade", "symbols": SYMBOL,
                 "start_ns": START_NS, "end_ns": END_NS},
    "collect": {"sources": "deribit", "symbols": SYMBOL, "channels": "trade"},
}
"""The six capabilities that spell it ``symbols``, with enough of a request to build one."""


@pytest.mark.parametrize("name", sorted(_ARGUMENTS))
def test_the_market_is_inferred_from_a_sequence_of_symbols(name: str) -> None:
    """The whole path a surface takes: build the params, read the symbols off them, resolve.

    Reading the built struct rather than the raw request is what makes ``symbols`` work
    without a second guess about spelling: by then a sequence is a sequence, whether the
    transport delivered it as a list, a repeated flag or a comma-separated string.
    """
    cap = dispatch.resolve(name)
    assert len(cap.impls) > 1, f"{name} would have nothing to infer"
    params = dispatch.build_params(cap, dict(_ARGUMENTS[name]))
    assert dispatch.symbol_hints(params) == (SYMBOL,)
    assert dispatch.resolve_asset_class(cap, symbols=dispatch.symbol_hints(params)) is (
        AssetClass.CRYPTO
    )


def test_rest_serves_a_symbols_request_without_being_told_the_market(lake: pathlib.Path) -> None:
    """Answering at all is the claim; the envelope's class is asserted where it is a fact.

    ``catalog-scan`` reads the lake as a lake, so its envelope now reports ``any`` — see
    ``Capability.cross_market``. Reading the resolution off the payload was never the
    strongest way to observe it, and ``test_a_symbol_settles_the_market_for_a_sequence_field``
    above asserts the resolution itself.
    """
    response = _client(lake).get(
        "/api/v1/catalog-scan",
        params={"channel": "trade", "symbols": SYMBOL, "start_ns": START_NS, "end_ns": END_NS},
    )
    assert response.status_code == 200, response.text
    assert response.json()["provenance"]["asset_class"] == "any"


def test_the_cli_serves_a_symbols_request_without_being_told_the_market(
    lake: pathlib.Path,
) -> None:
    result = CliRunner().invoke(
        cli.build_app(),
        ["replay", "--channels", "trade", "--symbols", SYMBOL, "--start-ns", str(START_NS),
         "--end-ns", str(END_NS), "--limit", "2", "--data-dir", str(lake)],
    )
    assert result.exit_code == 0, result.output
    assert SYMBOL in result.output


def test_mcp_serves_a_symbols_request_without_being_told_the_market(lake: pathlib.Path) -> None:
    """Same as above: the call succeeds unnamed, and the envelope does not echo an input."""
    body = mcp.call_tool("resolve-symbols", {"symbols": [SYMBOL]}, settings=_settings(lake))
    assert body["rows"]
    assert body["provenance"]["asset_class"] == "any"


def test_a_single_symbol_still_settles_it(lake: pathlib.Path) -> None:
    """``slippage`` spells it ``symbol``, and the singular case must not regress."""
    response = _client(lake).get(
        "/api/v1/slippage", params={"symbol": SYMBOL, "side": "buy", "size": "1.0"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["provenance"]["asset_class"] == "crypto"


# ---------------------------------------------------------------------------
# What is still refused, and must be
# ---------------------------------------------------------------------------


def test_symbols_from_two_markets_are_refused_rather_than_resolved_by_position() -> None:
    """One request cannot be served by two implementations, and the first is not the answer.

    Picking the first symbol's market would send the whole request — including the equity
    symbols — into the crypto implementation, which is how a query comes back plausible and
    empty. The refusal names both.
    """
    with pytest.raises(ValueError, match="two markets"):
        dispatch.resolve_asset_class(dispatch.resolve("replay"), symbols=(SYMBOL, _EQUITY_SYMBOL))


def test_a_source_both_registries_claim_settles_nothing() -> None:
    """``alpaca`` is a crypto exchange and an equity provider; an overlap is an ambiguity."""
    with pytest.raises(ValueError, match="cannot tell which market"):
        dispatch.resolve_asset_class(dispatch.resolve("replay"), symbols=(_CLAIMED_BY_BOTH,))


def test_a_symbol_with_no_registered_source_settles_nothing() -> None:
    with pytest.raises(ValueError, match="cannot tell which market"):
        dispatch.resolve_asset_class(dispatch.resolve("replay"), symbols=("nosuchvenue:BTC",))


def test_an_explicit_asset_class_is_never_overridden_by_a_symbol() -> None:
    """Step 1 of the order, and the symbol does not get a vote against it.

    The second half needs a capability that genuinely has no equity implementation, and this
    fixture has now been chased twice: it was ``collect-market`` until M3 gave that an equity
    half, then ``open-interest`` until M1 gave that one — and the agent that made the second
    swap could not see the first, because both landed in the same phase from different
    branches. A third name would be chased too. The population it was drawn from is
    *scheduled asymmetry*, and emptying that population is exactly what Phase 3 was, so no
    member of it is a durable fixture by construction.

    So the fixture is derived rather than named, and drawn from :data:`IRREDUCIBLE` — the
    other kind of asymmetry, which is a claim about the market and therefore does not expire.
    The guard below is the part that matters: deriving a subject from a set means the test
    quietly stops testing anything if the set empties, which is the vacuous-green shape this
    codebase keeps finding. If ``IRREDUCIBLE`` is ever empty, this fails and says why rather
    than passing over nothing.
    """
    assert dispatch.resolve_asset_class(
        dispatch.resolve("replay"), explicit=AssetClass.EQUITY, symbols=(SYMBOL,)
    ) is AssetClass.EQUITY

    load_all()
    crypto_only = sorted(
        name
        for name in IRREDUCIBLE
        if (cap := REGISTRY.get(name)) is not None and set(cap.impls) == {AssetClass.CRYPTO}
    )
    assert crypto_only, (
        "no capability is crypto-only, so this test has no subject and would pass over "
        "nothing. IRREDUCIBLE is what guarantees one exists; an empty one means either the "
        "list was emptied or its entries stopped being asymmetric, and both are decisions "
        "somebody should have to make deliberately."
    )
    for name in crypto_only:
        with pytest.raises(CapabilityUnavailable):
            dispatch.resolve_asset_class(
                dispatch.resolve(name), explicit=AssetClass.EQUITY, symbols=(SYMBOL,)
            )


def test_a_field_that_is_not_a_symbol_is_not_read_as_one() -> None:
    """``sql`` may contain a colon, and free text is not evidence about a market.

    The fields consulted are named, not sniffed: taking any string with a colon in it would
    let ``SELECT * FROM t WHERE note = 'binance:x'`` decide which implementation runs.
    """
    query = dispatch.resolve("query")
    params = dispatch.build_params(query, {"sql": "SELECT 'binance:BTC' AS note"})
    assert dispatch.symbol_hints(params) == ()


def test_a_symbol_shaped_field_that_cannot_hold_a_string_is_not_a_symbol() -> None:
    """``collect-market`` carries ``all_symbols`` and ``max_symbols``, and has no symbol.

    The name convention alone would say it does, which would exempt it from the sweep below
    — the one check that would catch the *next* capability whose leg is spelled something
    the convention does not cover.
    """
    assert dispatch.symbol_field_names(dispatch.resolve("collect-market").params) == frozenset()


# ---------------------------------------------------------------------------
# The sweep: every capability with a choice to make, asked to make it
# ---------------------------------------------------------------------------


_NO_CANONICAL_SYMBOL: dict[str, str] = {
    # The subject is a store, a venue list or a free-text query — there is no instrument in
    # the request to read a source off, so naming the market is genuinely the caller's job.
    **dict.fromkeys(
        (
            "catalog",
            "catalog-channels",
            "catalog-dates",
            "catalog-exchanges",
            "catalog-inventory",
            "catalog-stats",
            "catalog-summary",
            "catalog-symbols",
            "census",
            "list-exchanges",
            "markets",
            "query",
            "search",
            "universe",
            "collect-market",
        ),
        "asks about a store or a venue, not about an instrument. Some of these do carry a "
        "bare `source` or `exchange` field, which is market evidence of a different kind — "
        "a source name rather than a `source:RAW` symbol — and reading it would be a "
        "separate rule with its own failure modes (an optional filter deciding which "
        "implementation runs). This gate is about symbols.",
    ),
    # Pure functions over caller-supplied numbers. `caller_supplied` is their provenance
    # basis for the same reason: nothing in the request came out of a lake.
    **dict.fromkeys(
        ("chaos-score", "funding-predict", "label-transfers", "smart-money"),
        "takes measurements the caller produced, not an instrument this engine stores. "
        "There is no symbol because there is no lookup.",
    ),
    # The options family keys on `underlying`, which is the *asset* an option is written on
    # — `BTC`, `AAPL` — and is stored on `OptionsChain.underlying` in exactly that
    # unqualified form. It is not `source:RAW` and carries no source, so there is nothing
    # for `resolve_asset_class` to look up; renaming it `underlying_symbol` would make this
    # gate pass while the lookup still failed, which is the worse outcome.
    **dict.fromkeys(
        ("iv-surface", "term-structure", "vol-skew", "risk-reversal"),
        "keys on `underlying`, which is an asset name and not a canonical symbol: "
        "`OptionsChain.underlying` stores `BTC` and `AAPL`, with no source to resolve.",
    ),
    # Asks about an exchange's sessions, named by its MIC in a bare `mic` field, not about an
    # instrument. Reading a market off that field would be the separate rule this table's
    # neighbours decline, and the crypto half has no MIC at all. This gate is about symbols.
    "calendar": (
        "asks about an exchange's sessions, named by its MIC in a bare `mic` field, not about "
        "an instrument. Reading a market off that field would be the separate rule this "
        "table's neighbours decline, and the crypto half has no MIC at all. This gate is about "
        "symbols."
    ),
}
"""Two-implementation capabilities that carry no canonical symbol, and why.

An entry silences the sweep, which is why the argument is mandatory — the same discipline
:data:`crocodile.core.capability.IRREDUCIBLE` and ``CONSTANT_BY_DEFINITION`` carry. A new
capability that spells its leg something the convention does not cover lands here with no
argument to write, which is the moment somebody has to decide rather than discover.
"""


def _multi_impl() -> list[str]:
    load_all()
    return sorted(name for name, cap in REGISTRY.items() if len(cap.impls) > 1)


def _sample(annotation: Any) -> Any:
    """A value of the declared type, for a field the sweep does not care about.

    Enough to get past ``build_params``; ``resolve_asset_class`` runs before any
    implementation does, so nothing here has to be semantically valid.
    """
    if hasattr(annotation, "__metadata__"):  # Annotated[T, msgspec.Meta(...)]
        return _sample(get_args(annotation)[0])
    origin = get_origin(annotation)
    if origin is Literal:
        return get_args(annotation)[0]
    if origin in (Union, UnionType):
        return _sample(next(a for a in get_args(annotation) if a is not type(None)))
    if origin in (list, tuple, set, frozenset):
        args = [a for a in get_args(annotation) if a is not Ellipsis]
        return [_sample(args[0])] if args else []
    if origin is dict:
        return {}
    if annotation is bool:
        return False
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0
    return "x"


def _request(cap: Capability) -> dict[str, Any]:
    """A minimal request for ``cap`` with one canonical crypto symbol in every symbol field.

    Derived from the params struct rather than written out per capability, which is the
    difference between this and :data:`_ARGUMENTS` above: a capability that grows a second
    leg gets swept without anybody remembering to extend a table.
    """
    symbols = dispatch.symbol_field_names(cap.params)
    values: dict[str, Any] = {}
    for field in msgspec.structs.fields(cap.params):
        if field.name in symbols:
            values[field.name] = [SYMBOL] if field.name in _sequence(cap) else SYMBOL
        elif field.required:
            values[field.name] = _sample(field.type)
    return values


def _sequence(cap: Capability) -> frozenset[str]:
    from crocodile.surfaces.dispatch import _sequence_fields

    return _sequence_fields(cap.params)


@pytest.mark.parametrize("name", _multi_impl())
def test_a_capability_with_a_choice_to_make_can_make_it_from_its_symbols(name: str) -> None:
    """Every two-implementation capability either reads its market off a symbol, or argues.

    This is the check that was missing both times. ``_SYMBOL_FIELDS`` was widened once by
    hand and went stale again on the next capability to grow a second implementation, and
    nothing in either branch's diff could have shown it: one branch owned the dispatcher,
    the other owned the capability. The subject here is the registry, so a capability that
    becomes two-implementation is swept the moment it does.
    """
    cap = dispatch.resolve(name)
    fields = dispatch.symbol_field_names(cap.params)
    if not fields:
        assert name in _NO_CANONICAL_SYMBOL, (
            f"{name} has {len(cap.impls)} implementations and no field this dispatcher "
            f"reads a symbol off, so it cannot be called without --asset-class. Either its "
            f"symbol is spelled something `symbol_field_names` does not recognise, or it "
            f"genuinely has none — say which in _NO_CANONICAL_SYMBOL."
        )
        assert _NO_CANONICAL_SYMBOL[name].strip(), f"{name} is exempted with no argument"
        return
    params = dispatch.build_params(cap, _request(cap))
    hints = dispatch.symbol_hints(params)
    assert set(hints) == {SYMBOL}, (
        f"{name} declares {sorted(fields)} and symbol_hints returned {hints}"
    )
    assert dispatch.resolve_asset_class(cap, symbols=hints) is AssetClass.CRYPTO


def test_the_exemption_list_holds_no_stale_entry() -> None:
    """A capability that gained a symbol, or lost its second implementation, must not keep
    an exemption it no longer needs — that is how a list starts describing a tree that has
    moved."""
    swept = set(_multi_impl())
    stale = sorted(
        name
        for name in _NO_CANONICAL_SYMBOL
        if name not in swept
        or dispatch.symbol_field_names(dispatch.resolve(name).params)
    )
    assert not stale, f"_NO_CANONICAL_SYMBOL names capabilities that no longer need it: {stale}"


def test_the_sweep_has_subjects_on_both_sides() -> None:
    """Deriving a population means the test quietly stops testing if it empties.

    Both halves matter: no symbol-bearing capability and the resolution assertion never
    runs; no exempt one and the argument requirement never runs.
    """
    swept = _multi_impl()
    assert swept, "no capability has two implementations, so this sweep has no subject"
    with_symbols = [
        name for name in swept if dispatch.symbol_field_names(dispatch.resolve(name).params)
    ]
    assert with_symbols, "no two-implementation capability carries a symbol"
    assert _NO_CANONICAL_SYMBOL, "no capability is exempt, so the argument gate is vacuous"


@pytest.mark.parametrize("name", ["basis", "spot-future-basis"])
def test_the_two_capabilities_the_merge_made_unreachable_resolve_from_their_legs(
    name: str,
) -> None:
    """The regression itself, pinned by name as well as by the sweep.

    ``crocodile basis --spot-symbol deribit:BTC-SPOT --perp-symbol deribit:BTC-PERPETUAL``
    exited with *cannot tell which market 'basis' should serve*, and
    ``GET /api/v1/basis?spot_symbol=…`` answered 400, while ``/api/v1/perp-basis?symbol=…``
    answered 200 for the same lake — the difference being only how the leg is spelled. The
    field names are a pinned pre-merge contract
    (``tests/conformance/premerge_phase2_surface.json`` records ``get_spot_perp_basis`` and
    ``get_spot_future_basis`` taking exactly these), so the spelling was never the thing to
    change.
    """
    cap = dispatch.resolve(name)
    assert len(cap.impls) > 1, f"{name} would have nothing to infer"
    assert dispatch.symbol_field_names(cap.params) >= {"spot_symbol"}
    params = dispatch.build_params(cap, _request(cap))
    assert dispatch.resolve_asset_class(
        cap, symbols=dispatch.symbol_hints(params)
    ) is AssetClass.CRYPTO


def test_rest_serves_a_basis_request_without_being_told_the_market(lake: pathlib.Path) -> None:
    response = _client(lake).get(
        "/api/v1/basis",
        params={
            "spot_symbol": SYMBOL,
            "perp_symbol": SYMBOL,
            "start_ns": START_NS,
            "end_ns": END_NS,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["provenance"]["asset_class"] == "crypto"


def test_the_cli_serves_a_spot_future_basis_request_without_being_told_the_market(
    lake: pathlib.Path,
) -> None:
    result = CliRunner().invoke(
        cli.build_app(),
        ["spot-future-basis", "--future-symbol", SYMBOL, "--spot-symbol", SYMBOL,
         "--start-ns", str(START_NS), "--end-ns", str(END_NS), "--data-dir", str(lake)],
    )
    assert result.exit_code == 0, result.output


def test_mcp_serves_a_basis_request_without_being_told_the_market(lake: pathlib.Path) -> None:
    body = mcp.call_tool(
        "basis",
        {
            "spot_symbol": SYMBOL,
            "perp_symbol": SYMBOL,
            "start_ns": START_NS,
            "end_ns": END_NS,
        },
        settings=_settings(lake),
    )
    assert body["provenance"]["asset_class"] == "crypto"
# ---------------------------------------------------------------------------
# A cross-market answer says so instead of echoing what it was told
# ---------------------------------------------------------------------------


_CROSS_MARKET_REQUESTS: dict[str, dict[str, str]] = {
    # The three the exit review drove, each one returning symbols from both markets while
    # the envelope reported whichever class the caller had been forced to name.
    "search": {"q": "BTC"},
    "catalog-symbols": {},
    "catalog-exchanges": {},
}


def test_a_cross_market_capability_is_one_implementation_and_says_so() -> None:
    """The declaration is checkable, so it is checked rather than believed.

    An answer that does not depend on the market cannot have two implementations, two
    provenance ceilings or two bases — so a capability claiming ``cross_market`` and holding
    any of those is claiming something its own declaration contradicts.
    """
    from crocodile.core.capability import REGISTRY

    dispatch.wire_names()
    declared = [cap for cap in REGISTRY.values() if cap.cross_market]
    assert len(declared) > 5, "nothing claims to be cross-market; this gate proves nothing"
    for cap in declared:
        assert len({impl.fn for impl in cap.impls.values()}) == 1, cap.name
        assert len({(impl.prov, impl.basis) for impl in cap.impls.values()}) == 1, cap.name
        assert len(cap.impls) > 1, f"{cap.name} serves one market; it is not cross-market"


@pytest.mark.parametrize("wire", sorted(_CROSS_MARKET_REQUESTS))
def test_naming_either_market_gives_the_identical_answer(
    lake: pathlib.Path, wire: str
) -> None:
    """Measured, not declared: the two classes are driven and the rows compared.

    ``search?q=EQ&asset_class=crypto`` answered 200 with three ``stooq:`` equity symbols and
    stamped ``provenance.asset_class: "crypto"``. The caller could not omit the class — a
    hard 400 — any value was accepted, and the answer ignored it. Echoing an input back as a
    property of the answer is the part a caller can be misled by: filtering results on that
    field would partition a cross-market answer by an argument.
    """
    arguments = _CROSS_MARKET_REQUESTS[wire]
    answers = [
        mcp.call_tool(wire, {**arguments, "asset_class": value}, settings=_settings(lake))
        for value in ("crypto", "equity")
    ]
    assert answers[0]["rows"] == answers[1]["rows"], wire
    for answer in answers:
        assert answer["provenance"]["asset_class"] == "any", wire


def test_a_capability_that_does_serve_one_market_still_reports_which(
    lake: pathlib.Path,
) -> None:
    """The other side of the line, so ``any`` stays a statement rather than a blanket."""
    body = mcp.call_tool(
        "slippage",
        {"symbol": SYMBOL, "side": "buy", "size": 1.0, "asset_class": "crypto"},
        settings=_settings(lake),
    )
    assert body["provenance"]["asset_class"] == "crypto"
    assert dispatch.resolve("slippage").cross_market is False


# ---------------------------------------------------------------------------
# LOCAL@MIC: a listing symbol names the equity market (spec 3.11, Dispatch)
# ---------------------------------------------------------------------------


def test_a_listing_symbol_naming_a_registered_mic_settles_equity() -> None:
    """``THYAO@XIST`` is a canonical listing symbol; its MIC is in the ISO 10383 registry."""
    cap = dispatch.resolve("replay")
    assert dispatch.resolve_asset_class(cap, symbols=("THYAO@XIST",)) is AssetClass.EQUITY

    params = dispatch.build_params(
        cap, {**_ARGUMENTS["replay"], "symbols": "THYAO@XIST,7203@XJPX"}
    )
    assert dispatch.resolve_asset_class(cap, symbols=dispatch.symbol_hints(params)) is (
        AssetClass.EQUITY
    )


def test_a_segment_mic_is_still_registry_evidence() -> None:
    """``7203@XTKS`` names a segment of XJPX; membership counts segments on purpose."""
    cap = dispatch.resolve("replay")
    assert dispatch.resolve_asset_class(cap, symbols=("7203@XTKS",)) is AssetClass.EQUITY


def test_an_expired_mic_still_names_the_equity_market() -> None:
    """XTUR is expired (use XIST). The equity half then refuses with the registry's redirect;
    a dispatcher that answered "cannot tell which market" here would hide that message."""
    cap = dispatch.resolve("replay")
    assert dispatch.resolve_asset_class(cap, symbols=("THYAO@XTUR",)) is AssetClass.EQUITY


@pytest.mark.parametrize("symbol", ["FOO@ZZZZ", "THYAO@xist"])
def test_an_at_sign_naming_no_registered_mic_settles_nothing(symbol: str) -> None:
    """MICs are exact: an unregistered code and a lower-case one are not evidence."""
    cap = dispatch.resolve("replay")
    with pytest.raises(ValueError, match="cannot tell which market"):
        dispatch.resolve_asset_class(cap, symbols=(symbol,))


def test_a_bare_us_ticker_is_still_not_evidence() -> None:
    cap = dispatch.resolve("replay")
    with pytest.raises(ValueError, match="cannot tell which market"):
        dispatch.resolve_asset_class(cap, symbols=("AAPL",))


def test_a_listing_symbol_and_a_crypto_symbol_together_refuse() -> None:
    cap = dispatch.resolve("replay")
    with pytest.raises(ValueError, match="two markets"):
        dispatch.resolve_asset_class(cap, symbols=("THYAO@XIST", SYMBOL))


def test_a_listing_symbol_does_not_choose_a_market_a_capability_lacks() -> None:
    """A crypto-only capability has no equity claim to make, so the single implementation
    answers, exactly as before this rule existed."""
    load_all()
    crypto_only = sorted(
        name
        for name in IRREDUCIBLE
        if (cap := REGISTRY.get(name)) is not None and set(cap.impls) == {AssetClass.CRYPTO}
    )
    assert crypto_only, "no crypto-only capability, so this test would pass over nothing"
    for name in crypto_only:
        cap = dispatch.resolve(name)
        assert dispatch.resolve_asset_class(cap, symbols=("THYAO@XIST",)) is AssetClass.CRYPTO


class _MarketOfParams(msgspec.Struct, frozen=True):
    symbol: str


def _served_by_crypto(ctx: CapabilityContext, params: _MarketOfParams) -> dict[str, str]:
    return {"served": ctx.asset_class.value}


def _served_by_equity(ctx: CapabilityContext, params: _MarketOfParams) -> dict[str, str]:
    return {"served": ctx.asset_class.value}


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


def test_three_surfaces_resolve_a_listing_symbol_without_being_told_the_market(
    lake: pathlib.Path, _isolate: None
) -> None:
    """REST, MCP and the CLI each hand the built struct to the same resolver, so a listing
    symbol alone settles the equity half on all three."""
    declare(
        Capability(
            name="fixture-market-of",
            summary="Registered by a test, answers with the market it was served in.",
            params=_MarketOfParams,
            returns=ReturnKind.SCALAR,
            impls={
                AssetClass.CRYPTO: Impl(
                    fn=_served_by_crypto, prov=Provenance.NATIVE, basis="native"
                ),
                AssetClass.EQUITY: Impl(
                    fn=_served_by_equity, prov=Provenance.NATIVE, basis="native"
                ),
            },
        )
    )

    response = _client(lake).get("/api/v1/fixture-market-of", params={"symbol": "THYAO@XIST"})
    assert response.status_code == 200, response.text
    assert response.json()["result"]["served"] == "equity"
    assert response.json()["provenance"]["asset_class"] == "equity"

    body = mcp.call_tool(
        "fixture-market-of", {"symbol": "THYAO@XIST"}, settings=_settings(lake)
    )
    assert body["result"]["served"] == "equity"

    result = CliRunner().invoke(
        cli.build_app(),
        ["fixture-market-of", "--symbol", "THYAO@XIST", "--data-dir", str(lake)],
    )
    assert result.exit_code == 0, result.output
    assert "equity" in result.output
