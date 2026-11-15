"""``tmx``: the TMX Money quote, read from a captured answer."""

from __future__ import annotations

from pathlib import Path

import pytest

from crocodile.equity.universal.http import HttpRequest, HttpResponse
from crocodile.equity.universal.live.base import LivePayloadError
from crocodile.equity.universal.live.model import LiveListing, LiveTier, snapshot_records
from crocodile.equity.universal.live.sources import tmx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live" / "tmx"
CAPTURED_NS = 1_794_471_000_000_000_000
SHOP_TSX = LiveListing(mic="XTSE", local_symbol="SHOP")
SHOP_TSXV = LiveListing(mic="XTSX", local_symbol="SHOP")


def _body() -> bytes:
    return (FIXTURES / "quote_SHOP.json").read_bytes()


def test_parses_the_quote_in_dollars_and_shares() -> None:
    (snapshot,) = tmx.parse_quote(_body(), SHOP_TSX, captured_ns=CAPTURED_NS)
    assert snapshot.symbol_raw == "SHOP"
    assert snapshot.listing.symbol == "SHOP@XTSE"
    assert snapshot.last == 212.57
    assert snapshot.prev_close == 208.12
    assert (snapshot.open, snapshot.high, snapshot.low) == (212.0, 213.76, 209.73)
    # The fixture's volume is a share count and is passed through unchanged.
    assert snapshot.volume == 1_163_382.0
    assert snapshot.name == "Shopify Inc. Class A Subordinate Voting Shares"
    assert snapshot.currency == "CAD"
    assert snapshot.price_scale == 0
    assert snapshot.delay_s == 900
    assert snapshot.source_ts_ns is None
    assert snapshot.last_size is None
    assert snapshot.bids == () and snapshot.asks == ()
    assert snapshot.tier is LiveTier.LAST


def test_a_last_price_only_quote_produces_no_records() -> None:
    (snapshot,) = tmx.parse_quote(_body(), SHOP_TSX, captured_ns=CAPTURED_NS)
    assert snapshot_records(snapshot) == []


def test_the_same_ticker_on_the_other_board_is_not_attributed() -> None:
    assert tmx.parse_quote(_body(), SHOP_TSXV, captured_ns=CAPTURED_NS) == []


def test_tsx_venture_exchange_code_maps_to_xtsx() -> None:
    body = _body().replace(b'"exchangeCode":"TSX"', b'"exchangeCode":"TSXV"')
    assert tmx.parse_quote(body, SHOP_TSX, captured_ns=CAPTURED_NS) == []
    (snapshot,) = tmx.parse_quote(body, SHOP_TSXV, captured_ns=CAPTURED_NS)
    assert snapshot.listing.symbol == "SHOP@XTSX"
    assert snapshot.last == 212.57


def test_an_unknown_exchange_code_is_not_attributed() -> None:
    body = _body().replace(b'"exchangeCode":"TSX"', b'"exchangeCode":"NYSE"')
    assert tmx.parse_quote(body, SHOP_TSX, captured_ns=CAPTURED_NS) == []


def test_a_null_quote_means_an_unknown_symbol() -> None:
    body = b'{"data":{"getQuoteBySymbol":null}}'
    assert tmx.parse_quote(body, SHOP_TSX, captured_ns=CAPTURED_NS) == []


def test_a_missing_price_is_not_filled_from_prev_close() -> None:
    # Without a price the answer carries no price at all: prevClose, open and the day's range
    # are not a last price, so the poll is empty and no snapshot is produced.
    body = _body().replace(b'"price":212.57', b'"price":null')
    assert tmx.parse_quote(body, SHOP_TSX, captured_ns=CAPTURED_NS) == []


def test_an_unparseable_figure_is_none() -> None:
    body = _body().replace(b'"volume":1163382', b'"volume":"-"')
    (snapshot,) = tmx.parse_quote(body, SHOP_TSX, captured_ns=CAPTURED_NS)
    assert snapshot.volume is None
    assert snapshot.last == 212.57


def test_a_zero_or_negative_figure_is_none_and_a_published_zero_volume_is_kept() -> None:
    body = _body().replace(b'"dayLow":209.73', b'"dayLow":0')
    (snapshot,) = tmx.parse_quote(body, SHOP_TSX, captured_ns=CAPTURED_NS)
    assert snapshot.low is None
    assert snapshot.high == 213.76
    body = _body().replace(b'"openPrice":212', b'"openPrice":-212')
    (snapshot,) = tmx.parse_quote(body, SHOP_TSX, captured_ns=CAPTURED_NS)
    assert snapshot.open is None
    body = _body().replace(b'"volume":1163382', b'"volume":0')
    (snapshot,) = tmx.parse_quote(body, SHOP_TSX, captured_ns=CAPTURED_NS)
    assert snapshot.volume == 0.0
    body = _body().replace(b'"volume":1163382', b'"volume":-7')
    (snapshot,) = tmx.parse_quote(body, SHOP_TSX, captured_ns=CAPTURED_NS)
    assert snapshot.volume is None


def test_a_zero_or_negative_last_price_is_not_a_last_price() -> None:
    assert tmx.parse_quote(
        _body().replace(b'"price":212.57', b'"price":0'), SHOP_TSX, captured_ns=CAPTURED_NS
    ) == []
    assert tmx.parse_quote(
        _body().replace(b'"price":212.57', b'"price":-5'), SHOP_TSX, captured_ns=CAPTURED_NS
    ) == []


def test_an_answer_naming_another_symbol_is_not_attributed() -> None:
    body = _body().replace(b'"symbol":"SHOP"', b'"symbol":"AAPL"')
    assert tmx.parse_quote(body, SHOP_TSX, captured_ns=CAPTURED_NS) == []


def test_an_answer_without_the_quote_key_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        tmx.parse_quote(b'{"data":{}}', SHOP_TSX, captured_ns=CAPTURED_NS)


def test_an_errors_answer_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        tmx.parse_quote(b'{"errors":[{"message":"boom"}]}', SHOP_TSX, captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        tmx.parse_quote(
            b'{"data":null,"errors":[{"message":"boom"}]}', SHOP_TSX, captured_ns=CAPTURED_NS
        )


def test_a_body_that_is_not_an_object_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        tmx.parse_quote(b"[]", SHOP_TSX, captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        tmx.parse_quote(b"<html>", SHOP_TSX, captured_ns=CAPTURED_NS)


async def test_the_source_posts_one_request_per_listing() -> None:
    requests: list[HttpRequest] = []

    async def fetch(request: HttpRequest) -> HttpResponse:
        requests.append(request)
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=_body(),
            url=request.url,
        )

    source = tmx.TmxLive(fetch=fetch)
    snapshots = await source.snapshots([SHOP_TSX], captured_ns=CAPTURED_NS)
    assert [s.listing.symbol for s in snapshots] == ["SHOP@XTSE"]
    (request,) = requests
    assert request.method == "POST"
    assert request.url == "https://app-money.tmx.com/graphql"
    assert request.json_body == {
        "operationName": "getQuoteBySymbol",
        "variables": {"symbol": "SHOP", "locale": "en"},
        "query": tmx.QUERY,
    }
