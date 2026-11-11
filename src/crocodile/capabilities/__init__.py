"""Where capabilities are declared: one batch module per family, filled in parallel.

:mod:`crocodile.core.capability` holds the machinery; this package holds the list. The
split is not tidiness. Porting 47 capabilities off the six legacy surfaces is four agents'
work, and one declaration file would serialise them into a queue behind a single set of
merge conflicts. Four modules, four owners, one registry — and then ``onchain``, a fifth
module for the two capabilities the parity gate found after the batches closed, which is
what brought Phase 2's registry to the 49 names ``tests/conformance/test_gates.py`` pins as
``_REGISTRY_AS_SHIPPED``.

The families, and what belongs in each:

``catalog``
    The lake and what is in it: ``query``, ``search``, the ``catalog-*`` family,
    ``data-coverage``, ``resolve-symbols``.
``market``
    Market structure and reference data: ``markets``, ``universe``, ``census``,
    ``list-exchanges``, ``open-interest``, ``calendar``, and the instrument-level lookups.
``analytics``
    Anything computed from stored records: ``basis``, ``funding-apr``, ``iv-surface``,
    ``vol-skew``, ``term-structure``, ``slippage``, ``ofi``, ``indicators``.
``onchain``
    The two Base-mainnet pool readers, which are the one family that answers from a
    contract call rather than from the lake. A fifth module rather than a corner of
    ``market`` because it arrived after the four batches were closed, from the parity
    gate rather than from the port — see its docstring.
``ops``
    Things that move data rather than read it, plus the
    :data:`~crocodile.core.capability.IRREDUCIBLE` names: ``collect``, ``backfill``,
    ``replay``, ``export``.

``calendar`` joined ``market`` with the global-equities package (design 2026-11-08, section
3.11), the first capability declared after Phase 2. ``_ADDED_AFTER_PHASE_2`` pins it beside
Phase 2's names, so the registry now holds 50.

Membership is a judgement about the family, not a hard partition, and nothing enforces it
beyond review — what *is* enforced is that every module is on :data:`BATCHES` and that
:func:`load_all` fails loudly, which is what stops a capability from silently not existing.
"""

from __future__ import annotations

import importlib
from typing import Final

__all__ = ["BATCHES", "load_all"]

BATCHES: Final[tuple[str, ...]] = ("analytics", "catalog", "market", "onchain", "ops")
"""Every batch module, in load order.

A module missing from here is a module :func:`load_all` never imports, which is a block of
capabilities that silently does not exist — the same shape as the seven capabilities the
merge lost. ``tests/conformance/test_surfaces.py`` asserts this tuple against the modules
actually on disk, so adding a fifth file without adding it here fails rather than vanishes.
"""


def load_all() -> None:
    """Import every batch module, so every ``declare()`` has run.

    Deliberately without a ``try``. A batch module that raises on import is a capability
    list missing everything after it, and the failure mode that costs is not hypothetical:
    :func:`crocodile.core.schema.provenance.load_all_bases` walks this same package
    catching ``Exception`` into a ``RuntimeWarning``, so a registry assembled through
    *that* path can come back short with nothing but a warning to say so. This function is
    the path that refuses — a caller building a surface gets the ``ImportError`` where it
    happened, naming the module, rather than a projection that is quietly nine commands
    short.

    Idempotent, because :func:`importlib.import_module` is: a module body runs once per
    process however many times this is called.
    """
    for name in BATCHES:
        # The names come from this module's own constant, never from a caller.
        importlib.import_module(f"{__name__}.{name}")  # nosemgrep
