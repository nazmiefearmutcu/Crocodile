"""The commands that operate the process, hand-written because they are not capabilities.

Everything in :mod:`crocodile.surfaces.cli` is generated from a capability declaration and
nothing here is. That is the whole distinction: a capability has an asset class, a parameter
schema and a provenance, and answers a question about a market. These seven answer questions
about the *installation and its local state* — start a server, upgrade the package, rename a
directory, fill the listing master, collect on a schedule — so there is nothing for the registry
to hold and no honest ``prov`` to publish.

``universe-sync`` and ``schedule`` are operator commands for the reason ``migrate-lake`` is one
(spec 3.11, the ruling of task 15): they change local state (the listing master and the scheduler
state) or run for days, and neither is a question about a market. The cost is that REST and MCP
do not see them; either can be promoted later to a two-half capability.
:func:`crocodile.capabilities.ops._why_migrate_lake_is_infrastructure` and
:func:`~crocodile.capabilities.ops._why_gas_tracker_is_not_a_capability` are the long form of
that argument for the two names where it was contested.

There were seven, and two of them opened windows. ``flowmap`` and ``gas-tracker`` started a
``QApplication`` in a child process; the argument that kept them out of the registry — no
asset class, no parameter schema, no provenance, unreachable from REST or MCP — is the same
argument for why a headless data engine should not carry them at all, and it applies to the
PyQt6 dependency they were the only consumers of. FlowMap is a published product in its own
right and continues to be one; what left here is the launcher, not the visualizer.

Kept in a separate module from the projection on purpose. ``cli.build_app()`` is what Gate 4
measures against the registry, and a hand-written command inside it would make that gate
report an invented name every time. :func:`crocodile.surfaces.entrypoint.build_app` is where
the two lists become one command table.

What did *not* come across, and why, since these were the six commands' own helpers:

``resolve_data_dir`` walked ``test_data``, the repo root and ``~/Crypcodile/test_data``
looking for a lake with registered channels, and silently answered from whichever it found
when the one you asked for was empty. A command that reads a different lake than the one on
its command line and says so only in a warning is the shape of failure this merge exists to
end, and ``Settings`` is now the one resolver — see ``tests/conformance/
test_data_dir_resolution.py``, which asserts nothing outside ``core/config.py`` resolves a
lake at all.
"""

from __future__ import annotations

import datetime
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn

import typer

from crocodile.core.config import Settings
from crocodile.core.errors import CrocodileError
from crocodile.core.schema.enums import AssetClass
from crocodile.surfaces import dispatch

if TYPE_CHECKING:
    from crocodile.core.scheduler.state import SchedulerStateStore
    from crocodile.core.sink.base import Sink
    from crocodile.core.store.parquet_sink import ParquetSink
    from crocodile.equity.universal.orchestrator import (
        EodSchedule,
        RunReport,
        Scope,
        UniversalOrchestrator,
    )

__all__ = ["build_app", "is_interactive_stdin"]

_DataDirOpt = Annotated[
    Path | None,
    typer.Option("--data-dir", help="Lake root. Defaults to CROCODILE_DATA_DIR."),
]
"""The same option the projection adds to every command, spelled once for these too."""


def is_interactive_stdin() -> bool:
    """Whether a human is on the other end of stdin.

    The ``_mock_interactive`` escape hatch is load-bearing: a test that wants to drive the
    interactive branch cannot make a pipe into a tty, and patching ``sys.stdin.isatty``
    globally breaks pytest's own capture.
    """
    return sys.stdin.isatty() or bool(getattr(sys.stdin, "_mock_interactive", False))


def _lake(data_dir: Path | None) -> Path:
    return dispatch.data_dir_for(Settings.from_env(), data_dir)


_OPERATOR_ERRORS: tuple[type[Exception], ...] = (ValueError, PermissionError, CrocodileError)
"""What an operator can fix, printed as ``Error: ...`` with exit 1 rather than as a traceback.

``MarketError`` (the base of ``SanctionedMarketError`` and ``UnknownMarketError``) derives from
``CrocodileError``, so it is covered; a bug still raises its traceback.
"""


def build_app() -> typer.Typer:
    """Return a Typer app holding only the hand-written operator commands."""
    app = typer.Typer(add_completion=False)

    app.command(name="mcp")(mcp)
    app.command(name="api")(api)
    app.command(name="update")(update)
    app.command(name="shell")(shell)
    app.command(name="migrate-lake")(migrate_lake_cmd)
    app.command(name="universe-sync")(universe_sync_cmd)
    app.command(name="schedule")(schedule_cmd)
    return app


# ---------------------------------------------------------------------------
# Launchers
# ---------------------------------------------------------------------------


