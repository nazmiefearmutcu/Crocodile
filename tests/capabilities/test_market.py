"""The market-structure batch: its adapters, and the claims its declarations make.

Nothing here touches a venue. Four of the six capabilities reach the network in production,
and a test that reached Yahoo or a ccxt REST endpoint would fail on a plane, fail in CI
behind a proxy, and pass or fail depending on what BTC did this morning — so every network
edge is replaced at the module boundary and the adapter's own argument shuffling, filtering
and framing is what gets exercised. ``open-interest`` is the exception that needs no
mocking at all: its input is the lake, so it gets a real one, written into ``tmp_path``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import msgspec
import polars as pl
import pytest

from crocodile.capabilities import market
from crocodile.core.capability import (
    IRREDUCIBLE,
    PENDING_SYMMETRY,
    REGISTRY,
    AssetClass,
    CapabilityContext,
    ReturnKind,
    run_to_completion,
)
from crocodile.core.config import Settings
from crocodile.core.errors import ConfigError
from crocodile.core.schema.provenance import Provenance, level_for, provenance_fields
from crocodile.core.schema.records import DepthProfile, OpenInterest, OptionsChain
from crocodile.core.schema.records import OHLCV, DepthProfile, OpenInterest
from crocodile.core.store.catalog import Catalog
from crocodile.core.store.parquet_sink import ParquetSink
from crocodile.core.util.time import now_ns
from crocodile.crypto.instruments.registry import Instrument, Kind
from crocodile.equity.providers.openfigi.models import FigiRecord
from crocodile.equity.providers.sec_edgar.client import SecCompanyTicker
from crocodile.equity.providers.tiingo.client import TiingoTicker
from crocodile.equity.reference import universe as reference

_BASE_NS = 1_704_067_200_000_000_000

_MARKET_CAPABILITIES = (
    "list-exchanges",
    "markets",
    "universe",
    "census",
    "open-interest",
    "depth",
)


def _ctx(catalog: Any = None, asset_class: AssetClass = AssetClass.CRYPTO) -> CapabilityContext:
    """A context for the capabilities that do not read the lake.

    ``catalog=None`` is honest for those: they never dereference it, and handing them a real
    ``Catalog`` would hide an adapter that started to.
    """
    return CapabilityContext(catalog=catalog, settings=Settings(), asset_class=asset_class)


def _instrument(symbol: str, kind: Kind, base: str, quote: str) -> Instrument:
    return Instrument(
        canonical=f"binance:{symbol}",
        exchange="binance",
        symbol_raw=symbol,
        kind=kind,
        base=base,
        quote=quote,
    )


# ---------------------------------------------------------------------------
# The one answer to "what does an async implementation mean for three surfaces"
# ---------------------------------------------------------------------------


def test_a_coroutine_runs_to_a_value_when_the_caller_has_no_event_loop() -> None:
    """The CLI projection's branch: a synchronous caller gets a value, not a coroutine.

    Tested from here rather than from ``tests/conformance`` because this batch is where the
    helper was written and where its four call sites are. It lives in
    ``core/capability.py`` now, moved when a second batch wrote a bare ``asyncio.run``
    instead of finding it.
    """

    async def _answer() -> str:
        await asyncio.sleep(0)
        return "done"

    assert run_to_completion(_answer) == "done"


async def test_a_coroutine_still_runs_when_the_caller_is_already_on_an_event_loop() -> None:
    """The REST and MCP branch, which a bare ``asyncio.run`` would turn into a RuntimeError.

    This is the whole reason the helper exists: one declaration has to be callable from a
    synchronous CLI and from inside a running loop, and the failure it prevents is a
    capability that works on one surface and raises on another.
    """

    async def _answer() -> str:
        await asyncio.sleep(0)
        return "done"

    assert asyncio.get_running_loop() is not None
    assert run_to_completion(_answer) == "done"


async def test_a_failure_inside_the_coroutine_reaches_a_caller_that_is_on_a_loop() -> None:
    """A worker thread must not turn an error into a swallowed one."""

    async def _boom() -> None:
        raise ValueError("venue said no")

    with pytest.raises(ValueError, match="venue said no"):
        run_to_completion(_boom)


# ---------------------------------------------------------------------------
# list-exchanges
# ---------------------------------------------------------------------------


def test_list_exchanges_returns_the_crypto_connector_registry() -> None:
    from crocodile.crypto.exchanges.factory import list_exchanges as registry

    assert market.list_exchanges(_ctx(), market.ListExchangesParams()) == registry()


def test_list_providers_returns_the_equity_provider_registry() -> None:
    from crocodile.equity.providers.factory import list_providers as registry

    assert market.list_providers(_ctx(), market.ListExchangesParams()) == registry()


def test_list_exchanges_is_symmetric_today_rather_than_scheduled() -> None:
    """Both markets can already answer "which sources can this build pull from".

    The merge collapsed ``exchange=`` and ``provider=`` into one ``source=`` partition, so
    the two registries are two halves of one list rather than two different questions —
    which is why this capability needs no entry on either ledger.
    """
    cap = REGISTRY["list-exchanges"]
    assert set(cap.impls) == {AssetClass.CRYPTO, AssetClass.EQUITY}
    assert "list-exchanges" not in PENDING_SYMMETRY
    assert "list-exchanges" not in IRREDUCIBLE


def test_list_exchanges_takes_no_parameters_because_no_surface_offered_any() -> None:
    assert msgspec.structs.fields(market.ListExchangesParams) == ()


# ---------------------------------------------------------------------------
# markets
# ---------------------------------------------------------------------------


@pytest.fixture
def _two_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A venue universe with one native-only, one ccxt-only and one shadowed name."""
    monkeypatch.setattr(market.factory, "list_exchanges", lambda: ["binance", "base_onchain"])
    monkeypatch.setattr(market.factory, "list_ccxt_exchanges", lambda: ["binance", "kraken"])
    monkeypatch.setattr(
        market.factory, "list_all_exchanges", lambda: ["base_onchain", "binance", "kraken"]
    )


def test_markets_tags_a_venue_served_by_both_tiers_as_both(_two_tiers: None) -> None:
    """One row per venue, not two: a name in both routes to the native connector."""
    rows = market.markets(_ctx(), market.MarketsParams()).to_dicts()
    assert rows == [
        {"venue": "base_onchain", "native": True, "ccxt": False},
        {"venue": "binance", "native": True, "ccxt": True},
        {"venue": "kraken", "native": False, "ccxt": True},
    ]


def test_markets_filters_by_substring_case_insensitively(_two_tiers: None) -> None:
    frame = market.markets(_ctx(), market.MarketsParams(search="BIN"))
    assert frame["venue"].to_list() == ["binance"]


