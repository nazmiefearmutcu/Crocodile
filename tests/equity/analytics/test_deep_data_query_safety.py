"""Issuer and timestamp inputs must remain data in real FINRA lake queries."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import polars as pl
import pytest

from crocodile.core.store.catalog import Catalog
from crocodile.equity.analytics.short_interest import short_interest_history
from crocodile.equity.analytics.short_volume import regsho_short_ratio


@pytest.fixture
def finra_lake(tmp_path: Path) -> Iterator[Catalog]:
    common = {"symbol": ["AAPL", "MSFT", "AAPL"], "local_ts": [10, 15, 20]}
    for channel, columns in [
        ("short_volume", {
            "date_val": ["2026-01-01", "2026-01-01", "2026-01-02"],
            "short_volume": [50.0, 20.0, 70.0],
            "total_volume": [100.0, 100.0, 100.0],
            "short_exempt_volume": [1.0, 2.0, 3.0],
        }),
        ("short_interest", {
            "settlement_date": ["2026-01-01", "2026-01-01", "2026-01-02"],
            "short_interest": [50.0, 20.0, 70.0],
            "prev_short_interest": [40.0, 15.0, 50.0],
            "days_to_cover": [1.0, 2.0, 3.0],
            "change_pct": [25.0, 33.0, 40.0],
        }),
    ]:
        part = tmp_path / "source=finra" / f"channel={channel}" / "date=2026-01-01"
        part.mkdir(parents=True)
        pl.DataFrame({**common, **columns}).write_parquet(part / "part-0001.parquet")
    with Catalog(tmp_path) as catalog:
        yield catalog


@pytest.mark.parametrize("reader", [regsho_short_ratio, short_interest_history])
def test_quoted_symbol_cannot_expand_to_other_issuers(finra_lake: Catalog, reader) -> None:
    selected = reader(finra_lake, "aapl", 0, 15)
    assert selected["symbol"].to_list() == ["AAPL"]
    assert reader(finra_lake, "AAPL' OR 1=1 --").is_empty()
    assert reader(finra_lake, "AAPL'; DROP VIEW short_volume; --").is_empty()
    assert reader(finra_lake, "MSFT")["symbol"].to_list() == ["MSFT"]


@pytest.mark.parametrize("reader", [regsho_short_ratio, short_interest_history])
@pytest.mark.parametrize("position", ["start_ns", "end_ns"])
def test_timestamp_sql_is_rejected_without_changing_the_lake(
    finra_lake: Catalog, reader, position: str,
) -> None:
    with pytest.raises(ValueError, match="integer nanosecond timestamps"):
        reader(finra_lake, "AAPL", **{position: "0 OR 1=1; DROP VIEW short_volume; --"})
    assert reader(finra_lake, "AAPL")["symbol"].to_list() == ["AAPL", "AAPL"]
