"""What all three projections share: resolution, invocation, and the provenance envelope.

Everything here is surface-agnostic on purpose. A projector's own module should hold only
what is genuinely specific to its transport — how a parameter is spelled on a command
line, in a query string, in a JSON schema — because anything else in there is a fourth
copy of the capability list waiting to drift from the other three.

The one thing the surfaces are *allowed* to disagree about is trust, and
:func:`build_context` is where they say so.
"""

from __future__ import annotations

import inspect
import itertools
from collections.abc import Iterator, Sequence
from types import NoneType, UnionType
from typing import TYPE_CHECKING, Any, Final, Literal, Union, get_args, get_origin

import duckdb
import msgspec

from crocodile.capabilities import load_all
from crocodile.core.capability import (
    REGISTRY,
    AssetClass,
    Capability,
    CapabilityContext,
    Impl,
    ReturnKind,
    run_to_completion,
)
from crocodile.core.config import Settings
from crocodile.core.errors import CapabilityUnavailable, ConfigError
from crocodile.core.schema.provenance import Provenance, describe
from crocodile.core.util.json_safe import json_safe_float

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

    import polars as pl

    from crocodile.core.store.catalog import Catalog

__all__ = [
    "BAD_REQUEST",
    "NETWORK_ROW_LIMIT",
    "NOT_CONFIGURED",
    "REFUSED",
    "UNAVAILABLE",
    "asset_class_option_values",
    "banner_for",
    "build_context",
    "build_params",
    "drive",
    "effective_provenance",
    "invoke",
    "params_schema",
    "payload",
    "provenance_block",
    "requires_explicit_asset_class",
    "resolve",
    "resolve_asset_class",
    "stream_summary",
    "structured_fields",
    "symbol_field_names",
    "symbol_hints",
    "warning_for",
    "wire_names",
]

NETWORK_ROW_LIMIT: Final = 10_000
"""Rows a network surface will materialise from a raw-SQL read.

The number the legacy REST server already used, on every one of its fifteen
``_*_MAX_LIMIT`` constants. It is one constant here because fifteen copies of one policy
is the shape this package exists to remove, and because a per-capability limit is a
per-capability branch in a projector.
"""


# ---------------------------------------------------------------------------
# Whose fault a failure is
# ---------------------------------------------------------------------------
#
# Three categories, declared once and read by all three projectors, because the question
# they are answering is the same one in three vocabularies: a status code, an exit code, a
# JSON-RPC result. Anything not named here is *ours* — a 500, a traceback, a protocol error
# — and that default is the point. The legacy REST server caught ``Exception`` and answered
# 400, which is the opposite failure: a lake that cannot be read reported as a bad request
# tells the caller to fix a query that was fine, and pages nobody.

UNAVAILABLE: Final[tuple[type[BaseException], ...]] = (CapabilityUnavailable,)
"""This capability has no implementation for the asset class that was resolved.

Not the caller's mistake and not a fault: the request is well formed and the answer does
not exist yet, which is 501 on REST.
"""

NOT_CONFIGURED: Final[tuple[type[BaseException], ...]] = (ConfigError,)
"""The request is fine and *this deployment* has not been given what answering it needs.

``GET /api/v1/markets?asset_class=equity`` on an unconfigured install answered ``500
Internal Server Error`` with no body, while the CLI printed the fix — "set
``CROCODILE_SEC_USER_AGENT``…" — from the same exception. Two surfaces, one
:class:`~crocodile.core.errors.ConfigError`, and only one of them told anybody anything.

It is a category rather than a 500 with a body because of what 5xx *means to a client*: it
is the one status class every backing-off library retries, so a missing environment variable
became a retry storm against an endpoint that will answer identically forever. 501 is what
this projection already serves for :data:`UNAVAILABLE`, and it is the same sentence: the
request is well formed and this deployment has no answer for it. Whether the reason is "no
implementation for that asset class" or "no credential for the implementation there is" is
not a distinction a *caller* can act on differently — both are terminal, both are the
operator's to fix — so it is one status with the actionable message in the body.

Only ``ConfigError``, not ``CrocodileError``: a connector failing mid-request is a fault and
a 5xx is honest about it, and retrying a venue that timed out is the right behaviour. This
names the family whose docstring says it is "detected before any source is contacted".
"""

REFUSED: Final[tuple[type[BaseException], ...]] = (PermissionError,)
"""The request is fine and this surface is not trusted to run it.

``capabilities.ops._refuse_readonly`` chose ``PermissionError`` over ``ValueError``
precisely so this category could exist — its docstring says "a REST projection maps [it] to
403 and a caller must not retry" — and then no surface caught it, so a deliberate policy
refusal was served as a 500 and became indistinguishable from a crash. A crash invites a
retry; this must not be retried, because nothing about it will be different next time.
"""

BAD_REQUEST: Final[tuple[type[BaseException], ...]] = (
    ValueError,
    duckdb.ProgrammingError,
    duckdb.DataError,
)
"""The caller asked for something that cannot be answered as asked.

``ValueError`` is what an implementation raises for an unknown indicator or a symbol with
no stored book, and what ``build_params`` raises for a value that does not fit the schema.

The two DuckDB families are the ``query`` capability's, and they are named rather than
caught wholesale: DB-API says ``ProgrammingError`` is a statement problem (a missing table,
a syntax error, a bad column) and ``DataError`` a value problem, while ``OperationalError``
— ``IOException``, ``OutOfMemoryException`` — is the environment's. Only the first two are
the caller's. Before this, ``duckdb.CatalogException`` was neither ``CrocodileError`` nor
``ValueError``, so every user SQL typo answered 500: alerting fires, and every client that
backs off on 5xx retries a statement that will never compile.
"""


