"""The equity halves of ``collect`` and ``backfill`` through the universal sources (task 14c).

Offline throughout: the sources are the fakes of ``tests/equity/universal/fakes.py`` bound
through ``crocodile.equity.universal.bindings.default_bindings``, so no provider, client or
socket is built. The master is seeded through ``ListingMaster.for_data_dir``.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import inspect
from pathlib import Path
from typing import Any

import pytest

from crocodile.capabilities import ops
from crocodile.core.capability import AssetClass, CapabilityContext
from crocodile.core.config import Settings
from crocodile.core.errors import CapabilityUnavailable
from crocodile.core.markets.errors import SanctionedMarketError
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.master import ListingMaster
from crocodile.core.store.catalog import Catalog
from crocodile.equity.universal import orchestrator as orchestrator_module
from crocodile.equity.universal.orchestrator import EARLIEST_NS, SourceBindings
from tests.equity.universal.fakes import FakeHistorySource, make_observation, seed_master

_EPOCH = datetime.date(1970, 1, 1)
_NS_PER_DAY = 86_400 * 1_000_000_000


def _midnight_ns(day: datetime.date) -> int:
    return (day - _EPOCH).days * _NS_PER_DAY


_END_2024_03_08 = _midnight_ns(datetime.date(2024, 3, 8))
_END_2024_01_01 = _midnight_ns(datetime.date(2024, 1, 1))


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    root = tmp_path / "lake"
    root.mkdir()
    return root


def _equity_ctx(lake: Path, *, readonly: bool = False) -> CapabilityContext:
    return CapabilityContext(
        catalog=Catalog(lake),
        settings=Settings(data_dir=lake),
        asset_class=AssetClass.EQUITY,
        readonly=readonly,
    )


def _crypto_ctx(lake: Path) -> CapabilityContext:
    return CapabilityContext(
        catalog=Catalog(lake),
        settings=Settings(data_dir=lake),
        asset_class=AssetClass.CRYPTO,
    )


def _seed_turkish_listing(lake: Path) -> None:
    master = ListingMaster.for_data_dir(lake)
    try:
        seed_master(
            master,
            [
                make_observation(
                    "tradingview", "BIST:THYAO", "XIST", "THYAO", currency="TRY", price_scale=0
                )
            ],
        )
    finally:
        master.close()


def _bindings(history: FakeHistorySource | None = None) -> SourceBindings:
    history_map: dict[str, Any] = {}
    if history is not None:
        history_map[history.name] = history
    return SourceBindings(
        history=history_map,
        universe={},
        snapshot={},
        resources=(),
    )


def _bind(monkeypatch: pytest.MonkeyPatch, bindings: SourceBindings) -> None:
    monkeypatch.setattr(
        "crocodile.equity.universal.bindings.default_bindings",
        lambda settings, sink, **kw: bindings,
    )


def _thyao_history() -> FakeHistorySource:
    return FakeHistorySource(
        bars={
            "THYAO@XIST": [
                (datetime.date(2024, 3, 4), 300.0),
                (datetime.date(2024, 3, 5), 305.0),
            ]
        },
    )


@pytest.fixture
def _no_tick_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ops, "_UNIVERSAL_TICK_SECONDS", 0.01)


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


def test_collect_and_backfill_params_accept_a_market_slice_and_default_symbols_to_empty() -> None:
    collect_params = ops.CollectParams(sources=("universal",), channels=("ohlcv",), country="TR")
    assert collect_params.symbols == ()
    assert collect_params.country == "TR"
    assert collect_params.mic is None

    backfill_params = ops.BackfillParams(
        channel="ohlcv", start_ns=0, end_ns=_END_2024_01_01, sources=("universal",), mic="XIST"
    )
    assert backfill_params.source == ""
    assert backfill_params.symbols == ()
    assert backfill_params.mic == "XIST"


# ---------------------------------------------------------------------------
# The crypto halves refuse what only equity can answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dimension", [{"mic": "XIST"}, {"country": "TR"}])
def test_the_crypto_collect_refuses_a_market_slice(
    lake: Path, dimension: dict[str, str], network_forbidden: None
) -> None:
    params = ops.CollectParams(
        sources=("binance",), channels=("trade",), symbols=("BTCUSDT",), **dimension
    )
    with pytest.raises(CapabilityUnavailable, match="equity") as caught:
        ops.collect(_crypto_ctx(lake), params)
    assert next(iter(dimension)) in caught.value.reason


@pytest.mark.parametrize("dimension", [{"mic": "XIST"}, {"country": "TR"}])
def test_the_crypto_backfill_refuses_a_market_slice(
    lake: Path, dimension: dict[str, str], network_forbidden: None
) -> None:
    params = ops.BackfillParams(
        channel="trade",
        start_ns=0,
        end_ns=_END_2024_01_01,
        source="binance",
        symbols=("BTCUSDT",),
        **dimension,
    )
    with pytest.raises(CapabilityUnavailable, match="equity") as caught:
        ops.backfill(_crypto_ctx(lake), params)
    assert next(iter(dimension)) in caught.value.reason


def test_a_crypto_backfill_refuses_sources_and_requires_source(lake: Path) -> None:
    ctx = _crypto_ctx(lake)
    with pytest.raises(ValueError, match="sources"):
        ops.backfill(
            ctx,
            ops.BackfillParams(
                channel="trade",
                start_ns=0,
                end_ns=_END_2024_01_01,
                sources=("universal",),
                symbols=("BTCUSDT",),
            ),
        )
    with pytest.raises(ValueError, match="needs a source"):
        ops.backfill(
            ctx,
            ops.BackfillParams(
                channel="trade",
                start_ns=0,
                end_ns=_END_2024_01_01,
                symbols=("BTCUSDT",),
            ),
        )


# ---------------------------------------------------------------------------
# Equity source selection
# ---------------------------------------------------------------------------


def test_a_native_equity_source_with_a_market_slice_is_refused(lake: Path) -> None:
    ctx = _equity_ctx(lake)
    with pytest.raises(ValueError, match="slicing"):
        ops.collect_equities(
            ctx,
            ops.CollectParams(
                sources=("stooq",), channels=("ohlcv",), symbols=("AAPL",), mic="XNAS"
            ),
        )
    with pytest.raises(ValueError, match="slicing"):
        ops.backfill_equities(
            ctx,
            ops.BackfillParams(
                source="stooq",
                channel="ohlcv",
                start_ns=0,
                end_ns=_END_2024_01_01,
                symbols=("AAPL",),
                country="US",
            ),
        )


def test_mixing_universal_and_native_sources_is_refused(lake: Path) -> None:
    with pytest.raises(ValueError, match="mix"):
        ops.collect_equities(
            _equity_ctx(lake),
            ops.CollectParams(
                sources=("universal", "stooq"),
                channels=("ohlcv",),
                symbols=("AAPL",),
            ),
        )


def test_backfill_takes_either_source_or_sources(lake: Path) -> None:
    ctx = _equity_ctx(lake)
    with pytest.raises(ValueError, match="either source or sources"):
        ops.backfill_equities(
            ctx,
            ops.BackfillParams(
                source="universal",
                sources=("universal",),
                channel="ohlcv",
                start_ns=0,
                end_ns=_END_2024_01_01,
                country="TR",
            ),
        )


def test_a_native_equity_backfill_still_requires_symbols(lake: Path) -> None:
    with pytest.raises(ValueError, match="symbols"):
        ops.backfill_equities(
            _equity_ctx(lake),
            ops.BackfillParams(source="stooq", channel="ohlcv", start_ns=0, end_ns=1),
        )


# ---------------------------------------------------------------------------
# Universal backfill
# ---------------------------------------------------------------------------


def test_universal_backfill_returns_an_unstarted_run_that_writes_through_the_orchestrator(
    lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_turkish_listing(lake)
    _bind(monkeypatch, _bindings(_thyao_history()))
    run = ops.backfill_equities(
        _equity_ctx(lake),
        ops.BackfillParams(
            channel="ohlcv",
            start_ns=0,
            end_ns=_END_2024_03_08,
            sources=("universal",),
            country="TR",
        ),
    )
    assert inspect.iscoroutine(run)
    assert not list(lake.glob("source=*"))  # nothing written before the run starts

    written = asyncio.run(run)

    assert isinstance(written, int)
    assert written > 0
    assert list((lake / "source=yahoo_chart" / "channel=ohlcv").rglob("*.parquet"))
    assert list((lake / "state" / "runs").glob("backfill-*.json"))


def test_universal_backfill_also_accepts_the_alias_as_source(
    lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_turkish_listing(lake)
    _bind(monkeypatch, _bindings(_thyao_history()))
    run = ops.backfill_equities(
        _equity_ctx(lake),
        ops.BackfillParams(
            source="universal",
            channel="ohlcv",
            start_ns=0,
            end_ns=_END_2024_03_08,
            country="TR",
        ),
    )
    assert asyncio.run(run) > 0


def test_universal_backfill_reads_a_non_positive_start_as_the_earliest_history(
    lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_turkish_listing(lake)
    history = _thyao_history()
    _bind(monkeypatch, _bindings(history))
    run = ops.backfill_equities(
        _equity_ctx(lake),
        ops.BackfillParams(
            channel="ohlcv",
            start_ns=-5,
            end_ns=_END_2024_03_08,
            sources=("universal",),
            country="TR",
        ),
    )
    asyncio.run(run)
    assert history.calls
    assert history.calls[0][2] == EARLIEST_NS


def test_universal_backfill_refuses_instrument_and_names_universe_sync(lake: Path) -> None:
    with pytest.raises(ValueError, match="universe-sync"):
        ops.backfill_equities(
            _equity_ctx(lake),
            ops.BackfillParams(
                channel="instrument",
                start_ns=0,
                end_ns=_END_2024_01_01,
                sources=("universal",),
                country="TR",
            ),
        )


def test_universal_backfill_without_a_scope_is_refused(lake: Path) -> None:
    with pytest.raises(ValueError, match="symbols or mic/country"):
        ops.backfill_equities(
            _equity_ctx(lake),
            ops.BackfillParams(
                channel="ohlcv",
                start_ns=0,
                end_ns=_END_2024_01_01,
                sources=("universal",),
            ),
        )


def test_universal_backfill_refuses_an_inverted_range(lake: Path) -> None:
    with pytest.raises(ValueError, match="is after"):
        ops.backfill_equities(
            _equity_ctx(lake),
            ops.BackfillParams(
                channel="ohlcv",
                start_ns=_END_2024_03_08,
                end_ns=_END_2024_01_01,
                sources=("universal",),
                country="TR",
            ),
        )


def test_an_explicitly_named_sanctioned_market_is_refused_before_the_run_exists(
    lake: Path,
) -> None:
    with pytest.raises(SanctionedMarketError):
        ops.backfill_equities(
            _equity_ctx(lake),
            ops.BackfillParams(
                channel="ohlcv",
                start_ns=0,
                end_ns=_END_2024_01_01,
                sources=("universal",),
                country="RU",
            ),
        )
    assert not (lake / "state").exists()


def test_universal_backfill_with_no_listings_known_fails_when_run(
    lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(monkeypatch, _bindings(_thyao_history()))
    run = ops.backfill_equities(
        _equity_ctx(lake),
        ops.BackfillParams(
            channel="ohlcv",
            start_ns=0,
            end_ns=_END_2024_03_08,
            sources=("universal",),
            country="TR",
        ),
    )
    with pytest.raises(ValueError, match="universe-sync"):
        asyncio.run(run)


# ---------------------------------------------------------------------------
# Universal collect
# ---------------------------------------------------------------------------


def test_a_universal_collect_is_scoped_to_the_resolved_mics(lake: Path) -> None:
    sub = ops.collect_equities(
        _equity_ctx(lake),
        ops.CollectParams(sources=("universal",), channels=("ohlcv",), mic="XIST"),
    )
    assert sub.mics == ("XIST",)

    sub_country = ops.collect_equities(
        _equity_ctx(lake),
        ops.CollectParams(sources=("universal",), channels=("ohlcv",), country="TR"),
    )
    assert sub_country.mics
    assert all(ExchangeRegistry().get(mic).country == "TR" for mic in sub_country.mics)


async def _run_until_first_round_finishes(
    sub: ops.Subscription, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run an unbounded subscription until its first round has finished, then stop it.

    The stop signal is the orchestrator's own finish (which persists the run report), not a
    wall-clock bound, so a slow host cannot cut the first round short. The timeout only guards
    against a round that never ends.
    """
    from crocodile.equity.universal.orchestrator import UniversalOrchestrator

    finished = asyncio.Event()
    original_finish = UniversalOrchestrator._finish

    async def finish_then_signal(self: UniversalOrchestrator, run: Any, **kwargs: Any) -> Any:
        report = await original_finish(self, run, **kwargs)
        finished.set()
        return report

    monkeypatch.setattr(UniversalOrchestrator, "_finish", finish_then_signal)
    task = asyncio.ensure_future(sub.run())
    try:
        async with asyncio.timeout(60):
            await finished.wait()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_universal_collect_for_named_listings_polls_intraday(
    lake: Path, monkeypatch: pytest.MonkeyPatch, _no_tick_wait: None
) -> None:
    _seed_turkish_listing(lake)
    history = _thyao_history()
    _bind(monkeypatch, _bindings(history))
    sub = ops.collect_equities(
        _equity_ctx(lake),
        ops.CollectParams(
            sources=("universal",),
            channels=("ohlcv",),
            symbols=("THYAO@XIST",),
        ),
    )
    asyncio.run(_run_until_first_round_finishes(sub, monkeypatch))
    assert history.intraday_calls
    assert all(symbol == "THYAO@XIST" for symbol, _interval in history.intraday_calls)
    assert list((lake / "state" / "runs").glob("collect_intraday-*.json"))


