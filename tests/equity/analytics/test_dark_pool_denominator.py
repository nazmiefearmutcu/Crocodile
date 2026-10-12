"""Participant percentages must use the corresponding symbol/week total."""

import polars as pl

from crocodile.equity.analytics.dark_pool import build_dark_pool_frame, dark_pool_market_share


def test_each_of_two_equal_participants_has_half_share_in_each_week_and_symbol():
    rows = [
        {
            "weekStartDate": week,
            "issueSymbolIdentifier": symbol,
            "marketParticipantName": participant,
            "totalWeeklyShareQuantity": 100,
            "totalWeeklyTradeCount": 2,
        }
        for week in ("2026-09-28", "2026-10-05")
        for symbol in ("AAPL", "MSFT")
        for participant in ("PARTICIPANT_A", "PARTICIPANT_B")
    ]
    result = dark_pool_market_share(build_dark_pool_frame(rows))
    assert len(result) == 8
    assert result["share_pct"].to_list() == [50.0] * 8
    totals = result.group_by(["week_start", "symbol"]).agg(pl.col("share_pct").sum())
    assert totals["share_pct"].to_list() == [100.0] * 4


def test_zero_share_group_does_not_change_other_symbols_or_weeks():
    frame = pl.DataFrame({
        "week_start": ["2026-09-28", "2026-09-28", "2026-10-05", "2026-10-05", "2026-10-05"],
        "symbol": ["AAPL", "AAPL", "AAPL", "AAPL", "MSFT"],
        "participant": ["A", "B", "A", "B", "A"],
        "shares": [0, 0, 100, 300, 200],
    })
    result = dark_pool_market_share(frame)
    assert result.filter(pl.col("week_start") == "2026-09-28")["share_pct"].to_list() == [0.0, 0.0]
    aapl = result.filter(
        (pl.col("week_start") == "2026-10-05") & (pl.col("symbol") == "AAPL")
    )
    assert sorted(aapl["share_pct"].to_list()) == [25.0, 75.0]
    assert result.filter(pl.col("symbol") == "MSFT")["share_pct"].to_list() == [100.0]
