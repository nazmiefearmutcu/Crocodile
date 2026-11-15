"""``szse``: the Shenzhen five-level book, read from a captured answer."""

from __future__ import annotations

from pathlib import Path

import pytest

from crocodile.core.schema.provenance import Provenance
from crocodile.core.schema.records import BookSnapshot, Quote, Trade
from crocodile.equity.universal.http import HttpRequest, HttpResponse
from crocodile.equity.universal.live.base import LivePayloadError
from crocodile.equity.universal.live.model import (
    LiveListing,
    LiveSnapshot,
    LiveTier,
    snapshot_records,
)
from crocodile.equity.universal.live.sources import szse

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live" / "szse"
CAPTURED_NS = 1_794_471_000_000_000_000
PING_AN = LiveListing(mic="XSHE", local_symbol="000001")
# 2026-11-12 15:30:00 Asia/Shanghai, which is 07:30:00 UTC.
MARKET_TIME_NS = 1_794_468_600_000_000_000
PING_AN_NAME = "\u5e73\u5b89\u94f6\u884c"


def _body() -> bytes:
    return (FIXTURES / "timedata_000001.json").read_bytes()


def _parse() -> LiveSnapshot | None:
    return szse.parse_quote(_body(), PING_AN, captured_ns=CAPTURED_NS)


def test_parses_the_scalar_fields() -> None:
    snapshot = _parse()
    assert snapshot is not None
    assert snapshot.symbol_raw == "000001"
    assert snapshot.listing.symbol == "000001@XSHE"
    assert snapshot.last == 11.79
    # 12,186 lots of 100 shares.
    assert snapshot.last_size == 1_218_600.0
    assert snapshot.prev_close == 11.71
    assert (snapshot.open, snapshot.high, snapshot.low) == (11.69, 11.83, 11.66)
    # 1,082,721 lots of 100 shares.
    assert snapshot.volume == 108_272_100.0
    assert snapshot.turnover == 1_273_354_840.61
    assert snapshot.source_ts_ns == MARKET_TIME_NS
    assert snapshot.name == PING_AN_NAME
    assert snapshot.currency == "CNY"
    assert snapshot.price_scale == 0
    assert snapshot.delay_s is None
    assert snapshot.tier is LiveTier.L2


def test_units_are_lots_of_one_hundred_shares() -> None:
    snapshot = _parse()
    assert snapshot is not None
    # Turnover over shares traded is a price near the last price only at a lot of 100 shares.
    assert snapshot.turnover is not None and snapshot.volume is not None
    assert snapshot.turnover / snapshot.volume == pytest.approx(11.76, abs=0.01)
    # The 15:00 print: 12,186 lots and 14,366,928.51 CNY, which is 11.79 per share.
    assert 14_366_928.51 / (12_186 * 100) == pytest.approx(11.79, abs=0.01)


def test_parses_the_book_in_shares_and_in_order() -> None:
    snapshot = _parse()
    assert snapshot is not None
    # The first five sellbuy5 items are the asks, lowest first; the last five are the bids.
    assert [level.price for level in snapshot.asks] == [11.80, 11.81, 11.82, 11.83, 11.84]
    assert [level.size for level in snapshot.asks] == [
        480_800.0, 244_900.0, 1_382_100.0, 1_885_000.0, 773_700.0,
    ]
    assert [level.price for level in snapshot.bids] == [11.79, 11.78, 11.77, 11.76, 11.75]
    assert [level.size for level in snapshot.bids] == [
        295_000.0, 369_300.0, 188_800.0, 147_600.0, 237_500.0,
    ]
    assert snapshot.best_bid is not None and snapshot.best_ask is not None
    assert snapshot.best_bid.price < snapshot.best_ask.price


def test_asks_sit_above_the_last_price_and_bids_at_or_below_it() -> None:
    snapshot = _parse()
    assert snapshot is not None and snapshot.last is not None
    assert all(level.price > snapshot.last for level in snapshot.asks)
    assert all(level.price <= snapshot.last for level in snapshot.bids)