# ---------------------------------------------------------------------------
# Name resolution
# ---------------------------------------------------------------------------


def wire_names() -> dict[str, str]:
    """Every string the surfaces must answer to, mapped to the capability it names.

    Canonical names map to themselves; :attr:`Capability.aliases` map to their capability.
    Built fresh on each call rather than cached, because the registry is filled at import
    time by four batch modules and a cache would freeze whichever subset had loaded first.

    Raises:
        ValueError: two capabilities answer to one string. That is the original bug — two
            things behind one command — and it must not be resolvable by ordering, so it
            is refused rather than won by whichever declaration imported first.
    """
    load_all()
    names: dict[str, str] = {}
    for cap in REGISTRY.values():
        for spelling in (cap.name, *cap.aliases):
            existing = names.get(spelling)
            if existing is not None and existing != cap.name:
                raise ValueError(
                    f"{spelling!r} names both {existing!r} and {cap.name!r}; "
                    f"a capability has one name and an alias is a redirect to it"
                )
            names[spelling] = cap.name
    return names


def resolve(name: str) -> Capability:
    """Return the capability ``name`` refers to, following aliases.

    Raises:
        KeyError: no capability answers to that string.
    """
    resolved = wire_names().get(name)
    if resolved is None:
        raise KeyError(f"no capability named {name!r}")
    return REGISTRY[resolved]


# ---------------------------------------------------------------------------
# Asset class
# ---------------------------------------------------------------------------


def _sources_by_asset_class() -> dict[AssetClass, frozenset[str]]:
    """Which source names each market serves, from the two source registries.

    Imported here rather than at module scope: the equity provider factory pulls in five
    provider modules, and a surface that is only being asked to list its commands should
    not pay for that.
    """
    from crocodile.crypto.exchanges.factory import list_all_exchanges
    from crocodile.equity.providers.factory import list_providers

    return {
        AssetClass.CRYPTO: frozenset(list_all_exchanges()),
        AssetClass.EQUITY: frozenset(list_providers()),
    }


_SYMBOL_NAMES: Final = frozenset({"symbol", "symbols"})
_SYMBOL_SUFFIXES: Final = ("_symbol", "_symbols")


def symbol_field_names(params: type[msgspec.Struct]) -> frozenset[str]:
    """The fields of ``params`` that carry a canonical symbol, by name *and* by type.

    Derived rather than listed, which is the whole of the fix. This was a hand-written
    ``frozenset({"symbol", "symbols"})``, and a hand-written set of "fields that are
    symbols" rots on the next capability whose leg is spelled differently — as its own
    docstring recorded, having already been widened once, for ``catalog-scan``,
    ``resolve-symbols``, ``replay``, ``export``, ``backfill`` and ``collect``, all of which
    spell it ``symbols``. It then went stale a second time the moment ``basis`` and
    ``spot-future-basis`` grew equity halves: they name their legs ``spot_symbol``,
    ``perp_symbol`` and ``future_symbol``, so nothing was read off them, and two
    capabilities that had been resolving by "there is only one implementation" became
    unreachable on all three surfaces at once. The branch that gave them the second
    implementation does not touch this file, so nothing in its diff could have said so.

    Still named rather than sniffed, and this is the constraint the derivation has to keep.
    Reading *any* string containing a colon would let
    ``SELECT * FROM t WHERE note = 'binance:x'`` choose which implementation runs
    ``query`` — a request landing in a market because of a string literal. So the rule is a
    naming convention the registry already follows without exception: the field is
    ``symbol`` or ``symbols``, or it ends in ``_symbol`` or ``_symbols``.

    **And the declared type must admit a string**, which is not decoration.
    ``CollectMarketParams`` carries ``all_symbols: bool`` and ``max_symbols: int``; both
    end in ``_symbols`` and neither is a symbol. The runtime ``isinstance`` filter in
    :func:`symbol_hints` would have skipped them anyway, but the gate over this function
    asks a stronger question — *does this capability expose a symbol at all* — and a
    ``bool`` answering yes to it would exempt a capability from the very check that catches
    the next ``spot_symbol``.
    """
    found: set[str] = set()
    for field in msgspec.structs.fields(params):
        if field.name not in _SYMBOL_NAMES and not field.name.endswith(_SYMBOL_SUFFIXES):
            continue
        if _admits_str(field.type):
            found.add(field.name)
    return frozenset(found)


def _admits_str(annotation: Any) -> bool:
    """Whether ``annotation`` is ``str``, or a union or collection that can hold one."""
    if annotation is str:
        return True
    return any(_admits_str(arg) for arg in get_args(annotation))


def symbol_hints(params: Any) -> tuple[str, ...]:
    """Every canonical symbol a built request carries, for :func:`resolve_asset_class`.

    Read off the *built params struct* rather than off the raw request, which is what lets
    one rule cover three transports: by this point a sequence is a sequence, whether it
    arrived as a JSON list, a repeated flag or a comma-separated query parameter, and the
    surfaces no longer each need their own idea of how ``symbols`` is spelled.
    """
    fields = symbol_field_names(type(params))
    found: list[str] = []
    for field in msgspec.structs.fields(params):
        if field.name not in fields:
            continue
        value = getattr(params, field.name, None)
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, (list, tuple, set, frozenset)):
            found.extend(item for item in value if isinstance(item, str))
    return tuple(found)


def _equity_listing_mic(symbol: str) -> str | None:
    """The MIC a canonical ``LOCAL@MIC`` listing symbol names, when the MIC registry knows it.

    The rule is deliberately plain. Membership counts any status and segments, because spec
    3.11 asks only that the MIC be in the registry: an expired code still names the equity
    market, and the equity half then explains the redirect. The symbol is never upper-cased
    and :func:`parse_listing_symbol` is not called, since that strict parser would turn
    ``THYAO@XTUR`` into "cannot tell which market" and hide the better message.
    """
    from crocodile.core.markets.mic import MicRegistry

    local, sep, mic = symbol.rpartition("@")
    if sep and local and "@" not in local and mic in MicRegistry():
        return mic
    return None