def mcp(data_dir: _DataDirOpt = None) -> None:
    """Start the Model Context Protocol server on stdio."""
    import asyncio

    from crocodile.surfaces.stdio import serve_stdio

    if sys.stdin.isatty():
        typer.echo("Warning: the MCP server speaks JSON-RPC on stdio.", err=True)
        typer.echo("It is meant to be started by an agent client, not typed into.", err=True)
        typer.echo("Press Ctrl-C to exit.", err=True)

    typer.echo("Starting the Crocodile MCP server on stdio...", err=True)
    try:
        asyncio.run(serve_stdio(data_dir=data_dir))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    typer.echo("Crocodile MCP server stopped.", err=True)


def api(
    port: Annotated[int, typer.Option("--port", help="Port to bind the API server to.")] = 8000,
    host: Annotated[str, typer.Option("--host", help="Host address to bind to.")] = "127.0.0.1",
    data_dir: _DataDirOpt = None,
) -> None:
    """Start the REST server: every capability, plus the operational routes."""
    try:
        import uvicorn
    except ImportError as exc:
        typer.echo(
            "Error: uvicorn is required to serve the REST API. "
            "Install with: pip install 'crocodile[web]'",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    from crocodile.surfaces.server import build_server

    typer.echo(f"Starting the Crocodile API server on http://{host}:{port}...", err=True)
    uvicorn.run(build_server(data_dir=data_dir), host=host, port=port, log_level="info")


# ---------------------------------------------------------------------------
# The installation itself
# ---------------------------------------------------------------------------


def migrate_lake_cmd(data_dir: _DataDirOpt = None) -> None:
    """Rename legacy ``exchange=`` / ``provider=`` partitions to ``source=``.

    Hand-written rather than declared for the reason
    :func:`crocodile.capabilities.ops._why_migrate_lake_is_infrastructure` gives at length:
    it renames directories without opening a Parquet file, so it has no asset class, no
    parameters beyond the lake root and no provenance that is not a false statement about a
    path that moved.

    It now covers both legacy prefixes from one command, which closes the asymmetry that
    argument ends on: the crypto CLI was the only surface that carried it, so an operator
    with a ``provider=`` lake had to install the crypto fork to migrate it.
    """
    from crocodile.core.store.migrate import migrate_lake

    try:
        renamed = migrate_lake(_lake(data_dir))
    except FileExistsError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if renamed:
        typer.echo(f"Renamed {renamed} partition director{'y' if renamed == 1 else 'ies'}.")
    else:
        typer.echo("Already migrated.")


# ---------------------------------------------------------------------------
# Universal equity collection: the listing master and the end-of-day schedule
# ---------------------------------------------------------------------------

_MicOpt = Annotated[str | None, typer.Option("--mic", help="Operating or segment MIC, e.g. XIST.")]
_CountryOpt = Annotated[
    str | None, typer.Option("--country", help="ISO 3166-1 alpha-2 code, e.g. TR.")
]
_AllOpt = Annotated[
    bool,
    typer.Option(
        "--all",
        help="Every exchange in exchanges.csv; sanctioned ones are skipped and reported.",
    ),
]


def universe_sync_cmd(
    mic: _MicOpt = None,
    country: _CountryOpt = None,
    all_markets: _AllOpt = False,
    data_dir: _DataDirOpt = None,
) -> None:
    """Sync the listing master from the official directories and TradingView for one scope."""
    import asyncio

    try:
        scope = _scope_from_flags(mic, country, all_markets)
        settings = _settings_for(data_dir)
        rows, report = asyncio.run(_sync_universe(settings, scope))
    except _OPERATOR_ERRORS as exc:
        _exit_with(exc)
    typer.echo(str(_universe_table(rows)))
    typer.echo(f"Run report: {report.report_path}")
    if not any(row["status"] == "ok" for row in rows):
        raise typer.Exit(code=1)


def schedule_cmd(
    asset_class: Annotated[
        AssetClass, typer.Option("--asset-class", help="Only equity has a scheduled pull.")
    ],
    mic: _MicOpt = None,
    country: _CountryOpt = None,
    all_markets: _AllOpt = False,
    once: Annotated[
        bool, typer.Option("--once", help="Run one scheduler tick, report each market, exit.")
    ] = False,
    tick_seconds: Annotated[
        float, typer.Option("--tick-seconds", help="Seconds between ticks.")
    ] = 60.0,
    data_dir: _DataDirOpt = None,
) -> None:
    """Collect each market's end-of-day session after its calendar close."""
    import asyncio

    try:
        if asset_class is not AssetClass.EQUITY:
            raise ValueError(
                "only equity has a scheduled pull; crypto collection is continuous (use collect)"
            )
        scope = _scope_from_flags(mic, country, all_markets)
        settings = _settings_for(data_dir)
        code = asyncio.run(_run_schedule(settings, scope, once=once, tick_seconds=tick_seconds))
    except KeyboardInterrupt:
        typer.echo("Scheduler stopped.")
        return
    except _OPERATOR_ERRORS as exc:
        _exit_with(exc)
    if code:
        raise typer.Exit(code=code)


def _now() -> datetime.datetime:
    """The operator clock. Tests replace it; the orchestrator and the scheduler both read it."""
    return datetime.datetime.now(datetime.UTC)


def _scope_from_flags(mic: str | None, country: str | None, all_markets: bool) -> Scope:
    from crocodile.equity.universal.orchestrator import Scope

    given = [
        flag
        for flag, present in (
            ("--mic", mic is not None),
            ("--country", country is not None),
            ("--all", all_markets),
        )
        if present
    ]
    if len(given) != 1:
        raise ValueError("name exactly one of --mic, --country or --all")
    return Scope(mic=mic, country=country, all_markets=all_markets)


def _settings_for(data_dir: Path | None) -> Settings:
    import msgspec

    return msgspec.structs.replace(Settings.from_env(), data_dir=_lake(data_dir))


def _open_sink(lake: Path) -> ParquetSink:
    from crocodile.core.store.parquet_sink import ParquetSink

    return ParquetSink(data_dir=lake, max_buffer_rows=10_000, flush_interval_seconds=5.0)


def _make_orchestrator(settings: Settings, sink: Sink) -> UniversalOrchestrator:
    from crocodile.equity.universal.orchestrator import open_orchestrator

    # The lambda looks _now up at call time, so a test that replaces it moves the orchestrator's
    # clock and the scheduler's tick together: the closure dates the session the tick dated.
    return open_orchestrator(settings, sink, clock=lambda: _now())


def _schedule_state_store(lake: Path) -> SchedulerStateStore:
    from crocodile.core.scheduler import JSONFileStateStore

    return JSONFileStateStore(str(lake / "state" / "schedule-equity.json"))


def _exit_with(exc: BaseException) -> NoReturn:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=1) from exc


