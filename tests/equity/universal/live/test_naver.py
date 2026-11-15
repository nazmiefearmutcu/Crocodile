"""``naver``: the Korea Exchange realtime figures and asking ladder, read from captured answers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crocodile.core.schema.records import BookSnapshot, Quote
from crocodile.equity.universal.base import SourceUnservableError
from crocodile.equity.universal.http import Fetcher, HttpRequest, HttpResponse
from crocodile.equity.universal.live.base import LivePayloadError
from crocodile.equity.universal.live.model import LiveListing, LiveTier, snapshot_records
from crocodile.equity.universal.live.sources import naver
from crocodile.equity.universal.live.sources.naver import (
    ASKING_URL,
    REALTIME_URL,
    NaverLive,
    parse_asking,
    parse_realtime,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live" / "naver"
CAPTURED_NS = 1_794_471_000_000_000_000
SAMSUNG = LiveListing(mic="XKRX", local_symbol="005930")
KOSDAQ = LiveListing(mic="XKRX", local_symbol="247540")
SAMSUNG_NAME = "".join(chr(code) for code in (0xC0BC, 0xC131, 0xC804, 0xC790))


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _realtime() -> bytes:
    return _read("realtime_005930.json")


def _asking() -> bytes:
    return _read("asking_005930.json")


def mod_realtime(**fields: object) -> bytes:
    document = json.loads(_realtime())
    document["datas"][0].update(fields)
    return json.dumps(document).encode("utf-8")


def _fake(routes: dict[str, bytes]) -> tuple[list[HttpRequest], Fetcher]:
    requests: list[HttpRequest] = []

    async def fetch(request: HttpRequest) -> HttpResponse:
        requests.append(request)
        body = routes.get(request.url, b"{}")
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=body,
            url=request.url,
        )

    return requests, fetch


def test_urls_name_the_code() -> None:
    assert REALTIME_URL.format(code="005930") == (
        "https://polling.finance.naver.com/api/realtime/domestic/stock/005930"
    )
    assert ASKING_URL.format(code="005930") == (
        "https://m.stock.naver.com/api/stock/005930/askingPrice"
    )


def test_realtime_maps_the_session_figures() -> None:
    snapshot = parse_realtime(_realtime(), SAMSUNG, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.source == "naver"
    assert snapshot.symbol_raw == "005930"
    assert snapshot.listing.symbol == "005930@XKRX"
    assert snapshot.last == 208_000.0
    assert (snapshot.open, snapshot.high, snapshot.low) == (208_500.0, 214_000.0, 206_000.0)
    # Shares and won, as published: no conversion.
    assert snapshot.volume == 19_001_502.0
    assert snapshot.turnover == 3_977_310_000_000.0
    # 208,000 - 2,000 = 206,000 from the signed change; the asking answer states it too.
    assert snapshot.prev_close == 206_000.0
    # 2026-11-12T17:15:40.422782+09:00 is 08:15:40.422782 UTC.
    assert snapshot.source_ts_ns == 1_794_471_340_422_782_000
    assert snapshot.currency == "KRW"
    assert snapshot.price_scale == 0
    assert snapshot.delay_s == 0
    assert snapshot.name == SAMSUNG_NAME
    assert snapshot.bids == () and snapshot.asks == ()
    assert snapshot.tier is LiveTier.LAST


def test_asking_lists_both_sides_best_first() -> None:
    bids, asks, prev = parse_asking(_asking())
    # buyInfos is already best (highest) first.
    assert [(level.price, level.size) for level in bids] == [
        (206_500.0, 36_768.0),
        (206_000.0, 36_335.0),
        (205_500.0, 11_348.0),
        (205_000.0, 11_274.0),
        (204_500.0, 8_584.0),
    ]
    # sellInfo runs 209,000 down to 207,000 on the wire; reversed, the best ask comes first.
    assert [(level.price, level.size) for level in asks] == [
        (207_000.0, 1_381.0),
        (207_500.0, 24_266.0),
        (208_000.0, 22_970.0),
        (208_500.0, 30_320.0),
        (209_000.0, 15_381.0),
    ]
    assert prev == 206_000.0
    assert bids[0].price < asks[0].price


def test_asking_of_a_kosdaq_listing() -> None:
    bids, asks, prev = parse_asking(_read("asking_247540.json"))
    assert [(level.price, level.size) for level in asks] == [
        (89_300.0, 218.0),
        (89_400.0, 124.0),
        (89_500.0, 136.0),
        (89_600.0, 130.0),
        (89_700.0, 12.0),
    ]
    assert [(level.price, level.size) for level in bids] == [
        (89_200.0, 16.0),
        (89_100.0, 297.0),
        (89_000.0, 18.0),
        (88_900.0, 115.0),
        (88_800.0, 103.0),
    ]
    assert prev == 91_900.0


def test_a_listing_the_answer_does_not_contain_is_absent() -> None:
    assert parse_realtime(_realtime(), KOSDAQ, captured_ns=CAPTURED_NS) is None
    assert parse_realtime(b'{"datas":[]}', SAMSUNG, captured_ns=CAPTURED_NS) is None


def test_an_unexpected_body_is_a_payload_error() -> None:
    for body in (b"", b"<html>", b'{"datas":"x"}', b'{"other":[]}'):
        with pytest.raises(LivePayloadError):
            parse_realtime(body, SAMSUNG, captured_ns=CAPTURED_NS)
    for body in (b"", b"[]", b'{"lastClosePrice":1}'):
        with pytest.raises(LivePayloadError):
            parse_asking(body)


def test_a_zero_size_is_none_and_an_unordered_ladder_is_refused() -> None:
    bids, asks, _ = parse_asking(
        b'{"sellInfo":[{"price":"101","count":"0"}],"buyInfos":[{"price":"99","count":"5"}]}'
    )
    assert [(level.price, level.size) for level in asks] == [(101.0, None)]
    assert [(level.price, level.size) for level in bids] == [(99.0, 5.0)]
    _, asks, _ = parse_asking(
        b'{"sellInfo":[{"price":"100","count":"1"},{"price":"101","count":"2"}],"buyInfos":[]}'
    )
    assert asks == ()


def test_missing_raw_fields_fall_back_to_the_formatted_ones() -> None:
    body = _realtime().replace(b'"closePriceRaw":"208000",', b"")
    snapshot = parse_realtime(body, SAMSUNG, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.last == 208_000.0
    assert snapshot.prev_close == 206_000.0


def test_an_unparseable_change_leaves_prev_close_none() -> None:
    body = _realtime().replace(
        b'"compareToPreviousClosePriceRaw":"2000"', b'"compareToPreviousClosePriceRaw":"-"'
    )
    snapshot = parse_realtime(body, SAMSUNG, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.last == 208_000.0
    assert snapshot.prev_close is None


def test_currency_and_delay_are_the_publishers_statement() -> None:
    usd = _realtime().replace(b'"code":"KRW"', b'"code":"USD"')
    snapshot = parse_realtime(usd, SAMSUNG, captured_ns=CAPTURED_NS)
    assert snapshot is not None and snapshot.currency == "USD"
    delayed = _realtime().replace(b'"delayTime":0', b'"delayTime":20')
    snapshot = parse_realtime(delayed, SAMSUNG, captured_ns=CAPTURED_NS)
    assert snapshot is not None and snapshot.delay_s == 1200
    no_delay = _realtime().replace(b'"delayTime":0,', b"")
    snapshot = parse_realtime(no_delay, SAMSUNG, captured_ns=CAPTURED_NS)
    assert snapshot is not None and snapshot.delay_s is None


def test_a_missing_stamp_is_none() -> None:
    body = _realtime().replace(
        b'"localTradedAt":"2026-11-12T17:15:40.422782+09:00"', b'"localTradedAt":"not-a-time"'
    )
    snapshot = parse_realtime(body, SAMSUNG, captured_ns=CAPTURED_NS)
    assert snapshot is not None and snapshot.source_ts_ns is None


def test_currency_defaults_to_krw_when_unstated() -> None:
    body = _realtime().replace(b'"currencyType":{"code":"KRW"', b'"currencyType":{"x":"KRW"')
    snapshot = parse_realtime(body, SAMSUNG, captured_ns=CAPTURED_NS)
    assert snapshot is not None and snapshot.currency == "KRW"


def test_a_missing_last_price_yields_no_last_and_no_trade() -> None:
    body = mod_realtime(closePriceRaw="-", closePrice="-")
    snapshot = parse_realtime(body, SAMSUNG, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.last is None and snapshot.prev_close is None
    assert snapshot.last_size is None
    assert snapshot_records(snapshot) == []


def test_a_non_finite_change_or_delay_is_none_not_an_error() -> None:
    overflow = mod_realtime(closePriceRaw="1e308", compareToPreviousClosePriceRaw="-1e308")
    snapshot = parse_realtime(overflow, SAMSUNG, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.last == 1e308 and snapshot.prev_close is None
    huge = mod_realtime(stockExchangeType={"delayTime": 1e308})
    snapshot = parse_realtime(huge, SAMSUNG, captured_ns=CAPTURED_NS)
    assert snapshot is not None and snapshot.delay_s is None


async def test_a_listing_code_that_is_not_alphanumeric_is_not_requested() -> None:
    requests, fetch = _fake({})
    listing = LiveListing(mic="XKRX", local_symbol="../x")
    assert await NaverLive(fetch=fetch).snapshots([listing], captured_ns=CAPTURED_NS) == []
    assert requests == []


async def test_a_wall_on_the_asking_answer_is_raised() -> None:
    async def fetch(request: HttpRequest) -> HttpResponse:
        if "askingPrice" in request.url:
            return HttpResponse(
                status=403, headers={"content-type": "text/html"}, body=b"<html>", url=request.url
            )
        return HttpResponse(
            status=200, headers={"content-type": "application/json"}, body=_realtime(),
            url=request.url,
        )

    with pytest.raises(SourceUnservableError):
        await NaverLive(fetch=fetch).snapshots([SAMSUNG], captured_ns=CAPTURED_NS)


async def test_the_source_merges_realtime_and_asking() -> None:
    requests, fetch = _fake({
        REALTIME_URL.format(code="005930"): _realtime(),
        ASKING_URL.format(code="005930"): _asking(),
    })
    source = NaverLive(fetch=fetch)
    snapshots = await source.snapshots([SAMSUNG], captured_ns=CAPTURED_NS)
    assert [request.url for request in requests] == [
        REALTIME_URL.format(code="005930"),
        ASKING_URL.format(code="005930"),
    ]
    assert all(request.method == "GET" and request.params == () for request in requests)
    (snapshot,) = snapshots
    assert snapshot.tier is LiveTier.L2
    assert snapshot.last == 208_000.0
    assert snapshot.prev_close == 206_000.0
    assert snapshot.bids[0].price == 206_500.0 and snapshot.bids[0].size == 36_768.0
    assert snapshot.asks[0].price == 207_000.0 and snapshot.asks[0].size == 1_381.0
    assert len(snapshot.bids) == 5 and len(snapshot.asks) == 5

    book_and_quote = snapshot_records(snapshot)
    assert [type(record) for record in book_and_quote] == [BookSnapshot, Quote]
    book, quote = book_and_quote
    assert isinstance(book, BookSnapshot) and isinstance(quote, Quote)
    assert book.depth == 5 and book.prov_basis == "polled_book_snapshot"
    assert (quote.bid_px, quote.bid_sz) == (206_500.0, 36_768.0)
    assert (quote.ask_px, quote.ask_sz) == (207_000.0, 1_381.0)


async def test_an_asking_failure_leaves_the_snapshot_without_a_book() -> None:
    _, fetch = _fake({
        REALTIME_URL.format(code="005930"): _realtime(),
        ASKING_URL.format(code="005930"): b"",
    })
    snapshots = await NaverLive(fetch=fetch).snapshots([SAMSUNG], captured_ns=CAPTURED_NS)
    (snapshot,) = snapshots
    assert snapshot.bids == () and snapshot.asks == ()
    assert snapshot.last == 208_000.0
    assert snapshot.tier is LiveTier.LAST


async def test_an_absent_listing_is_not_returned_and_not_asked_for() -> None:
    requests, fetch = _fake({REALTIME_URL.format(code="247540"): _realtime()})
    snapshots = await NaverLive(fetch=fetch).snapshots([KOSDAQ], captured_ns=CAPTURED_NS)
    assert snapshots == []
    assert [request.url for request in requests] == [REALTIME_URL.format(code="247540")]


async def test_a_wall_is_raised_not_worked_around() -> None:
    async def fetch(request: HttpRequest) -> HttpResponse:
        return HttpResponse(
            status=403, headers={"content-type": "text/html"}, body=b"<html>", url=request.url
        )

    with pytest.raises(SourceUnservableError):
        await NaverLive(fetch=fetch).snapshots([SAMSUNG], captured_ns=CAPTURED_NS)


def test_the_module_declares_the_exemplar_contract() -> None:
    assert naver.SOURCE == "naver"
    assert NaverLive.supported_mics == frozenset({"XKRX"})
    assert NaverLive.max_tier is LiveTier.L2
    assert NaverLive.depth_levels == 5
    assert NaverLive.delay_s is None
    assert NaverLive.has_tape is False
    assert NaverLive.batch_size == 1
    assert NaverLive.min_interval_s == 7.0
    assert NaverLive.official is False
