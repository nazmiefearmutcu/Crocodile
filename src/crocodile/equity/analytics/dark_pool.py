"""FINRA ATS (Dark Pool) & OTC Wholesaler Internalization Analytics.

Processes weekly off-exchange transparency disclosures:
- Breaks down volume across Alternative Trading Systems (ATS dark pools like Liquidnet,
  Virtu MatchIt, UBS, Crossfinder).
- Identifies retail internalization by OTC market makers (Citadel Securities, Virtu,
  Two Sigma, Jane Street).
"""

from __future__ import annotations

from typing import Any

import polars as pl


def build_dark_pool_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Transform FINRA weeklySummary rows into an analytical Polars DataFrame."""
    if not rows:
        return pl.DataFrame(
            schema={
                "week_start": pl.Utf8,
                "symbol": pl.Utf8,
                "participant": pl.Utf8,
                "mpid": pl.Utf8,
                "venue_type": pl.Utf8,
                "shares": pl.Int64,
                "trades": pl.Int64,
                "avg_trade_size": pl.Float64,
                "notional_usd": pl.Int64,
            }
        )

    parsed = []
    for r in rows:
        code = str(r.get("summaryTypeCode", ""))
        venue_type = "ATS_DARK_POOL" if "ATS" in code else "OTC_WHOLESALER"
        shares = int(r.get("totalWeeklyShareQuantity") or 0)
        trades = int(r.get("totalWeeklyTradeCount") or 0)
        avg_trade_size = round(shares / trades, 2) if trades > 0 else 0.0

        parsed.append(
            {
                "week_start": str(r.get("weekStartDate", "")),
                "symbol": str(r.get("issueSymbolIdentifier", "")),
                "participant": str(r.get("marketParticipantName", r.get("MPID", "UNKNOWN"))),
                "mpid": str(r.get("MPID") or ""),
                "venue_type": venue_type,
                "shares": shares,
                "trades": trades,
                "avg_trade_size": avg_trade_size,
                "notional_usd": int(r.get("totalNotionalSum") or 0),
            }
        )

    df = pl.DataFrame(parsed)
    return df.sort(["week_start", "shares"], descending=[True, True])


def dark_pool_market_share(df: pl.DataFrame) -> pl.DataFrame:
    """Calculate market share percentages per dark pool and wholesaler."""
    if df.is_empty():
        return df

    total_shares = df["shares"].sum()
    if total_shares == 0:
        return df.with_columns(pl.lit(0.0).alias("share_pct"))

    return df.with_columns(
        (pl.col("shares") / total_shares * 100.0).round(2).alias("share_pct")
    ).sort("share_pct", descending=True)
