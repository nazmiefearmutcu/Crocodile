"""``boerse_frankfurt``: the Xetra and Frankfurt-floor book and quote, from captured answers."""

from __future__ import annotations

from pathlib import Path

import pytest

from crocodile.core.schema.provenance import Provenance
from crocodile.core.schema.records import BookSnapshot, Quote, Trade
from crocodile.equity.universal.http import Fetcher, HttpRequest, HttpResponse
from crocodile.equity.universal.live.base import LivePayloadError
from crocodile.equity.universal.live.model import (
    LiveListing,
    LiveSnapshot,
    LiveTier,
    snapshot_records,
)
from crocodile.equity.universal.live.sources import boerse_frankfurt
from crocodile.equity.universal.live.sources.boerse_frankfurt import (
    BOOK_URL,
    PRICE_URL,
    QUOTE_URL,
    BoerseFrankfurtLive,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live" / "boerse_frankfurt"
CAPTURED_NS = 1_794_470_500_000_000_000
ISIN = "DE0007164600"
XETR = LiveListing(mic="XETR", local_symbol="SAP", isin=ISIN)
XFRA = LiveListing(mic="XFRA", local_symbol="SAP", isin=ISIN)
QUOTE_XETR_NS = 1_794_470_435_000_000_000
QUOTE_XFRA_NS = 1_794_471_329_000_000_000


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _book() -> bytes:
    return _read("bid_ask_xetr.json")


def _quote() -> bytes:
    return _read("quote_box_xetr.json")


def _price() -> bytes:
    return _read("price_info_xetr.json")


def _xfra_quote() -> bytes:
    return _read("quote_box_xfra.json")


def _compose_xetr() -> LiveSnapshot | None:
    book = boerse_frankfurt.parse_book(_book())
    quote = boerse_frankfurt.parse_quote_box(_quote())
    price = boerse_frankfurt.parse_price_information(_price())
    return boerse_frankfurt.compose(XETR, CAPTURED_NS, book, quote, price)


def test_serves_only_xetr_and_xfra_with_an_isin() -> None:
    source = BoerseFrankfurtLive(fetch=_never)
    assert source.serves(XETR) and source.serves(XFRA)
    assert not source.serves(LiveListing(mic="XTAI", local_symbol="2330", isin=ISIN))
    assert not source.serves(LiveListing(mic="XETR", local_symbol="SAP"))


def test_book_ladders_are_best_first_in_shares() -> None:
    bids, asks = boerse_frankfurt.parse_book(_book())
    assert len(bids) == 10 and len(asks) == 10
    assert (bids[0].price, bids[0].size) == (211.95, 326.0)
    assert (bids[-1].price, bids[-1].size) == (211.5, 765.0)
    assert (asks[0].price, asks[0].size) == (212.1, 144.0)
    assert (asks[-1].price, asks[-1].size) == (212.55, 167.0)


def test_book_without_a_data_list_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        boerse_frankfurt.parse_book(b'{"rtcode":"9999"}')
    with pytest.raises(LivePayloadError):
        boerse_frankfurt.parse_book(b"<html>")


def test_an_empty_book_has_no_levels() -> None:
    assert boerse_frankfurt.parse_book(b'{"data":[]}') == ((), ())


def test_a_side_that_is_not_monotone_is_refused_whole() -> None:
    body = (
        b'{"data":[{"bidPrice":1.0,"bidUnits":5,"askPrice":2.0,"askUnits":5},'
        b'{"bidPrice":2.0,"bidUnits":5,"askPrice":3.0,"askUnits":5}]}'
    )
    bids, asks = boerse_frankfurt.parse_book(body)
    assert bids == ()
    assert [level.price for level in asks] == [2.0, 3.0]


def test_placeholder_rows_are_empty_slots() -> None:
    body = (
        b'{"data":[{"bidPrice":"-","bidUnits":"","askPrice":null,"askUnits":5},'
        b'{"bidPrice":"211.95","bidUnits":"-","askPrice":"212.10","askUnits":"144"}]}'
    )
    bids, asks = boerse_frankfurt.parse_book(body)
    assert [(level.price, level.size) for level in bids] == [(211.95, None)]
    assert [(level.price, level.size) for level in asks] == [(212.1, 144.0)]


def test_quote_box_fields() -> None:
    quote = boerse_frankfurt.parse_quote_box(_quote())
    assert quote.isin == ISIN
    assert (quote.bid_limit, quote.bid_size) == (211.95, 326.0)
    assert (quote.ask_limit, quote.ask_size) == (212.1, 144.0)
    assert quote.last == 212.0
    assert quote.open == 212.3
    assert quote.ts_ns == QUOTE_XETR_NS


def test_a_quote_box_stamp_without_an_offset_is_none() -> None:
    body = _quote().replace(
        b'"timestamp":"2026-11-12T08:00:35Z"', b'"timestamp":"2026-11-12T08:00:35"'
    )
    assert boerse_frankfurt.parse_quote_box(body).ts_ns is None


def test_a_quote_box_without_an_isin_is_a_payload_error() -> None:
    for body in (b"{}", b"[]", b"<html>"):
        with pytest.raises(LivePayloadError):
            boerse_frankfurt.parse_quote_box(body)


def test_price_information_fields() -> None:
    price = boerse_frankfurt.parse_price_information(_price())
    assert price.last == 212.0
    assert price.prev_close == 210.95
    assert (price.high, price.low) == (212.4, 211.8)
    assert price.volume == 53_634.0
    assert price.turnover == 11_383_590.25
    assert price.currency == "EUR"


def test_price_information_without_price_fields_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        boerse_frankfurt.parse_price_information(b'{"foo":1}')
    with pytest.raises(LivePayloadError):
        boerse_frankfurt.parse_price_information(b"[]")


def test_composes_the_xetr_snapshot() -> None:
    snapshot = _compose_xetr()
    assert snapshot is not None
    assert snapshot.tier is LiveTier.L2
    assert snapshot.symbol_raw == ISIN
    assert snapshot.last == 212.0
    assert snapshot.last_size is None
    assert snapshot.open == 212.3
    assert snapshot.prev_close == 210.95
    assert (snapshot.high, snapshot.low) == (212.4, 211.8)
    assert snapshot.volume == 53_634.0
    assert snapshot.turnover == 11_383_590.25
    assert snapshot.currency == "EUR"
    assert snapshot.price_scale == 0
    assert snapshot.delay_s is None
    assert snapshot.source_ts_ns == QUOTE_XETR_NS
    assert snapshot.captured_ns == CAPTURED_NS
    assert len(snapshot.bids) == 10 and len(snapshot.asks) == 10


def test_xetr_records_are_a_native_book_and_quote_and_no_trade() -> None:
    snapshot = _compose_xetr()
    assert snapshot is not None
    records = snapshot_records(snapshot)
    assert [type(record) for record in records] == [BookSnapshot, Quote]
    book, quote = records
    assert isinstance(book, BookSnapshot) and isinstance(quote, Quote)
    assert book.source == "boerse_frankfurt" and book.symbol == "SAP@XETR"
    assert book.depth == 10 and len(book.bids) == 10 and len(book.asks) == 10
    assert book.prov is Provenance.NATIVE and book.prov_basis == "polled_book_snapshot"
    assert (quote.bid_px, quote.bid_sz) == (211.95, 326.0)
    assert (quote.ask_px, quote.ask_sz) == (212.1, 144.0)


def test_the_frankfurt_floor_falls_back_to_the_quote_box_top() -> None:
    quote = boerse_frankfurt.parse_quote_box(_xfra_quote())
    snapshot = boerse_frankfurt.compose(XFRA, CAPTURED_NS, None, quote, None)
    assert snapshot is not None
    assert snapshot.tier is LiveTier.L1
    assert [(level.price, level.size) for level in snapshot.bids] == [(211.95, 200.0)]
    assert [(level.price, level.size) for level in snapshot.asks] == [(212.05, 200.0)]
    assert snapshot.last == 212.15
    assert snapshot.open == 211.65
    assert snapshot.prev_close is None and snapshot.volume is None
    assert snapshot.currency is None
    assert snapshot.source_ts_ns == QUOTE_XFRA_NS
    records = snapshot_records(snapshot)
    assert [type(record) for record in records] == [BookSnapshot, Quote]
    book, top = records
    assert isinstance(book, BookSnapshot) and isinstance(top, Quote)
    assert book.depth == 1
    assert (top.bid_px, top.bid_sz, top.ask_px, top.ask_sz) == (211.95, 200.0, 212.05, 200.0)


def test_an_empty_order_book_also_falls_back_to_the_quote_box() -> None:
    book = boerse_frankfurt.parse_book(b'{"data":[]}')
    quote = boerse_frankfurt.parse_quote_box(_quote())
    snapshot = boerse_frankfurt.compose(XETR, CAPTURED_NS, book, quote, None)
    assert snapshot is not None
    assert snapshot.tier is LiveTier.L1
    assert [level.price for level in snapshot.bids] == [211.95]
    assert [level.price for level in snapshot.asks] == [212.1]


def test_a_zero_limit_is_no_quote_on_that_side() -> None:
    body = _xfra_quote().replace(b'"bidLimit":211.95', b'"bidLimit":0')
    quote = boerse_frankfurt.parse_quote_box(body)
    snapshot = boerse_frankfurt.compose(XFRA, CAPTURED_NS, None, quote, None)
    assert snapshot is not None
    assert snapshot.bids == ()
    assert [level.price for level in snapshot.asks] == [212.05]
    assert snapshot.tier is LiveTier.L1


def test_a_missing_size_stays_none_and_makes_no_quote_record() -> None:
    body = _xfra_quote().replace(b'"bidSize":200', b'"bidSize":null')
    quote = boerse_frankfurt.parse_quote_box(body)
    snapshot = boerse_frankfurt.compose(XFRA, CAPTURED_NS, None, quote, None)
    assert snapshot is not None
    assert [(level.price, level.size) for level in snapshot.bids] == [(211.95, None)]
    assert not any(isinstance(record, Quote) for record in snapshot_records(snapshot))


def test_a_missing_last_price_is_none_and_makes_no_trade() -> None:
    quote = boerse_frankfurt.parse_quote_box(
        _quote().replace(b'"lastPrice":212,', b'"lastPrice":null,')
    )
    price = boerse_frankfurt.parse_price_information(
        _price().replace(b'"lastPrice":212,', b'"lastPrice":null,')
    )
    snapshot = boerse_frankfurt.compose(
        XETR, CAPTURED_NS, boerse_frankfurt.parse_book(_book()), quote, price
    )
    assert snapshot is not None
    assert snapshot.last is None
    assert snapshot.prev_close == 210.95
    assert not any(isinstance(record, Trade) for record in snapshot_records(snapshot))


def test_the_last_price_falls_back_to_the_price_information() -> None:
    quote = boerse_frankfurt.parse_quote_box(
        _quote().replace(b'"lastPrice":212,', b'"lastPrice":null,')
    )
    price = boerse_frankfurt.parse_price_information(
        _price().replace(b'"lastPrice":212,', b'"lastPrice":212.5,')
    )
    snapshot = boerse_frankfurt.compose(XETR, CAPTURED_NS, None, quote, price)
    assert snapshot is not None
    assert snapshot.last == 212.5


def test_a_quote_box_for_another_isin_is_absent() -> None:
    body = _quote().replace(ISIN.encode(), b"DE0005190003")
    book = boerse_frankfurt.parse_book(_book())
    quote = boerse_frankfurt.parse_quote_box(body)
    price = boerse_frankfurt.parse_price_information(_price())
    assert boerse_frankfurt.compose(XETR, CAPTURED_NS, book, quote, price) is None


async def _never(request: HttpRequest) -> HttpResponse:
    raise AssertionError(f"unexpected request to {request.url}")


def _routes(routes: dict[str, bytes | int], seen: list[HttpRequest]) -> Fetcher:
    async def fetch(request: HttpRequest) -> HttpResponse:
        seen.append(request)
        answer = routes.get(request.url, 404)
        if isinstance(answer, int):
            return HttpResponse(status=answer, headers={}, body=b"", url=request.url)
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=answer,
            url=request.url,
        )

    return fetch