def test_markets_with_both_tier_flags_returns_the_overlap_rather_than_nothing(
    _two_tiers: None,
) -> None:
    """The CLI printed nothing for this combination, which was a rendering artefact.

    Two independent ``if`` blocks each skipped their own section, so asking for both tiers
    silently produced no output at all. Two filters over one row set compose, and the
    composition is the set of venues that really are both.
    """
    frame = market.markets(_ctx(), market.MarketsParams(native_only=True, ccxt_only=True))
    assert frame["venue"].to_list() == ["binance"]


def test_markets_reports_only_native_venues_when_the_ccxt_extra_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing optional extra is "no extra venues", never an error."""
    monkeypatch.setattr(market.factory, "list_ccxt_exchanges", lambda: [])
    monkeypatch.setattr(market.factory, "list_exchanges", lambda: ["binance"])
    monkeypatch.setattr(market.factory, "list_all_exchanges", lambda: ["binance"])
    assert market.markets(_ctx(), market.MarketsParams()).to_dicts() == [
        {"venue": "binance", "native": True, "ccxt": False}
    ]


def test_markets_returns_a_table_with_its_columns_when_nothing_matches(_two_tiers: None) -> None:
    """An empty result is still a table; a caller selecting a column must not get a KeyError."""
    frame = market.markets(_ctx(), market.MarketsParams(search="no-such-venue"))
    assert frame.height == 0
    assert frame.columns == ["venue", "native", "ccxt"]


# ---------------------------------------------------------------------------
# universe
# ---------------------------------------------------------------------------


@pytest.fixture
def _venue_instruments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for ``load_markets`` — the one network call the enumerating branch makes."""
    instruments = [
        _instrument("BTCUSDT", Kind.SPOT, "BTC", "USDT"),
        _instrument("ETHUSDT", Kind.SPOT, "ETH", "USDT"),
        _instrument("BTCUSD_PERP", Kind.PERPETUAL, "BTC", "USD"),
    ]

    async def _fake(exchange: str, **_: Any) -> list[Instrument]:
        assert exchange == "binance"
        return instruments

    monkeypatch.setattr(market, "exchange_instruments", _fake)


def test_universe_enumerates_a_venue_into_one_row_per_instrument(_venue_instruments: None) -> None:
    rows = market.universe(_ctx(), market.UniverseParams(source="binance")).to_dicts()
    assert rows == [
        {"symbol": "BTCUSDT", "kind": "spot", "base": "BTC", "quote": "USDT", "rank": None},
        {"symbol": "ETHUSDT", "kind": "spot", "base": "ETH", "quote": "USDT", "rank": None},
        {"symbol": "BTCUSD_PERP", "kind": "perpetual", "base": "BTC", "quote": "USD", "rank": None},
    ]


def test_universe_applies_the_kind_and_quote_filters(_venue_instruments: None) -> None:
    frame = market.universe(
        _ctx(), market.UniverseParams(source="binance", kinds=("spot",), quote="usdt")
    )
    assert frame["symbol"].to_list() == ["BTCUSDT", "ETHUSDT"]


def test_universe_caps_the_enumerated_rows_at_the_limit(_venue_instruments: None) -> None:
    frame = market.universe(_ctx(), market.UniverseParams(source="binance", limit=1))
    assert frame["symbol"].to_list() == ["BTCUSDT"]


def test_universe_does_not_filter_by_quote_when_none_was_asked_for(
    _venue_instruments: None,
) -> None:
    """``top_symbols_by_volume`` defaults ``quote`` to USDT; the capability must not inherit it.

    An unrequested filter that silently empties a USD-quoted venue is worse than no filter,
    and the CLI passed its own ``None`` straight through for exactly that reason.
    """
    frame = market.universe(_ctx(), market.UniverseParams(source="binance"))
    assert "USD" in frame["quote"].to_list()


def test_universe_ranks_by_volume_when_top_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(exchange: str, n: int, **kwargs: Any) -> list[str]:
        assert (exchange, n) == ("binance", 2)
        assert kwargs == {"quote": "USDT", "kinds": {Kind.SPOT}}
        return ["BTC/USDT", "ETH/USDT"]

    monkeypatch.setattr(market, "top_symbols_by_volume", _fake)
    rows = market.universe(
        _ctx(),
        market.UniverseParams(source="binance", top=2, quote="USDT", kinds=("spot",)),
    ).to_dicts()
    assert [(row["symbol"], row["rank"]) for row in rows] == [("BTC/USDT", 1), ("ETH/USDT", 2)]


def test_universe_returns_the_same_columns_from_either_branch(
    monkeypatch: pytest.MonkeyPatch, _venue_instruments: None
) -> None:
    """Two column sets under one capability would be unrenderable by one projection.

    The ranked branch reads ``fetch_tickers`` and sees no instrument metadata; the
    enumerating branch reads ``load_markets`` and sees no ranking. A null says "this path
    does not observe it", and the schema stays one schema.
    """

    async def _fake(exchange: str, n: int, **_: Any) -> list[str]:
        return ["BTC/USDT"]

    monkeypatch.setattr(market, "top_symbols_by_volume", _fake)
    enumerated = market.universe(_ctx(), market.UniverseParams(source="binance"))
    ranked = market.universe(_ctx(), market.UniverseParams(source="binance", top=1))
    assert enumerated.columns == ranked.columns
    assert enumerated.schema == ranked.schema
    assert enumerated["rank"].null_count() == enumerated.height
    assert ranked["kind"].null_count() == ranked.height


def test_universe_rejects_an_unknown_kind_instead_of_matching_nothing() -> None:
    """A typo must not read as a venue with no perpetuals."""
    with pytest.raises(ValueError, match="unknown instrument kind"):
        market.universe(_ctx(), market.UniverseParams(source="binance", kinds=("perp",)))


# ---------------------------------------------------------------------------
# census
# ---------------------------------------------------------------------------


def test_census_passes_the_venue_list_and_page_count_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def _fake(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"venues": {"enumerated": 2}}

    monkeypatch.setattr(market.census_mod, "market_census", _fake)
    snapshot = market.census(
        _ctx(), market.CensusParams(venues=("binance", "okx"), coin_pages=3)
    )
    assert snapshot == {"venues": {"enumerated": 2}}
    assert seen["venues"] == ["binance", "okx"]
    assert seen["coin_pages"] == 3


