"""Source usage classes: what a source's terms let Crocodile do with its data.

Every source adapter declares exactly one class. The classes come from the research
ledger (``docs/superpowers/research/crocodile-kuresel-borsa-arastirmasi-2026-11-08.md``,
lines 443-445):

- ``redistribution``: the source's terms explicitly allow passing the data to third
  parties, possibly with attribution (for example TWSE OpenAPI under OGDL v1, GLEIF
  CC0).
- ``display``: the data may be shown to the end user of a deployment, not passed on
  as data.
- ``internal``: internal analysis only, never served outward. Yahoo and TradingView
  are ``internal`` (spec section 3.10).

This module only defines the vocabulary and the merge rule. The check made when
Crocodile serves data outward belongs to a later task. ``most_restrictive`` is the
merge rule for derived or merged data: a value derived from an ``internal`` input
is ``internal``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Final

__all__ = ["RESTRICTIVENESS", "UsageClass", "most_restrictive"]


class UsageClass(StrEnum):
    """How a source's data may leave Crocodile, from most to least permissive."""

    REDISTRIBUTION = "redistribution"
    """Passing the data to third parties is allowed, possibly with attribution."""

    DISPLAY = "display"
    """Shown to the end user of a deployment; not passed on as data."""

    INTERNAL = "internal"
    """Internal analysis only; never served outward."""


RESTRICTIVENESS: Final[Mapping[UsageClass, int]] = {
    UsageClass.REDISTRIBUTION: 0,
    UsageClass.DISPLAY: 1,
    UsageClass.INTERNAL: 2,
}
"""Higher is more restrictive. The order decides :func:`most_restrictive`."""


def most_restrictive(classes: Iterable[UsageClass]) -> UsageClass:
    """Return the most restrictive class of ``classes``.

    A derived value inherits the strictest terms of every input, so one ``internal``
    input makes the whole value ``internal``.

    Raises:
        ValueError: ``classes`` is empty. An empty merge has no usage class to report,
            and inventing one would make a value look more shareable than it is.
    """
    ordered = list(classes)
    if not ordered:
        raise ValueError("most_restrictive needs at least one usage class")
    return max(ordered, key=RESTRICTIVENESS.__getitem__)
