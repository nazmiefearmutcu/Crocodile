"""The local-state columns of ``markets --asset-class equity`` (task 15, steps 3 and 5).

``listings_known`` counts the listing master, ``last_success`` reads the universal state, and
``usage_classes`` reads each source's declared class. A read never creates or writes a file,
and an unreadable or newer file degrades to nulls with one warning. Every test seeds a
``tmp_path`` lake through the real ``ListingMaster`` and ``UniversalState`` APIs and never
touches the network.
"""

from __future__ import annotations

import datetime
import logging
import sqlite3
from pathlib import Path

import polars as pl
import pytest

from crocodile.capabilities import market
from crocodile.core.capability import CapabilityContext
from crocodile.core.config import Settings
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.master import ListingMaster, ListingObservation, ListingStatus
from crocodile.core.schema.enums import AssetClass
from crocodile.equity.universal import state as state_module
from crocodile.equity.universal.order import source_order, usage_class_of
from crocodile.equity.universal.state import (
    STATE_SCHEMA_VERSION,
    MarketCoverage,
    UniversalState,
    read_market_coverage,
)
from tests.equity.universal.fakes import FIXED_NS, seed_master

pytestmark = pytest.mark.usefixtures("network_forbidden")

COVERAGE_COLUMNS: tuple[str, ...] = ("usage_classes", "listings_known", "last_success")
SPEC_ORDER: list[str] = [
    "venue",
    "native",
    "ccxt",
    "mic",
    "name",
    "country",
    "timezone",
    "currency",
    "quote_scale",
    "calendar_quality",
    "sources",
    "usage_classes",
    "tier_research",
    "sanctioned",
    "listings_known",
    "last_success",
]
SESSION = datetime.date(2024, 3, 8)
EARLIER_NS = FIXED_NS - 3_600_000_000_000
LATER_NS = FIXED_NS


def _lake_context(lake: Path, *, readonly: bool = False) -> CapabilityContext:
    return CapabilityContext(
        catalog=None,
        settings=Settings(data_dir=lake),
        asset_class=AssetClass.EQUITY,
        readonly=readonly,
    )


def _xist_listing(symbol: str, local: str, status: ListingStatus) -> ListingObservation:
    return ListingObservation(
        source="tradingview",
        mic="XIST",
        local_symbol=local,
        symbol_raw=f"BIST:{local}",
        observed_ns=FIXED_NS,
        currency="TRY",
        price_scale=0,
        status=status,
    )


def _seed_xist(lake: Path) -> None:
    master = ListingMaster.for_data_dir(lake)
    try:
        seed_master(
            master,
            [
                _xist_listing("THYAO", "THYAO", ListingStatus.ACTIVE),
                _xist_listing("GARAN", "GARAN", ListingStatus.ACTIVE),
                _xist_listing("OLDCO", "OLDCO", ListingStatus.DELISTED),
            ],
        )
    finally:
        master.close()


def _seed_state(lake: Path) -> None:
    state = UniversalState.at(lake)
    try:
        state.record_eod(source="tradingview", mic="XIST", session=SESSION, at_ns=EARLIER_NS)
        state.record_success(
            source="yahoo_chart",
            symbol="THYAO@XIST",
            mic="XIST",
            channel="ohlcv",
            interval="1d",
            covered_from_ns=EARLIER_NS,
            covered_until_ns=EARLIER_NS,
            at_ns=LATER_NS,
        )
    finally:
        state.close()


# ---------------------------------------------------------------- reading local state


def test_read_market_coverage_counts_listings_that_are_not_delisted(tmp_path: Path) -> None:
    _seed_xist(tmp_path)
    coverage = read_market_coverage(tmp_path)
    assert coverage.listings_known("XIST") == 2
    assert coverage.listings_known("XNAS") == 0


def test_read_market_coverage_takes_last_success_from_the_universal_state(tmp_path: Path) -> None:
    _seed_state(tmp_path)
    coverage = read_market_coverage(tmp_path)
    assert coverage.last_success_ns("XIST") == LATER_NS
    assert coverage.last_success_ns("XNAS") is None


def test_read_market_coverage_on_an_empty_lake_creates_no_file(tmp_path: Path) -> None:
    coverage = read_market_coverage(tmp_path)
    assert coverage.listings_known("XIST") == 0
    assert coverage.last_success_ns("XIST") is None
    assert not (tmp_path / "reference" / "listings.sqlite").exists()
    assert not (tmp_path / "state" / "universal.sqlite").exists()
    assert list(tmp_path.iterdir()) == []


