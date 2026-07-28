"""No optional dependency may make `import crocodile.*` fail.

This gate exists because one did. `crocodile.crypto.gui` imported FlowMap's Python
package at module level; FlowMap deleted that package in its v1 to v2 cutover
(``e6cb3cc``, 2026-07-18), and from then on collecting the test suite ended in::

    ERROR tests/gui/test_flowmap_window.py
    !!!!! Interrupted: 1 error during collection !!!!!

Not one test failed — every test stopped running. That is the same shape as the seven
capabilities Phase 1 lost: a dependency that vanished, and no gate positioned to notice,
because every gate was asking whether something *worked* rather than whether something had
*stopped existing*.

An optional integration is allowed to be absent. It is not allowed to be absent *at import
time*: absence must surface where a caller asks for the feature, as a raised error that
names what is missing, which is the same rule the evasion extra follows.

The module that prompted this gate is gone. ``crocodile.crypto.gui`` was deleted with the
rest of the PyQt6 desktop layer, and with it
``test_the_flowmap_integration_degrades_instead_of_exploding``, which asserted that
``HAS_FLOWMAP`` existed and that ``dict_to_flowmap_objects`` raised ``ConfigError`` naming
what was missing. That test is not replaced, because there is nothing left to replace it
over: the rule it demonstrated is the one
:func:`test_every_module_imports` enforces across the whole tree, and the demonstration was
only ever a second copy of it aimed at the one module that had failed. The strongest form
of "an optional dependency may not break the import" is not needing the optional dependency
at all, which is what the deletion bought.
"""

from __future__ import annotations

import importlib
import pathlib

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "crocodile"


def _module_names() -> list[str]:
    names = []
    for path in sorted(_SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(_SRC.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.append(".".join(parts))
    return names


def test_the_module_scan_is_anchored_where_this_test_thinks_it_is() -> None:
    """Guard the guard: an empty scan would pass this file in silence."""
    assert _SRC.is_dir(), f"package tree not found at {_SRC}"
    assert len(_module_names()) > 150, "package scan is implausibly small; check the anchor"


@pytest.mark.parametrize("module", _module_names())
def test_every_module_imports(module: str) -> None:
    """Every module in the package imports on the base install.

    Parametrised rather than looped so a regression names the one module that broke
    instead of the first one the loop happened to reach.
    """
    try:
        # The name comes from walking this repository's own `src/crocodile` tree, which is
        # the only thing this gate can possibly be about; there is no external input here.
        importlib.import_module(module)  # nosemgrep
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"{module} cannot be imported: {exc}.\n"
            "An optional dependency must be reached lazily, or guarded so its absence "
            "raises ConfigError when the feature is used — never at import."
        )

