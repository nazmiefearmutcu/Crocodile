"""The default source bindings of the universal orchestrator.

``default_bindings`` adapts the sources tasks 10-12 registered to the three protocols the
orchestrator owns (``HistorySource``, ``UniverseSource``, ``SnapshotSource``). Every source gets one
instance per call, so every listing of a run shares that source's ``budget(source, host)`` bucket.
Nothing here opens a session at construction: the providers and clients create their HTTP sessions
lazily, so the adapters run offline until a request is made.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import date
from typing import Any

from crocodile.core.config import Settings
from crocodile.core.markets.errors import InvalidSymbolError
from crocodile.core.markets.grammar import GrammarError
from crocodile.core.markets.master import Listing, ListingMaster
from crocodile.core.markets.sanctions import allowed_countries
from crocodile.core.markets.symbols import listing_symbol
from crocodile.core.schema.records import Record
from crocodile.core.sink.base import Sink
from crocodile.core.store.catalog import Catalog
from crocodile.equity.providers.factory import make_provider, supported_channels, supported_mics
from crocodile.equity.reference.registry import InstrumentRegistry
from crocodile.equity.universal import tradingview as tv
from crocodile.equity.universal import yahoo_chart as yc
from crocodile.equity.universal.base import UniverseSnapshot
from crocodile.equity.universal.http import Fetcher
from crocodile.equity.universal.orchestrator import (
    HistorySource,
    SnapshotBatch,
    SnapshotSource,
    SourceBindings,
    UniverseSource,
)
from crocodile.equity.universal.order import directory_source_names

_YAHOO: str = "yahoo_chart"
_TRADINGVIEW: str = "tradingview"
_DAILY: str = "1d"


class _YahooHistory:
    """yahoo_chart as the only history source: daily bars, corporate actions and intraday bars."""

    name: str = _YAHOO

    def __init__(self, daily: Any, intraday: Mapping[str, Any]) -> None:
        self._daily = daily
        self._intraday = dict(intraday)

    def serves(self, mic: str, channel: str) -> bool:
        return channel in (supported_channels(_YAHOO) or frozenset()) and mic in (
            supported_mics(_YAHOO) or frozenset()
        )

    def history(
        self,
        listing: Listing,
        channel: str,
        start_ns: int,
        end_ns: int,
        *,
        captured_ns: int,
    ) -> AsyncIterator[Record]:
        return self._history(listing, channel, start_ns, end_ns)

    async def _history(
        self, listing: Listing, channel: str, start_ns: int, end_ns: int
    ) -> AsyncIterator[Record]:
        async for record in self._daily.backfill(channel, listing.symbol, start_ns, end_ns):
            yield record

    def intraday(
        self, listing: Listing, interval: str, *, captured_ns: int
    ) -> AsyncIterator[Record]:
        return self._intraday_stream(listing, interval, captured_ns)

    async def _intraday_stream(
        self, listing: Listing, interval: str, captured_ns: int
    ) -> AsyncIterator[Record]:
        provider = self._intraday.get(interval)
        if provider is None:
            raise ValueError(f"yahoo_chart has no intraday provider for {interval!r}")
        start_ns, _end_ns = yc.intraday_window(interval, captured_ns)
        async for record in provider.backfill("ohlcv", listing.symbol, start_ns, captured_ns):
            yield record


class _DirectoryUniverse:
    """One official listing directory as a universe source (its name is its unit)."""

    def __init__(self, name: str, provider: Any) -> None:
        self.name = name
        self._provider = provider

    def units_for(self, mics: frozenset[str]) -> Mapping[str, frozenset[str]]:
        enumerated = (supported_mics(self.name) or frozenset()) & mics
        return {self.name: frozenset(enumerated)} if enumerated else {}

    async def universe(self, unit: str, *, captured_ns: int) -> UniverseSnapshot:
        snapshot: UniverseSnapshot = await self._provider.snapshot()
        return snapshot


def _tradingview_units(mics: frozenset[str], allow: frozenset[str]) -> Mapping[str, frozenset[str]]:
    """Scannable TradingView markets, each with the requested MICs its market table enumerates."""
    found: dict[str, set[str]] = {}
    for market in tv.scannable_markets(allow=allow):
        for mic in mics:
            names = {row.market for row in tv.markets_for_mic(mic)}
            if market.market in names:
                found.setdefault(market.market, set()).add(mic)
    return {unit: frozenset(members) for unit, members in found.items()}


class _TradingViewUniverse:
    """TradingView's universe scan: one scan per market, bounded by ``scan_max_rows`` when set."""

    name: str = _TRADINGVIEW

    def __init__(
        self,
        client: tv.TradingViewClient,
        *,
        allow: frozenset[str],
        scan_max_rows: int | None,
    ) -> None:
        self._client = client
        self._allow = allow
        self._scan_max_rows = scan_max_rows

    def units_for(self, mics: frozenset[str]) -> Mapping[str, frozenset[str]]:
        return _tradingview_units(mics, self._allow)

    async def universe(self, unit: str, *, captured_ns: int) -> UniverseSnapshot:
        scan = await self._client.scan(unit, tv.UNIVERSE_COLUMNS, max_rows=self._scan_max_rows)
        return tv.universe_from_scan(scan, captured_at_ns=captured_ns, allow=self._allow)