async def test_the_source_requests_three_documents_and_composes() -> None:
    seen: list[HttpRequest] = []
    source = BoerseFrankfurtLive(
        fetch=_routes({BOOK_URL: _book(), QUOTE_URL: _quote(), PRICE_URL: _price()}, seen)
    )
    snapshots = await source.snapshots([XETR], captured_ns=CAPTURED_NS)
    assert [request.url for request in seen] == [BOOK_URL, QUOTE_URL, PRICE_URL]
    assert all(
        request.params == (("isin", ISIN), ("mic", "XETR")) and request.method == "GET"
        for request in seen
    )
    (snapshot,) = snapshots
    assert snapshot.listing == XETR
    assert snapshot.tier is LiveTier.L2
    assert (snapshot.last, snapshot.open, snapshot.prev_close) == (212.0, 212.3, 210.95)
    assert snapshot.bids[0].size == 326.0 and snapshot.asks[0].size == 144.0
    await source.close()


async def test_a_book_payload_failure_leaves_the_book_to_the_quote_box() -> None:
    seen: list[HttpRequest] = []
    source = BoerseFrankfurtLive(
        fetch=_routes({QUOTE_URL: _quote(), PRICE_URL: _price()}, seen)
    )
    (snapshot,) = await source.snapshots([XETR], captured_ns=CAPTURED_NS)
    assert snapshot.tier is LiveTier.L1
    assert [level.price for level in snapshot.bids] == [211.95]
    assert snapshot.last == 212.0


