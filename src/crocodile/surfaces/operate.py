"""The commands that operate the process, hand-written because they are not capabilities.

Everything in :mod:`crocodile.surfaces.cli` is generated from a capability declaration and
nothing here is. That is the whole distinction: a capability has an asset class, a parameter
schema and a provenance, and answers a question about a market. These five answer questions
about the *installation* — start a server, upgrade the package, rename a directory — so
there is nothing for the registry to hold and no honest ``prov`` to publish.
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

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from crocodile.core.config import Settings
from crocodile.surfaces import dispatch

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


def build_app() -> typer.Typer:
    """Return a Typer app holding only the hand-written operator commands."""
    app = typer.Typer(add_completion=False)

    app.command(name="mcp")(mcp)
    app.command(name="api")(api)
    app.command(name="update")(update)
    app.command(name="shell")(shell)
    app.command(name="migrate-lake")(migrate_lake_cmd)
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
