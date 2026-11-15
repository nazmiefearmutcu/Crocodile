"""``tencent``: the Tencent quote endpoint, read from a captured answer."""

from __future__ import annotations

from pathlib import Path

import pytest

from crocodile.core.schema.records import BookSnapshot, Quote, Trade
from crocodile.equity.universal.base import SourceUnservableError
from crocodile.equity.universal.http import HttpRequest, HttpResponse
from crocodile.equity.universal.live.base import LivePayloadError
from crocodile.equity.universal.live.model import (
    LiveListing,
    LiveSnapshot,
    LiveTier,
    snapshot_records,
)
from crocodile.equity.universal.live.sources import tencent
from crocodile.equity.universal.live.sources.tencent import QUOTE_URL, TencentLive

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live" / "tencent"
CAPTURED_NS = 1_794_471_000_000_000_000
MAOTAI = LiveListing(mic="XSHG", local_symbol="600519")
PINGAN = LiveListing(mic="XSHE", local_symbol="000001")
WANDA = LiveListing(mic="BJSE", local_symbol="920002")
TENCENT = LiveListing(mic="XHKG", local_symbol="700")
ALL_FOUR = [MAOTAI, PINGAN, WANDA, TENCENT]
MAOTAI_NAME = "\u8d35\u5dde\u8305\u53f0"
WANDA_NAME = "\u4e07\u8fbe\u8f74\u627f"
TENCENT_NAME = "\u817e\u8baf\u63a7\u80a1"


def _body() -> bytes:
    return (FIXTURES / "quotes.txt").read_bytes()


def _patched(old: str, new: str) -> bytes:
    text = _body().decode("gbk")
    assert text.count(old) == 1
    return text.replace(old, new).encode("gbk")


def _by_symbol(listings: list[LiveListing]) -> dict[str, LiveSnapshot]:
    snapshots = tencent.parse_quotes(_body(), listings, captured_ns=CAPTURED_NS)
    return {snapshot.listing.symbol: snapshot for snapshot in snapshots}


def test_quote_key_names_the_market() -> None:
    assert tencent.quote_key(MAOTAI) == "sh600519"
    assert tencent.quote_key(PINGAN) == "sz000001"
    assert tencent.quote_key(WANDA) == "bj920002"
    assert tencent.quote_key(TENCENT) == "hk00700"


def test_parses_a_five_level_mainland_book_in_shares() -> None:
    snapshots = _by_symbol(ALL_FOUR)
    maotai = snapshots[MAOTAI.symbol]
    assert maotai.tier is LiveTier.L2
    # Field 9/10 .. 17/18 are the bids, lots of 100 shares; field 10 is 17 lots = 1,700 shares.
    assert [(level.price, level.size) for level in maotai.bids] == [
        (1410.27, 1700.0),
        (1410.22, 100.0),
        (1410.21, 400.0),
        (1410.19, 400.0),
        (1410.00, 100.0),
    ]
    # Field 19/20 .. 27/28 are the asks.
    assert [(level.price, level.size) for level in maotai.asks] == [
        (1410.43, 200.0),
        (1410.45, 100.0),
        (1410.46, 100.0),
        (1410.50, 500.0),
        (1410.55, 100.0),
    ]
    assert maotai.last == 1410.27
    assert maotai.prev_close == 1402.30
    assert (maotai.open, maotai.high, maotai.low) == (1395.13, 1411.00, 1388.11)
    # Field 6 is 32373 lots: 3,237,300 shares.
    assert maotai.volume == 3_237_300.0
    # Field 37 is 453681 units of 10,000 CNY: 4,536,810,000 CNY.
    assert maotai.turnover == 4_536_810_000.0
    # Field 35 states the exact amount 4536814518; field 37 agrees to within rounding.
    assert abs(maotai.turnover - 4_536_814_518) < 10_000
    # Field 30 "20261112161452" is Asia/Shanghai wall-clock 16:14:52 on 2026-11-12.
    assert maotai.source_ts_ns == 1_794_471_292_000_000_000
    assert maotai.last_size is None
    assert maotai.prints == ()
    assert maotai.currency == "CNY"
    assert maotai.price_scale == 0
    assert maotai.delay_s is None
    assert maotai.name == MAOTAI_NAME
    assert maotai.symbol_raw == "sh600519"


