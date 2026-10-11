"""The ops batch: the five irreducible capabilities, the lake operations, and two refusals.

Every adapter here is exercised against a real temporary lake or against pure inputs. What
is deliberately *not* exercised is a network call: ``collect`` and ``backfill`` are asserted
to hand back a run that has not started, which is the property that keeps them out of this
suite's runtime as well as out of a REST route's event loop.
"""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from crocodile.capabilities import ops
from crocodile.core.capability import (
    IRREDUCIBLE,
    PENDING_SYMMETRY,
    REGISTRY,
    SPEC_METHODS,
    AssetClass,
    CapabilityContext,
    ReturnKind,
)
from crocodile.core.config import Settings
from crocodile.core.errors import ConfigError
from crocodile.core.schema.enums import Side
from crocodile.core.schema.provenance import Provenance, registered_bases
from crocodile.core.schema.records import OHLCV, BookTicker, Record, Trade
from crocodile.core.sink.base import Sink
from crocodile.core.store.catalog import Catalog
from crocodile.core.store.parquet_sink import ParquetSink

_BASE_NS = 1_700_000_000_000_000_000
"""2023-11-14 22:13:20 UTC, on a second boundary. Far enough in the past that the lake
paths ``calculate_peg_deviation`` bounds with ``time.time()`` include it."""

_1S = 1_000_000_000

