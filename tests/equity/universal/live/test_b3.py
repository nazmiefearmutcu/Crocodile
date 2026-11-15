"""``b3``: B3's delayed quotation, read from a captured answer."""

from __future__ import annotations

from pathlib import Path

import pytest

from crocodile.equity.universal.http import HttpRequest, HttpResponse
from crocodile.equity.universal.live.base import LivePayloadError
from crocodile.equity.universal.live.model import LiveListing, LiveTier, snapshot_records
from crocodile.equity.universal.live.sources import b3

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live" / "b3"
CAPTURED_NS = 1_794_471_000_000_000_000
PETR4 = LiveListing(mic="BVMF", local_symbol="PETR4")
VALE3 = LiveListing(mic="BVMF", local_symbol="VALE3")
PETR4_URL = "https://cotacao.b3.com.br/mds/api/v1/instrumentQuotation/PETR4"


def _body() -> bytes:
    return (FIXTURES / "quotation_PETR4.json").read_bytes()


def test_quote_url_names_the_local_symbol() -> None:
    assert b3.QUOTE_URL + "PETR4" == PETR4_URL


def test_parses_the_delayed_session_figures() -> None:
    (snapshot,) = b3.parse_quote(_body(), PETR4, captured_ns=CAPTURED_NS)
    assert snapshot.source == "b3" and snapshot.listing.symbol == "PETR4@BVMF"
    assert snapshot.last == 43.55
    assert (snapshot.open, snapshot.high, snapshot.low) == (44.0, 44.52, 43.3)
    # 43.55 / (1 - 0.031145717) = 44.95; the fixture's change is (43.55 - 44.95) / 44.95.
    assert snapshot.prev_close == 44.95
    assert snapshot.name == "PETROBRAS PN N2"
    assert snapshot.source_ts_ns == 1_794_471_339_000_000_000
    assert snapshot.currency == "BRL"
    assert snapshot.price_scale == 0
    assert snapshot.delay_s == 900
    assert snapshot.tier is LiveTier.LAST
    assert snapshot.bids == () and snapshot.asks == ()


def test_ttl_quantity_is_not_mapped_to_volume() -> None:
    # ttlQty 96974 is in the fixture; the payload does not say whether it counts trades or shares.
    (snapshot,) = b3.parse_quote(_body(), PETR4, captured_ns=CAPTURED_NS)
    assert snapshot.volume is None
    assert snapshot.last_size is None
    assert snapshot.turnover is None


def test_a_last_price_without_a_size_or_book_makes_no_record() -> None:
    (snapshot,) = b3.parse_quote(_body(), PETR4, captured_ns=CAPTURED_NS)
    assert snapshot_records(snapshot) == []


def test_a_row_for_another_symbol_is_ignored() -> None:
    assert b3.parse_quote(_body(), VALE3, captured_ns=CAPTURED_NS) == []


def test_missing_last_price_stays_none_and_derives_no_previous_close() -> None:
    body = _body().replace(b'"curPrc":43.55', b'"curPrc":"-"')
    (snapshot,) = b3.parse_quote(body, PETR4, captured_ns=CAPTURED_NS)
    assert snapshot.last is None
    assert snapshot.prev_close is None
    assert snapshot.open == 44.0


def test_change_of_minus_one_hundred_percent_derives_no_previous_close() -> None:
    body = _body().replace(b'"prcFlcn":-3.1145717', b'"prcFlcn":-100')
    (snapshot,) = b3.parse_quote(body, PETR4, captured_ns=CAPTURED_NS)
    assert snapshot.last == 43.55
    assert snapshot.prev_close is None


def test_missing_change_derives_no_previous_close() -> None:
    body = _body().replace(b'"prcFlcn":-3.1145717', b'"prcFlcnX":-3.1145717')
    (snapshot,) = b3.parse_quote(body, PETR4, captured_ns=CAPTURED_NS)
    assert snapshot.last == 43.55
    assert snapshot.prev_close is None


