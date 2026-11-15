"""``sse_yunhq``: the Shanghai Stock Exchange's snap and trd1 answers, from captured fixtures."""

from __future__ import annotations

import dataclasses
import json
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from crocodile.core.schema.enums import Side
from crocodile.core.schema.records import BookSnapshot, Quote, Trade
from crocodile.equity.universal.base import SourceUnservableError
from crocodile.equity.universal.http import HttpRequest, HttpResponse
from crocodile.equity.universal.live.base import LivePayloadError
from crocodile.equity.universal.live.model import LiveListing, LiveTier, snapshot_records
from crocodile.equity.universal.live.sources import sse_yunhq

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live" / "sse_yunhq"
CAPTURED_NS = 1_794_471_000_000_000_000
MOUTAI = LiveListing(mic="XSHG", local_symbol="600519")
SHANGHAI = ZoneInfo("Asia/Shanghai")
# 2026-11-12 16:15:38 Asia/Shanghai, the snap's own stamp.
SNAP_TS_NS = 1_794_471_338_000_000_000
NAME = chr(0x8D35) + chr(0x5DDE) + chr(0x8305) + chr(0x53F0)


def _snap_bytes() -> bytes:
    return (FIXTURES / "snap_600519.json").read_bytes()


def _trd_bytes() -> bytes:
    return (FIXTURES / "trd1_600519.json").read_bytes()


def _date() -> date:
    return date(2026, 11, 12)


def _snap_doc() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(_snap_bytes())
    return document


def _dump(document: dict[str, Any]) -> bytes:
    return json.dumps(document).encode("utf-8")


