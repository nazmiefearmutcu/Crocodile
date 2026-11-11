"""Gate (spec 6): the listing master cannot hold two rows for one identity.

The uniqueness rules are read back from SQLite's own index catalogue, so a schema edit that drops
a constraint fails here even when no behaviour test exercises it.
"""

from typing import Any

import pytest

from crocodile.core.markets import errors as market_errors
from crocodile.core.markets import master as master_module
from crocodile.core.markets.master import ListingMaster, ListingObservation

pytestmark = pytest.mark.usefixtures("network_forbidden")

DAY = 86_400_000_000_000
T1 = 1_762_560_000_000_000_000


def _indexes(master: ListingMaster, table: str) -> list[dict[str, Any]]:
    """Each index of ``table`` with its uniqueness, partiality and column names."""
    found: list[dict[str, Any]] = []
    for row in master._conn.execute(f"PRAGMA index_list('{table}')").fetchall():
        columns = [
            info["name"]
            for info in master._conn.execute(f"PRAGMA index_info('{row['name']}')").fetchall()
        ]
        found.append(
            {
                "name": row["name"],
                "unique": bool(row["unique"]),
                "partial": bool(row["partial"]),
                "columns": columns,
            }
        )
    return found


def test_the_listings_table_is_unique_on_market_and_local_symbol() -> None:
    with ListingMaster() as master:
        indexes = _indexes(master, "listings")
        assert any(
            index["unique"] and index["columns"] == ["mic", "local_symbol"] for index in indexes
        )


def test_the_listings_table_is_unique_on_the_canonical_symbol() -> None:
    with ListingMaster() as master:
        indexes = _indexes(master, "listings")
        assert any(index["unique"] and index["columns"] == ["symbol"] for index in indexes)


def test_open_aliases_are_unique_per_spelling() -> None:
    with ListingMaster() as master:
        indexes = {index["name"]: index for index in _indexes(master, "symbol_aliases")}
        open_raw = indexes["idx_aliases_open_raw"]
        assert open_raw["partial"] is True
        assert open_raw["unique"] is True


def test_000001_on_xshe_and_xshg_are_two_listings() -> None:
    with ListingMaster() as master:
        for mic, raw in (("XSHE", "SZSE:000001"), ("XSHG", "SSE:000001")):
            master.upsert(
                ListingObservation(
                    source="tradingview",
                    mic=mic,
                    local_symbol="000001",
                    symbol_raw=raw,
                    observed_ns=T1,
                )
            )
        symbols = [listing.symbol for listing in master.listings()]
        assert sorted(symbols) == ["000001@XSHE", "000001@XSHG"]


def test_master_reexports_the_one_listing_conflict_error() -> None:
    assert master_module.ListingConflictError is market_errors.ListingConflictError
