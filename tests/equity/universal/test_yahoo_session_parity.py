"""Legacy ``yahoo`` and ``yahoo_chart`` date the same payloads alike (spec section 6, acceptance 2).

Both readers get the same bytes: the task 07 fixture and this task's byte-identical copy. The
legacy side goes through yfinance's frame shape (``_yfinance_daily_frame`` from task 07), the
universal side through ``parse_chart``. Offline: both read recorded files only.
"""

from __future__ import annotations

import pathlib
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

import msgspec
import pytest

from crocodile.core.schema.records import OHLCV
from crocodile.equity.providers.yahoo import client as yahoo
from crocodile.equity.universal import yahoo_chart as yc
from tests.equity.test_yahoo_session_dates import _yfinance_daily_frame

UNIVERSAL_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "yahoo_chart"
LEGACY_FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures" / "yahoo_chart"
CAPTURED_NS = 1_794_000_000_000_000_000

# (task 07 fixture name, this task's copy, legacy yahoo symbol, listing)
PARITY: list[tuple[str, str, str, yc.ChartListing]] = [
    (
        "thyao_is.json",
        "THYAO.IS_5d_1d.json",
        "THYAO.IS",
        yc.ChartListing(
            symbol="THYAO@XIST",
            local="THYAO",
            mics=frozenset({"XIST"}),
            yahoo_symbol="THYAO.IS",
            tz=ZoneInfo("Europe/Istanbul"),
            calendar_mic="XIST",
        ),
    ),
    (
        "7203_t.json",
        "7203.T_5d_1d.json",
        "7203.T",
        yc.ChartListing(
            symbol="7203@XJPX",
            local="7203",
            mics=frozenset({"XJPX"}),
            yahoo_symbol="7203.T",
            tz=ZoneInfo("Asia/Tokyo"),
            calendar_mic="XJPX",
        ),
    ),
    (
        "2330_tw.json",
        "2330.TW_5d_1d.json",
        "2330.TW",
        yc.ChartListing(
            symbol="2330@XTAI",
            local="2330",
            mics=frozenset({"XTAI"}),
            yahoo_symbol="2330.TW",
            tz=ZoneInfo("Asia/Taipei"),
            calendar_mic="XTAI",
        ),
    ),
    (
        "bhp_ax.json",
        "BHP.AX_5d_1d.json",
        "BHP.AX",
        yc.ChartListing(
            symbol="BHP@XASX",
            local="BHP",
            mics=frozenset({"XASX"}),
            yahoo_symbol="BHP.AX",
            tz=ZoneInfo("Australia/Sydney"),
            calendar_mic="XASX",
        ),
    ),
]


@pytest.mark.parametrize(("legacy_name", "copy_name", "yahoo_symbol", "listing"), PARITY)
def test_the_legacy_yahoo_client_dates_the_same_payloads_identically(
    legacy_name: str,
    copy_name: str,
    yahoo_symbol: str,
    listing: yc.ChartListing,
) -> None:
    legacy_bytes = (LEGACY_FIXTURES / legacy_name).read_bytes()
    universal_bytes = (UNIVERSAL_FIXTURES / copy_name).read_bytes()
    assert legacy_bytes == universal_bytes, "the two fixture copies must be byte-identical"

    frame = _yfinance_daily_frame(legacy_name)
    legacy_dates = [
        record.source_ts
        for record in yahoo._daily_records(frame, yahoo_symbol, 7)
        if isinstance(record, OHLCV)
    ]
    payload: dict[str, Any] = msgspec.json.decode(universal_bytes)
    parse = yc.parse_chart(
        payload,
        listing,
        interval="1d",
        captured_at_ns=CAPTURED_NS,
        through_session=date(2026, 12, 31),
    )
    universal_dates = [record.source_ts for record in parse.records if isinstance(record, OHLCV)]

    assert legacy_dates
    assert universal_dates == legacy_dates