def test_parses_the_snapshot_fields_in_their_stated_units() -> None:
    snapshot = sse_yunhq.parse_snap(_snap_bytes(), MOUTAI, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.symbol_raw == "600519"
    assert snapshot.name == NAME
    assert snapshot.last == 1410.27
    assert snapshot.last_size is None
    assert snapshot.prev_close == 1402.3
    assert (snapshot.open, snapshot.high, snapshot.low) == (1395.13, 1411.0, 1388.11)
    # Shares, as stated: amount / volume is 1401.42, a price, so volume is not in lots.
    assert snapshot.volume == 3_237_294.0
    assert snapshot.turnover == 4_536_814_518.0
    assert snapshot.source_ts_ns == SNAP_TS_NS
    assert snapshot.currency == "CNY"
    assert snapshot.price_scale == 0
    assert snapshot.delay_s is None
    assert snapshot.tier is LiveTier.L2


def test_ladders_are_best_first_with_shares_and_no_conversion() -> None:
    snapshot = sse_yunhq.parse_snap(_snap_bytes(), MOUTAI, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert [(level.price, level.size) for level in snapshot.bids] == [
        (1410.27, 1655.0),
        (1410.22, 100.0),
        (1410.21, 400.0),
        (1410.19, 400.0),
        (1410.0, 100.0),
    ]
    assert [(level.price, level.size) for level in snapshot.asks] == [
        (1410.43, 200.0),
        (1410.45, 100.0),
        (1410.46, 100.0),
        (1410.5, 500.0),
        (1410.55, 100.0),
    ]


def test_a_listing_that_was_not_asked_for_is_not_returned() -> None:
    other = LiveListing(mic="XSHG", local_symbol="600000")
    assert sse_yunhq.parse_snap(_snap_bytes(), other, captured_ns=CAPTURED_NS) is None


def test_an_empty_snapshot_is_none() -> None:
    document = _snap_doc()
    document["snap"] = [NAME, None, None, None, None, None, None, None, [], [], "E110    "]
    assert sse_yunhq.parse_snap(_dump(document), MOUTAI, captured_ns=CAPTURED_NS) is None


def test_a_zero_size_level_keeps_its_price_and_loses_its_size() -> None:
    document = _snap_doc()
    document["snap"][8][1] = 0
    snapshot = sse_yunhq.parse_snap(_dump(document), MOUTAI, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.bids[0].price == 1410.27
    assert snapshot.bids[0].size is None


def test_a_non_monotone_ladder_is_refused_whole() -> None:
    document = _snap_doc()
    document["snap"][8] = [1410.22, 100, 1410.27, 1655]
    snapshot = sse_yunhq.parse_snap(_dump(document), MOUTAI, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.bids == ()
    assert snapshot.tier is LiveTier.L2  # the five asks still stand on their own


def test_a_short_snap_array_is_a_payload_error() -> None:
    document = _snap_doc()
    document["snap"] = document["snap"][:9]
    with pytest.raises(LivePayloadError):
        sse_yunhq.parse_snap(_dump(document), MOUTAI, captured_ns=CAPTURED_NS)


def test_a_non_list_bid_is_a_payload_error() -> None:
    document = _snap_doc()
    document["snap"][8] = "1410.27,1655"
    with pytest.raises(LivePayloadError):
        sse_yunhq.parse_snap(_dump(document), MOUTAI, captured_ns=CAPTURED_NS)


def test_an_empty_or_html_body_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        sse_yunhq.parse_snap(b"", MOUTAI, captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        sse_yunhq.parse_snap(b"<html>", MOUTAI, captured_ns=CAPTURED_NS)


def test_a_zero_price_is_not_a_price() -> None:
    document = _snap_doc()
    document["snap"][1:5] = [0, 0, 0, 0]  # prev_close, open, high, low
    document["snap"][5] = 0  # last
    snapshot = sse_yunhq.parse_snap(_dump(document), MOUTAI, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert (snapshot.last, snapshot.prev_close) == (None, None)
    assert (snapshot.open, snapshot.high, snapshot.low) == (None, None, None)
    assert snapshot.volume == 3_237_294.0  # a figure of its own, unaffected
    assert snapshot.tier is LiveTier.L2
    assert not any(isinstance(r, Trade) for r in snapshot_records(snapshot))


def test_a_zero_last_with_no_book_is_an_empty_snapshot() -> None:
    document = _snap_doc()
    document["snap"][5] = 0
    document["snap"][8] = []
    document["snap"][9] = []
    assert sse_yunhq.parse_snap(_dump(document), MOUTAI, captured_ns=CAPTURED_NS) is None


def test_a_zero_volume_is_kept_and_a_negative_one_is_not() -> None:
    document = _snap_doc()
    document["snap"][6] = 0
    document["snap"][7] = -1
    snapshot = sse_yunhq.parse_snap(_dump(document), MOUTAI, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.volume == 0.0
    assert snapshot.turnover is None


def test_an_out_of_range_date_is_no_timestamp_and_no_crash() -> None:
    for stamp in (10**30, 20261132, 99991399, 0):
        document = _snap_doc()
        document["date"] = stamp
        snapshot = sse_yunhq.parse_snap(_dump(document), MOUTAI, captured_ns=CAPTURED_NS)
        assert snapshot is not None
        assert snapshot.source_ts_ns is None


def test_the_tape_is_the_publishers_prints_oldest_first() -> None:
    prints = sse_yunhq.parse_trades(_trd_bytes(), _date(), SHANGHAI)
    assert len(prints) == 19
    first, last = prints[0], prints[-1]
    assert (first.price, first.size, first.ts_ns) == (1403.5, 3500.0, 1_794_466_567_000_000_000)
    assert first.side is Side.UNKNOWN and first.id is None
    assert (last.price, last.size, last.ts_ns) == (
        1410.27,
        198_245.0,
        1_794_466_801_000_000_000,
    )
    assert [p.ts_ns for p in prints] == sorted(p.ts_ns for p in prints if p.ts_ns is not None)


def test_a_print_without_a_positive_price_or_size_is_dropped() -> None:
    body = _dump({"trd1": [[145607, 1403.5, 0], [145610, 0, 100], [145613, 1404.0, 300]]})
    prints = sse_yunhq.parse_trades(body, _date(), SHANGHAI)
    assert [(p.price, p.size) for p in prints] == [(1404.0, 300.0)]


def test_a_print_with_an_unreadable_time_keeps_price_and_size() -> None:
    body = _dump({"trd1": [[99999, 1403.5, 100]]})
    (only,) = sse_yunhq.parse_trades(body, _date(), SHANGHAI)
    assert (only.price, only.size, only.ts_ns) == (1403.5, 100.0, None)


def test_a_trade_answer_without_a_tape_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        sse_yunhq.parse_trades(b'{"code":"600519"}', _date(), SHANGHAI)
    with pytest.raises(LivePayloadError):
        sse_yunhq.parse_trades(_dump({"trd1": [[145607, 1403.5]]}), _date(), SHANGHAI)


def test_records_are_book_quote_and_one_trade_per_print() -> None:
    snapshot = sse_yunhq.parse_snap(_snap_bytes(), MOUTAI, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    prints = sse_yunhq.parse_trades(_trd_bytes(), _date(), SHANGHAI)
    records = snapshot_records(dataclasses.replace(snapshot, prints=prints))
    book, quote, *trades = records
    assert isinstance(book, BookSnapshot) and isinstance(quote, Quote)
    assert book.symbol == "600519@XSHG" and book.source == "sse_yunhq"
    assert book.depth == 5 and len(book.bids) == 5 and len(book.asks) == 5
    assert (quote.bid_px, quote.bid_sz) == (1410.27, 1655.0)
    assert (quote.ask_px, quote.ask_sz) == (1410.43, 200.0)
    assert len(trades) == 19 and all(isinstance(t, Trade) for t in trades)
    first = trades[0]
    assert isinstance(first, Trade)
    assert (first.price, first.amount) == (1403.5, 3500.0)
    assert first.id == "1794466567000000000-0"


class _Fake:
    """A fetcher that answers each SSE URL with a fixed body and records what it was asked."""

    def __init__(self, snap: bytes, trades: bytes, *, trade_status: int = 200) -> None:
        self.snap = snap
        self.trades = trades
        self.trade_status = trade_status
        self.snap_status = 200
        self.requests: list[HttpRequest] = []

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if "/snap/" in request.url:
            body, status = self.snap, self.snap_status
        else:
            body, status = self.trades, self.trade_status
        return HttpResponse(
            status=status,
            headers={"content-type": "application/json"},
            body=body,
            url=request.url,
        )


async def test_the_source_reads_snap_then_tape_and_returns_both() -> None:
    fake = _Fake(_snap_bytes(), _trd_bytes())
    source = sse_yunhq.SseYunhqLive(fetch=fake)
    (snapshot,) = await source.snapshots([MOUTAI], captured_ns=CAPTURED_NS)
    assert [r.url for r in fake.requests] == [
        "https://yunhq.sse.com.cn:32042/v1/sh1/snap/600519",
        "https://yunhq.sse.com.cn:32042/v1/sh1/trd1/600519",
    ]
    assert fake.requests[0].params == (
        ("select", "name,prev_close,open,high,low,last,volume,amount,bid,ask,tradephase"),
    )
    assert fake.requests[1].params == (
        ("begin", "-20"),
        ("end", "-1"),
        ("select", "time,price,volume"),
    )
    assert snapshot.listing.symbol == "600519@XSHG"
    assert snapshot.tier is LiveTier.L2
    assert len(snapshot.prints) == 19
    assert snapshot.prints[-1].price == 1410.27


async def test_a_failed_tape_leaves_the_snapshot_without_prints() -> None:
    fake = _Fake(_snap_bytes(), b'{"rtcode":"9999"}')
    source = sse_yunhq.SseYunhqLive(fetch=fake)
    (snapshot,) = await source.snapshots([MOUTAI], captured_ns=CAPTURED_NS)
    assert snapshot.last == 1410.27
    assert snapshot.prints == ()


async def test_a_wall_on_the_tape_propagates() -> None:
    fake = _Fake(_snap_bytes(), _trd_bytes(), trade_status=403)
    source = sse_yunhq.SseYunhqLive(fetch=fake)
    with pytest.raises(SourceUnservableError):
        await source.snapshots([MOUTAI], captured_ns=CAPTURED_NS)


async def test_listings_the_source_does_not_serve_make_no_request() -> None:
    fake = _Fake(_snap_bytes(), _trd_bytes())
    source = sse_yunhq.SseYunhqLive(fetch=fake)
    others = [
        LiveListing(mic="XSHE", local_symbol="000001"),
        LiveListing(mic="XSHG", local_symbol="60051A"),
    ]
    assert await source.snapshots(others, captured_ns=CAPTURED_NS) == []
    assert fake.requests == []


async def test_a_listing_the_answer_does_not_name_is_absent() -> None:
    fake = _Fake(_snap_bytes(), _trd_bytes())
    source = sse_yunhq.SseYunhqLive(fetch=fake)
    other = LiveListing(mic="XSHG", local_symbol="600000")
    assert await source.snapshots([other], captured_ns=CAPTURED_NS) == []
    assert len(fake.requests) == 1  # the snap was asked for; no tape without a snapshot


async def test_a_failed_tape_status_leaves_the_snapshot_without_prints() -> None:
    fake = _Fake(_snap_bytes(), _trd_bytes(), trade_status=500)
    source = sse_yunhq.SseYunhqLive(fetch=fake)
    (snapshot,) = await source.snapshots([MOUTAI], captured_ns=CAPTURED_NS)
    assert snapshot.last == 1410.27
    assert snapshot.prints == ()


async def test_a_wall_on_the_snap_propagates() -> None:
    fake = _Fake(_snap_bytes(), _trd_bytes())
    fake.snap_status = 403
    source = sse_yunhq.SseYunhqLive(fetch=fake)
    with pytest.raises(SourceUnservableError):
        await source.snapshots([MOUTAI], captured_ns=CAPTURED_NS)