async def test_a_quote_box_payload_failure_propagates() -> None:
    seen: list[HttpRequest] = []
    source = BoerseFrankfurtLive(fetch=_routes({BOOK_URL: _book(), PRICE_URL: _price()}, seen))
    with pytest.raises(LivePayloadError):
        await source.snapshots([XETR], captured_ns=CAPTURED_NS)


async def test_a_listing_without_an_isin_is_never_requested() -> None:
    seen: list[HttpRequest] = []
    source = BoerseFrankfurtLive(fetch=_routes({}, seen))
    listing = LiveListing(mic="XETR", local_symbol="SAP")
    assert await source.snapshots([listing], captured_ns=1) == []
    assert seen == []


async def test_an_empty_quote_box_body_raises_a_payload_error() -> None:
    seen: list[HttpRequest] = []
    source = BoerseFrankfurtLive(fetch=_routes({QUOTE_URL: b""}, seen))
    with pytest.raises(LivePayloadError):
        await source.snapshots([XETR], captured_ns=CAPTURED_NS)


async def test_a_quote_box_for_another_isin_yields_no_snapshot() -> None:
    seen: list[HttpRequest] = []
    body = _quote().replace(ISIN.encode(), b"DE0005190003")
    source = BoerseFrankfurtLive(
        fetch=_routes({BOOK_URL: _book(), QUOTE_URL: body, PRICE_URL: _price()}, seen)
    )
    assert await source.snapshots([XETR], captured_ns=CAPTURED_NS) == []
    await source.close()
