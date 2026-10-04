"""Tests for the FINRA market data client and provider."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from crocodile.core.schema.enums import AssetClass, Channel
from crocodile.core.schema.provenance import Provenance
from crocodile.core.sink.memory import MemorySink
from crocodile.equity.providers.factory import make_provider
from crocodile.equity.providers.finra import FinraClient, FinraProvider
from crocodile.equity.reference.registry import InstrumentRegistry

_SAMPLE_REGSHO_TXT = b"""Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market
20240920|AAPL|219565|100|362575|B,Q,N
20240920|GME|983508|564|2162484|B,Q,N
20240920|TSLA|150145|530|280157|Q,N
"""

_SAMPLE_SHORT_INTEREST_JSON = [
    {
        "symbolCode": "GME",
        "settlementDate": "2024-06-14",
        "currentShortPositionQuantity": 44729815,
        "previousShortPositionQuantity": 46538235,
        "daysToCoverQuantity": 1.5,
        "changePercent": -3.89,
    },
    {
        "symbolCode": "GME",
        "settlementDate": "2024-06-28",
        "currentShortPositionQuantity": 46000000,
        "previousShortPositionQuantity": 44729815,
        "daysToCoverQuantity": 1.8,
        "changePercent": 2.84,
    },
]

_SAMPLE_ATS_WEEKLY_JSON = [
    {
        "issueSymbolIdentifier": "GME",
        "summaryTypeCode": "ATS_W_SMBL_FIRM",
        "marketParticipantName": "KCGM VIRTU MATCHIT ATS",
        "MPID": "KCGM",
        "totalWeeklyShareQuantity": 397021,
        "totalWeeklyTradeCount": 4060,
        "totalNotionalSum": 72449116,
        "weekStartDate": "2024-06-03",
    },
    {
        "issueSymbolIdentifier": "GME",
        "summaryTypeCode": "OTC_W_SMBL_FIRM",
        "marketParticipantName": "CITADEL SECURITIES LLC",
        "MPID": "CDEL",
        "totalWeeklyShareQuantity": 1500000,
        "totalWeeklyTradeCount": 18200,
        "totalNotionalSum": 320000000,
        "weekStartDate": "2024-06-03",
    },
]

_SAMPLE_THRESHOLD_TXT = (
    b"Symbol|Security Name|Market Category|Reg SHO Threshold Flag|Rule 3210|Filler\n"
    b"GME|GAMESTOP CORP CL A|S|Y|N|\n"
    b"ASST|ASSET ENTITIES INC CL B NEW|S|Y|N|\n"
)


class _MockFinraClient(FinraClient):
    """Stubbed FINRA client returning canned responses without hitting network."""

    def __init__(self) -> None:
        super().__init__()

    async def _get(self, url: str) -> tuple[int, bytes]:
        if "CNMSshvol" in url:
            return 200, _SAMPLE_REGSHO_TXT
        if "nasdaqth" in url:
            return 200, _SAMPLE_THRESHOLD_TXT
        return 404, b""

    async def _post_json(self, url: str, payload: dict[str, Any]) -> tuple[int, Any]:
        if "consolidatedShortInterest" in url:
            return 200, _SAMPLE_SHORT_INTEREST_JSON
        if "weeklySummary" in url:
            return 200, _SAMPLE_ATS_WEEKLY_JSON
        return 404, []


@pytest.mark.asyncio
async def test_finra_client_regsho_parsing() -> None:
    client = _MockFinraClient()
    records = await client.get_regsho_short_volume_day(date(2024, 9, 20), symbol="GME")

    assert len(records) == 1
    rec = records[0]
    assert rec.symbol == "GME"
    assert rec.date == "2024-09-20"
    assert rec.short_volume == 983508.0
    assert rec.short_exempt_volume == 564.0
    assert rec.total_volume == 2162484.0
    assert rec.source == "finra"
    assert rec.asset_class == AssetClass.EQUITY
    assert rec.prov == Provenance.NATIVE
    assert rec.prov_basis == "native"
    assert rec.prov_confidence == 1.0


@pytest.mark.asyncio
async def test_finra_client_short_interest_parsing() -> None:
    client = _MockFinraClient()
    records = await client.get_short_interest("GME")

    assert len(records) == 2
    rec1 = records[0]
    assert rec1.symbol == "GME"
    assert rec1.settlement_date == "2024-06-14"
    assert rec1.short_interest == 44729815.0
    assert rec1.prev_short_interest == 46538235.0
    assert rec1.days_to_cover == 1.5
    assert rec1.change_pct == -3.89
    assert rec1.source == "finra"
    assert rec1.prov == Provenance.NATIVE


@pytest.mark.asyncio
async def test_finra_client_ats_weekly_parsing() -> None:
    client = _MockFinraClient()
    rows = await client.get_ats_weekly_summary("GME")

    assert len(rows) == 2
    assert rows[0]["MPID"] == "KCGM"
    assert rows[0]["summaryTypeCode"] == "ATS_W_SMBL_FIRM"
    assert rows[1]["MPID"] == "CDEL"
    assert rows[1]["marketParticipantName"] == "CITADEL SECURITIES LLC"


@pytest.mark.asyncio
async def test_finra_client_threshold_securities() -> None:
    client = _MockFinraClient()
    symbols = await client.get_threshold_securities()

    assert "GME" in symbols
    assert "ASST" in symbols


@pytest.mark.asyncio
async def test_finra_provider_backfill_short_volume() -> None:
    client = _MockFinraClient()
    sink = MemorySink()
    provider = FinraProvider(
        symbols=["GME"],
        channels=[Channel.SHORT_VOLUME.value],
        out=sink,
        registry=InstrumentRegistry(),
        client=client,
    )

    # 2024-09-20 in nanoseconds
    start_ns = 1_726_790_400_000_000_000  # 2024-09-20 00:00:00 UTC
    end_ns = start_ns + 86_400 * 1_000_000_000

    recs = [
        rec async for rec in provider.backfill(Channel.SHORT_VOLUME.value, "GME", start_ns, end_ns)
    ]
    assert len(recs) == 1
    assert recs[0].symbol == "GME"
    assert recs[0].short_volume == 983508.0


@pytest.mark.asyncio
async def test_finra_provider_backfill_short_interest() -> None:
    client = _MockFinraClient()
    sink = MemorySink()
    provider = FinraProvider(
        symbols=["GME"],
        channels=[Channel.SHORT_INTEREST.value],
        out=sink,
        registry=InstrumentRegistry(),
        client=client,
    )

    # 2024-06-01 to 2024-06-20
    start_ns = 1_717_200_000_000_000_000
    end_ns = 1_718_841_600_000_000_000

    recs = [
        rec
        async for rec in provider.backfill(Channel.SHORT_INTEREST.value, "GME", start_ns, end_ns)
    ]
    # First record is 2024-06-14 (in range), second is 2024-06-28 (out of range)
    assert len(recs) == 1
    assert recs[0].settlement_date == "2024-06-14"
    assert recs[0].short_interest == 44729815.0


def test_make_provider_instantiates_finra() -> None:
    sink = MemorySink()
    provider = make_provider(
        provider="finra",
        symbols=["GME"],
        channels=[Channel.SHORT_VOLUME.value],
        out=sink,
        registry=InstrumentRegistry(),
    )
    assert isinstance(provider, FinraProvider)
    assert provider.name == "finra"
    assert Channel.SHORT_VOLUME.value in provider.supported_channels
    assert Channel.SHORT_INTEREST.value in provider.supported_channels