def resolve_asset_class(
    cap: Capability,
    *,
    explicit: AssetClass | None = None,
    symbols: Sequence[str] = (),
) -> AssetClass:
    """Decide which of ``cap.impls`` serves this request.

    In order, and the order is the point — each step is a *stronger* claim than the next:

    1. What the caller said. An explicit choice is never overridden.
    2. What the symbols say. A canonical symbol is ``source:RAW``, and the source
       registries know which market each source serves, so ``deribit:BTC-PERPETUAL`` is
       evidence rather than a guess. A source both registries claim resolves to neither —
       ``alpaca`` is a crypto exchange *and* an equity provider — because an overlap is a
       real ambiguity and picking one silently is how a request lands in the wrong market's
       implementation and comes back with plausible numbers. Symbols that name *different*
       markets refuse for the same reason and more sharply: one request has one
       implementation, so resolving by position would send the equity symbols into the
       crypto one, which answers plausibly and empty. A canonical listing symbol ``LOCAL@MIC``
       (spec 3.4) whose MIC the ISO 10383 registry knows is equity evidence of the same
       strength: canonical listing symbols never contain ``:``, so the two rules cannot both
       fire on one symbol.
    3. Whether there is a choice at all. A capability with one implementation has nothing
       to decide. This step reads ``cap.impls`` and neither ledger, which is what keeps it
       correct as the ledgers move: today the single-implementation set is exactly the
       seven names on ``IRREDUCIBLE``, and while ``PENDING_SYMMETRY`` holds anything its
       entries look identical from here. ``tests/conformance/test_gates.py``'s
       ``test_gate2_every_capability_is_symmetric`` is what ties the two together — a
       one-implementation capability on neither ledger fails there, not here.

    Anything else refuses, naming the option that settles it. Defaulting to crypto here
    would make every unrecognised equity symbol quietly return an empty crypto answer.

    Raises:
        ValueError: the asset class cannot be established, or the symbols disagree about it.
        CapabilityUnavailable: an asset class was named explicitly and this capability does
            not implement it.
    """
    if explicit is not None:
        if explicit not in cap.impls:
            raise CapabilityUnavailable(
                cap.name,
                explicit.value,
                reason=f"implemented for {sorted(a.value for a in cap.impls)}",
            )
        return explicit

    by_asset_class = _sources_by_asset_class()
    claimed: set[AssetClass] = set()
    for symbol in symbols:
        if not symbol:
            continue
        if ":" in symbol:
            source = symbol.rsplit(":", 1)[0].strip().lower()
            matched = [
                asset_class
                for asset_class, sources in by_asset_class.items()
                if source in sources and asset_class in cap.impls
            ]
            if len(matched) == 1:
                claimed.add(matched[0])
        elif (
            "@" in symbol
            and AssetClass.EQUITY in cap.impls
            and _equity_listing_mic(symbol) is not None
        ):
            claimed.add(AssetClass.EQUITY)
    if len(claimed) == 1:
        return next(iter(claimed))
    if len(claimed) > 1:
        raise ValueError(
            f"{cap.name!r} was given symbols from two markets "
            f"({sorted(a.value for a in claimed)}): {list(symbols)}. One request is served "
            f"by one implementation, so split it rather than have one market answer for both"
        )

    if len(cap.impls) == 1:
        return next(iter(cap.impls))

    raise ValueError(
        f"cannot tell which market {cap.name!r} should serve"
        + (f" for symbols {list(symbols)}" if symbols else "")
        + f"; name it explicitly as one of {sorted(a.value for a in cap.impls)}"
    )


def asset_class_option_values(cap: Capability | None = None) -> list[str]:
    """The accepted spellings of an explicit asset class, for help text and schemas.

    Narrowed to what ``cap`` actually implements when one is given. A route or tool that
    publishes ``equity`` for a capability with no equity half is describing a request that
    can only ever answer 501, and the published schema is the only thing a generated client
    has to go on.
    """
    classes = AssetClass if cap is None else cap.impls
    return sorted(a.value for a in classes)


def requires_explicit_asset_class(cap: Capability) -> bool:
    """Whether a caller *must* name the asset class for this capability to be answerable.

    :func:`resolve_asset_class` resolves in three steps and only the first two can succeed
    without the caller saying anything: an explicit choice, or a canonical ``source:RAW``
    symbol the source registries can place, or a ``LOCAL@MIC`` listing symbol whose MIC the
    registry knows. A capability with two implementations and no
    symbol field has neither, so omitting the class is a hard refusal — and 31 of the 58 wire
    names are in exactly that position.

    Published rather than discovered. The MCP ``inputSchema`` did not mention ``asset_class``
    at all, and the REST document listed it ``required: false`` on all of them, so the one
    input that makes those calls answerable appeared nowhere a client could read it: a
    generated client's only route to the truth was a 400 at runtime.

    Read off :func:`symbol_field_names` rather than restated, so a capability that gains a
    symbol parameter stops being listed as requiring the option on the same day.

    This function and that one landed in the same phase from different branches, and the
    first draft here read a module-level ``_SYMBOL_FIELDS`` frozenset that the other branch
    had already replaced with the derived form. Git merged both without a conflict — one
    added a caller, the other removed a name — and the result was an ``F821`` in a file
    neither branch's own suite could fail on. Naming the function rather than a constant is
    what makes the dependency visible to the next merge.
    """
    if len(cap.impls) < 2:
        return False
    return not symbol_field_names(cap.params)


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


