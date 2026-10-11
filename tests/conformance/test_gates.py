"""The contracts that make symmetry a build failure.

Gate 1 — every record carries the canonical header.
Gate 2 — every capability is implemented for both asset classes (Task 13).
Gate 3 — every emitted prov_basis has a registered formula (Task 13).
Gate 4 — every capability appears in all three surfaces (Phase 2).
"""

import ast
import inspect
import itertools
import operator
import pathlib
import re
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from typing import NamedTuple, get_args

import msgspec
import pytest

from crocodile.core.schema import records
from crocodile.core.schema.enums import CHANNEL_SUCCESSORS, AssetClass, Channel, Side
from crocodile.core.schema.provenance import Provenance, provenance_fields
from crocodile.core.schema.records import OHLCV, Funding, OptionsChain, Record, Trade, _Header

CANONICAL_HEADER = (
    "source",
    "symbol",
    "symbol_raw",
    "local_ts",
    "asset_class",
    "source_ts",
    "prov",
    "prov_basis",
    "prov_confidence",
    "prov_inputs",
)


def _declared_record_types() -> tuple[type[msgspec.Struct], ...]:
    """Every tagged struct *declared in* ``records.py``, found without consulting ``Record``.

    The subject list must not come from the thing under test: a struct left out of the
    ``Record`` union is undecodable, and if the union also chose the tests, nothing would
    notice. Merging two modules whose tags overlap on nine names makes exactly that the
    likeliest mistake.
    """
    found = {
        obj: None
        for obj in vars(records).values()
        if isinstance(obj, type)
        and issubclass(obj, msgspec.Struct)
        and obj.__module__ == records.__name__
        and obj.__struct_config__.tag is not None
    }
    # Deduplicated by identity, not by name: while ``Record`` is a single-member alias it
    # is a second module-level binding of the same class, and counting it twice would make
    # the tag-uniqueness check fail on a module that is perfectly correct.
    return tuple(found)


def _union_members() -> tuple[type[msgspec.Struct], ...]:
    """The members of the ``Record`` union, tolerating the single-member alias form."""
    args = get_args(Record)
    return args if args else (Record,)


def _a_trade(**overrides: object) -> Trade:
    """A minimal valid Trade, so each test states only the field it is about."""
    kwargs: dict[str, object] = {
        "source": "deribit",
        "symbol": "deribit:BTC-PERPETUAL",
        "symbol_raw": "BTC-PERPETUAL",
        "local_ts": 1_700_000_000_000_000_000,
        "asset_class": AssetClass.CRYPTO,
        "source_ts": None,
        "id": "42",
        "price": 42_000.5,
        "amount": 0.25,
        "side": Side.BUY,
    }
    kwargs.update(overrides)
    return Trade(**kwargs)  # type: ignore[arg-type]


def test_the_record_module_declares_at_least_one_record() -> None:
    assert len(_declared_record_types()) > 0


def test_every_declared_record_is_reachable_through_the_union() -> None:
    declared = set(_declared_record_types())
    union = set(_union_members())
    assert declared == union, (
        f"declared but not in Record (undecodable): "
        f"{sorted(c.__name__ for c in declared - union)}; "
        f"in Record but not declared here: {sorted(c.__name__ for c in union - declared)}"
    )


@pytest.mark.parametrize("cls", _declared_record_types(), ids=lambda c: c.__name__)
def test_gate1_record_header_conformance(cls: type[msgspec.Struct]) -> None:
    names = tuple(f.name for f in msgspec.structs.fields(cls))
    assert names[: len(CANONICAL_HEADER)] == CANONICAL_HEADER, (
        f"{cls.__name__} header is {names[: len(CANONICAL_HEADER)]}"
    )


@pytest.mark.parametrize("cls", _declared_record_types(), ids=lambda c: c.__name__)
def test_gate1_every_record_is_a_frozen_tagged_struct(cls: type[msgspec.Struct]) -> None:
    cfg = cls.__struct_config__
    assert cfg.frozen, f"{cls.__name__} is not frozen"
    assert cfg.tag_field == "channel", f"{cls.__name__} discriminates on {cfg.tag_field!r}"
    assert isinstance(cfg.tag, str) and cfg.tag, f"{cls.__name__} has no tag"


@pytest.mark.parametrize("cls", _declared_record_types(), ids=lambda c: c.__name__)
def test_gate1_every_tag_is_a_channel_member(cls: type[msgspec.Struct]) -> None:
    tag = cls.__struct_config__.tag
    assert tag in {c.value for c in Channel}, f"{cls.__name__} tag {tag!r} is not a Channel"


def test_gate1_tags_are_unique() -> None:
    counts = Counter(cls.__struct_config__.tag for cls in _declared_record_types())
    assert [tag for tag, n in counts.items() if n > 1] == []


@pytest.mark.parametrize("cls", _declared_record_types(), ids=lambda c: c.__name__)
def test_gate1_no_record_defaults_to_unavailable(cls: type[msgspec.Struct]) -> None:
    """UNAVAILABLE is a capability-envelope state, never a record state.

    A record on disk is always a real observation; a capability with nothing to return
    says so in its envelope rather than fabricating hole-records.
    """
    prov = next(f for f in msgspec.structs.fields(cls) if f.name == "prov")
    assert prov.default is not Provenance.UNAVAILABLE, (
        f"{cls.__name__} defaults prov to UNAVAILABLE; a record is always a real observation"
    )


def test_gate1_the_header_base_is_not_kw_only() -> None:
    """``kw_only`` belongs on the records, never on ``_Header``.

    msgspec orders positional fields ahead of keyword-only ones. Marking the base
    ``kw_only`` while a record is not flips the order to subclass-first and silently
    destroys the header-leads invariant, so probe the base directly rather than waiting
    for a downstream ordering test to notice.
    """
    kinds = {p.name: p.kind for p in inspect.signature(_Header).parameters.values()}
    assert kinds, "_Header has no fields"
    keyword_only = sorted(n for n, k in kinds.items() if k is inspect.Parameter.KEYWORD_ONLY)
    assert keyword_only == [], (
        f"_Header fields are keyword-only ({keyword_only}); "
        f"kw_only belongs on the record structs, not the base"
    )


@pytest.mark.parametrize("cls", _declared_record_types(), ids=lambda c: c.__name__)
def test_gate1_record_bodies_are_kw_only(cls: type[msgspec.Struct]) -> None:
    """The record's own fields are keyword-only; that is what lets the tail live in the base."""
    header = set(CANONICAL_HEADER)
    body = [
        p.name
        for p in inspect.signature(cls).parameters.values()
        if p.name not in header and p.kind is not inspect.Parameter.KEYWORD_ONLY
    ]
    assert body == [], f"{cls.__name__} is missing kw_only=True; positional body fields: {body}"


def test_gate1_round_trips_through_msgspec() -> None:
    original = _a_trade()
    blob = msgspec.json.encode(original)
    assert msgspec.json.decode(blob, type=Record) == original


def test_gate1_source_ts_is_required() -> None:
    """An adapter must state whether the venue supplied a timestamp, as crypto always made it."""
    with pytest.raises(TypeError, match="source_ts"):
        Trade(  # type: ignore[call-arg]
            source="deribit",
            symbol="deribit:BTC-PERPETUAL",
            symbol_raw="BTC-PERPETUAL",
            local_ts=1,
            asset_class=AssetClass.CRYPTO,
            id="1",
            price=1.0,
            amount=1.0,
            side=Side.SELL,
        )


def test_gate1_default_tail_matches_what_provenance_fields_produces() -> None:
    """One encoding of "native", not two.

    A default-constructed record and ``provenance_fields("native")`` must agree, or
    ``WHERE prov_basis = 'native'`` misses every default-constructed record.
    """
    t = _a_trade()
    assert (t.prov, t.prov_basis, t.prov_confidence, t.prov_inputs) == provenance_fields("native")


def test_gate1_mutable_prov_inputs_default_is_not_shared() -> None:
    a, b = _a_trade(), _a_trade()
    a.prov_inputs.append("MUTATED")
    assert b.prov_inputs == []
    assert _a_trade().prov_inputs == []


def test_gate1_channel_enum_covers_every_record_tag() -> None:
    """``Channel`` names at least every tag a record declares.

    A subset, deliberately not an equality: ``Channel`` also carries members whose
    structs arrive with the equity port, plus ``bar`` and ``option_quote``, which have
    stored data in existing lakes and stay after their structs collapse into ``ohlcv``
    and ``options_chain``. What keeps those partitions readable is the read path
    ``CHANNEL_SUCCESSORS`` declares, not the member; see the gate below.
    """
    tags = {c.__struct_config__.tag for c in _declared_record_types()}
    members = {c.value for c in Channel}
    assert tags <= members, f"record tags with no Channel member: {sorted(tags - members)}"


def test_gate1_channel_members_without_a_record_have_a_read_path() -> None:
    """Channel may outlive a struct, but only with somewhere for its rows to go.

    This gate used to assert that the member was *declared*, and called deleting one
    "data loss wearing a passing build". The declaration prevented nothing. A lake
    holding ``channel=bar/`` alongside ``channel=ohlcv/`` returned the ``ohlcv`` half
    and called it all of it, and ``replay(["bar"])`` raised ``Unknown channel tag``, so
    those rows were unreachable through the record API entirely — with ``Channel.BAR``
    declared the whole time. Only a read path prevents the loss, so that is what is
    asserted: every orphaned member names the tag that absorbed it, and that tag has a
    live record.

    ``book_ticker`` is not an orphan and is not exempted. Equity's ``BookTicker`` was an
    alias of ``Quote``, but the crypto struct of that name survives, so the member has a
    record and never reaches the check below.

    Any member that is neither backed by a record nor mapped to one is a porting
    mistake. The fix is never to delete it — that really would drop a partition on the
    floor — but to port the record or declare the successor.
    """
    tags = {c.__struct_config__.tag for c in _declared_record_types()}
    orphans = {c.value for c in Channel} - tags

    unmapped = sorted(o for o in orphans if o not in CHANNEL_SUCCESSORS)
    assert not unmapped, (
        f"Channel members with no record and no successor: {unmapped}. "
        f"A declared member is not a read path; map it in CHANNEL_SUCCESSORS or port "
        f"the record."
    )
    dangling = sorted(o for o in orphans if CHANNEL_SUCCESSORS[o] not in tags)
    assert not dangling, f"retired tags whose successor has no record either: {dangling}"


@pytest.mark.parametrize(
    ("retired", "row"),
    [
        pytest.param(
            "bar",
            {
                "interval": "1d",
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "volume": 9.0,
                "trade_count": 77,
            },
            id="bar",
        ),
        pytest.param(
            "option_quote",
            {"underlying": "AAPL", "expiry": "2026-06-18", "strike": 1.0, "type": "C"},
            id="option_quote",
        ),
    ],
)
def test_gate1_a_row_under_a_retired_tag_decodes_into_its_successor(
    retired: str, row: dict[str, object]
) -> None:
    """The read path the gate above requires, exercised rather than described.

    ``CHANNEL_SUCCESSORS`` naming a tag is only half of a read path; the other half is
    ``from_row`` accepting a row that carries it.
    """
    from crocodile.core.store.rows import from_row

    record = from_row(
        {
            "channel": retired,
            "provider": "alpaca",
            "symbol": "AAPL",
            "symbol_raw": "AAPL",
            "source_ts": None,
            "local_ts": 1_700_000_000_000_000_000,
            "date": "2023-11-14",
            "bucket": 42,
            **row,
        }
    )

    assert record.__struct_config__.tag == CHANNEL_SUCCESSORS[retired]


def test_option_expiry_is_nanoseconds() -> None:
    """Equity spelled expiry ``YYYY-MM-DD``; a date cannot express an intraday expiry.

    Nanoseconds are the unit every other timestamp in this codebase uses, so the merge of
    ``OptionQuote`` into ``OptionsChain`` converts rather than widens to ``int | str``.
    """
    fields = {f.name: f.type for f in msgspec.structs.fields(OptionsChain)}
    assert fields["expiry"] is int, "expiry must be UTC epoch nanoseconds, not a date string"


