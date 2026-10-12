"""FINRA Consolidated Short Interest Historical Analytics.

Analyzes official bi-weekly short positions reported by broker-dealers:
- Current vs Previous short positions
- Days to cover trends (short interest / average daily volume)
- Short interest percentage changes
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import polars as pl

if TYPE_CHECKING:
    from crocodile.core.store.catalog import Catalog

_END_OF_TIME = 2**63 - 1


def short_interest_history(
    catalog: Catalog,
    symbol: str,
    start_ns: int = 0,
    end_ns: int = _END_OF_TIME,
) -> pl.DataFrame:
    """Retrieve historical bi-weekly short interest positions from stored lake records.

    Reads the ``short_interest`` channel in the lake.
    """
    if (
        isinstance(start_ns, bool) or not isinstance(start_ns, int)
        or isinstance(end_ns, bool) or not isinstance(end_ns, int)
    ):
        raise ValueError("start_ns and end_ns must be integer nanosecond timestamps")
    sql = """
    SELECT
        settlement_date,
        symbol,
        short_interest,
        prev_short_interest,
        days_to_cover,
        change_pct,
        (short_interest - prev_short_interest) AS net_shares_changed
    FROM "short_interest"
    WHERE symbol = ?
      AND local_ts >= ?
      AND local_ts <= ?
    ORDER BY settlement_date ASC
    """
    return catalog.query(sql, params=[symbol.upper(), start_ns, end_ns])


def short_interest_metrics(catalog: Catalog, symbol: str) -> dict[str, Any]:
    """Compute summary short interest metrics and days-to-cover risk."""
    df = short_interest_history(catalog, symbol)
    if df.is_empty():
        return {
            "symbol": symbol.upper(),
            "latest_settlement_date": None,
            "current_short_interest": None,
            "days_to_cover": None,
            "change_pct": None,
            "max_short_interest": None,
            "min_short_interest": None,
        }

    latest = df[-1]
    return {
        "symbol": symbol.upper(),
        "latest_settlement_date": str(latest["settlement_date"][0]),
        "current_short_interest": float(latest["short_interest"][0]),
        "days_to_cover": float(latest["days_to_cover"][0])
        if latest["days_to_cover"][0] is not None
        else None,
        "change_pct": float(latest["change_pct"][0])
        if latest["change_pct"][0] is not None
        else None,
        "max_short_interest": float(df["short_interest"].max()),  # type: ignore[arg-type]
        "min_short_interest": float(df["short_interest"].min()),  # type: ignore[arg-type]
    }