def params_schema(cap: Capability) -> dict[str, Any]:
    """Return ``cap.params`` as one inlined JSON Schema object.

    ``msgspec.json.schema`` returns a ``$ref`` into ``$defs``, which is correct JSON Schema
    and is what MCP publishes verbatim. A CLI building options and a REST server building
    query parameters both need the properties themselves, so the reference is followed here
    once instead of in two projectors.
    """
    schema = msgspec.json.schema(cap.params)
    ref = schema.get("$ref")
    if isinstance(ref, str):
        defs = schema.get("$defs", {})
        assert isinstance(defs, dict)
        return dict(defs[ref.rsplit("/", 1)[-1]])
    return dict(schema)


def build_params(cap: Capability, values: dict[str, Any]) -> Any:
    """Build ``cap.params`` from whatever the transport collected.

    ``strict=False`` because every transport here delivers strings: a query string has no
    integers, and a command line has no floats. Letting msgspec coerce against the declared
    types means the params struct is the single statement of what a parameter *is*, rather
    than each surface parsing to its own idea of it — which is how one capability came to
    accept ``period=14`` in three subtly different ways.

    ``None`` values are dropped rather than passed through, so a transport that represents
    "not supplied" as ``None`` gets the struct's own default instead of overwriting it with
    a null.

    A string arriving for a *sequence of scalars* is split on commas, because a command line
    and a query string have no lists either. Without this, fifteen of the registry's
    capabilities were unreachable from two of their three surfaces: ``--symbols BTCUSDT`` and
    ``?symbols=BTC`` both failed with ``Expected 'array', got 'str'``, which is a capability
    that is declared, projected, listed by Gate 4 and impossible to call. Comma separation is
    not invented here — it is what both forks' REST servers and both CLIs took, and what
    ``_PARAM_RENAMES`` in the surface-parity gate already documents for ``open-interest``.

    A string arriving for a *structured* field is JSON. Splitting ``[{"a": 1}, {"b": 2}]`` on
    commas produces ``['[{"a"', '1}', '{"b"', '2}]']``, which is the same unreachability one
    step further along: ``gas-vol``, ``mev-sandwich``, ``smart-money`` and ``label-transfers``
    take arrays of objects, and a transport that can only hand over text — a command line —
    has no other way to spell one. What those four take is a JSON document either way; this
    only says so where the transport lost the type.

    A name the capability does not declare is **refused**, not dropped. ``msgspec.convert``
    ignores what it does not recognise, so ``GET /api/v1/catalog-inventory?source=nope``
    answered 200 with the whole inventory: the field is spelled ``exchange``, ``source`` is
    the lake's own partition key and therefore the obvious guess, and a filter that is
    silently ignored narrows nothing and does not say so. The caller reads the entire lake
    as the answer to a question about one exchange. The refusal lists what the capability
    does accept, so a near miss is one line from being fixed rather than a hunt through a
    schema.

    Raises:
        ValueError: a name is not a parameter of this capability, a value does not fit the
            declared schema, a required one is missing, or a structured field was handed text
            that is not JSON. msgspec's own message names the field and the type, and is not
            reworded.
    """
    known = {field.name for field in msgspec.structs.fields(cap.params)}
    unknown = sorted(set(values) - known)
    if unknown:
        raise ValueError(
            f"{cap.name}: {', '.join(unknown)} "
            f"{'is not a parameter' if len(unknown) == 1 else 'are not parameters'} of this "
            f"capability; it takes {sorted(known)}"
        )
    sequences = _sequence_fields(cap.params)
    structured = structured_fields(cap)
    supplied = {
        key: _from_text(cap, key, value, sequence=key in sequences, structured=key in structured)
        for key, value in values.items()
        if value is not None
    }
    try:
        return msgspec.convert(supplied, type=cap.params, strict=False)
    except msgspec.ValidationError as exc:
        raise ValueError(f"{cap.name}: {exc}") from exc


def _from_text(
    cap: Capability, key: str, value: Any, *, sequence: bool, structured: bool
) -> Any:
    """Recover a value a text-only transport had to flatten. Anything else passes through."""
    if not isinstance(value, str):
        return value
    if structured:
        try:
            return msgspec.json.decode(value)
        except msgspec.DecodeError as exc:
            raise ValueError(
                f"{cap.name}: {key} takes a JSON document and {value!r} is not one: {exc}"
            ) from exc
    if sequence:
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


def _sequence_fields(params: type[msgspec.Struct]) -> frozenset[str]:
    """Field names whose declared type is a sequence rather than a scalar.

    Read off the annotation rather than guessed from the name: ``symbols`` is a list and
    ``symbol`` is not, but so is ``rates``, and ``sql`` is a string that would be ruined by
    a split on commas.
    """
    found: set[str] = set()
    for field in msgspec.structs.fields(params):
        for candidate in (field.type, *get_args(field.type)):
            origin = get_origin(candidate) or candidate
            if isinstance(origin, type) and issubclass(origin, (list, tuple, set, frozenset)):
                found.add(field.name)
    return frozenset(found)


def structured_fields(cap: Capability) -> frozenset[str]:
    """Field names whose declared type has no faithful spelling in a URL.

    A scalar is spellable, and so is a sequence of scalars: ``?symbols=BTC,ETH`` is a
    convention both forks already served. An array of *objects* is not — ``?trades=`` would
    have to carry an escaped JSON document through a length limit, an access log and a
    browser history — and neither is a mapping.

    This is the whole of what tells a projection that a capability wants a body. It is read
    off the parameter declaration because that is the only place the answer exists: a list of
    capability names in a projector would be the fourth copy of the registry this package
    exists to remove, and it would be wrong the first time a parameter changed type.
    """
    return frozenset(
        field.name
        for field in msgspec.structs.fields(cap.params)
        if not _url_expressible(field.type)
    )