def test_records_are_native_book_quote_and_print() -> None:
    snapshot = _parse()
    assert snapshot is not None
    records = snapshot_records(snapshot)
    assert [type(record) for record in records] == [BookSnapshot, Quote, Trade]
    book, quote, trade = records
    assert isinstance(book, BookSnapshot) and isinstance(quote, Quote)
    assert isinstance(trade, Trade)
    assert book.symbol == "000001@XSHE" and book.source == "szse"
    assert book.depth == 5 and len(book.bids) == 5 and len(book.asks) == 5
    assert book.prov is Provenance.NATIVE and book.prov_basis == "polled_book_snapshot"
    assert (quote.bid_px, quote.bid_sz) == (11.79, 295_000.0)
    assert (quote.ask_px, quote.ask_sz) == (11.80, 480_800.0)
    assert trade.price == 11.79 and trade.amount == 1_218_600.0
    assert trade.prov_basis == "polled_last_print"


def test_a_listing_not_in_the_answer_is_absent() -> None:
    other = LiveListing(mic="XSHE", local_symbol="000002")
    assert szse.parse_quote(_body(), other, captured_ns=CAPTURED_NS) is None


def test_no_last_price_means_no_last_and_no_trade() -> None:
    body = _body()
    assert b'"now":"11.79"' in body
    snapshot = szse.parse_quote(
        body.replace(b'"now":"11.79"', b'"now":"-"'), PING_AN, captured_ns=CAPTURED_NS
    )
    assert snapshot is not None
    assert snapshot.last is None and snapshot.last_size is None
    assert snapshot.prev_close == 11.71
    assert not any(isinstance(record, Trade) for record in snapshot_records(snapshot))


def test_a_zero_last_trade_size_is_not_a_size() -> None:
    body = _body()
    assert b'"lastVolume":12186' in body
    snapshot = szse.parse_quote(
        body.replace(b'"lastVolume":12186', b'"lastVolume":0'), PING_AN, captured_ns=CAPTURED_NS
    )
    assert snapshot is not None
    assert snapshot.last == 11.79 and snapshot.last_size is None
    assert not any(isinstance(record, Trade) for record in snapshot_records(snapshot))


def test_a_ladder_out_of_order_is_refused_whole() -> None:
    body = _body()
    assert b'"price":"11.80"' in body
    snapshot = szse.parse_quote(
        body.replace(b'"price":"11.80"', b'"price":"11.90"'), PING_AN, captured_ns=CAPTURED_NS
    )
    assert snapshot is not None
    assert snapshot.asks == ()
    assert [level.price for level in snapshot.bids] == [11.79, 11.78, 11.77, 11.76, 11.75]


def test_an_answer_for_another_code_is_no_snapshot() -> None:
    body = _body().replace(b'"code":"000001"', b'"code":"000002"', 1)
    assert szse.parse_quote(body, PING_AN, captured_ns=CAPTURED_NS) is None


