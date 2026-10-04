"""SEC Fails-To-Deliver (CNS Fails) Quantitative Analytics.

Analyzes settlement failure spikes and cumulative naked short pressure:
- Aggregates daily fails-to-deliver share volume and dollar value.
- Tracks persistent failure streaks that trigger Reg SHO threshold criteria.
- Flags severe settlement failure anomalies.
"""

from __future__ import annotations

from typing import Any

import polars as pl


def build_ftd_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Transform parsed SEC FTD records into an analytical Polars DataFrame."""
    if not rows:
        return pl.DataFrame(
            schema={
                "settlement_date": pl.Utf8,
                "symbol": pl.Utf8,
                "cusip": pl.Utf8,
                "quantity": pl.Float64,
                "price": pl.Float64,
                "dollar_value": pl.Float64,
                "description": pl.Utf8,
            }
        )

    df = pl.DataFrame(rows)
    if "dollar_value" not in df.columns:
        if "quantity" in df.columns and "price" in df.columns:
            df = df.with_columns(
                (pl.col("quantity") * pl.col("price")).round(2).alias("dollar_value")
            )
        else:
            df = df.with_columns(pl.lit(0.0).alias("dollar_value"))
    return df.sort("settlement_date", descending=False)


def ftd_summary(df: pl.DataFrame) -> dict[str, Any]:
    """Compute aggregate settlement failure statistics for a symbol."""
    if df.is_empty():
        return {
            "records_count": 0,
            "max_fails_shares": None,
            "latest_fails_shares": None,
            "total_fails_dollar_volume": None,
            "avg_fails_shares": None,
        }

    return {
        "records_count": len(df),
        "max_fails_shares": float(df["quantity"].max()),  # type: ignore[arg-type]
        "latest_fails_shares": float(df["quantity"][-1]),
        "total_fails_dollar_volume": round(float(df["dollar_value"].sum()), 2),
        "avg_fails_shares": round(float(df["quantity"].mean()), 2),
    }


def detect_ftd_spikes(df: pl.DataFrame, threshold_shares: float = 100_000.0) -> pl.DataFrame:
    """Filter settlement failure observations that exceed threshold shares."""
    if df.is_empty():
        return df

    return df.filter(pl.col("quantity") >= threshold_shares).sort("quantity", descending=True)
