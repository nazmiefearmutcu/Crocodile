"""``twse_mis``: the Taiwan MIS five-level book, read from a captured answer."""

from __future__ import annotations

from pathlib import Path

import pytest

from crocodile.core.schema.enums import Channel
from crocodile.core.schema.provenance import Provenance
from crocodile.core.schema.records import BookSnapshot, Quote, Trade
from crocodile.equity.universal.live.base import LivePayloadError
from crocodile.equity.universal.live.model import LiveListing, LiveTier, snapshot_records
from crocodile.equity.universal.live.sources import twse_mis

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live" / "twse_mis"
CAPTURED_NS = 1_794_471_000_000_000_000
TSMC = LiveListing(mic="XTAI", local_symbol="2330")
TPEX = LiveListing(mic="ROCO", local_symbol="6488")


def _body() -> bytes:
    return (FIXTURES / "quotes.json").read_bytes()


def test_channel_key_names_the_market() -> None:
    assert twse_mis.channel_key(TSMC) == "tse_2330.tw"
    assert twse_mis.channel_key(TPEX) == "otc_6488.tw"


def test_parses_a_five_level_book_in_shares() -> None:
    snapshots = {s.listing.symbol: s for s in twse_mis.parse_quotes(
        _body(), [TSMC, TPEX], captured_ns=CAPTURED_NS
    )}
    tsmc = snapshots["2330@XTAI"]
    assert tsmc.tier is LiveTier.L2
    assert [level.price for level in tsmc.bids] == [2335.0, 2330.0, 2325.0, 2320.0, 2315.0]
    assert [level.price for level in tsmc.asks] == [2340.0, 2345.0, 2350.0, 2355.0, 2360.0]
    # 150 lots at the best bid is 150,000 shares.
    assert tsmc.bids[0].size == 150_000.0
    assert tsmc.asks[0].size == 802_000.0
    assert tsmc.last == 2340.0
    assert tsmc.last_size == 3_501_000.0
    assert tsmc.prev_close == 2395.0
    assert (tsmc.open, tsmc.high, tsmc.low) == (2385.0, 2395.0, 2330.0)
    assert tsmc.volume == 28_991_000.0
    assert tsmc.source_ts_ns == 1_794_465_000_000 * 1_000_000
    assert tsmc.currency == "TWD"
    assert tsmc.delay_s == 0
    assert snapshots["6488@ROCO"].bids[0].price == 621.0


def test_a_listing_that_was_not_asked_for_is_not_returned() -> None:
    snapshots = twse_mis.parse_quotes(_body(), [TPEX], captured_ns=CAPTURED_NS)
    assert [s.listing.symbol for s in snapshots] == ["6488@ROCO"]


def test_records_are_native_book_quote_and_print() -> None:
    (snapshot,) = twse_mis.parse_quotes(_body(), [TSMC], captured_ns=CAPTURED_NS)
    records = snapshot_records(snapshot)
    kinds = [type(record) for record in records]
    assert kinds == [BookSnapshot, Quote, Trade]
    book, quote, trade = records
    assert isinstance(book, BookSnapshot) and isinstance(quote, Quote)
    assert isinstance(trade, Trade)
    assert book.symbol == "2330@XTAI" and book.source == "twse_mis"
    assert book.depth == 5 and len(book.bids) == 5 and len(book.asks) == 5
    assert book.prov is Provenance.NATIVE and book.prov_basis == "polled_book_snapshot"
    assert (quote.bid_px, quote.ask_px) == (2335.0, 2340.0)
    assert trade.price == 2340.0 and trade.amount == 3_501_000.0
    assert trade.prov_basis == "polled_last_print"
    assert Channel.BOOK_SNAPSHOT.value == "book_snapshot"


def test_no_last_price_means_no_trade() -> None:
    body = _body().replace(b'"z":"2340.0000","ts"', b'"z":"-","ts"')
    (snapshot,) = twse_mis.parse_quotes(body, [TSMC], captured_ns=CAPTURED_NS)
    assert snapshot.last is None and snapshot.last_size is None
    assert not any(isinstance(record, Trade) for record in snapshot_records(snapshot))


def test_an_answer_without_rows_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        twse_mis.parse_quotes(b'{"rtcode":"9999"}', [TSMC], captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        twse_mis.parse_quotes(b"<html>", [TSMC], captured_ns=CAPTURED_NS)
