"""FINRA Reg SHO Short Sale Volume Microstructure Analytics.

Computes off-exchange and consolidated short selling metrics:
- Short Volume Ratio: short_volume / total_volume
- Short Exempt Ratio: short_exempt_volume / total_volume
- Elevated Short Selling flags (>50% of daily volume marked short)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import polars as pl

if TYPE_CHECKING:
    from crocodile.core.store.catalog import Catalog

_END_OF_TIME = 2**63 - 1


def regsho_short_ratio(
    catalog: Catalog,
    symbol: str,
    start_ns: int = 0,
    end_ns: int = _END_OF_TIME,
) -> pl.DataFrame:
    """Compute daily Reg SHO short sale ratio and short exempt ratio from stored records.

    Reads the ``short_volume`` channel in the lake.
    """
    if (
        isinstance(start_ns, bool) or not isinstance(start_ns, int)
        or isinstance(end_ns, bool) or not isinstance(end_ns, int)
    ):
        raise ValueError("start_ns and end_ns must be integer nanosecond timestamps")
    sql = """
    SELECT
        date_val AS date,
        symbol,
        short_volume,
        total_volume,
        short_exempt_volume,
        ROUND(short_volume / NULLIF(total_volume, 0), 4) AS short_ratio,
        ROUND(short_exempt_volume / NULLIF(total_volume, 0), 4) AS short_exempt_ratio,
        (short_volume / NULLIF(total_volume, 0) >= 0.50) AS is_elevated_short
    FROM "short_volume"
    WHERE symbol = ?
      AND local_ts >= ?
      AND local_ts <= ?
    ORDER BY date_val ASC
    """
    df = catalog.query(sql, params=[symbol.upper(), start_ns, end_ns])
    return df


def short_volume_summary(catalog: Catalog, symbol: str) -> dict[str, Any]:
    """Compute aggregate short sale summary statistics for a symbol."""
    df = regsho_short_ratio(catalog, symbol)
    if df.is_empty():
        return {
            "symbol": symbol.upper(),
            "records_count": 0,
            "avg_short_ratio": None,
            "max_short_ratio": None,
            "latest_short_ratio": None,
            "elevated_short_days": 0,
        }

    short_ratios = df["short_ratio"].drop_nulls()
    return {
        "symbol": symbol.upper(),
        "records_count": len(df),
        "avg_short_ratio": round(float(short_ratios.mean()), 4)
        if not short_ratios.is_empty()
        else None,
        "max_short_ratio": round(float(short_ratios.max()), 4)
        if not short_ratios.is_empty()
        else None,
        "latest_short_ratio": round(float(short_ratios[-1]), 4)
        if not short_ratios.is_empty()
        else None,
        "elevated_short_days": int(df["is_elevated_short"].sum()),
    }
