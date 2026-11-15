"""``vps``: the VPS broker feed for HOSE and HNX, read from a captured answer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from crocodile.core.schema.enums import Channel
from crocodile.core.schema.provenance import Provenance
from crocodile.core.schema.records import BookSnapshot, Quote, Trade
from crocodile.equity.universal.base import SourceUnservableError
from crocodile.equity.universal.http import HttpRequest, HttpResponse
from crocodile.equity.universal.live.base import LivePayloadError
from crocodile.equity.universal.live.model import (
    LiveListing,
    LiveSnapshot,
    LiveTier,
    snapshot_records,
)
from crocodile.equity.universal.live.sources import vps

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live" / "vps"
CAPTURED_NS = 1_794_471_000_000_000_000
VNM = LiveListing(mic="XSTC", local_symbol="VNM")
FPT = LiveListing(mic="XSTC", local_symbol="FPT")
SHS = LiveListing(mic="HSTC", local_symbol="SHS")
ALL = [VNM, FPT, SHS]


def _body() -> bytes:
    return (FIXTURES / "quotes.json").read_bytes()


def _rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads(_body())
    return rows


def _encode(rows: list[Any]) -> bytes:
    return json.dumps(rows).encode("utf-8")


def _by_symbol(body: bytes, listings: list[LiveListing]) -> dict[str, LiveSnapshot]:
    parsed = vps.parse_quotes(body, listings, captured_ns=CAPTURED_NS)
    return {s.listing.symbol: s for s in parsed}


def test_parses_three_listings_with_ladders_in_shares_and_vnd() -> None:
    snapshots = _by_symbol(_body(), ALL)
    assert set(snapshots) == {"VNM@XSTC", "FPT@XSTC", "SHS@HSTC"}

    vnm = snapshots["VNM@XSTC"]
    assert vnm.tier is LiveTier.L2
    assert [(lv.price, lv.size) for lv in vnm.bids] == [
        (54200.0, 68500.0),
        (54100.0, 99600.0),
        (54000.0, 207100.0),
    ]
    assert [(lv.price, lv.size) for lv in vnm.asks] == [
        (54400.0, 1100.0),
        (54500.0, 12700.0),
        (54600.0, 34200.0),
    ]
    assert vnm.last == 54200.0
    assert vnm.last_size == 300.0
    assert vnm.prev_close == 54500.0
    assert (vnm.open, vnm.high, vnm.low) == (54800.0, 54800.0, 54100.0)
    assert vnm.volume == 1_337_300.0
    assert vnm.currency == "VND"
    assert vnm.price_scale == 0
    assert vnm.source_ts_ns is None
    assert vnm.delay_s is None
    assert vnm.name is None

    fpt = snapshots["FPT@XSTC"]
    assert [(lv.price, lv.size) for lv in fpt.bids] == [
        (49550.0, 1200.0),
        (49500.0, 530100.0),
        (49450.0, 520500.0),
    ]
    assert [(lv.price, lv.size) for lv in fpt.asks] == [
        (49600.0, 17200.0),
        (49700.0, 5800.0),
        (49750.0, 5800.0),
    ]
    assert fpt.last == 49550.0
    assert fpt.last_size == 1000.0
    assert fpt.prev_close == 50900.0
    assert (fpt.open, fpt.high, fpt.low) == (51000.0, 51000.0, 49450.0)
    assert fpt.volume == 17_335_200.0

    shs = snapshots["SHS@HSTC"]
    assert shs.tier is LiveTier.L2
    assert [(lv.price, lv.size) for lv in shs.bids] == [
        (13400.0, 302500.0),
        (13300.0, 778500.0),
        (13200.0, 1_113_600.0),
    ]
    assert [(lv.price, lv.size) for lv in shs.asks] == [
        (13500.0, 653000.0),
        (13600.0, 1_171_200.0),
        (13700.0, 535900.0),
    ]
    assert shs.last == 13400.0
    assert shs.last_size == 100.0
    assert shs.prev_close == 13600.0
    assert (shs.open, shs.high, shs.low) == (13500.0, 13700.0, 13300.0)
    assert shs.volume == 14_607_400.0


def test_the_published_close_confirms_the_thousands_of_vnd_unit() -> None:
    # The row states closePrice in VND and r in thousands of VND: the same figure.
    for row in _rows():
        assert float(row["closePrice"]) == pytest.approx(float(row["r"]) * 1000)
    # fBValue / fBVol is about ten times the price in thousands, which pins the 10-share unit.
    shs = next(row for row in _rows() if row["sym"] == "SHS")
    value_per_unit = float(shs["fBValue"]) / float(shs["fBVol"])
    assert value_per_unit == pytest.approx(10 * float(shs["lastPrice"]))


def test_a_listing_that_was_not_asked_for_is_absent() -> None:
    snapshots = vps.parse_quotes(_body(), [FPT], captured_ns=CAPTURED_NS)
    assert [s.listing.symbol for s in snapshots] == ["FPT@XSTC"]


def test_a_row_under_another_market_is_absent() -> None:
    rows = _rows()
    rows[0]["marketId"] = "HOSE"  # not a market this source maps: the row is skipped
    rows[2]["marketId"] = "STO"  # SHS now reports under Ho Chi Minh: not the listing asked for
    snapshots = vps.parse_quotes(_encode(rows), ALL, captured_ns=CAPTURED_NS)
    assert [s.listing.symbol for s in snapshots] == ["FPT@XSTC"]


def test_empty_slots_and_session_codes_are_dropped() -> None:
    rows = _rows()
    rows[0]["g1"] = "0|0|e"  # empty slot
    rows[0]["g4"] = "ATO|5000|i"  # auction order: no numeric price
    (vnm,) = vps.parse_quotes(_encode(rows[:1]), [VNM], captured_ns=CAPTURED_NS)
    assert [(lv.price, lv.size) for lv in vnm.bids] == [(54100.0, 99600.0), (54000.0, 207100.0)]
    assert [(lv.price, lv.size) for lv in vnm.asks] == [(54500.0, 12700.0), (54600.0, 34200.0)]


def test_a_non_monotone_side_is_refused_whole() -> None:
    rows = _rows()
    rows[0]["g2"] = "54.3|9960|d"  # bids would run 54.2, 54.3, 54.0: not best first
    (vnm,) = vps.parse_quotes(_encode(rows[:1]), [VNM], captured_ns=CAPTURED_NS)
    assert vnm.bids == ()
    assert [lv.price for lv in vnm.asks] == [54400.0, 54500.0, 54600.0]


def test_zero_volume_or_session_code_last_price_states_no_last_print() -> None:
    rows = _rows()
    rows[0]["lastVolume"] = 0
    rows[1]["lastPrice"] = "ATO"
    snapshots = _by_symbol(_encode(rows), [VNM, FPT])
    vnm, fpt = snapshots["VNM@XSTC"], snapshots["FPT@XSTC"]
    assert vnm.last == 54200.0 and vnm.last_size is None
    assert fpt.last is None and fpt.last_size is None
    assert not any(isinstance(r, Trade) for r in snapshot_records(fpt))


def test_records_are_native_book_quote_and_print() -> None:
    (snapshot,) = vps.parse_quotes(_body(), [VNM], captured_ns=CAPTURED_NS)
    records = snapshot_records(snapshot)
    assert [type(record) for record in records] == [BookSnapshot, Quote, Trade]
    book, quote, trade = records
    assert isinstance(book, BookSnapshot) and isinstance(quote, Quote)
    assert isinstance(trade, Trade)
    assert book.symbol == "VNM@XSTC" and book.source == "vps"
    assert book.depth == 3 and len(book.bids) == 3 and len(book.asks) == 3
    assert book.prov is Provenance.NATIVE and book.prov_basis == "polled_book_snapshot"
    assert book.source_ts is None
    assert (quote.bid_px, quote.bid_sz) == (54200.0, 68500.0)
    assert (quote.ask_px, quote.ask_sz) == (54400.0, 1100.0)
    assert quote.currency == "VND" and quote.price_scale == 0
    assert trade.price == 54200.0 and trade.amount == 300.0
    assert trade.prov_basis == "polled_last_print"
    assert Channel.BOOK_SNAPSHOT.value == "book_snapshot"


def test_an_answer_that_is_not_an_array_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        vps.parse_quotes(b'{"msg":"error"}', [VNM], captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        vps.parse_quotes(b"<html>", [VNM], captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        vps.parse_quotes(b"", [VNM], captured_ns=CAPTURED_NS)


async def test_the_source_makes_one_path_request_and_returns_snapshots() -> None:
    requested: list[HttpRequest] = []

    async def fetch(request: HttpRequest) -> HttpResponse:
        requested.append(request)
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=_body(),
            url=request.url,
        )

    source = vps.VpsLive(fetch=fetch)
    unserved = LiveListing(mic="XNYS", local_symbol="AAPL")
    snapshots = await source.snapshots([VNM, unserved, FPT, SHS], captured_ns=CAPTURED_NS)

    assert [r.url for r in requested] == [vps.QUOTE_URL + "VNM,FPT,SHS"]
    assert requested[0].params == ()
    assert {s.listing.symbol for s in snapshots} == {"VNM@XSTC", "FPT@XSTC", "SHS@HSTC"}


async def test_a_source_with_nothing_to_serve_makes_no_request() -> None:
    requested: list[HttpRequest] = []

    async def fetch(request: HttpRequest) -> HttpResponse:
        requested.append(request)
        raise AssertionError("no request expected")

    source = vps.VpsLive(fetch=fetch)
    assert await source.snapshots([LiveListing(mic="XNYS", local_symbol="AAPL")],
                                  captured_ns=CAPTURED_NS) == []
    assert requested == []


def test_missing_null_and_placeholder_fields_are_none_never_zero() -> None:
    rows = _rows()
    rows[0].update(
        lastPrice=None,
        lastVolume="-",
        r="-",
        openPrice="",
        highPrice=None,
        lowPrice="N/A",
        lot="--",
    )
    rows[0]["g1"] = None  # a bid slot that is not a string
    rows[0]["g2"] = "-|-|e"  # a bid slot with a placeholder price
    (vnm,) = vps.parse_quotes(_encode(rows[:1]), [VNM], captured_ns=CAPTURED_NS)
    assert vnm.last is None and vnm.last_size is None
    assert vnm.prev_close is None
    assert (vnm.open, vnm.high, vnm.low) == (None, None, None)
    assert vnm.volume is None
    assert [(lv.price, lv.size) for lv in vnm.bids] == [(54000.0, 207100.0)]
    assert not any(isinstance(r, Trade) for r in snapshot_records(vnm))


def test_non_finite_negative_and_oversized_values_are_none() -> None:
    rows = _rows()
    rows[0]["lastPrice"] = 1e308  # times 1,000 overflows to infinity: no price
    rows[0]["lot"] = -5  # a negative cumulative volume is no volume
    rows[1]["lot"] = 1e308  # times 10 overflows to infinity: no volume
    rows[1]["lastVolume"] = 10**400  # an integer no float can hold: no size
    rows[2]["r"] = 10**400  # the previous close is no number
    snapshots = _by_symbol(_encode(rows), ALL)
    vnm, fpt, shs = snapshots["VNM@XSTC"], snapshots["FPT@XSTC"], snapshots["SHS@HSTC"]
    assert vnm.last is None and vnm.last_size is None and vnm.volume is None
    assert vnm.bids and vnm.asks  # the ladder is still published and still read
    assert not any(isinstance(r, Trade) for r in snapshot_records(vnm))
    assert fpt.last == 49550.0 and fpt.last_size is None and fpt.volume is None
    assert shs.prev_close is None and shs.last == 13400.0


def test_rows_of_the_wrong_shape_are_skipped_and_the_good_row_survives() -> None:
    rows: list[Any] = [
        1,
        "VNM",
        None,
        ["STO", "VNM"],
        {"marketId": "STO"},
        {"marketId": None, "sym": "VNM"},
        {"marketId": "STO", "sym": 7},
        {"marketId": "STO", "sym": "VNM"},  # right listing, nothing published: no snapshot
        *_rows()[:1],
    ]
    (vnm,) = vps.parse_quotes(_encode(rows), [VNM], captured_ns=CAPTURED_NS)
    assert vnm.symbol_raw == "VNM" and vnm.last == 54200.0


async def test_an_html_answer_is_a_wall_and_an_empty_answer_a_payload_error() -> None:
    async def html(request: HttpRequest) -> HttpResponse:
        return HttpResponse(
            status=200,
            headers={"content-type": "text/html"},
            body=b"<html><body>Please verify you are human</body></html>",
            url=request.url,
        )

    async def empty(request: HttpRequest) -> HttpResponse:
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=b"",
            url=request.url,
        )

    async def not_an_array(request: HttpRequest) -> HttpResponse:
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=b'{"msg":"error"}',
            url=request.url,
        )

    with pytest.raises(SourceUnservableError):
        await vps.VpsLive(fetch=html).snapshots([VNM], captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        await vps.VpsLive(fetch=empty).snapshots([VNM], captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        await vps.VpsLive(fetch=not_an_array).snapshots([VNM], captured_ns=CAPTURED_NS)


async def test_a_symbol_is_one_quoted_path_segment() -> None:
    """A local symbol can never add a path or a query to the request."""
    seen: list[str] = []

    async def fetch(request: HttpRequest) -> HttpResponse:
        seen.append(request.url)
        return HttpResponse(
            status=200, headers={"content-type": "application/json"}, body=b"[]", url=request.url
        )

    source = vps.VpsLive(fetch)
    odd = LiveListing(mic="XSTC", local_symbol="A/B?x=1")
    await source.snapshots([odd], captured_ns=1)
    assert seen == [vps.QUOTE_URL + "A%2FB%3Fx%3D1"]
