"""Gate: ``crocodile.core.markets`` stays below the market and surface layers.

``core/markets`` is market-neutral foundation. It must not import the crypto or
equity layers, the capability, surface, or contrib layers. Two checks enforce it:
an AST scan of every module, and a fresh interpreter that imports every submodule
and inspects ``sys.modules``. The subprocess is needed because this pytest process
has already imported crypto and equity.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"
_PKG_DIR = _SRC / "crocodile" / "core" / "markets"
_FORBIDDEN = (
    "crocodile.crypto",
    "crocodile.equity",
    "crocodile.capabilities",
    "crocodile.surfaces",
    "crocodile.contrib",
)


def _scanned_files() -> list[Path]:
    return sorted(_PKG_DIR.rglob("*.py"))


def _is_forbidden(module: str) -> bool:
    return any(module == prefix or module.startswith(prefix + ".") for prefix in _FORBIDDEN)


def _forbidden_imports(source: str, module: str, *, is_package: bool = False) -> list[str]:
    """Return the imports in ``source`` that resolve into a forbidden package.

    ``module`` is the dotted name of the module the source belongs to. Relative
    imports are resolved against its package the way Python resolves them.
    """
    package = module if is_package else module.rpartition(".")[0]
    flagged: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            flagged.extend(alias.name for alias in node.names if _is_forbidden(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                target = importlib.util.resolve_name(
                    "." * node.level + (node.module or ""), package
                )
            else:
                target = node.module or ""
            if _is_forbidden(target):
                flagged.append(target)
    return flagged


def _module_name(path: Path) -> tuple[str, bool]:
    relative = path.relative_to(_SRC).with_suffix("")
    parts = relative.parts
    if parts[-1] == "__init__":
        return ".".join(parts[:-1]), True
    return ".".join(parts), False


def test_the_layering_scan_is_anchored_where_this_test_thinks_it_is() -> None:
    names = {path.name for path in _scanned_files()}
    for expected in ("errors.py", "mic.py", "exchanges.py", "currency.py"):
        assert expected in names, expected


def test_core_markets_imports_nothing_from_the_market_or_surface_layers() -> None:
    violations: list[str] = []
    for path in _scanned_files():
        module, is_package = _module_name(path)
        source = path.read_text(encoding="utf-8")
        for target in _forbidden_imports(source, module, is_package=is_package):
            violations.append(f"{path.relative_to(_REPO).as_posix()} imports {target}")
    assert violations == []


def test_importing_core_markets_loads_no_forbidden_package() -> None:
    code = (
        "import importlib, pkgutil, sys\n"
        "import crocodile.core.markets as pkg\n"
        "for info in pkgutil.walk_packages(pkg.__path__, 'crocodile.core.markets.'):\n"
        "    importlib.import_module(info.name)\n"
        f"forbidden = {_FORBIDDEN!r}\n"
        "loaded = sorted(\n"
        "    name for name in sys.modules\n"
        "    if any(name == p or name.startswith(p + '.') for p in forbidden)\n"
        ")\n"
        "print('\\n'.join(loaded))\n"
    )
    existing = os.environ.get("PYTHONPATH", "")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(_SRC), existing]))}
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", result.stdout


@pytest.mark.parametrize(
    ("source", "module", "expected"),
    [
        ("import crocodile.equity.providers\n", "crocodile.core.markets.mic", True),
        ("from crocodile.surfaces import cli\n", "crocodile.core.markets.mic", True),
        ("from ...equity import x\n", "crocodile.core.markets.mic", True),
        ("from crocodile.core.markets import mic\n", "crocodile.core.markets.mic", False),
        # ``..equity`` from crocodile.core.markets resolves to crocodile.core.equity,
        # which is not a forbidden package, so it is a control and must not be flagged.
        ("from ..equity import x\n", "crocodile.core.markets.mic", False),
        ("from . import errors\n", "crocodile.core.markets.mic", False),
    ],
)
def test_the_scanner_flags_a_forbidden_import(source: str, module: str, expected: bool) -> None:
    flagged = _forbidden_imports(source, module)
    assert bool(flagged) is expected, flagged
