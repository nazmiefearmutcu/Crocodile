"""``asx``: the ASX header of one listing, read from a captured answer."""

from __future__ import annotations

from pathlib import Path

import pytest

from crocodile.equity.universal.http import HttpRequest, HttpResponse
from crocodile.equity.universal.live.base import LivePayloadError
from crocodile.equity.universal.live.model import LiveListing, LiveTier, snapshot_records
from crocodile.equity.universal.live.sources import asx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live" / "asx"
CAPTURED_NS = 1_794_471_000_000_000_000
CBA = LiveListing(mic="XASX", local_symbol="CBA")
NYSE_CBA = LiveListing(mic="XNYS", local_symbol="CBA")


def _body() -> bytes:
    return (FIXTURES / "header_CBA.json").read_bytes()


def test_header_url_names_the_listing() -> None:
    assert asx.header_url("CBA") == (
        "https://asx.api.markitdigital.com/asx-research/1.0/companies/CBA/header"
    )


def test_parses_the_top_of_book_prices() -> None:
    snapshot = asx.parse_header(_body(), CBA, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.listing.symbol == "CBA@XASX" and snapshot.source == "asx"
    assert snapshot.symbol_raw == "CBA" and snapshot.captured_ns == CAPTURED_NS
    assert snapshot.tier is LiveTier.L1
    assert [(level.price, level.size) for level in snapshot.bids] == [(171.58, None)]
    assert [(level.price, level.size) for level in snapshot.asks] == [(171.79, None)]
    assert snapshot.last == 171.76
    assert snapshot.last_size is None
    # 171.76 - 4.819999999999993 rounded to six decimals.
    assert snapshot.prev_close == 166.94
    assert (snapshot.open, snapshot.high, snapshot.low) == (None, None, None)
    # 2,305,426 shares, as published.
    assert snapshot.volume == 2_305_426.0
    assert snapshot.turnover is None
    assert snapshot.prints == ()
    assert snapshot.source_ts_ns is None
    assert snapshot.currency == "AUD"
    assert snapshot.price_scale == 0
    assert snapshot.delay_s == 1200
    assert snapshot.name == "COMMONWEALTH BANK OF AUSTRALIA."


def test_price_only_top_of_book_yields_no_records() -> None:
    snapshot = asx.parse_header(_body(), CBA, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot_records(snapshot) == []


def test_an_answer_about_another_symbol_is_not_returned() -> None:
    body = _body().replace(b'"symbol":"CBA"', b'"symbol":"NAB"')
    assert asx.parse_header(body, CBA, captured_ns=CAPTURED_NS) is None


def test_a_zero_bid_and_a_missing_change_are_absent_not_invented() -> None:
    body = _body().replace(b'"priceBid":171.58', b'"priceBid":0').replace(
        b'"priceChange":4.819999999999993,', b""
    )
    snapshot = asx.parse_header(body, CBA, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.bids == ()
    assert [level.price for level in snapshot.asks] == [171.79]
    assert snapshot.tier is LiveTier.L1
    assert snapshot.prev_close is None
    assert snapshot.last == 171.76


def test_a_null_last_price_yields_no_trade_and_no_invented_prev_close() -> None:
    body = _body().replace(b'"priceLast":171.76', b'"priceLast":null').replace(
        b'"priceAsk":171.79', b'"priceAsk":"-"'
    )
    snapshot = asx.parse_header(body, CBA, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.last is None and snapshot.prev_close is None
    assert [level.price for level in snapshot.bids] == [171.58]
    assert snapshot.asks == ()
    assert snapshot.prints == () and snapshot.last_size is None
    assert snapshot.tier is LiveTier.L1
    assert snapshot_records(snapshot) == []


def test_a_negative_volume_and_a_prev_close_not_above_zero_are_absent() -> None:
    body = _body().replace(b'"volume":2305426', b'"volume":-5')
    snapshot = asx.parse_header(body, CBA, captured_ns=CAPTURED_NS)
    assert snapshot is not None and snapshot.volume is None
    body = _body().replace(b'"priceChange":4.819999999999993', b'"priceChange":200')
    snapshot = asx.parse_header(body, CBA, captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.prev_close is None and snapshot.last == 171.76


def test_malformed_answers_are_payload_errors_never_other_exceptions() -> None:
    for body in (b"", b"[]", b'{"data":null}', b'{"data":[1,2]}', b'{"data":"CBA"}', b"\xff\xfe"):
        with pytest.raises(LivePayloadError):
            asx.parse_header(body, CBA, captured_ns=CAPTURED_NS)


def test_a_listing_with_no_price_at_all_is_absent() -> None:
    body = b'{"data":{"symbol":"CBA","volume":0}}'
    assert asx.parse_header(body, CBA, captured_ns=CAPTURED_NS) is None


def test_an_answer_without_a_data_object_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        asx.parse_header(b'{"error":"not found"}', CBA, captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        asx.parse_header(b"<html>", CBA, captured_ns=CAPTURED_NS)


async def test_the_source_fetches_one_header_per_listing() -> None:
    requested: list[HttpRequest] = []

    async def fake_fetch(request: HttpRequest) -> HttpResponse:
        requested.append(request)
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=_body(),
            url=request.url,
        )

    source = asx.AsxLive(fetch=fake_fetch)
    snapshots = await source.snapshots([CBA], captured_ns=CAPTURED_NS)
    await source.close()
    assert [(r.method, r.url, r.params) for r in requested] == [
        ("GET", asx.header_url("CBA"), ())
    ]
    assert [s.listing.symbol for s in snapshots] == ["CBA@XASX"]
    assert snapshots[0].last == 171.76


async def test_a_listing_outside_the_supported_market_is_not_requested() -> None:
    requested: list[HttpRequest] = []

    async def fake_fetch(request: HttpRequest) -> HttpResponse:
        requested.append(request)
        return HttpResponse(status=200, headers={}, body=_body(), url=request.url)

    source = asx.AsxLive(fetch=fake_fetch)
    snapshots = await source.snapshots([NYSE_CBA], captured_ns=CAPTURED_NS)
    await source.close()
    assert snapshots == []
    assert requested == []