def test_ohlcv_keeps_vwap_and_keeps_it_optional() -> None:
    """``vwap`` is the one equity bar field with no crypto counterpart.

    Equity's ``Bar`` and crypto's ``OHLCV`` were nearly the same struct, which is exactly
    what makes this field easy to lose: ``trade_count`` was a rename to ``num_trades``,
    but ``vwap`` was not a rename at all. It must stay, and stay optional — most crypto
    venues do not publish one, and ``0.0`` would be a false price where ``None`` is honest.
    """
    field = next(f for f in msgspec.structs.fields(OHLCV) if f.name == "vwap")
    assert field.type == float | None, f"vwap must be float | None, got {field.type}"
    assert field.default is None, "vwap must default to None, not to a fabricated price"


def test_ohlcv_side_volumes_are_a_hole_and_not_a_measured_zero() -> None:
    """``0.0`` on these two fields was a measurement standing in for an unfilled field.

    The same argument ``vwap`` above wins on, lost once: these declared ``float = 0.0``,
    and ``0.0`` on a volume split is a *claim* — no buying happened in this bar. Across the
    lake it was almost never that. Two writers ever set them: ``binance/backfill.py`` off
    ``takerBuyBaseAssetVolume``, and ``corpactions/calculator.py``, which rescales what it
    is given. Six equity providers, two other crypto ones and all three record resamplers
    left the default, so ``sum(buy_volume) / sum(volume)`` over a mixed lake answered with
    the Binance share of it, and a consumer had nothing to filter on: an unset field and a
    genuinely one-sided bar are the same bytes.

    ``None`` is the same encoding ``num_trades`` beside them has always used, and unlike a
    sentinel it survives Parquet, ``from_row`` and a ``WHERE buy_volume IS NULL``.
    """
    fields = {f.name: f for f in msgspec.structs.fields(OHLCV)}
    for name in ("buy_volume", "sell_volume"):
        assert fields[name].type == float | None, (
            f"{name} must be float | None, got {fields[name].type}"
        )
        assert fields[name].default is None, (
            f"{name} must default to None; 0.0 is a measurement of no buying, and it was "
            f"standing in for every path that never filled the field"
        )


def test_gate1_the_union_discriminates_by_tag() -> None:
    """What widening ``Record`` actually buys: a blob decodes back to its own class."""
    f = Funding(
        source="binance",
        symbol="binance:BTCUSDT",
        symbol_raw="BTCUSDT",
        local_ts=1,
        asset_class=AssetClass.CRYPTO,
        source_ts=None,
        funding_rate=0.0001,
    )
    decoded = msgspec.json.decode(msgspec.json.encode(f), type=Record)
    assert type(decoded) is Funding
    assert decoded.prov_basis == "native"


# ---------------------------------------------------------------------------
# Gate 2 — every capability is implemented for both asset classes.
# ---------------------------------------------------------------------------


def test_gate2_registry_is_not_empty():
    from crocodile.core.capability import REGISTRY

    assert REGISTRY, (
        "an empty registry makes the symmetry gate vacuously true; "
        "Phase 1 seeds it with `indicators`"
    )


_REGISTRY_AS_SHIPPED: frozenset[str] = frozenset(
    {
        "backfill",
        "base-market-data",
        "basis",
        "catalog",
        "catalog-channels",
        "catalog-dates",
        "catalog-exchanges",
        "catalog-inventory",
        "catalog-scan",
        "catalog-stats",
        "catalog-summary",
        "catalog-symbols",
        "census",
        "chaos-score",
        "collect",
        "collect-market",
        "data-coverage",
        "depth",
        "export",
        "funding-apr",
        "funding-predict",
        "gas-vol",
        "indicators",
        "iv-surface",
        "label-transfers",
        "lending-stress",
        "liquidity-depth",
        "list-exchanges",
        "markets",
        "mev-sandwich",
        "ofi",
        "onchain-price",
        "open-interest",
        "peg-deviation",
        "perp-basis",
        "query",
        "replay",
        "resample",
        "resolve-symbols",
        "risk-reversal",
        "search",
        "sequencer-latency",
        "slippage",
        "smart-money",
        "spot-future-basis",
        "term-structure",
        "universe",
        "vol-skew",
        "whale-alerts",
    }
)
"""The 49 capabilities Phase 2 shipped, by name, so that losing one is a red test.

:func:`test_gate2_registry_is_not_empty` guards the vacuous case — a gate over an empty
registry proves nothing — and this is the same argument one capability at a time. Every
other gate in the tree takes ``REGISTRY`` as its subject: Gate 2 iterates it, Gate 3 reads
the bases declared in it, Gate 4 compares the three surfaces *against* it. A name that
leaves therefore leaves every gate's subject at once, and all of them stay green over the
smaller world — which is exactly how an exit review deleted ``catalog-dates``, excused its
two wire names as infrastructure, and passed all 689 conformance tests.

A census cannot be evaded that way, because it is the one assertion whose subject is a
number this file states rather than a list the code supplies.
"""


def test_gate2_no_capability_leaves_the_registry_unremarked() -> None:
    """Phase 1 lost seven capabilities and nothing raised; the queries came back empty.

    The whole merge rests on the claim that nothing disappeared silently, and every
    mechanism defending it reads ``REGISTRY``. So the registry itself is pinned: removing a
    declaration fails here first and by name, and the failure is not one an exemption ledger
    can answer — ``IRREDUCIBLE`` and ``PENDING_SYMMETRY`` both describe capabilities that
    *exist* and are asymmetric, which is a different claim from one that is gone.
    """
    from crocodile.capabilities import load_all
    from crocodile.core.capability import REGISTRY

    load_all()
    live = set(REGISTRY)

    gone = sorted(_REGISTRY_AS_SHIPPED - live)
    assert not gone, (
        f"{gone} were declared capabilities and are not in REGISTRY any more. A capability "
        "that leaves takes its command, its route and its tool with it, and every gate that "
        "would notice reads REGISTRY. Restore the declaration, or — if it is genuinely being "
        "retired — retire it here in the same commit, where the diff shows what the product "
        "stopped answering."
    )

    added = sorted(live - _REGISTRY_AS_SHIPPED)
    assert not added, (
        f"{added} are declared and unrecorded here. New capabilities are welcome; add them "
        "to _REGISTRY_AS_SHIPPED so the census keeps counting, and so the next deletion is "
        "measured against a list that includes them."
    )


def test_gate2_every_capability_is_symmetric():
    """Two ways out, and both cost something.

    ``IRREDUCIBLE`` is a claim about the market and is permanent. ``PENDING_SYMMETRY`` is a
    claim about the schedule and has to be repaid by Phase 3's exit. Anything else fails
    here, which is what stops Phase 2's port from being able to quietly drop a half.
    """
    from crocodile.core.capability import (
        IRREDUCIBLE,
        PENDING_SYMMETRY,
        REGISTRY,
        AssetClass,
    )

    for cap in REGISTRY.values():
        if cap.name in IRREDUCIBLE or cap.name in PENDING_SYMMETRY:
            continue
        assert set(cap.impls) == {AssetClass.CRYPTO, AssetClass.EQUITY}, (
            f"{cap.name} implements {sorted(cap.impls)}; add the missing asset class, "
            f"schedule it in PENDING_SYMMETRY, or justify it in IRREDUCIBLE"
        )


def test_gate2_irreducible_entries_carry_a_justification():
    from crocodile.core.capability import IRREDUCIBLE

    for name, why in IRREDUCIBLE.items():
        assert why.strip(), f"{name} is on IRREDUCIBLE with no justification"


def test_gate2_params_schema_is_json_serialisable():
    """Phase 2 uses this as the MCP inputSchema; a struct that cannot be
    described has to be found now, not then."""
    import msgspec

    from crocodile.core.capability import REGISTRY

    for cap in REGISTRY.values():
        schema = msgspec.json.schema(cap.params)
        assert schema.get("$ref") or schema.get("type"), cap.name


# ---------------------------------------------------------------------------
# Gate 3 — every emitted prov_basis has a registered confidence formula.
# ---------------------------------------------------------------------------


def _crocodile_sources() -> list[pathlib.Path]:
    """Every ``crocodile`` source file, located from this file rather than from the cwd.

    The two gates below scan source text. A relative ``Path("src/crocodile")`` resolves
    against the working directory, so running pytest from anywhere but the repo root would
    make ``rglob`` yield nothing and turn both gates permanently green — the failure mode a
    conformance gate can least afford. Anchoring on ``__file__`` and asserting the tree is
    non-empty means a gate that finds nothing says so instead of passing.
    """
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "crocodile"
    files = sorted(root.rglob("*.py"))
    assert files, f"no crocodile sources under {root}; these gates would pass vacuously"
    return files


def test_gate3_every_declared_basis_is_registered():
    from crocodile.core.capability import REGISTRY
    from crocodile.core.schema.provenance import load_all_bases, registered_bases

    load_all_bases()
    known = registered_bases()
    for cap in REGISTRY.values():
        for asset_class, impl in cap.impls.items():
            assert impl.basis in known, (
                f"{cap.name}/{asset_class} declares basis {impl.basis!r}, "
                f"which has no registered confidence formula"
            )


class _BasisSite(NamedTuple):
    """A line of source that names a ``prov_basis``, and the name it states."""

    lineno: int
    basis: str


def _module_level_strings(tree: ast.Module) -> dict[str, str]:
    """``NAME -> "literal"`` for the module's top-level string constants.

    Four of the tree's basis names are stated as constants (``AMM_TICK_CURVE``,
    ``CAST_SEARCH``, ``REFERENCE_MERGE_BASIS``, ``SCRAPED_LAST_PRICE``) rather than as
    literals at the call site. Resolving them is what keeps the scanner's subject the set
    of bases the tree *names*, rather than the subset that happens to be spelled inline.
    """
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
        else:
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in targets:
                found[target.id] = node.value.value
    return found


def _bases_named_in(source: str) -> tuple[list[_BasisSite], list[int]]:
    """Every basis a module names, and the lines where it names one the scanner cannot read.

    Two shapes name a basis, and the gate below has to see both:

    ``prov_basis="…"`` — a literal keyword on a record constructor. This is the
    hand-assembled tail :func:`test_gate3_no_source_file_hand_writes_a_confidence` argues
    against; it is kept as a subject because a future one should still be caught. It does
    not occur today: all 22 live ``prov_basis=`` sites are ``prov_basis=tail.prov_basis``,
    forwarding a tail that :func:`provenance_fields` already built. A non-literal there is
    therefore the *correct* shape and is deliberately not an offence.

    ``provenance_fields("…")`` — the first argument of the only supported constructor of a
    provenance tail, and where every basis this tree stamps onto a record is actually
    named. Scanning only the first shape is why six registered-but-undeclared bases
    (``book_resample``, ``ohlcv_from_ohlcv``, ``ohlcv_from_quotes``, ``ohlcv_from_trades``,
    ``sec_13f_hr``, ``yahoo_1m_vap``) reached runtime records with no gate over them: they
    are named here and nowhere in ``REGISTRY``, so the sibling gate that reads
    ``impl.basis`` cannot see them either.

    Returns ``(sites, opaque_linenos)``. A first argument that is neither a literal nor a
    module-level string constant is *opaque*: the gate reports it rather than skipping it.
    Skipping would reopen the hole this function exists to close — one
    ``provenance_fields(_pick_basis())`` and a basis leaves the gate's subject with nothing
    said — and a warning that does not fail is the same hole with a nicer log line. The
    runtime does raise ``UnregisteredBasisError`` on an unknown basis, but only on the line
    that executes, which is exactly the guarantee a static gate is here to strengthen.
    There are no opaque sites today, so the rule costs nothing to adopt and the escape it
    forbids is one nobody has taken.
    """
    tree = ast.parse(source)
    constants = _module_level_strings(tree)
    sites: list[_BasisSite] = []
    opaque: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "prov_basis" and isinstance(kw.value, ast.Constant):
                if isinstance(kw.value.value, str):
                    sites.append(_BasisSite(kw.value.lineno, kw.value.value))
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if called != "provenance_fields" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            sites.append(_BasisSite(first.lineno, first.value))
        elif isinstance(first, ast.Name) and first.id in constants:
            sites.append(_BasisSite(first.lineno, constants[first.id]))
        else:
            opaque.append(first.lineno)
    return sites, opaque


