"""The live feed: walk a market's chain and answer with the deepest view a publisher gives.

:class:`LiveFeed` is what a consumer holds. Given a listing it asks the market's sources in the
order :func:`crocodile.equity.universal.live.registry.sources_for` declares, deepest first, and
returns the first snapshot that carries a price. It keeps one instance of each source, so every
listing of a run shares a source's HTTP session, and it keeps three small pieces of state:

- A cadence floor. A source is not asked about the same listing more often than its own
  ``min_interval_s``; a faster caller gets the previous answer back. Cost if wrong: a redraw
  loop would turn into a request loop against a publisher.
- Walls. A source that answers with a consent page, a bot challenge, a 401/403 or a geo block
  is not asked again for ``wall_cooldown_s``. The wall is recorded in :attr:`walls` and the
  chain moves on; nothing is retried around it.
- Faults. A source whose 2xx answers cannot be read is skipped for the poll, and after
  ``_FAULTS_BEFORE_COOLDOWN`` consecutive unreadable answers it rests for ``fault_cooldown_s``.

Ruling: the feed never merges two publishers into one snapshot. A snapshot has one source, and
its records carry that source. A consumer that wants the session figures of a deeper source's
listing asks for them separately (:meth:`LiveFeed.poll` with ``max_tier``), and knows which
number came from where.

:func:`volume_delta_trade` is the one derivation here: for a publisher that states a cumulative
volume and no print size, the trading between two polls as one DERIVED trade.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Final

from crocodile.core.markets.symbols import parse_listing_symbol
from crocodile.core.schema.enums import AssetClass, Side
from crocodile.core.schema.provenance import provenance_fields
from crocodile.core.schema.records import Trade
from crocodile.equity.providers.base import ProviderError
from crocodile.equity.universal.base import SourceUnservableError
from crocodile.equity.universal.http import Fetcher
from crocodile.equity.universal.live.base import LivePayloadError, LiveSource
from crocodile.equity.universal.live.model import (
    TIER_RANK,
    LiveListing,
    LiveSnapshot,
    LiveTier,
)
from crocodile.equity.universal.live.registry import sources_for
from crocodile.equity.universal.live.sources.tradingview import TradingViewLive
from crocodile.equity.universal.live.sources.yahoo_chart import MinuteBar, YahooChartLive

__all__ = ["LiveFeed", "SourceNote", "listing_of", "volume_delta_trade"]

log = logging.getLogger(__name__)

_NS_PER_SECOND: Final = 1_000_000_000
_FAULTS_BEFORE_COOLDOWN: Final = 3
US_LIVE_MIC: Final = "XNAS"
"""A bare US ticker carries no MIC; the live layer needs one to pick a chain. Every US chain
reads consolidated or aggregator data keyed by ticker alone, so one US MIC stands for all."""


def listing_of(symbol: str, *, isin: str | None = None) -> LiveListing:
    """A canonical symbol (``THYAO@XIST``, or a bare US ticker) as a live listing."""
    local, mic = parse_listing_symbol(symbol)
    return LiveListing(mic=mic if mic is not None else US_LIVE_MIC, local_symbol=local, isin=isin)


@dataclass(frozen=True, slots=True)
class SourceNote:
    """Why a source is resting: what it answered, and until when it is not asked again."""

    source: str
    kind: str
    """``wall`` (the reason is an ``UnservableReason`` value) or ``fault``."""

    reason: str
    detail: str
    until_ns: int


def volume_delta_trade(
    previous: LiveSnapshot, current: LiveSnapshot, *, cadence_s: float
) -> Trade | None:
    """The trading between two polls of one listing as one DERIVED trade, or ``None``.

    Built only when both snapshots come from the same source and state a cumulative volume,
    the volume grew, and the later one states a last price. A source that publishes its own
    tape or its last print's size is never derived from: the native record already exists.
    """
    if previous.source != current.source or previous.listing != current.listing:
        return None
    if current.prints or current.last_size is not None:
        return None
    if previous.volume is None or current.volume is None or current.last is None:
        return None
    grown = current.volume - previous.volume
    gap_s = (current.captured_ns - previous.captured_ns) / _NS_PER_SECOND
    if not grown > 0.0 or not gap_s > 0.0 or not current.last > 0.0 or not cadence_s > 0.0:
        return None
    tail = provenance_fields("volume_delta_print", {"cadence_s": cadence_s, "gap_s": gap_s})
    return Trade(
        source=current.source,
        symbol=current.listing.symbol,
        symbol_raw=current.symbol_raw,
        local_ts=current.captured_ns,
        asset_class=AssetClass.EQUITY,
        source_ts=current.source_ts_ns,
        prov=tail.prov,
        prov_basis=tail.prov_basis,
        prov_confidence=tail.prov_confidence,
        prov_inputs=tail.prov_inputs,
        id=f"vd-{current.captured_ns}",
        price=current.last,
        amount=grown,
        side=Side.UNKNOWN,
        currency=current.currency,
        price_scale=current.price_scale,
    )


class LiveFeed:
    """Polls the live chain of any allow-listed market. One instance serves many listings."""

    def __init__(
        self,
        *,
        fetch: Fetcher | None = None,
        allow: Collection[str] = (),
        clock_ns: Callable[[], int] | None = None,
        wall_cooldown_s: float = 900.0,
        fault_cooldown_s: float = 60.0,
    ) -> None:
        self._fetch = fetch
        self._allow = frozenset(allow)
        self._clock: Callable[[], int] = clock_ns if clock_ns is not None else time.time_ns
        self._wall_cooldown_ns = int(wall_cooldown_s * _NS_PER_SECOND)
        self._fault_cooldown_ns = int(fault_cooldown_s * _NS_PER_SECOND)
        self._instances: dict[str, LiveSource] = {}
        self._resting: dict[str, SourceNote] = {}
        self._faults: dict[str, int] = {}
        self._last: dict[tuple[str, LiveListing], tuple[int, LiveSnapshot | None]] = {}
        self._identified: dict[LiveListing, LiveListing] = {}

    @property
    def walls(self) -> tuple[SourceNote, ...]:
        """Every source that is resting now, with the answer that put it there."""
        now = self._clock()
        return tuple(note for note in self._resting.values() if note.until_ns > now)

    def _instance(self, cls: type[LiveSource]) -> LiveSource:
        source = self._instances.get(cls.name)
        if source is None:
            source = cls(self._fetch)
            self._instances[cls.name] = source
        return source

    def chain(self, listing: LiveListing) -> list[LiveSource]:
        """The instantiated sources that can answer for this listing, deepest view first."""
        out: list[LiveSource] = []
        for cls in sources_for(listing.mic, allow=self._allow):
            source = self._instance(cls)
            if source.serves(listing):
                out.append(source)
        return out

    def best_tier(self, listing: LiveListing) -> LiveTier | None:
        """The deepest tier the listing's chain declares, or ``None`` for an empty chain."""
        chain = self.chain(listing)
        return chain[0].tier_for(listing.mic) if chain else None

    def _is_resting(self, source: LiveSource, now: int) -> bool:
        note = self._resting.get(source.name)
        if note is None:
            return False
        if note.until_ns > now:
            return True
        del self._resting[source.name]
        return False

    def _rest(self, source: LiveSource, kind: str, reason: str, detail: str, span_ns: int) -> None:
        self._resting[source.name] = SourceNote(
            source=source.name,
            kind=kind,
            reason=reason,
            detail=detail,
            until_ns=self._clock() + span_ns,
        )

    def _cached(
        self, source: LiveSource, listing: LiveListing, now: int
    ) -> tuple[bool, LiveSnapshot | None]:
        """Whether the cadence floor answers for this source and listing now, and with what.

        ``(True, snapshot)`` inside ``min_interval_s`` of the last request; ``(True, None)`` when
        that request found nothing; ``(False, None)`` when the source may be asked again.
        """
        cached = self._last.get((source.name, listing))
        if cached is None:
            return False, None
        age_s = (now - cached[0]) / _NS_PER_SECOND
        if 0.0 <= age_s < source.min_interval_s:
            return True, cached[1]
        return False, None

    async def _ask(
        self, source: LiveSource, listings: Sequence[LiveListing], now: int
    ) -> dict[LiveListing, LiveSnapshot]:
        """One request to one source, with walls and faults recorded. Never raises."""
        try:
            answers = await source.snapshots(listings, captured_ns=now)
        except SourceUnservableError as exc:
            log.info("live source %s is walled: %s", source.name, exc)
            self._rest(source, "wall", exc.reason.value, exc.detail, self._wall_cooldown_ns)
            return {}
        except (LivePayloadError, ProviderError) as exc:
            count = self._faults.get(source.name, 0) + 1
            self._faults[source.name] = count
            log.info("live source %s answered unreadably (%s): %s", source.name, count, exc)
            if count >= _FAULTS_BEFORE_COOLDOWN:
                self._faults[source.name] = 0
                self._rest(source, "fault", type(exc).__name__, str(exc), self._fault_cooldown_ns)
            return {}
        self._faults[source.name] = 0
        # A source may hand back a listing it learned more about (an ISIN); the answer still
        # belongs to the listing that was asked for, matched on market and local symbol.
        asked = {(listing.mic, listing.local_symbol): listing for listing in listings}
        out: dict[LiveListing, LiveSnapshot] = {}
        for answer in answers:
            requested = asked.get((answer.listing.mic, answer.listing.local_symbol))
            if requested is not None and not answer.is_empty:
                out[requested] = answer
        return out

    async def poll(
        self, listing: LiveListing, *, max_tier: LiveTier | None = None
    ) -> LiveSnapshot | None:
        """The deepest snapshot any source of the listing's chain gives now, or ``None``.

        ``max_tier`` skips sources that declare a deeper tier than it: ``LiveTier.LAST`` asks
        only the last-price sources, which is how a caller reads a listing's session figures
        beside a book it already has from a deeper source.
        """
        now = self._clock()
        for source in self.chain(listing):
            if (
                max_tier is not None
                and TIER_RANK[source.tier_for(listing.mic)] < TIER_RANK[max_tier]
            ):
                continue
            if self._is_resting(source, now):
                continue
            hit, cached = self._cached(source, listing, now)
            if hit:
                if cached is not None:
                    return cached
                continue
            answers = await self._ask(source, [listing], now)
            snapshot = answers.get(listing)
            self._last[(source.name, listing)] = (now, snapshot)
            if snapshot is not None:
                return snapshot
        return None

    async def identify(self, listing: LiveListing) -> LiveListing:
        """The listing with its ISIN filled in, when a source of its market needs one.

        Some publishers key their service by ISIN. A caller that knows a listing only by its
        symbol passes it here once; if any source of the market declares ``needs_isin`` and the
        listing carries none, the scanner is asked for the listing's ISIN. The answer, found or
        not, is remembered for the life of the feed, and the listing is returned unchanged
        when nothing was found.
        """
        if listing.isin is not None:
            return listing
        known = self._identified.get(listing)
        if known is not None:
            return known
        classes = sources_for(listing.mic, allow=self._allow)
        resolved = listing
        if any(cls.needs_isin for cls in classes):
            now = self._clock()
            for cls in classes:
                if cls.name != TradingViewLive.name:
                    continue
                source = self._instance(cls)
                if not source.serves(listing) or self._is_resting(source, now):
                    continue
                answers = await self._ask(source, [listing], now)
                for answer in answers.values():
                    if answer.listing.isin is not None:
                        resolved = LiveListing(
                            mic=listing.mic,
                            local_symbol=listing.local_symbol,
                            isin=answer.listing.isin,
                        )
        self._identified[listing] = resolved
        return resolved

    async def minute_bars(self, listing: LiveListing) -> list[MinuteBar]:
        """Today's (or the latest session's) one-minute bars, oldest first; empty when none.

        Read from the chart source when it covers the listing's market. A wall rests the
        source like any other; an unreadable answer is an empty list.
        """
        now = self._clock()
        for cls in sources_for(listing.mic, allow=self._allow):
            if cls is not YahooChartLive:
                continue
            source = self._instance(cls)
            if not isinstance(source, YahooChartLive) or not source.serves(listing):
                continue
            if self._is_resting(source, now):
                return []
            try:
                return await source.minute_bars(listing)
            except SourceUnservableError as exc:
                self._rest(source, "wall", exc.reason.value, exc.detail, self._wall_cooldown_ns)
            except (LivePayloadError, ProviderError) as exc:
                log.info("live source %s minute bars were unreadable: %s", source.name, exc)
        return []

    async def poll_many(self, listings: Sequence[LiveListing]) -> dict[LiveListing, LiveSnapshot]:
        """The deepest available snapshot of each listing, batching where a source allows it.

        Listings are grouped by the first non-resting source of their chain and sent in
        requests of that source's ``batch_size``; whatever a source does not answer falls to
        the next source of its chain. The cadence floor applies per listing, as in :meth:`poll`:
        a source that answered a listing within its ``min_interval_s`` is not asked again.
        """
        now = self._clock()
        out: dict[LiveListing, LiveSnapshot] = {}
        pending: dict[LiveListing, list[LiveSource]] = {
            listing: [s for s in self.chain(listing) if not self._is_resting(s, now)]
            for listing in dict.fromkeys(listings)
        }
        while True:
            groups: dict[str, tuple[LiveSource, list[LiveListing]]] = {}
            for listing, chain in pending.items():
                if listing in out:
                    continue
                while chain:
                    source = chain.pop(0)
                    hit, cached = self._cached(source, listing, now)
                    if hit:
                        # The cadence floor answers for this source: no request is made.
                        if cached is not None:
                            out[listing] = cached
                            break
                        continue
                    groups.setdefault(source.name, (source, []))[1].append(listing)
                    break
            if not groups:
                return out
            for source, members in groups.values():
                if self._is_resting(source, now):
                    continue
                size = max(1, source.batch_size)
                for start in range(0, len(members), size):
                    batch = members[start : start + size]
                    answers = await self._ask(source, batch, now)
                    for listing in batch:
                        self._last[(source.name, listing)] = (now, answers.get(listing))
                    out.update(answers)
                    if self._is_resting(source, self._clock()):
                        break

    async def board(self, mic: str, *, limit: int | None = None) -> list[LiveSnapshot]:
        """Every listing some source of the market's chain can enumerate; empty when none can."""
        now = self._clock()
        for cls in sources_for(mic, allow=self._allow):
            if not cls.enumerates:
                continue
            source = self._instance(cls)
            if self._is_resting(source, now):
                continue
            try:
                rows = await source.board(mic, captured_ns=now, limit=limit)
            except SourceUnservableError as exc:
                self._rest(source, "wall", exc.reason.value, exc.detail, self._wall_cooldown_ns)
                continue
            except (LivePayloadError, ProviderError) as exc:
                log.info("live source %s board was unreadable: %s", source.name, exc)
                continue
            if rows:
                return rows
        return []

    async def close(self) -> None:
        """Close every HTTP session the feed's sources opened."""
        instances, self._instances = self._instances, {}
        for source in instances.values():
            await source.close()