def _url_expressible(annotation: Any) -> bool:
    """Whether a value of this type can be written in a query string or on a command line."""
    origin = get_origin(annotation)
    if origin is not None and origin in (list, tuple, set, frozenset):
        return all(_scalar(arg) for arg in get_args(annotation) if arg is not Ellipsis)
    if _is_union(annotation):
        return all(_url_expressible(arg) for arg in get_args(annotation) if arg is not NoneType)
    return _scalar(annotation)


def _scalar(annotation: Any) -> bool:
    """Whether this is a single value: a string, a number, a flag, or a literal among them.

    ``Literal`` counts because a ``Literal["error", "first"]`` is spelled as one of its
    strings, and a ``StrEnum`` counts because it *is* a ``str`` — which is what makes the
    check ``issubclass`` rather than an identity test against a tuple of types.
    """
    if get_origin(annotation) is Literal:
        return True
    if _is_union(annotation):
        return all(_scalar(arg) for arg in get_args(annotation) if arg is not NoneType)
    return isinstance(annotation, type) and issubclass(annotation, (str, int, float, bool))


def _is_union(annotation: Any) -> bool:
    """``X | None`` and ``Union[X, None]`` are the same thing and are spelled two ways."""
    return get_origin(annotation) in (Union, UnionType)


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------


def build_context(
    catalog: Catalog,
    asset_class: AssetClass,
    *,
    settings: Settings | None = None,
    readonly: bool,
    row_limit: int | None,
) -> CapabilityContext:
    """Assemble the context for one invocation.

    ``readonly`` and ``row_limit`` are keyword-only and have no defaults on purpose. They
    are the whole of a surface's trust policy, and a default would let a new surface
    inherit one silently — which is the accident that gave ``query`` three behaviours
    across six stacks. Every caller states its own.
    """
    return CapabilityContext(
        catalog=catalog,
        settings=settings if settings is not None else Settings.from_env(),
        asset_class=asset_class,
        readonly=readonly,
        row_limit=row_limit,
    )


def implementation(cap: Capability, ctx: CapabilityContext) -> Impl:
    """Return the implementation serving ``ctx.asset_class``.

    Raises:
        CapabilityUnavailable: this asset class has no implementation. Raised rather than
            returning an empty result, because "no answer" and "the answer is nothing" are
            different and the merge already lost seven capabilities to that confusion.
    """
    impl = cap.impls.get(ctx.asset_class)
    if impl is None:
        raise CapabilityUnavailable(
            cap.name,
            ctx.asset_class.value,
            reason=f"implemented for {sorted(a.value for a in cap.impls)}",
        )
    return impl


def invoke(cap: Capability, ctx: CapabilityContext, params: Any) -> Any:
    """Call the implementation. The entire dispatch, and there is nothing else to it.

    This function is deliberately three lines. Everything a surface might be tempted to do
    around a call — coercing arguments, choosing the market, attaching provenance — is a
    separate function here so that no projector grows a reason to special-case one
    capability on its way through.
    """
    return implementation(cap, ctx).fn(ctx, params)


def drive(result: Any, *, row_limit: int | None) -> Any:
    """Finish anything a capability handed back unstarted or unconsumed.

    Three return shapes in the registry are *work* rather than an answer, and all three are
    deliberate: an implementation cannot know whether its caller already owns an event loop,
    and ``asyncio.run`` from inside a running one raises — so ``backfill`` returns an
    unstarted coroutine, a ``STREAM`` returns an unstarted
    :class:`~crocodile.capabilities.ops.Subscription`, and ``replay`` returns a lazy iterator
    because the whole point of its k-way merge is to stay O(channels) in memory.

    This lives here rather than in one projector because the hazard is not one surface's.
    The first two were fixed inside the CLI, and that is exactly why the third shipped
    unnoticed on all three at once: ``replay`` printed ``<itertools.islice object at 0x…>``
    and exited **0** on the CLI, answered 500 on REST, and failed ``json.dumps`` on MCP.
    A zero exit code over work that never ran is the quietest possible failure, and a
    projection that answers it in one place answers it once.

    ``row_limit`` bounds the materialisation, because a lazy result is only safe on a network
    surface while somebody bounds it: ``replay`` reads through :meth:`Catalog.scan` rather
    than :meth:`CapabilityContext.query`, so the ``LIMIT`` wrapper that caps raw SQL never
    sees it and draining the iterator is how one request materialises a lake. ``None`` — the
    CLI's posture, on the machine that owns the lake — drains it all.

    Both awaits go through :func:`~crocodile.core.capability.run_to_completion` rather than a
    bare ``asyncio.run``, and that is the same one-line bug this module documents twice
    elsewhere: ``asyncio.run`` inside a running event loop raises ``RuntimeError``, and MCP
    *is* one — ``operate.mcp`` opens a loop and ``stdio.serve_stdio`` calls ``handle_request``
    inline on it, with no worker thread between. The REST projection is safe only because its
    endpoint hands the work to ``run_in_threadpool``; nothing gave MCP the same protection,
    and nothing forbids a read-only capability returning a coroutine. It was latent rather
    than harmless: the four implementations that reach here — ``backfill`` twice, ``collect``,
    ``collect-market`` — are all writes, so ``_refuse_readonly`` fires first on that surface.
    A bug that only one refusal stands in front of is a bug with a schedule.
    """
    begin = getattr(result, "run", None)
    if callable(begin) and not isinstance(result, type):
        return run_to_completion(begin)
    if inspect.iscoroutine(result):
        pending = result
        return run_to_completion(lambda: pending)
    if isinstance(result, Iterator):
        # ``Iterator`` and not ``Iterable``: a str, a dict, a list and a polars frame are all
        # iterable and none of them is unconsumed work. What is being caught here is the
        # generator/islice family, which has a ``__next__`` and one shot at being read.
        return list(result) if row_limit is None else list(itertools.islice(result, row_limit))
    return result