def test_shenzhen_and_beijing_rows_are_read_the_same_way() -> None:
    snapshots = _by_symbol(ALL_FOUR)
    pingan = snapshots[PINGAN.symbol]
    assert pingan.tier is LiveTier.L2
    assert (pingan.bids[0].price, pingan.bids[0].size) == (11.79, 295_000.0)
    assert (pingan.asks[0].price, pingan.asks[0].size) == (11.80, 480_800.0)
    # Field 6 is 1082721 lots; field 37 is 127335 units of 10,000 CNY.
    assert pingan.volume == 108_272_100.0
    assert pingan.turnover == 1_273_350_000.0
    assert pingan.source_ts_ns == 1_794_471_300_000_000_000

    wanda = snapshots[WANDA.symbol]
    assert wanda.tier is LiveTier.L2
    assert [level.price for level in wanda.bids] == [48.50, 48.43, 48.41, 48.40, 48.38]
    assert [level.size for level in wanda.bids] == [5000.0, 500.0, 500.0, 2000.0, 400.0]
    assert [level.price for level in wanda.asks] == [48.68, 48.70, 48.73, 48.88, 48.98]
    assert wanda.volume == 1_496_900.0
    assert wanda.name == WANDA_NAME
    assert wanda.currency == "CNY"
    assert wanda.source_ts_ns == 1_794_468_752_000_000_000


def test_hong_kong_row_has_no_levels_and_is_tier_last() -> None:
    tencent_hk = _by_symbol(ALL_FOUR)[TENCENT.symbol]
    assert tencent_hk.tier is LiveTier.LAST
    assert tencent_hk.bids == () and tencent_hk.asks == ()
    assert tencent_hk.last == 397.6
    assert tencent_hk.prev_close == 409.6
    assert (tencent_hk.open, tencent_hk.high, tencent_hk.low) == (404.0, 405.8, 395.8)
    # Field 6 is already in shares; it is not multiplied.
    assert tencent_hk.volume == 22_514_201.0
    # Field 30 "2026/11/12 16:00:00" is Asia/Hong_Kong, i.e. 08:00:00 UTC.
    assert tencent_hk.source_ts_ns == 1_794_470_400_000_000_000
    assert tencent_hk.currency == "HKD"
    assert tencent_hk.price_scale == 0
    assert tencent_hk.last_size is None
    assert tencent_hk.name == TENCENT_NAME
    assert snapshot_records(tencent_hk) == []


def test_records_are_book_and_quote_with_no_trade() -> None:
    (snapshot,) = tencent.parse_quotes(_body(), [MAOTAI], captured_ns=CAPTURED_NS)
    records = snapshot_records(snapshot)
    assert [type(record) for record in records] == [BookSnapshot, Quote]
    book, quote = records
    assert isinstance(book, BookSnapshot) and isinstance(quote, Quote)
    assert book.symbol == MAOTAI.symbol and book.source == "tencent"
    assert book.depth == 5 and len(book.bids) == 5 and len(book.asks) == 5
    assert (quote.bid_px, quote.bid_sz) == (1410.27, 1700.0)
    assert (quote.ask_px, quote.ask_sz) == (1410.43, 200.0)
    assert not any(isinstance(record, Trade) for record in records)


def test_a_zero_bid_size_is_unknown_not_zero() -> None:
    body = _patched("~1410.27~17~1410.22~", "~1410.27~0~1410.22~")
    (snapshot,) = tencent.parse_quotes(body, [MAOTAI], captured_ns=CAPTURED_NS)
    assert snapshot.bids[0].price == 1410.27
    assert snapshot.bids[0].size is None
    assert not any(isinstance(record, Quote) for record in snapshot_records(snapshot))


def test_a_non_monotone_ladder_is_refused_whole() -> None:
    body = _patched("~1410.43~2~1410.45~1~", "~1410.45~1~1410.43~2~")
    (snapshot,) = tencent.parse_quotes(body, [MAOTAI], captured_ns=CAPTURED_NS)
    assert snapshot.asks == ()
    assert len(snapshot.bids) == 5


def test_an_unparseable_last_price_is_none_not_the_previous_close() -> None:
    body = _patched("~1410.27~1402.30~1395.13~", "~-~1402.30~1395.13~")
    (snapshot,) = tencent.parse_quotes(body, [MAOTAI], captured_ns=CAPTURED_NS)
    assert snapshot.last is None
    assert snapshot.prev_close == 1402.30
    assert not any(isinstance(record, Trade) for record in snapshot_records(snapshot))


def test_an_unparseable_stamp_is_none() -> None:
    body = _patched("~20261112161452~", "~2026111216145x~")
    (snapshot,) = tencent.parse_quotes(body, [MAOTAI], captured_ns=CAPTURED_NS)
    assert snapshot.source_ts_ns is None


