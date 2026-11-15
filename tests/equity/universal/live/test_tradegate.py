"""``tradegate``: the Tradegate Exchange quote, read from a captured answer."""

from __future__ import annotations

from pathlib import Path

import pytest

from crocodile.core.schema.provenance import Provenance
from crocodile.core.schema.records import BookSnapshot, Quote
from crocodile.equity.universal.http import HttpRequest, HttpResponse
from crocodile.equity.universal.live.base import LivePayloadError
from crocodile.equity.universal.live.model import LiveListing, LiveTier, snapshot_records
from crocodile.equity.universal.live.sources import tradegate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live" / "tradegate"
ISIN = "DE0007164600"
CAPTURED_NS = 1_794_471_000_000_000_000
LISTING = LiveListing(mic="TGAT", local_symbol="SAP", isin=ISIN)


def _body() -> bytes:
    return (FIXTURES / "refresh_DE0007164600.json").read_bytes()


def test_german_number_reads_both_notations() -> None:
    assert tradegate.german_number(211.95) == 211.95
    assert tradegate.german_number(150) == 150.0
    assert tradegate.german_number("212,60") == 212.6
    assert tradegate.german_number("0,28") == 0.28
    assert tradegate.german_number("1.234,56") == 1234.56
    assert tradegate.german_number("1 234,56") == 1234.56
    assert tradegate.german_number("1\u00a0234,56") == 1234.56
    assert tradegate.german_number(" 212,00 ") == 212.0
    assert tradegate.german_number("1.234") == 1.234
    assert tradegate.german_number("-212,00") == -212.0


def test_german_number_refuses_placeholders_and_non_numbers() -> None:
    for placeholder in ("", "   ", "-", "./."):
        assert tradegate.german_number(placeholder) is None
    assert tradegate.german_number("abc") is None
    assert tradegate.german_number("1_000") is None
    assert tradegate.german_number("nan") is None
    assert tradegate.german_number(float("inf")) is None
    assert tradegate.german_number(True) is None
    assert tradegate.german_number(None) is None


def test_parses_the_top_of_book_in_eur_and_shares() -> None:
    snapshot = tradegate.parse_refresh(_body(), LISTING, captured_ns=CAPTURED_NS)
    assert snapshot.source == "tradegate"
    assert snapshot.listing == LISTING
    assert snapshot.symbol_raw == ISIN
    assert snapshot.tier is LiveTier.L1
    assert [(level.price, level.size) for level in snapshot.bids] == [(211.95, 150.0)]
    assert [(level.price, level.size) for level in snapshot.asks] == [(212.0, 200.0)]
    assert snapshot.last == 212.0
    assert snapshot.last_size is None
    assert snapshot.prev_close == 211.4
    assert (snapshot.high, snapshot.low) == (213.0, 211.3)
    assert snapshot.open is None
    assert snapshot.volume == 16149.0
    assert snapshot.turnover == 3422987.0
    assert snapshot.currency == "EUR"
    assert snapshot.price_scale == 0
    assert snapshot.source_ts_ns is None
    assert snapshot.delay_s is None


def test_records_are_book_and_quote_without_a_trade() -> None:
    snapshot = tradegate.parse_refresh(_body(), LISTING, captured_ns=CAPTURED_NS)
    records = snapshot_records(snapshot)
    assert [type(record) for record in records] == [BookSnapshot, Quote]
    book, quote = records
    assert isinstance(book, BookSnapshot) and isinstance(quote, Quote)
    assert book.symbol == "SAP@TGAT" and book.source == "tradegate"
    assert book.depth == 1 and len(book.bids) == 1 and len(book.asks) == 1
    assert book.prov is Provenance.NATIVE and book.prov_basis == "polled_book_snapshot"
    assert (quote.bid_px, quote.bid_sz, quote.ask_px, quote.ask_sz) == (211.95, 150.0, 212.0, 200.0)