def _universe_table(rows: list[dict[str, Any]]) -> Any:
    import polars as pl

    return pl.DataFrame(
        rows,
        schema={
            "mic": pl.String,
            "source": pl.String,
            "status": pl.String,
            "listings": pl.Int64,
            "reason": pl.String,
        },
    )


def _schedule_table(rows: list[dict[str, Any]]) -> Any:
    import polars as pl

    return pl.DataFrame(
        rows,
        schema={
            "mic": pl.String,
            "task": pl.String,
            "outcome": pl.String,
            "records": pl.Int64,
            "reason": pl.String,
        },
    )


async def _sync_universe(
    settings: Settings, scope: Scope
) -> tuple[list[dict[str, Any]], RunReport]:
    sink = _open_sink(settings.data_dir)
    orch: UniversalOrchestrator | None = None
    try:
        orch = _make_orchestrator(settings, sink)
        report = await orch.sync_universe(scope)
        return _market_rows(report), report
    finally:
        await sink.close()
        if orch is not None:
            await orch.aclose()


def _market_rows(report: RunReport) -> list[dict[str, Any]]:
    """One row per market-level outcome: such an outcome's subject is its MIC."""
    return [
        {
            "mic": outcome.mic,
            "source": outcome.source or "",
            "status": str(outcome.status),
            "listings": outcome.records,
            "reason": outcome.reason or "",
        }
        for outcome in report.outcomes
        if outcome.mic is not None and outcome.subject == outcome.mic
    ]


async def _run_schedule(
    settings: Settings, scope: Scope, *, once: bool, tick_seconds: float
) -> int:
    from crocodile.equity.universal.orchestrator import build_eod_schedule

    lake = settings.data_dir
    sink = _open_sink(lake)
    orch: UniversalOrchestrator | None = None
    try:
        orch = _make_orchestrator(settings, sink)
        schedule = build_eod_schedule(orch, scope, state_store=_schedule_state_store(lake))
        if not once:
            await schedule.coordinator.start(interval_seconds=tick_seconds)
            return 0
        await schedule.coordinator.check_and_run_once(current_time=_now())
        typer.echo(str(_schedule_table(_schedule_rows(schedule))))
        return 1 if schedule.failures else 0
    finally:
        await sink.close()
        if orch is not None:
            await orch.aclose()


