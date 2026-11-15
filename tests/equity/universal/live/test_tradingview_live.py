"""``tradingview``: the scanner's figures for many listings, from captured answers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from crocodile.equity.universal.base import SourceUnservableError, UnservableReason
from crocodile.equity.universal.http import HttpRequest, HttpResponse
from crocodile.equity.universal.live.base import LivePayloadError
from crocodile.equity.universal.live.model import LiveListing, LiveTier, snapshot_records
from crocodile.equity.universal.live.sources import tradingview
from crocodile.equity.universal.live.sources.tradingview import (
    COLUMNS,
    MARKET_SCAN_URL,
    SCAN_URL,
    TradingViewLive,
    board_body,
    delay_of,
    parse_board,
    parse_scan,
    scan_body,
    ticker_of,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live" / "tradingview"
CAPTURED_NS = 1_794_471_000_000_000_000
THYAO = LiveListing(mic="XIST", local_symbol="THYAO")
VOD = LiveListing(mic="XLON", local_symbol="VOD")
TOYOTA = LiveListing(mic="XJPX", local_symbol="7203")
SAP = LiveListing(mic="XETR", local_symbol="SAP")
DANGCEM = LiveListing(mic="XNSA", local_symbol="DANGCEM")
LISTINGS = [THYAO, VOD, TOYOTA, SAP, DANGCEM]
TICKERS = ["BIST:THYAO", "LSE:VOD", "TSE:7203", "XETR:SAP", "NSENG:DANGCEM"]
NO_TABLE = LiveListing(mic="AQSE", local_symbol="ABC")

_EXPECTED_COLUMNS = [
    "name",
    "description",
    "currency",
    "close",
    "open",
    "high",
    "low",
    "volume",
    "change",
    "update_mode",
    "Value.Traded",
    "isin",
]


def _scan() -> bytes:
    return (FIXTURES / "scan_tickers.json").read_bytes()


def _board() -> bytes:
    return (FIXTURES / "board_turkey.json").read_bytes()


class FakeFetch:
    """Answers each request with the next ``(status, body)`` reply and records the request."""

    def __init__(self, *replies: tuple[int, bytes]) -> None:
        self.requests: list[HttpRequest] = []
        self._replies = list(replies)

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        status, body = self._replies.pop(0)
        return HttpResponse(
            status=status,
            headers={"content-type": "application/json"},
            body=body,
            url=request.url,
        )


def _by_symbol(snapshots: list[Any]) -> dict[str, Any]:
    return {snapshot.listing.symbol: snapshot for snapshot in snapshots}


# --- spelling and delay ----------------------------------------------------------------------


def test_ticker_of_spells_the_prefix_code() -> None:
    assert ticker_of(THYAO) == "BIST:THYAO"
    assert ticker_of(VOD) == "LSE:VOD"
    assert ticker_of(TOYOTA) == "TSE:7203"
    assert ticker_of(SAP) == "XETR:SAP"
    assert ticker_of(DANGCEM) == "NSENG:DANGCEM"


def test_ticker_of_a_market_without_a_row_is_none() -> None:
    assert ticker_of(NO_TABLE) is None
    assert ticker_of(LiveListing(mic="XXXX", local_symbol="THYAO")) is None


def test_delay_of_each_update_mode() -> None:
    assert delay_of("streaming") == 0
    assert delay_of("delayed_streaming_900") == 900
    assert delay_of("delayed_streaming_1800") == 1800
    assert delay_of("endofday") is None
    assert delay_of("junk") is None
    assert delay_of(None) is None
    assert delay_of(900) is None


def test_scan_body_asks_for_the_twelve_columns_of_the_tickers() -> None:
    assert scan_body(["BIST:THYAO", "LSE:VOD"]) == {
        "columns": _EXPECTED_COLUMNS,
        "symbols": {"tickers": ["BIST:THYAO", "LSE:VOD"]},
    }
    assert list(COLUMNS) == _EXPECTED_COLUMNS


def test_board_body_filters_one_exchange_and_sorts_by_volume() -> None:
    assert board_body(["BIST"], 5) == {
        "columns": _EXPECTED_COLUMNS,
        "filter": [{"left": "exchange", "operation": "in_range", "right": ["BIST"]}],
        "sort": {"sortBy": "volume", "sortOrder": "desc"},
        "range": [0, 5],
    }


# --- parse_scan ------------------------------------------------------------------------------


def test_scan_rows_map_to_snapshots_with_concrete_values() -> None:
    snapshots = _by_symbol(parse_scan(_scan(), LISTINGS, captured_ns=CAPTURED_NS))
    assert sorted(snapshots) == [
        "7203@XJPX",
        "DANGCEM@XNSA",
        "SAP@XETR",
        "THYAO@XIST",
        "VOD@XLON",
    ]

    thyao = snapshots["THYAO@XIST"]
    assert thyao.source == "tradingview"
    assert thyao.symbol_raw == "BIST:THYAO"
    assert thyao.captured_ns == CAPTURED_NS
    assert thyao.last == 317.5
    assert thyao.open == 315.5
    assert thyao.high == 320.0
    assert thyao.low == 315.5
    assert thyao.volume == 10_324_743.0
    assert thyao.turnover == 63_955_846.157775
    assert thyao.prev_close == 314.75
    assert thyao.currency == "TRY"
    assert thyao.price_scale == 0
    assert thyao.delay_s == 900
    assert thyao.name == "Turk Hava Yollari A.O."
    assert thyao.tier is LiveTier.LAST

    vod = snapshots["VOD@XLON"]
    assert vod.last == 118.45
    assert vod.open == 117.4
    assert vod.high == 118.7
    assert vod.low == 117.2
    assert vod.volume == 2_904_977.0
    assert vod.turnover == 4_590_565.06669665
    assert vod.prev_close == 117.35
    assert vod.currency == "GBP"
    assert vod.price_scale == 2
    assert vod.delay_s == 0
    assert vod.name == "Vodafone Group Public Limited Company"

    toyota = snapshots["7203@XJPX"]
    assert toyota.last == 3408.0
    assert toyota.prev_close == 3435.0
    assert toyota.open == 3365.0
    assert toyota.high == 3433.0
    assert toyota.low == 3363.0
    assert toyota.volume == 19_336_700.0
    assert toyota.currency == "JPY"
    assert toyota.price_scale == 0
    assert toyota.delay_s == 1200
    assert toyota.name == "Toyota Motor Corp."

    sap = snapshots["SAP@XETR"]
    assert sap.last == 212.0
    assert sap.prev_close == 210.95
    assert sap.currency == "EUR"
    assert sap.delay_s == 900

    dangcem = snapshots["DANGCEM@XNSA"]
    assert dangcem.last == 1469.9
    assert dangcem.prev_close == 1469.9
    assert dangcem.volume == 348_957.0
    assert dangcem.currency == "NGN"
    assert dangcem.delay_s == 1800


def test_the_gbx_quote_unit_is_gbp_at_scale_two() -> None:
    (vod,) = parse_scan(_scan(), [VOD], captured_ns=CAPTURED_NS)
    assert vod.currency == "GBP"
    assert vod.price_scale == 2
    assert vod.last == 118.45


def test_a_row_for_a_ticker_that_was_not_requested_is_ignored() -> None:
    snapshots = parse_scan(_scan(), [THYAO], captured_ns=CAPTURED_NS)
    assert [s.listing.symbol for s in snapshots] == ["THYAO@XIST"]


def test_a_row_with_the_wrong_number_of_columns_is_ignored() -> None:
    doc = json.loads(_scan())
    doc["data"][0]["d"].pop()
    snapshots = parse_scan(json.dumps(doc).encode(), LISTINGS, captured_ns=CAPTURED_NS)
    symbols = [s.listing.symbol for s in snapshots]
    assert "THYAO@XIST" not in symbols
    assert len(symbols) == 4


def test_a_valid_isin_is_copied_onto_a_listing_that_had_none() -> None:
    (thyao,) = parse_scan(_scan(), [THYAO], captured_ns=CAPTURED_NS)
    assert thyao.listing.isin == "TRATHYAO91M5"
    assert thyao.listing.symbol == "THYAO@XIST"


def test_an_invalid_isin_is_not_copied() -> None:
    body = _scan()
    assert body.count(b'"TRATHYAO91M5"') == 1
    edited = body.replace(b'"TRATHYAO91M5"', b'"TRATHYAO91M4"')
    (thyao,) = parse_scan(edited, [THYAO], captured_ns=CAPTURED_NS)
    assert thyao.listing.isin is None


def test_a_listing_that_already_has_an_isin_keeps_it() -> None:
    requested = LiveListing(mic="XLON", local_symbol="VOD", isin="DE0007164600")
    (vod,) = parse_scan(_scan(), [requested], captured_ns=CAPTURED_NS)
    assert vod.listing.isin == "DE0007164600"


def test_an_empty_scan_is_no_snapshots() -> None:
    assert parse_scan(b'{"totalCount":0}', LISTINGS, captured_ns=CAPTURED_NS) == []
    assert parse_scan(b'{"totalCount":0,"data":[]}', LISTINGS, captured_ns=CAPTURED_NS) == []


def test_a_scan_without_a_data_array_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        parse_scan(b'{"totalCount":3}', LISTINGS, captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        parse_scan(b"[]", LISTINGS, captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        parse_scan(b"<html>", LISTINGS, captured_ns=CAPTURED_NS)


def test_scan_snapshots_make_no_records_without_a_sized_last_print() -> None:
    snapshots = parse_scan(_scan(), LISTINGS, captured_ns=CAPTURED_NS)
    assert snapshots
    for snapshot in snapshots:
        assert snapshot_records(snapshot) == []


# --- parse_board -----------------------------------------------------------------------------


def test_board_rows_of_the_requested_market() -> None:
    snapshots = parse_board(_board(), "XIST", captured_ns=CAPTURED_NS)
    assert [s.listing.local_symbol for s in snapshots] == [
        "SASA",
        "KTLEV",
        "PEKGY",
        "INFO",
        "ISCTR",
    ]
    assert {s.listing.mic for s in snapshots} == {"XIST"}
    sasa = snapshots[0]
    assert sasa.symbol_raw == "BIST:SASA"
    assert sasa.last == 1.61
    assert sasa.open == 1.56
    assert sasa.high == 1.61
    assert sasa.low == 1.56
    assert sasa.volume == 425_211_071.0
    assert sasa.prev_close == 1.56
    assert sasa.currency == "TRY"
    assert sasa.delay_s == 900
    assert sasa.listing.isin == "TRASASAW91E4"
    assert sasa.name == "Sasa Polyester Sanayi A.S."


def test_a_board_keeps_only_the_rows_of_the_requested_mic() -> None:
    doc = json.loads(_board())
    lse_row = json.loads(_scan())["data"][1]
    doc["data"].append(lse_row)
    doc["data"].append({"s": "NOPE:XYZ", "d": ["XYZ"] * len(COLUMNS)})
    body = json.dumps(doc).encode()
    assert len(parse_board(body, "XIST", captured_ns=CAPTURED_NS)) == 5
    (vod,) = parse_board(body, "XLON", captured_ns=CAPTURED_NS)
    assert vod.listing.symbol == "VOD@XLON"
    assert vod.listing.isin == "GB00BH4HKS39"
    assert parse_board(body, "XNSA", captured_ns=CAPTURED_NS) == []


def test_a_board_without_a_data_array_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        parse_board(b'{"foo":1}', "XIST", captured_ns=CAPTURED_NS)


def test_an_empty_board_is_no_snapshots() -> None:
    assert parse_board(b'{"totalCount":0}', "XIST", captured_ns=CAPTURED_NS) == []


# --- the source with a fake fetcher ----------------------------------------------------------


async def test_many_listings_are_one_post() -> None:
    fetch = FakeFetch((200, _scan()))
    source = TradingViewLive(fetch=fetch)
    snapshots = await source.snapshots(LISTINGS, captured_ns=CAPTURED_NS)
    assert len(snapshots) == 5
    (request,) = fetch.requests
    assert request.method == "POST"
    assert request.url == SCAN_URL == "https://scanner.tradingview.com/global/scan"
    assert request.json_body == scan_body(TICKERS)
    assert request.params == ()
    assert request.form == ()


async def test_listings_without_a_ticker_make_no_request() -> None:
    fetch = FakeFetch()
    source = TradingViewLive(fetch=fetch)
    assert await source.snapshots([NO_TABLE], captured_ns=CAPTURED_NS) == []
    assert fetch.requests == []


async def test_board_is_one_post_to_the_market_scan() -> None:
    fetch = FakeFetch((200, _board()))
    source = TradingViewLive(fetch=fetch)
    snapshots = await source.board("XIST", captured_ns=CAPTURED_NS, limit=5)
    assert len(snapshots) == 5
    (request,) = fetch.requests
    assert request.method == "POST"
    assert request.url == MARKET_SCAN_URL.format(market="turkey")
    assert request.url == "https://scanner.tradingview.com/turkey/scan"
    assert request.json_body == board_body(["BIST"], 5)


async def test_board_defaults_to_one_hundred_fifty_rows() -> None:
    fetch = FakeFetch((200, _board()))
    source = TradingViewLive(fetch=fetch)
    await source.board("XIST", captured_ns=CAPTURED_NS)
    (request,) = fetch.requests
    assert request.json_body == board_body(["BIST"], 150)
    assert request.json_body is not None and request.json_body["range"] == [0, 150]


async def test_board_of_a_market_without_a_grammar_row_makes_no_request() -> None:
    fetch = FakeFetch()
    source = TradingViewLive(fetch=fetch)
    assert await source.board("XXXX", captured_ns=CAPTURED_NS) == []
    assert fetch.requests == []


async def test_a_forbidden_scan_is_a_wall_and_is_not_retried() -> None:
    fetch = FakeFetch((403, b"{}"))
    source = TradingViewLive(fetch=fetch)
    with pytest.raises(SourceUnservableError) as caught:
        await source.snapshots(LISTINGS, captured_ns=CAPTURED_NS)
    assert caught.value.reason is UnservableReason.FORBIDDEN
    assert len(fetch.requests) == 1


async def test_a_server_error_is_a_payload_error() -> None:
    source = TradingViewLive(fetch=FakeFetch((500, b"{}")))
    with pytest.raises(LivePayloadError):
        await source.snapshots(LISTINGS, captured_ns=CAPTURED_NS)


async def test_a_board_with_an_html_answer_is_a_bot_challenge_wall() -> None:
    source = TradingViewLive(fetch=FakeFetch((200, b"<html>captcha</html>")))
    with pytest.raises(SourceUnservableError) as caught:
        await source.board("XIST", captured_ns=CAPTURED_NS)
    assert caught.value.reason is UnservableReason.BOT_CHALLENGE


def test_serves_follows_the_prefix_grammar() -> None:
    source = TradingViewLive(fetch=FakeFetch())
    assert source.serves(THYAO)
    assert not source.serves(NO_TABLE)
    assert TradingViewLive.enumerates is True


def test_tradingview_module_exports() -> None:
    assert tradingview.HOST == "scanner.tradingview.com"
    assert tradingview.SOURCE == "tradingview"