def test_a_zero_or_placeholder_size_is_none_not_zero() -> None:
    body = _body().replace(b'"asksize": 200', b'"asksize": "-"').replace(
        b'"bidsize": 150', b'"bidsize": 0'
    )
    snapshot = tradegate.parse_refresh(body, LISTING, captured_ns=CAPTURED_NS)
    assert snapshot.bids[0].price == 211.95 and snapshot.bids[0].size is None
    assert snapshot.asks[0].price == 212.0 and snapshot.asks[0].size is None
    assert not any(isinstance(record, Quote) for record in snapshot_records(snapshot))


def test_no_last_price_is_not_filled_from_the_previous_close() -> None:
    body = _body().replace(b'"last": "212,00"', b'"last": "-"')
    snapshot = tradegate.parse_refresh(body, LISTING, captured_ns=CAPTURED_NS)
    assert snapshot.last is None
    assert snapshot.prev_close == 211.4
    assert snapshot.tier is LiveTier.L1


def test_an_answer_without_a_quote_is_a_payload_error() -> None:
    for body in (
        b"[]",
        b"{}",
        b'"212,00"',
        b'{"bid": "-", "ask": "", "last": "./."}',
        b'{"bid": 0, "ask": "0,00", "last": "-1"}',
        b"",
        b"<html>",
    ):
        with pytest.raises(LivePayloadError):
            tradegate.parse_refresh(body, LISTING, captured_ns=CAPTURED_NS)


def test_a_zero_or_negative_price_is_none_not_a_price() -> None:
    body = (
        b'{"bid": "0,00", "ask": "212,00", "asksize": 200, "last": 0, "high": "0,00", '
        b'"low": "-1", "close": -211.4, "stueck": 0, "umsatz": "-5"}'
    )
    snapshot = tradegate.parse_refresh(body, LISTING, captured_ns=CAPTURED_NS)
    assert snapshot.bids == ()
    assert [(level.price, level.size) for level in snapshot.asks] == [(212.0, 200.0)]
    assert snapshot.last is None
    assert (snapshot.high, snapshot.low, snapshot.prev_close) == (None, None, None)
    assert snapshot.volume == 0.0
    assert snapshot.turnover is None
    assert [type(record) for record in snapshot_records(snapshot)] == [BookSnapshot]


async def test_the_source_asks_for_one_isin_and_returns_its_snapshot() -> None:
    requested: list[HttpRequest] = []

    async def fetch(request: HttpRequest) -> HttpResponse:
        requested.append(request)
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=_body(),
            url=request.url,
        )

    source = tradegate.TradegateLive(fetch=fetch)
    snapshots = await source.snapshots([LISTING], captured_ns=CAPTURED_NS)
    assert len(requested) == 1
    assert requested[0].method == "GET"
    assert requested[0].url == "https://www.tradegatebsx.com/refresh.php"
    assert requested[0].params == (("isin", ISIN),)
    assert [snapshot.listing.symbol for snapshot in snapshots] == ["SAP@TGAT"]
    assert snapshots[0].last == 212.0


async def test_an_unknown_isin_answer_raises_rather_than_inventing_a_quote() -> None:
    async def fetch(request: HttpRequest) -> HttpResponse:
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=b"{}",
            url=request.url,
        )

    source = tradegate.TradegateLive(fetch=fetch)
    with pytest.raises(LivePayloadError):
        await source.snapshots([LISTING], captured_ns=CAPTURED_NS)


async def test_a_listing_without_an_isin_or_another_mic_is_not_asked() -> None:
    requested: list[HttpRequest] = []

    async def fetch(request: HttpRequest) -> HttpResponse:
        requested.append(request)
        return HttpResponse(status=200, headers={}, body=_body(), url=request.url)

    source = tradegate.TradegateLive(fetch=fetch)
    listings = [
        LiveListing(mic="TGAT", local_symbol="SAP"),
        LiveListing(mic="XETR", local_symbol="SAP", isin=ISIN),
    ]
    assert await source.snapshots(listings, captured_ns=CAPTURED_NS) == []
    assert requested == []
