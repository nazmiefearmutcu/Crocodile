"""Shared order-book sync primitives for multi-venue gap recovery.

``SyncResult``, :class:`OrderBookSync` and the buffer-filter helper are used by
:class:`~crocodile.core.ingest.gap_bridge.BookResyncBridge`.

:class:`OrderBookSync` is the generic ``U``/``u``/``pu`` implementation of
:class:`BookSyncMachine` — the update-id continuity rules Binance publishes and
that every venue speaking that dialect (including the equity providers) reuses
verbatim.  It used to live in ``exchanges/binance/book.py``; that module now
re-exports it, so both asset classes share one state machine rather than two
copies that can drift.

Venues whose continuity fields do *not* follow that dialect keep their own
implementation of :class:`BookSyncMachine` under ``exchanges/<venue>/book.py``:
OKX is wired via ``OkxOrderBookSync`` (``seqId`` / ``prevSeqId``) and
REST ``GET /market/books``.  Bybit is intentionally not wired yet — see
the deferral note on ``BybitConnector`` (REST ``u`` only aligns with
``orderbook.1000``, while the connector uses ``orderbook.50``; recovery
is re-snapshot, not Binance-style REST-anchored delta replay).
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from crocodile.core.schema.records import BookDelta

__all__ = [
    "BookSyncMachine",
    "OrderBookSync",
    "SyncResult",
    "filter_buffered_book_deltas",
    "keep_delta_after_snapshot",
]


class SyncResult(StrEnum):
    """Action returned by a venue order-book continuity state machine."""

    DROP = "drop"
    APPLY = "apply"
    RESYNC = "resync"


@runtime_checkable
class BookSyncMachine(Protocol):
    """Minimal interface required by :class:`~crocodile.core.ingest.gap_bridge.BookResyncBridge`.

    Implementations must expose ``_venue`` for post-snapshot buffer filtering
    (``"spot"`` / ``"futures"`` today; future venues map to one of those
    boundary rules or extend :func:`filter_buffered_book_deltas`).
    """

    _venue: str

    def set_snapshot(self, last_update_id: int) -> None:
        """Anchor continuity to a REST (or WS) snapshot update id."""
        ...

    def note_applied(self, u: int) -> None:
        """Record that a delta ending at update id *u* was applied after resync."""
        ...


class OrderBookSync:
    """Continuity state machine for a ``U``/``u``/``pu`` depth stream.

    Rules (Binance appendix §3.2 + §8; the equity providers speak the same
    dialect):

    Spot:
      - Drop buffered events where ``u <= lastUpdateId``.
      - First applied event: ``U <= lastUpdateId + 1`` AND ``u >= lastUpdateId + 1``.
      - Thereafter: drop ``u <= prev_u``; apply ``U <= prev_u + 1 <= u``.
      - Continuity break → RESYNC.

    Futures:
      - Drop buffered events where ``u < lastUpdateId``.
      - First applied event: ``U <= lastUpdateId`` AND ``u >= lastUpdateId``.
      - Thereafter: ``pu == prev_u``.
      - Continuity break → RESYNC.
    """

    def __init__(self, venue: Literal["spot", "futures"]) -> None:
        """Initialise for 'spot' or 'futures' venue."""
        if venue not in ("spot", "futures"):
            raise ValueError(f"venue must be 'spot' or 'futures', got {venue!r}")
        # Annotated ``str``, not the ``Literal``: ``BookSyncMachine`` declares a
        # mutable ``_venue: str``, and protocol attributes match invariantly, so a
        # narrower inferred type would stop this class satisfying its own Protocol.
        self._venue: str = venue  # "spot" or "futures"
        self._snapshot_id: int | None = None
        self._prev_u: int | None = None
        self._have_first: bool = False

    def set_snapshot(self, last_update_id: int) -> None:
        """Called once the REST snapshot has been fetched."""
        self._snapshot_id = last_update_id
        self._prev_u = None
        self._have_first = False

    def note_applied(self, u: int) -> None:
        """Record that a delta ending at update id *u* was applied.

        Used after :class:`~crocodile.core.ingest.gap_bridge.BookResyncBridge`
        replays buffered deltas past a REST snapshot so the next live event
        checks continuity against the last applied ``u`` rather than re-running
        first-event rules against the snapshot id.
        """
        self._have_first = True
        self._prev_u = u

    def feed(self, U: int, u: int, pu: int | None) -> SyncResult:
        """Process one depth diff event and return the action to take.

        Parameters
        ----------
        U:  First update id in this event.
        u:  Final update id in this event.
        pu: Previous final update id (futures only; None for spot).
        """
        if self._snapshot_id is None:
            # No snapshot yet — buffer (treat as DROP until snapshot arrives)
            return SyncResult.DROP

        sid = self._snapshot_id

        if not self._have_first:
            if self._venue == "spot":
                # Drop stale events
                if u <= sid:
                    return SyncResult.DROP
                # First valid: U <= sid+1 AND u >= sid+1
                if U <= sid + 1 and u >= sid + 1:
                    self._have_first = True
                    self._prev_u = u
                    return SyncResult.APPLY
                # Otherwise gap before first event -> resync
                return SyncResult.RESYNC
            else:
                # futures
                # Drop stale events: u < lastUpdateId
                if u < sid:
                    return SyncResult.DROP
                # First valid: U <= lastUpdateId AND u >= lastUpdateId
                if U <= sid and u >= sid:
                    self._have_first = True
                    self._prev_u = u
                    return SyncResult.APPLY
                return SyncResult.RESYNC
        else:
            # Subsequent events — check continuity.
            # Invariant: both branches that set _have_first=True also set _prev_u=u,
            # so _prev_u can never be None here.  If somehow it were, we would crash
            # with a confusing TypeError on `self._prev_u + 1`; raise explicitly instead.
            if self._prev_u is None:
                raise RuntimeError("invariant violated: _prev_u is None with _have_first=True")
            if self._venue == "spot":
                if u <= self._prev_u:
                    return SyncResult.DROP
                if U <= self._prev_u + 1 <= u:
                    self._prev_u = u
                    return SyncResult.APPLY
                return SyncResult.RESYNC
            else:
                # futures: pu must equal prev_u
                if pu == self._prev_u:
                    self._prev_u = u
                    return SyncResult.APPLY
                return SyncResult.RESYNC


def keep_delta_after_snapshot(
    delta: BookDelta,
    snap_seq: int | None,
    *,
    venue: str = "spot",
) -> bool:
    """Return True if *delta* should be applied after a snapshot at *snap_seq*.

    Venue rules (Binance today):
      - ``futures``: keep ``seq_id >= snap_seq`` (boundary inclusive)
      - all others (``spot``, and any future venue reusing spot rules):
        keep ``seq_id > snap_seq`` (boundary exclusive)

    When *snap_seq* or the delta's ``seq_id`` is ``None``, keep (best-effort
    path when the REST snapshot has no usable anchor).
    """
    if snap_seq is None or delta.seq_id is None:
        return True
    if venue == "futures":
        return delta.seq_id >= snap_seq
    return delta.seq_id > snap_seq


def filter_buffered_book_deltas(
    buffer: Sequence[BookDelta],
    snap_seq: int | None,
    *,
    venue: str = "spot",
) -> list[BookDelta]:
    """Keep buffered deltas that apply after a snapshot sequence anchor.

    See :func:`keep_delta_after_snapshot` for venue boundary rules.
    """
    return [delta for delta in buffer if keep_delta_after_snapshot(delta, snap_seq, venue=venue)]
