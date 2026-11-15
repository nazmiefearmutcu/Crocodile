"""``yahoo_chart``: the chart meta block and today's one-minute bars, from a captured answer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.grammar import load_grammar
from crocodile.equity.universal.base import SourceUnservableError, UnservableReason
from crocodile.equity.universal.http import HttpRequest, HttpResponse
from crocodile.equity.universal.live.base import LivePayloadError
from crocodile.equity.universal.live.model import LiveListing, LiveTier, snapshot_records
from crocodile.equity.universal.live.sources import yahoo_chart
from crocodile.equity.universal.live.sources.yahoo_chart import (
    CHART_URL,
    MinuteBar,
    YahooChartLive,
    extra_suffixes,
    parse_live_chart,
    parse_minute_bars,
    yahoo_symbol,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live" / "yahoo_chart"
CHART = FIXTURES / "chart_1m_THYAO_IS.json"
CAPTURED_NS = 1_794_471_000_000_000_000
THYAO = LiveListing(mic="XIST", local_symbol="THYAO")
KFH = LiveListing(mic="XKUW", local_symbol="KFH")
AAPL = LiveListing(mic="XNAS", local_symbol="AAPL")
NO_TABLE = LiveListing(mic="AQSE", local_symbol="ABC")
THYAO_NAME = "T" + chr(0xFC) + "rk Hava Yollari Anonim Ortakligi"

_EXTRA_MICS = frozenset(
    {
        "BVCA",
        "CHIA",
        "DSMD",
        "NEOE",
        "XCNQ",
        "XDFM",
        "XHAN",
        "XICE",
        "XKUW",
        "XLIT",
        "XLUX",
        "XRIS",
        "XSAP",
        "XTAL",
        "XTSX",
    }
)


def _body() -> bytes:
    return CHART.read_bytes()


def _edited(old: bytes, new: bytes) -> bytes:
    body = _body()
    assert body.count(old) == 1, old
    return body.replace(old, new)


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


# --- symbol spelling -------------------------------------------------------------------------


def test_grammar_spells_thyao_with_the_is_suffix() -> None:
    assert yahoo_symbol(THYAO) == "THYAO.IS"


def test_a_us_listing_has_no_suffix() -> None:
    assert yahoo_symbol(AAPL) == "AAPL"


def test_extra_table_spells_kfh_with_the_kw_suffix() -> None:
    assert yahoo_symbol(KFH) == "KFH.KW"


def test_a_market_in_neither_table_has_no_symbol() -> None:
    assert yahoo_symbol(NO_TABLE) is None


def test_extra_suffix_rows_are_dotted_and_allow_listed() -> None:
    table = extra_suffixes()
    assert set(table) == _EXTRA_MICS
    assert len(table) == 15
    registry = ExchangeRegistry()
    for mic, suffix in table.items():
        assert suffix.startswith("."), mic
        registry.get(mic)


def test_extra_mics_are_not_already_in_the_yahoo_grammar() -> None:
    overlap = _EXTRA_MICS & load_grammar("yahoo_chart").supported_mics()
    assert overlap == frozenset()


def test_supported_mics_is_the_grammar_plus_the_extra_table() -> None:
    grammar = load_grammar("yahoo_chart").supported_mics()
    supported = YahooChartLive.supported_mics
    assert supported == grammar | _EXTRA_MICS
    assert "XIST" in supported and "XKUW" in supported
    assert "AQSE" not in supported


def test_serves_follows_the_symbol_spelling() -> None:
    source = YahooChartLive(fetch=FakeFetch())
    assert source.serves(THYAO)
    assert source.serves(KFH)
    assert not source.serves(NO_TABLE)


# --- the live snapshot -----------------------------------------------------------------------


def test_meta_block_maps_to_the_snapshot_fields() -> None:
    snapshot = parse_live_chart(_body(), THYAO, symbol_raw="THYAO.IS", captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.source == "yahoo_chart"
    assert snapshot.symbol_raw == "THYAO.IS"
    assert snapshot.captured_ns == CAPTURED_NS
    assert snapshot.listing == THYAO
    assert snapshot.last == 318.5
    assert snapshot.last_size is None
    assert snapshot.prev_close == 314.75
    assert snapshot.high == 320.0
    assert snapshot.low == 315.5
    assert snapshot.volume == 7_986_220.0
    assert snapshot.source_ts_ns == 1_794_470_434_000_000_000
    assert snapshot.currency == "TRY"
    assert snapshot.price_scale == 0
    assert snapshot.name == THYAO_NAME
    assert snapshot.delay_s is None
    assert snapshot.turnover is None
    assert snapshot.tier is LiveTier.LAST


def test_open_is_the_first_bar_when_it_starts_inside_the_session() -> None:
    snapshot = parse_live_chart(_body(), THYAO, symbol_raw="THYAO.IS", captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.open == 315.5


def test_open_is_unset_when_the_first_bar_precedes_the_session() -> None:
    # The regular session is moved to start after the first bar, so that bar is not today's.
    body = _edited(
        b'"regular":{"timezone":"TRT","start":1794465000',
        b'"regular":{"timezone":"TRT","start":1794466600',
    )
    snapshot = parse_live_chart(body, THYAO, symbol_raw="THYAO.IS", captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.open is None
    assert snapshot.last == 318.5


def test_gbp_pence_unit_is_gbp_at_scale_two() -> None:
    body = _edited(b'"currency":"TRY"', b'"currency":"GBp"')
    snapshot = parse_live_chart(body, THYAO, symbol_raw="VOD.L", captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.currency == "GBP"
    assert snapshot.price_scale == 2


def test_an_unknown_currency_spelling_leaves_both_unset() -> None:
    body = _edited(b'"currency":"TRY"', b'"currency":"XYZ"')
    snapshot = parse_live_chart(body, THYAO, symbol_raw="THYAO.IS", captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot.currency is None
    assert snapshot.price_scale is None
    assert snapshot.last == 318.5


def test_a_null_result_is_no_symbol() -> None:
    body = json.dumps(
        {"chart": {"result": None, "error": {"code": "Not Found", "description": "no data"}}}
    ).encode()
    assert parse_live_chart(body, THYAO, symbol_raw="THYAO.IS", captured_ns=CAPTURED_NS) is None


def test_a_body_without_a_chart_object_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        parse_live_chart(b'{"finance":{}}', THYAO, symbol_raw="THYAO.IS", captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        parse_live_chart(b"<html>", THYAO, symbol_raw="THYAO.IS", captured_ns=CAPTURED_NS)


def test_a_snapshot_with_a_last_price_but_no_size_makes_no_trade() -> None:
    snapshot = parse_live_chart(_body(), THYAO, symbol_raw="THYAO.IS", captured_ns=CAPTURED_NS)
    assert snapshot is not None
    assert snapshot_records(snapshot) == []


# --- minute bars -----------------------------------------------------------------------------


def test_minute_bars_count_and_first_and_last() -> None:
    bars = parse_minute_bars(_body())
    assert len(bars) == 62
    assert bars[0] == MinuteBar(
        ts_ns=1_794_466_500_000_000_000,
        open=315.5,
        high=315.5,
        low=315.5,
        close=315.5,
        volume=0.0,
    )
    assert bars[-1] == MinuteBar(
        ts_ns=1_794_470_434_000_000_000,
        open=318.5,
        high=318.5,
        low=318.5,
        close=318.5,
        volume=0.0,
    )


def test_bars_with_a_null_cell_are_dropped_and_order_is_kept() -> None:
    bars = parse_minute_bars(_body())
    # The fixture's index 1 to 4 and 65 carry nulls; the bar after the gap is index 5.
    assert bars[1].ts_ns == 1_794_466_800_000_000_000
    stamps = [bar.ts_ns for bar in bars]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)


def test_a_null_result_has_no_bars() -> None:
    body = json.dumps({"chart": {"result": None, "error": {"code": "x"}}}).encode()
    assert parse_minute_bars(body) == []


# --- the source with a fake fetcher ----------------------------------------------------------


async def test_snapshots_request_shape_and_result() -> None:
    fetch = FakeFetch((200, _body()))
    source = YahooChartLive(fetch=fetch)
    snapshots = await source.snapshots([THYAO], captured_ns=CAPTURED_NS)
    assert [s.listing.symbol for s in snapshots] == ["THYAO@XIST"]
    assert snapshots[0].last == 318.5
    (request,) = fetch.requests
    assert request.method == "GET"
    assert request.url == CHART_URL.format(symbol="THYAO.IS")
    assert request.url == "https://query2.finance.yahoo.com/v8/finance/chart/THYAO.IS"
    assert request.params == (("interval", "1m"), ("range", "1d"))
    assert request.json_body is None
    assert request.form == ()


async def test_a_404_makes_the_listing_absent() -> None:
    fetch = FakeFetch((404, b'{"chart":{"result":null,"error":{"code":"Not Found"}}}'))
    source = YahooChartLive(fetch=fetch)
    assert await source.snapshots([THYAO], captured_ns=CAPTURED_NS) == []
    assert len(fetch.requests) == 1


async def test_a_listing_without_a_symbol_makes_no_request() -> None:
    fetch = FakeFetch()
    source = YahooChartLive(fetch=fetch)
    assert await source.snapshots([NO_TABLE], captured_ns=CAPTURED_NS) == []
    assert await source.minute_bars(NO_TABLE) == []
    assert fetch.requests == []


async def test_a_server_error_is_a_payload_error_not_an_absent_listing() -> None:
    source = YahooChartLive(fetch=FakeFetch((500, b"{}")))
    with pytest.raises(LivePayloadError):
        await source.snapshots([THYAO], captured_ns=CAPTURED_NS)


async def test_a_forbidden_answer_is_a_wall_and_is_not_retried() -> None:
    fetch = FakeFetch((403, b"{}"))
    source = YahooChartLive(fetch=fetch)
    with pytest.raises(SourceUnservableError) as caught:
        await source.snapshots([THYAO], captured_ns=CAPTURED_NS)
    assert caught.value.reason is UnservableReason.FORBIDDEN
    assert len(fetch.requests) == 1


async def test_a_html_page_is_a_bot_challenge_wall() -> None:
    fetch = FakeFetch((200, b"<!doctype html><html>consent</html>"))
    source = YahooChartLive(fetch=fetch)
    with pytest.raises(SourceUnservableError) as caught:
        await source.snapshots([THYAO], captured_ns=CAPTURED_NS)
    assert caught.value.reason is UnservableReason.BOT_CHALLENGE
    assert len(fetch.requests) == 1


async def test_minute_bars_of_a_listing() -> None:
    fetch = FakeFetch((200, _body()))
    source = YahooChartLive(fetch=fetch)
    bars = await source.minute_bars(THYAO)
    assert len(bars) == 62
    assert bars[0].open == 315.5
    (request,) = fetch.requests
    assert request.params == (("interval", "1m"), ("range", "1d"))


async def test_minute_bars_of_a_listing_the_chart_does_not_know_are_empty() -> None:
    fetch = FakeFetch((404, b"{}"))
    source = YahooChartLive(fetch=fetch)
    assert await source.minute_bars(THYAO) == []


async def test_the_snapshot_from_the_source_is_last_tier_with_no_records() -> None:
    fetch = FakeFetch((200, _body()))
    source = YahooChartLive(fetch=fetch)
    (snapshot,) = await source.snapshots([THYAO], captured_ns=CAPTURED_NS)
    assert snapshot.source == yahoo_chart.SOURCE == "yahoo_chart"
    assert snapshot.tier is LiveTier.LAST
    assert snapshot.prev_close == 314.75
    assert snapshot_records(snapshot) == []