_OPS_CAPABILITIES = (
    "gas-vol",
    "mev-sandwich",
    "sequencer-latency",
    "peg-deviation",
    "lending-stress",
    "collect",
    "collect-market",
    "backfill",
    "replay",
    "export",
    "resample",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _trade(ts: int, price: float, amount: float, tid: str) -> Trade:
    return Trade(
        source="deribit",
        symbol="deribit:BTC-PERPETUAL",
        symbol_raw="BTC-PERPETUAL",
        local_ts=ts,
        asset_class=AssetClass.CRYPTO,
        source_ts=ts,
        id=tid,
        price=price,
        amount=amount,
        side=Side.BUY,
    )


def _book_ticker(ts: int, bid: float, ask: float, *, source_ts: int | None = None) -> BookTicker:
    return BookTicker(
        source="base_onchain",
        symbol="base_onchain:USDC-USDbC",
        symbol_raw="USDC-USDbC",
        local_ts=ts,
        asset_class=AssetClass.CRYPTO,
        source_ts=ts if source_ts is None else source_ts,
        bid_px=bid,
        bid_sz=1.0,
        ask_px=ask,
        ask_sz=1.0,
    )


async def _write(data_dir: Path, records: list[Record]) -> None:
    sink = ParquetSink(data_dir, max_buffer_rows=10_000, flush_interval_seconds=9999)
    for record in records:
        await sink.put(record)
    await sink.flush()


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """A lake root with nothing in it yet. Each test writes what it needs."""
    root = tmp_path / "lake"
    root.mkdir()
    return root


@pytest.fixture
def ctx(lake: Path) -> Iterator[CapabilityContext]:
    """A trusted crypto context over an empty temporary lake."""
    catalog = Catalog(lake)
    try:
        yield CapabilityContext(
            catalog=catalog,
            settings=Settings(data_dir=lake),
            asset_class=AssetClass.CRYPTO,
        )
    finally:
        catalog.close()


@pytest.fixture
def readonly_ctx(lake: Path) -> Iterator[CapabilityContext]:
    """The same lake, reached through a surface that declared itself read-only."""
    catalog = Catalog(lake)
    try:
        yield CapabilityContext(
            catalog=catalog,
            settings=Settings(data_dir=lake),
            asset_class=AssetClass.CRYPTO,
            readonly=True,
        )
    finally:
        catalog.close()


# ---------------------------------------------------------------------------
# What this batch declared, and what it refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _OPS_CAPABILITIES)
def test_every_capability_this_batch_owns_is_in_the_registry(name: str) -> None:
    assert name in REGISTRY


@pytest.mark.parametrize("name", _OPS_CAPABILITIES)
def test_every_implementation_rests_on_a_basis_with_a_registered_formula(name: str) -> None:
    """Gate 3 asks this over the whole registry; asked here it names the batch that broke it."""
    known = registered_bases()
    for asset_class, impl in REGISTRY[name].impls.items():
        assert impl.basis in known, f"{name}/{asset_class} declares an unregistered basis"


@pytest.mark.parametrize("name", _OPS_CAPABILITIES)
def test_every_implementation_is_a_named_module_level_adapter(name: str) -> None:
    """A lambda or a partial would satisfy the calling convention and name nothing.

    The calling-convention gate checks the *shape* of ``Impl.fn``; this checks that a stack
    trace through one lands on a function with a file and a line number in this module.
    """
    for asset_class, impl in REGISTRY[name].impls.items():
        where = f"{name}/{asset_class}"
        assert inspect.isfunction(impl.fn), f"{where} is not a plain function"
        assert impl.fn.__module__ == ops.__name__, f"{where} is not declared in this batch"
        assert getattr(ops, impl.fn.__name__, None) is impl.fn, f"{where} is not module-level"


def test_every_irreducible_name_is_a_declared_capability() -> None:
    """``IRREDUCIBLE`` is now all capabilities, which is what the list is for.

    It named six and one of them was not: ``gas-tracker``. An exemption list whose entries
    are not all the same kind of thing means two things at once, and the one that is not a
    capability is the one nothing can check.

    ``load_all()`` because two of the seven — ``onchain-price`` and ``base-market-data`` —
    are declared in the ``onchain`` batch, which importing ``ops`` does not pull in. Without
    it this file passed inside the full suite and failed on its own, which is the wrong way
    round for a test whose whole subject is what the registry holds.
    """
    from crocodile.capabilities import load_all

    load_all()
    assert {name for name in IRREDUCIBLE if name in REGISTRY} == set(IRREDUCIBLE)


def test_gas_tracker_is_a_launcher_and_is_on_no_list_that_would_imply_otherwise() -> None:
    """Migrated: this used to assert ``"gas-tracker" in IRREDUCIBLE`` and call it a finding.

    It was a finding — recorded here because ``IRREDUCIBLE`` was not this batch's file to
    edit — and the coordinator acted on it, so the assertion now pins the resolution rather
    than the contradiction. A Qt launcher has no parameters, no return, no provenance and
    no asset class; it could not reach REST or MCP even in principle, and its old
    justification argued about gas *data*, which ``gas-vol`` already carries. ``flowmap``
    was the same kind of thing and was never on the list, which is the evidence.

    The reasoning stays reachable from the batch module rather than living only in a commit
    message, because the next reader of ``UNDECLARED`` is someone asking why this name is
    not in the registry.

    Both commands have since been deleted: the PyQt6 desktop layer left the repository, so
    ``gas-tracker`` is no longer a CLI command either. This test is unchanged by that, and
    deliberately so — ``UNDECLARED`` is a record of a decision, not an index of what ships,
    and the next reader asking "why is this name not a capability" is at least as likely to
    be reading a changelog as a ``--help``. What the deletion does change is the tense of
    the argument, not its content.
    """
    assert "gas-tracker" not in IRREDUCIBLE
    assert "gas-tracker" not in REGISTRY
    assert "gas-tracker" not in PENDING_SYMMETRY
    assert "gas-tracker" in ops.UNDECLARED
    assert ops.UNDECLARED["gas-tracker"]().strip()
    assert (ops._why_gas_tracker_is_not_a_capability.__doc__ or "").strip()


def test_migrate_lake_is_infrastructure_and_stays_out_of_the_registry() -> None:
    """It renames directories. There is no honest ``prov`` for a path that moved."""
    assert "migrate-lake" not in REGISTRY
    assert "migrate-lake" not in IRREDUCIBLE
    assert "migrate-lake" not in PENDING_SYMMETRY
    assert ops.UNDECLARED["migrate-lake"]().strip()
    assert (ops._why_migrate_lake_is_infrastructure.__doc__ or "").strip()


def test_collect_market_left_the_ledger_when_the_method_that_closes_it_landed() -> None:
    """It was scheduled against M3 because nothing enumerated an equity universe.

    Something does now — ``crocodile.equity.reference.universe``, which is M3's
    SEC EDGAR x OpenFIGI x Tiingo merge — so the entry had to go: a name whose equity half
    has landed and whose schedule survives is the hoarding
    ``test_the_ledger_is_not_hoarding_capabilities_that_became_symmetric`` exists to catch,
    and it would make a later *removal* of the equity half invisible.
    """
    assert "collect-market" not in PENDING_SYMMETRY
    assert "M3" in SPEC_METHODS
    assert "collect-market" not in IRREDUCIBLE
    assert set(REGISTRY["collect-market"].impls) == {AssetClass.CRYPTO, AssetClass.EQUITY}


def test_the_capabilities_that_serve_both_markets_declare_both_halves() -> None:
    for name in ("collect", "collect-market", "backfill", "replay", "export", "resample"):
        assert set(REGISTRY[name].impls) == {AssetClass.CRYPTO, AssetClass.EQUITY}, name


def test_only_the_two_never_ending_capabilities_are_streams() -> None:
    """``STREAM`` is about who ends the sequence, not about whether it arrives lazily.

    ``replay`` yields lazily and is a ``TABLE`` because the time range ends it; ``collect``
    is a ``STREAM`` because nothing does.
    """
    streams = {name for name in _OPS_CAPABILITIES if REGISTRY[name].returns is ReturnKind.STREAM}
    assert streams == {"collect", "collect-market"}
    assert REGISTRY["replay"].returns is ReturnKind.TABLE


# ---------------------------------------------------------------------------
# gas-vol
# ---------------------------------------------------------------------------


def test_gas_vol_finds_a_perfect_correlation_between_series_that_move_together(
    ctx: CapabilityContext,
) -> None:
    rows = [{"local_ts": _BASE_NS + i * _1S, "gas_price": float(i)} for i in range(10)]
    vol = [{"local_ts": _BASE_NS + i * _1S, "volatility": float(i) * 2.0} for i in range(10)]
    result = ops.gas_vol(ctx, ops.GasVolParams(gas=rows, vol=vol))
    assert result["pearson"] == pytest.approx(1.0)
    assert result["spearman"] == pytest.approx(1.0)


def test_gas_vol_reports_an_undefined_correlation_when_a_series_is_empty(
    ctx: CapabilityContext,
) -> None:
    """Empty is not zero correlation; it is no answer, which the pure function spells NaN."""
    result = ops.gas_vol(ctx, ops.GasVolParams(gas=[], vol=[]))
    assert math.isnan(result["pearson"])
    assert math.isnan(result["spearman"])


def test_a_caller_supplied_series_keeps_its_nanosecond_join_key_exact() -> None:
    """Why the row type is ``dict[str, Any]`` and not ``dict[str, float]``.

    A nanosecond epoch is past 2**53, so a float-typed schema would round the key the two
    series are aligned on and pair rows that were never adjacent.
    """
    frame = ops._rows_to_frame([{"local_ts": _BASE_NS + 1, "gas": 1.0}])
    assert frame.schema["local_ts"] == pl.Int64
    assert frame["local_ts"][0] == _BASE_NS + 1


# ---------------------------------------------------------------------------
# mev-sandwich
# ---------------------------------------------------------------------------


def _sandwich_rows() -> list[dict[str, Any]]:
    """One planted sandwich: attacker buys, victim buys, attacker sells, same block+pool."""
    return [
        {"block": 1, "pool": "AERO-USDC", "log_index": 0, "sender": "0xatk", "is_buy": True},
        {"block": 1, "pool": "AERO-USDC", "log_index": 1, "sender": "0xvic", "is_buy": True},
        {"block": 1, "pool": "AERO-USDC", "log_index": 2, "sender": "0xatk", "is_buy": False},
        {"block": 2, "pool": "AERO-USDC", "log_index": 0, "sender": "0xbob", "is_buy": True},
    ]


def test_mev_sandwich_flags_every_leg_of_a_planted_sandwich(ctx: CapabilityContext) -> None:
    out = ops.mev_sandwich(ctx, ops.MevSandwichParams(trades=_sandwich_rows()))
    assert out.height == 4
    assert out.filter(pl.col("is_sandwich")).height == 3


def test_mev_sandwich_returns_only_the_legs_when_the_caller_asks_for_only_the_legs(
    ctx: CapabilityContext,
) -> None:
    """The crypto CLI's ``--sandwiches-only``, which REST and MCP never had."""
    out = ops.mev_sandwich(
        ctx, ops.MevSandwichParams(trades=_sandwich_rows(), sandwiches_only=True)
    )
    assert out.height == 3
    assert out["is_sandwich"].all()


def test_mev_sandwich_answers_an_empty_sequence_with_an_empty_table(
    ctx: CapabilityContext,
) -> None:
    out = ops.mev_sandwich(ctx, ops.MevSandwichParams(trades=[]))
    assert out.height == 0
    assert "is_sandwich" in out.columns


def test_mev_sandwich_still_refuses_a_sequence_that_is_missing_a_required_column(
    ctx: CapabilityContext,
) -> None:
    """The early return covers emptiness only; a malformed non-empty sequence still raises."""
    with pytest.raises(ValueError, match="missing required columns"):
        ops.mev_sandwich(ctx, ops.MevSandwichParams(trades=[{"block": 1}]))


# ---------------------------------------------------------------------------
# sequencer-latency
# ---------------------------------------------------------------------------


def test_sequencer_latency_summarises_production_interval_and_ingestion_delay(
    lake: Path, ctx: CapabilityContext
) -> None:
    records: list[Record] = [
        BookTicker(
            source="base_onchain",
            symbol="base_onchain:USDC-USDbC",
            symbol_raw="USDC-USDbC",
            local_ts=_BASE_NS + i * _1S + _1S // 2,
            asset_class=AssetClass.CRYPTO,
            source_ts=_BASE_NS + i * _1S,
            bid_px=1.0,
            bid_sz=1.0,
            ask_px=1.0,
            ask_sz=1.0,
        )
        for i in range(4)
    ]
    asyncio.run(_write(lake, records))

    out = ops.sequencer_latency(ctx, ops.SequencerLatencyParams(source="base_onchain"))
    assert out["metric"].to_list() == ["production_interval", "ingestion_delay"]
    by_metric = dict(zip(out["metric"].to_list(), out["avg_seconds"].to_list(), strict=True))
    assert by_metric["production_interval"] == pytest.approx(1.0)
    assert by_metric["ingestion_delay"] == pytest.approx(0.5)


def test_sequencer_latency_reads_a_blank_exchange_as_the_default_chain(
    ctx: CapabilityContext,
) -> None:
    """An explicitly empty exchange means "the default chain", not "no chain".

    Both the CLI and the REST route normalised it, and without that the query filters on a
    source no record carries and an operator reads an empty lake instead of a typo.
    """
    empty = ops.sequencer_latency(ctx, ops.SequencerLatencyParams(source="   "))
    assert empty.columns == ["metric", "avg_seconds", "max_seconds", "std_seconds"]


# ---------------------------------------------------------------------------
# peg-deviation — the capability with two modes and one return kind
# ---------------------------------------------------------------------------


def test_peg_deviation_evaluates_one_price_without_reading_the_lake(
    ctx: CapabilityContext,
) -> None:
    out = ops.peg_deviation(ctx, ops.PegDeviationParams(price=0.98))
    assert out.height == 1
    assert out["price"][0] == pytest.approx(0.98)
    assert out["deviation_pct"][0] == pytest.approx(0.02)
    assert out["is_alert_triggered"][0]
    assert out["timestamp"][0] is None


def test_peg_deviation_takes_the_midpoint_when_given_a_bid_and_an_ask(
    ctx: CapabilityContext,
) -> None:
    out = ops.peg_deviation(ctx, ops.PegDeviationParams(bid=0.98, ask=1.00))
    assert out["price"][0] == pytest.approx(0.99)


def test_peg_deviation_measures_against_the_requested_target_in_pure_mode(
    ctx: CapabilityContext,
) -> None:
    out = ops.peg_deviation(ctx, ops.PegDeviationParams(price=0.98, target=0.98))
    assert out["deviation_pct"][0] == pytest.approx(0.0)
    assert not out["is_alert_triggered"][0]


def test_peg_deviation_reads_the_lake_when_only_a_symbol_is_given(
    lake: Path, ctx: CapabilityContext
) -> None:
    """The mode the crypto CLI exposed and REST and MCP did not. Losing it was the risk."""
    asyncio.run(
        _write(
            lake,
            [
                _book_ticker(_BASE_NS, 0.979, 0.981),
                _book_ticker(_BASE_NS + _1S, 0.999, 1.001),
            ],
        )
    )
    out = ops.peg_deviation(ctx, ops.PegDeviationParams(symbol="base_onchain:USDC-USDbC"))
    assert out.height == 2
    assert out["price"].to_list() == pytest.approx([0.98, 1.0])
    assert out["is_alert_triggered"].to_list() == [True, False]
    assert out["timestamp"][0] == _BASE_NS


def test_peg_deviation_measures_against_the_requested_target_in_lake_mode_too(
    lake: Path, ctx: CapabilityContext
) -> None:
    """``target`` is REST's and MCP's parameter and the lake function hard-codes 1.0.

    Accepting it and dropping it on one of the two modes would answer a question the caller
    did not ask, with nothing in the result to say so.
    """
    asyncio.run(_write(lake, [_book_ticker(_BASE_NS, 0.979, 0.981)]))
    out = ops.peg_deviation(
        ctx, ops.PegDeviationParams(symbol="base_onchain:USDC-USDbC", target=0.98)
    )
    assert out["deviation_pct"][0] == pytest.approx(0.0)
    assert not out["is_alert_triggered"][0]


def test_peg_deviation_answers_both_of_its_modes_with_the_same_columns(
    lake: Path, ctx: CapabilityContext
) -> None:
    """One capability has one ``ReturnKind``, so the two modes have to agree on a shape."""
    asyncio.run(_write(lake, [_book_ticker(_BASE_NS, 0.979, 0.981)]))
    pure = ops.peg_deviation(ctx, ops.PegDeviationParams(price=0.98))
    from_lake = ops.peg_deviation(ctx, ops.PegDeviationParams(symbol="base_onchain:USDC-USDbC"))
    assert pure.columns == from_lake.columns == list(ops._PEG_COLUMNS)
    assert pure.schema == from_lake.schema


def test_peg_deviation_refuses_a_request_naming_neither_a_price_nor_a_symbol(
    ctx: CapabilityContext,
) -> None:
    with pytest.raises(ValueError, match="either a mid price"):
        ops.peg_deviation(ctx, ops.PegDeviationParams())


# ---------------------------------------------------------------------------
# lending-stress
# ---------------------------------------------------------------------------


def test_lending_stress_reports_the_health_factor_before_and_after_the_haircut(
    ctx: CapabilityContext,
) -> None:
    out = ops.lending_stress(
        ctx,
        ops.LendingStressParams(
            collateral_usd=10_000.0,
            debt_usd=5_000.0,
            liquidation_threshold=0.8,
            haircut_pct=0.2,
        ),
    )
    assert out["current_health_factor"] == pytest.approx(1.6)
    assert out["simulated_health_factor"] == pytest.approx(1.28)
    assert out["is_liquidatable"] is False
    assert out["simulated_is_liquidatable"] is False


def test_lending_stress_echoes_the_inputs_because_one_of_them_is_normalised(
    ctx: CapabilityContext,
) -> None:
    """``20`` and ``0.20`` both mean twenty percent, so a result alone cannot be checked."""
    as_percent = ops.lending_stress(
        ctx,
        ops.LendingStressParams(
            collateral_usd=10_000.0,
            debt_usd=5_000.0,
            liquidation_threshold=0.8,
            haircut_pct=20.0,
        ),
    )
    assert as_percent["haircut_pct"] == 20.0
    assert as_percent["simulated_health_factor"] == pytest.approx(1.28)


# ---------------------------------------------------------------------------
# collect — and what STREAM means in code
# ---------------------------------------------------------------------------


def _collect_params(**overrides: Any) -> ops.CollectParams:
    fields: dict[str, Any] = {
        "sources": ("deribit",),
        "symbols": ("BTC-PERPETUAL",),
        "channels": ("trade",),
    }
    fields.update(overrides)
    return ops.CollectParams(**fields)


def test_collect_hands_back_a_run_that_has_not_started(ctx: CapabilityContext) -> None:
    """The property that stops a capability hanging a surface that cannot host it.

    Building the subscription resolves connectors and opens no socket, so a REST route can
    reject an unbounded request after the parameters are known and before any I/O happens.
    """
    sub = ops.collect(ctx, _collect_params(duration_seconds=1.5))
    assert isinstance(sub, ops.Subscription)
    assert sub.sources == ("deribit",)
    assert sub.channels == ("trade",)
    assert sub.duration_seconds == 1.5
    assert asyncio.iscoroutinefunction(sub.begin)


def test_collect_refuses_a_read_only_surface(readonly_ctx: CapabilityContext) -> None:
    """A surface that will not trust a caller with mutating SQL cannot trust one to write."""
    with pytest.raises(PermissionError, match="read-only"):
        ops.collect(readonly_ctx, _collect_params())


def test_collect_for_equities_refuses_a_read_only_surface(
    readonly_ctx: CapabilityContext,
) -> None:
    with pytest.raises(PermissionError, match="read-only"):
        ops.collect_equities(readonly_ctx, _collect_params(sources=("stooq",)))


@pytest.mark.parametrize("missing", ["sources", "symbols", "channels"])
def test_collect_refuses_an_empty_list_rather_than_returning_at_once(
    ctx: CapabilityContext, missing: str
) -> None:
    """``collect([])`` closes the sink and returns, which reads as a run that already ended."""
    with pytest.raises(ValueError, match=missing):
        ops.collect(ctx, _collect_params(**{missing: ()}))


def test_the_equity_half_accepts_the_crypto_only_dead_letter_path_and_ignores_it(
    ctx: CapabilityContext,
) -> None:
    """One struct serves both, on the argument ``SlippageParams`` makes for ``size_unit``.

    Equity providers have no dead-letter queue, so there is nothing for the field to mean on
    that side — and dropping it would delete the only way to place a crypto run's report.

    ``ohlcv`` and not the fixture's default ``trade``: ``stooq`` now declares what it serves
    and refuses a construction whose every channel is outside that set, so this used to build
    a provider that would have polled and emitted nothing. The dead channel this test was
    walking into is the one ``tests/conformance/test_provider_channels.py`` exists for.
    """
    sub = ops.collect_equities(
        ctx,
        _collect_params(
            sources=("stooq",),
            symbols=("AAPL",),
            channels=("ohlcv",),
            dlq_report_path="/tmp/dlq.json",
        ),
    )
    assert isinstance(sub, ops.Subscription)


def test_the_lake_root_comes_from_the_context_and_not_from_a_parameter() -> None:
    """A caller-supplied lake root in a published schema is a caller choosing where to write."""
    assert "data_dir" not in ops.CollectParams.__struct_fields__
    assert "dlq_report_path" in ops.CollectParams.__struct_fields__


async def test_a_bounded_subscription_stops_when_its_bound_expires() -> None:
    async def _forever() -> None:
        await asyncio.sleep(3600)

    sub = ops.Subscription(
        sources=("fake",), channels=("trade",), duration_seconds=0.02, begin=_forever
    )
    await asyncio.wait_for(sub.run(), timeout=5.0)


async def test_an_unbounded_subscription_runs_until_it_is_cancelled() -> None:
    """The CLI's idiom: no bound, and SIGINT is what ends it.

    The cancellation must reach the caller rather than being swallowed, or a surface can
    never tell "I stopped it" from "it finished".
    """
    started = asyncio.Event()

    async def _forever() -> None:
        started.set()
        await asyncio.sleep(3600)

    sub = ops.Subscription(
        sources=("fake",), channels=("trade",), duration_seconds=None, begin=_forever
    )
    task = asyncio.create_task(sub.run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# collect-market
# ---------------------------------------------------------------------------


def _market_params(**overrides: Any) -> ops.CollectMarketParams:
    fields: dict[str, Any] = {"sources": ("binance",), "channels": ("trade",), "top": 10}
    fields.update(overrides)
    return ops.CollectMarketParams(**fields)


def test_collect_market_judges_everything_it_can_before_a_subscription_exists(
    ctx: CapabilityContext,
) -> None:
    sub = ops.collect_market(ctx, _market_params())
    assert isinstance(sub, ops.Subscription)
    assert sub.sources == ("binance",)


def test_sbe_collect_market_refuses_before_universe_lookup(ctx):
    fast = CapabilityContext(
        catalog=ctx.catalog,
        asset_class=AssetClass.CRYPTO,
        settings=Settings(
            data_dir=ctx.settings.data_dir,
            binance_wire_format="sbe",
            binance_sbe_api_key="fixture-key",
        ),
    )
    with pytest.raises(ConfigError, match="native symbols"):
        ops.collect_market(fast, _market_params())


def test_collect_preserves_connector_supplied_transport(ctx, monkeypatch):
    from crocodile.core.ingest.transport import FakeTransport
    from crocodile.crypto.exchanges.binance.connector import BinanceConnector

    custom = FakeTransport([])
    created = []

    def factory(**kwargs):
        conn = BinanceConnector(
            kwargs["symbols"], kwargs["channels"], kwargs["out"], kwargs["registry"]
        )
        conn.transport = custom
        created.append(conn)
        return conn

    monkeypatch.setattr("crocodile.crypto.exchanges.factory.make_connector", factory)
    ops.collect(ctx, _collect_params(sources=("binance",), symbols=("BTCUSDT",)))
    assert created[0].transport is custom


@pytest.mark.parametrize(
    ("params", "message"),
    [
        pytest.param({"top": None}, "either top", id="neither_slice"),
        pytest.param({"all_symbols": True}, "not both", id="both_slices"),
        pytest.param({"channels": ()}, "channels", id="no_channels"),
        pytest.param({"kinds": ("nonsense",)}, "nonsense", id="unknown_kind"),
    ],
)
def test_collect_market_rejects_a_malformed_slice_up_front(
    ctx: CapabilityContext, params: dict[str, Any], message: str
) -> None:
    """Resolving the slice is asynchronous; refusing a malformed one is not.

    Everything judgeable without the venue is judged synchronously, so a bad request fails
    before a subscription is handed back rather than on the surface's first await.
    """
    with pytest.raises(ValueError, match=message):
        ops.collect_market(ctx, _market_params(**params))


def test_collect_market_refuses_a_read_only_surface(readonly_ctx: CapabilityContext) -> None:
    with pytest.raises(PermissionError, match="read-only"):
        ops.collect_market(readonly_ctx, _market_params())


# ---------------------------------------------------------------------------
# collect-market, the equity half M3 landed
# ---------------------------------------------------------------------------
# The reference resolution and the ranking are tested where they live —
# `tests/equity/test_reference_universe.py` — so what these exercise is the adapter's own
# division of labour: what it refuses synchronously, and what it resolves inside the run.


_SEC_AGENT = "Crocodile-Test/1.0 (tests@example.com)"


@pytest.fixture
def equity_ctx(lake: Path) -> Iterator[CapabilityContext]:
    """A trusted equity context whose lake holds two symbols' worth of daily bars.

    The bars are the point: the ``top`` slice ranks on this deployment's own stored volume,
    so an equity ``collect-market`` needs a lake with something in it before it can name a
    single symbol.
    """

    async def _write() -> None:
        sink = ParquetSink(lake, max_buffer_rows=10_000, flush_interval_seconds=9999)
        for offset, (symbol, volume) in enumerate({"AAPL": 100.0, "MSFT": 900.0}.items()):
            await sink.put(
                OHLCV(
                    source="stooq",
                    symbol=symbol,
                    symbol_raw=symbol,
                    source_ts=_BASE_NS + offset,
                    local_ts=_BASE_NS + offset,
                    asset_class=AssetClass.EQUITY,
                    interval="1d",
                    open=1.0,
                    high=2.0,
                    low=0.5,
                    close=1.5,
                    volume=volume,
                )
            )
        await sink.flush()

    asyncio.run(_write())
    catalog = Catalog(lake)
    try:
        yield CapabilityContext(
            catalog=catalog,
            settings=Settings(data_dir=lake, sec_user_agent=_SEC_AGENT),
            asset_class=AssetClass.EQUITY,
        )
    finally:
        catalog.close()


def _equity_market_params(**overrides: Any) -> ops.CollectMarketParams:
    fields: dict[str, Any] = {"sources": ("stooq",), "channels": ("ohlcv",), "top": 2}
    fields.update(overrides)
    return ops.CollectMarketParams(**fields)


@pytest.fixture
def _equity_universe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three listings standing in for the two bulk reference downloads."""
    from crocodile.equity.providers.sec_edgar.client import SecCompanyTicker
    from crocodile.equity.providers.tiingo.client import TiingoTicker
    from crocodile.equity.reference import universe as reference

    rows = [("AAPL", "Stock"), ("MSFT", "Stock"), ("SPY", "ETF")]
    evidence = reference.ReferenceEvidence(
        as_of_ns=_BASE_NS,
        by_source={
            reference.SOURCE_SEC: reference.instruments_from_sec(
                [
                    SecCompanyTicker(cik=index + 1, ticker=ticker, title=ticker)
                    for index, (ticker, _) in enumerate(rows)
                ],
                as_of_ns=_BASE_NS,
            ),
            reference.SOURCE_TIINGO: reference.instruments_from_tiingo(
                [
                    TiingoTicker(
                        ticker=ticker,
                        exchange="NASDAQ",
                        asset_type=asset_type,
                        price_currency="USD",
                        start_date="1990-01-02",
                        end_date="2024-01-01",
                    )
                    for ticker, asset_type in rows
                ],
                as_of_ns=_BASE_NS,
            ),
        },
        currency=dict.fromkeys((ticker for ticker, _ in rows), "USD"),
    )

    async def _fake(**kwargs: Any) -> Any:
        assert kwargs["sec_user_agent"] == _SEC_AGENT
        return evidence

    monkeypatch.setattr(reference, "fetch_bulk_evidence", _fake)


def _symbols_the_run_resolved(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture the symbol list ``begin()`` builds, without opening a socket for it."""
    seen: list[list[str]] = []

    def _make_provider(*, symbols: list[str], **_: Any) -> Any:
        seen.append(list(symbols))
        raise _StopBeforeConnecting

    async def _never_collect(*_: Any, **__: Any) -> None:  # pragma: no cover
        raise AssertionError("the run must not reach the collector in these tests")

    from crocodile.equity.providers import factory as equity_factory

    monkeypatch.setattr(equity_factory, "make_provider", _make_provider)
    monkeypatch.setattr(ops, "collect_equity", _never_collect)
    return seen


class _StopBeforeConnecting(Exception):
    """Raised by the fake provider factory once the symbol list has been observed."""


def test_equity_collect_market_judges_everything_it_can_before_a_subscription_exists(
    equity_ctx: CapabilityContext, _equity_universe: None
) -> None:
    sub = ops.collect_market_equities(equity_ctx, _equity_market_params())
    assert isinstance(sub, ops.Subscription)
    assert sub.sources == ("stooq",)
    assert sub.channels == ("ohlcv",)


@pytest.mark.parametrize(
    ("params", "message"),
    [
        pytest.param({"top": None}, "either top", id="neither_slice"),
        pytest.param({"all_symbols": True}, "not both", id="both_slices"),
        pytest.param({"channels": ()}, "channels", id="no_channels"),
        pytest.param({"sources": ()}, "sources", id="no_sources"),
        pytest.param({"kinds": ("perpetual",)}, "unknown instrument kind", id="a_crypto_kind"),
    ],
)
def test_equity_collect_market_rejects_a_malformed_slice_up_front(
    equity_ctx: CapabilityContext, params: dict[str, Any], message: str
) -> None:
    """Same division as the crypto half: resolving is asynchronous, refusing is not."""
    with pytest.raises(ValueError, match=message):
        ops.collect_market_equities(equity_ctx, _equity_market_params(**params))


def test_equity_collect_market_refuses_a_missing_sec_contact_string_before_awaiting(
    lake: Path,
) -> None:
    """Configuration must not first fail on an await inside somebody else's event loop."""
    catalog = Catalog(lake)
    try:
        ctx = CapabilityContext(
            catalog=catalog, settings=Settings(data_dir=lake), asset_class=AssetClass.EQUITY
        )
        with pytest.raises(ConfigError, match="CROCODILE_SEC_USER_AGENT"):
            ops.collect_market_equities(ctx, _equity_market_params())
    finally:
        catalog.close()


def test_equity_collect_market_refuses_a_read_only_surface(readonly_ctx: CapabilityContext) -> None:
    with pytest.raises(PermissionError, match="read-only"):
        ops.collect_market_equities(readonly_ctx, _equity_market_params())


def test_the_top_slice_resolves_the_most_traded_symbols_in_this_lake(
    equity_ctx: CapabilityContext, _equity_universe: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ranking's source, named and asserted: the lake's own stored ohlcv bars.

    MSFT leads AAPL because of what was collected, and SPY — which nothing has been collected
    for — falls out of a two-symbol slice rather than displacing a symbol there is evidence
    for.
    """
    seen = _symbols_the_run_resolved(monkeypatch)
    sub = ops.collect_market_equities(equity_ctx, _equity_market_params(top=2))
    with pytest.raises(_StopBeforeConnecting):
        asyncio.run(sub.begin())
    assert seen == [["MSFT", "AAPL"]]


def test_the_top_slice_refuses_a_lake_with_no_bars_to_rank(
    lake: Path, _equity_universe: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An arbitrary N tickers wearing the word "top" is the answer this refuses to give."""
    _symbols_the_run_resolved(monkeypatch)
    catalog = Catalog(lake)
    try:
        ctx = CapabilityContext(
            catalog=catalog,
            settings=Settings(data_dir=lake, sec_user_agent=_SEC_AGENT),
            asset_class=AssetClass.EQUITY,
        )
        sub = ops.collect_market_equities(ctx, _equity_market_params(top=2))
        with pytest.raises(ValueError, match="collect or backfill"):
            asyncio.run(sub.begin())
    finally:
        catalog.close()


def test_the_all_slice_is_capped_on_a_volume_ordered_list_rather_than_an_alphabetical_one(
    equity_ctx: CapabilityContext, _equity_universe: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``max_symbols`` caps how much of a market is watched, which is what its docstring is about.

    Over a merged reference universe the natural order is alphabetical, so a cap of two would
    subscribe to AAPL and MSFT by spelling rather than by liquidity — and on a real universe
    it would be five hundred tickers beginning with A. Ordering by the same evidence first is
    what makes the cap take the part of the market anyone meant.
    """
    seen = _symbols_the_run_resolved(monkeypatch)
    sub = ops.collect_market_equities(
        equity_ctx, _equity_market_params(top=None, all_symbols=True, max_symbols=2)
    )
    with pytest.raises(_StopBeforeConnecting):
        asyncio.run(sub.begin())
    assert seen == [["MSFT", "AAPL"]]


def test_the_all_slice_still_answers_when_the_lake_has_no_evidence(
    lake: Path, _equity_universe: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticker order is what is left when nothing better exists, not the default.

    The ``all`` slice is defined without the ranking, so it degrades rather than refusing —
    which is the opposite call from ``top``, whose whole meaning is the ordering.
    """
    seen = _symbols_the_run_resolved(monkeypatch)
    catalog = Catalog(lake)
    try:
        ctx = CapabilityContext(
            catalog=catalog,
            settings=Settings(data_dir=lake, sec_user_agent=_SEC_AGENT),
            asset_class=AssetClass.EQUITY,
        )
        sub = ops.collect_market_equities(
            ctx, _equity_market_params(top=None, all_symbols=True, max_symbols=3)
        )
        with pytest.raises(_StopBeforeConnecting):
            asyncio.run(sub.begin())
    finally:
        catalog.close()
    assert seen == [["AAPL", "MSFT", "SPY"]]


def test_the_kind_filter_narrows_the_slice_in_the_equitys_own_vocabulary(
    equity_ctx: CapabilityContext, _equity_universe: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _symbols_the_run_resolved(monkeypatch)
    sub = ops.collect_market_equities(
        equity_ctx, _equity_market_params(top=None, all_symbols=True, kinds=("ETF",))
    )
    with pytest.raises(_StopBeforeConnecting):
        asyncio.run(sub.begin())
    assert seen == [["SPY"]]


def test_a_slice_that_matches_nothing_says_so_rather_than_subscribing_to_an_empty_list(
    equity_ctx: CapabilityContext, _equity_universe: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty subscription reads to a caller as a run that started and ended."""
    _symbols_the_run_resolved(monkeypatch)
    sub = ops.collect_market_equities(
        equity_ctx, _equity_market_params(top=None, all_symbols=True, quote="JPY")
    )
    with pytest.raises(ValueError, match="no symbols matched"):
        asyncio.run(sub.begin())


def test_the_equity_half_rests_on_what_it_writes_and_not_on_how_it_chose_the_symbols() -> None:
    """Renamed and re-pinned: this asserted ``reference_merge`` and inverted the trust order.

    The old argument was that this implementation *rests on* a symbol set merged out of three
    registries and a ranking computed from stored bars, and that no venue published either.
    Both halves are true and neither is about the returned answer. ``basis`` is what makes
    ``prov`` a bound a caller can read — ``worst_provenance`` writes the rule down, "a
    derivation can never be more trustworthy than its worst input" — and NATIVE over a basis
    registered DERIVED broke it, uniquely among the ninety-one implementations, with nothing
    asking. ``test_no_implementation_claims_a_provenance_stronger_than_its_basis`` now asks.

    What this returns is a subscription that writes what a provider streamed, and every
    record carries the provider's own tail; none of them says ``reference_merge``. How the
    symbol list was chosen is a property of the *request*, argued at length in
    :func:`~crocodile.capabilities.ops.collect_market_equities`, and it stays there.
    """
    impl = REGISTRY["collect-market"].impls[AssetClass.EQUITY]
    assert impl.fn is ops.collect_market_equities
    assert impl.prov is Provenance.NATIVE
    assert impl.basis == "native"
    assert impl.basis == REGISTRY["collect-market"].impls[AssetClass.CRYPTO].basis


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------


def _backfill_params(**overrides: Any) -> ops.BackfillParams:
    fields: dict[str, Any] = {
        "source": "binance",
        "channel": "trade",
        "symbols": ("BTCUSDT",),
        "start_ns": _BASE_NS,
        "end_ns": _BASE_NS + _1S,
    }
    fields.update(overrides)
    return ops.BackfillParams(**fields)


def test_backfill_returns_its_run_unstarted_so_the_surface_owns_the_loop(
    ctx: CapabilityContext,
) -> None:
    """An adapter that called ``asyncio.run`` would work on the CLI and raise inside REST."""
    run = ops.backfill(ctx, _backfill_params())
    try:
        assert inspect.iscoroutine(run)
    finally:
        run.close()


@pytest.mark.parametrize("adapter", ["backfill", "backfill_equities"])
def test_backfill_refuses_an_inverted_time_range(ctx: CapabilityContext, adapter: str) -> None:
    """Not an empty result: the venue helpers page forward and answer differently to it."""
    with pytest.raises(ValueError, match="is after"):
        getattr(ops, adapter)(ctx, _backfill_params(start_ns=_BASE_NS + _1S, end_ns=_BASE_NS))


@pytest.mark.parametrize("adapter", ["backfill", "backfill_equities"])
def test_backfill_refuses_a_read_only_surface(
    readonly_ctx: CapabilityContext, adapter: str
) -> None:
    with pytest.raises(PermissionError, match="read-only"):
        getattr(ops, adapter)(readonly_ctx, _backfill_params())


class _CountingSink(Sink):
    """A sink that remembers what it was given and whether anyone closed it."""

    def __init__(self) -> None:
        self.records: list[Record] = []
        self.closed = False

    async def put(self, record: Record) -> None:
        self.records.append(record)

    async def flush(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class _FakeProvider:
    """A provider whose history is a fixed list and whose session is closeable."""

    def __init__(self, records: list[Record]) -> None:
        self._records = records
        self.closed = False

    async def backfill(
        self, channel: str, symbol: str, start_ns: int, end_ns: int
    ) -> AsyncIterator[Record]:
        for record in self._records:
            yield record

    async def close(self) -> None:
        self.closed = True


async def test_backfill_for_equities_drains_the_provider_history_into_the_lake(
    ctx: CapabilityContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The equity half nobody had assembled: the provider API was always there.

    ``Provider.backfill`` is on the base class and ``stooq`` and ``msn_money`` implement it,
    so this is orchestration a port supplies rather than a method Phase 3 has to invent —
    which is why the capability declares both halves instead of being scheduled.
    """
    records = [_trade(_BASE_NS + i * _1S, 100.0 + i, 1.0, str(i)) for i in range(3)]
    provider = _FakeProvider(records)
    monkeypatch.setattr(
        "crocodile.equity.providers.factory.make_provider",
        lambda **kwargs: provider,
    )
    sink = _CountingSink()
    written = await ops._drain_provider_backfill("stooq", _backfill_params(), sink)
    assert written == 3
    assert provider.closed is True


async def test_backfill_for_equities_closes_the_sink_even_when_the_provider_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial fetch has still written rows; rows in a sink nobody closed are rows lost."""

    class _Exploding(_FakeProvider):
        async def backfill(
            self, channel: str, symbol: str, start_ns: int, end_ns: int
        ) -> AsyncIterator[Record]:
            yield _trade(_BASE_NS, 100.0, 1.0, "0")
            raise RuntimeError("venue said no")

    provider = _Exploding([])
    monkeypatch.setattr(
        "crocodile.equity.providers.factory.make_provider",
        lambda **kwargs: provider,
    )
    sink = _CountingSink()
    with pytest.raises(RuntimeError, match="venue said no"):
        await ops._drain_provider_backfill("stooq", _backfill_params(), sink)
    assert sink.closed is True
    assert provider.closed is True


# ---------------------------------------------------------------------------
# replay, export, resample
# ---------------------------------------------------------------------------


def test_replay_merges_the_stored_channels_in_time_order(
    lake: Path, ctx: CapabilityContext
) -> None:
    asyncio.run(
        _write(
            lake,
            [
                _trade(_BASE_NS + 2 * _1S, 102.0, 1.0, "c"),
                _trade(_BASE_NS, 100.0, 1.0, "a"),
                _trade(_BASE_NS + _1S, 101.0, 1.0, "b"),
            ],
        )
    )
    records = list(
        ops.replay(
            ctx,
            ops.ReplayParams(
                channels=("trade",),
                symbols=("deribit:BTC-PERPETUAL",),
                start_ns=0,
                end_ns=_BASE_NS + 10 * _1S,
            ),
        )
    )
    assert [r.local_ts for r in records] == [_BASE_NS, _BASE_NS + _1S, _BASE_NS + 2 * _1S]


def test_replay_honours_a_limit_across_the_merged_stream(
    lake: Path, ctx: CapabilityContext
) -> None:
    """The per-channel bound is an optimisation; the answer's bound is applied once, globally."""
    asyncio.run(_write(lake, [_trade(_BASE_NS + i * _1S, 100.0, 1.0, str(i)) for i in range(5)]))
    records = list(
        ops.replay(
            ctx,
            ops.ReplayParams(
                channels=("trade",),
                symbols=("deribit:BTC-PERPETUAL",),
                start_ns=0,
                end_ns=_BASE_NS + 10 * _1S,
                limit=2,
            ),
        )
    )
    assert len(records) == 2


@pytest.mark.parametrize("empty", ["channels", "symbols"])
def test_replay_answers_an_empty_request_without_touching_the_lake(
    ctx: CapabilityContext, empty: str
) -> None:
    fields: dict[str, Any] = {
        "channels": ("trade",),
        "symbols": ("deribit:BTC-PERPETUAL",),
        "start_ns": 0,
        "end_ns": _BASE_NS,
    }
    fields[empty] = ()
    assert list(ops.replay(ctx, ops.ReplayParams(**fields))) == []


def test_export_writes_the_requested_rows_and_returns_where_they_went(
    lake: Path, ctx: CapabilityContext, tmp_path: Path
) -> None:
    prints = [_trade(_BASE_NS + i * _1S, 100.0 + i, 1.0, str(i)) for i in range(3)]
    asyncio.run(_write(lake, prints))
    dest = tmp_path / "out" / "trades.csv"
    where = ops.export(
        ctx,
        ops.ExportParams(
            channel="trade",
            symbols=("deribit:BTC-PERPETUAL",),
            start_ns=0,
            end_ns=_BASE_NS + 10 * _1S,
            dest=str(dest),
            fmt="csv",
        ),
    )
    assert where == str(dest)
    assert dest.is_file()
    assert pl.read_csv(dest).height == 3


def test_export_encodes_the_nested_columns_every_record_now_carries(
    lake: Path, ctx: CapabilityContext, tmp_path: Path
) -> None:
    """Why one implementation serves both, and why it is the crypto one.

    ``prov_inputs`` is a ``list[str]`` on every canonical record. The equity twin casts
    lists to a joined string, which cannot express the ``list[struct]`` of a book channel;
    the crypto one JSON-encodes and exports either.
    """
    asyncio.run(_write(lake, [_trade(_BASE_NS, 100.0, 1.0, "a")]))
    dest = tmp_path / "trades.csv"
    ops.export(
        ctx,
        ops.ExportParams(
            channel="trade",
            symbols=("deribit:BTC-PERPETUAL",),
            start_ns=0,
            end_ns=_BASE_NS + _1S,
            dest=str(dest),
            fmt="csv",
        ),
    )
    assert "prov_inputs" in pl.read_csv(dest).columns


def test_export_refuses_a_read_only_surface(
    lake: Path, readonly_ctx: CapabilityContext, tmp_path: Path
) -> None:
    """The one writer this batch left unguarded, and the only one whose path is a parameter.

    Measured on the shipped build: ``GET /api/v1/export?channel=trade&symbols=…&dest=…&
    fmt=csv`` returned ``200 {"result":"/…/pwned.csv"}`` with a 5 960-byte file on disk.
    ``dest`` is caller-chosen and unvalidated, so substituting ``~/.zshrc`` overwrites it —
    an unauthenticated network caller writing an arbitrary path as the server's user.

    ``ExportParams``' own docstring named this hazard and left it to the surfaces, which is
    the split that produced it: ``_refuse_readonly`` guarded ``collect``,
    ``collect-market`` and ``backfill`` — the three that write the *lake* — and nobody
    re-derived that a file write is the same trust question. It is the same question, and
    it is answered in the same place, so a fourth surface cannot forget it either.
    """
    dest = tmp_path / "pwned.csv"
    prints = [_trade(_BASE_NS, 100.0, 1.0, "a")]
    asyncio.run(_write(lake, prints))

    with pytest.raises(PermissionError, match="read-only"):
        ops.export(
            readonly_ctx,
            ops.ExportParams(
                channel="trade",
                symbols=("deribit:BTC-PERPETUAL",),
                start_ns=0,
                end_ns=_BASE_NS + _1S,
                dest=str(dest),
                fmt="csv",
            ),
        )

    assert not dest.exists(), "the refusal has to happen before anything is written"


def test_every_capability_that_writes_outside_the_process_refuses_a_read_only_surface(
    readonly_ctx: CapabilityContext, tmp_path: Path
) -> None:
    """The census that would have caught ``export``, rather than four separate tests.

    Each of the tests above names one adapter, which is how the fourth one came to have no
    test at all. This asks the question the other way round — of every adapter this batch
    declares, which ones write something a read-only surface must not be able to start? —
    so a fifth writer arrives already covered or already failing.
    """
    writers = {
        "collect": _collect_params(),
        "collect_equities": _collect_params(sources=("stooq",)),
        "collect_market": _market_params(),
        # The fifth writer, arriving with M3. Added here rather than only in its own test for
        # the reason this census exists: the named tests are how the fourth one came to have
        # none at all.
        "collect_market_equities": _equity_market_params(),
        "backfill": _backfill_params(),
        "backfill_equities": _backfill_params(),
        "export": ops.ExportParams(
            channel="trade",
            symbols=("deribit:BTC-PERPETUAL",),
            start_ns=0,
            end_ns=_BASE_NS,
            dest=str(tmp_path / "out.csv"),
            fmt="csv",
        ),
    }
    unguarded = []
    for adapter, params in writers.items():
        try:
            getattr(ops, adapter)(readonly_ctx, params)
        except PermissionError:
            continue
        except Exception:  # any other failure is not the refusal being asked about
            pass
        unguarded.append(adapter)
    assert not unguarded, (
        f"{unguarded} write outside this process and did not refuse a read-only surface; "
        f"call _refuse_readonly before anything is constructed"
    )


def test_export_refuses_an_unsupported_format(ctx: CapabilityContext, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported fmt"):
        ops.export(
            ctx,
            ops.ExportParams(
                channel="trade",
                symbols=(),
                start_ns=0,
                end_ns=1,
                dest=str(tmp_path / "x.xyz"),
                fmt="xyz",
            ),
        )


def test_resample_aggregates_stored_prints_into_bars(lake: Path, ctx: CapabilityContext) -> None:
    asyncio.run(
        _write(
            lake,
            [
                _trade(_BASE_NS, 100.0, 1.0, "a"),
                _trade(_BASE_NS + _1S // 2, 102.0, 2.0, "b"),
                _trade(_BASE_NS + 60 * _1S, 101.0, 1.0, "c"),
            ],
        )
    )
    bars = ops.resample(
        ctx,
        ops.ResampleParams(
            symbol="deribit:BTC-PERPETUAL",
            start_ns=_BASE_NS,
            end_ns=_BASE_NS + 120 * _1S,
            interval="1m",
        ),
    )
    assert bars.height == 2
    assert bars["open"][0] == pytest.approx(100.0)
    assert bars["high"][0] == pytest.approx(102.0)
    assert bars["volume"][0] == pytest.approx(3.0)
    assert bars["prov_basis"][0] == "ohlcv_from_trades"


def test_resample_declares_its_inputs_native_while_its_rows_name_the_method(
    lake: Path, ctx: CapabilityContext
) -> None:
    """``Impl.basis`` names where the inputs came from; the method applied is on the rows.

    A trade print is reported by the venue, which is the same reading that makes
    ``indicators`` native, and ``ohlcv_from_trades`` is measured per bar rather than
    promised once at the declaration.
    """
    for impl in REGISTRY["resample"].impls.values():
        assert impl.basis == "native"
        assert impl.prov is Provenance.DERIVED