def stream_summary(pending: Any) -> dict[str, Any] | None:
    """Describe an unstarted subscription, or ``None`` if this is not one.

    A ``STREAM`` run returns ``None`` when it finishes — there is no last element to report —
    so a surface that renders only the return value prints ``None`` after an hour of
    collection and says nothing about what it collected. Everything worth reporting is known
    *before* the run starts, which is the whole reason a ``Subscription`` exists, so it is
    read off here while it still exists.

    Duck-typed rather than an ``isinstance`` against
    :class:`~crocodile.capabilities.ops.Subscription`: importing that module here would drag
    every connector and every analytics dependency into a surface that is often only being
    asked to list its commands, which is the cost ``dispatch`` defers everywhere else.
    """
    sources = getattr(pending, "sources", None)
    channels = getattr(pending, "channels", None)
    if not callable(getattr(pending, "run", None)) or sources is None or channels is None:
        return None
    return {
        "sources": list(sources),
        "channels": list(channels),
        "duration_seconds": getattr(pending, "duration_seconds", None),
    }


# ---------------------------------------------------------------------------
# The response envelope
# ---------------------------------------------------------------------------


def effective_provenance(impl: Impl, settings: Settings) -> tuple[Provenance, str]:
    """The level and basis this deployment will actually produce, not the best it could.

    :attr:`Impl.prov` is a ceiling, and every announcement in this module used to publish it
    unconditionally. For the three implementations that reach the lake through
    ``select_depth_source`` that made the *default* deployment lie: with no Alpaca keys the
    switch returns the synthetic Yahoo ladder and stamps its records
    ``SYNTHETIC``/``yahoo_1m_vap``, while REST and MCP published ``DERIVED``/``alpaca_l1``
    and the CLI — where :func:`banner_for` suppresses ``DERIVED`` — printed nothing at all.
    A modelled answer arriving with an empty stderr is the failure :func:`warning_for` exists
    to end, on the one endpoint whose hand-written banner it was generalised from.

    Resolved here rather than in each of the three announcements, because they must agree:
    a payload that says ``synthetic`` under a terminal that said nothing is the same defect
    with an extra step. The predicate belongs to the implementation
    (:attr:`Fallback.reachable`) so that no surface has to know what a credential is.
    """
    fallback = impl.fallback
    if fallback is None or fallback.reachable(settings):
        return impl.prov, impl.basis
    return fallback.prov, fallback.basis


def provenance_block(cap: Capability, ctx: CapabilityContext) -> dict[str, Any]:
    """The block every network response carries, describing how the answer was obtained.

    Derived from the declaration rather than measured per call: the per-record truth is on
    each record's own provenance tail where it can be measured. Publishing this is still
    worth doing — it is what lets a caller see that an equity depth answer is modelled
    *before* reading a single row.

    What is published is the **effective** level and basis rather than
    :attr:`Impl.prov`'s ceiling, because a ceiling the deployment cannot reach is a method
    that never ran. ``prov_ceiling`` carries the declaration alongside it, and only when the
    two differ: an unconditional field would put a second provenance number in every
    response for the eighty-eight implementations that have one answer, and the caller who
    needs to know a key would upgrade this endpoint is exactly the caller reading a degraded
    answer.
    """
    impl = implementation(cap, ctx)
    prov, basis = effective_provenance(impl, ctx.settings)
    block: dict[str, Any] = {
        "capability": cap.name,
        # ``any`` for a capability whose answer does not depend on which implementation ran.
        # ``search?q=EQ&asset_class=crypto`` returned three ``stooq:`` equity symbols and
        # stamped ``"crypto"`` — a value the caller was *forced* to supply, that selected
        # nothing, echoed back as though it had. Same for ``catalog-symbols`` and
        # ``catalog-exchanges``, and for the ten other capabilities in that batch. Echoing an
        # input is not reporting a fact, and a caller filtering answers by this field would
        # have partitioned a cross-market answer by the argument they happened to send.
        "asset_class": "any" if cap.cross_market else ctx.asset_class.value,
        "prov": prov.value,
        "prov_basis": basis,
        "method": describe(basis),
    }
    if basis != impl.basis or prov is not impl.prov:
        block["prov_ceiling"] = impl.prov.value
        block["prov_ceiling_basis"] = impl.basis
    if ctx.row_limit is not None:
        # Published so a truncated answer reads as a stated ceiling rather than as the
        # whole lake. The legacy REST server wrapped a LIMIT and said nothing.
        #
        # It says "at most this many rows are in the answer", and until :func:`payload`
        # started enforcing it that was false for 30 of the 33 TABLE capabilities: only
        # ``query``, ``catalog-scan`` and ``replay`` reached a cap at all — the first two
        # through :meth:`CapabilityContext.query`'s LIMIT wrapper, the third through
        # :func:`drive`'s islice — and every other implementation built its own statement or
        # went through ``Catalog.scan``, so ``GET /api/v1/resample`` answered twelve thousand
        # rows over a published ceiling of ten thousand. The OpenAPI document repeated the
        # claim to every generated client.
        block["row_limit"] = ctx.row_limit
    return block


def warning_for(cap: Capability, ctx: CapabilityContext) -> str | None:
    """A human-readable warning when the answer is not a native observation.

    This generalises the banner the equity depth route already ships — "SYNTHETIC —
    relative volume-at-price from Yahoo 1m bars, not real resting liquidity" — which was
    written by hand for one endpoint and therefore existed for exactly one of the fifty-odd
    capabilities that can return modelled data. The text comes from the basis registration,
    so a new synthetic method is announced the moment it is registered rather than the
    moment someone remembers to write a banner for it.
    """
    impl = implementation(cap, ctx)
    prov, basis = effective_provenance(impl, ctx.settings)
    if prov is Provenance.NATIVE:
        return None
    return (
        f"{prov.value.upper()} — {cap.name} for {ctx.asset_class.value} is not a "
        f"venue-reported observation. Its inputs rest on {basis!r}: {_headline(basis)}"
    )


