"""Which live sources answer for a market, in which order, and the coverage table that says so.

Every live source is registered here once. :func:`sources_for` returns a market's chain: the
sources that declare the market, ordered so that the deepest published view is asked first and
the shallowest last.

Order of a chain, in this precedence:

1. Tier: a source that publishes a ladder (``L2``) before one that publishes a top of book
   (``L1``) before one that publishes only the last price (``LAST``).
2. Publisher: the exchange's own service before an aggregator, at the same tier.
3. Delay: a real-time or undeclared service before a declared-delayed one.
4. Registration order, which is the order of :data:`SOURCE_CLASSES`.

Ruling: sanctioned markets are default-closed here as everywhere else in the universal
sources. :func:`sources_for` returns an empty chain for a sanctioned market unless the operator
opted the country in, and :func:`coverage` reports the market as closed. Cost if wrong: a live
poll would be the one path that reads a market the daily pipeline refuses.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import functools
from collections.abc import Collection
from dataclasses import dataclass
from typing import Final

from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.sanctions import is_sanctioned
from crocodile.equity.universal.live.base import LiveSource
from crocodile.equity.universal.live.model import TIER_RANK, LiveTier
from crocodile.equity.universal.live.sites import site_source_classes
from crocodile.equity.universal.live.sources.asx import AsxLive
from crocodile.equity.universal.live.sources.b3 import B3Live
from crocodile.equity.universal.live.sources.boerse_frankfurt import BoerseFrankfurtLive
from crocodile.equity.universal.live.sources.groww import GrowwLive
from crocodile.equity.universal.live.sources.naver import NaverLive
from crocodile.equity.universal.live.sources.sgx import SgxLive
from crocodile.equity.universal.live.sources.six import SixLive
from crocodile.equity.universal.live.sources.sse_yunhq import SseYunhqLive
from crocodile.equity.universal.live.sources.szse import SzseLive
from crocodile.equity.universal.live.sources.tencent import TencentLive
from crocodile.equity.universal.live.sources.tmx import TmxLive
from crocodile.equity.universal.live.sources.tradegate import TradegateLive
from crocodile.equity.universal.live.sources.tradingview import TradingViewLive
from crocodile.equity.universal.live.sources.twse_mis import TwseMisLive
from crocodile.equity.universal.live.sources.vps import VpsLive
from crocodile.equity.universal.live.sources.yahoo_chart import YahooChartLive

__all__ = [
    "SOURCE_CLASSES",
    "CoverageRow",
    "coverage",
    "source_class",
    "sources_for",
]

_DEDICATED: Final[tuple[type[LiveSource], ...]] = (
    TwseMisLive,
    SseYunhqLive,
    SzseLive,
    BoerseFrankfurtLive,
    NaverLive,
    GrowwLive,
    VpsLive,
    TencentLive,
    TradegateLive,
    SgxLive,
    SixLive,
    AsxLive,
    B3Live,
    TmxLive,
)
_FLOOR: Final[tuple[type[LiveSource], ...]] = (TradingViewLive, YahooChartLive)


@functools.cache
def _all_classes() -> tuple[type[LiveSource], ...]:
    classes = (*_DEDICATED, *site_source_classes(), *_FLOOR)
    names = [cls.name for cls in classes]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"live source names are not unique: {duplicates}")
    return classes


def __getattr__(name: str) -> object:
    if name == "SOURCE_CLASSES":
        return _all_classes()
    raise AttributeError(name)


SOURCE_CLASSES: tuple[type[LiveSource], ...]
"""Every registered live source, in registration order (resolved lazily on first access)."""


def source_class(name: str) -> type[LiveSource]:
    """The registered class of a source name."""
    for cls in _all_classes():
        if cls.name == name:
            return cls
    raise KeyError(name)


def _order_key(mic: str, index: int, cls: type[LiveSource]) -> tuple[int, int, int, int]:
    delayed = 1 if cls.delay_s is not None and cls.delay_s > 0 else 0
    return (TIER_RANK[cls.tier_for(mic)], 0 if cls.official else 1, delayed, index)


def sources_for(mic: str, *, allow: Collection[str] = ()) -> tuple[type[LiveSource], ...]:
    """The live chain of an operating MIC, deepest view first. Empty for a closed market."""
    registry = ExchangeRegistry()
    registry.get(mic)
    if is_sanctioned(mic, allow=frozenset(allow)):
        return ()
    indexed = [
        (index, cls) for index, cls in enumerate(_all_classes()) if mic in cls.supported_mics
    ]
    indexed.sort(key=lambda item: _order_key(mic, *item))
    return tuple(cls for _, cls in indexed)


@dataclass(frozen=True, slots=True)
class CoverageRow:
    """What the live layer can show for one market, as declared by its chain."""

    mic: str
    name: str
    country: str
    closed: bool
    """True for a sanctioned, default-closed market."""

    tier: LiveTier | None
    """Deepest tier any source of the chain declares; ``None`` when the chain is empty."""

    depth_levels: int
    has_tape: bool
    official: bool
    """True when the deepest-tier source is the exchange's own service."""

    enumerates: bool
    """True when some source of the chain can list the market's whole board."""

    sources: tuple[str, ...]


def coverage(*, allow: Collection[str] = ()) -> list[CoverageRow]:
    """One row per allow-listed market, in MIC order."""
    registry = ExchangeRegistry()
    rows: list[CoverageRow] = []
    for info in sorted(registry.all(), key=lambda item: item.mic):
        closed = is_sanctioned(info.mic, allow=frozenset(allow))
        chain = () if closed else sources_for(info.mic, allow=allow)
        best = chain[0] if chain else None
        rows.append(
            CoverageRow(
                mic=info.mic,
                name=info.name,
                country=info.country,
                closed=closed,
                tier=best.tier_for(info.mic) if best is not None else None,
                depth_levels=best.depth_for(info.mic) if best is not None else 0,
                has_tape=any(cls.has_tape for cls in chain),
                official=best.official if best is not None else False,
                enumerates=any(getattr(cls, "enumerates", False) for cls in chain),
                sources=tuple(cls.name for cls in chain),
            )
        )
    return rows
