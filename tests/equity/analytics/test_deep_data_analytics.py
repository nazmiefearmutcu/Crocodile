"""Unit tests for stock market deep data analytics.
Tests short volume, short interest, dark pool, and FTD modules.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import polars as pl
import pytest

from crocodile.equity.analytics.dark_pool import (
    build_dark_pool_frame,
    dark_pool_market_share,
)
from crocodile.equity.analytics.fails_to_deliver import (
    build_ftd_frame,
    detect_ftd_spikes,
    ftd_summary,
)
from crocodile.equity.analytics.short_interest import (
    short_interest_history,
    short_interest_metrics,
)
from crocodile.equity.analytics.short_volume import (
    regsho_short_ratio,
    short_volume_summary,
)
from crocodile.equity.providers.sec_edgar import SecEdgarClient


def test_short_volume_analytics() -> None:
    catalog = MagicMock()
    mock_df = pl.DataFrame(
        {
            "date": ["2024-09-19", "2024-09-20"],
            "symbol": ["GME", "GME"],
            "short_volume": [500000.0, 1200000.0],
            "total_volume": [1000000.0, 2000000.0],
            "short_exempt_volume": [1000.0, 5000.0],
            "short_ratio": [0.5000, 0.6000],
            "short_exempt_ratio": [0.0010, 0.0025],
            "is_elevated_short": [True, True],
        }
    )
    catalog.query.return_value = mock_df

    df = regsho_short_ratio(catalog, "GME")
    assert len(df) == 2
    assert "short_ratio" in df.columns

    summary = short_volume_summary(catalog, "GME")
    assert summary["symbol"] == "GME"
    assert summary["records_count"] == 2
    assert summary["avg_short_ratio"] == 0.55
    assert summary["max_short_ratio"] == 0.60
    assert summary["elevated_short_days"] == 2


def test_short_interest_analytics() -> None:
    catalog = MagicMock()
    mock_df = pl.DataFrame(
        {
            "settlement_date": ["2024-06-14", "2024-06-28"],
            "symbol": ["GME", "GME"],
            "short_interest": [44729815.0, 46000000.0],
            "prev_short_interest": [46538235.0, 44729815.0],
            "days_to_cover": [1.5, 1.8],
            "change_pct": [-3.89, 2.84],
            "net_shares_changed": [-1808420.0, 1270185.0],
        }
    )
    catalog.query.return_value = mock_df

    df = short_interest_history(catalog, "GME")
    assert len(df) == 2

    metrics = short_interest_metrics(catalog, "GME")
    assert metrics["symbol"] == "GME"
    assert metrics["latest_settlement_date"] == "2024-06-28"
    assert metrics["current_short_interest"] == 46000000.0
    assert metrics["days_to_cover"] == 1.8
    assert metrics["change_pct"] == 2.84


def test_dark_pool_analytics() -> None:
    raw_rows = [
        {
            "issueSymbolIdentifier": "GME",
            "summaryTypeCode": "ATS_W_SMBL_FIRM",
            "marketParticipantName": "KCGM VIRTU MATCHIT ATS",
            "MPID": "KCGM",
            "totalWeeklyShareQuantity": 400000,
            "totalWeeklyTradeCount": 2000,
            "totalNotionalSum": 10000000,
            "weekStartDate": "2024-06-03",
        },
        {
            "issueSymbolIdentifier": "GME",
            "summaryTypeCode": "OTC_W_SMBL_FIRM",
            "marketParticipantName": "CITADEL SECURITIES LLC",
            "MPID": "CDEL",
            "totalWeeklyShareQuantity": 600000,
            "totalWeeklyTradeCount": 3000,
            "totalNotionalSum": 15000000,
            "weekStartDate": "2024-06-03",
        },
    ]

    df = build_dark_pool_frame(raw_rows)
    assert len(df) == 2
    assert "ATS_DARK_POOL" in df["venue_type"].to_list()
    assert "OTC_WHOLESALER" in df["venue_type"].to_list()

    share_df = dark_pool_market_share(df)
    assert "share_pct" in share_df.columns
    # 600k / 1M = 60%, 400k / 1M = 40%
    assert 60.0 in share_df["share_pct"].to_list()
    assert 40.0 in share_df["share_pct"].to_list()


def test_fails_to_deliver_analytics() -> None:
    raw_ftd = [
        {
            "settlement_date": "2024-09-03",
            "cusip": "123456789",
            "symbol": "GME",
            "quantity": 50000.0,
            "price": 20.0,
            "dollar_value": 1000000.0,
            "description": "GAMESTOP CORP",
        },
        {
            "settlement_date": "2024-09-04",
            "cusip": "123456789",
            "symbol": "GME",
            "quantity": 150000.0,
            "price": 22.0,
            "dollar_value": 3300000.0,
            "description": "GAMESTOP CORP",
        },
    ]

    df = build_ftd_frame(raw_ftd)
    assert len(df) == 2

    stats = ftd_summary(df)
    assert stats["records_count"] == 2
    assert stats["max_fails_shares"] == 150000.0
    assert stats["total_fails_dollar_volume"] == 4300000.0

    spikes = detect_ftd_spikes(df, threshold_shares=100000.0)
    assert len(spikes) == 1
    assert spikes["quantity"][0] == 150000.0


@pytest.mark.asyncio
async def test_sec_edgar_get_fails_to_deliver(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import zipfile

    # Build a mock zip with pipe-separated CNS fails text
    cns_text = (
        "SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE\n"
        "20240903|36467W109|GME|12345|GAMESTOP CORP CL A|21.50\n"
        "20240903|037833100|AAPL|67890|APPLE INC|220.00\n"
        "Trailer record count 2\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("cnsfails202409a.txt", cns_text)
    zip_bytes = buf.getvalue()

    client = SecEdgarClient(user_agent="SampleApp/1.0 (test@example.com)")

    async def _mock_request_bytes(url: str) -> bytes:
        return zip_bytes

    monkeypatch.setattr(client, "_request_bytes", _mock_request_bytes)

    ftd_rows = await client.get_fails_to_deliver("GME", limit_months=1)
    assert len(ftd_rows) >= 1
    assert ftd_rows[0]["symbol"] == "GME"
    assert ftd_rows[0]["settlement_date"] == "2024-09-03"
    assert ftd_rows[0]["quantity"] == 12345.0
    assert ftd_rows[0]["price"] == 21.50
    assert ftd_rows[0]["dollar_value"] == round(12345.0 * 21.50, 2)