def banner_for(cap: Capability, ctx: CapabilityContext) -> str | None:
    """The subset of :func:`warning_for` that belongs on a terminal's error stream.

    Only when the answer was *modelled* — when the quantity was produced from a different
    data class than the one it reports, which is what :attr:`Provenance.SYNTHETIC` means. An
    equity depth ladder is invented from 1m bars and an operator has to be told before they
    read the numbers.

    ``DERIVED`` is deliberately not announced here, and the difference is what a banner is
    for. A derived answer is computed from native inputs, which is what an indicator *is*:
    the caller asked for ``--indicator rsi`` by name, so being told that an RSI was computed
    is not news. Printing it on every successful call made two things worse at once — a
    script asserting empty stderr broke on a command that worked, and an operator who sees
    the banner on every call stops reading the one that matters, which is the SYNTHETIC one
    arriving on the same channel.

    Nothing is lost on the surfaces that can carry it: :func:`warning_for` still announces
    every non-``NATIVE`` implementation in the payload, where it is a field a machine reader
    can consult rather than a line a human learns to skip.

    The level tested is the **effective** one, which is what made this channel carry nothing
    that reads a market. Four of the ninety-one implementations declare ``SYNTHETIC`` and all
    four are ``caller_supplied`` calculators, so before :func:`effective_provenance` the one
    banner this design exists to generalise — the equity depth ladder modelled from Yahoo 1m
    bars — was silent in the default deployment, which is the only deployment most operators
    have.
    """
    impl = implementation(cap, ctx)
    prov, _ = effective_provenance(impl, ctx.settings)
    if prov in (Provenance.NATIVE, Provenance.DERIVED):
        return None
    return warning_for(cap, ctx)


def _headline(basis: str) -> str:
    """The first line of a basis's description, which is what fits in a banner.

    ``describe`` returns the registration's full docstring, several paragraphs for some
    bases. The wording of the sentence around it is careful for a reason: ``prov`` describes
    what the implementation *hands back* and ``basis`` names what its *inputs* were, and
    those genuinely differ — ``indicators`` is DERIVED from inputs whose basis is ``native``.
    Running the two together would have the banner announce a computed answer as a
    venue-reported one.
    """
    described = describe(basis).strip()
    return described.splitlines()[0] if described else "(no description registered)"


