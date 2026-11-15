"""``six``: SIX Swiss Exchange's whole-market quote answer, read from a captured answer."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from crocodile.core.markets.usage import UsageClass
from crocodile.core.schema.enums import Channel
from crocodile.core.schema.records import BookSnapshot, Quote, Trade
from crocodile.equity.universal.http import HttpRequest, HttpResponse
from crocodile.equity.universal.live.base import LivePayloadError
from crocodile.equity.universal.live.model import LiveListing, LiveTier, snapshot_records
from crocodile.equity.universal.live.sources import six

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live" / "six"
CAPTURED_NS = 1_794_472_000_000_000_000
ABB = LiveListing(mic="XSWX", local_symbol="ABBN")
ABB_NEXT = LiveListing(mic="XSWX", local_symbol="ABBNE")
DDD = LiveListing(mic="XSWX", local_symbol="DDD")
MMM = LiveListing(mic="XSWX", local_symbol="MMM")
ONE_ONE = LiveListing(mic="XSWX", local_symbol="1U1")
NOT_HERE = LiveListing(mic="XSWX", local_symbol="NESN")
TSMC = LiveListing(mic="XTAI", local_symbol="2330")


def _body() -> bytes:
    return (FIXTURES / "ref_all.json").read_bytes()


def _document() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_body())
    return loaded


def _encoded(document: dict[str, Any]) -> bytes:
    return json.dumps(document).encode("utf-8")


def _cell(document: dict[str, Any], row: int, column: str) -> Any:
    return document["rowData"][row][document["colNames"].index(column)]


def _set(document: dict[str, Any], row: int, column: str, value: Any) -> None:
    document["rowData"][row][document["colNames"].index(column)] = value


def _parse(body: bytes, *listings: LiveListing) -> dict[str, Any]:
    snapshots = six.parse_quotes(body, list(listings), captured_ns=CAPTURED_NS)
    return {snapshot.listing.symbol: snapshot for snapshot in snapshots}


def test_the_answer_states_the_fixture_is_truncated() -> None:
    document = _document()
    assert document["totalRows"] == 813
    assert len(document["rowData"]) == 6


def test_maps_the_abb_row_in_chf() -> None:
    abb = _parse(_body(), ABB, ABB_NEXT)["ABBN@XSWX"]
    assert abb.source == "six"
    assert abb.symbol_raw == "ABBN"
    assert abb.name == "ABB LTD N"
    assert abb.currency == "CHF"
    assert abb.price_scale is None
    assert abb.delay_s == 900
    assert abb.last == 70.38
    assert abb.last_size == 278.0
    assert (abb.prev_close, abb.open, abb.high, abb.low) == (70.7, 69.74, 70.52, 69.74)
    assert abb.volume == 206_118.0
    assert [(level.price, level.size) for level in abb.bids] == [(70.38, 566.0)]
    assert [(level.price, level.size) for level in abb.asks] == [(70.4, 346.0)]
    assert abb.tier is LiveTier.L1
    # 09:20:20.829 Europe/Zurich on 2026-11-12 is 08:20:20.829 UTC.
    assert abb.source_ts_ns == 1_794_471_620_829_000_000


def test_a_second_row_keeps_its_own_fields() -> None:
    nxt = _parse(_body(), ABB_NEXT)["ABBNE@XSWX"]
    assert nxt.last == 70.48
    assert nxt.last_size == 250.0
    assert [(level.price, level.size) for level in nxt.bids] == [(70.2, 1000.0)]
    assert [(level.price, level.size) for level in nxt.asks] == [(70.5, 250.0)]
    assert nxt.volume == 6717.0
    assert nxt.source_ts_ns == 1_794_471_620_795_000_000


def test_a_zero_volume_is_a_real_zero_and_a_zero_trade_time_is_none() -> None:
    ddd = _parse(_body(), DDD)["DDD@XSWX"]
    assert ddd.volume == 0.0
    assert ddd.last == 1.5398
    assert ddd.last_size is None
    assert (ddd.open, ddd.high, ddd.low) == (None, None, None)
    assert ddd.source_ts_ns is None
    assert ddd.currency == "CHF"


def test_a_symbol_on_two_currencies_resolves_to_the_mic_currency() -> None:
    # The fixture has DDD on a CHF row and on a USD row; XSWX is CHF.
    ddd = _parse(_body(), DDD)["DDD@XSWX"]
    assert ddd.currency == "CHF"
    assert ddd.last == 1.5398
    assert [(level.price, level.size) for level in ddd.bids] == [(1.4904, 12_000.0)]


def test_a_symbol_on_two_rows_of_the_same_currency_is_absent() -> None:
    document = _document()
    document["rowData"].append(copy.deepcopy(document["rowData"][4]))
    assert "ABBN@XSWX" not in _parse(_encoded(document), ABB)


def test_a_non_numeric_bid_is_no_level_and_a_zero_trade_size_is_none() -> None:
    mmm = _parse(_body(), MMM)["MMM@XSWX"]
    assert mmm.bids == ()
    assert [(level.price, level.size) for level in mmm.asks] == [(160.0, 1.0)]
    assert mmm.last == 157.0
    assert mmm.last_size is None
    assert mmm.tier is LiveTier.L1


def test_records_are_book_quote_and_last_print_for_a_two_sided_top() -> None:
    (snapshot,) = six.parse_quotes(_body(), [ABB], captured_ns=CAPTURED_NS)
    records = snapshot_records(snapshot)
    assert [type(record) for record in records] == [BookSnapshot, Quote, Trade]
    book, quote, trade = records
    assert isinstance(book, BookSnapshot) and isinstance(quote, Quote)
    assert isinstance(trade, Trade)
    assert book.symbol == "ABBN@XSWX" and book.source == "six"
    assert book.depth == 1
    assert (quote.bid_px, quote.bid_sz, quote.ask_px, quote.ask_sz) == (70.38, 566.0, 70.4, 346.0)
    assert (trade.price, trade.amount) == (70.38, 278.0)
    assert Channel.BOOK_SNAPSHOT.value == "book_snapshot"


def test_an_ask_only_top_is_a_book_without_a_quote_or_a_trade() -> None:
    (snapshot,) = six.parse_quotes(_body(), [MMM], captured_ns=CAPTURED_NS)
    assert [type(record) for record in snapshot_records(snapshot)] == [BookSnapshot]


def test_a_listing_that_was_not_asked_for_is_not_returned() -> None:
    assert list(_parse(_body(), ABB)) == ["ABBN@XSWX"]
    assert list(_parse(_body(), ONE_ONE, NOT_HERE)) == ["1U1@XSWX"]


def test_a_listing_on_another_market_is_not_matched() -> None:
    assert list(_parse(_body(), TSMC)) == []


def test_an_empty_last_price_is_none_and_the_book_stays() -> None:
    document = _document()
    _set(document, 4, "ClosingPrice", "")
    abb = _parse(_encoded(document), ABB)["ABBN@XSWX"]
    assert abb.last is None and abb.last_size is None
    assert [level.price for level in abb.bids] == [70.38]
    assert not any(isinstance(record, Trade) for record in snapshot_records(abb))


def test_a_zero_level_size_is_none_not_zero() -> None:
    document = _document()
    _set(document, 4, "BidVolume", 0)
    abb = _parse(_encoded(document), ABB)["ABBN@XSWX"]
    assert [(level.price, level.size) for level in abb.bids] == [(70.38, None)]
    assert [type(record) for record in snapshot_records(abb)] == [BookSnapshot, Trade]


def test_a_non_numeric_total_volume_is_none() -> None:
    document = _document()
    _set(document, 4, "TotalVolume", "")
    assert _parse(_encoded(document), ABB)["ABBN@XSWX"].volume is None


def test_an_impossible_trade_date_drops_the_stamp() -> None:
    document = _document()
    _set(document, 4, "LatestTradeDate", 20261340)
    assert _parse(_encoded(document), ABB)["ABBN@XSWX"].source_ts_ns is None


def test_a_missing_delay_falls_back_to_the_class_delay() -> None:
    document = _document()
    del document["delayMinutes"]
    assert _parse(_encoded(document), ABB)["ABBN@XSWX"].delay_s == six.DELAY_S


def test_an_answer_without_the_row_table_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        six.parse_quotes(b'{"totalRows": 0}', [ABB], captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        six.parse_quotes(b"<html>", [ABB], captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        six.parse_quotes(b"", [ABB], captured_ns=CAPTURED_NS)


def test_a_table_without_a_required_column_is_a_payload_error() -> None:
    document = _document()
    document["colNames"][document["colNames"].index("AskPrice")] = "Ask"
    with pytest.raises(LivePayloadError):
        six.parse_quotes(_encoded(document), [ABB], captured_ns=CAPTURED_NS)


async def test_the_source_makes_one_request_for_the_whole_market() -> None:
    requests: list[HttpRequest] = []

    async def fetch(request: HttpRequest) -> HttpResponse:
        requests.append(request)
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=_body(),
            url=request.url,
        )

    source = six.SixLive(fetch=fetch)
    snapshots = await source.snapshots(
        [ABB, DDD, NOT_HERE, TSMC], captured_ns=CAPTURED_NS
    )
    assert len(requests) == 1
    assert requests[0].url == "https://www.six-group.com/fqs/ref.json"
    assert requests[0].params == six.PARAMS
    params = dict(requests[0].params)
    assert params["where"] == "PortalSegment=EQ"
    assert params["orderby"] == "ShortName"
    assert params["pagesize"] == "1000"
    assert len(params["select"].split(",")) == 18
    assert sorted(snapshot.listing.symbol for snapshot in snapshots) == ["ABBN@XSWX", "DDD@XSWX"]


async def test_no_request_is_made_for_a_listing_the_source_does_not_serve() -> None:
    requests: list[HttpRequest] = []

    async def fetch(request: HttpRequest) -> HttpResponse:
        requests.append(request)
        return HttpResponse(status=200, headers={}, body=_body(), url=request.url)

    source = six.SixLive(fetch=fetch)
    assert await source.snapshots([TSMC], captured_ns=CAPTURED_NS) == []
    assert requests == []


def test_the_next_row_carries_its_session_figures() -> None:
    nxt = _parse(_body(), ABB_NEXT)["ABBNE@XSWX"]
    assert nxt.name == "ABB LTD N 2. LINIE"
    assert nxt.currency == "CHF"
    assert (nxt.prev_close, nxt.open, nxt.high, nxt.low) == (70.72, 70.62, 70.64, 70.26)


def test_the_ddd_chf_row_has_its_ask_and_its_previous_close() -> None:
    ddd = _parse(_body(), DDD)["DDD@XSWX"]
    assert [(level.price, level.size) for level in ddd.asks] == [(1.5464, 12_000.0)]
    assert ddd.prev_close == 1.5398


def test_a_stale_row_keeps_its_price_and_states_no_trade_time() -> None:
    one_one = _parse(_body(), ONE_ONE)["1U1@XSWX"]
    assert one_one.name == "1&1 AG"
    assert one_one.last == 22.25
    assert one_one.last_size is None
    assert one_one.volume == 0.0
    assert (one_one.open, one_one.high, one_one.low) == (None, None, None)
    assert [(level.price, level.size) for level in one_one.bids] == [(22.25, 900.0)]
    assert [(level.price, level.size) for level in one_one.asks] == [(22.8, 900.0)]
    assert one_one.source_ts_ns is None


def test_the_mmm_row_keeps_its_ask_and_its_zero_volume() -> None:
    mmm = _parse(_body(), MMM)["MMM@XSWX"]
    assert mmm.volume == 0.0
    assert mmm.prev_close == 157.0
    assert mmm.source_ts_ns is None


def test_a_listing_asked_twice_is_returned_once() -> None:
    snapshots = six.parse_quotes(_body(), [ABB, ABB], captured_ns=CAPTURED_NS)
    assert [snapshot.listing.symbol for snapshot in snapshots] == ["ABBN@XSWX"]


def test_missing_optional_columns_are_none_not_an_error() -> None:
    document = _document()
    for column in (
        "ShortName",
        "TradingBaseCurrency",
        "OpeningPrice",
        "LatestTradeDate",
        "LatestTradeTime",
        "TotalVolume",
    ):
        document["colNames"][document["colNames"].index(column)] = "Renamed" + column
    abb = _parse(_encoded(document), ABB)["ABBN@XSWX"]
    assert abb.last == 70.38
    assert (abb.name, abb.currency, abb.open, abb.volume, abb.source_ts_ns) == (
        None,
        None,
        None,
        None,
        None,
    )


def test_a_bad_trade_clock_or_a_fractional_date_drops_the_stamp() -> None:
    document = _document()
    _set(document, 4, "LatestTradeTime", 250_000)
    assert _parse(_encoded(document), ABB)["ABBN@XSWX"].source_ts_ns is None
    _set(document, 4, "LatestTradeTime", 92_020.829)
    _set(document, 4, "LatestTradeDate", 20261112.5)
    assert _parse(_encoded(document), ABB)["ABBN@XSWX"].source_ts_ns is None


def test_the_class_attributes_are_the_specified_ones() -> None:
    assert six.SOURCE == "six"
    assert six.SixLive.name == "six"
    assert six.SixLive.usage_class is UsageClass.DISPLAY
    assert six.SixLive.attribution == "Source: SIX Swiss Exchange (six-group.com)"
    assert six.SixLive.official is True
    assert six.SixLive.supported_mics == frozenset({"XSWX"})
    assert six.SixLive.max_tier is LiveTier.L1
    assert six.SixLive.depth_levels == 1
    assert six.SixLive.delay_s == 900
    assert six.SixLive.has_tape is False
    assert six.SixLive.batch_size == 2000
    assert six.SixLive.min_interval_s == 60.0


async def test_a_2xx_answer_that_is_not_the_document_is_a_payload_error() -> None:
    async def fetch(request: HttpRequest) -> HttpResponse:
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=b"[1, 2]",
            url=request.url,
        )

    with pytest.raises(LivePayloadError):
        await six.SixLive(fetch=fetch).snapshots([ABB], captured_ns=CAPTURED_NS)
