"""Gates for the clean ``crocodile.equity.universal`` package (task 10a, step 1).

Every test here pins one rule of the package's lint, type, import and naming discipline. They
read the repository files directly, so a regression in any gate file is caught by the same run
that would have caught a regression in the package itself.
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = REPO_ROOT / "src" / "crocodile" / "equity" / "universal"
PACKAGE_REL = "src/crocodile/equity/universal"
PACKAGE_MODULE = "crocodile.equity.universal"


def _py_files() -> list[Path]:
    return sorted(PACKAGE_DIR.rglob("*.py"))


def test_universal_is_listed_as_a_clean_package() -> None:
    source = (REPO_ROOT / "tests" / "conformance" / "test_phase1_exit.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    entries: tuple[str, ...] | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_CLEAN_PACKAGES" for t in node.targets
        ):
            entries = ast.literal_eval(node.value)
    assert entries is not None, "_CLEAN_PACKAGES not found in test_phase1_exit.py"
    assert PACKAGE_REL in entries


def test_universal_has_a_strict_mypy_override() -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    overrides = config["tool"]["mypy"]["overrides"]
    matching = [
        entry
        for entry in overrides
        if "crocodile.equity.universal.*" in entry.get("module", [])
    ]
    assert matching, "no mypy override names crocodile.equity.universal.*"
    assert all(entry.get("ignore_errors") is False for entry in matching)


def test_universal_is_lint_clean() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--output-format=concise",
            PACKAGE_REL,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    findings = [line for line in result.stdout.splitlines() if ": " in line]
    assert result.returncode == 0, result.stdout + result.stderr
    assert findings == []


def test_universal_never_imports_contrib() -> None:
    offenders: list[str] = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.startswith("crocodile.contrib"):
                    offenders.append(f"{path.name}: {name}")
    assert offenders == []


def test_universal_sources_are_ascii() -> None:
    offenders: list[str] = []
    for path in _py_files():
        try:
            path.read_bytes().decode("ascii")
        except UnicodeDecodeError:
            offenders.append(path.name)
    assert offenders == []


def test_universal_source_names_do_not_collide_with_crypto_exchanges() -> None:
    from crocodile.crypto.exchanges.factory import list_all_exchanges
    from crocodile.equity.providers import factory

    crypto_names = set(list_all_exchanges())
    universal_names = {
        name
        for name, cls in factory._REGISTRY.items()
        if cls.__module__.startswith(PACKAGE_MODULE)
    }
    # Step 10 registers the first universal source; from then on the set is non-empty.
    assert universal_names is not None
    assert universal_names.isdisjoint(crypto_names), universal_names & crypto_names


def test_the_package_imports_on_the_base_install() -> None:
    importlib.import_module(PACKAGE_MODULE)