def test_gate3_no_source_file_emits_an_unregistered_basis():
    from crocodile.core.schema.provenance import load_all_bases, registered_bases

    # Without this the registry reflects only what happens to have been imported,
    # and a basis registered in an unimported module becomes a false offender.
    load_all_bases()
    known = registered_bases()
    offenders = []
    opaque_sites = []
    seen = 0
    for path in _crocodile_sources():
        sites, opaque = _bases_named_in(path.read_text())
        seen += len(sites)
        offenders += [
            f"{path}:{site.lineno} {site.basis!r}" for site in sites if site.basis not in known
        ]
        opaque_sites += [f"{path}:{lineno}" for lineno in opaque]
    assert not offenders, f"unregistered bases named in source: {offenders}"
    assert not opaque_sites, (
        f"provenance_fields() called with a basis this gate cannot read: {opaque_sites}. "
        f"Name it with a string literal or a module-level string constant; a basis chosen "
        f"at runtime is one no static gate can check."
    )
    # The scanner found nothing at all only if it stopped matching the tree's shape — the
    # previous version of this gate evaluated its condition 3773 times and took it never.
    assert seen, "no basis is named anywhere in the tree; this gate has stopped looking"


_SCANNER_FIXTURE = """
BASIS_CONSTANT = "constant_basis"
LATER: Final = "annotated_basis"


def build(tail):
    a = provenance_fields("literal_basis")
    b = provenance_fields(BASIS_CONSTANT, {"n": 1})
    c = provenance_fields(LATER)
    d = provenance_fields(pick())
    return OHLCV(prov_basis=tail.prov_basis), OHLCV(prov_basis="hand_written")
"""


def test_the_basis_scanner_reads_every_shape_the_tree_uses():
    """The gate above passes over real source; this is what proves it can fail.

    A scanner is only worth what its subject is, and the subject is what this asserts:
    literal first arguments, constants (plain and annotated), the forwarded
    ``prov_basis=tail.prov_basis`` that must *not* register, the hand-written literal that
    must, and the runtime-chosen argument that has to surface as opaque rather than vanish.
    """
    sites, opaque = _bases_named_in(_SCANNER_FIXTURE)
    assert sorted(site.basis for site in sites) == [
        "annotated_basis",
        "constant_basis",
        "hand_written",
        "literal_basis",
    ]
    assert len(opaque) == 1


def test_an_unregistered_basis_named_only_at_a_provenance_fields_call_is_caught():
    """The exact defect the old scanner could not see, driven end to end.

    ``ohlcv_from_trades`` and its five siblings are named nowhere but here. A file that
    named an *unregistered* one passed the previous gate in silence because the gate only
    read ``prov_basis=`` keywords.
    """
    from crocodile.core.schema.provenance import load_all_bases, registered_bases

    load_all_bases()
    known = registered_bases()
    sites, _ = _bases_named_in('tail = provenance_fields("ohlcv_from_moonbeams")\n')
    unregistered = [site.basis for site in sites if site.basis not in known]
    assert unregistered == ["ohlcv_from_moonbeams"]


def test_gate3_no_source_file_hand_writes_a_confidence():
    """The bypass the basis gate alone does not catch.

    ``prov_basis="yahoo_1m_vap", prov_confidence=0.93`` satisfies every other gate
    while never touching the registry — the easy path routing around the
    mechanism. Confidence must come from ``provenance_fields()``, so a literal
    number at a call site is the signature of a hand-assembled tail.
    """
    offenders = []
    for path in _crocodile_sources():
        if path.name == "provenance.py":
            continue  # the registry itself legitimately returns numbers
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "prov_confidence" or not isinstance(kw.value, ast.Constant):
                    continue
                if kw.value.value is not None:
                    offenders.append(f"{path}:{kw.value.lineno} = {kw.value.value!r}")
    assert not offenders, (
        f"hand-written prov_confidence literals: {offenders}. "
        f"Build the tail with provenance_fields(basis, inputs) instead."
    )


# ---------------------------------------------------------------------------
# Gate 3b — a module that derives records must say so on every record it builds.
# ---------------------------------------------------------------------------
# Both scanners above look for an explicit ``prov_basis=`` / ``prov_confidence=``
# keyword. Omission is invisible to them, and omission is not a neutral state:
# ``_Header`` defaults to ``prov=NATIVE, prov_basis="native",
# prov_confidence=1.0``, and ``native`` means *the venue reported this value
# directly*. A derived record that simply says nothing therefore ships a false
# claim rather than a missing one — and a silent one, because ``describe()`` is
# what the REST and MCP surfaces are required to emit as a warning for every
# record whose ``prov`` is not NATIVE. ``core.resample.book`` did exactly this:
# it built a reconstructed ``BookSnapshot`` with no ``prov*`` argument at all and
# passed every gate in this file.

DERIVES_RECORDS: dict[str, str] = {
    "crypto/depth/book_slice.py": (
        "Builds a DepthProfile out of a stored BookSnapshot. The venue published a book, "
        "at its own instant and to its own depth; what this emits is that book re-cut to a "
        "requested ladder and re-stamped at a requested instant, so a silent record here "
        "would claim a venue published a ladder it never published. Declared even though "
        "the discovery rule below only walks `resample` packages, because this is the shape "
        "that rule was written for and the fabrication scanner further down cannot see it: "
        "nothing is fabricated, every number is the venue's, and the defect would be "
        "entirely in what the record says about itself."
    ),
    "core/resample/book.py": (
        "Reconstructs a BookSnapshot from an OrderBook replayed over other records; "
        "nothing it emits was reported by a venue. This is now the only book resampler: "
        "`equity/resample/book.py` held a second one under the boundary rule that emits "
        "before it applies, and that rule won — the entry for it left this list with the "
        "module."
    ),
    "equity/resample/ohlcv.py": (
        "Aggregates bars out of trades, quotes or narrower bars. None of the three was "
        "published by a venue, and the quote one is not even a traded price: its `volume` "
        "is a structural zero. Left silent they would all have inherited the header "
        "default and claimed NATIVE."
    ),
    "equity/reference/universe.py": (
        "Builds an Instrument out of three other Instruments: SEC EDGAR's registrant row, "
        "Tiingo's listing row and OpenFIGI's identifier row, resolved by CoverageResolver "
        "under fill_nulls. The merged row is the one that matters here — no registry "
        "published it, because the CIK is SEC's, the venue is Tiingo's and the FIGI is "
        "OpenFIGI's — so left silent it would have inherited the highest-priority source's "
        "own `native` tail and claimed a registrar reported it whole. It carries "
        "`reference_merge` instead, whose confidence counts how many of the three agreed. "
        "The three per-source builders are on this list too and legitimately claim NATIVE: "
        "a pre-merge row is one registry's own statement, read rather than reconstructed. "
        "Being here is what makes that a claim somebody wrote rather than a default nobody "
        "noticed, which is the entire distinction this list exists to force. Note the "
        "discovery rule below only walks `resample` packages, so this entry is voluntary — "
        "and it is exactly the kind of module that rule was blind to when `google_finance` "
        "put a synthesiser in `providers/`."
    ),
}
"""Modules that build records out of other records, and why each one counts as derived.

The justification is mandatory for the same reason it is on
:data:`crocodile.core.capability.IRREDUCIBLE`: an entry here changes what a gate
demands, so an entry with nothing to say is an entry nobody argued for. A path that
no longer exists, or that no longer builds a canonical record, fails its own gate
rather than sitting here as decoration.

The limit is worth stating: this list is a declaration, and a synthesiser written
somewhere no rule below looks would go undeclared. The discovery rule here covers the
``resample`` packages, and it used to be the *only* discovery rule — which is how
``google_finance`` came to put a synthesiser in ``providers/`` and sit outside every
gate by construction. It is no longer alone: the fabrication scanner further down
covers the whole tree, and
:func:`test_gate3b_a_record_carrying_a_fabricated_measurement_states_prov` makes what
it finds carry the same ``prov=`` obligation this list imposes. What that second rule
does *not* cover is a module that derives records without writing a constant into a
required measurement field — a resampler is exactly that, which is why this list did
not become redundant.
"""


def _canonical_record_names() -> frozenset[str]:
    """The class names ``crocodile.core.schema.records`` declares."""
    return frozenset(cls.__name__ for cls in _declared_record_types())


def _canonical_record_calls(tree: ast.AST, names: frozenset[str]) -> list[ast.Call]:
    """Every call in ``tree`` that constructs a canonical record.

    Import aliases are resolved rather than ignored: ``equity.resample.book`` imported
    ``BookSnapshot as CoreBookSnapshot``, and a scanner matching bare class names would
    have read that module as building no records at all — a gate that passes because it
    could not see its subject. That module has since been merged into
    ``core.resample.book`` and the alias with it, so the resolution now guards the next
    such import rather than a live one; it stays because the failure mode does.
    """
    local: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "crocodile.core.schema.records":
            for alias in node.names:
                if alias.name in names:
                    local[alias.asname or alias.name] = alias.name
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in local:
            calls.append(node)
        elif isinstance(func, ast.Attribute) and func.attr in names:
            # ``records.Trade(...)`` — the module-qualified form.
            calls.append(node)
    return calls


def _source_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2] / "src" / "crocodile"


def test_gate3b_every_declared_deriving_module_exists_and_builds_records():
    """Self-cleaning, so the list cannot become a place obligations go to be forgotten."""
    names = _canonical_record_names()
    root = _source_root()
    for relative, why in DERIVES_RECORDS.items():
        path = root / relative
        assert path.is_file(), f"DERIVES_RECORDS names {relative}, which does not exist"
        assert why.strip(), f"{relative} is on DERIVES_RECORDS with no justification"
        assert _canonical_record_calls(ast.parse(path.read_text()), names), (
            f"{relative} is on DERIVES_RECORDS but builds no canonical record; "
            f"drop the entry or the gate below guards nothing"
        )


def test_gate3b_a_deriving_module_states_prov_on_every_record_it_builds():
    """Omission is the bypass Gate 3's two scanners cannot see.

    They look for a keyword that is present and wrong. This looks for one that is
    absent, in the modules where absent means a reconstruction inheriting the venue's
    word for itself.
    """
    names = _canonical_record_names()
    root = _source_root()
    offenders = []
    for relative in DERIVES_RECORDS:
        path = root / relative
        for call in _canonical_record_calls(ast.parse(path.read_text()), names):
            if not any(kw.arg == "prov" for kw in call.keywords):
                func = call.func
                built = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "?")
                offenders.append(f"{relative}:{call.lineno} {built}(...)")
    assert not offenders, (
        f"records built without an explicit prov= in a deriving module: {offenders}. "
        f"No prov= means prov=NATIVE, prov_basis='native', prov_confidence=1.0 — "
        f"the claim that a venue reported this value directly."
    )


def test_gate3b_every_resampler_that_builds_a_record_is_declared():
    """A new resampler cannot be added without answering the question.

    Without this the gate only ever asks what it was already told to ask, which is the
    vacuous shape a conformance test can least afford: the list stays green by staying
    short.
    """
    names = _canonical_record_names()
    root = _source_root()
    undeclared = []
    for path in sorted(root.rglob("*.py")):
        if "resample" not in path.relative_to(root).parts:
            continue
        if not _canonical_record_calls(ast.parse(path.read_text()), names):
            continue
        relative = path.relative_to(root).as_posix()
        if relative not in DERIVES_RECORDS:
            undeclared.append(relative)
    assert not undeclared, (
        f"resampler modules building canonical records but not on DERIVES_RECORDS: "
        f"{undeclared}. Add each with the argument for why it derives, so the "
        f"prov= gate covers it."
    )


# ---------------------------------------------------------------------------
# Gate 3b, widened — a fabricated measurement anywhere in the tree, not just in resample/
# ---------------------------------------------------------------------------
# The discovery rule above looks in ``resample`` packages, which was where every
# derivation lived when it was written. ``google_finance`` put a synthesiser in
# ``providers/`` — ``Trade(amount=1.0)`` and a ``Quote`` with ``bid_px == ask_px``
# and ``bid_sz = ask_sz = 1.0`` — and it was outside every gate by construction.
# So the scan below covers the whole tree, and looks for the thing that made those
# records fabrications rather than derivations: a *literal number* standing where a
# measurement belongs.
#
# "Where a measurement belongs" is derived from the structs rather than listed:
# a required field annotated exactly ``float``. Those are the quantities a record
# cannot exist without and cannot have a default for — ``Trade.amount``,
# ``Quote.bid_sz``, ``OHLCV.volume``, ``DepthProfile.reference_price``. An adapter
# reading one off a payload writes an expression; only a fabricated one writes ``1.0``.