def test_a_listing_that_was_not_asked_for_is_not_returned() -> None:
    snapshots = tencent.parse_quotes(_body(), [PINGAN], captured_ns=CAPTURED_NS)
    assert [snapshot.listing.symbol for snapshot in snapshots] == [PINGAN.symbol]
    assert tencent.parse_quotes(_body(), [], captured_ns=CAPTURED_NS) == []


def test_a_listing_the_answer_does_not_contain_is_absent() -> None:
    absent = LiveListing(mic="XSHG", local_symbol="600000")
    assert tencent.parse_quotes(_body(), [absent], captured_ns=CAPTURED_NS) == []


def test_an_unknown_code_answer_is_absent_not_an_error() -> None:
    body = b'v_pv_none_match="1";'
    assert tencent.parse_quotes(body, [MAOTAI], captured_ns=CAPTURED_NS) == []


def test_a_zero_open_price_is_none_not_zero() -> None:
    body = _patched("~1410.27~1402.30~1395.13~", "~1410.27~1402.30~0~")
    (snapshot,) = tencent.parse_quotes(body, [MAOTAI], captured_ns=CAPTURED_NS)
    assert snapshot.open is None
    assert snapshot.last == 1410.27
    assert snapshot.prev_close == 1402.30


def test_a_listing_outside_the_source_is_ignored_by_the_parser() -> None:
    nasdaq = LiveListing(mic="XNAS", local_symbol="AAPL")
    snapshots = tencent.parse_quotes(_body(), [nasdaq, PINGAN], captured_ns=CAPTURED_NS)
    assert [snapshot.listing.symbol for snapshot in snapshots] == [PINGAN.symbol]


def test_a_short_row_is_skipped() -> None:
    body = b'v_sh600519="1~a~600519~1410.27~1402.30";'
    assert tencent.parse_quotes(body, [MAOTAI], captured_ns=CAPTURED_NS) == []


def test_an_answer_without_statements_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        tencent.parse_quotes(b"<html>", [MAOTAI], captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        tencent.parse_quotes(b"", [MAOTAI], captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        tencent.parse_quotes(b'{"msgArray": []}', [MAOTAI], captured_ns=CAPTURED_NS)


async def test_an_html_body_through_the_source_is_a_payload_error() -> None:
    async def fetch(request: HttpRequest) -> HttpResponse:
        return HttpResponse(
            status=200, headers={"content-type": "text/html"}, body=b"<html></html>",
            url=request.url,
        )

    source = TencentLive(fetch=fetch)
    with pytest.raises(LivePayloadError):
        await source.snapshots([MAOTAI], captured_ns=CAPTURED_NS)
    await source.close()


async def test_a_wall_is_raised_and_not_worked_around() -> None:
    async def fetch(request: HttpRequest) -> HttpResponse:
        return HttpResponse(status=403, headers={}, body=b"", url=request.url)

    source = TencentLive(fetch=fetch)
    with pytest.raises(SourceUnservableError):
        await source.snapshots([MAOTAI], captured_ns=CAPTURED_NS)
    await source.close()


async def test_the_source_sends_one_batched_get_and_parses_it() -> None:
    requested: list[HttpRequest] = []

    async def fetch(request: HttpRequest) -> HttpResponse:
        requested.append(request)
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=_body(),
            url=request.url,
        )

    source = TencentLive(fetch=fetch)
    snapshots = await source.snapshots(
        [*ALL_FOUR, LiveListing(mic="XNAS", local_symbol="AAPL")], captured_ns=CAPTURED_NS
    )
    (request,) = requested
    assert request.method == "GET"
    assert request.url == QUOTE_URL == "https://qt.gtimg.cn/"
    assert request.params == (("q", "sh600519,sz000001,bj920002,hk00700"),)
    assert sorted(snapshot.listing.symbol for snapshot in snapshots) == sorted(
        listing.symbol for listing in ALL_FOUR
    )
    await source.close()


async def test_the_source_sends_nothing_for_unsupported_listings() -> None:
    requested: list[HttpRequest] = []

    async def fetch(request: HttpRequest) -> HttpResponse:
        requested.append(request)
        return HttpResponse(status=200, headers={}, body=_body(), url=request.url)

    source = TencentLive(fetch=fetch)
    snapshots = await source.snapshots(
        [LiveListing(mic="XNAS", local_symbol="AAPL")], captured_ns=CAPTURED_NS
    )
    assert snapshots == []
    assert requested == []
    await source.close()
