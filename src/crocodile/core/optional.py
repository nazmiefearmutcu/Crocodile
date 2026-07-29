"""Import a dependency the tree declares but an environment may not be able to have.

``tests/conformance/test_import_safety.py`` states the rule this module serves:
an optional integration is allowed to be absent, but it is not allowed to be
absent *at import time* — absence must surface where a caller asks for the
feature, as a raised error that names what is missing.

That rule turned out to have teeth beyond tidiness. ``pyarrow`` publishes no
``win_arm64`` wheel in **any** release, so on Windows-on-ARM it is not merely
optional, it is unobtainable. Imported at module scope by the Arrow writer it
took ``crocodile.equity.client`` down with it — and with that package,
``collect``, which has nothing to do with Arrow. One unreachable format made an
entire live-collection path unimportable on a whole platform.
"""

from __future__ import annotations

import importlib
from types import ModuleType

from crocodile.core.errors import ConfigError

__all__ = ["require"]


def require(module: str, *, feature: str, hint: str) -> ModuleType:
    """Import ``module``, or raise :class:`ConfigError` naming what is missing.

    Call this at the point of use, not at module scope — deferring the import is
    the whole point. ``feature`` names what the caller asked for and ``hint``
    says what to do instead, so the error is actionable rather than a bare
    ``ModuleNotFoundError`` from somewhere in the middle of a write.
    """
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        top = module.split(".")[0]
        raise ConfigError(f"{feature} needs {top!r}, which is not installed here. {hint}") from exc
