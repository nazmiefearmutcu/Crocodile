"""The CLI, projected: one Typer command per capability, options from its params struct.

Replaces 5 499 lines of hand-written crypto Typer commands and 1 100 of equity ones with a
loop. There is no per-capability branch anywhere below, and there must never be: a command
that needs special handling in this file is a capability whose declaration is wrong.

The CLI's trust posture is the permissive one. It runs on the machine that owns the lake,
under the operator's own account, so raw SQL is not vetted and results are not capped —
which is also the only posture that keeps working, since the crypto CLI legitimately
reaches ``Catalog.query`` with SQL that :func:`assert_readonly_sql` rejects (see its
docstring on ``get_available_option_underlyings``). That is a decision about *this
surface*, made once here, rather than an omission repeated at forty-eight call sites.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Annotated, Any, get_args

import msgspec
import typer

from crocodile.core.capability import REGISTRY, AssetClass, Capability, ReturnKind
from crocodile.core.config import Settings
from crocodile.core.errors import CrocodileError
from crocodile.core.store.catalog import Catalog
from crocodile.surfaces import dispatch

__all__ = ["POSITIONAL", "RESERVED_OPTIONS", "build_app", "command_names", "positional_field"]

RESERVED_OPTIONS: frozenset[str] = frozenset({"asset_class", "data_dir"})
"""Option names this surface adds itself, which a params field may therefore not use.

Not a style rule: a params field called ``data_dir`` would silently shadow the option that
chooses the lake, and the shadowing would be invisible in the declaration. Checked at build
time so the collision is a startup failure rather than a capability that reads the wrong
directory.
"""

_REPORTED: tuple[type[BaseException], ...] = (
    CrocodileError,
    *dispatch.BAD_REQUEST,
    *dispatch.REFUSED,
)
"""What this surface reports as a message and an exit code rather than as a traceback.

The three categories :mod:`~crocodile.surfaces.dispatch` classifies, plus every
``CrocodileError``, because on a command line there is no distinction to draw between them:
each one is a sentence for the operator and ``exit 1``. What is deliberately *not* here is
everything else — a bug in an implementation still prints its traceback, because that is
the one audience who can act on it.

``duckdb.CatalogException`` is why this is a named tuple rather than
``(CrocodileError, ValueError)``: a mistyped table name printed forty lines of traceback at
an operator who had simply spelled a table wrong.
"""

_SCALARS: tuple[type, ...] = (str, int, float, bool)


def _option_type(declared: Any) -> type:
    """Return the type Typer should parse a field as.

    Click has no notion of a union that is not ``Optional``, so ``size: float | str`` — the
    crypto sizing parameter, which accepts ``"305 USDT"`` as readily as ``305`` — cannot be
    expressed as one. Such a field is collected as text and handed back to
    :func:`dispatch.build_params`, where msgspec coerces it against the real declaration.
    That keeps the params struct the single statement of what a parameter is; a second
    opinion here is how a surface starts accepting something the others do not.
    """
    args = [arg for arg in get_args(declared) if arg is not type(None)]
    candidates = args or [declared]
    if len(candidates) == 1 and candidates[0] in _SCALARS:
        found: type = candidates[0]
        return found
    return str


def _option_decl(name: str, kind: type) -> str:
    """``period`` becomes ``--period``; ``start_ns`` becomes ``--start-ns``."""
    flag = f"--{name.replace('_', '-')}"
    return f"{flag}/--no-{name.replace('_', '-')}" if kind is bool else flag


def _option_default(declared: Any) -> Any:
    """Return a declared default in the spelling *this transport* would have collected it in.

    A default has to survive the same round trip a typed value does. :func:`_option_type`
    widens every non-scalar field to ``str`` because Click has no lists, so the value Click
    hands back for ``--symbols BTC,ETH`` is the text ``"BTC,ETH"`` and
    :func:`dispatch.build_params` splits it. A *default* went round a different way: it was
    handed to Click as the tuple ``()``, Click stringified it to ``"()"`` — Click's contract
    for a non-string default on a string parameter — and the splitter turned that into the
    one-element list ``['()']``.

    The damage was not a message. ``open-interest`` and ``census`` filter on that list, so
    every default invocation asked for the literal pattern ``()``, matched nothing, printed
    "No data found for the given parameters." and **exited 0** — the same request answering
    twelve thousand rows on REST. ``universe`` and ``collect-market`` parse it as an
    enumeration and were unreachable from this surface entirely, with ``'()' is not a valid
    Kind``. Four fields, one line, and the only visible symptom was an empty answer that
    reads exactly like an empty lake.

    So a sequence default is written the way a caller would type it, and an *empty* sequence
    is written as no value at all — which is what it means. ``build_params`` drops ``None``
    rather than passing it through, so the struct's own default is what reaches the
    implementation, and the declaration stays the single statement of what the parameter is.
    ``--help`` gains from it too: a non-empty default now shows ``binance,okx`` instead of
    ``('binance', 'okx')``, which is a value a reader can paste back.
    """
    if declared is msgspec.NODEFAULT:
        return None
    if isinstance(declared, (list, tuple, set, frozenset)):
        return ",".join(str(item) for item in declared) or None
    return declared


POSITIONAL = "_positional"
"""The synthesised name of the optional positional argument. Not a params field name.