def _jsonable(value: Any) -> Any:
    """Make one cell safe to serialise, on every surface's encoder rather than on one.

    JSON has no ``NaN`` and no ``Infinity``. Polars produces both from ordinary analytics —
    a Bollinger band over a constant window, a ratio with a zero denominator — and
    ``json.dumps`` emits them as bare ``NaN`` tokens that most clients reject as malformed.
    ``None`` is the honest encoding: the number does not exist. The rule itself is
    :func:`crocodile.core.util.json_safe.json_safe_float` rather than an ``isfinite`` written
    here. Both deleted REST servers and both deleted MCP servers re-exported that function
    precisely so there would be one answer, and a fourth copy in the projection would undo
    what the re-export was for.

    Narrowing only ``float`` was not enough, and the gap divided the surfaces exactly the way
    ``DepthProfile`` did. Every lake read carries a ``date`` cell — the partition column — and
    FastAPI's encoder knows what to do with one while ``json.dumps`` does not, so
    ``catalog-scan`` answered 200 on REST and raised ``Object of type date is not JSON
    serializable`` on MCP. A ``date``, a ``Decimal``, a ``UUID`` and a nested Struct are all
    values a capability may legitimately return; which of them a given transport happens to
    understand is not something a caller should have to know.

    ``msgspec.to_builtins`` supplies the conversion for anything that is not already a JSON
    primitive, and the walk continues into the result so a non-finite float *inside* a
    converted structure is still caught. Sequence types are preserved rather than flattened
    to lists: both encoders write a tuple as an array, so changing it would only churn the
    shape a caller sees.
    """
    if isinstance(value, float):
        return json_safe_float(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        converted = [_jsonable(item) for item in value]
        return tuple(converted) if isinstance(value, tuple) else converted
    builtin = msgspec.to_builtins(value)
    # ``to_builtins`` returns a str for a date and a dict for a Struct, so one more pass
    # finishes the walk. The type check is what makes that pass terminate rather than
    # recur forever on a value msgspec handed straight back.
    return _jsonable(builtin) if type(builtin) is not type(value) else builtin


def payload(cap: Capability, result: Any, *, row_limit: int | None = None) -> dict[str, Any]:
    """Shape a return value according to :attr:`Capability.returns`, under this surface's cap.

    A ``TABLE`` becomes ``rows``, a ``SCALAR`` becomes ``result``. The distinction is read
    off the declaration rather than off the value's Python type, which is what stops a
    one-row frame from being served as a table by one surface and as an object by another —
    ``slippage`` returns exactly that and is declared ``SCALAR``.

    Whatever comes out is plain JSON data all the way down, because the three surfaces do not
    share an encoder and the one thing they must share is what they are asked to encode.

    ``row_limit`` is where the network surfaces' published ceiling is *made true*, and the
    choice of this function over the read is the whole of the argument. Two designs were on
    the table:

    *Push the cap into the read.* Strictly better as work: rows that are never returned are
    never computed. It requires every implementation to reach the lake through
    :meth:`CapabilityContext.query <crocodile.core.capability.CapabilityContext.query>`, and
    30 of the 33 ``TABLE`` capabilities do not — ``open-interest`` hands a catalog to an
    aggregator that owns its statement, ``resample`` and ``replay`` go through
    ``Catalog.scan``, ``depth`` walks a network ladder, ``ofi`` computes over a frame. Doing
    it would mean editing 30 implementations across four batch modules, each edit a chance
    for one capability's numbers to change quietly, and — this is the part that decides it —
    it would leave the property as **30 separate claims**, each of which can be forgotten
    again by the next implementation. Being forgotten 30 times is how the defect arrived.

    *Cap every answer where every answer passes.* This. One place, one gate, and the claim
    ``provenance.row_limit`` makes is enforced by construction rather than by 30 authors
    remembering. The cost is stated rather than hidden: the frame is computed in full and
    then cut, so the ceiling is a bound on the *response*, not on the work. Bounding the work
    is what an implementation's own ``limit`` parameter is for — ``catalog-scan``, ``replay``
    and ``export`` each take one — and pushing the cap deeper later is a pure optimisation
    that cannot change what the published number means, because the number already means what
    it says.

    ``truncated`` is added only when the cap actually bound. Without it a caller who receives
    exactly ``row_limit`` rows cannot tell a full answer of that size from a cut one, which is
    the ambiguity the published ceiling exists to remove.
    """
    truncated = False
    if cap.returns is ReturnKind.TABLE:
        result, truncated = _under_ceiling(result, row_limit)
    rows = _rows(result)
    if cap.returns is ReturnKind.TABLE:
        body: dict[str, Any] = {"rows": rows if rows is not None else _encodable(cap, result)}
        if truncated:
            body["truncated"] = True
        return body
    if rows is not None:
        return {"result": rows[0] if rows else None}
    return {"result": _encodable(cap, result)}


def _under_ceiling(result: Any, row_limit: int | None) -> tuple[Any, bool]:
    """Cut ``result`` to ``row_limit`` rows, and say whether that changed anything.

    Cutting the *frame* rather than the converted dicts, because ``_rows`` walks every cell
    of every row through :func:`_jsonable` and the rows past the ceiling are being thrown
    away: a 12 000-row answer under a 10 000 ceiling would otherwise pay for 12 000
    conversions to publish 10 000. ``head`` is polars' own slice and copies no data.

    A list is handled beside the frame because a ``TABLE`` implementation is allowed to return
    one — nothing in the registry declares that it returns a frame — and a ceiling that only
    holds for the common representation is a ceiling with an undocumented exception in it.
    """
    if row_limit is None:
        return result, False
    head = getattr(result, "head", None)
    height = getattr(result, "height", None)
    if callable(head) and isinstance(height, int):
        return (head(row_limit), True) if height > row_limit else (result, False)
    if isinstance(result, list) and len(result) > row_limit:
        return result[:row_limit], True
    return result, False


def _encodable(cap: Capability, result: Any) -> Any:
    """Render a result as plain data, or say loudly that it cannot be.

    ``depth`` returns a ``DepthProfile``, which is a Struct and is this codebase's wire type
    — but only for *msgspec's* encoder. FastAPI serialises with pydantic, which refuses an
    unknown type, so the route answered 500 with ``Unable to serialize unknown type:
    DepthProfile`` while the CLI printed it happily. A capability that works on one surface
    and 500s on another is the divergence this projection exists to end, so the conversion
    is here — once, for every surface — rather than in the REST handler.

    ``to_builtins`` and not ``json.encode``: the result has to stay a Python object for the
    CLI to render and for MCP to embed, and it is msgspec's own recursive walk, so a Struct
    nested inside a dict or a list is converted too.

    The failure is **raised**. Swallowing it and handing the object back is what let a lazy
    ``replay`` reach three different callers as three different symptoms — an ``islice`` repr
    on stdout under exit 0, a 500 with no detail, and a JSON-RPC internal error — none of
    which names the capability or the type. Anything unstarted is :func:`drive`'s to finish
    before it gets here, so reaching this branch means the projection has a bug, and a bug
    says so where it happens.

    Raises:
        TypeError: the result is not encodable by any surface.
    """
    try:
        return _jsonable(msgspec.to_builtins(result))
    except (TypeError, NotImplementedError) as exc:
        raise TypeError(
            f"{cap.name} returned a {type(result).__name__}, which no surface can encode; "
            f"a capability returns data, and work handed back unstarted must be driven to "
            f"completion before it reaches the envelope"
        ) from exc


def _rows(result: Any) -> list[dict[str, Any]] | None:
    """Return ``result`` as JSON-safe row dicts, or ``None`` if it is not a frame."""
    to_dicts = getattr(result, "to_dicts", None)
    if to_dicts is None:
        return None
    frame: pl.DataFrame = result
    if frame.width == 0:
        # The Catalog empty-result contract: zero rows *and* zero columns.
        return []
    return [{key: _jsonable(value) for key, value in row.items()} for row in to_dicts()]


def data_dir_for(settings: Settings, override: Path | None) -> Path:
    """Where the lake is: what the caller said, else what the environment said."""
    return override if override is not None else settings.data_dir


#: The listing-master readers. They answer from the lake a surface was given, as ``universe-sync``
#: and ``schedule`` write to it. Every other capability keeps CROCODILE_DATA_DIR as its lake root
#: (README: ``collect`` and ``backfill`` always write there).
LISTING_READERS: Final = frozenset({"universe", "markets"})


def surface_settings(cap: Capability, settings: Settings, override: Path | None) -> Settings:
    """The settings a surface hands ``cap``: its ``--data-dir`` is the lake root for the readers."""
    if override is None or cap.name not in LISTING_READERS:
        return settings
    return msgspec.structs.replace(settings, data_dir=override)