def test_an_unreadable_state_file_degrades_to_unknown(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state_path = UniversalState.path_for(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(b"this is not a sqlite database at all" * 40)
    with caplog.at_level(logging.WARNING, logger=state_module.__name__):
        coverage = read_market_coverage(tmp_path)
    assert coverage.last_success is None
    assert coverage.last_success_ns("XIST") is None
    assert any("universal.sqlite" in record.getMessage() for record in caplog.records)


def test_a_newer_state_or_master_schema_degrades_to_unknown(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_xist(tmp_path)
    _seed_state(tmp_path)
    _set_schema_version(UniversalState.path_for(tmp_path), STATE_SCHEMA_VERSION + 1)
    _set_schema_version(tmp_path / "reference" / "listings.sqlite", 99)

    with caplog.at_level(logging.WARNING, logger=state_module.__name__):
        coverage = read_market_coverage(tmp_path)

    assert coverage.last_success is None
    assert coverage.listings is None
    assert coverage.listings_known("XIST") is None
    assert coverage.last_success_ns("XIST") is None
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def _set_schema_version(path: Path, version: int) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE schema_version SET version = ?", (version,))
        connection.commit()
    finally:
        connection.close()


def test_market_coverage_is_a_plain_value_with_unknown_states() -> None:
    unknown = MarketCoverage(listings=None, last_success=None)
    assert unknown.listings_known("XIST") is None
    assert unknown.last_success_ns("XIST") is None
    known = MarketCoverage(listings={"XIST": 2}, last_success={"XIST": 7})
    assert (known.listings_known("XIST"), known.listings_known("XNAS")) == (2, 0)
    assert (known.last_success_ns("XIST"), known.last_success_ns("XNAS")) == (7, None)


def test_usage_class_of_reads_the_declared_class() -> None:
    assert usage_class_of("yahoo_chart") == "internal"
    assert usage_class_of("twse") == "redistribution"
    assert usage_class_of("stooq") is None
    assert usage_class_of("no_such_source") is None


# ---------------------------------------------------------------- markets columns


def test_equity_markets_has_the_coverage_columns_in_spec_order(tmp_path: Path) -> None:
    frame = market.markets_equities(_lake_context(tmp_path), market.MarketsParams())
    assert frame.columns == SPEC_ORDER
    assert frame.columns == list(market._MARKETS_SCHEMA)
    assert frame.schema["usage_classes"] == pl.List(pl.String())
    assert frame.schema["listings_known"] == pl.Int64()
    assert frame.schema["last_success"] == pl.Int64()


def test_the_crypto_half_leaves_the_coverage_columns_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(market.factory, "list_exchanges", lambda: ["binance", "base_onchain"])
    monkeypatch.setattr(market.factory, "list_ccxt_exchanges", lambda: ["binance", "kraken"])
    monkeypatch.setattr(
        market.factory, "list_all_exchanges", lambda: ["base_onchain", "binance", "kraken"]
    )
    _seed_xist(tmp_path)
    crypto = market.markets(
        CapabilityContext(catalog=None, settings=Settings(), asset_class=AssetClass.CRYPTO),
        market.MarketsParams(),
    )
    equity = market.markets_equities(_lake_context(tmp_path), market.MarketsParams())
    assert crypto.height == 3
    for column in COVERAGE_COLUMNS:
        assert crypto[column].to_list() == [None] * crypto.height, column
    assert crypto.columns == equity.columns == list(market._MARKETS_SCHEMA)
    assert crypto.schema == equity.schema


def test_every_exchange_appears_once(tmp_path: Path) -> None:
    frame = market.markets_equities(_lake_context(tmp_path), market.MarketsParams())
    assert frame.height == len(ExchangeRegistry().all())
    assert frame["mic"].n_unique() == frame.height


def test_listings_known_and_last_success_come_from_local_state(tmp_path: Path) -> None:
    _seed_xist(tmp_path)
    _seed_state(tmp_path)
    rows = {
        row["mic"]: row
        for row in market.markets_equities(
            _lake_context(tmp_path), market.MarketsParams()
        ).to_dicts()
    }
    assert (rows["XIST"]["listings_known"], rows["XIST"]["last_success"]) == (2, LATER_NS)
    assert (rows["XNAS"]["listings_known"], rows["XNAS"]["last_success"]) == (0, None)


def test_sources_keep_the_task_08_set_in_the_declared_order(tmp_path: Path) -> None:
    frame = market.markets_equities(_lake_context(tmp_path), market.MarketsParams(mic="XIST"))
    (row,) = frame.to_dicts()
    serving = market._serving_sources("XIST", market._grammar_mics())
    assert set(row["sources"]) == set(serving)
    assert len(row["sources"]) == len(set(row["sources"]))

    order = source_order()
    chain: list[str] = []
    for channel in ("ohlcv", "instrument", "corp_action"):
        chain.extend(order.order_for("XIST", channel))
    ranked = [name for name in dict.fromkeys(chain) if name in serving]
    rest = sorted(name for name in serving if name not in ranked)
    assert row["sources"] == ranked + rest
    assert row["usage_classes"] == [usage_class_of(name) for name in row["sources"]]


def test_the_mic_and_country_filters_keep_the_new_columns(tmp_path: Path) -> None:
    _seed_xist(tmp_path)
    by_country = market.markets_equities(
        _lake_context(tmp_path), market.MarketsParams(country="TR")
    )
    assert by_country["mic"].to_list() == ["XIST"]
    assert by_country.columns == SPEC_ORDER
    assert by_country["listings_known"].to_list() == [2]
    by_mic = market.markets_equities(_lake_context(tmp_path), market.MarketsParams(mic="XIST"))
    assert by_mic.columns == SPEC_ORDER
    assert by_mic["last_success"].to_list() == [None]


def test_markets_reads_local_state_on_a_read_only_surface_without_writing(tmp_path: Path) -> None:
    _seed_xist(tmp_path)
    _seed_state(tmp_path)
    before = _snapshot_of(tmp_path)
    frame = market.markets_equities(
        _lake_context(tmp_path, readonly=True), market.MarketsParams(mic="XIST")
    )
    after = _snapshot_of(tmp_path)
    assert frame["listings_known"].to_list() == [2]
    assert after == before


def _snapshot_of(lake: Path) -> dict[str, int]:
    return {
        path.relative_to(lake).as_posix(): path.stat().st_mtime_ns
        for path in sorted(lake.rglob("*"))
        if path.is_file()
    }