FABRICATES_MEASUREMENTS: dict[str, tuple[tuple[tuple[str, float], ...], str]] = {
    "equity/providers/google_finance/connector.py": (
        (("amount", 0.0),),
        "Trade.amount = _UNPUBLISHED_SIZE, a structural 0.0. The quote page publishes a "
        "last price and no per-print size, for any symbol at any time, and Trade.amount "
        "is required — a trade cannot exist without a quantity. 0.0 sums to nothing, "
        "which is the truth about how much volume a scrape contributes; the 1.0 it "
        "replaced made SELECT sum(amount) return the poll count as share volume, growing "
        "with the scrape interval rather than with trading. prov_basis names the method "
        "and scraped_last_price scores it 0.0, so nothing here reads as measured.",
    ),
    "equity/resample/ohlcv.py": (
        (("volume", 0.0),),
        "OHLCV.volume = 0.0 on every bar resample_quotes_to_bars emits. A quote carries "
        "no size that belongs in a bar — nothing in one of these bars was transacted — so "
        "the zero is structural rather than a measurement of nothing traded, and the "
        "ohlcv_from_quotes registration argues exactly that as the reason the basis is "
        "SYNTHETIC rather than DERIVED. The sample size that *is* observable is on the "
        "record as num_trades, the quote count.",
    ),
}
"""Modules that write a constant number into a required measurement field: which, and why.

The **field and the value** are declared beside the argument and asserted against the
tree, on the same discipline :data:`CONSTANT_BY_DEFINITION` carries — an exemption is a
licence for one number in one field, not for a file. ``_UNPUBLISHED_SIZE = 0.0`` and
``_ASSUMED_SIZE = 1.0`` are the same syntax and opposite claims: the first says a scrape
contributes no volume, the second reported the poll count as share volume. A file-keyed
licence carries over from one to the other in silence.

Keying on the value alone had the same hole one axis over, and the docstring above said
so while the code did not: this module is licensed for ``0.0`` because a quote bar's
``volume`` is a structural zero, and that licence silently covered ``Trade(amount=0.0)``
and ``DepthProfile(reference_price=0.0, …)`` added anywhere in the same file — which is
exactly where new resamplers get added. A zero volume on a quote bar and a zero
reference price on a depth profile are different claims about different quantities, and
only one of them has an argument.


Every entry is a record asserting a quantity nobody measured. Adding one is allowed
— a structural zero can be the honest encoding, as ``ohlcv_from_quotes`` argues for
a quote bar's ``volume`` — but it is allowed *out loud*, with the argument next to
it, on the same discipline as :data:`crocodile.core.capability.IRREDUCIBLE`. A
silent one is what shipped a zero-width NBBO at ``prov_confidence=1.0``.

Both entries below were invisible to the scanner that shipped with this list, and
that is why it was empty rather than because the tree was clean. It compared
``kw.value`` against ``ast.Constant``, so ``_UNPUBLISHED_SIZE = 0.0`` beside
``amount=_UNPUBLISHED_SIZE`` — which is what the fix for the ``google_finance``
fabrication actually wrote — produced no hit at all. Restoring ``_ASSUMED_SIZE =
1.0`` would have kept the gate green. A gate that only sees the literal spelling
is a gate that teaches people to rename.

Key is the path relative to ``src/crocodile``; value is the argument.
"""


def _measurement_fields() -> dict[str, frozenset[str]]:
    """Return ``{record class: required fields annotated exactly float}``."""
    return {
        cls.__name__: frozenset(
            f.name for f in msgspec.structs.fields(cls) if f.required and f.type is float
        )
        for cls in _declared_record_types()
    }


