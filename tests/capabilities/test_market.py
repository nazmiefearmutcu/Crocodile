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
from datetime import date, timedelta
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
from crocodile.core.errors import CapabilityUnavailable, ConfigError
from crocodile.core.markets.calendar import AlwaysOpenCalendar, CalendarQuality, calendar_for
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.master import (
    ListingMaster,
    ListingObservation,
    ListingStatus,
    default_listings_path,
)
from crocodile.core.schema.enums import SecurityType
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
from crocodile.equity.universal.order import SourceOrder
from crocodile.surfaces import dispatch

_BASE_NS = 1_704_067_200_000_000_000

_MARKET_CAPABILITIES = (
    "list-exchanges",
    "markets",
    "universe",
    "census",
    "open-interest",
    "depth",
    "calendar",
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


def test_markets_and_universe_take_mic_and_country_fields() -> None:
    """Both structs carry the two market dimensions, both optional; ``source`` is optional too.

    ``source`` became optional so that ``universe --asset-class equity --mic XIST`` can answer
    without a reference-merge exchange label (spec 3.11).
    """
    for struct in (market.MarketsParams, market.UniverseParams):
        fields = {f.name: f for f in msgspec.structs.fields(struct)}
        assert {"mic", "country"} <= set(fields), struct.__name__
        assert fields["mic"].default is None
        assert fields["country"].default is None
    universe_fields = {f.name: f for f in msgspec.structs.fields(market.UniverseParams)}
    assert universe_fields["source"].default is None


def test_crypto_markets_refuses_a_mic(_two_tiers: None) -> None:
    """A crypto venue has no MIC, so ignoring the filter would return every venue as an answer."""
    with pytest.raises(CapabilityUnavailable) as exc:
        market.markets(_ctx(), market.MarketsParams(mic="XJPX"))
    assert exc.value.capability == "markets"
    assert exc.value.asset_class == "crypto"
    assert "mic" in exc.value.reason


def test_crypto_markets_refuses_a_country(_two_tiers: None) -> None:
    with pytest.raises(CapabilityUnavailable) as exc:
        market.markets(_ctx(), market.MarketsParams(country="JP"))
    assert exc.value.capability == "markets"
    assert exc.value.asset_class == "crypto"
    assert "country" in exc.value.reason


def test_crypto_markets_refuses_both_dimensions_and_names_both(_two_tiers: None) -> None:
    with pytest.raises(CapabilityUnavailable) as exc:
        market.markets(_ctx(), market.MarketsParams(mic="XJPX", country="JP"))
    assert "mic" in exc.value.reason
    assert "country" in exc.value.reason


async def _must_not_be_reached(*_: Any, **__: Any) -> Any:
    raise AssertionError("must not be reached")


def test_crypto_universe_refuses_a_mic_before_reaching_the_venue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(market, "exchange_instruments", _must_not_be_reached)
    monkeypatch.setattr(market, "top_symbols_by_volume", _must_not_be_reached)
    with pytest.raises(CapabilityUnavailable) as exc:
        market.universe(_ctx(), market.UniverseParams(source="binance", mic="XJPX"))
    assert exc.value.capability == "universe"
    assert exc.value.asset_class == "crypto"
    assert "mic" in exc.value.reason


def test_crypto_universe_refuses_a_country_before_reaching_the_venue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(market, "exchange_instruments", _must_not_be_reached)
    monkeypatch.setattr(market, "top_symbols_by_volume", _must_not_be_reached)
    with pytest.raises(CapabilityUnavailable) as exc:
        market.universe(_ctx(), market.UniverseParams(source="binance", country="JP"))
    assert exc.value.capability == "universe"
    assert "country" in exc.value.reason


def test_crypto_universe_needs_a_source() -> None:
    """With no venue and no market dimension, the crypto half has nothing to enumerate."""
    with pytest.raises(ValueError, match="source"):
        market.universe(_ctx(), market.UniverseParams())


def test_crypto_markets_leaves_every_exchange_column_null(_two_tiers: None) -> None:
    """The one schema is shared with the equity half; crypto fills only what it observes.

    The exchange reference columns (``mic``, ``name``, ``country``, ...) are null on this half
    because it does not observe them, which is a different statement from "this venue lacks
    them" (the argument ``_UNIVERSE_SCHEMA``'s docstring makes).
    """
    frame = market.markets(_ctx(), market.MarketsParams())
    observed = {"venue", "native", "ccxt"}
    for row in frame.to_dicts():
        for key in market._MARKETS_SCHEMA:
            if key not in observed:
                assert row[key] is None, key


def test_markets_tags_a_venue_served_by_both_tiers_as_both(_two_tiers: None) -> None:
    """One row per venue, not two: a name in both routes to the native connector.

    The comparison selects the three tier columns because the frame now carries the equity
    reference columns too, null on this half (see ``_MARKETS_SCHEMA``).
    """
    rows = market.markets(_ctx(), market.MarketsParams()).select("venue", "native", "ccxt")
    assert rows.to_dicts() == [
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
    """A missing optional extra is "no extra venues", never an error.

    Selects the tier columns: the frame also carries the equity reference columns, null here.
    """
    monkeypatch.setattr(market.factory, "list_ccxt_exchanges", lambda: [])
    monkeypatch.setattr(market.factory, "list_exchanges", lambda: ["binance"])
    monkeypatch.setattr(market.factory, "list_all_exchanges", lambda: ["binance"])
    frame = market.markets(_ctx(), market.MarketsParams()).select("venue", "native", "ccxt")
    assert frame.to_dicts() == [{"venue": "binance", "native": True, "ccxt": False}]


def test_markets_returns_a_table_with_its_columns_when_nothing_matches(_two_tiers: None) -> None:
    """An empty result is still a table; a caller selecting a column must not get a KeyError.

    The expected columns are the whole shared schema: the frame now carries the equity
    reference columns too (null on this half), so the list is ``_MARKETS_SCHEMA`` itself.
    """
    frame = market.markets(_ctx(), market.MarketsParams(search="no-such-venue"))
    assert frame.height == 0
    assert frame.columns == list(market._MARKETS_SCHEMA)


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


_EQUITY_TIER_COLUMNS = ("venue", "native", "ccxt")
"""The tier columns every equity row states; the reference columns are checked separately."""


def test_equity_markets_lists_every_exchange_in_the_table() -> None:
    """The row source is ``exchanges.csv``, not the listing data, so every exchange is here.

    Before this change the equity half listed the venues the merged reference data named,
    which is why a clean lake had none. Now the bundled exchange table is the source (spec
    3.11): one row per allow-listed MIC, sorted by MIC, with the operating MIC as venue.
    """
    registry = ExchangeRegistry()
    frame = market.markets_equities(_equity_ctx(), market.MarketsParams())
    assert frame.height == len(registry.all())
    assert frame["mic"].to_list() == [info.mic for info in registry.all()]
    assert frame["venue"].to_list() == frame["mic"].to_list()
    assert set(frame["native"].to_list()) == {True}
    assert set(frame["ccxt"].to_list()) == {False}


def test_an_equity_markets_row_states_the_exchange_reference_columns() -> None:
    info = ExchangeRegistry().get("XIST")
    row = market.markets_equities(_equity_ctx(), market.MarketsParams(mic="XIST")).to_dicts()[0]
    reference_columns = {
        key: value
        for key, value in row.items()
        if key not in _EQUITY_TIER_COLUMNS
        and key not in {"sources", "usage_classes", "listings_known", "last_success"}
    }
    assert reference_columns == {
        "mic": "XIST",
        "name": info.name,
        "country": "TR",
        "timezone": "Europe/Istanbul",
        "currency": "TRY",
        "quote_scale": 0,
        "tier_research": info.tier_research,
        "sanctioned": False,
        "calendar_quality": CalendarQuality.LIBRARY.value,
    }


def test_a_minor_unit_exchange_says_so_in_quote_scale() -> None:
    """The column is how a reader learns that prices are in pence, not pounds (spec 3.3)."""
    row = market.markets_equities(_equity_ctx(), market.MarketsParams(mic="XLON")).to_dicts()[0]
    assert row["currency"] == "GBP"
    assert row["quote_scale"] == 2


def test_a_sanctioned_exchange_is_listed_and_flagged() -> None:
    """Acceptance 4 lists every MIC; refusing collection is the collection paths' job."""
    frame = market.markets_equities(_equity_ctx(), market.MarketsParams(mic="MISX"))
    assert frame.height == 1
    assert frame["sanctioned"].to_list() == [True]


def test_an_exchange_without_a_library_calendar_says_weekday_fallback() -> None:
    frame = market.markets_equities(_equity_ctx(), market.MarketsParams())
    qualities = dict(zip(frame["mic"].to_list(), frame["calendar_quality"].to_list(), strict=True))
    for mic in ("XNSE", "XCAI"):
        assert qualities[mic] == CalendarQuality.WEEKDAY_FALLBACK.value, mic


def test_equity_markets_calendar_quality_agrees_with_calendar_for() -> None:
    """The row's quality is what ``calendar_for`` answers, so the two can never diverge."""
    for mic in ("XNYS", "XNAS", "XIST", "XJPX", "XNSE", "XCAI"):
        row = market.markets_equities(_equity_ctx(), market.MarketsParams(mic=mic)).to_dicts()[0]
        assert row["calendar_quality"] == calendar_for(mic).quality.value, mic


def test_equity_markets_filters_by_mic_case_insensitively() -> None:
    frame = market.markets_equities(_equity_ctx(), market.MarketsParams(mic="xist"))
    assert frame["mic"].to_list() == ["XIST"]


def test_equity_markets_answers_a_segment_mic_with_its_operating_exchange() -> None:
    frame = market.markets_equities(_equity_ctx(), market.MarketsParams(mic="XTKS"))
    assert frame["mic"].to_list() == ["XJPX"]


def test_equity_markets_filters_by_country() -> None:
    frame = market.markets_equities(_equity_ctx(), market.MarketsParams(country="jp"))
    assert frame.height > 0
    assert set(frame["country"].to_list()) == {"JP"}
    assert "XJPX" in frame["mic"].to_list()


def test_equity_markets_refuses_an_unknown_mic() -> None:
    """ZZZZ is absent from the ISO snapshot; XXXX would be an ISO code and is not used."""
    with pytest.raises(ValueError, match="ZZZZ"):
        market.markets_equities(_equity_ctx(), market.MarketsParams(mic="ZZZZ"))


def test_equity_markets_refuses_an_expired_mic_and_names_its_replacement() -> None:
    with pytest.raises(ValueError, match="XIST"):
        market.markets_equities(_equity_ctx(), market.MarketsParams(mic="XTUR"))


def test_equity_markets_refuses_an_active_mic_outside_the_exchange_table() -> None:
    """XOFF is an active ISO MIC (an OPRT) that exchanges.csv does not allow-list."""
    with pytest.raises(ValueError, match="XOFF"):
        market.markets_equities(_equity_ctx(), market.MarketsParams(mic="XOFF"))


def test_equity_markets_refuses_a_country_with_no_exchange() -> None:
    with pytest.raises(ValueError, match="AQ"):
        market.markets_equities(_equity_ctx(), market.MarketsParams(country="AQ"))
    with pytest.raises(ValueError, match="TUR"):
        market.markets_equities(_equity_ctx(), market.MarketsParams(country="TUR"))


def test_equity_markets_refuses_a_mic_and_country_that_disagree() -> None:
    with pytest.raises(ValueError, match="TR"):
        market.markets_equities(_equity_ctx(), market.MarketsParams(mic="XIST", country="DE"))


def test_equity_markets_search_matches_mic_or_name() -> None:
    assert market.markets_equities(
        _equity_ctx(), market.MarketsParams(search="istanbul")
    )["mic"].to_list() == ["XIST"]
    assert "XJPX" in market.markets_equities(
        _equity_ctx(), market.MarketsParams(search="xjp")
    )["mic"].to_list()


def test_equity_markets_needs_no_network_and_no_sec_contact_string(
    network_forbidden: None,
) -> None:
    """The table is packaged data: no SEC, no network, no CROCODILE_SEC_USER_AGENT."""
    frame = market.markets_equities(
        _equity_ctx(sec_user_agent=None), market.MarketsParams()
    )
    assert frame.height == len(ExchangeRegistry().all())


def test_equity_markets_lists_the_registered_sources_that_serve_a_mic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sources`` is the registered providers that claim a MIC, by declaration or by grammar.

    ``google_finance`` serves XIST, XNAS and XNYS through its grammar table; ``fake_feed``
    declares XIST. An unregistered source contributes nothing: a grammar table for a source
    nothing can collect from is not coverage.
    """
    monkeypatch.setattr(
        market.provider_factory, "list_providers", lambda: ["fake_feed", "google_finance"]
    )
    monkeypatch.setattr(
        market.provider_factory, "supported_mics", {"fake_feed": frozenset({"XIST"})}.get
    )
    # The fake provider is not in the registry that source_order() validates against, so the
    # packaged table is read unvalidated here; the rank is not what this test asserts.
    monkeypatch.setattr(market, "source_order", lambda: SourceOrder.load())
    rows = {
        row["mic"]: row["sources"]
        for row in market.markets_equities(_equity_ctx(), market.MarketsParams()).to_dicts()
    }
    assert rows["XIST"] == ["fake_feed", "google_finance"]
    assert rows["XNYS"] == ["google_finance"]
    assert rows["XJPX"] == []


def test_equity_markets_filters_by_substring_case_insensitively(_reference: None) -> None:
    """The venue-list fixture is no longer the row source; the search is still case-blind."""
    frame = market.markets_equities(_equity_ctx(), market.MarketsParams(search="NASDAQ"))
    assert "XNAS" in frame["mic"].to_list()
    assert frame["venue"].to_list() == frame["mic"].to_list()


def test_equity_markets_answers_the_ccxt_filter_with_nothing_because_that_is_the_answer() -> None:
    """ccxt is a cryptocurrency exchange library and reaches no equity venue at any version.

    Empty here is a statement about the tier, not a filter that failed to match — which is
    why the columns are still present for a caller that selects on them. The frame carries
    the whole shared schema, reference columns included, because both halves share one.
    """
    frame = market.markets_equities(_equity_ctx(), market.MarketsParams(ccxt_only=True))
    assert frame.height == 0
    assert frame.columns == list(market._MARKETS_SCHEMA)


def test_equity_markets_keeps_everything_under_the_native_filter() -> None:
    """One connector tier, and it is the hand-written one; ``--native-only`` narrows nothing."""
    unfiltered = market.markets_equities(_equity_ctx(), market.MarketsParams())
    native = market.markets_equities(_equity_ctx(), market.MarketsParams(native_only=True))
    assert native["venue"].to_list() == unfiltered["venue"].to_list()


def test_both_halves_of_markets_return_the_same_columns(_two_tiers: None) -> None:
    """A projection renders one capability; two column sets would make that impossible.

    The equity half no longer reads the listing fixture, so this compares the two halves
    against the shared schema and against each other.
    """
    crypto = market.markets(_ctx(), market.MarketsParams())
    equity = market.markets_equities(_equity_ctx(), market.MarketsParams())
    assert crypto.columns == equity.columns == list(market._MARKETS_SCHEMA)
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


# ---------------------------------------------------------------------------
# universe from the listing master (mic / country), spec 3.11
# ---------------------------------------------------------------------------


def _observation(
    mic: str,
    local: str,
    currency: str,
    *,
    observed_ns: int = 1,
    status: ListingStatus = ListingStatus.ACTIVE,
    kind: SecurityType = SecurityType.CS,
) -> ListingObservation:
    return ListingObservation(
        source="fixture",
        mic=mic,
        local_symbol=local,
        symbol_raw=f"{mic}:{local}",
        observed_ns=observed_ns,
        currency=currency,
        price_scale=0,
        status=status,
        security_type=kind,
    )


def _master(*observations: ListingObservation) -> ListingMaster:
    master = ListingMaster(":memory:")
    for observation in observations:
        master.upsert(observation)
    return master


_FIXTURE_ROWS = (
    ("XIST", "THYAO", "TRY"),
    ("XIST", "GARAN", "TRY"),
    ("XJPX", "7203", "JPY"),
)


@pytest.fixture
def _listing_world(monkeypatch: pytest.MonkeyPatch) -> None:
    """THYAO and GARAN on XIST (TRY, CS) and 7203 on XJPX (JPY, CS), behind the seam.

    The seam returns a fresh master per call because the adapter closes what it opens.
    """
    monkeypatch.setattr(
        market,
        "_listing_master",
        lambda ctx: _master(*(_observation(m, local, ccy) for m, local, ccy in _FIXTURE_ROWS)),
    )


def _equity_ctx_at(data_dir: Path, catalog: Any = None) -> CapabilityContext:
    return CapabilityContext(
        catalog=catalog,
        settings=Settings(data_dir=data_dir, sec_user_agent=_SEC_AGENT),
        asset_class=AssetClass.EQUITY,
    )


def test_equity_universe_lists_one_mic_out_of_the_listing_master(_listing_world: None) -> None:
    rows = market.universe_equities(_equity_ctx(), market.UniverseParams(mic="XIST")).to_dicts()
    assert rows == [
        {"symbol": "GARAN@XIST", "kind": "CS", "base": "GARAN@XIST", "quote": "TRY", "rank": None},
        {"symbol": "THYAO@XIST", "kind": "CS", "base": "THYAO@XIST", "quote": "TRY", "rank": None},
    ]


def test_equity_universe_lists_one_country_out_of_the_listing_master(_listing_world: None) -> None:
    symbols = market.universe_equities(
        _equity_ctx(), market.UniverseParams(country="tr")
    )["symbol"].to_list()
    assert symbols == ["GARAN@XIST", "THYAO@XIST"]
    jp = market.universe_equities(_equity_ctx(), market.UniverseParams(country="JP"))
    assert jp["symbol"].to_list() == ["7203@XJPX"]


def test_equity_universe_keeps_unseen_listings_and_drops_delisted_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unseen is absence of evidence, not evidence of absence (as ``rank_listings`` argues)."""
    master = _master(
        _observation("XIST", "THYAO", "TRY", observed_ns=1),
        _observation("XIST", "GARAN", "TRY", observed_ns=1),
        _observation("XIST", "OLDCO", "TRY", observed_ns=1),
    )
    master.mark_unseen("XIST", "fixture", {"THYAO"}, at_ns=2)
    master.upsert(
        _observation("XIST", "OLDCO", "TRY", observed_ns=3, status=ListingStatus.DELISTED)
    )
    monkeypatch.setattr(market, "_listing_master", lambda ctx: master)
    symbols = market.universe_equities(_equity_ctx(), market.UniverseParams(mic="XIST"))[
        "symbol"
    ].to_list()
    assert symbols == ["GARAN@XIST", "THYAO@XIST"]


def test_equity_universe_applies_kind_and_currency_filters_to_a_listing_master_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fresh master per call: the adapter closes what it opens.
    monkeypatch.setattr(
        market,
        "_listing_master",
        lambda ctx: _master(
            _observation("XIST", "THYAO", "TRY"),
            _observation("XIST", "GLDTR", "TRY", kind=SecurityType.ETF),
        ),
    )
    etfs = market.universe_equities(
        _equity_ctx(), market.UniverseParams(mic="XIST", kinds=("ETF",))
    )
    assert etfs["symbol"].to_list() == ["GLDTR@XIST"]
    usd = market.universe_equities(_equity_ctx(), market.UniverseParams(mic="XIST", quote="usd"))
    assert usd.height == 0
    assert usd.columns == list(market._UNIVERSE_SCHEMA)


def test_equity_universe_rejects_an_unknown_kind_on_a_listing_master_slice(
    _listing_world: None,
) -> None:
    with pytest.raises(ValueError, match="unknown instrument kind"):
        market.universe_equities(_equity_ctx(), market.UniverseParams(mic="XIST", kinds=("perpetual",)))


def test_equity_universe_caps_a_listing_master_slice_at_limit(_listing_world: None) -> None:
    rows = market.universe_equities(_equity_ctx(), market.UniverseParams(mic="XIST", limit=1))
    assert rows["symbol"].to_list() == ["GARAN@XIST"]


def test_equity_universe_ranks_a_listing_master_slice_by_lake_volume(
    tmp_path: Path, _listing_world: None
) -> None:
    catalog = _lake_with_bars(tmp_path, {"GARAN@XIST": 100.0, "THYAO@XIST": 900.0})
    frame = market.universe_equities(
        _equity_ctx(catalog), market.UniverseParams(mic="XIST", top=2)
    )
    assert [(row["symbol"], row["rank"]) for row in frame.to_dicts()] == [
        ("THYAO@XIST", 1),
        ("GARAN@XIST", 2),
    ]


def test_equity_universe_refuses_to_rank_a_listing_master_slice_without_bars(
    tmp_path: Path, _listing_world: None
) -> None:
    with pytest.raises(ValueError, match="collect or backfill"):
        market.universe_equities(
            _equity_ctx(Catalog(tmp_path)), market.UniverseParams(mic="XIST", top=1)
        )


def test_equity_universe_needs_a_source_or_a_market_dimension() -> None:
    with pytest.raises(ValueError, match="mic"):
        market.universe_equities(_equity_ctx(), market.UniverseParams())


def test_equity_universe_refuses_a_source_together_with_a_market_dimension(
    _listing_world: None,
) -> None:
    with pytest.raises(ValueError):
        market.universe_equities(
            _equity_ctx(), market.UniverseParams(source="NASDAQ", mic="XIST")
        )


def test_equity_universe_refuses_an_empty_listing_master_slice(_listing_world: None) -> None:
    with pytest.raises(ValueError, match="universe-sync"):
        market.universe_equities(_equity_ctx(), market.UniverseParams(mic="XLON"))


def test_equity_universe_refuses_when_no_listing_master_exists_and_creates_none(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="universe-sync"):
        market.universe_equities(
            _equity_ctx_at(tmp_path), market.UniverseParams(mic="XIST")
        )
    assert not (tmp_path / "reference").exists()


def test_equity_universe_opens_the_master_at_the_lake_path(tmp_path: Path) -> None:
    """A read leaves no -wal or -shm behind: universe is served on read-only surfaces too."""
    writer = ListingMaster(default_listings_path(tmp_path))
    writer.upsert(_observation("XIST", "THYAO", "TRY"))
    writer.close()
    frame = market.universe_equities(_equity_ctx_at(tmp_path), market.UniverseParams(mic="XIST"))
    assert frame["symbol"].to_list() == ["THYAO@XIST"]
    assert sorted(p.name for p in (tmp_path / "reference").iterdir()) == ["listings.sqlite"]


def test_a_listing_master_slice_is_cut_by_the_surface_ceiling_and_says_so(
    _listing_world: None,
) -> None:
    """The adapter does not pre-cut at the row ceiling; ``dispatch.payload`` sets ``truncated``."""
    frame = market.universe_equities(_equity_ctx(), market.UniverseParams(mic="XIST"))
    assert frame.height == 2
    shaped = dispatch.payload(REGISTRY["universe"], frame, row_limit=1)
    assert len(shaped["rows"]) == 1
    assert shaped["truncated"] is True


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


def test_equity_census_reports_every_security_type_even_at_zero(_reference: None) -> None:
    """A kind with no listings is a zero the census states, not a key it leaves out.

    ``census_equities`` derives its kind keys from :class:`SecurityType`, so a member added to
    the enum must appear here at zero rather than vanish until a listing of that kind exists.
    """
    snapshot = market.census_equities(_equity_ctx(), market.CensusParams())
    kind_keys = {member.value for member in SecurityType}
    assert set(snapshot["venues"]["by_kind"]) == kind_keys
    for row in snapshot["venues"]["rows"]:
        assert kind_keys <= set(row)
    assert snapshot["venues"]["by_kind"]["FUND"] == 0


# The refusal all three share


@pytest.mark.parametrize(
    ("adapter", "params"),
    [
        # ``markets_equities`` is not here: it reads the bundled exchange table and never
        # contacts SEC, so it needs no contact string (task 08a, spec 3.11). ``universe``
        # (source branch) and ``census`` still do.
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


def test_universe_and_census_equity_halves_declare_the_merge_they_rest_on() -> None:
    """``prov`` is the ceiling and ``basis`` names the inputs; both moved off the crypto pair.

    DERIVED because no registry published the merged row, and ``reference_merge`` because
    the inputs are three registries reconciled rather than one venue's own market list.
    ``native`` there would claim a venue reported the universe, which is the one thing no
    equity source does. ``markets`` is split out: it no longer reads the merge.
    """
    for name in ("universe", "census"):
        impl = REGISTRY[name].impls[AssetClass.EQUITY]
        assert impl.prov is Provenance.DERIVED, name
        assert impl.basis == "reference_merge", name
        assert level_for(impl.basis) is Provenance.DERIVED


def test_equity_markets_is_native_because_it_reads_this_builds_reference_table() -> None:
    """Read from the bundled exchange table and the build's declarations, not reconstructed."""
    impl = REGISTRY["markets"].impls[AssetClass.EQUITY]
    assert impl.prov is Provenance.NATIVE
    assert impl.basis == "native"
    assert impl.reads == ()


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


# calendar


def _sessions(frame: pl.DataFrame) -> list[tuple[date, Any, Any]]:
    return list(
        zip(
            frame["date"].to_list(),
            frame["open_utc"].to_list(),
            frame["close_utc"].to_list(),
            strict=True,
        )
    )


def test_calendar_crypto_is_always_open_every_calendar_day() -> None:
    """Saturday and Sunday are sessions too, and the bounds are the always-open calendar's."""
    frame = market.calendar_crypto(
        _ctx(), market.CalendarParams(start="2026-01-02", end="2026-01-05")
    )
    assert frame["date"].to_list() == [
        date(2026, 1, 2),
        date(2026, 1, 3),
        date(2026, 1, 4),
        date(2026, 1, 5),
    ]
    assert frame["mic"].null_count() == frame.height
    assert frame["quality"].to_list() == [CalendarQuality.ALWAYS_OPEN.value] * 4
    cal = AlwaysOpenCalendar()
    expected = [(d, cal.session_open_utc(d), cal.session_close_utc(d)) for d in frame["date"]]
    assert _sessions(frame) == expected


def test_calendar_crypto_refuses_a_mic() -> None:
    """Every crypto venue trades around the clock, so a MIC cannot be honoured by it."""
    with pytest.raises(CapabilityUnavailable) as exc:
        market.calendar_crypto(
            _ctx(), market.CalendarParams(start="2026-01-02", end="2026-01-05", mic="XIST")
        )
    assert exc.value.capability == "calendar"
    assert exc.value.asset_class == "crypto"
    assert "mic" in exc.value.reason


def test_calendar_equities_lists_the_sessions_of_one_mic() -> None:
    """Framing, not holiday data: the rows are the calendar's own sessions, in UTC."""
    cal = calendar_for("XIST")
    frame = market.calendar_equities(
        _equity_ctx(),
        market.CalendarParams(mic="XIST", start="2026-01-05", end="2026-01-11"),
    )
    expected = [
        (d, cal.session_open_utc(d), cal.session_close_utc(d))
        for d in cal.sessions(date(2026, 1, 5), date(2026, 1, 11))
    ]
    assert _sessions(frame) == expected
    assert all(d.weekday() < 5 for d in frame["date"].to_list())
    assert frame["mic"].to_list() == ["XIST"] * frame.height
    assert frame["quality"].to_list() == [CalendarQuality.LIBRARY.value] * frame.height


def test_calendar_equities_answers_a_segment_mic_with_its_operating_exchange() -> None:
    frame = market.calendar_equities(
        _equity_ctx(),
        market.CalendarParams(mic="XTKS", start="2026-01-05", end="2026-01-07"),
    )
    assert frame.height > 0
    assert frame["mic"].to_list() == ["XJPX"] * frame.height


def test_calendar_rows_of_a_fallback_exchange_say_so() -> None:
    """XCAI has no library calendar: weekday rules, no holidays, no open, and a close only
    when the exchange table carries one."""
    frame = market.calendar_equities(
        _equity_ctx(),
        market.CalendarParams(mic="XCAI", start="2026-01-04", end="2026-01-10"),
    )
    dates = frame["date"].to_list()
    assert date(2026, 1, 9) not in dates
    assert date(2026, 1, 10) not in dates
    assert frame["quality"].to_list() == [CalendarQuality.WEEKDAY_FALLBACK.value] * frame.height
    assert frame["open_utc"].null_count() == frame.height
    closes_known = ExchangeRegistry().get("XCAI").close_local is not None
    assert frame["close_utc"].null_count() == (0 if closes_known else frame.height)


def test_a_fallback_exchange_with_a_known_close_reports_it() -> None:
    """XNAM has no library alias and a curated close (17:00 local), so its closes are known
    while its opens are not. (XSHE, the brief's example, now has the XSHG alias from task C1
    and answers from the library, so it is not a fallback exchange any more.)"""
    cal = calendar_for("XNAM")
    frame = market.calendar_equities(
        _equity_ctx(),
        market.CalendarParams(mic="XNAM", start="2026-01-05", end="2026-01-09"),
    )
    assert frame.height == 5
    assert frame["quality"].to_list() == [CalendarQuality.WEEKDAY_FALLBACK.value] * 5
    assert frame["open_utc"].null_count() == 5
    assert frame["close_utc"].to_list() == [
        cal.session_close_utc(d) for d in frame["date"].to_list()
    ]


def test_calendar_rows_past_a_library_bound_say_weekday_fallback() -> None:
    """XSHG's library ends at its window bound; later dates answer by weekday rules."""
    last = calendar_for("XSHG").window[1]  # type: ignore[attr-defined]
    frame = market.calendar_equities(
        _equity_ctx(),
        market.CalendarParams(
            mic="XSHG",
            start=(last + timedelta(days=4)).isoformat(),
            end=(last + timedelta(days=10)).isoformat(),
        ),
    )
    assert frame.height > 0
    assert frame["quality"].to_list() == [CalendarQuality.WEEKDAY_FALLBACK.value] * frame.height
    assert frame["open_utc"].null_count() == frame.height


def test_a_range_across_a_library_bound_switches_quality_on_the_bound() -> None:
    last = calendar_for("XSHG").window[1]  # type: ignore[attr-defined]
    frame = market.calendar_equities(
        _equity_ctx(),
        market.CalendarParams(
            mic="XSHG",
            start=(last - timedelta(days=3)).isoformat(),
            end=(last + timedelta(days=7)).isoformat(),
        ),
    )
    library_rows = [q for d, q in zip(frame["date"], frame["quality"], strict=True) if d <= last]
    fallback_rows = [q for d, q in zip(frame["date"], frame["quality"], strict=True) if d > last]
    assert library_rows and fallback_rows
    assert set(library_rows) == {CalendarQuality.LIBRARY.value}
    assert set(fallback_rows) == {CalendarQuality.WEEKDAY_FALLBACK.value}


def test_calendar_equities_needs_a_mic() -> None:
    with pytest.raises(ValueError, match="mic"):
        market.calendar_equities(
            _equity_ctx(), market.CalendarParams(start="2026-01-05", end="2026-01-09")
        )


def test_calendar_equities_refuses_an_unknown_mic() -> None:
    with pytest.raises(ValueError, match="ZZZZ"):
        market.calendar_equities(
            _equity_ctx(),
            market.CalendarParams(mic="ZZZZ", start="2026-01-05", end="2026-01-09"),
        )


@pytest.mark.parametrize("field", ["start", "end"])
def test_calendar_rejects_a_malformed_date(field: str) -> None:
    dates = {"start": "2026-01-05", "end": "2026-01-09"}
    dates[field] = "2026-13-01"
    with pytest.raises(ValueError, match=field):
        market.calendar_crypto(_ctx(), market.CalendarParams(**dates))


def test_calendar_rejects_an_inverted_range() -> None:
    with pytest.raises(ValueError):
        market.calendar_crypto(_ctx(), market.CalendarParams(start="2026-01-09", end="2026-01-05"))


def test_calendar_rejects_a_range_longer_than_the_cap() -> None:
    """No holiday table reaches a century, so a longer span is a typo, not a question."""
    with pytest.raises(ValueError, match="days"):
        market.calendar_crypto(_ctx(), market.CalendarParams(start="0001-01-01", end="9999-12-31"))


def test_calendar_maps_a_library_range_error_to_a_bad_request() -> None:
    with pytest.raises(ValueError) as exc:
        market.calendar_equities(
            _equity_ctx(),
            market.CalendarParams(mic="XIST", start="1800-01-01", end="1800-01-10"),
        )
    assert isinstance(exc.value, ValueError)


def test_both_halves_of_calendar_return_the_same_columns() -> None:
    crypto = market.calendar_crypto(
        _ctx(), market.CalendarParams(start="2026-01-05", end="2026-01-09")
    )
    equity = market.calendar_equities(
        _equity_ctx(),
        market.CalendarParams(mic="XIST", start="2026-01-05", end="2026-01-09"),
    )
    assert crypto.columns == equity.columns == list(market._CALENDAR_SCHEMA)
    assert crypto.schema == equity.schema


def test_calendar_declares_two_distinct_derived_halves() -> None:
    """Crypto is a pure function of the caller's dates (caller_supplied); equity reads the
    published schedules the library encodes (native), and the rows are computed from them."""
    cap = REGISTRY["calendar"]
    assert cap.returns is ReturnKind.TABLE
    assert cap.params is market.CalendarParams
    crypto = cap.impls[AssetClass.CRYPTO]
    equity = cap.impls[AssetClass.EQUITY]
    assert (crypto.prov, crypto.basis, crypto.reads) == (
        Provenance.DERIVED,
        "caller_supplied",
        (),
    )
    assert (equity.prov, equity.basis, equity.reads) == (Provenance.DERIVED, "native", ())
    assert crypto.fn is not equity.fn