class _TradingViewSnapshot:
    """TradingView's end-of-day scan, attributed to the sessions the orchestrator asks for."""

    name: str = _TRADINGVIEW

    def __init__(
        self,
        client: tv.TradingViewClient,
        *,
        data_dir: Any,
        allow: frozenset[str],
        scan_max_rows: int | None,
    ) -> None:
        self._client = client
        self._data_dir = data_dir
        self._allow = allow
        self._scan_max_rows = scan_max_rows

    def units_for(self, mics: frozenset[str]) -> Mapping[str, frozenset[str]]:
        return _tradingview_units(mics, self._allow)

    async def snapshot(
        self, unit: str, sessions: Mapping[str, date], *, captured_ns: int
    ) -> SnapshotBatch:
        scan = await self._client.scan(unit, tv.EOD_COLUMNS, max_rows=self._scan_max_rows)
        grammar = tv.TradingViewProvider.grammar()
        mic_of: dict[str, str] = {}
        for row in scan.rows:
            try:
                local, mic = grammar.from_source(row.ticker)
                symbol = listing_symbol(local, mic)
            except (GrammarError, InvalidSymbolError):
                continue
            mic_of[symbol] = mic
        catalog = Catalog(self._data_dir)
        try:
            previous = tv.previous_snapshot_values(catalog, sorted(mic_of))
        finally:
            catalog.close()
        eod = tv.eod_bars_from_scan(
            scan, captured_at_ns=captured_ns, previous=previous, allow=self._allow
        )
        attributed = frozenset(mic for mic in sessions if eod.sessions.get(mic) == sessions[mic])
        records = tuple(bar for bar in eod.bars if mic_of.get(bar.symbol) in attributed)
        return SnapshotBatch(records=records, attributed_mics=attributed)


def _check_scan_bound(scan_max_rows: int | None) -> None:
    if scan_max_rows is None:
        return
    if isinstance(scan_max_rows, bool) or not isinstance(scan_max_rows, int) or scan_max_rows < 1:
        raise ValueError(f"scan_max_rows must be None or a positive int, got {scan_max_rows!r}")


def default_bindings(
    settings: Settings,
    sink: Sink,
    *,
    master: ListingMaster | None = None,
    clock_ns: Callable[[], int] | None = None,
    scan_max_rows: int | None = None,
    yahoo_client: yc.YahooChartClient | None = None,
    tradingview_client: tv.TradingViewClient | None = None,
    directory_fetch: Fetcher | None = None,
) -> SourceBindings:
    """Bind yahoo_chart (history), TradingView (universe and snapshot) and the directories.

    ``scan_max_rows`` bounds both TradingView scans to one page; ``None`` pages to ``totalCount``.
    The keyword seams (``yahoo_client``, ``tradingview_client``, ``directory_fetch``) are the
    offline test doubles the sources already accept.
    """
    _check_scan_bound(scan_max_rows)
    clock = clock_ns if clock_ns is not None else time.time_ns
    allow = allowed_countries(settings)
    yahoo = yahoo_client if yahoo_client is not None else yc.YahooChartClient()
    trading = tradingview_client if tradingview_client is not None else tv.TradingViewClient()

    daily = make_provider(
        _YAHOO,
        symbols=[],
        channels=["ohlcv", "corp_action"],
        out=sink,
        registry=InstrumentRegistry(),
        settings=settings,
        data_dir=settings.data_dir,
        master=master,
        client=yahoo,
        clock=clock,
    )
    intraday: dict[str, Any] = {}
    for interval in yc.INTRADAY_WINDOWS:
        intraday[interval] = make_provider(
            _YAHOO,
            symbols=[],
            channels=["ohlcv"],
            out=sink,
            registry=InstrumentRegistry(),
            settings=settings,
            data_dir=settings.data_dir,
            master=master,
            client=yahoo,
            clock=clock,
            interval=interval,
        )
    directories: dict[str, UniverseSource] = {}
    directory_providers: list[object] = []
    for name in sorted(directory_source_names()):
        provider = make_provider(
            name,
            symbols=["*"],
            channels=["instrument"],
            out=sink,
            registry=InstrumentRegistry(),
            settings=settings,
            data_dir=settings.data_dir,
            clock=clock,
            fetch=directory_fetch,
        )
        directory_providers.append(provider)
        directories[name] = _DirectoryUniverse(name, provider)

    history: dict[str, HistorySource] = {_YAHOO: _YahooHistory(daily, intraday)}
    universe: dict[str, UniverseSource] = {
        **directories,
        _TRADINGVIEW: _TradingViewUniverse(trading, allow=allow, scan_max_rows=scan_max_rows),
    }
    snapshot: dict[str, SnapshotSource] = {
        _TRADINGVIEW: _TradingViewSnapshot(
            trading,
            data_dir=settings.data_dir,
            allow=allow,
            scan_max_rows=scan_max_rows,
        )
    }
    resources: tuple[object, ...] = (
        daily,
        *intraday.values(),
        yahoo,
        *directory_providers,
        trading,
    )
    return SourceBindings(
        history=history,
        universe=universe,
        snapshot=snapshot,
        resources=resources,
    )