def test_universal_collect_for_a_market_slice_collects_end_of_day(
    lake: Path, monkeypatch: pytest.MonkeyPatch, _no_tick_wait: None
) -> None:
    _seed_turkish_listing(lake)
    history = _thyao_history()
    _bind(monkeypatch, _bindings(history))
    sub = ops.collect_equities(
        _equity_ctx(lake),
        ops.CollectParams(
            sources=("universal",),
            channels=("ohlcv",),
            country="TR",
            duration_seconds=0.2,
        ),
    )
    asyncio.run(sub.run())
    assert history.calls
    assert not history.intraday_calls
    assert list((lake / "state" / "runs").glob("collect_eod-*.json"))


def test_universal_collect_refuses_channels_other_than_ohlcv(lake: Path) -> None:
    with pytest.raises(ValueError, match="universe-sync"):
        ops.collect_equities(
            _equity_ctx(lake),
            ops.CollectParams(sources=("universal",), channels=("corp_action",), country="TR"),
        )


def test_universal_collect_refuses_more_named_listings_than_the_intraday_cap(lake: Path) -> None:
    symbols = tuple(f"S{i}@XIST" for i in range(orchestrator_module.MAX_INTRADAY_LISTINGS + 1))
    with pytest.raises(ValueError, match="100"):
        ops.collect_equities(
            _equity_ctx(lake),
            ops.CollectParams(sources=("universal",), channels=("ohlcv",), symbols=symbols),
        )


