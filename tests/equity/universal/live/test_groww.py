"""``groww``: the NSE book and last price, read from captured answers."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from crocodile.core.schema.provenance import Provenance
from crocodile.core.schema.records import BookSnapshot, Quote, Trade
from crocodile.equity.universal.base import SourceUnservableError
from crocodile.equity.universal.http import Fetcher, HttpRequest, HttpResponse
from crocodile.equity.universal.live.base import LivePayloadError
from crocodile.equity.universal.live.model import LiveListing, LiveTier, snapshot_records
from crocodile.equity.universal.live.sources import groww

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live" / "groww"
CAPTURED_NS = 1_794_471_000_000_000_000
RELIANCE = LiveListing(mic="XNSE", local_symbol="RELIANCE")
_BASE = "https://groww.in/v1/api/stocks_data/v1/"
_SEGMENT = "/exchange/NSE/segment/CASH/"
RELIANCE_PRICE_URL = f"{_BASE}tr_live_prices{_SEGMENT}RELIANCE/latest"
RELIANCE_BOOK_URL = f"{_BASE}tr_live_book{_SEGMENT}RELIANCE/latest"


def _price() -> bytes:
    return (FIXTURES / "price_nse_RELIANCE.json").read_bytes()


def _book() -> bytes:
    return (FIXTURES / "book_nse_RELIANCE.json").read_bytes()


def _fetcher(price: bytes, book: bytes, requested: list[str]) -> Fetcher:
    async def fetch(request: HttpRequest) -> HttpResponse:
        requested.append(request.url)
        body = book if "/tr_live_book/" in request.url else price
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=body,
            url=request.url,
        )

    return fetch


def test_the_class_declares_the_publisher_contract() -> None:
    assert groww.SOURCE == "groww" and groww.GrowwLive.name == "groww"
    assert groww.GrowwLive.supported_mics == frozenset({"XNSE"})
    assert groww.GrowwLive.official is False and groww.GrowwLive.attribution is None
    assert groww.GrowwLive.max_tier is LiveTier.L2 and groww.GrowwLive.depth_levels == 5
    assert groww.GrowwLive.delay_s is None and groww.GrowwLive.has_tape is True
    assert groww.GrowwLive.batch_size == 1 and groww.GrowwLive.min_interval_s == 5.0


def test_price_maps_every_field_in_rupees_and_shares() -> None:
    snapshot = groww.parse_price(_price(), RELIANCE, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.listing.symbol == "RELIANCE@XNSE" and snapshot.symbol_raw == "RELIANCE"
    assert snapshot.last == 1335.8
    assert snapshot.last_size == 20.0
    assert snapshot.prev_close == 1335.4
    assert (snapshot.open, snapshot.high, snapshot.low) == (1335.4, 1342.5, 1331.5)
    assert snapshot.volume == 8_270_544.0
    assert snapshot.turnover is None and snapshot.name is None
    assert snapshot.currency == "INR" and snapshot.price_scale == 0
    assert snapshot.delay_s is None
    assert snapshot.bids == () and snapshot.asks == ()
    assert snapshot.tier is LiveTier.LAST


def test_price_stamp_is_seconds_because_the_fixture_is_seconds() -> None:
    snapshot = groww.parse_price(_price(), RELIANCE, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.source_ts_ns == 1_794_471_338 * 1_000_000_000
    assert datetime.fromtimestamp(snapshot.source_ts_ns // 1_000_000_000, UTC) == datetime(
        2026, 11, 12, 8, 15, 38, tzinfo=UTC
    )


def test_a_stamp_in_milliseconds_is_read_as_milliseconds() -> None:
    body = _price().replace(b'"tsInMillis":1794471338,', b'"tsInMillis":1794471338000,')
    snapshot = groww.parse_price(body, RELIANCE, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.source_ts_ns == 1_794_471_338 * 1_000_000_000


def test_book_is_five_levels_per_side_best_first_in_shares() -> None:
    bids, asks = groww.parse_book(_book())
    assert [(level.price, level.size) for level in bids] == [
        (1335.7, 210.0),
        (1335.5, 471.0),
        (1335.3, 143.0),
        (1335.2, 796.0),
        (1335.1, 2312.0),
    ]
    assert [(level.price, level.size) for level in asks] == [
        (1335.9, 643.0),
        (1336.0, 1273.0),
        (1336.1, 2238.0),
        (1336.2, 5951.0),
        (1336.3, 3808.0),
    ]


def test_a_listing_book_for_another_symbol_is_empty() -> None:
    other = LiveListing(mic="XNSE", local_symbol="TCS")
    assert groww.parse_book(_book(), listing=other) == ((), ())
    assert groww.parse_price(_price(), other, captured_ns=CAPTURED_NS) is None


def test_a_crossing_bid_ladder_is_refused_whole() -> None:
    body = _book().replace(
        b'"2":{"orderCount":3,"price":1335.5', b'"2":{"orderCount":3,"price":1335.8'
    )
    bids, asks = groww.parse_book(body)
    assert bids == ()
    assert len(asks) == 5


def test_an_empty_slot_is_dropped_and_a_zero_size_is_not_a_size() -> None:
    body = _book().replace(b'"price":1336.3,"qty":3808', b'"price":0,"qty":3808').replace(
        b'"price":1335.9,"qty":643', b'"price":1335.9,"qty":0'
    )
    bids, asks = groww.parse_book(body)
    assert len(bids) == 5
    assert [level.price for level in asks] == [1335.9, 1336.0, 1336.1, 1336.2]
    assert asks[0].size is None and asks[1].size == 1273.0


def test_a_zero_last_trade_quantity_is_no_last_size_and_no_trade() -> None:
    body = _price().replace(b'"lastTradeQty":20,', b'"lastTradeQty":0,')
    snapshot = groww.parse_price(body, RELIANCE, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.last == 1335.8 and snapshot.last_size is None
    assert not any(isinstance(record, Trade) for record in snapshot_records(snapshot))


def test_no_last_price_keeps_the_session_figures_and_builds_no_trade() -> None:
    body = _price().replace(b'"ltp":1335.8,', b'"ltp":null,')
    snapshot = groww.parse_price(body, RELIANCE, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.last is None and snapshot.last_size is None
    assert snapshot.prev_close == 1335.4 and snapshot.volume == 8_270_544.0
    assert snapshot_records(snapshot) == []


def test_a_zero_last_price_is_no_price() -> None:
    body = _price().replace(b'"ltp":1335.8,', b'"ltp":0,')
    snapshot = groww.parse_price(body, RELIANCE, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.last is None and snapshot.last_size is None
    assert snapshot.open == 1335.4


def test_a_placeholder_string_is_no_figure() -> None:
    body = _price().replace(b'"high":1342.5,', b'"high":"-",').replace(
        b'"volume":8270544,', b'"volume":"",'
    )
    snapshot = groww.parse_price(body, RELIANCE, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.high is None and snapshot.volume is None
    assert snapshot.low == 1331.5


def test_an_answer_that_is_not_the_document_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        groww.parse_price(b'{"error":"not found"}', RELIANCE, captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        groww.parse_price(b"<html>", RELIANCE, captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        groww.parse_price(b"", RELIANCE, captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        groww.parse_price(b"[]", RELIANCE, captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        groww.parse_book(b'{"symbol":"RELIANCE"}')
    with pytest.raises(LivePayloadError):
        groww.parse_book(b"[]")
    with pytest.raises(LivePayloadError):
        groww.parse_book(b"")


def test_records_are_native_book_quote_and_print() -> None:
    snapshot = groww.parse_price(_price(), RELIANCE, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    bids, asks = groww.parse_book(_book())
    snapshot = replace(snapshot, bids=bids, asks=asks)
    assert snapshot.tier is LiveTier.L2
    records = snapshot_records(snapshot)
    assert [type(record) for record in records] == [BookSnapshot, Quote, Trade]
    book, quote, trade = records
    assert isinstance(book, BookSnapshot) and isinstance(quote, Quote)
    assert isinstance(trade, Trade)
    assert book.symbol == "RELIANCE@XNSE" and book.source == "groww"
    assert book.depth == 5 and len(book.bids) == 5 and len(book.asks) == 5
    assert book.prov is Provenance.NATIVE and book.prov_basis == "polled_book_snapshot"
    assert (quote.bid_px, quote.bid_sz, quote.ask_px, quote.ask_sz) == (
        1335.7,
        210.0,
        1335.9,
        643.0,
    )
    assert trade.price == 1335.8 and trade.amount == 20.0
    assert trade.currency == "INR" and trade.prov_basis == "polled_last_print"


async def test_the_source_asks_price_then_book_and_returns_both() -> None:
    requested: list[str] = []
    source = groww.GrowwLive(fetch=_fetcher(_price(), _book(), requested))
    snapshots = await source.snapshots([RELIANCE], captured_ns=CAPTURED_NS)
    assert requested == [RELIANCE_PRICE_URL, RELIANCE_BOOK_URL]
    (snapshot,) = snapshots
    assert snapshot.tier is LiveTier.L2
    assert snapshot.last == 1335.8
    assert snapshot.bids[0].price == 1335.7 and snapshot.asks[0].size == 643.0


async def test_a_book_that_is_not_the_document_leaves_the_price_only() -> None:
    requested: list[str] = []
    source = groww.GrowwLive(fetch=_fetcher(_price(), b'{"error":"not found"}', requested))
    (snapshot,) = await source.snapshots([RELIANCE], captured_ns=CAPTURED_NS)
    assert requested == [RELIANCE_PRICE_URL, RELIANCE_BOOK_URL]
    assert snapshot.bids == () and snapshot.asks == ()
    assert snapshot.last == 1335.8 and snapshot.last_size == 20.0
    assert [type(record) for record in snapshot_records(snapshot)] == [Trade]


async def test_a_symbol_is_percent_encoded_and_an_unnamed_symbol_is_absent() -> None:
    requested: list[str] = []
    source = groww.GrowwLive(fetch=_fetcher(_price(), _book(), requested))
    snapshots = await source.snapshots(
        [LiveListing(mic="XNSE", local_symbol="M&M")], captured_ns=CAPTURED_NS
    )
    assert snapshots == []
    assert requested == [f"{_BASE}tr_live_prices{_SEGMENT}M%26M/latest"]


async def test_a_price_without_ltp_keeps_its_book_and_is_not_dropped() -> None:
    requested: list[str] = []
    price = _price().replace(b'"ltp":1335.8,', b'"ltp":null,')
    source = groww.GrowwLive(fetch=_fetcher(price, _book(), requested))
    (snapshot,) = await source.snapshots([RELIANCE], captured_ns=CAPTURED_NS)
    assert requested == [RELIANCE_PRICE_URL, RELIANCE_BOOK_URL]
    assert snapshot.last is None and snapshot.tier is LiveTier.L2
    assert [type(record) for record in snapshot_records(snapshot)] == [BookSnapshot, Quote]


async def test_a_price_and_book_that_are_both_empty_is_absent() -> None:
    requested: list[str] = []
    price = b'{"symbol":"RELIANCE","ltp":null,"volume":null}'
    book = b'{"buyBook":{},"sellBook":{},"symbol":"RELIANCE"}'
    source = groww.GrowwLive(fetch=_fetcher(price, book, requested))
    assert await source.snapshots([RELIANCE], captured_ns=CAPTURED_NS) == []


async def test_an_empty_price_body_is_a_payload_error() -> None:
    source = groww.GrowwLive(fetch=_fetcher(b"", _book(), []))
    with pytest.raises(LivePayloadError):
        await source.snapshots([RELIANCE], captured_ns=CAPTURED_NS)


async def test_a_wall_on_the_book_is_not_caught_and_is_raised() -> None:
    async def fetch(request: HttpRequest) -> HttpResponse:
        if "/tr_live_book/" in request.url:
            return HttpResponse(
                status=200,
                headers={"content-type": "text/html"},
                body=b"<!doctype html><html>challenge</html>",
                url=request.url,
            )
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=_price(),
            url=request.url,
        )

    source = groww.GrowwLive(fetch=fetch)
    with pytest.raises(SourceUnservableError):
        await source.snapshots([RELIANCE], captured_ns=CAPTURED_NS)


async def test_an_nse_listing_only_is_served() -> None:
    requested: list[str] = []
    source = groww.GrowwLive(fetch=_fetcher(_price(), _book(), requested))
    snapshots = await source.snapshots(
        [LiveListing(mic="XBOM", local_symbol="500325")], captured_ns=CAPTURED_NS
    )
    assert snapshots == [] and requested == []
