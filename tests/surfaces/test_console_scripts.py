"""The commands a user actually types, checked against the modules they name.

``crocodile`` is the one console script this refactor produces and nothing in the repository
exercised it. ``test_phase1_exit`` runs ``crypcodile`` and ``stockodile``, whose targets did
not change, so the entry that *did* change was the only one no gate touched — and in this
checkout it raises ``ModuleNotFoundError: crocodile.crypto.legacy.cli``, a tree the merge
deleted.

That particular breakage is stale environment state: the shim in ``.venv/bin`` was generated
before the cutover and only a reinstall rewrites it. The missing gate is not. A console
script is a two-part promise — a name in ``pyproject.toml`` and a module that answers to it —
and nothing was checking the second half for the primary command.

Everything below therefore reads the declaration and drives the module it names, which is
what a correct install would run. The one thing it deliberately does not do is invoke
``.venv/bin/crocodile``: that would be testing this working copy's installation rather than
this working copy's source, and it is shared with other agents.
"""

from __future__ import annotations

import importlib
import os
import pathlib
import subprocess
import sys
import tomllib

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"

PRIMARY = "crocodile"
"""The command this refactor produces. Both markets, every capability, one name."""


def _declared_scripts() -> dict[str, str]:
    with (_REPO / "pyproject.toml").open("rb") as handle:
        scripts = tomllib.load(handle)["project"]["scripts"]
    assert isinstance(scripts, dict)
    return scripts


def _resolve(target: str) -> object:
    """Import ``module:attribute`` the way a generated console script does.

    Raises:
        ModuleNotFoundError, AttributeError, ValueError: exactly what the shim would raise
            on the user's terminal, which is the point of resolving it the same way.
    """
    module_name, _, attribute = target.partition(":")
    if not module_name or not attribute:
        raise ValueError(f"{target!r} is not a module:attribute entry point")
    # The name comes from this repository's own `pyproject.toml`, which is the only thing
    # this gate can possibly be about; there is no external input here.
    return getattr(importlib.import_module(module_name), attribute)  # nosemgrep


def _child_environment() -> dict[str, str]:
    """Point a subprocess at *this* checkout.

    This venv's editable install names an absolute source directory belonging to a different
    working copy, so a bare interpreter started here imports someone else's ``crocodile``.
    A subprocess gate that does not pin this is green about a tree it never read — which is
    the anchor problem ``test_phase1_exit`` already documents for its own scan.
    """
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(_SRC)
    return environment


def test_the_subprocess_gate_reads_this_checkout() -> None:
    """Guard the guard, before anything below trusts a subprocess."""
    result = subprocess.run(
        [sys.executable, "-c", "import crocodile; print(crocodile.__file__)"],
        cwd=_REPO,
        env=_child_environment(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith(str(_SRC)), result.stdout


@pytest.mark.parametrize("command", sorted(_declared_scripts()))
def test_every_declared_console_script_names_something_that_exists(command: str) -> None:
    """A ``[project.scripts]`` entry is a promise that this import works on a terminal.

    Parametrised so a broken entry names itself instead of the first one the loop reached.
    """
    target = _declared_scripts()[command]
    assert callable(_resolve(target)), f"{command} = {target!r} is not callable"


def test_a_target_naming_a_deleted_module_is_refused() -> None:
    """The rejecting branch, driven rather than assumed.

    The measured failure was a script pointing at ``crocodile.crypto.legacy.cli``, which the
    merge deleted. A gate that has only ever seen working input has not been shown to reject
    anything.
    """
    with pytest.raises(ModuleNotFoundError):
        _resolve("crocodile.crypto.legacy.cli:main")
    with pytest.raises(AttributeError):
        _resolve("crocodile.surfaces.entrypoint:no_such_entry_point")
    with pytest.raises(ValueError, match="module:attribute"):
        _resolve("crocodile.surfaces.entrypoint")


def test_the_primary_command_is_the_projections_entry_point() -> None:
    """``crocodile`` is the only command. The ``crypcodile`` and ``stockodile`` aliases
    were removed in 0.4, so nothing else may be declared."""
    scripts = _declared_scripts()
    assert scripts[PRIMARY] == "crocodile.surfaces.entrypoint:main"
    assert set(scripts) == {PRIMARY}


def test_the_retired_commands_are_not_declared_or_packaged() -> None:
    """The pyproject entries and the wheel's package list both name nothing retired."""
    with (_REPO / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)
    wheel_packages = project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert wheel_packages == ["src/crocodile"]
    assert not {"crypcodile", "stockodile"} & set(_declared_scripts())
    assert not any("_deprecated_cli" in target for target in _declared_scripts().values())


@pytest.mark.parametrize("retired", ["crypcodile", "stockodile"])
def test_the_retired_import_names_do_not_resolve(retired: str) -> None:
    """``import crypcodile`` and ``import stockodile`` are ModuleNotFoundError in 0.4.

    Run in a subprocess with this checkout's ``src`` first on the path, so an editable install
    cannot supply a stale copy of the shim from another working tree.
    """
    result = subprocess.run(
        [sys.executable, "-c", f"import {retired}"],
        cwd=_REPO,
        env=_child_environment(),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "ModuleNotFoundError" in result.stderr, result.stderr


def test_the_primary_command_runs_and_lists_capabilities() -> None:
    """What a user gets when they type it: the whole point, and previously untested.

    Run as ``python -m`` rather than through ``.venv/bin/crocodile`` — see the module
    docstring — because the shim adds nothing this does not cover except the state of an
    install.
    """
    result = subprocess.run(
        [sys.executable, "-m", "crocodile.surfaces.entrypoint", "--help"],
        cwd=_REPO,
        env=_child_environment(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    for expected in ("query", "indicators", "replay", "api", "mcp"):
        assert expected in result.stdout, f"{expected} is missing from the command table"


def test_the_primary_command_answers_a_capability_end_to_end(tmp_path: pathlib.Path) -> None:
    """One capability, through the console script's own entry point, against a lake.

    ``--help`` proves the table builds. This proves the command it lists can be run, which
    is the half that was ``ModuleNotFoundError`` in this checkout.
    """
    result = subprocess.run(
        [sys.executable, "-m", "crocodile.surfaces.entrypoint", "query",
         "SELECT 1 AS one", "--asset-class", "crypto", "--data-dir", str(tmp_path)],
        cwd=_REPO,
        env=_child_environment(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "one" in result.stdout, result.stdout


def test_the_operator_commands_are_reachable_from_the_same_command() -> None:
    """`crocodile api` and `crocodile mcp` are hand-written and merged in at the entry point.

    They are not capabilities and Gate 4 must not see them, so nothing that checks the
    projection checks that they are still wired up.
    """
    from crocodile.surfaces import cli, entrypoint, operate

    names = {command.name for command in entrypoint.build_app().registered_commands}
    assert names == cli.command_names() | {
        command.name for command in operate.build_app().registered_commands
    }
    assert {"api", "mcp", "shell"} <= names