Leading underscore because it must never collide with one: a struct field called
``_positional`` would silently take over the argument that fills a different field.
"""


def positional_field(cap: Capability) -> str | None:
    """The one parameter this capability may also take positionally, if there is one.

    A capability with **exactly one** required parameter, spellable as text, gets it as an
    optional positional argument as well as an option. That is how the legacy CLI took every
    one of them — ``crypcodile query "SELECT 1"``, ``crypcodile search BTC`` — and losing it
    was not a decision anyone made, it fell out of synthesising keyword-only options from a
    struct.

    One required parameter, or none. Two would make the order something a caller has to
    memorise, and ``crocodile indicators deribit:BTC-PERPETUAL 1700000000000000000
    1700003600000000000`` is a line nobody can read back. A structured parameter is excluded
    for the opposite reason: it is a document rather than a word, and a shell hands those
    over on a pipe, which is what :func:`_from_stdin` is for.
    """
    required = [field for field in msgspec.structs.fields(cap.params) if field.required]
    if len(required) != 1 or required[0].name in dispatch.structured_fields(cap):
        return None
    return required[0].name


def _parameters(cap: Capability) -> list[inspect.Parameter]:
    """Build the Typer signature for one capability: its params, then this surface's own.

    Typer reads a command's options off the function signature, so projecting a struct onto
    a command means synthesising one. The alternative — a hand-written function per
    capability — is the 5 499 lines this module replaces.
    """
    descriptions = dispatch.params_schema(cap).get("properties", {})
    parameters: list[inspect.Parameter] = []
    positional = positional_field(cap)
    if positional is not None:
        # First in the signature, because ``inspect.Signature`` refuses a positional
        # parameter after a keyword-only one. It defaults to ``None`` so that every existing
        # invocation — which passes the option — keeps working unchanged.
        parameters.append(
            inspect.Parameter(
                POSITIONAL,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=None,
                annotation=Annotated[
                    str | None,
                    typer.Argument(
                        metavar=positional.upper(),
                        help=f"{positional}, positionally. Also readable from stdin.",
                    ),
                ],
            )
        )
    for field in msgspec.structs.fields(cap.params):
        if field.name == POSITIONAL:
            raise ValueError(
                f"{cap.name}: params field {field.name!r} collides with the positional "
                f"argument this surface synthesises"
            )
        if field.name in RESERVED_OPTIONS:
            raise ValueError(
                f"{cap.name}: params field {field.name!r} collides with the "
                f"{_option_decl(field.name, str)} option this surface adds"
            )
        kind = _option_type(field.type)
        described = descriptions.get(field.name, {})
        help_text = described.get("description") or f"{field.name} ({kind.__name__})"
        option = typer.Option(_option_decl(field.name, kind), help=help_text)
        default: Any = inspect.Parameter.empty
        annotation: Any = Annotated[kind, option]
        if field.name == positional:
            # Required, but not required *as an option*: Click would reject the command line
            # before the runner ever sees the positional or the pipe that supplies it. The
            # requirement is still enforced — `build_params` refuses a struct with a missing
            # field, naming it, after all three ways of supplying it have been tried.
            default = None
            annotation = Annotated[kind | None, option]
        elif not field.required:
            default = _option_default(field.default)
            # A field with a real default keeps its declared type so ``--help`` shows the
            # value; one that defaults to None (or to a factory, or to an empty sequence,
            # which is this transport's way of spelling "no filter") becomes optional.
            if default is None:
                annotation = Annotated[kind | None, option]
        parameters.append(
            inspect.Parameter(
                field.name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=annotation,
            )
        )

    parameters.append(
        inspect.Parameter(
            "asset_class",
            inspect.Parameter.KEYWORD_ONLY,
            default=None,
            annotation=Annotated[
                AssetClass | None,
                typer.Option(
                    "--asset-class",
                    help="Which market to serve. Inferred from the symbol's source if omitted.",
                ),
            ],
        )
    )
    parameters.append(
        inspect.Parameter(
            "data_dir",
            inspect.Parameter.KEYWORD_ONLY,
            default=None,
            annotation=Annotated[
                Path | None,
                typer.Option("--data-dir", help="Lake root. Defaults to CROCODILE_DATA_DIR."),
            ],
        )
    )
    return parameters


def _runner(cap: Capability) -> Any:
    """Return the callable Typer will invoke for ``cap``, with a synthesised signature."""

    def run(**kwargs: Any) -> None:
        asset_class: AssetClass | None = kwargs.pop("asset_class", None)
        data_dir: Path | None = kwargs.pop("data_dir", None)
        _fill_positional(cap, kwargs)
        settings = Settings.from_env()
        try:
            params = dispatch.build_params(cap, kwargs)
            resolved = dispatch.resolve_asset_class(
                cap, explicit=asset_class, symbols=dispatch.symbol_hints(params)
            )
        except _REPORTED as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        with Catalog(dispatch.data_dir_for(settings, data_dir)) as catalog:
            ctx = dispatch.build_context(
                catalog,
                resolved,
                settings=dispatch.surface_settings(cap, settings, data_dir),
                # See the module docstring: local operator, own lake, no guard and no cap.
                readonly=False,
                row_limit=None,
            )
            banner = dispatch.banner_for(cap, ctx)
            if banner:
                typer.echo(banner, err=True)
            try:
                pending = dispatch.invoke(cap, ctx, params)
                # See the module docstring: local operator, own lake, so nothing is capped.
                result = dispatch.drive(pending, row_limit=None)
            except _REPORTED as exc:
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            typer.echo(_render(cap, result, pending))

    runner: Any = run
    runner.__name__ = cap.name.replace("-", "_")
    runner.__doc__ = cap.summary
    # Typer reads options from ``inspect.signature``; ``__annotations__`` is left empty
    # because ``get_type_hints`` is only consulted to resolve forward references, and every
    # annotation attached above is already a live object.
    runner.__signature__ = inspect.Signature(_parameters(cap), return_annotation=None)
    runner.__annotations__ = {}
    return runner


def _fill_positional(cap: Capability, kwargs: dict[str, Any]) -> None:
    """Resolve the one parameter this surface accepts three ways into the one it passes on.

    Precedence is option, then positional, then stdin, and it is that way round because it
    goes from most explicit to least: a caller who typed ``--sql`` said which value they
    meant, and a pipe is what is left when nothing was said at all.
    """
    positional = kwargs.pop(POSITIONAL, None)
    field = positional_field(cap)
    if field is None:
        return
    if kwargs.get(field) is None:
        kwargs[field] = positional
    if kwargs.get(field) is None:
        kwargs[field] = _from_stdin()


def _from_stdin() -> str | None:
    """A piped value, or ``None`` if there is no pipe and nothing on it.

    Guarded on ``isatty`` so an interactive ``crocodile query`` reports the missing argument
    instead of sitting silently waiting for a terminal that has nothing to send. A pipe is
    how a shell hands over a value too long to type, and ``echo "SELECT …" | crypcodile
    query`` is what the legacy CLI accepted; whitespace-only input is treated as nothing,
    because ``< /dev/null`` is not an argument.
    """
    import sys

    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        piped = sys.stdin.read().strip()
    except (AttributeError, ValueError, OSError):
        return None
    return piped or None


def _render(cap: Capability, result: Any, pending: Any = None) -> str:
    """Print a table as a table and everything else as JSON, per the declaration.

    ``pending`` is what the capability handed back before :func:`dispatch.drive` finished it,
    and it is only consulted for a ``STREAM``: a subscription run returns ``None``, so
    rendering the return value printed the word ``None`` after a collection run and reported
    neither what was collected nor for how long.

    JSON rather than ``str(...)`` for everything that is not a polars frame. A Python
    ``repr`` of a dict is single-quoted, which is neither JSON nor the bordered frame the
    legacy CLI printed, so it could be read by neither ``jq`` nor a person — and this is the
    only surface whose output is routinely piped into another program.
    """
    if cap.returns is ReturnKind.STREAM:
        return _stream_line(pending)
    shaped = dispatch.payload(cap, result)
    if "rows" in shaped:
        rows = shaped["rows"]
        if hasattr(result, "to_dicts"):
            return "No data found for the given parameters." if not rows else str(result)
        return json.dumps(rows, indent=2)
    return json.dumps(shaped["result"], indent=2)


def _stream_line(pending: Any) -> str:
    """What a finished subscription has to report, which is not its return value."""
    summary = dispatch.stream_summary(pending)
    if summary is None:
        return "Stream finished."
    bound = summary["duration_seconds"]
    return (
        f"Collected {', '.join(summary['channels']) or 'no channels'} from "
        f"{', '.join(summary['sources']) or 'no sources'} into the lake"
        + (f" for {bound}s." if bound is not None else " until cancelled.")
    )


def build_app() -> typer.Typer:
    """Return a Typer app holding one command per capability, plus one per alias.

    Aliases are separate commands rather than a lookup, because a Typer app *is* its
    command table: a retired spelling that is not a command does not tab-complete, does not
    appear in ``--help``, and does not run. Both spellings dispatch through the same
    capability, so there is still exactly one implementation behind them.
    """
    app = typer.Typer(
        add_completion=False,
        help="Crocodile — every capability, projected from one registry.",
        no_args_is_help=True,
    )
    for wire, name in sorted(dispatch.wire_names().items()):
        cap = REGISTRY[name]
        summary = cap.summary if wire == name else f"{cap.summary}  [alias of {name}]"
        app.command(name=wire, help=summary)(_runner(cap))
    return app


def command_names() -> set[str]:
    """The command names this projection actually registers.

    Read off the built app rather than off the registry, so the surface-parity gate is
    measuring the surface instead of re-deriving the answer it is meant to check.
    """
    return {
        command.name or (command.callback.__name__ if command.callback else "")
        for command in build_app().registered_commands
    }