def test_a_non_zero_outer_code_or_missing_data_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        szse.parse_quote(b'{"code":"1","data":null}', PING_AN, captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        szse.parse_quote(b'{"code":"0","data":[]}', PING_AN, captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        szse.parse_quote(b'{"code":"0"}', PING_AN, captured_ns=CAPTURED_NS)


def test_an_html_or_empty_answer_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        szse.parse_quote(b"<html>", PING_AN, captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        szse.parse_quote(b"", PING_AN, captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        szse.parse_quote(b"\xff\xfe\x00", PING_AN, captured_ns=CAPTURED_NS)


def test_a_json_answer_of_the_wrong_top_level_shape_is_a_payload_error() -> None:
    for body in (b"[]", b"null", b'"0"', b"42"):
        with pytest.raises(LivePayloadError):
            szse.parse_quote(body, PING_AN, captured_ns=CAPTURED_NS)


def test_zero_prices_are_placeholders_not_prices() -> None:
    body = _body()
    assert b'"now":"11.79"' in body and b'"close":"11.71"' in body
    assert b'"open":"11.69"' in body and b'"high":"11.83"' in body
    patched = (
        body.replace(b'"now":"11.79"', b'"now":"0"')
        .replace(b'"close":"11.71"', b'"close":"0"')
        .replace(b'"open":"11.69"', b'"open":"0.00"')
        .replace(b'"high":"11.83"', b'"high":"0"')
    )
    snapshot = szse.parse_quote(patched, PING_AN, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.last is None and snapshot.last_size is None
    assert snapshot.prev_close is None and snapshot.open is None and snapshot.high is None
    # The low and the book survive: only the zero placeholders are dropped.
    assert snapshot.low == 11.66
    assert len(snapshot.asks) == 5 and len(snapshot.bids) == 5
    assert not any(isinstance(record, Trade) for record in snapshot_records(snapshot))


def test_null_and_placeholder_fields_are_none_and_a_zero_volume_is_kept() -> None:
    body = _body()
    assert b'"volume":1082721' in body and b'"amount":1273354840.61' in body
    patched = body.replace(b'"volume":1082721', b'"volume":null').replace(
        b'"amount":1273354840.61', b'"amount":"-"'
    )
    snapshot = szse.parse_quote(patched, PING_AN, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.volume is None and snapshot.turnover is None
    assert snapshot.last == 11.79
    zero = szse.parse_quote(
        body.replace(b'"volume":1082721', b'"volume":0'), PING_AN, captured_ns=CAPTURED_NS
    )
    assert zero is not None and zero.volume == 0.0


def test_a_non_numeric_ladder_slot_is_an_empty_slot_and_a_null_size_stays_null() -> None:
    body = _body()
    assert b'{"price":"11.84","volume":7737}' in body
    assert b'{"price":"11.76","volume":1476}' in body
    patched = body.replace(b'{"price":"11.84","volume":7737}', b'"x"').replace(
        b'{"price":"11.76","volume":1476}', b'{"price":"11.76","volume":null}'
    )
    snapshot = szse.parse_quote(patched, PING_AN, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert [level.price for level in snapshot.asks] == [11.80, 11.81, 11.82, 11.83]
    assert [level.size for level in snapshot.asks] == [
        480_800.0, 244_900.0, 1_382_100.0, 1_885_000.0,
    ]
    bid = snapshot.bids[3]
    assert (bid.price, bid.size) == (11.76, None)


def test_a_sellbuy5_of_the_wrong_length_gives_no_ladder() -> None:
    body = _body()
    assert b',{"price":"11.75","volume":2375}' in body
    patched = body.replace(b',{"price":"11.75","volume":2375}', b"")
    snapshot = szse.parse_quote(patched, PING_AN, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.asks == () and snapshot.bids == ()
    assert snapshot.last == 11.79
    assert snapshot.tier is LiveTier.LAST


def test_a_market_time_that_is_not_a_clock_is_no_source_time() -> None:
    body = _body()
    assert b'"marketTime":"2026-11-12 15:30:00"' in body
    snapshot = szse.parse_quote(
        body.replace(b'"marketTime":"2026-11-12 15:30:00"', b'"marketTime":"15:30"'),
        PING_AN,
        captured_ns=CAPTURED_NS,
    )
    assert snapshot is not None
    assert snapshot.source_ts_ns is None and snapshot.last == 11.79


async def test_the_source_asks_for_one_listing_and_returns_its_snapshot() -> None:
    requests: list[HttpRequest] = []

    async def fake_fetch(request: HttpRequest) -> HttpResponse:
        requests.append(request)
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=_body(),
            url=request.url,
        )

    source = szse.SzseLive(fetch=fake_fetch)
    snapshots = await source.snapshots([PING_AN], captured_ns=CAPTURED_NS)
    assert len(requests) == 1
    assert requests[0].url == szse.QUOTE_URL
    assert requests[0].params == (("marketId", "1"), ("code", "000001"))
    assert [s.listing.symbol for s in snapshots] == ["000001@XSHE"]
    assert snapshots[0].last == 11.79


async def test_a_listing_of_another_market_is_not_requested() -> None:
    requests: list[HttpRequest] = []

    async def fake_fetch(request: HttpRequest) -> HttpResponse:
        requests.append(request)
        return HttpResponse(status=200, headers={}, body=_body(), url=request.url)

    source = szse.SzseLive(fetch=fake_fetch)
    snapshots = await source.snapshots(
        [LiveListing(mic="XSHG", local_symbol="600000")], captured_ns=CAPTURED_NS
    )
    assert snapshots == []
    assert requests == []
