"""Provenance bases of the live layer.

Three ways a live record comes to exist, each registered once here so that
:func:`crocodile.core.schema.provenance.provenance_fields` is the only place a live record gets
its tail from. :mod:`crocodile.equity.universal.live.model` imports this module, so the bases are
registered before any snapshot is turned into records.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from crocodile.core.schema.provenance import (
    ConfidenceInputError,
    Provenance,
    register_basis,
)

__all__ = ["BASIS_POLLED_BOOK", "BASIS_POLLED_PRINT", "BASIS_VOLUME_DELTA"]

BASIS_POLLED_BOOK = "polled_book_snapshot"
BASIS_POLLED_PRINT = "polled_last_print"
BASIS_VOLUME_DELTA = "volume_delta_print"


@register_basis(BASIS_POLLED_BOOK, level=Provenance.NATIVE, inputs=[])
def _polled_book_snapshot(_: Mapping[str, Any]) -> float:
    """A publisher's resting ladder, read by polling: 1.0, by definition.

    Every price and size in the record is the publisher's own statement of the book at the
    instant it stamped, copied and not reconstructed, which is native's argument. Nothing is
    modelled and nothing is interpolated, so there is no measurement that varies between two
    polls of the same method and the formula is a constant.

    What polling costs is completeness between records, not the truth of a record: orders that
    rest and cancel between two polls are never seen, and the ladder is only as deep as the
    publisher exposes (``depth`` on the record says how many levels that was). The basis exists
    for its name, as ``listing_directory`` does: ``WHERE prov_basis = 'polled_book_snapshot'``
    separates these sampled ladders from a venue's continuous incremental feed, which ``native``
    alone could not. A delayed publisher is still native; its delay is carried on the live
    snapshot and in ``source_ts``, never hidden.
    """
    return 1.0


@register_basis(BASIS_POLLED_PRINT, level=Provenance.NATIVE, inputs=[])
def _polled_last_print(_: Mapping[str, Any]) -> float:
    """A print the publisher itself reported, read by polling: 1.0, by definition.

    The record is one real trade: the publisher stated its price and its size, either as a row
    of its own tape or as the "last trade" beside its quote. Both numbers are copied, so the
    record is fully sampled at its own level and the formula is a constant.

    This is not ``scraped_last_price``, which scores 0.0 because that page publishes no size and
    the record's quantity is therefore invented. Here the size is published, and a snapshot whose
    publisher states no size produces no trade at all. What polling costs is the tape's
    completeness: a "last trade" poll sees one print per poll and misses the ones between, which
    is why the basis has its own name. The aggressor side is ``unknown`` unless the publisher
    states it.
    """
    return 1.0


@register_basis(BASIS_VOLUME_DELTA, level=Provenance.DERIVED, inputs=["trade"])
def _volume_delta_print(inputs: Mapping[str, Any]) -> float:
    """Trading between two polls, reported as one print: ``min(1, cadence_s / gap_s)``.

    When a publisher states a last price and a cumulative session volume but no per-print size,
    the volume traded between two consecutive polls is the difference of the two cumulative
    figures. This method reports that difference as a single print at the later poll's last
    price. The size is exact in aggregate and the price is exact for the last print of the
    interval; what is lost is how many prints there were and at which prices the earlier ones
    traded, which is why the level is DERIVED and never NATIVE.

    The score is sampling adequacy at that level: a poll that arrives at the declared cadence
    scores 1.0, and one that arrives after a gap of ``gap_s`` seconds scores the fraction of the
    interval the declared cadence would have resolved. A two-minute outage on a ten-second
    cadence scores 1/12, because that single print stands for twelve polls' worth of trading.
    """
    try:
        cadence = float(inputs["cadence_s"])
        gap = float(inputs["gap_s"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfidenceInputError(
            "volume_delta_print needs numeric 'cadence_s' and 'gap_s' inputs"
        ) from exc
    if not (math.isfinite(cadence) and math.isfinite(gap)) or not (cadence > 0.0 and gap > 0.0):
        raise ConfidenceInputError(
            f"'cadence_s' and 'gap_s' must be finite and positive, got {cadence!r} and {gap!r}"
        )
    return min(1.0, cadence / gap)