def test_unparseable_stamp_is_none() -> None:
    body = _body().replace(b"2026-11-12 05:15:39", b"not a time")
    (snapshot,) = b3.parse_quote(body, PETR4, captured_ns=CAPTURED_NS)
    assert snapshot.source_ts_ns is None


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"<html>",
        b"[1, 2]",
        b'{"BizSts":{"cd":"NOK"},"Msg":{"dtTm":"2026-11-12 05:15:39"},"Trad":[]}',
        b'{"BizSts":{"cd":"OK"},"Msg":{"dtTm":"2026-11-12 05:15:39"}}',
        b'{"BizSts":{"cd":"OK"},"Msg":{"dtTm":"2026-11-12 05:15:39"},"Trad":[]}',
        b'{"BizSts":{"cd":"OK"},"Trad":{"scty":{}}}',
        b'{"BizSts":{"cd":"OK"},"Trad":[{"scty":{"symb":"PETR4"}}]}',
        b'{"BizSts":{"cd":"OK"},"Trad":[{"scty":{"symb":"PETR4","SctyQtn":[]}}]}',
        b'{"BizSts":{},"Trad":[{}]}',
    ],
)
def test_a_malformed_answer_is_a_payload_error(body: bytes) -> None:
    with pytest.raises(LivePayloadError):
        b3.parse_quote(body, PETR4, captured_ns=CAPTURED_NS)


def test_two_rows_for_the_listing_are_refused_not_returned_twice() -> None:
    body = _body().replace(b'"Trad":[', b'"Trad":[{"scty":{"symb":"PETR4","SctyQtn":{}}},')
    with pytest.raises(LivePayloadError):
        b3.parse_quote(body, PETR4, captured_ns=CAPTURED_NS)


def test_non_object_rows_are_skipped() -> None:
    body = _body().replace(b'"Trad":[', b'"Trad":[1,"x",null,')
    (snapshot,) = b3.parse_quote(body, PETR4, captured_ns=CAPTURED_NS)
    assert snapshot.last == 43.55


def test_null_open_and_name_stay_none() -> None:
    body = _body().replace(b'"opngPric":44', b'"opngPric":null').replace(
        b'"desc":"PETROBRAS   PN      N2"', b'"desc":null'
    )
    (snapshot,) = b3.parse_quote(body, PETR4, captured_ns=CAPTURED_NS)
    assert snapshot.open is None
    assert snapshot.name is None
    assert snapshot.last == 43.55


def test_a_zero_last_price_is_absent_and_derives_no_previous_close() -> None:
    body = _body().replace(b'"curPrc":43.55', b'"curPrc":0')
    (snapshot,) = b3.parse_quote(body, PETR4, captured_ns=CAPTURED_NS)
    assert snapshot.last is None
    assert snapshot.prev_close is None


def test_a_decimal_comma_is_refused_not_read_as_a_thousands_separator() -> None:
    # "43,55" would otherwise parse as 4355.0, a price a hundred times too high.
    body = _body().replace(b'"curPrc":43.55', b'"curPrc":"43,55"')
    (snapshot,) = b3.parse_quote(body, PETR4, captured_ns=CAPTURED_NS)
    assert snapshot.last is None
    assert snapshot.prev_close is None


def test_a_non_finite_previous_close_is_none() -> None:
    # 1e308 / (1 - 0.999999) overflows to infinity; an infinite previous close is not a price.
    body = _body().replace(b'"curPrc":43.55', b'"curPrc":1e308').replace(
        b'"prcFlcn":-3.1145717', b'"prcFlcn":-99.9999'
    )
    (snapshot,) = b3.parse_quote(body, PETR4, captured_ns=CAPTURED_NS)
    assert snapshot.last == 1e308
    assert snapshot.prev_close is None


async def test_the_source_requests_one_quote_and_returns_its_snapshot() -> None:
    requested: list[str] = []

    async def fake(request: HttpRequest) -> HttpResponse:
        requested.append(request.url)
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=_body(),
            url=request.url,
        )

    source = b3.B3Live(fetch=fake)
    try:
        snapshots = await source.snapshots([PETR4], captured_ns=CAPTURED_NS)
    finally:
        await source.close()
    assert requested == [PETR4_URL]
    assert [s.listing.symbol for s in snapshots] == ["PETR4@BVMF"]
    assert snapshots[0].last == 43.55


async def test_an_unserved_market_is_never_requested() -> None:
    requested: list[str] = []

    async def fake(request: HttpRequest) -> HttpResponse:
        requested.append(request.url)
        return HttpResponse(status=200, headers={}, body=_body(), url=request.url)

    source = b3.B3Live(fetch=fake)
    try:
        snapshots = await source.snapshots(
            [LiveListing(mic="XTAI", local_symbol="2330")], captured_ns=CAPTURED_NS
        )
    finally:
        await source.close()
    assert requested == [] and snapshots == []