def test_a_universal_run_reports_its_dead_letters(
    lake: Path, monkeypatch: pytest.MonkeyPatch, _no_tick_wait: None
) -> None:
    _seed_turkish_listing(lake)
    _bind(monkeypatch, _bindings(_thyao_history()))
    seen: dict[str, Any] = {}
    real_open = orchestrator_module.open_orchestrator

    def spy(settings: Settings, sink: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real_open(settings, sink, **kwargs)

    monkeypatch.setattr(orchestrator_module, "open_orchestrator", spy)
    sub = ops.collect_equities(
        _equity_ctx(lake),
        ops.CollectParams(
            sources=("universal",),
            channels=("ohlcv",),
            country="TR",
            duration_seconds=0.1,
        ),
    )
    asyncio.run(sub.run())
    assert seen.get("dlq_report_path") is None  # the default: runs under state/runs/

    explicit = lake / "reports" / "dlq.json"
    sub = ops.collect_equities(
        _equity_ctx(lake),
        ops.CollectParams(
            sources=("universal",),
            channels=("ohlcv",),
            country="TR",
            duration_seconds=0.1,
            dlq_report_path=str(explicit),
        ),
    )
    asyncio.run(sub.run())
    assert seen["dlq_report_path"] == explicit


def test_a_read_only_surface_refuses_universal_runs(lake: Path) -> None:
    ctx = _equity_ctx(lake, readonly=True)
    with pytest.raises(PermissionError, match="read-only"):
        ops.collect_equities(
            ctx, ops.CollectParams(sources=("universal",), channels=("ohlcv",), country="TR")
        )
    with pytest.raises(PermissionError, match="read-only"):
        ops.backfill_equities(
            ctx,
            ops.BackfillParams(
                channel="ohlcv",
                start_ns=0,
                end_ns=_END_2024_01_01,
                sources=("universal",),
                country="TR",
            ),
        )


# ---------------------------------------------------------------------------
# Nothing opens before the run starts
# ---------------------------------------------------------------------------


def test_the_universal_adapters_open_no_file_before_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    ctx = CapabilityContext(catalog=None, settings=Settings(), asset_class=AssetClass.EQUITY)

    sub = ops.collect_equities(
        ctx, ops.CollectParams(sources=("universal",), channels=("ohlcv",), country="TR")
    )
    run = ops.backfill_equities(
        ctx,
        ops.BackfillParams(
            channel="ohlcv",
            start_ns=0,
            end_ns=_END_2024_01_01,
            sources=("universal",),
            country="TR",
        ),
    )
    try:
        assert sub.mics
        assert inspect.iscoroutine(run)
    finally:
        run.close()
    assert list(tmp_path.iterdir()) == []


def test_the_universal_backfill_keeps_its_resolved_scope_as_a_local(
    lake: Path,
) -> None:
    run = ops.backfill_equities(
        _equity_ctx(lake),
        ops.BackfillParams(
            channel="ohlcv",
            start_ns=0,
            end_ns=_END_2024_01_01,
            sources=("universal",),
            country="TR",
        ),
    )
    try:
        assert inspect.getcoroutinelocals(run)["resolved"].mics == ("XIST",)
    finally:
        run.close()
