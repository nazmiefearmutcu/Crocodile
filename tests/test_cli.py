"""The two CLI commands that are hand-written rather than projected.

Almost everything this file used to hold has gone. It was an acceptance suite for the crypto
Typer app — one test per ``catalog``/``catalog-stats``/``catalog-dates``/``search``/
``resolve-symbols``/``data-coverage``/``export``/``replay``/``indicators``/``query``
command — and every one of those names is now a capability that
:mod:`crocodile.surfaces.cli` projects with no per-command code. ``tests/conformance/
test_surfaces.py`` asserts each of them is reachable on all three surfaces exactly once and
``tests/surfaces/test_end_to_end.py`` drives one of them end to end against a real lake, so
a command-by-command copy here would be pinning a loop forty-seven times.

The interactive wizards went with them: ``select_symbols_interactively``,
``prompt_time_range_helper`` and ``resolve_input_symbols`` were helpers of that Typer module
and have no successor — a required option replaces a prompt.

What is left is the two commands in :mod:`crocodile.surfaces.operate` that these tests were
the only cover for. They are hand-written because they are not capabilities: ``migrate-lake``
renames directories without opening a Parquet file and ``shell`` is a REPL over the command
table, so neither has an asset class, a parameter schema or an honest provenance.
"""

from __future__ import annotations

import pathlib

from typer.testing import CliRunner

from crocodile.surfaces import entrypoint

# ---------------------------------------------------------------------------
# migrate-lake
# ---------------------------------------------------------------------------


def test_cli_migrate_lake_renames_then_reports_already_migrated(
    tmp_path: pathlib.Path,
) -> None:
    """The command the legacy-partition warning names must exist and be re-runnable.

    It now covers both retired prefixes from one command: ``exchange=`` was the crypto
    fork's and ``provider=`` the equity fork's, and only the crypto CLI ever carried a
    migration.
    """
    (tmp_path / "exchange=deribit" / "channel=trade").mkdir(parents=True)
    (tmp_path / "provider=yahoo" / "channel=bar").mkdir(parents=True)

    runner = CliRunner()
    first = runner.invoke(entrypoint.build_app(), ["migrate-lake", "--data-dir", str(tmp_path)])
    assert first.exit_code == 0, f"stdout:\n{first.output}"
    assert "Renamed 2" in first.output
    assert (tmp_path / "source=deribit" / "channel=trade").is_dir()
    assert (tmp_path / "source=yahoo" / "channel=bar").is_dir()

    second = runner.invoke(entrypoint.build_app(), ["migrate-lake", "--data-dir", str(tmp_path)])
    assert second.exit_code == 0, f"stdout:\n{second.output}"
    assert "Already migrated." in second.output


def test_cli_migrate_lake_refuses_to_merge_partitions(tmp_path: pathlib.Path) -> None:
    """A collision exits non-zero rather than combining two sources silently."""
    (tmp_path / "exchange=deribit").mkdir(parents=True)
    (tmp_path / "source=deribit").mkdir(parents=True)

    result = CliRunner().invoke(
        entrypoint.build_app(), ["migrate-lake", "--data-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert (tmp_path / "exchange=deribit").is_dir()


# ---------------------------------------------------------------------------
# shell
# ---------------------------------------------------------------------------


def test_cli_shell_lists_the_command_table_and_exits() -> None:
    """``shell`` runs the same app the console script does, and leaves on ``exit``.

    ``shell`` used to set ``CRYPCODILE_SHELL=1`` so that a ``flowmap`` started from inside
    it returned instead of blocking on its GUI child, and this test took a
    ``monkeypatch.setenv`` so an un-restored process environment could not carry that into
    every later test. The flag had exactly one reader, ``flowmap``, and both are gone with
    the desktop layer — so ``shell`` now mutates no environment at all and there is nothing
    to isolate.
    """
    result = CliRunner().invoke(entrypoint.build_app(), ["shell"], input="help\nexit\n")
    assert result.exit_code == 0, result.output
    assert "Crocodile interactive shell" in result.output
    # The REPL's command list is the app's own, so a projected command and a hand-written
    # one both appear; a second hand-kept list is what this stopped being.
    assert "query" in result.output
    assert "migrate-lake" in result.output