def _schedule_rows(schedule: EodSchedule) -> list[dict[str, Any]]:
    """One row per market in scope: its task, what this tick did, and why it was skipped."""
    from crocodile.equity.universal.orchestrator import OutcomeStatus, eod_task_name

    rows: list[dict[str, Any]] = []
    for mic in sorted({*schedule.mics, *schedule.skipped}):
        if mic in schedule.skipped:
            rows.append(_schedule_row(mic, "", schedule.skipped[mic], 0, ""))
            continue
        task = eod_task_name(mic)
        report = schedule.reports.get(mic)
        if report is None:
            outcome = "failed" if mic in schedule.failures else "not_due"
            rows.append(_schedule_row(mic, task, outcome, 0, schedule.failures.get(mic, "")))
            continue
        outcomes = report.market_outcomes(mic)
        if any(item.status is OutcomeStatus.OK for item in outcomes):
            outcome = "ok"
        else:
            outcome = str(outcomes[0].status) if outcomes else "no_outcome"
        reason = next((item.reason for item in outcomes if item.reason), None) or ""
        records = sum(item.records for item in outcomes)
        rows.append(_schedule_row(mic, task, outcome, records, reason))
    return rows


def _schedule_row(mic: str, task: str, outcome: str, records: int, reason: str) -> dict[str, Any]:
    return {"mic": mic, "task": task, "outcome": outcome, "records": records, "reason": reason}


_REPOSITORY = "git+https://github.com/nazmiefearmutcu/Crypcodile.git"


def update(
    force: Annotated[
        bool, typer.Option("--force", help="Upgrade even when already up to date.")
    ] = False,
) -> None:
    """Upgrade this installation from the source repository."""
    import subprocess

    from crocodile import __version__

    typer.echo(f"Upgrading Crocodile (currently {__version__})...", err=True)
    if not force and os.environ.get("CROCODILE_NO_UPDATE"):
        typer.echo("CROCODILE_NO_UPDATE is set; refusing to upgrade.", err=True)
        raise typer.Exit(code=1)

    # `uv pip` only means anything inside a virtualenv it can see; outside one it would
    # install into a different environment than the running interpreter's.
    use_uv = bool(os.environ.get("VIRTUAL_ENV")) and _has_uv()
    command = (
        ["uv", "pip", "install", "--upgrade", _REPOSITORY]
        if use_uv
        else [sys.executable, "-m", "pip", "install", "--upgrade", _REPOSITORY]
    )
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        typer.echo("Failed to upgrade Crocodile.", err=True)
        if result.stderr:
            typer.echo(f"Details:\n{result.stderr}", err=True)
        raise typer.Exit(code=1)
    typer.echo("Upgrade complete.", err=True)


def _has_uv() -> bool:
    import subprocess

    try:
        subprocess.run(["uv", "--version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


# ---------------------------------------------------------------------------
# The REPL
# ---------------------------------------------------------------------------


def shell() -> None:
    """Start the interactive shell that dispatches every other command."""
    import shlex

    import click

    from crocodile import __version__
    from crocodile.surfaces import entrypoint

    typer.echo(f"Crocodile interactive shell (v{__version__}).")
    typer.echo("Type 'help' to list commands. Type 'exit' or 'quit' to leave.")

    # The command table comes from the same app the console script runs, so the REPL cannot
    # offer a command the CLI does not have — which is what a second hand-kept list would.
    group = typer.main.get_group(entrypoint.build_app())
    summaries = {
        name: (command.help or "").split("\n")[0].strip()
        for name, command in group.commands.items()
    }
    session = _prompt_session(summaries)

    while True:
        try:
            line = session().strip()
        except (KeyboardInterrupt, EOFError):
            typer.echo("\nGoodbye!")
            return
        if not line:
            continue
        if line.lower() in ("exit", "quit"):
            return
        if line.lower() == "shell":
            typer.echo("You are already in the Crocodile shell.")
            continue
        args = ["--help"] if line.lower() in ("help", "?", "-h") else shlex.split(line)
        try:
            group(args, standalone_mode=False)
        except click.exceptions.ClickException as exc:
            exc.show()
        except (click.exceptions.Exit, SystemExit):
            pass
        except Exception as exc:
            typer.echo(f"Error executing command: {exc}", err=True)


def _prompt_session(summaries: dict[str, str]) -> Callable[[], str]:
    """Return a zero-argument callable that reads one line.

    ``prompt_toolkit`` is only worth constructing when there is a terminal to complete
    into; under a pipe or under pytest it would try to negotiate with a device that is not
    there, so those cases read a line and nothing more.
    """
    if not is_interactive_stdin() or "pytest" in sys.modules:
        return lambda: input("crocodile> ")

    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.history import InMemoryHistory

    words = [*summaries, "exit", "quit", "help"]
    session: PromptSession[str] = PromptSession(
        history=InMemoryHistory(),
        auto_suggest=AutoSuggestFromHistory(),
        completer=WordCompleter(
            words=words,
            meta_dict={
                **summaries,
                "exit": "Leave the shell",
                "quit": "Leave the shell",
                "help": "List commands",
            },
            ignore_case=True,
        ),
        complete_while_typing=True,
    )
    return lambda: session.prompt("crocodile> ")
