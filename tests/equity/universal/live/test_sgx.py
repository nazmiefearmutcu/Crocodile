"""``sgx``: the Singapore Exchange quote, read from a captured answer."""

from __future__ import annotations

from pathlib import Path

import pytest

from crocodile.core.schema.records import BookSnapshot, Quote, Trade
from crocodile.equity.universal.http import HttpRequest, HttpResponse
from crocodile.equity.universal.live.base import LivePayloadError
from crocodile.equity.universal.live.model import LiveLevel, LiveListing, LiveTier, snapshot_records
from crocodile.equity.universal.live.sources import sgx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live" / "sgx"
CAPTURED_NS = 1_794_471_340_000_000_000
DBS = LiveListing(mic="XSES", local_symbol="D05")
OTHER = LiveListing(mic="XSES", local_symbol="D06")


def _body() -> bytes:
    return (FIXTURES / "code_D05.json").read_bytes()


def test_parses_the_top_of_book_in_shares() -> None:
    (dbs,) = sgx.parse_quotes(_body(), [DBS], captured_ns=CAPTURED_NS)
    assert dbs.listing.symbol == "D05@XSES"
    assert dbs.symbol_raw == "D05"
    assert dbs.name == "DBS"
    assert dbs.tier is LiveTier.L1
    # b "71.250" with bv 5.29 thousand shares, s "71.260" with sv 8.9 thousand shares.
    assert dbs.bids == (LiveLevel(price=71.25, size=5_290.0),)
    assert dbs.asks == (LiveLevel(price=71.26, size=8_900.0),)
    assert dbs.last == 71.25
    assert dbs.last_size is None
    assert dbs.prev_close == 70.97
    assert (dbs.open, dbs.high, dbs.low) == (71.55, 71.6, 71.0)
    # vl 2626.8 thousand shares.
    assert dbs.volume == 2_626_800.0
    assert dbs.turnover == 187_368_081.3
    assert dbs.currency == "SGD"
    assert dbs.price_scale == 0
    assert dbs.delay_s is None


def test_volume_unit_is_thousands_of_shares() -> None:
    # v / (vl * 1000) is the session's average price: 71.33, inside the 71.00 to 71.60 range
    # and beside the last price of 71.25.
    (dbs,) = sgx.parse_quotes(_body(), [DBS], captured_ns=CAPTURED_NS)
    assert dbs.turnover is not None and dbs.volume is not None
    assert dbs.turnover / dbs.volume == pytest.approx(71.33, abs=0.01)
    assert dbs.low is not None and dbs.high is not None
    assert dbs.low <= dbs.turnover / dbs.volume <= dbs.high


def test_source_stamp_is_utc_trading_time() -> None:
    # 20261112_081400 UTC is 16:14 in Singapore, inside the session.
    (dbs,) = sgx.parse_quotes(_body(), [DBS], captured_ns=CAPTURED_NS)
    assert dbs.source_ts_ns == 1_794_471_240_000_000_000


def test_records_are_book_and_quote_without_a_trade() -> None:
    (dbs,) = sgx.parse_quotes(_body(), [DBS], captured_ns=CAPTURED_NS)
    records = snapshot_records(dbs)
    assert [type(record) for record in records] == [BookSnapshot, Quote]
    book, quote = records
    assert isinstance(book, BookSnapshot) and isinstance(quote, Quote)
    assert book.depth == 1 and len(book.bids) == 1 and len(book.asks) == 1
    assert (quote.bid_px, quote.bid_sz) == (71.25, 5_290.0)
    assert (quote.ask_px, quote.ask_sz) == (71.26, 8_900.0)
    assert not any(isinstance(record, Trade) for record in records)


def test_a_listing_that_was_not_asked_for_is_not_returned() -> None:
    assert sgx.parse_quotes(_body(), [OTHER], captured_ns=CAPTURED_NS) == []


def test_zero_last_price_means_no_trade_yet() -> None:
    body = _body().replace(b'"lt":71.250000', b'"lt":0')
    (dbs,) = sgx.parse_quotes(body, [DBS], captured_ns=CAPTURED_NS)
    assert dbs.last is None
    assert dbs.bids == (LiveLevel(price=71.25, size=5_290.0),)


def test_null_last_price_means_no_trade_yet() -> None:
    body = _body().replace(b'"lt":71.250000', b'"lt":null')
    (dbs,) = sgx.parse_quotes(body, [DBS], captured_ns=CAPTURED_NS)
    assert dbs.last is None


def test_zero_bid_size_is_unknown_not_zero() -> None:
    body = _body().replace(b'"bv":5.29', b'"bv":0')
    (dbs,) = sgx.parse_quotes(body, [DBS], captured_ns=CAPTURED_NS)
    assert dbs.bids == (LiveLevel(price=71.25, size=None),)
    assert [type(record) for record in snapshot_records(dbs)] == [BookSnapshot]


def test_missing_trading_time_means_no_source_stamp() -> None:
    body = _body().replace(b'"trading_time":"20261112_081400"', b'"trading_time":null')
    (dbs,) = sgx.parse_quotes(body, [DBS], captured_ns=CAPTURED_NS)
    assert dbs.source_ts_ns is None


def test_a_non_200_meta_code_is_a_payload_error() -> None:
    body = _body().replace(b'"code":"200"', b'"code":"500"')
    with pytest.raises(LivePayloadError):
        sgx.parse_quotes(body, [DBS], captured_ns=CAPTURED_NS)