def test_census_asks_for_the_curated_majors_when_no_venue_was_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``None`` and ``[]`` mean opposite things to ``market_census``: defaults, or no venues."""
    seen: dict[str, Any] = {}

    async def _fake(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(market.census_mod, "market_census", _fake)
    market.census(_ctx(), market.CensusParams())
    assert seen["venues"] is None


def test_census_stamps_the_snapshot_from_the_clock_rather_than_from_a_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``generated_ns`` keeps ``market_census`` deterministic; it is not a user's choice."""
    seen: dict[str, Any] = {}

    async def _fake(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(market.census_mod, "market_census", _fake)
    monkeypatch.setattr(market, "now_ns", lambda: _BASE_NS)
    market.census(_ctx(), market.CensusParams())
    assert seen["generated_ns"] == _BASE_NS
    assert "generated_ns" not in {f.name for f in msgspec.structs.fields(market.CensusParams)}


# ---------------------------------------------------------------------------
# M3 — the equity halves of markets, universe and census
# ---------------------------------------------------------------------------
# The reference resolution is exercised on its own in
# `tests/equity/test_reference_universe.py`; what these test is the adapter over it — the
# framing, the filters and the two refusals. `fetch_bulk_evidence` is replaced at the module
# boundary for the reason every other network edge in this file is: a test that fetched
# sec.gov would fail on a plane and go green or red on whatever NASDAQ listed this morning.

_SEC_AGENT = "Crocodile-Test/1.0 (tests@example.com)"
"""SEC blocks a request that does not say who is making it, and the adapters refuse to
invent one — so every equity context here carries a real-shaped contact string."""


def _equity_ctx(catalog: Any = None, sec_user_agent: str | None = _SEC_AGENT) -> CapabilityContext:
    return CapabilityContext(
        catalog=catalog,
        settings=Settings(sec_user_agent=sec_user_agent),
        asset_class=AssetClass.EQUITY,
    )


def _listing_rows(
    *tickers: tuple[str, str, str, str],
) -> reference.ReferenceEvidence:
    """Build evidence directly, as ``(ticker, exchange, assetType, currency)`` tuples.

    Both bulk sources name every ticker, which is the two-attestation state a keyless run
    actually produces — so ``prov_confidence`` here is 0.67 unless a test adds FIGI.
    """
    return reference.ReferenceEvidence(
        as_of_ns=_BASE_NS,
        by_source={
            reference.SOURCE_SEC: reference.instruments_from_sec(
                [
                    SecCompanyTicker(cik=index + 1, ticker=ticker, title=f"{ticker} Inc.")
                    for index, (ticker, _, _, _) in enumerate(tickers)
                ],
                as_of_ns=_BASE_NS,
            ),
            reference.SOURCE_TIINGO: reference.instruments_from_tiingo(
                [
                    TiingoTicker(
                        ticker=ticker,
                        exchange=exchange,
                        asset_type=asset_type,
                        price_currency=currency,
                        start_date="1990-01-02",
                        end_date="2024-01-01",
                    )
                    for ticker, exchange, asset_type, currency in tickers
                ],
                as_of_ns=_BASE_NS,
            ),
        },
        currency={ticker: currency for ticker, _, _, currency in tickers},
    )


@pytest.fixture
def _reference(monkeypatch: pytest.MonkeyPatch) -> None:
    """A three-venue, four-listing universe standing in for the two bulk downloads."""
    evidence = _listing_rows(
        ("AAPL", "NASDAQ", "Stock", "USD"),
        ("MSFT", "NASDAQ", "Stock", "USD"),
        ("SPY", "NYSE ARCA", "ETF", "USD"),
        ("SHOP", "TSX", "Stock", "CAD"),
    )

    async def _fake(**kwargs: Any) -> reference.ReferenceEvidence:
        assert kwargs["sec_user_agent"] == _SEC_AGENT, "the contact string must reach the client"
        return evidence

    monkeypatch.setattr(market.reference, "fetch_bulk_evidence", _fake)

    async def _no_figi(*_: Any, **__: Any) -> dict[str, list[Any]]:
        return {}

    monkeypatch.setattr(market.reference, "fetch_figi", _no_figi)


def _lake_with_bars(tmp_path: Path, volumes: dict[str, float]) -> Catalog:
    """A lake holding one 1d bar per symbol, which is what the volume ranking reads."""

    async def _write() -> None:
        sink = ParquetSink(tmp_path, max_buffer_rows=10_000, flush_interval_seconds=9999)
        for offset, (symbol, volume) in enumerate(volumes.items()):
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
    return Catalog(tmp_path)


# markets


def test_equity_markets_lists_the_venues_the_reference_data_names(_reference: None) -> None:
    """The row source is the resolved universe, so a venue exists because a listing says so.

    ``TSX`` is absent because ``instruments_from_tiingo`` keeps US exchange labels only (spec
    3.6), and ``SHOP``'s SEC row has no exchange, so no venue is named for it. The ``TSX`` row
    stays in the fixture: it demonstrates the scoping end to end.
    """
    rows = market.markets_equities(_equity_ctx(), market.MarketsParams()).to_dicts()
    assert rows == [
        {"venue": "NASDAQ", "native": True, "ccxt": False},
        {"venue": "NYSE ARCA", "native": True, "ccxt": False},
    ]


def test_equity_markets_filters_by_substring_case_insensitively(_reference: None) -> None:
    frame = market.markets_equities(_equity_ctx(), market.MarketsParams(search="nas"))
    assert frame["venue"].to_list() == ["NASDAQ"]


def test_equity_markets_answers_the_ccxt_filter_with_nothing_because_that_is_the_answer(
    _reference: None,
) -> None:
    """ccxt is a cryptocurrency exchange library and reaches no equity venue at any version.

    Empty here is a statement about the tier, not a filter that failed to match — which is
    why the columns are still present for a caller that selects on them.
    """
    frame = market.markets_equities(_equity_ctx(), market.MarketsParams(ccxt_only=True))
    assert frame.height == 0
    assert frame.columns == ["venue", "native", "ccxt"]


def test_equity_markets_keeps_everything_under_the_native_filter(_reference: None) -> None:
    """One connector tier, and it is the hand-written one; ``--native-only`` narrows nothing."""
    unfiltered = market.markets_equities(_equity_ctx(), market.MarketsParams())
    native = market.markets_equities(_equity_ctx(), market.MarketsParams(native_only=True))
    assert native["venue"].to_list() == unfiltered["venue"].to_list()


def test_both_halves_of_markets_return_the_same_columns(_reference: None, _two_tiers: None) -> None:
    """A projection renders one capability; two column sets would make that impossible."""
    crypto = market.markets(_ctx(), market.MarketsParams())
    equity = market.markets_equities(_equity_ctx(), market.MarketsParams())
    assert crypto.columns == equity.columns
    assert crypto.schema == equity.schema


# universe


def test_equity_universe_enumerates_one_exchange_into_one_row_per_listing(
    _reference: None,
) -> None:
    """``source`` is a venue on both sides, which is what makes this the same capability.

    ``base`` and ``quote`` decompose the instrument into what you acquire and what you pay
    with — AAPL for dollars, exactly as BTCUSDT is BTC for USDT.
    """
    rows = market.universe_equities(
        _equity_ctx(), market.UniverseParams(source="NASDAQ")
    ).to_dicts()
    assert rows == [
        {"symbol": "AAPL", "kind": "CS", "base": "AAPL", "quote": "USD", "rank": None},
        {"symbol": "MSFT", "kind": "CS", "base": "MSFT", "quote": "USD", "rank": None},
    ]


def test_equity_universe_applies_the_kind_and_currency_filters(_reference: None) -> None:
    etfs = market.universe_equities(
        _equity_ctx(), market.UniverseParams(source="NYSE ARCA", kinds=("ETF",))
    )
    assert etfs["symbol"].to_list() == ["SPY"]

    wrong_currency = market.universe_equities(
        _equity_ctx(), market.UniverseParams(source="NASDAQ", quote="CAD")
    )
    assert wrong_currency.height == 0


def test_equity_universe_caps_the_enumerated_rows_at_the_limit(_reference: None) -> None:
    frame = market.universe_equities(
        _equity_ctx(), market.UniverseParams(source="NASDAQ", limit=1)
    )
    assert frame["symbol"].to_list() == ["AAPL"]


def test_equity_universe_rejects_an_unknown_kind_instead_of_matching_nothing(
    _reference: None,
) -> None:
    """A crypto kind is a typo here, and a typo must not read as a venue with no ETFs."""
    with pytest.raises(ValueError, match="unknown instrument kind"):
        market.universe_equities(
            _equity_ctx(), market.UniverseParams(source="NASDAQ", kinds=("perpetual",))
        )


def test_equity_universe_ranks_by_the_volume_stored_in_this_lake(
    _reference: None, tmp_path: Path
) -> None:
    """The ranking's data source, asserted: this deployment's own ``channel=ohlcv/`` bars.

    There is no free whole-market equity volume board to rank against, so the honest source
    is the one the lake already holds — and MSFT outranks AAPL here because of what was
    collected, which is exactly the claim the capability makes.
    """
    catalog = _lake_with_bars(tmp_path, {"AAPL": 100.0, "MSFT": 900.0})
    rows = market.universe_equities(
        _equity_ctx(catalog), market.UniverseParams(source="NASDAQ", top=2)
    ).to_dicts()
    assert [(row["symbol"], row["rank"]) for row in rows] == [("MSFT", 1), ("AAPL", 2)]


def test_equity_universe_refuses_to_rank_against_a_lake_with_no_bars(
    _reference: None, tmp_path: Path
) -> None:
    """An arbitrary N tickers wearing the word "top" is worse than a refusal that says why."""
    with pytest.raises(ValueError, match="collect or backfill"):
        market.universe_equities(
            _equity_ctx(Catalog(tmp_path)), market.UniverseParams(source="NASDAQ", top=2)
        )


def test_equity_universe_returns_the_same_columns_from_either_branch(
    _reference: None, tmp_path: Path
) -> None:
    catalog = _lake_with_bars(tmp_path, {"AAPL": 100.0, "MSFT": 900.0})
    enumerated = market.universe_equities(
        _equity_ctx(catalog), market.UniverseParams(source="NASDAQ")
    )
    ranked = market.universe_equities(
        _equity_ctx(catalog), market.UniverseParams(source="NASDAQ", top=1)
    )
    assert enumerated.columns == ranked.columns
    assert enumerated.schema == ranked.schema
    assert enumerated["rank"].null_count() == enumerated.height


def test_both_halves_of_universe_return_the_same_columns(
    _reference: None, _venue_instruments: None
) -> None:
    crypto = market.universe(_ctx(), market.UniverseParams(source="binance"))
    equity = market.universe_equities(_equity_ctx(), market.UniverseParams(source="NASDAQ"))
    assert crypto.columns == equity.columns
    assert crypto.schema == equity.schema


def test_equity_universe_enriches_the_slice_it_returns_with_openfigi(
    _reference: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enrichment is applied after the slice is chosen, which is what bounds the third source.

    Asking OpenFIGI about ninety thousand tickers to return two of them would spend the whole
    keyless allowance on rows nobody requested. The confidence is what reports the difference:
    a bulk row is attested twice, an enriched one three times.
    """
    asked: list[list[str]] = []

    async def _figi(symbols: Any, **_: Any) -> dict[str, list[FigiRecord]]:
        asked.append(list(symbols))
        return {
            symbol: [FigiRecord(figi=f"BBG{symbol}", ticker=symbol, exch_code="UW")]
            for symbol in symbols
        }

    monkeypatch.setattr(market.reference, "fetch_figi", _figi)
    frame = market.universe_equities(_equity_ctx(), market.UniverseParams(source="NASDAQ"))
    assert asked == [["AAPL", "MSFT"]], "only the returned slice, never the whole universe"
    assert frame["symbol"].to_list() == ["AAPL", "MSFT"]


def test_a_slice_past_the_openfigi_burst_is_returned_unenriched_rather_than_half_enriched(
    _reference: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Half a slice at three attestations and half at two would make the confidence a report
    on where a batch boundary fell, and a caller comparing two rows would read a difference in
    coverage that is really a difference in position."""
    monkeypatch.setattr(market, "_FIGI_KEYLESS_BURST", 1)

    async def _never(*_: Any, **__: Any) -> dict[str, list[FigiRecord]]:  # pragma: no cover
        raise AssertionError("a slice over budget must not be enriched at all")

    monkeypatch.setattr(market.reference, "fetch_figi", _never)
    frame = market.universe_equities(_equity_ctx(), market.UniverseParams(source="NASDAQ"))
    assert frame["symbol"].to_list() == ["AAPL", "MSFT"]


def test_a_key_raises_the_enrichment_budget_and_buys_no_extra_fields() -> None:
    """The two OpenFIGI tiers differ in throughput and in nothing else."""
    assert market._figi_budget(Settings()) == market._FIGI_KEYLESS_BURST
    assert market._figi_budget(Settings(openfigi_api_key="k")) == market._FIGI_KEYED_BURST
    assert market._FIGI_KEYED_BURST > market._FIGI_KEYLESS_BURST


# census


def test_equity_census_counts_the_venues_and_the_listings_m3_resolves(_reference: None) -> None:
    """The crypto census counts a venue universe and a coin universe; this counts listings."""
    snapshot = market.census_equities(_equity_ctx(), market.CensusParams())
    # TSX is absent: the Tiingo universe keeps US exchange labels only (spec 3.6), so SHOP's
    # Tiingo row is skipped and only its SEC row remains, under the blank venue it names.
    assert snapshot["venues"]["enumerated"] == 3
    assert snapshot["venues"]["total_markets"] == 4
    assert snapshot["venues"]["by_kind"]["CS"] == 2
    assert snapshot["venues"]["by_kind"]["ETF"] == 1
    assert snapshot["venues"]["rows"][0]["exchange"] == "NASDAQ"
    assert snapshot["venues"]["rows"][0]["markets"] == 2
    assert snapshot["securities"]["resolved"] == 4
    assert snapshot["securities"]["with_cik"] == 4


def test_equity_census_reports_how_many_registries_agreed(_reference: None) -> None:
    """The number the crypto census has no question for, and the reason it is worth having.

    One authority per venue can only report what it found; three overlapping authorities can
    be asked how much of the market they agree exists.
    """
    snapshot = market.census_equities(_equity_ctx(), market.CensusParams())
    assert snapshot["securities"]["attested_by"] == {
        "one_source": 1,
        "two_sources": 3,
        "three_sources": 0,
    }
    assert snapshot["securities"]["by_source"] == {"tiingo": 3, "openfigi": 0, "sec_edgar": 4}


def test_equity_census_restricts_the_count_to_the_venues_it_was_given(_reference: None) -> None:
    snapshot = market.census_equities(_equity_ctx(), market.CensusParams(venues=("NASDAQ",)))
    assert snapshot["venues"]["enumerated"] == 1
    assert snapshot["securities"]["resolved"] == 2


def test_equity_census_mirrors_the_crypto_connector_block(_reference: None) -> None:
    """Field for field, so one projection can render either snapshot."""
    from crocodile.equity.providers.factory import list_providers

    snapshot = market.census_equities(_equity_ctx(), market.CensusParams())
    assert set(snapshot["connectors"]) == {
        "native",
        "native_count",
        "ccxt_count",
        "total_reachable",
    }
    assert snapshot["connectors"]["native"] == sorted(list_providers())
    assert snapshot["connectors"]["ccxt_count"] == 0


def test_equity_census_ignores_the_coin_page_count_rather_than_refusing_it(
    _reference: None,
) -> None:
    """There is no coin universe here to page through, and a caller who omits it pays nothing.

    The same resolution ``CollectParams`` makes for ``dlq_report_path``.
    """
    paged = market.census_equities(_equity_ctx(), market.CensusParams(coin_pages=9))
    plain = market.census_equities(_equity_ctx(), market.CensusParams())
    assert paged["securities"] == plain["securities"]


def test_equity_census_stamps_the_snapshot_from_the_clock(
    _reference: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(market, "now_ns", lambda: _BASE_NS)
    assert market.census_equities(_equity_ctx(), market.CensusParams())["generated_ns"] == _BASE_NS


# The refusal all three share


@pytest.mark.parametrize(
    ("adapter", "params"),
    [
        ("markets_equities", market.MarketsParams()),
        ("universe_equities", market.UniverseParams(source="NASDAQ")),
        ("census_equities", market.CensusParams()),
    ],
)
def test_every_equity_half_refuses_to_invent_a_contact_string_for_sec(
    adapter: str, params: Any, _reference: None
) -> None:
    """SEC's condition is that the User-Agent identify someone contactable, and it blocks
    requests carrying none. ``SecEdgarClient``'s default satisfies the string check with a
    dead mailbox, which is the silent failure ``Settings.sec_user_agent`` was written about.
    """
    with pytest.raises(ConfigError, match="CROCODILE_SEC_USER_AGENT"):
        getattr(market, adapter)(_equity_ctx(sec_user_agent=None), params)


# The declarations these adapters landed


def test_the_three_equity_halves_declare_the_merge_they_rest_on() -> None:
    """``prov`` is the ceiling and ``basis`` names the inputs; both moved off the crypto pair.

    DERIVED because no registry published the merged row, and ``reference_merge`` because
    the inputs are three registries reconciled rather than one venue's own market list.
    ``native`` there would claim a venue reported the universe, which is the one thing no
    equity source does.
    """
    for name in ("markets", "universe", "census"):
        impl = REGISTRY[name].impls[AssetClass.EQUITY]
        assert impl.prov is Provenance.DERIVED, name
        assert impl.basis == "reference_merge", name
        assert level_for(impl.basis) is Provenance.DERIVED


# ---------------------------------------------------------------------------
# open-interest — the one capability whose input is the lake, so it gets a real one
# ---------------------------------------------------------------------------


def _oi(ts: int, source: str, symbol: str, value: float) -> OpenInterest:
    return OpenInterest(
        source=source,
        symbol=symbol,
        symbol_raw=symbol.split(":")[-1],
        source_ts=ts,
        local_ts=ts,
        asset_class=AssetClass.CRYPTO,
        open_interest=value,
    )


@pytest.fixture
def _lake(tmp_path: Path) -> Catalog:
    """Two venues and two underlyings, so a substring filter has something to be wrong about."""

    async def _write() -> None:
        sink = ParquetSink(tmp_path, max_buffer_rows=10_000, flush_interval_seconds=9999)
        for record in (
            _oi(_BASE_NS + 1, "binance", "binance:BTCUSDT", 100.0),
            _oi(_BASE_NS + 1, "okx", "okx:BTC-USDT-SWAP", 50.0),
            _oi(_BASE_NS + 2, "binance", "binance:ETHUSDT", 7.0),
        ):
            await sink.put(record)
        await sink.flush()

    asyncio.run(_write())
    return Catalog(tmp_path)


def test_open_interest_aligns_the_venues_it_finds(_lake: Catalog) -> None:
    frame = market.open_interest(_ctx(_lake), market.OpenInterestParams())
    assert {"local_ts", "binance", "okx", "total_oi"} <= set(frame.columns)
    assert frame.height == 2


def test_open_interest_treats_each_entry_as_a_substring_pattern(_lake: Catalog) -> None:
    """The semantic the implementation has always had, asserted against a symbol it is not.

    ``BTC`` is not a symbol in this lake — ``binance:BTCUSDT`` and ``okx:BTC-USDT-SWAP``
    are — so a list-of-identities reading would return nothing here.
    """
    frame = market.open_interest(_ctx(_lake), market.OpenInterestParams(symbols=("BTC",)))
    assert frame["total_oi"].to_list() == [150.0]


def test_open_interest_ors_several_patterns_together(_lake: Catalog) -> None:
    """The reading REST could never deliver: its comma string went through as one pattern.

    ``?symbols=BTCUSDT,ETHUSDT`` matched no symbol at all and returned an empty board that
    reads exactly like an empty lake.
    """
    both = market.open_interest(
        _ctx(_lake), market.OpenInterestParams(symbols=("BTCUSDT", "ETHUSDT"))
    )
    assert both["total_oi"].to_list()[-1] == 107.0

    unsplit = market.open_interest(
        _ctx(_lake), market.OpenInterestParams(symbols=("BTCUSDT,ETHUSDT",))
    )
    assert unsplit.height == 0


def test_open_interest_with_no_pattern_covers_every_symbol(_lake: Catalog) -> None:
    assert market.open_interest(_ctx(_lake), market.OpenInterestParams())[
        "total_oi"
    ].to_list() == [150.0, 157.0]


def test_the_default_range_covers_the_whole_lake_rather_than_none_of_it(_lake: Catalog) -> None:
    """REST defaulted ``end`` to 0, which told every caller the market has no open interest."""
    defaulted = market.open_interest(_ctx(_lake), market.OpenInterestParams())
    explicit = market.open_interest(
        _ctx(_lake), market.OpenInterestParams(start_ns=0, end_ns=_BASE_NS + 10)
    )
    assert defaulted.height == explicit.height == 2


def test_the_end_of_time_default_is_representable_as_a_signed_64_bit_timestamp() -> None:
    """The CLI's 9999999999999999999 is not, and only worked because DuckDB widened it."""
    assert market._END_OF_TIME == 2**63 - 1
    assert market.OpenInterestParams().end_ns == market._END_OF_TIME


def test_open_interest_takes_one_plural_field_where_three_surfaces_disagreed() -> None:
    """CLI ``--symbol``, REST ``symbols``, MCP ``str | list[str]`` — one struct, one meaning."""
    fields = {f.name: f for f in msgspec.structs.fields(REGISTRY["open-interest"].params)}
    assert set(fields) == {"symbols", "start_ns", "end_ns"}
    assert fields["symbols"].default == ()


# ---------------------------------------------------------------------------
# depth
# ---------------------------------------------------------------------------


class _StubDepthSource:
    """A depth source that records how it was built and never leaves the process."""

    def __init__(self, profile: DepthProfile) -> None:
        self.profile = profile
        self.asked: list[str] = []

    async def snapshot(self, symbol: str) -> DepthProfile:
        self.asked.append(symbol)
        return self.profile


def _a_profile(basis: str, inputs: dict[str, Any]) -> DepthProfile:
    tail = provenance_fields(basis, inputs)
    return DepthProfile(
        source="synth",
        symbol="synth:AAPL",
        symbol_raw="AAPL",
        local_ts=_BASE_NS,
        asset_class=AssetClass.EQUITY,
        source_ts=None,
        bids=[(99.0, 10.0)],
        asks=[(101.0, 10.0)],
        reference_price=100.0,
        depth=2,
        prov=tail.prov,
        prov_basis=tail.prov_basis,
        prov_confidence=tail.prov_confidence,
        prov_inputs=tail.prov_inputs,
    )


def test_depth_forwards_the_ladder_shape_to_the_source_and_the_symbol_to_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _StubDepthSource(_a_profile("yahoo_1m_vap", {"n_volume_bars": 390}))
    built: dict[str, Any] = {}

    def _select(**kwargs: Any) -> _StubDepthSource:
        built.update(kwargs)
        return source

    monkeypatch.setattr(market, "select_depth_source", _select)
    ctx = _ctx(asset_class=AssetClass.EQUITY)
    profile = market.depth(
        ctx,
        market.DepthParams(symbol="AAPL", method="typical", bins=12, top_n=3),
    )
    # ``settings`` joined the call when the surfaces started announcing which branch of this
    # switch would run: the projection resolves the branch from ``ctx.settings`` and the
    # switch has to resolve it from the same object, or the announcement and the branch are
    # two readings of one fact — which is how a keyless deployment came to publish
    # ``alpaca_l1`` over a ladder modelled from Yahoo bars.
    assert built == {"bins": 12, "top_n": 3, "method": "typical", "settings": ctx.settings}
    assert source.asked == ["AAPL"]
    assert profile is source.profile


def test_depth_declares_the_keyed_ceiling_rather_than_todays_environment() -> None:
    """``prov`` is a maximum, and which branch actually ran is on the record, not the ledger.

    ``select_depth_source`` returns Alpaca L1 when two keys are set and a modelled Yahoo VAP
    ladder otherwise, so a declaration that read the environment would say different things
    on two machines running the same build. The record each branch emits carries its own
    measured tail, which is where the difference belongs.
    """
    impl = REGISTRY["depth"].impls[AssetClass.EQUITY]
    assert impl.basis == "alpaca_l1"
    assert impl.prov is Provenance.DERIVED
    assert level_for("alpaca_l1") is Provenance.DERIVED

    keyless = _a_profile("yahoo_1m_vap", {"n_volume_bars": 390})
    assert keyless.prov is Provenance.SYNTHETIC
    assert keyless.is_synthetic

    keyed = _a_profile("alpaca_l1", {"n_quoted_sides": 2})
    assert keyed.prov is Provenance.DERIVED
    assert not keyed.is_synthetic


def test_depth_was_the_capability_whose_missing_half_was_the_crypto_one() -> None:
    """The batch's one backwards gap, closed — and asserted as closed rather than described.

    Every entry in ``SPEC_METHODS`` closes an *equity* gap, so while ``depth`` was
    asymmetric it was scheduled against M6, the only method that named depth at all, and M6
    describes the equity half that already shipped. That mismatch is what M8 was written
    for. Every other name in this batch ran the usual direction and every one of them has
    landed, which is what makes ``depth`` worth a test of its own rather than a line in the
    parametrised sweep: it is the one whose two implementations are asymmetric in *kind*, one
    reaching a vendor and one reading the lake, and asserting both are present is the cheapest
    way to notice if either leaves.

    The loop below is over the whole batch rather than over the names that happened to be
    scheduled when this was written. A test narrowed to "what is still crypto-only" has to be
    edited every time a half lands, and an edit that narrows is indistinguishable from an edit
    that gives up.
    """
    assert set(REGISTRY["depth"].impls) == {AssetClass.EQUITY, AssetClass.CRYPTO}
    assert "depth" not in PENDING_SYMMETRY
    for name in _MARKET_CAPABILITIES:
        assert set(REGISTRY[name].impls) == {AssetClass.CRYPTO, AssetClass.EQUITY}, name
        assert name not in PENDING_SYMMETRY, name


def _book_frame(*, local_ts: int) -> pl.DataFrame:
    """One stored ``book_snapshot`` row, in the shape a lake read hands back."""
    return pl.DataFrame(
        {
            "source": ["deribit"],
            "symbol": ["deribit:BTC-PERPETUAL"],
            "symbol_raw": ["BTC-PERPETUAL"],
            "local_ts": [local_ts],
            "source_ts": [None],
            "bids": [[{"price": 99.0, "amount": 5.0}, {"price": 98.0, "amount": 4.0}]],
            "asks": [[{"price": 101.0, "amount": 5.0}, {"price": 102.0, "amount": 4.0}]],
        },
        schema_overrides={"source_ts": pl.Int64},
    )


class _RecordingCatalog:
    """A lake that answers one book row and remembers how it was asked.

    Enough of a ``Catalog`` for :meth:`CapabilityContext.query`, and nothing else — an
    adapter that reached for ``scan``, ``refresh_views`` or ``connection`` raises here rather
    than quietly working, which is the point.
    """

    def __init__(self, frame: pl.DataFrame) -> None:
        self.frame = frame
        self.seen: list[tuple[str, bool]] = []

    def query(self, sql: str, *, readonly: bool = False) -> pl.DataFrame:
        self.seen.append((sql, readonly))
        return self.frame


def test_the_crypto_half_reads_the_lake_under_the_surfaces_policy_not_around_it() -> None:
    """``ctx.query`` applies ``readonly`` and ``row_limit``; ``ctx.catalog.query`` does not.

    The assertion is the wrapper, not the call count: ``CapabilityContext.query`` is the only
    thing in the tree that wraps a statement in ``SELECT * FROM (…) LIMIT n`` and forwards
    ``readonly``, so seeing both at the catalog proves the adapter went through it. Reaching
    the catalog directly compiles and runs and ignores both fields, which is how the crypto
    CLI came to have no SQL guard while REST and MCP each grew their own.
    """
    catalog = _RecordingCatalog(_book_frame(local_ts=_BASE_NS))
    ctx = CapabilityContext(
        catalog=catalog,
        settings=Settings(),
        asset_class=AssetClass.CRYPTO,
        readonly=True,
        row_limit=5,
    )
    profile = market.depth_crypto(
        ctx, market.DepthParams(symbol="deribit:BTC-PERPETUAL", as_of_ns=_BASE_NS, top_n=2)
    )
    assert profile.reference_price == 100.0
    assert profile.asset_class is AssetClass.CRYPTO

    sql, readonly = catalog.seen[0]
    assert readonly is True
    assert sql.startswith("SELECT * FROM (") and sql.endswith("LIMIT 5")
    assert "book_snapshot" in sql


def test_the_crypto_half_defaults_its_instant_to_the_moment_it_is_called() -> None:
    """``None`` means "when you are asked", and a struct default cannot be a call.

    A default evaluated at import time would freeze the instant at process start, so a
    long-running REST process would answer every unqualified depth request against the book
    as it stood when the server booted.
    """
    catalog = _RecordingCatalog(_book_frame(local_ts=now_ns()))
    ctx = CapabilityContext(catalog=catalog, settings=Settings(), asset_class=AssetClass.CRYPTO)

    before = now_ns()
    market.depth_crypto(ctx, market.DepthParams(symbol="deribit:BTC-PERPETUAL"))
    after = now_ns()

    bound = int(catalog.seen[0][0].split("local_ts <= ")[1].split(" ")[0])
    assert before <= bound <= after


def test_the_two_halves_share_one_parameter_struct() -> None:
    """Symmetry is the same name *and* the same schema; two structs would drift."""
    cap = REGISTRY["depth"]
    assert cap.params is market.DepthParams
    fields = {f.name for f in msgspec.structs.fields(cap.params)}
    assert fields == {"symbol", "method", "bins", "top_n", "as_of_ns", "max_age_ns"}
    # One struct for two halves is the assertion; that both halves now exist is what makes
    # it a statement about a shared schema rather than about a schema with one reader.
    assert set(REGISTRY["depth"].impls) == {AssetClass.CRYPTO, AssetClass.EQUITY}


# ---------------------------------------------------------------------------
# The declarations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _MARKET_CAPABILITIES)
def test_every_market_capability_is_declared_with_a_summary_and_a_params_struct(
    name: str,
) -> None:
    cap = REGISTRY[name]
    assert cap.summary.strip().endswith("."), f"{name} has no one-line summary"
    assert issubclass(cap.params, msgspec.Struct)
    assert cap.returns in (ReturnKind.TABLE, ReturnKind.SCALAR)


@pytest.mark.parametrize("name", _MARKET_CAPABILITIES)
def test_every_implementation_is_a_named_module_level_adapter(name: str) -> None:
    """A stack trace and the calling-convention gate both need a file and a line number."""
    for impl in REGISTRY[name].impls.values():
        assert impl.fn is getattr(market, impl.fn.__name__)
        assert impl.fn.__module__ == market.__name__


@pytest.mark.parametrize("name", _MARKET_CAPABILITIES)
def test_every_declared_basis_carries_a_registered_confidence_formula(name: str) -> None:
    """``level_for`` raises for an unregistered basis, which is the assertion."""
    for impl in REGISTRY[name].impls.values():
        assert isinstance(level_for(impl.basis), Provenance)


def test_the_ledger_schedules_every_asymmetric_capability_in_this_batch() -> None:
    """The batch's filing, with the loop that could not run removed rather than reworded.

    ``for method in scheduled.values(): assert method in SPEC_METHODS`` stood below the
    next assertion and had zero iterations by construction: the line above pins
    ``scheduled`` to the empty dict, so the loop's body was unreachable for as long as that
    assertion held, and unreachable-while-a-neighbour-passes is the whole shape this pass
    is clearing out. It is deleted rather than replaced. This batch's claim is the stronger
    one — *nothing here is scheduled at all* — and the spec-method rule it restated now
    lives once, in
    :func:`~tests.conformance.test_pending_symmetry.assert_asymmetry_is_scheduled`, where
    ``_isolate`` drives it with a fixture capability and proves it bites.
    """
    scheduled = {
        name: PENDING_SYMMETRY[name] for name in _MARKET_CAPABILITIES if name in PENDING_SYMMETRY
    }
    # Empty, and asserted as a dict rather than with `not scheduled` so a regression names
    # the capability that came back. Five of the six left through M2, M3 and M8;
    # `list-exchanges` never needed an entry.
    assert scheduled == {}
    for name in _MARKET_CAPABILITIES:
        symmetric = set(REGISTRY[name].impls) == {AssetClass.CRYPTO, AssetClass.EQUITY}
        assert symmetric != (name in scheduled), f"{name} is scheduled and symmetric, or neither"


def test_no_capability_in_this_batch_claims_irreducibility() -> None:
    """None of these is a property of the market; every one of them is a schedule."""
    assert not set(_MARKET_CAPABILITIES) & set(IRREDUCIBLE)


def test_the_open_interest_frame_is_a_table_and_the_census_snapshot_is_not() -> None:
    """``ReturnKind`` is how a surface decides between rows and one object."""
    assert REGISTRY["open-interest"].returns is ReturnKind.TABLE
    assert REGISTRY["census"].returns is ReturnKind.SCALAR
    assert REGISTRY["depth"].returns is ReturnKind.SCALAR


def test_a_table_capability_returns_a_polars_frame_so_a_surface_can_page_it() -> None:
    """``ReturnKind.TABLE`` is a promise about the shape, and one frame type keeps it."""
    assert isinstance(
        market.markets(_ctx(), market.MarketsParams(search="no-such-venue")), pl.DataFrame
    )
    assert isinstance(market.list_exchanges(_ctx(), market.ListExchangesParams()), list)


# ---------------------------------------------------------------------------
# open-interest, equity half — M2, closed
# ---------------------------------------------------------------------------


def _chain_row(ts: int, underlying: str, strike: float, value: float | None) -> OptionsChain:
    """One Yahoo-shaped contract carrying its own ``openInterest`` and nothing else."""
    symbol = f"yahoo:{underlying}-{int(strike)}-C"
    return OptionsChain(
        source="yahoo",
        symbol=symbol,
        symbol_raw=symbol.split(":", 1)[-1],
        source_ts=ts,
        local_ts=ts,
        asset_class=AssetClass.EQUITY,
        underlying=underlying,
        underlying_price=100.0,
        strike=strike,
        expiry=_BASE_NS + 365 * 86_400 * 1_000_000_000,
        opt_type="C",
        open_interest=value,
    )


@pytest.fixture
def _equity_lake(tmp_path: Path) -> Catalog:
    """One poll of two underlyings' chains, so a substring filter has something to sort."""

    async def _write() -> None:
        sink = ParquetSink(tmp_path, max_buffer_rows=10_000, flush_interval_seconds=9999)
        for record in (
            _chain_row(_BASE_NS + 1, "AAPL", 100.0, 40.0),
            _chain_row(_BASE_NS + 1, "AAPL", 110.0, 20.0),
            _chain_row(_BASE_NS + 1, "MSFT", 400.0, 7.0),
        ):
            await sink.put(record)
        await sink.flush()

    asyncio.run(_write())
    return Catalog(tmp_path)


def _call_open_interest(catalog: Catalog, params: market.OpenInterestParams) -> pl.DataFrame:
    """Through the registry, which is the path a surface takes to reach this half."""
    frame = REGISTRY["open-interest"].impls[AssetClass.EQUITY].fn(_equity_ctx(catalog), params)
    assert isinstance(frame, pl.DataFrame)
    return frame


def test_open_interest_for_equities_sums_the_chain_per_underlying(
    _equity_lake: Catalog,
) -> None:
    """M2's sentence. No equity feed publishes an underlying's open interest as one number.

    Yahoo publishes ``openInterest`` per contract, so the underlying's figure is the sum
    over its chain — which is the whole of what the equity half adds before handing the
    samples to the same alignment the crypto half uses.
    """
    frame = _call_open_interest(_equity_lake, market.OpenInterestParams())
    assert frame.columns == ["local_ts", "yahoo", "total_oi"]
    assert frame.height == 1
    assert frame["total_oi"].to_list() == [67.0]


def test_open_interest_treats_an_equity_pattern_the_way_it_treats_a_crypto_one(
    _equity_lake: Catalog,
) -> None:
    """One ``symbols`` field, one meaning: case-insensitive literal substrings, OR-ed.

    What each half matches them *against* is the series it counts per — a perpetual's
    ``symbol`` there, an ``underlying`` here. A field that meant "pattern" for one asset
    class and "identity" for the other would be the divergence under one name that
    ``OpenInterestParams``' own docstring is a history of.
    """
    aapl = _call_open_interest(_equity_lake, market.OpenInterestParams(symbols=("aapl",)))
    assert aapl["total_oi"].to_list() == [60.0]

    both = _call_open_interest(
        _equity_lake, market.OpenInterestParams(symbols=("AAPL", "MSFT"))
    )
    assert both["total_oi"].to_list() == [67.0]

    unsplit = _call_open_interest(
        _equity_lake, market.OpenInterestParams(symbols=("AAPL,MSFT",))
    )
    assert unsplit.height == 0


def test_the_default_range_covers_the_whole_equity_lake_too(_equity_lake: Catalog) -> None:
    """``end_ns`` defaults to the largest representable timestamp on both halves.

    REST's ``start=0, end=0`` returned nothing for every lake, which reads as a market with
    no open interest rather than as a caller who named no range — and the equity half
    inherits the struct, so it would have inherited the bug.
    """
    defaulted = _call_open_interest(_equity_lake, market.OpenInterestParams())
    explicit = _call_open_interest(
        _equity_lake, market.OpenInterestParams(start_ns=0, end_ns=_BASE_NS + 10)
    )
    assert defaulted.height == explicit.height == 1


def test_the_two_halves_of_open_interest_are_not_the_same_function() -> None:
    """They read different channels and count different series; one object cannot do both.

    Binding ``fn=open_interest`` for equities would have read the ``open_interest`` channel,
    which no equity provider writes, and returned an empty board under a declaration
    promising a real one — the shape ``slippage``'s equity half shipped in and the reason
    this is asserted rather than assumed.
    """
    impls = REGISTRY["open-interest"].impls
    assert set(impls) == {AssetClass.CRYPTO, AssetClass.EQUITY}
    assert impls[AssetClass.CRYPTO].fn is not impls[AssetClass.EQUITY].fn


def test_both_halves_of_open_interest_declare_the_same_ceiling() -> None:
    """DERIVED on ``native``, and the sum does not change either word.

    The inputs are reported open interest on both sides — a perpetual's own figure, a
    contract's own figure — which is what ``native`` names. The alignment, the forward fill
    and the sum are this engine's work, which is what makes the board DERIVED rather than
    NATIVE; and a sum of reported values is still not a model of anything, which is the
    line SYNTHETIC sits the far side of.
    """
    for impl in REGISTRY["open-interest"].impls.values():
        assert impl.prov is Provenance.DERIVED
        assert impl.basis == "native"
        assert level_for(impl.basis) is Provenance.NATIVE