def _canonical_record_calls_by_class(
    tree: ast.AST, names: frozenset[str]
) -> list[tuple[str, ast.Call]]:
    """Every canonical-record construction in ``tree``, paired with the class it builds.

    Import aliases are resolved for the reason :func:`_canonical_record_calls` gives:
    a scanner matching bare class names reads an aliasing module as building nothing.
    """
    local: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "crocodile.core.schema.records":
            for alias in node.names:
                if alias.name in names:
                    local[alias.asname or alias.name] = alias.name
    calls: list[tuple[str, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in local:
            calls.append((local[func.id], node))
        elif isinstance(func, ast.Attribute) and func.attr in names:
            calls.append((func.attr, node))
    return calls


# ---------------------------------------------------------------------------
# Seeing through the spellings a fabrication can wear
# ---------------------------------------------------------------------------
# The first version of this scanner compared ``kw.value`` against ``ast.Constant``
# and stopped. That catches ``Trade(amount=1.0)`` and nothing else — not the name
# bound to 1.0 one line above it, not ``1.0 * 1``, not ``float(1)``, not
# ``**{"amount": 1.0}``, not ``msgspec.structs.replace(t, amount=1.0)``. The fix
# for the ``google_finance`` fabrication wrote ``_UNPUBLISHED_SIZE = 0.0`` and
# ``amount=_UNPUBLISHED_SIZE``, so it left the gate green and the list empty by
# hiding from the gate rather than by having nothing to declare.
#
# So the scanner folds constants instead of recognising them. A name counts as a
# constant only when *every* assignment to it in its own scope is one, which is
# what separates ``resample_quotes_to_bars``'s ``volume``, assigned 0.0 and
# nothing else, from ``resample_trades_to_bars``'s, assigned ``trade.amount``.

_NUMERIC_COERCIONS = frozenset({"float", "int"})
"""Calls that turn a constant into a constant of another type, and nothing else."""

_CONSTANT_BUILTINS: dict[str, Callable[..., float]] = {
    "min": min,
    "max": max,
    "abs": abs,
}
"""Builtins that are a constant when every argument is. ``min(1.0, 2.0)`` is ``1.0``."""

FABRICATION_BLIND_SPOTS: dict[str, tuple[str, str]] = {
    "a name assigned a constant on one branch": (
        "def f(payload, x):\n"
        "    val = 0.0\n"
        "    if x:\n"
        "        val = float(payload['size'])\n"
        "    return Trade(amount=val)",
        "A constant that reaches the field through a *name* rather than through the "
        "expression at the call site. The guard rule below reads the call-site "
        "expression only, and deliberately: chasing names was measured over this tree and "
        "flagged 27 sites in `equity/resample/ohlcv.py` alone, where `open_px = 0.0` is an "
        "accumulator initialiser and not a substitute for anything. Every one of the 13 it "
        "found outside the resamplers was real, so the noise is not evenly spread — but a "
        "gate that is two-thirds false positives on its largest subject is a gate that gets "
        "turned off, which is worse than one that says what it misses.",
    ),
    "a clamp over a real reading": (
        "def f(msg):\n    return Trade(amount=max(msg.size, 0.0001))",
        "`min`/`max`/`abs` over a measurement are folded only when *every* argument is "
        "constant, so a floor applied to a real reading passes. It fabricates when the "
        "reading is zero, and flagging it would flag every legitimate clamp in the tree.",
    ),
    "a class attribute": (
        "class C:\n    SIZE = 1.0\n\n\nTrade(amount=C.SIZE)",
        "Class bodies are separate scopes and are not descended into, so the attribute "
        "never becomes a binding.",
    ),
    "a default argument": (
        "def f(size=1.0):\n    return Trade(amount=size)",
        "Parameters are deliberately recorded as non-constant, because a caller may pass a "
        "real measurement; folding the default would flag every honest reader with a "
        "fallback.",
    ),
    "a helper's return value": (
        "def _size():\n    return 1.0\n\n\nTrade(amount=_size())",
        "Only `float`/`int` and the builtins in `_CONSTANT_BUILTINS` are folded through; "
        "user calls are opaque.",
    ),
    "a constant imported from another module": (
        "from crocodile.core.schema.sizes import ASSUMED\n\nTrade(amount=ASSUMED)",
        "The scanner parses one file at a time and has no cross-module binding table.",
    ),
    "a constant reached through a container": (
        "def f(cfg):\n    return Trade(amount=cfg.assumed_size)",
        "An attribute of a dataclass, a `NamedTuple` field, a dict built by a function: the "
        "scanner cannot see into any of them to learn the value is a constant.",
    ),
    "an invented scale over a real reading": (
        "def f(payload):\n    return Trade(amount=float(payload['size']) / 1e9)",
        "A divisor is not the measurement, so the expression is not constant-foldable and "
        "the scanner reads the whole thing as a reading — correctly, for every honest "
        "adapter, since a fixed-point payload has to be scaled by something and the "
        "something is always a literal. `gmx_synthetix/connector.py` shipped "
        "`entryFundingRate / 1e9` under the comment `# illustrative scale`; GMX v1's "
        "`FUNDING_RATE_PRECISION` is 1e6, so the divisor was out by a thousand and nothing "
        "in this file could see it. Closing this would need a per-venue table of the real "
        "constants, which is a different kind of gate: this one reads source, and the "
        "answer is on a chain. What the review reached for instead was the comment, and a "
        "comment is not a declaration — hence "
        "`test_gate3b_a_scale_is_not_licensed_by_calling_it_illustrative`, which bans the "
        "licence without pretending to know the number.",
    ),
}
"""What this scanner does **not** see, each with a probe proving it is still not seen.

Every entry is a way to write a fabricated measurement that reads green. They are here
rather than closed because each would cost either a cross-module analysis or a false
positive on an honest reading — a default argument is the clearest case, since
`def f(size=1.0)` is what a real adapter writes when the payload may omit the field.

Closing one is welcome; deleting the entry without closing it is not — and until the
review that added the probes, nothing enforced that half. This was seven prose strings no
test read, sitting between `_EVASIONS` and `_HONEST_READINGS`, which are both parametrised.
An unread list of limitations decays exactly the way an unread list of exemptions does: the
earlier review found the plain `from msgspec.structs import replace` spelling evading a
guard whose own evasion set covered only the aliased form, which is the same failure as an
undocumented blind spot with extra steps. The gate looked complete.

So each entry now carries a snippet the scanner must **not** flag, and
:func:`test_gate3b_a_documented_blind_spot_is_still_open` runs it. Close a blind spot and
that test goes red, which is the prompt to delete the entry; delete an entry without
closing it and :func:`test_gate3b_the_blind_spot_list_is_the_set_that_was_measured` goes
red instead. The prose can no longer drift away from the scanner in either direction.
"""


def _scope_bindings(scope: ast.AST) -> dict[str, list[ast.expr | None]]:
    """Return ``{name: values assigned to it}`` for one scope's own body.

    Nested functions and classes are not descended into: they are separate scopes,
    and merging them is what would poison ``volume`` in ``resample_quotes_to_bars``
    with the ``volume = trade.amount`` two functions above it.

    A binding whose value cannot be an expression — a loop variable, a ``with``
    target, an ``except`` name, a parameter, an import, an augmented assignment —
    is recorded as ``None``, which is what makes it *fail* the constant test rather
    than be absent from it. Absent would mean "look in the enclosing scope".
    """
    bound: dict[str, list[ast.expr | None]] = {}

    def note(target: ast.AST, value: ast.expr | None) -> None:
        if isinstance(target, ast.Name):
            bound.setdefault(target.id, []).append(value)
            return
        for sub in ast.walk(target):
            if isinstance(sub, ast.Name):
                bound.setdefault(sub.id, []).append(None)

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                bound.setdefault(child.name, []).append(None)
                continue
            if isinstance(child, ast.Lambda):
                continue
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    note(target, child.value)
            elif isinstance(child, ast.AnnAssign):
                note(child.target, child.value)
            elif isinstance(child, ast.NamedExpr):
                note(child.target, child.value)
            elif isinstance(child, ast.AugAssign | ast.For | ast.AsyncFor):
                note(child.target, None)
            elif isinstance(child, ast.comprehension):
                note(child.target, None)
            elif isinstance(child, ast.withitem) and child.optional_vars is not None:
                note(child.optional_vars, None)
            elif isinstance(child, ast.ExceptHandler) and child.name:
                bound.setdefault(child.name, []).append(None)
            elif isinstance(child, ast.Import | ast.ImportFrom):
                for alias in child.names:
                    bound.setdefault((alias.asname or alias.name).split(".")[0], []).append(None)
            visit(child)

    if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        spec = scope.args
        for arg in (*spec.posonlyargs, *spec.args, *spec.kwonlyargs, spec.vararg, spec.kwarg):
            if arg is not None:
                bound.setdefault(arg.arg, []).append(None)
    visit(scope)
    return bound


_BINARY_FOLDS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


def _fold_constant(
    node: ast.expr | None,
    bindings: dict[str, list[ast.expr | None]],
    seen: frozenset[str] = frozenset(),
) -> tuple[bool, float | None]:
    """Return ``(is a constant number, its value)`` for ``node``.

    Constant-ness is the property that separates a fabrication from a reading: an
    adapter that read the value off a payload writes an expression over something it
    was given, and only an invented one can be folded to a number by reading the
    source.

    The value is folded as well as detected so the offence line reports the number
    rather than the name it is hiding behind, and so an exemption cannot be
    inherited by a differently-valued constant that merely reuses the spelling. It
    is ``None`` when the expression is constant but its value cannot be computed
    here — a name bound to two different constants on two branches, say.
    """
    if node is None:
        return False, None
    if isinstance(node, ast.Constant):
        if isinstance(node.value, int | float) and not isinstance(node.value, bool):
            return True, float(node.value)
        return False, None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        ok, value = _fold_constant(node.operand, bindings, seen)
        if not ok:
            return False, None
        if value is None:
            return True, None
        return True, -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp):
        left_ok, left = _fold_constant(node.left, bindings, seen)
        right_ok, right = _fold_constant(node.right, bindings, seen)
        if not (left_ok and right_ok):
            return False, None
        fold = _BINARY_FOLDS.get(type(node.op))
        if fold is None or left is None or right is None:
            return True, None
        try:
            return True, float(fold(left, right))
        except (ArithmeticError, ValueError, OverflowError):
            return True, None
    if isinstance(node, ast.IfExp):
        # ``1.0 if x else 1.0`` is 1.0 whichever way the test goes, and a branch that
        # reads a payload on one side is not constant, so this cannot swallow a reader.
        for branch in (node.body, node.orelse):
            ok, _ = _fold_constant(branch, bindings, seen)
            if not ok:
                return False, None
        _, left = _fold_constant(node.body, bindings, seen)
        _, right = _fold_constant(node.orelse, bindings, seen)
        return True, left if left == right else None
    if isinstance(node, ast.NamedExpr):
        # ``Trade(amount=(size := 1.0))`` binds and passes in one expression.
        return _fold_constant(node.value, bindings, seen)
    if isinstance(node, ast.Subscript):
        # ``_KW["amount"]`` / ``_SIZES[0]`` over a container written out in the source.
        return _fold_subscript(node, bindings, seen)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.keywords:
            return False, None
        if node.func.id in _CONSTANT_BUILTINS:
            folded = [_fold_constant(arg, bindings, seen) for arg in node.args]
            if not folded or not all(ok for ok, _ in folded):
                return False, None
            values = [value for _, value in folded]
            if any(value is None for value in values):
                return True, None
            try:
                return True, float(_CONSTANT_BUILTINS[node.func.id](*values))
            except (ArithmeticError, TypeError, ValueError):
                return True, None
        if node.func.id not in _NUMERIC_COERCIONS:
            return False, None
        if not node.args:
            return True, 0.0
        ok, value = _fold_constant(node.args[0], bindings, seen)
        if not ok or value is None:
            return ok, None
        return True, float(int(value)) if node.func.id == "int" else value
    if isinstance(node, ast.Name):
        values = bindings.get(node.id)
        if not values or node.id in seen:
            return False, None
        nested = seen | {node.id}
        folded = [_fold_constant(value, bindings, nested) for value in values]
        if not all(ok for ok, _ in folded):
            return False, None
        distinct = {value for _, value in folded}
        return True, distinct.pop() if len(distinct) == 1 else None
    return False, None


def _resolve_container(
    node: ast.expr | None, bindings: dict[str, list[ast.expr | None]]
) -> ast.expr | None:
    """Return the literal container ``node`` names, or ``None``.

    A name counts only when every assignment to it in scope is the *same* kind of
    literal container, for the reason :func:`_fold_constant` folds names at all: a
    container rebound to something read off a payload is not a constant.
    """
    if isinstance(node, ast.Dict | ast.Tuple | ast.List):
        return node
    if not isinstance(node, ast.Name):
        return None
    values = bindings.get(node.id)
    if not values or len(values) != 1:
        return None
    inner = values[0]
    return inner if isinstance(inner, ast.Dict | ast.Tuple | ast.List) else None


def _fold_subscript(
    node: ast.Subscript, bindings: dict[str, list[ast.expr | None]], seen: frozenset[str]
) -> tuple[bool, float | None]:
    """Fold ``_KW["amount"]`` and ``_SIZES[0]`` when the container is written in the source.

    A module-level table of assumed values is the tidiest way to write a fabrication —
    it looks like configuration — and reading one element of it is the same claim as
    writing the number inline.
    """
    container = _resolve_container(node.value, bindings)
    if container is None or not isinstance(node.slice, ast.Constant):
        return False, None
    key = node.slice.value
    if isinstance(container, ast.Dict):
        for dict_key, dict_value in zip(container.keys, container.values, strict=True):
            if isinstance(dict_key, ast.Constant) and dict_key.value == key:
                return _fold_constant(dict_value, bindings, seen)
        return False, None
    if (
        isinstance(key, int)
        and not isinstance(key, bool)
        and -len(container.elts) <= key < len(container.elts)
    ):
        return _fold_constant(container.elts[key], bindings, seen)
    return False, None


def _substituted_constant(
    node: ast.expr, bindings: dict[str, list[ast.expr | None]], seen: frozenset[str] = frozenset()
) -> tuple[bool, float | None]:
    """Return ``(a guard here can substitute a constant, its value)`` for ``node``.

    :func:`_fold_constant` answers whether an expression *is* a constant. That misses the
    shape the ``msn_money`` bars were written in, where the constant sits on one branch of
    a guard and a payload read on the other::

        open=safe_float(open_p[idx]) if idx < len(open_p) else 0.0,
        bid_sz=_f(ticker.get("bidVolume")) or 0.0,

    A required measurement is not a field with a sensible default. When the guard takes
    the constant branch the payload supplied no measurement, so the number written there
    is invented on exactly the rows where it matters, and the record still claims
    ``prov=NATIVE`` — the ragged-array tail of an MSN chart went into the lake as a run of
    zero-priced bars nothing could tell from real ones.

    Only the expression written at the call site is read; a constant reached through a
    name is not, and :data:`FABRICATION_BLIND_SPOTS` carries the measurement that decided
    that. ``and``/``or`` are treated together with the conditional because ``x or 0.0`` is
    the same guard with the test elided.
    """
    branches: list[ast.expr]
    if isinstance(node, ast.IfExp):
        branches = [node.body, node.orelse]
    elif isinstance(node, ast.BoolOp):
        branches = list(node.values)
    else:
        return False, None
    for branch in branches:
        constant, value = _fold_constant(branch, bindings, seen)
        if constant:
            return True, value
        nested, value = _substituted_constant(branch, bindings, seen)
        if nested:
            return True, value
    return False, None


def _replace_calls(tree: ast.AST) -> list[ast.Call]:
    """Every ``msgspec.structs.replace(...)`` call, however it was imported.

    A record's fields are frozen, so ``replace`` is the *only* way to change one
    after construction — and it takes the same keywords the constructor does, which
    makes it the same fabrication surface. The class being replaced is not knowable
    from the call, so the caller checks these against the union of every
    measurement field rather than one record's.

    ``aliases`` is seeded empty and filled from the module's own imports, so a bare
    ``replace(...)`` counts exactly when ``from msgspec.structs import replace`` is
    what put the name there. It used to be seeded with ``{"replace"}`` and then
    guarded with ``func.id != "replace"`` — excluding the one name it was seeded
    with, so the plainest spelling in the family was the one that evaded. The
    suite's own evasion case used ``as swap``, which *is* caught, so a parametrised
    test reported green over a hole in the middle of what it claimed to cover.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "msgspec.structs":
            aliases.update(alias.asname or alias.name for alias in node.names)
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "replace":
            root = func.value
            names = {n.id for n in ast.walk(root) if isinstance(n, ast.Name)} | {
                a.attr for a in ast.walk(root) if isinstance(a, ast.Attribute)
            }
            if "structs" in names or "msgspec" in names:
                calls.append(node)
        elif isinstance(func, ast.Name) and func.id in aliases:
            calls.append(node)
    return calls


def _keyword_values(
    call: ast.Call, bindings: dict[str, list[ast.expr | None]]
) -> list[tuple[str, ast.expr]]:
    """Return ``(field, value)`` for every keyword the call sets, ``**{...}`` included.

    ``Trade(**{"amount": 1.0})`` is the same construction as ``Trade(amount=1.0)``
    with the keyword one layer of syntax away, and a scanner reading only
    ``kw.arg`` sees no field name at all. ``Trade(**_KW)`` is the same again with the
    dict given a name, which is how a fabrication comes to look like configuration;
    the name is resolved through the scope on the same rule
    :func:`_resolve_container` states.
    """
    pairs: list[tuple[str, ast.expr]] = []
    for kw in call.keywords:
        if kw.arg is not None:
            pairs.append((kw.arg, kw.value))
            continue
        unpacked = _resolve_container(kw.value, bindings)
        if not isinstance(unpacked, ast.Dict):
            continue
        for key, value in zip(unpacked.keys, unpacked.values, strict=True):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                pairs.append((key.value, value))
    return pairs


class _Fabrication(NamedTuple):
    """One constant number found standing where a measurement belongs."""

    module: str
    lineno: int
    built: str
    field: str
    source: str
    value: float | None
    states_prov: bool = True
    """Whether the construction carrying this offence passes ``prov=``.

    Recorded at scan time because the call is not reachable from the offence otherwise,
    and :func:`test_gate3b_a_record_carrying_a_fabricated_measurement_states_prov` is the
    rule that makes a licensed fabrication say so on the row as well as in the list.
    """

    def __str__(self) -> str:
        shown = self.source if self.value is None else f"{self.source} == {self.value!r}"
        return f"{self.module}:{self.lineno} {self.built}.{self.field} = {shown}"


def _fabrications_in(
    tree: ast.AST, fields: dict[str, frozenset[str]], relative: str
) -> list[_Fabrication]:
    """Return one offence per constant number standing in a measurement position."""
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    cache: dict[int, dict[str, list[ast.expr | None]]] = {}

    def bindings_for(node: ast.AST) -> dict[str, list[ast.expr | None]]:
        chain: list[ast.AST] = []
        current: ast.AST | None = node
        while current is not None:
            if isinstance(current, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef):
                chain.append(current)
            current = parents.get(current)
        merged: dict[str, list[ast.expr | None]] = {}
        for scope in reversed(chain):  # outermost first; the innermost binding wins
            if id(scope) not in cache:
                cache[id(scope)] = _scope_bindings(scope)
            merged.update(cache[id(scope)])
        return merged

    every_field = frozenset().union(*fields.values()) if fields else frozenset()
    subjects: list[tuple[str, frozenset[str], ast.Call]] = [
        (cls, fields[cls], call)
        for cls, call in _canonical_record_calls_by_class(tree, frozenset(fields))
    ]
    subjects += [("replace", every_field, call) for call in _replace_calls(tree)]

    offences: list[_Fabrication] = []
    for label, measurements, call in subjects:
        bindings = bindings_for(call)
        states_prov = any(kw.arg == "prov" for kw in call.keywords)
        for field, value in _keyword_values(call, bindings):
            if field not in measurements:
                continue
            constant, folded = _fold_constant(value, bindings)
            if not constant:
                constant, folded = _substituted_constant(value, bindings)
            if not constant:
                continue
            offences.append(
                _Fabrication(
                    relative, value.lineno, label, field, ast.unparse(value), folded, states_prov
                )
            )
    return offences


def _fabricated_measurements() -> dict[str, list[_Fabrication]]:
    """Return ``{module: offences}`` for every constant number in a measurement position."""
    fields = _measurement_fields()
    root = _source_root()
    found: dict[str, list[_Fabrication]] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        offences = _fabrications_in(ast.parse(path.read_text()), fields, relative)
        if offences:
            found[relative] = offences
    return found


def test_gate3b_no_module_fabricates_a_measurement_without_declaring_it():
    """The gate the ``resample``-only discovery rule could not have caught.

    ``Trade.amount = 1.0`` on every scraped last price made
    ``SELECT sum(amount) … WHERE source='google_finance'`` return the poll count as share
    volume, and it lived in ``providers/``, where no rule looked.
    """
    offenders = [
        str(offence)
        for module, offences in _fabricated_measurements().items()
        for offence in offences
        if (offence.field, offence.value) not in FABRICATES_MEASUREMENTS.get(module, ((), ""))[0]
    ]
    assert not offenders, (
        f"constant numbers in required measurement fields: {sorted(offenders)}. "
        f"Read the value from the source, or declare it in FABRICATES_MEASUREMENTS "
        f"as (field, value) with the argument for why that constant is the honest "
        f"encoding of that quantity."
    )


def test_gate3b_a_record_carrying_a_fabricated_measurement_states_prov():
    """The ``prov=`` obligation, discovered by evidence rather than by directory.

    ``DERIVES_RECORDS`` above finds its subjects by looking in ``resample`` packages,
    and said so in its own docstring — so ``google_finance``, which built a ``Trade``
    with an invented size in ``providers/``, was outside it by construction and outside
    every other gate with it. The scanner in this section already reads the whole tree.
    This joins the two: whatever it finds must also *say* it is not a venue reading,
    because ``prov`` is what the REST and MCP surfaces turn into a warning, and a
    fabrication left silent is served as though a venue had published it.

    The obligation is per **record**, not per module, and that is a deliberate
    difference from the resample rule. A resampler derives everything it emits, so the
    module is the right unit there. A provider mixes: ``google_finance`` scrapes one
    page and writes a real index level beside a ``Trade`` whose size the page never
    published. Demanding ``prov=Provenance.NATIVE, prov_basis="native"`` boilerplate on
    the honest records would be noise, and noise is how a gate gets turned off.

    Being on ``FABRICATES_MEASUREMENTS`` does not exempt a record from this. The list
    licenses a number in a field; it does not license the row claiming a venue reported
    it. A structural zero is honest *and* declared, or it is neither.
    """
    silent = [
        str(offence)
        for offences in _fabricated_measurements().values()
        for offence in offences
        if not offence.states_prov
    ]
    assert not silent, (
        f"records built with a fabricated measurement and no explicit prov=: {silent}. "
        f"No prov= means prov=NATIVE, prov_basis='native', prov_confidence=1.0 — the "
        f"claim that a venue reported this value directly. Build the tail with "
        f"provenance_fields(basis, inputs) and pass all four fields."
    )


_FABRICATION_HEADER = "import msgspec.structs\nfrom crocodile.core.schema.records import Trade\n"

_EVASIONS: dict[str, str] = {
    "literal": "Trade(amount=1.0)",
    "module-level constant": "_SIZE = 1.0\nTrade(amount=_SIZE)",
    "constant renamed twice": "_A = 1.0\n_B = _A\nTrade(amount=_B)",
    "local variable": "def f():\n    size = 1.0\n    return Trade(amount=size)",
    "folded arithmetic": "Trade(amount=1.0 * 1)",
    "negated constant": "Trade(amount=-0.0)",
    "coercion call": "Trade(amount=float(1))",
    "coercion of a bound constant": "_SIZE = 1\ndef f():\n    return Trade(amount=float(_SIZE))",
    "dict unpacking": 'Trade(**{"amount": 1.0})',
    "aliased import": "from crocodile.core.schema.records import Trade as T\nT(amount=1.0)",
    "msgspec replace": "msgspec.structs.replace(t, amount=1.0)",
    "msgspec replace, imported": (
        "from msgspec.structs import replace as swap\nswap(t, amount=1.0)"
    ),
    "msgspec replace, imported under its own name": (
        "from msgspec.structs import replace\nreplace(t, amount=1.0)"
    ),
    "a conditional with two constant branches": (
        "def f(x):\n    return Trade(amount=1.0 if x else 1.0)"
    ),
    "a constant builtin": "Trade(amount=min(1.0, 2.0))",
    "a walrus": "Trade(amount=(size := 1.0))",
    "a module-level table of assumed values": '_KW = {"amount": 1.0}\nTrade(amount=_KW["amount"])',
    "a tuple of assumed values": "_SIZES = (1.0, 2.0)\nTrade(amount=_SIZES[0])",
    "that table unpacked whole": '_KW = {"amount": 1.0}\nTrade(**_KW)',
    "an index guard over a short array": (
        "def f(sizes, i):\n    return Trade(amount=sizes[i] if i < len(sizes) else 0.0)"
    ),
    "a presence guard": (
        "def f(msg):\n    return Trade(amount=float(msg['s']) if 's' in msg else 0.0)"
    ),
    "an or-pad": "def f(msg):\n    return Trade(amount=msg.get('size') or 0.0)",
    "a guard nested inside a guard": (
        "def f(msg, x):\n    return Trade(amount=msg.size if x else (msg.alt or 1.0))"
    ),
}
"""Every spelling of one fabrication the scanner has to read as the same thing.

The list is the review's, verbatim, plus the chained forms it implies. The scanner
that shipped caught the first row and missed every other one — which is exactly how
``_UNPUBLISHED_SIZE = 0.0`` came to satisfy a gate whose whole subject it is, and why
restoring ``_ASSUMED_SIZE = 1.0`` would also have satisfied it.

The plain ``from msgspec.structs import replace`` row is the later one. ``_replace_calls``
seeded its alias set with ``{"replace"}`` and then excluded that exact name, so the
plainest spelling in the family evaded — and this list covered only the ``as swap``
form, the single spelling that *was* caught, so the parametrised test reported green
over a hole in the middle of what it claimed to cover. A probe set that only holds the
cases the implementation already handles is not a probe set.

The four guard rows are the latest, and one of them used to sit in
:data:`_HONEST_READINGS` as "a conditional with one measured branch" — declared
acceptable on the argument that a real adapter writes a fallback. ``msn_money`` is what
that argument looked like in production: ``safe_float(open_p[idx]) if idx <
len(open_p) else 0.0`` on all five of a bar's measurements, so the tail of every ragged
chart payload became zero-priced bars at ``prov=NATIVE``. A required measurement is not
a field with a sensible default. Where the guard takes the constant branch the payload
supplied nothing, and the honest move is to skip the record.

What is still not seen is in :data:`FABRICATION_BLIND_SPOTS`, out loud.
"""

_HONEST_READINGS: dict[str, str] = {
    "read off a payload": "Trade(amount=float(payload['size']))",
    "read off an attribute": "Trade(amount=msg.size)",
    "computed from an input": "def f(n):\n    return Trade(amount=n * 2)",
    "a name that is not always constant": (
        "def f(msg):\n    size = 0.0\n    if msg:\n        size = msg.size\n    "
        "return Trade(amount=size)"
    ),
    "a loop variable": "def f(sizes):\n    for size in sizes:\n        yield Trade(amount=size)",
    "a builtin over a measurement": "def f(msg):\n    return Trade(amount=min(1.0, msg.size))",
    "a guard between two measurements": (
        "def f(x, msg):\n    return Trade(amount=msg.size if x else msg.alt_size)"
    ),
    "a table rebound to a payload": (
        "def f(payload):\n    kw = {'amount': 1.0}\n    kw = payload\n    return Trade(**kw)"
    ),
}
"""Readings the scanner must *not* flag, so the gate is a filter and not a ban.

``a name that is not always constant`` is the discrimination that matters, and it is
also the price of the guard rule: a name assigned ``0.0`` on one branch and a payload
value on another is exactly the same fabrication as the conditional form, and it is
deliberately not flagged, because chasing names was measured over this tree and read
``resample_trades_to_bars``'s accumulator as a constant. That trade-off is written down
in :data:`FABRICATION_BLIND_SPOTS` rather than left for someone to discover.

``a guard between two measurements`` is the shape the guard rule must stay clear of: a
conditional whose branches are both readings is a choice between sources, not a
substitute for one.
"""


@pytest.mark.parametrize("form", sorted(_EVASIONS), ids=lambda k: k.replace(" ", "_"))
def test_gate3b_the_fabrication_scanner_sees_through_every_spelling(form: str) -> None:
    """A gate that only sees the literal spelling is a gate that teaches people to rename."""
    tree = ast.parse(_FABRICATION_HEADER + _EVASIONS[form])

    offences = _fabrications_in(tree, _measurement_fields(), "<probe>")

    assert offences, f"{form!r} evaded the scanner: {_EVASIONS[form]!r}"


@pytest.mark.parametrize("form", sorted(_HONEST_READINGS), ids=lambda k: k.replace(" ", "_"))
def test_gate3b_the_fabrication_scanner_leaves_a_real_reading_alone(form: str) -> None:
    """The other half of the gate: an adapter reading a payload writes an expression."""
    tree = ast.parse(_FABRICATION_HEADER + _HONEST_READINGS[form])

    assert _fabrications_in(tree, _measurement_fields(), "<probe>") == []


@pytest.mark.parametrize(
    "blind_spot", sorted(FABRICATION_BLIND_SPOTS), ids=lambda k: k.replace(" ", "_")
)
def test_gate3b_a_documented_blind_spot_is_still_open(blind_spot: str) -> None:
    """Each declared limit, exercised, so the prose cannot outlive the limitation.

    The assertion runs the *opposite* way to :data:`_EVASIONS`: this is a fabrication the
    scanner does not see, and the test says so out loud rather than leaving a reader to
    take the paragraph's word for it. A red result here is good news — somebody taught the
    scanner one more spelling — and the fix is to delete the entry, which
    :func:`test_gate3b_the_blind_spot_list_is_the_set_that_was_measured` then asks to be
    confirmed.
    """
    probe, _why = FABRICATION_BLIND_SPOTS[blind_spot]
    tree = ast.parse(_FABRICATION_HEADER + probe)

    offences = _fabrications_in(tree, _measurement_fields(), "<probe>")

    assert offences == [], (
        f"{blind_spot!r} is no longer a blind spot — the scanner flagged {probe!r}. Delete "
        f"its FABRICATION_BLIND_SPOTS entry; the list is what the gate admits it misses, "
        f"and an entry for something it now catches understates it."
    )


def test_gate3b_the_blind_spot_list_is_the_set_that_was_measured() -> None:
    """Pinning the set is what stops an entry being deleted instead of closed.

    The docstring above has always said deleting without closing is unwelcome, and nothing
    read it. Both halves are enforced now: the parametrised test above fails when a listed
    hole closes, and this one fails when a hole leaves the list — so the only way out of
    the list is a commit that shows both.
    """
    assert sorted(FABRICATION_BLIND_SPOTS) == [
        "a clamp over a real reading",
        "a class attribute",
        "a constant imported from another module",
        "a constant reached through a container",
        "a default argument",
        "a helper's return value",
        "a name assigned a constant on one branch",
        "an invented scale over a real reading",
    ], (
        "the blind-spot list changed. Adding one is a limit worth documenting; removing one "
        "means the scanner now sees that spelling, which "
        "test_gate3b_a_documented_blind_spot_is_still_open would already have said."
    )

    for blind_spot, (probe, why) in FABRICATION_BLIND_SPOTS.items():
        assert probe.strip(), f"{blind_spot} carries no probe, so nothing checks it is open"
        assert why.strip(), f"{blind_spot} carries no argument for why it is left open"


_LICENCE_WORDS = ("illustrative", "placeholder", "approximate", "assumed", "rough", "guess")
"""Words that turn a number nobody measured into a number somebody signed off."""

_SCALE_EXPR = re.compile(
    r"(?:[*/]\s*(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][-+]?\d+)?)"
    r"|(?:(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][-+]?\d+)?\s*[*/])"
)
"""A numeric literal multiplying or dividing something — the shape a scale factor has."""


def _licensed_scale(code: str, comment: str) -> bool:
    """True when a trailing comment excuses a numeric scale on its own line.

    Both halves are required, and narrowly. The word alone catches three lines in
    ``base_onchain`` that annotate a URL and two event-topic hashes — real questions, and
    not this one; a gate that reports them here is a gate that gets read as noise. The
    expression alone is every honest fixed-point conversion in the tree, which is most of
    the on-chain connectors.
    """
    return bool(_SCALE_EXPR.search(code)) and any(w in comment.lower() for w in _LICENCE_WORDS)


_SCALE_LICENCES: dict[str, tuple[str, str]] = {
    "the gmx line as it shipped": (
        'funding_rate = float(decoded.args["entryFundingRate"]) / 1e9 ',
        " illustrative scale",
    ),
    "a factor rather than a divisor": ("qty = raw * 1e-8 ", " assumed decimals"),
}
"""Lines this rule must flag, the first of them verbatim from the tree it was written for."""

_HONEST_SCALES: dict[str, tuple[str, str]] = {
    "a cited constant": (
        "price = float(args['price']) / 1e30 ",
        " PRICE_PRECISION, GMX v1 Vault.sol",
    ),
    "a candid word about something that is not a scale": (
        'ws_url = "wss://base-rpc.publicnode.com" ',
        " placeholder",
    ),
    "a bare conversion": ("ns = ms * 1_000_000", ""),
}
"""Lines it must leave alone, so the rule is a filter and not a ban on arithmetic."""


@pytest.mark.parametrize("case", sorted(_SCALE_LICENCES), ids=lambda k: k.replace(" ", "_"))
def test_gate3b_the_scale_rule_flags_a_licence(case: str) -> None:
    """Guard the guard: a rule narrowed until it matches nothing is a rule that passes."""
    code, comment = _SCALE_LICENCES[case]
    assert _licensed_scale(code, comment), f"{case!r} went unflagged"


@pytest.mark.parametrize("case", sorted(_HONEST_SCALES), ids=lambda k: k.replace(" ", "_"))
def test_gate3b_the_scale_rule_leaves_an_argued_constant_alone(case: str) -> None:
    code, comment = _HONEST_SCALES[case]
    assert not _licensed_scale(code, comment), f"{case!r} was flagged"


def test_gate3b_a_scale_is_not_licensed_by_calling_it_illustrative() -> None:
    """A trailing comment is not a declaration, however candid it sounds.

    ``gmx_synthetix/connector.py`` carried
    ``float(decoded.args["entryFundingRate"]) / 1e9 # illustrative scale``, and the comment
    is the whole of why it survived review: it reads as a decision that was taken and
    recorded, so nobody asked what the real divisor was. It is 1e6 — GMX v1's
    ``FUNDING_RATE_PRECISION`` — and the quantity was not a funding rate at any divisor,
    being one endpoint of a difference between cumulative indices.

    The fabrication scanner above cannot reach this shape at all; that limit is declared in
    :data:`FABRICATION_BLIND_SPOTS` under ``an invented scale over a real reading``. What
    this rule can do is shut the door the comment opened. A number this codebase cannot
    justify belongs in :data:`FABRICATES_MEASUREMENTS` with its argument, or belongs
    nowhere.

    Only *trailing* comments are read — a comment sharing its line with code, which is the
    form that annotates an expression and so reads as permission for it. A standalone
    comment saying what a previous fabrication was and why it went is documentation, and
    the module this rule was written for now holds exactly such a paragraph.
    """
    offences: list[str] = []
    for path in sorted(_source_root().rglob("*.py")):
        for index, text in enumerate(path.read_text().splitlines(), start=1):
            code, sep, comment = text.partition("#")
            if not sep or not code.strip():
                continue
            if _licensed_scale(code, comment):
                relative = path.relative_to(_source_root()).as_posix()
                offences.append(f"{relative}:{index} — {comment.strip()!r}")
    assert not offences, (
        f"a comment standing in for a measurement: {offences}. If the value is right, say "
        f"what makes it right and cite the source; if it cannot be determined, the record "
        f"should not carry it. FABRICATES_MEASUREMENTS is where a number nobody measured "
        f"is declared, next to the argument for it — a comment beside the expression is "
        f"read as a decision already taken."
    )


def test_gate3b_the_scanner_records_whether_the_fabricating_record_states_prov() -> None:
    """A licence in FABRICATES_MEASUREMENTS does not carry the row's provenance claim.

    The list argues that one number in one field is the honest encoding of a quantity.
    It says nothing about whether a venue reported it, and `prov` is the field the REST
    and MCP surfaces turn into a warning — so a declared fabrication that stays silent
    on `prov` is served exactly as a venue reading, which is the whole failure.
    """
    fields = _measurement_fields()

    silent = _fabrications_in(
        ast.parse(_FABRICATION_HEADER + "Trade(amount=1.0)"), fields, "<probe>"
    )
    declared = _fabrications_in(
        ast.parse(_FABRICATION_HEADER + "Trade(amount=1.0, prov='synthetic')"), fields, "<probe>"
    )

    assert [offence.states_prov for offence in silent] == [False]
    assert [offence.states_prov for offence in declared] == [True]


def test_gate3b_a_guard_reports_the_constant_it_would_substitute() -> None:
    """The licence is keyed on ``(field, value)``, so a guard has to yield its value too.

    Reporting the whole expression and no number would make every padded guard
    unlicensable, which is the pressure that gets a gate deleted rather than satisfied.
    """
    tree = ast.parse(
        _FABRICATION_HEADER
        + "def f(sizes, i):\n    return Trade(amount=sizes[i] if i < len(sizes) else 0.0)"
    )

    offences = _fabrications_in(tree, _measurement_fields(), "<probe>")

    assert [(offence.field, offence.value) for offence in offences] == [("amount", 0.0)]


def test_gate3b_the_fabrication_list_cannot_hold_a_stale_or_silent_entry():
    """Self-cleaning, on the same discipline as DERIVES_RECORDS and IRREDUCIBLE."""
    fabricated = _fabricated_measurements()
    for relative, (licensed, why) in FABRICATES_MEASUREMENTS.items():
        assert why.strip(), f"{relative} is on FABRICATES_MEASUREMENTS with no justification"
        assert relative in fabricated, (
            f"FABRICATES_MEASUREMENTS names {relative}, which fabricates nothing any more; "
            f"drop the entry rather than leave a licence lying around"
        )
        found = {(offence.field, offence.value) for offence in fabricated[relative]}
        stale = sorted(pair for pair in licensed if pair not in found)
        assert not stale, (
            f"FABRICATES_MEASUREMENTS licenses {stale} in {relative}, which no longer "
            f"writes it; drop the entry rather than leave a licence lying around"
        )


def test_gate3b_a_licence_for_one_field_does_not_cover_another_in_the_same_module() -> None:
    """A licence is for a number in a *field*, not for a file.

    ``equity/resample/ohlcv.py`` is licensed for a structural ``volume = 0.0`` on quote
    bars. Keyed on the value alone, that licence silently covered ``Trade(amount=0.0)``
    and ``DepthProfile(reference_price=0.0, …)`` added anywhere in the same module — and
    that module is exactly where new resamplers get added. Three different quantities,
    one argument between them.
    """
    licensed = FABRICATES_MEASUREMENTS["equity/resample/ohlcv.py"][0]
    module = _source_root() / "equity" / "resample" / "ohlcv.py"
    sneaked = (
        "\n\nfrom crocodile.core.schema.records import Trade as _T, DepthProfile as _D\n"
        "def _sneak():\n"
        "    return _T(amount=0.0), _D(reference_price=0.0)\n"
    )

    offences = _fabrications_in(
        ast.parse(module.read_text() + sneaked),
        _measurement_fields(),
        "equity/resample/ohlcv.py",
    )
    exempt = {o.field for o in offences if (o.field, o.value) in licensed}
    caught = {o.field for o in offences if (o.field, o.value) not in licensed}

    assert exempt == {"volume"}, "the licensed quantity is the only one that stays exempt"
    assert caught == {"amount", "reference_price"}


# ---------------------------------------------------------------------------
# Gate 3c — a registered formula's output must actually vary with its inputs
# ---------------------------------------------------------------------------
# Gate 3 scans *call sites* for a hand-written ``prov_confidence=``. That is the
# bypass it was built for, and it left the registry itself unwatched — so a
# constant moved one indirection inwards was invisible to every gate here, and the
# test that pinned it (``confidence_for(basis, {}) == 1.0``) could not fail: it
# asserted whatever the registry held, plus that a docstring was non-empty, which
# is not the same as arguing. ``ohlcv_from_ohlcv`` reported 1.0 for a 1d bar built
# from three 1m bars while holding both numbers the ratio needs.
#
# So the registry is probed instead of read: feed each formula different inputs and
# require the answers to differ. A constant is still allowed — ``native`` is 1.0 and
# ``unavailable`` is 0.0 *by definition* — but only when declared and argued below,
# and the declaration is checked against the formula rather than trusted.

CONSTANT_BY_DEFINITION: dict[str, tuple[float, str]] = {
    "native": (
        1.0,
        "A venue-reported value is fully sampled at its own level because it was read "
        "rather than reconstructed. There is no partial reading of a number a venue "
        "published.",
    ),
    "unavailable": (
        0.0,
        "A hole carries no information, so it is fully unsampled at any level. This is "
        "the definition of the level, not a measurement of an instance of it.",
    ),
    "ohlcv_from_trades": (
        1.0,
        "A trade stream declares no denominator. How many prints a bucket should hold is "
        "not a property of the market, so there is nothing to divide by — unlike "
        "ohlcv_from_ohlcv, where each input bar declares its own width, and unlike "
        "yahoo_1m_vap, where a session contains 390 one-minute bars whether or not any "
        "were fetched. The sample size is reported on the record as num_trades.",
    ),
    "ohlcv_from_quotes": (
        1.0,
        "Same argument as ohlcv_from_trades, and the same absent denominator: a quote "
        "stream has no count a bucket ought to contain. A one-quote bucket really does "
        "yield open == high == low == close, and what says so is num_trades = 1 and a "
        "volume that is a structural zero, not a ratio with an invented base.",
    ),
    "caller_supplied": (
        1.0,
        "A pure function is exact over whatever it was handed, so there is no sampling "
        "loss inside this engine to grade — and the sampling story of inputs the caller "
        "produced is not ours to assert. A lower number would be this engine grading a "
        "stranger's data it cannot observe. The honesty is in the basis name, which "
        "separates these from anything the lake produced; `native` could not, which is "
        "why two porting agents independently reported it as claiming too much.",
    ),
    "scraped_last_price": (
        0.0,
        "The page publishes a price and never a size, for any symbol at any time, so the "
        "quantity that makes a Trade a trade is unsampled at every call. Nothing varies "
        "because nothing about the method varies.",
    ),
    "book_resample": (
        1.0,
        "A book capture is exact at the instant it is stamped: the engine replays absolute "
        "levels from a real venue snapshot and raises BookGap rather than guessing across a "
        "sequence break, so there is nothing estimated to score. This entry is the one on "
        "this list that arrived by *losing* its formula. It was "
        "max(1 - lookahead_ns / interval_ns, 0.0), whose only observable was the crypto "
        "resampler's apply-then-emit ordering — a snapshot stamped 10:00:00 holding a "
        "10:00:00.200 update, scored 0.8 and written to the lake. The two book resamplers "
        "collapsed onto the ordering that flushes boundaries before applying, which makes "
        "lookahead_ns structurally zero at every capture, and this gate is exactly what a "
        "surviving `1 - 0/interval` would have walked past: probed with inputs the call "
        "site can no longer produce, it varies; reached from the resampler, it cannot. "
        "What used to be scored is now refused — _capture_snapshot raises ProvenanceError "
        "rather than build a record whose tail would be a lie. Staleness is the tempting "
        "replacement and is declined on purpose: a quiet interval is not an unsampled one, "
        "and how often a book ought to tick has no reference to divide by.",
    ),
    "farcaster_cast_search": (
        0.0,
        "Neynar's cast search returns casts, and every one of FarcasterCorrelation's "
        "three required fields is produced by the adapter from them — a page length, a "
        "substring test over author bios, and arithmetic on the count. There is no "
        "sampling evidence to grade because nothing on the record was sampled, which is "
        "the same reading scraped_last_price carries for the same reason.",
    ),
}
"""Bases whose confidence is a constant, the value, and the argument for it.

An entry silences the probe below, which is exactly why the argument is mandatory and
why the value is asserted rather than taken on trust — the same discipline
:data:`crocodile.core.capability.IRREDUCIBLE` carries for the symmetry gate.
"""

_PROBE_VALUES = (False, True, 0, 1, 2, 3, 4, 391, 1_000_000)
"""Boolean and integer observations to feed a formula. Non-negative numeric probes,
because every formula in the registry validates
its inputs and a probe that only ever trips validation measures nothing — but not all
small. The first version stopped at 3, so a key read only behind a guard that rejects
everything below 4 was never reached at all, and a formula could hide a whole branch
from the probe by validating its way past it. 391 is one bar past a full US session,
which is the smallest input that separates a saturating formula from a linear one.
Boolean observations cover strict protocol states such as synchronization; integer
1 must not be mistaken for a boolean True by a formula that validates its inputs."""


class _KeyRecorder(Mapping[str, object]):
    """A mapping that answers every lookup and remembers what was asked for.

    Formulas declare the observables they need by reading them, not in the registration
    — ``inputs=`` names data *channels*, which is a different thing. Rather than keep a
    second hand-written list of key names for the gate to go stale against, the keys are
    discovered by letting the formula ask.
    """

    def __init__(self, value: object) -> None:
        self._value = value
        self.seen: list[str] = []

    def __getitem__(self, key: str) -> object:
        if key not in self.seen:
            self.seen.append(key)
        return self._value

    def __iter__(self) -> Iterator[str]:
        return iter(self.seen)

    def __len__(self) -> int:
        return len(self.seen)


def _shipped_registry() -> dict[str, object]:
    """The bases ``crocodile`` itself registers.

    Reaching into ``_REGISTRY`` is deliberate: ``registered_bases()`` returns names, and
    this gate needs the formula. Tests register throwaway bases in the same process, so
    the registry is filtered to formulas defined under ``crocodile.`` — otherwise this
    gate would pass or fail on test ordering.
    """
    from crocodile.core.schema.provenance import _REGISTRY, load_all_bases

    load_all_bases()
    return {
        basis: registered
        for basis, registered in _REGISTRY.items()
        if getattr(registered.fn, "__module__", "").startswith("crocodile.")
    }


def _observed_keys(fn) -> list[str]:
    """Return the input keys ``fn`` reads, discovered by calling it under a recorder."""
    keys: list[str] = []
    for value in _PROBE_VALUES:
        recorder = _KeyRecorder(value)
        try:
            fn(recorder)
        except Exception:  # a rejected probe still reveals the keys it read first
            pass
        for key in recorder.seen:
            if key not in keys:
                keys.append(key)
    return keys


_MAX_PROBE_GRID = 100_000
"""Ceiling on the assignments below, so a wide formula fails loudly rather than hangs.

The widest formula in the registry reads four keys, which is 2 401 points and runs in
milliseconds. A seventh key would be 823 543, and a gate that silently takes a minute is a
gate somebody eventually deletes — so the search refuses instead, and whoever writes that
formula decides what to do about it in the open.
"""


def _held_assignments(others: list[str]) -> Iterator[dict[str, int]]:
    """Every assignment of ``others`` drawn from :data:`_PROBE_VALUES`."""
    for combo in itertools.product(_PROBE_VALUES, repeat=len(others)):
        yield dict(zip(others, combo, strict=True))


def _sweep(basis: str, held: dict[str, int], key: str) -> set[float]:
    """Answers ``basis`` gives as ``key`` moves over the probe values, everything else fixed."""
    from crocodile.core.schema.provenance import ConfidenceInputError, confidence_for

    results: set[float] = set()
    for value in _PROBE_VALUES:
        try:
            results.add(confidence_for(basis, {**held, key: value}))
        except ConfidenceInputError:
            continue
    return results


def _variation_witness(
    basis: str, keys: list[str], key: str
) -> tuple[dict[str, int], set[float]] | None:
    """Find one assignment of the other keys under which ``key`` moves the answer.

    A *witness*, not a single fixed baseline, and that is the whole of the change this
    function represents. The gate's name claims a formula varies with every input it
    declares, and the honest way to test one input is to hold the others somewhere and move
    it — but "somewhere" cannot be an arbitrary constant, because a clamped term is flat over
    part of its range and a fixed baseline lands inside one.

    Both live examples are the multiplicative kind, and neither is a defect. Hold every key
    of ``book_snapshot_slice`` at 1 and ``age_ns == window_ns``, so ``freshness`` is exactly
    zero and the product is zero whatever ``n_levels`` does — the fill term is real, and the
    baseline switched it off. Worse, *no* uniform baseline can switch it back on, because
    ``age`` and ``window`` are equal in all of them. ``ohlcv_from_ohlcv``'s ``covered_ns`` is
    the same story with ``sampled_ns``. So the search is over assignments rather than over
    one of them: ``{n_requested: 1, age_ns: 0, window_ns: 1}`` witnesses ``n_levels``,
    ``{sampled_ns: 1, tradeable_ns: 2}`` witnesses ``covered_ns``, and every declared input
    of every registered formula has a witness today.

    Saturation is therefore handled by asking the right question rather than by an exemption
    list. "This input never moves the answer" is the defect; "this input does not move the
    answer *here*" is what a clamp is for, and a gate that could not tell them apart would
    have to be loosened until it stopped saying anything — which is the state it was found in.
    """
    others = [name for name in keys if name != key]
    grid = len(_PROBE_VALUES) ** len(others)
    assert grid <= _MAX_PROBE_GRID, (
        f"{basis} reads {len(keys)} inputs, so witnessing one takes {grid} assignments. "
        f"Narrow the formula or narrow _PROBE_VALUES; do not widen the ceiling to make this "
        f"quiet."
    )
    for held in _held_assignments(others):
        results = _sweep(basis, held, key)
        if len(results) > 1:
            return held, results
    return None


def _probe_results(basis: str, keys: list[str]) -> set[float]:
    """Every answer ``basis`` gives over the whole probe grid.

    Used by the constant check, which asks the opposite question from the one above: not
    "does some input move this" but "does *anything*", so it wants the widest sweep there is
    rather than a witness. It used to vary one key at a time from a fixed baseline of 1,
    which is the same blind spot :func:`_variation_witness` documents — a declared constant
    that had quietly grown a clamped formula could have read as constant from that baseline.
    """
    from crocodile.core.schema.provenance import ConfidenceInputError, confidence_for

    results: set[float] = set()
    for held in _held_assignments(keys):
        try:
            results.add(confidence_for(basis, held))
        except ConfidenceInputError:
            continue
    return results


@pytest.mark.parametrize("basis", sorted(set(_shipped_registry()) - set(CONSTANT_BY_DEFINITION)))
def test_gate3c_a_registered_formula_varies_with_its_inputs(basis: str) -> None:
    """Every input a formula declares must move its answer, not merely one of them."""
    _assert_every_declared_input_moves_the_answer(basis, _shipped_registry()[basis].fn)


def _assert_every_declared_input_moves_the_answer(basis: str, fn: object) -> None:
    """The gate's body, per input, so a fixture basis can be run through the same assertion.

    This used to assert what its name did not. ``_probe_results`` accumulated one flat set
    across every key and the gate asked ``len(results) > 1`` — which is "varies with *at
    least one* input". A formula with one live input and three decorative ones passed with
    the same green as a formula where all four were load-bearing, and the failure message
    still listed all four keys as though they had been checked.

    That is not hypothetical. A referee found ``treasury_carry``'s ``n_price_legs`` frozen at
    its ``def`` default because no call site passes it, so one of that formula's three terms
    is dead in production while the old gate reported the formula as varying — on the
    strength of the other two.

    Note what this gate can and cannot see, because the ``treasury_carry`` finding sits
    across the line. It probes the *formula*, so it can say every declared input is
    load-bearing in the arithmetic. It cannot say a call site actually passes one: an input
    that varies here and is never supplied is a reachability fact about the caller, and
    reaching for it would mean this gate scanning call sites, which is Gate 3's job and a
    different scanner. The honest division is that this one refuses a decorative *parameter*
    and Gate 3's neighbours refuse a decorative *call*.
    """
    keys = _observed_keys(fn)
    assert keys, (
        f"{basis} reads no inputs, so its confidence cannot vary. Measure something "
        f"observable, or declare it in CONSTANT_BY_DEFINITION with the argument."
    )
    dead = [key for key in keys if _variation_witness(basis, keys, key) is None]
    assert not dead, (
        f"{basis} declares {keys} and its answer does not move with {dead} under any "
        f"assignment of the others. An input that cannot change the number is decoration: "
        f"either it belongs in the formula and does not appear in it, or it does not belong "
        f"in the formula and should not be read. Remove it, or make it count."
    )


@pytest.mark.parametrize("basis", sorted(CONSTANT_BY_DEFINITION))
def test_gate3c_a_declared_constant_is_the_constant_it_declares(basis: str) -> None:
    """The exemption is checked, not trusted.

    A basis on the list that quietly grew a formula, or that returns something other than
    the value argued for, would otherwise be exempt from both this gate and Gate 3.
    """
    from crocodile.core.schema.provenance import confidence_for, describe

    expected, why = CONSTANT_BY_DEFINITION[basis]
    assert why.strip(), f"{basis} is on CONSTANT_BY_DEFINITION with no argument"
    assert basis in _shipped_registry(), f"{basis} is declared constant but is not registered"

    fn = _shipped_registry()[basis].fn
    keys = _observed_keys(fn)
    observed = _probe_results(basis, keys) if keys else {confidence_for(basis, {})}
    assert observed == {expected}, (
        f"{basis} is declared constant at {expected} but returned {sorted(observed)}"
    )
    assert describe(basis).strip(), f"{basis} must carry the argument in its description too"


def test_gate3c_the_constant_list_holds_no_stale_entry() -> None:
    """A name that left the registry must not leave its exemption behind."""
    unknown = sorted(set(CONSTANT_BY_DEFINITION) - set(_shipped_registry()))
    assert not unknown, f"CONSTANT_BY_DEFINITION names unregistered bases: {unknown}"


# ---------------------------------------------------------------------------
# Gate 3c's own mechanism, exercised — a decorative input, and a clamped one
# ---------------------------------------------------------------------------
# Every registered formula passes today, so the assertion above never takes its
# failing branch against live state. That is the shape this file keeps finding, and
# it is how the gate came to assert "varies with at least one input" under a name
# claiming all of them: nothing in the tree could tell the two readings apart.
# `tests/conformance/conftest.py` snapshots the provenance registry per test, so a
# fixture basis registered here leaves nothing behind.


def _register_fixture_basis(basis: str, fn: Callable[[Mapping[str, object]], float]) -> object:
    from crocodile.core.schema.provenance import Provenance as _Provenance
    from crocodile.core.schema.provenance import register_basis

    register_basis(basis, level=_Provenance.DERIVED, inputs=["ohlcv"])(fn)
    from crocodile.core.schema.provenance import _REGISTRY

    return _REGISTRY[basis].fn


def test_gate3c_a_decorative_input_is_caught() -> None:
    """One live input, one read and ignored — the case the old flat set could not see.

    The formula varies, so ``len(results) > 1`` held and the gate was green. Its name
    promised more than that, and this is the difference asserted rather than argued.
    """

    def _two_thirds_decoration(inputs: Mapping[str, object]) -> float:
        """Bars sampled out of a full session; the other two reads are the fixture."""
        live = int(inputs["n_bars"])  # type: ignore[call-overload]
        inputs["window"]  # read and discarded, which is what makes it decoration
        inputs["tolerance"]
        return min(live / 391, 1.0)

    fn = _register_fixture_basis("fixture_decorative", _two_thirds_decoration)
    with pytest.raises(AssertionError, match="does not move with"):
        _assert_every_declared_input_moves_the_answer("fixture_decorative", fn)


def test_gate3c_an_input_that_only_moves_the_answer_off_a_clamp_is_accepted() -> None:
    """The false positive a single fixed baseline produces, asserted as a pass.

    ``n_levels`` here is live, and it is invisible from ``{n_levels: 1, age: 1, window: 1}``
    because the freshness factor is exactly zero there — which is ``book_snapshot_slice``'s
    real shape, reduced to the two terms that collide. The witness search finds
    ``age=0, window=1`` and the input reads as live, which it is.
    """

    def _fill_times_freshness(inputs: Mapping[str, object]) -> float:
        """How full the ladder is, times how fresh it is — book_snapshot_slice's shape."""
        n_levels = int(inputs["n_levels"])  # type: ignore[call-overload]
        age = int(inputs["age_ns"])  # type: ignore[call-overload]
        window = int(inputs["window_ns"])  # type: ignore[call-overload]
        if window <= 0:
            from crocodile.core.schema.provenance import ConfidenceInputError

            raise ConfidenceInputError("window_ns must be positive")
        return min(n_levels / 10, 1.0) * max(1.0 - age / window, 0.0)

    fn = _register_fixture_basis("fixture_clamped", _fill_times_freshness)
    _assert_every_declared_input_moves_the_answer("fixture_clamped", fn)


def test_gate3c_a_formula_that_reads_nothing_is_caught() -> None:
    """The pre-existing branch, kept covered now that the assertion around it moved."""

    def _flat(_: Mapping[str, object]) -> float:
        """A number with no observable behind it."""
        return 0.5

    fn = _register_fixture_basis("fixture_flat", _flat)
    with pytest.raises(AssertionError, match="reads no inputs"):
        _assert_every_declared_input_moves_the_answer("fixture_flat", fn)