def test_an_answer_without_prices_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        sgx.parse_quotes(b'{"meta":{"code":"200"},"data":{}}', [DBS], captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        sgx.parse_quotes(b"", [DBS], captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        sgx.parse_quotes(b"<html>", [DBS], captured_ns=CAPTURED_NS)


async def test_source_requests_one_quote_and_returns_its_snapshot() -> None:
    requests: list[HttpRequest] = []

    async def fetch(request: HttpRequest) -> HttpResponse:
        requests.append(request)
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=_body(),
            url=request.url,
        )

    source = sgx.SgxLive(fetch=fetch)
    snapshots = await source.snapshots([DBS], captured_ns=CAPTURED_NS)
    assert [request.url for request in requests] == [
        "https://api.sgx.com/securities/v1.1/stocks/code/D05"
    ]
    assert requests[0].params == (("params", sgx.FIELDS),)
    assert [snapshot.listing.symbol for snapshot in snapshots] == ["D05@XSES"]
    assert snapshots[0].bids == (LiveLevel(price=71.25, size=5_290.0),)


async def test_source_skips_listings_it_does_not_serve() -> None:
    requests: list[HttpRequest] = []

    async def fetch(request: HttpRequest) -> HttpResponse:
        requests.append(request)
        return HttpResponse(status=200, headers={}, body=_body(), url=request.url)

    source = sgx.SgxLive(fetch=fetch)
    snapshots = await source.snapshots([LiveListing(mic="XTAI", local_symbol="2330")],
                                       captured_ns=CAPTURED_NS)
    assert requests == []
    assert snapshots == []


def test_negative_volume_and_turnover_are_unknown() -> None:
    body = _body().replace(b'"vl":2626.800000', b'"vl":-1').replace(
        b'"v":187368081.300000', b'"v":-1'
    )
    (dbs,) = sgx.parse_quotes(body, [DBS], captured_ns=CAPTURED_NS)
    assert dbs.volume is None
    assert dbs.turnover is None
    assert dbs.last == 71.25


def test_zero_turnover_is_kept_as_published() -> None:
    body = _body().replace(b'"v":187368081.300000', b'"v":0')
    (dbs,) = sgx.parse_quotes(body, [DBS], captured_ns=CAPTURED_NS)
    assert dbs.turnover == 0.0


def test_missing_currency_falls_back_to_the_market_currency() -> None:
    body = _body().replace(b'"cur":"SGD",', b"")
    (dbs,) = sgx.parse_quotes(body, [DBS], captured_ns=CAPTURED_NS)
    assert dbs.currency == "SGD"


def test_a_row_for_another_code_is_ignored() -> None:
    body = _body().replace(b'"nc":"D05"', b'"nc":"D06"')
    assert sgx.parse_quotes(body, [DBS], captured_ns=CAPTURED_NS) == []


def test_placeholder_bid_is_unknown_and_the_ask_survives() -> None:
    body = _body().replace(b'"b":"71.250"', b'"b":"-"')
    (dbs,) = sgx.parse_quotes(body, [DBS], captured_ns=CAPTURED_NS)
    assert dbs.bids == ()
    assert dbs.asks == (LiveLevel(price=71.26, size=8_900.0),)
    assert dbs.last == 71.25
    assert [type(record) for record in snapshot_records(dbs)] == [BookSnapshot]


def test_placeholder_last_and_empty_book_leave_no_snapshot() -> None:
    # A row with no last price and no resting level has no price, so it is absent: the
    # previous close alone is never turned into a last price.
    body = _body().replace(b'"lt":71.250000', b'"lt":"N/A"').replace(
        b'"b":"71.250"', b'"b":""'
    ).replace(b'"s":"71.260"', b'"s":"-"')
    assert sgx.parse_quotes(body, [DBS], captured_ns=CAPTURED_NS) == []


def test_a_row_with_no_price_at_all_is_absent() -> None:
    body = (
        b'{"meta":{"code":"200"},"data":{"prices":[{"nc":"D05","n":"DBS",'
        b'"lt":null,"b":null,"s":"-","bv":null,"sv":null,"trading_time":null}]}}'
    )
    assert sgx.parse_quotes(body, [DBS], captured_ns=CAPTURED_NS) == []


def test_an_unparseable_trading_time_means_no_source_stamp() -> None:
    body = _body().replace(b'"trading_time":"20261112_081400"', b'"trading_time":"soon"')
    (dbs,) = sgx.parse_quotes(body, [DBS], captured_ns=CAPTURED_NS)
    assert dbs.source_ts_ns is None


def test_wrong_shapes_are_payload_errors_not_crashes() -> None:
    for body in (
        b"[]",
        b'{"meta":"200","data":{"prices":[]}}',
        b'{"meta":{"code":"200"},"data":[]}',
        b'{"meta":{"code":"200"},"data":{"prices":null}}',
        b'{"meta":{"code":"200"}}',
    ):
        with pytest.raises(LivePayloadError):
            sgx.parse_quotes(body, [DBS], captured_ns=CAPTURED_NS)


def test_non_object_rows_are_skipped_without_crashing() -> None:
    body = b'{"meta":{"code":"200"},"data":{"prices":[1,"x",null,[]]}}'
    assert sgx.parse_quotes(body, [DBS], captured_ns=CAPTURED_NS) == []


async def test_the_local_symbol_is_one_path_segment() -> None:
    requests: list[HttpRequest] = []

    async def fetch(request: HttpRequest) -> HttpResponse:
        requests.append(request)
        return HttpResponse(status=200, headers={}, body=_body(), url=request.url)

    source = sgx.SgxLive(fetch=fetch)
    await source.snapshots(
        [LiveListing(mic="XSES", local_symbol="D05/../x?y")], captured_ns=CAPTURED_NS
    )
    assert requests[0].url == (
        "https://api.sgx.com/securities/v1.1/stocks/code/D05%2F..%2Fx%3Fy"
    )
