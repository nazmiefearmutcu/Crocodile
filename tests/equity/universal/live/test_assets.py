"""``assets``: futures, forex, index, bond and option rows of the scanner, from captured answers."""

from __future__ import annotations

import csv
import importlib.resources
import io
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from crocodile.core.ratelimit import reset_budgets
from crocodile.core.schema.enums import AssetClass, OptType
from crocodile.core.schema.provenance import Provenance
from crocodile.core.schema.records import DerivativeTicker, IndexValue, OpenInterest
from crocodile.equity.universal.base import SourceUnservableError, UnservableReason
from crocodile.equity.universal.http import HttpRequest, HttpResponse
from crocodile.equity.universal.live.assets import (
    COLUMNS,
    OPTION_CHAIN_LIMIT,
    OPTION_COLUMNS,
    OPTION_SERIES_COLUMNS,
    SOURCE,
    AssetFeed,
    AssetKind,
    AssetQuote,
    OptionSeries,
    asset_records,
    board_body,
    known_venues,
    option_body,
    parse_option_chain,
    parse_option_series,
    parse_rows,
    series_body,
    split_ticker,
    yahoo_symbol_for,
)
from crocodile.equity.universal.live.base import LivePayloadError
from crocodile.equity.universal.live.sources.yahoo_chart import MinuteBar

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live"
ASSETS = FIXTURES / "assets"
YAHOO_CHART = FIXTURES / "yahoo_chart" / "chart_1m_THYAO_IS.json"
CAPTURED_NS = 1_794_471_000_000_000_000
NS_PER_SECOND = 1_000_000_000
SCAN_BASE = "https://scanner.tradingview.com"
BOARD_FILES = (
    "board_futures_eurex.json",
    "board_futures_bist.json",
    "board_forex.json",
    "board_index.json",
    "board_bonds.json",
)


@pytest.fixture(autouse=True)
def _fresh_budgets() -> None:
    reset_budgets()


def _fixture(name: str) -> bytes:
    return (ASSETS / name).read_bytes()


def _doc(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_fixture(name))
    return loaded


def _trimmed(name: str) -> bytes:
    """A capture that was cut to a few rows, restated as the whole answer it is tested as.

    ``options_thyao.json`` and ``options_aapl.json`` keep the scanner's own ``totalCount`` (94
    and 3396) over 8 and 6 rows: as captured they are partial answers, which is what the
    refusal tests read them as.
    """
    doc = _doc(name)
    doc["totalCount"] = len(doc["data"])
    return json.dumps(doc).encode()


def _by_ticker(quotes: list[AssetQuote]) -> dict[str, AssetQuote]:
    return {quote.ticker: quote for quote in quotes}


def _utc_ns(day: date) -> int:
    """Epoch nanoseconds of the UTC midnight that opens ``day``."""
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * NS_PER_SECOND


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


def _feed(fetch: FakeFetch) -> AssetFeed:
    return AssetFeed(fetch=fetch, clock_ns=lambda: CAPTURED_NS)


# --- split_ticker ----------------------------------------------------------------------------


def test_split_ticker_reads_the_venue_and_the_symbol() -> None:
    assert split_ticker("CME_MINI:ES1!") == ("CME_MINI", "ES1!")
    assert split_ticker("FX_IDC:EURUSD") == ("FX_IDC", "EURUSD")
    assert split_ticker("SP:SPX") == ("SP", "SPX")
    assert split_ticker("BIST:XU100") == ("BIST", "XU100")


def test_split_ticker_refuses_anything_that_is_not_venue_colon_symbol() -> None:
    with pytest.raises(ValueError):
        split_ticker("ES1!")
    with pytest.raises(ValueError):
        split_ticker("")
    with pytest.raises(ValueError):
        split_ticker("cme:ES")


# --- parse_rows: the boards ------------------------------------------------------------------


def test_eurex_futures_board_rows() -> None:
    quotes = parse_rows(_fixture("board_futures_eurex.json"), captured_ns=CAPTURED_NS)
    assert [quote.ticker for quote in quotes] == [
        "EUREX:FGBLZ2026",
        "EUREX:FGBL1!",
        "EUREX:FGBM1!",
        "EUREX:FGBMZ2026",
        "EUREX:FGBSZ2026",
    ]
    assert {quote.kind for quote in quotes} == {AssetKind.FUTURE}

    dated = quotes[0]
    assert (dated.venue, dated.symbol) == ("EUREX", "FGBLZ2026")
    assert dated.name == "Euro-Bund Futures (Dec 2026)"
    assert dated.captured_ns == CAPTURED_NS
    assert dated.last == 122.22
    assert dated.bid is None and dated.ask is None
    assert (dated.open, dated.high, dated.low) == (122.64, 122.76, 122.2)
    assert dated.volume == 962_283.0
    assert dated.open_interest == 1_559_932.0
    assert dated.expiry == date(2026, 12, 8)
    assert dated.root == "EUREX:FGBL"
    assert (dated.currency, dated.price_scale) == ("EUR", 0)
    assert dated.delay_s == 900
    assert dated.coupon is None and dated.maturity is None

    continuous = quotes[1]
    assert continuous.symbol == "FGBL1!"
    assert continuous.expiry is None
    assert continuous.open_interest == 1_559_932.0

    assert quotes[2].last == 113.02 and quotes[2].root == "EUREX:FGBM"
    assert quotes[4].expiry == date(2026, 12, 8)
    assert quotes[4].open_interest == 2_936_955.0


def test_bist_futures_board_rows() -> None:
    quotes = _by_ticker(parse_rows(_fixture("board_futures_bist.json"), captured_ns=CAPTURED_NS))
    assert list(quotes) == [
        "BIST:SASA1!",
        "BIST:SASAX2026",
        "BIST:ISCTR1!",
        "BIST:ISCTRX2026",
        "BIST:HEKTS1!",
    ]
    sasa = quotes["BIST:SASA1!"]
    assert sasa.kind is AssetKind.FUTURE
    assert sasa.name == "SASA POLYESTER SANAYI FUTURES"
    assert sasa.last == 1.61
    assert (sasa.open, sasa.high, sasa.low) == (1.58, 1.66, 1.57)
    assert sasa.volume == 3_962_816.0
    assert sasa.open_interest == 6_320_111.0
    assert sasa.expiry is None
    assert sasa.root == "BIST:SASA"
    assert (sasa.currency, sasa.price_scale) == ("TRY", 0)
    assert sasa.delay_s == 900

    dated = quotes["BIST:SASAX2026"]
    assert dated.expiry == date(2026, 11, 30)
    assert dated.open_interest == 6_320_111.0

    assert quotes["BIST:ISCTR1!"].last == 13.81
    assert quotes["BIST:ISCTRX2026"].expiry == date(2026, 11, 30)
    hekts = quotes["BIST:HEKTS1!"]
    assert hekts.last == 1.73
    assert hekts.open_interest == 705_692.0


def test_forex_board_rows_carry_no_book_size_and_no_expiry() -> None:
    quotes = parse_rows(_fixture("board_forex.json"), captured_ns=CAPTURED_NS)
    assert [quote.ticker for quote in quotes] == [
        "FX_IDC:UYUJPY",
        "FX_IDC:PENSEK",
        "FX_IDC:UYUSEK",
        "FX_IDC:MXNSEK",
        "FX_IDC:CNYSEK",
    ]
    assert {quote.kind for quote in quotes} == {AssetKind.FOREX}
    uyujpy = quotes[0]
    assert uyujpy.venue == "FX_IDC" and uyujpy.symbol == "UYUJPY"
    assert uyujpy.last == 3.93701
    assert uyujpy.bid is None and uyujpy.ask is None
    assert (uyujpy.open, uyujpy.high, uyujpy.low) == (3.93748, 3.94463, 3.92592)
    assert uyujpy.volume == 1_767_279.0
    assert uyujpy.open_interest is None and uyujpy.expiry is None
    assert (uyujpy.currency, uyujpy.price_scale) == ("JPY", 0)
    assert uyujpy.delay_s == 0

    pensek = quotes[1]
    assert pensek.last == 2.9489334
    assert pensek.currency == "SEK"


def test_index_board_rows() -> None:
    quotes = _by_ticker(parse_rows(_fixture("board_index.json"), captured_ns=CAPTURED_NS))
    assert len(quotes) == 8
    assert {quote.kind for quote in quotes.values()} == {AssetKind.INDEX}

    total = quotes["CRYPTOCAP:TOTAL"]
    assert total.venue == "CRYPTOCAP" and total.symbol == "TOTAL"
    assert total.last == 2_372_905_418_285.0
    assert total.currency == "USD"
    assert total.delay_s == 0

    stable_dominance = quotes["CRYPTOCAP:STABLE.C.D"]
    assert stable_dominance.last == 12.470951491521177
    assert stable_dominance.currency is None and stable_dominance.price_scale is None

    sse = quotes["SSE:000001"]
    assert sse.name == "SSE Composite Index"
    assert sse.last == 3655.8295
    assert sse.currency is None
    assert sse.delay_s == 900


def test_bond_board_rows_carry_coupon_and_maturity() -> None:
    quotes = _by_ticker(parse_rows(_fixture("board_bonds.json"), captured_ns=CAPTURED_NS))
    assert list(quotes) == ["TVC:IN08", "TVC:TH15Y", "TVC:CO15Y", "TVC:NZ03MY", "TVC:SK06Y"]
    assert {quote.kind for quote in quotes.values()} == {AssetKind.BOND}

    india = quotes["TVC:IN08"]
    assert india.last == 97.081
    assert (india.bid, india.ask) == (97.081, 97.571)
    assert (india.open, india.high, india.low) == (97.514, 97.518, 97.081)
    assert india.coupon == 6.79
    assert india.maturity is None
    assert india.delay_s == 0

    thailand = quotes["TVC:TH15Y"]
    assert thailand.coupon == 2.86
    assert thailand.maturity == date(2042, 5, 17)

    new_zealand = quotes["TVC:NZ03MY"]
    assert new_zealand.coupon == 0.0
    assert new_zealand.maturity == date(2027, 2, 12)


def test_mixed_quotes_keep_every_covered_kind_and_drop_the_stock() -> None:
    quotes = _by_ticker(parse_rows(_fixture("quotes_mixed.json"), captured_ns=CAPTURED_NS))
    assert list(quotes) == [
        "CME_MINI:ES1!",
        "CME_MINI:ESZ2026",
        "FX_IDC:EURUSD",
        "TVC:US10Y",
        "BIST:XU100",
    ]
    assert "BIST:THYAO" not in quotes
    assert quotes["CME_MINI:ES1!"].kind is AssetKind.FUTURE
    assert quotes["FX_IDC:EURUSD"].kind is AssetKind.FOREX
    assert quotes["TVC:US10Y"].kind is AssetKind.BOND
    assert quotes["BIST:XU100"].kind is AssetKind.INDEX

    es = quotes["CME_MINI:ES1!"]
    assert es.name == "E-mini S&P 500 Futures"
    assert es.last == 7654.75
    assert es.bid is None and es.ask is None
    assert (es.open, es.high, es.low) == (7616.75, 7674.75, 7572.0)
    assert es.volume == 1_065_827.0
    assert es.open_interest == 1_947_844.0
    assert es.expiry is None
    assert es.root == "CME_MINI:ES"
    assert (es.currency, es.price_scale) == ("USD", 0)
    assert es.delay_s == 600

    dated = quotes["CME_MINI:ESZ2026"]
    assert dated.expiry == date(2026, 12, 18)
    assert dated.name == "E-mini S&P 500 Futures (Dec 2026)"

    eurusd = quotes["FX_IDC:EURUSD"]
    assert (eurusd.bid, eurusd.ask) == (1.10544, 1.10546)
    assert eurusd.volume == 0.0
    assert eurusd.open_interest is None
    assert eurusd.delay_s == 0

    us10y = quotes["TVC:US10Y"]
    assert us10y.last == 5.105
    assert us10y.coupon == 5.0
    assert us10y.maturity is None

    xu100 = quotes["BIST:XU100"]
    assert xu100.last == 11811.19
    assert xu100.currency is None
    assert xu100.delay_s == 900


def test_prev_close_is_the_close_before_the_stated_change() -> None:
    for name in (*BOARD_FILES, "quotes_mixed.json"):
        quotes = _by_ticker(parse_rows(_fixture(name), captured_ns=CAPTURED_NS))
        for row in _doc(name)["data"]:
            if row["s"] not in quotes:
                continue
            cells = dict(zip(COLUMNS, row["d"], strict=True))
            quote = quotes[row["s"]]
            expected = cells["close"] / (1 + cells["change"] / 100)
            assert quote.prev_close == pytest.approx(expected), row["s"]
            assert quote.change_pct == pytest.approx(cells["change"]), row["s"]


def test_change_pct_states_the_change_column_for_a_dated_future() -> None:
    quotes = _by_ticker(parse_rows(_fixture("board_futures_eurex.json"), captured_ns=CAPTURED_NS))
    dated = quotes["EUREX:FGBLZ2026"]
    assert dated.prev_close == pytest.approx(122.22 / (1 + -0.2285714285714295 / 100))
    assert dated.change_pct == pytest.approx(-0.2285714285714295)


def test_a_row_with_the_wrong_number_of_cells_is_skipped() -> None:
    doc = _doc("board_futures_eurex.json")
    doc["data"][0]["d"].pop()
    quotes = parse_rows(json.dumps(doc).encode(), captured_ns=CAPTURED_NS)
    assert [quote.ticker for quote in quotes] == [
        "EUREX:FGBL1!",
        "EUREX:FGBM1!",
        "EUREX:FGBMZ2026",
        "EUREX:FGBSZ2026",
    ]


def test_a_non_positive_close_gives_no_last_price() -> None:
    for close in (0, -1.5):
        doc = _doc("board_futures_eurex.json")
        doc["data"][0]["d"][5] = close
        first = parse_rows(json.dumps(doc).encode(), captured_ns=CAPTURED_NS)[0]
        assert first.ticker == "EUREX:FGBLZ2026"
        assert first.last is None
        assert first.prev_close is None
        assert first.change_pct is None


def test_an_empty_scan_is_no_rows() -> None:
    assert parse_rows(b'{"totalCount":0,"data":null}', captured_ns=CAPTURED_NS) == []


def test_a_body_without_a_data_array_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        parse_rows(b'{"totalCount":3}', captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        parse_rows(b"[]", captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError):
        parse_rows(b"<html>", captured_ns=CAPTURED_NS)


# --- request bodies --------------------------------------------------------------------------


def test_the_column_lists_are_in_the_fixture_order() -> None:
    assert COLUMNS[0] == "name" and COLUMNS[5] == "close" and COLUMNS[13] == "update_mode"
    assert COLUMNS[15] == "expiration" and COLUMNS[18] == "maturity_date"
    assert len(COLUMNS) == 19
    assert OPTION_COLUMNS[3] == "strike" and OPTION_COLUMNS[5] == "option-type"
    assert OPTION_COLUMNS[6] == "theoPrice"
    assert len(OPTION_COLUMNS) == 15


def test_a_board_with_a_venue_filters_by_exchange_sorts_by_volume_and_ranges() -> None:
    assert board_body("EUREX", 5) == {
        "columns": list(COLUMNS),
        "filter": [{"left": "exchange", "operation": "in_range", "right": ["EUREX"]}],
        "sort": {"sortBy": "volume", "sortOrder": "desc"},
        "range": [0, 5],
    }


def test_a_board_without_a_venue_has_no_filter_key() -> None:
    body = board_body(None, 200)
    assert "filter" not in body
    assert body["range"] == [0, 200]
    assert body["sort"] == {"sortBy": "volume", "sortOrder": "desc"}


def test_an_option_body_names_the_underlying_ticker() -> None:
    assert option_body("BIST:THYAO", 5000) == {
        "columns": list(OPTION_COLUMNS),
        "index_filters": [{"name": "underlying_symbol", "values": ["BIST:THYAO"]}],
        "range": [0, 5000],
    }


# --- parse_option_chain ----------------------------------------------------------------------


def test_thyao_chain_has_eight_contracts_in_expiry_strike_right_order() -> None:
    chain = parse_option_chain(
        _trimmed("options_thyao.json"), "BIST:THYAO", captured_ns=CAPTURED_NS
    )
    assert [record.symbol for record in chain] == [
        "BIST:THYAO261130P230",
        "BIST:THYAO261130C250",
        "BIST:THYAO261231P270",
        "BIST:THYAO261231C330",
        "BIST:THYAO261231P330",
        "BIST:THYAO270129P260",
        "BIST:THYAO270129P350",
        "BIST:THYAO270129C390",
    ]


def test_thyao_put_record_carries_the_scanner_figures() -> None:
    chain = parse_option_chain(
        _trimmed("options_thyao.json"), "BIST:THYAO", captured_ns=CAPTURED_NS
    )
    put = chain[0]
    assert put.source == SOURCE
    assert put.symbol_raw == put.symbol == "BIST:THYAO261130P230"
    assert put.underlying == "BIST:THYAO"
    assert put.strike == 230.0
    assert put.expiry == _utc_ns(date(2026, 11, 30))
    assert put.opt_type is OptType.PUT
    assert (put.bid_px, put.ask_px) == (0.8, 1.5)
    assert put.mark_price == 0.79
    assert put.mark_iv == 0.8361749566041666
    assert put.delta == -0.03182152987216876
    assert put.gamma == 0.0012060079681176577
    assert put.theta == -0.11752311127743993
    assert put.vega == 0.050606737504017285
    assert put.rho == -0.005394329972483636
    assert put.local_ts == CAPTURED_NS
    assert put.prov is Provenance.NATIVE
    assert put.prov_basis == "scanner_snapshot"
    assert put.asset_class is AssetClass.EQUITY
    assert put.bid_sz is None and put.ask_sz is None
    assert put.volume is None and put.open_interest is None


def test_thyao_call_record_and_a_contract_with_no_bid() -> None:
    chain = parse_option_chain(
        _trimmed("options_thyao.json"), "BIST:THYAO", captured_ns=CAPTURED_NS
    )
    call = chain[1]
    assert call.opt_type is OptType.CALL
    assert call.strike == 250.0
    assert call.expiry == _utc_ns(date(2026, 11, 30))
    assert (call.bid_px, call.ask_px) == (20.1, 20.9)
    assert call.mark_price == 69.74
    assert call.mark_iv == 0.6518648755881087
    assert call.delta == 0.9599881480267054
    assert call.gamma == 0.001847667739104122
    assert call.theta == -0.10753467025505963
    assert call.vega == 0.06044250614359337
    assert call.rho == 0.11662732847415828

    no_bid = chain[5]
    assert no_bid.symbol == "BIST:THYAO270129P260"
    assert no_bid.bid_px is None and no_bid.ask_px is None
    assert no_bid.mark_price == 4.02


def test_aapl_chain_has_six_contracts_sorted_by_expiry_then_strike() -> None:
    chain = parse_option_chain(
        _trimmed("options_aapl.json"), "NASDAQ:AAPL", captured_ns=CAPTURED_NS
    )
    assert [record.symbol for record in chain] == [
        "OPRA:AAPL261116C340.0",
        "OPRA:AAPL261116P382.5",
        "OPRA:AAPL261120C295.0",
        "OPRA:AAPL261204C400.0",
        "OPRA:AAPL261218C115.0",
        "OPRA:AAPL271217C235.0",
    ]
    call = chain[0]
    assert call.underlying == "NASDAQ:AAPL"
    assert call.strike == 340.0
    assert call.expiry == _utc_ns(date(2026, 11, 16))
    assert call.opt_type is OptType.CALL
    assert (call.bid_px, call.ask_px) == (16.15, 17.75)
    assert call.mark_price == 17.0
    assert call.mark_iv == 0.2537421822547913
    assert call.delta == 0.9639239834902533
    assert call.gamma == 0.008194287633915876
    assert call.theta == -0.12678402174502315
    assert call.vega == 0.030177353170740582
    assert call.rho == 0.03728483299561811
    assert call.prov_basis == "scanner_snapshot"
    assert call.asset_class is AssetClass.EQUITY

    put = chain[1]
    assert put.opt_type is OptType.PUT
    assert put.strike == 382.5
    assert put.mark_price == 25.65
    assert put.delta == -0.9922347870330179

    last = chain[5]
    assert last.expiry == _utc_ns(date(2027, 12, 17))
    assert last.strike == 235.0


def test_a_row_without_a_strike_or_with_an_unknown_right_is_dropped() -> None:
    doc = _doc("options_thyao.json")
    doc["totalCount"] = len(doc["data"])
    doc["data"][0]["d"][3] = None
    doc["data"][1]["d"][5] = "straddle"
    chain = parse_option_chain(json.dumps(doc).encode(), "BIST:THYAO", captured_ns=CAPTURED_NS)
    symbols = [record.symbol for record in chain]
    assert len(chain) == 6
    assert "BIST:THYAO261130P230" not in symbols
    assert "BIST:THYAO261130C250" not in symbols


def test_an_empty_option_scan_is_no_contracts() -> None:
    assert parse_option_chain(b'{"totalCount":0,"data":null}', "BIST:THYAO", captured_ns=1) == []


def test_an_option_answer_without_a_data_array_is_a_payload_error() -> None:
    with pytest.raises(LivePayloadError):
        parse_option_chain(b'{"totalCount":2}', "BIST:THYAO", captured_ns=1)


# --- asset_records ---------------------------------------------------------------------------


def _es() -> AssetQuote:
    return _by_ticker(parse_rows(_fixture("quotes_mixed.json"), captured_ns=CAPTURED_NS))[
        "CME_MINI:ES1!"
    ]


def test_a_future_with_open_interest_gives_a_ticker_and_its_open_interest() -> None:
    ticker, interest = asset_records(_es())
    assert isinstance(ticker, DerivativeTicker)
    assert isinstance(interest, OpenInterest)
    assert ticker.last_price == 7654.75
    assert ticker.open_interest == 1_947_844.0
    assert interest.open_interest == 1_947_844.0
    assert ticker.symbol == interest.symbol == _es().ticker == "CME_MINI:ES1!"
    assert ticker.local_ts == CAPTURED_NS
    assert ticker.prov is Provenance.NATIVE
    assert ticker.prov_basis == "scanner_snapshot"


def test_a_future_without_open_interest_gives_only_the_ticker() -> None:
    (only,) = asset_records(replace(_es(), open_interest=None))
    assert isinstance(only, DerivativeTicker)
    assert only.open_interest is None
    assert only.last_price == 7654.75


def test_an_index_row_gives_an_index_value() -> None:
    sse = _by_ticker(parse_rows(_fixture("board_index.json"), captured_ns=CAPTURED_NS))[
        "SSE:000001"
    ]
    (value,) = asset_records(sse)
    assert isinstance(value, IndexValue)
    assert value.value == 3655.8295
    assert value.symbol == "SSE:000001"


def test_forex_and_bond_rows_give_no_record() -> None:
    quotes = _by_ticker(parse_rows(_fixture("quotes_mixed.json"), captured_ns=CAPTURED_NS))
    assert asset_records(quotes["FX_IDC:EURUSD"]) == []
    assert asset_records(quotes["TVC:US10Y"]) == []


def test_a_future_without_a_last_price_gives_no_record() -> None:
    assert asset_records(replace(_es(), last=None)) == []


# --- known_venues ----------------------------------------------------------------------------


def test_future_venues_are_measured_with_their_counts() -> None:
    futures = known_venues(AssetKind.FUTURE)
    assert futures["EUREX"] == 19_385
    assert futures["CME"] == 2_580
    assert futures["BIST"] == 257
    assert all(count > 0 for count in futures.values())


def test_forex_and_bond_venues() -> None:
    assert known_venues(AssetKind.FOREX)["FX_IDC"] == 4_000
    assert known_venues(AssetKind.BOND)["TVC"] == 1_163


def test_option_venues_are_empty() -> None:
    assert len(known_venues(AssetKind.OPTION)) == 0


def test_the_venue_table_is_read_only() -> None:
    venues: Any = known_venues(AssetKind.FUTURE)
    with pytest.raises(TypeError):
        venues["NOPE"] = 1


# --- yahoo_symbol_for ------------------------------------------------------------------------


def test_a_currency_pair_is_spelled_with_equals_x() -> None:
    assert yahoo_symbol_for(AssetKind.FOREX, "FX_IDC:EURUSD") == "EURUSD=X"
    assert yahoo_symbol_for(AssetKind.FOREX, "FX_IDC:EURUS") is None
    assert yahoo_symbol_for(AssetKind.FOREX, "FX_IDC:EURUSDX") is None


def test_a_continuous_us_future_is_spelled_as_its_root_with_f() -> None:
    assert yahoo_symbol_for(AssetKind.FUTURE, "CME_MINI:ES1!") == "ES=F"
    assert yahoo_symbol_for(AssetKind.FUTURE, "NYMEX:CL1!") == "CL=F"


def test_a_dated_contract_and_a_non_us_future_have_no_spelling() -> None:
    assert yahoo_symbol_for(AssetKind.FUTURE, "CME_MINI:ESZ2026") is None
    assert yahoo_symbol_for(AssetKind.FUTURE, "EUREX:FDAX1!") is None


def test_indices_are_looked_up_in_the_verified_map() -> None:
    assert yahoo_symbol_for(AssetKind.INDEX, "SP:SPX") == "^GSPC"
    assert yahoo_symbol_for(AssetKind.INDEX, "BIST:XU100") == "XU100.IS"
    assert yahoo_symbol_for(AssetKind.INDEX, "SP:NOPE") is None


def test_a_bond_and_a_malformed_ticker_have_no_spelling() -> None:
    assert yahoo_symbol_for(AssetKind.BOND, "TVC:US10Y") is None
    assert yahoo_symbol_for(AssetKind.FUTURE, "ES1!") is None
    assert yahoo_symbol_for(AssetKind.FOREX, "fx_idc:EURUSD") is None


def test_every_index_map_row_is_backed_by_a_verified_chart_answer() -> None:
    text = (
        importlib.resources.files("crocodile.equity.universal.live")
        .joinpath("data/yahoo_index_map.csv")
        .read_text("ascii")
    )
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 20
    assert all(row["evidence"].startswith("verified:") for row in rows)


# --- AssetFeed with a fake fetcher -----------------------------------------------------------


async def test_a_futures_board_is_one_post_to_the_futures_scan() -> None:
    fetch = FakeFetch((200, _fixture("board_futures_eurex.json")))
    quotes = await _feed(fetch).board(AssetKind.FUTURE, "EUREX", limit=5)
    assert len(quotes) == 5
    assert all(quote.kind is AssetKind.FUTURE for quote in quotes)
    assert quotes[0].captured_ns == CAPTURED_NS
    (request,) = fetch.requests
    assert request.method == "POST"
    assert request.url == f"{SCAN_BASE}/futures/scan"
    assert request.json_body == board_body("EUREX", 5)


async def test_a_forex_board_defaults_to_the_composite_venue() -> None:
    fetch = FakeFetch((200, _fixture("board_forex.json")))
    quotes = await _feed(fetch).board(AssetKind.FOREX)
    assert len(quotes) == 5
    (request,) = fetch.requests
    assert request.url == f"{SCAN_BASE}/forex/scan"
    assert request.json_body == board_body("FX_IDC", 200)
    assert request.json_body is not None
    assert request.json_body["filter"][0]["right"] == ["FX_IDC"]


async def test_a_bond_board_defaults_to_the_government_bond_venue() -> None:
    fetch = FakeFetch((200, _fixture("board_bonds.json")))
    quotes = await _feed(fetch).board(AssetKind.BOND)
    assert len(quotes) == 5
    assert all(quote.kind is AssetKind.BOND for quote in quotes)
    (request,) = fetch.requests
    assert request.url == f"{SCAN_BASE}/bonds/scan"
    assert request.json_body == board_body("TVC", 200)


async def test_an_index_board_sends_no_exchange_filter() -> None:
    fetch = FakeFetch((200, _fixture("board_index.json")))
    quotes = await _feed(fetch).board(AssetKind.INDEX)
    assert len(quotes) == 8
    assert all(quote.kind is AssetKind.INDEX for quote in quotes)
    (request,) = fetch.requests
    assert request.url == f"{SCAN_BASE}/cfd/scan"
    assert request.json_body is not None
    assert "filter" not in request.json_body
    assert request.json_body == board_body(None, 200)


async def test_an_option_board_is_refused_before_any_request() -> None:
    fetch = FakeFetch()
    with pytest.raises(ValueError):
        await _feed(fetch).board(AssetKind.OPTION)
    assert fetch.requests == []


async def test_quotes_are_one_global_post_with_the_tickers_deduplicated() -> None:
    fetch = FakeFetch((200, _fixture("quotes_mixed.json")))
    quotes = await _feed(fetch).quotes(
        ["CME_MINI:ES1!", "FX_IDC:EURUSD", "CME_MINI:ES1!", "cme:ES", "ES1!", "", "BIST:XU100"]
    )
    assert len(quotes) == 5
    (request,) = fetch.requests
    assert request.method == "POST"
    assert request.url == f"{SCAN_BASE}/global/scan"
    assert request.json_body == {
        "columns": list(COLUMNS),
        "symbols": {"tickers": ["CME_MINI:ES1!", "FX_IDC:EURUSD", "BIST:XU100"]},
    }


async def test_quotes_with_nothing_valid_asked_make_no_request() -> None:
    fetch = FakeFetch()
    assert await _feed(fetch).quotes(["ES1!", "", "cme:ES"]) == []
    assert fetch.requests == []


async def test_an_option_chain_is_one_post_to_the_options_scan() -> None:
    fetch = FakeFetch((200, _trimmed("options_thyao.json")))
    chain = await _feed(fetch).option_chain("BIST:THYAO")
    assert len(chain) == 8
    assert chain[0].symbol == "BIST:THYAO261130P230"
    (request,) = fetch.requests
    assert request.method == "POST"
    assert request.url == f"{SCAN_BASE}/options/scan"
    assert request.json_body == option_body("BIST:THYAO", OPTION_CHAIN_LIMIT)


async def test_an_option_chain_of_a_bad_ticker_is_refused_before_any_request() -> None:
    fetch = FakeFetch()
    with pytest.raises(ValueError):
        await _feed(fetch).option_chain("bad")
    assert fetch.requests == []


async def test_minute_bars_of_a_currency_pair_come_from_the_chart_endpoint() -> None:
    fetch = FakeFetch((200, YAHOO_CHART.read_bytes()))
    bars = await _feed(fetch).minute_bars(AssetKind.FOREX, "FX_IDC:EURUSD")
    assert len(bars) == 62
    assert bars[0] == MinuteBar(
        ts_ns=1_794_466_500 * NS_PER_SECOND,
        open=315.5,
        high=315.5,
        low=315.5,
        close=315.5,
        volume=0.0,
    )
    (request,) = fetch.requests
    assert request.method == "GET"
    assert request.url == "https://query2.finance.yahoo.com/v8/finance/chart/EURUSD%3DX"
    assert request.params == (("interval", "1m"), ("range", "1d"))


async def test_minute_bars_without_a_yahoo_spelling_make_no_request() -> None:
    fetch = FakeFetch()
    feed = _feed(fetch)
    assert await feed.minute_bars(AssetKind.BOND, "TVC:US10Y") == []
    assert await feed.minute_bars(AssetKind.FUTURE, "EUREX:FGBL1!") == []
    assert fetch.requests == []


async def test_a_chart_404_gives_no_bars() -> None:
    fetch = FakeFetch((404, b"{}"))
    assert await _feed(fetch).minute_bars(AssetKind.FOREX, "FX_IDC:EURUSD") == []
    assert len(fetch.requests) == 1


async def test_a_scanner_403_is_a_wall() -> None:
    fetch = FakeFetch((403, b"{}"))
    with pytest.raises(SourceUnservableError) as caught:
        await _feed(fetch).board(AssetKind.FUTURE, "EUREX")
    assert caught.value.reason is UnservableReason.FORBIDDEN


async def test_a_scanner_500_is_a_payload_error() -> None:
    fetch = FakeFetch((500, b"{}"))
    with pytest.raises(LivePayloadError):
        await _feed(fetch).option_chain("BIST:THYAO")


async def test_close_is_safe_with_an_injected_fetcher() -> None:
    feed = _feed(FakeFetch())
    await feed.close()
    await feed.close()


# --- option series and whole answers ---------------------------------------------------------


def test_a_series_scan_reads_three_columns_and_no_prices() -> None:
    assert OPTION_SERIES_COLUMNS == ("expiration", "root", "underlying_symbol")
    assert series_body(["NASDAQ:IBM", "NYSE:IBM"], 100) == {
        "columns": ["expiration", "root", "underlying_symbol"],
        "index_filters": [{"name": "underlying_symbol", "values": ["NASDAQ:IBM", "NYSE:IBM"]}],
        "range": [0, 100],
    }


def test_an_option_body_narrowed_to_one_series_filters_expiry_and_family() -> None:
    body = option_body("SP:SPX", 500, expiry=date(2026, 11, 20), root="SPXW")
    assert body["filter"] == [
        {"left": "expiration", "operation": "equal", "right": 20261120},
        {"left": "root", "operation": "equal", "right": "SPXW"},
    ]
    assert body["index_filters"] == [{"name": "underlying_symbol", "values": ["SP:SPX"]}]
    only_day = option_body("SP:SPX", 500, expiry=date(2027, 1, 5))
    assert only_day["filter"] == [{"left": "expiration", "operation": "equal", "right": 20270105}]
    assert "filter" not in option_body("SP:SPX", 500)


def test_the_thyao_series_scan_lists_three_expiries_with_their_sizes() -> None:
    # Captured whole on 2026-11-12: 94 rows, three month-end expiries of one family.
    series = parse_option_series(_fixture("options_series_thyao.json"))
    assert series == [
        OptionSeries(
            underlying="BIST:THYAO", root="THYAO", expiry=date(2026, 11, 30), contracts=32
        ),
        OptionSeries(
            underlying="BIST:THYAO", root="THYAO", expiry=date(2026, 12, 31), contracts=30
        ),
        OptionSeries(underlying="BIST:THYAO", root="THYAO", expiry=date(2027, 1, 29), contracts=32),
    ]
    assert sum(row.contracts for row in series) == _doc("options_series_thyao.json")["totalCount"]


def test_series_are_split_by_family_and_by_the_underlying_each_row_states() -> None:
    rows = [
        ("OPRA:SPXW261120C7000.0", [20261120, "SPXW", "SP:SPX"]),
        ("OPRA:SPXW261120P7000.0", [20261120, "SPXW", "SP:SPX"]),
        ("OPRA:SPX261120C7000.0", [20261120, "SPX", "SP:SPX"]),
        ("OPRA:IBM261113C300.0", [20261113, "IBM", "NYSE:IBM"]),
    ]
    body = json.dumps(
        {"totalCount": 4, "data": [{"s": ticker, "d": cells} for ticker, cells in rows]}
    ).encode()
    assert parse_option_series(body) == [
        OptionSeries(underlying="NYSE:IBM", root="IBM", expiry=date(2026, 11, 13), contracts=1),
        OptionSeries(underlying="SP:SPX", root="SPX", expiry=date(2026, 11, 20), contracts=1),
        OptionSeries(underlying="SP:SPX", root="SPXW", expiry=date(2026, 11, 20), contracts=2),
    ]


def test_a_series_row_without_an_expiry_a_family_or_an_underlying_is_dropped() -> None:
    rows = [
        [None, "THYAO", "BIST:THYAO"],
        [20261332, "THYAO", "BIST:THYAO"],
        [20261130, None, "BIST:THYAO"],
        [20261130, "", "BIST:THYAO"],
        [20261130, "THYAO", None],
        [20261130, "THYAO"],
        [20261130, "THYAO", "BIST:THYAO"],
    ]
    body = json.dumps(
        {"totalCount": 7, "data": [{"s": "x", "d": cells} for cells in rows]}
    ).encode()
    assert parse_option_series(body) == [
        OptionSeries(underlying="BIST:THYAO", root="THYAO", expiry=date(2026, 11, 30), contracts=1)
    ]


def test_an_empty_series_scan_is_no_series() -> None:
    assert parse_option_series(b'{"totalCount":0,"data":null}') == []
    assert parse_option_series(b'{"totalCount":0,"data":[]}') == []


def test_a_partial_series_answer_is_refused() -> None:
    doc = _doc("options_series_thyao.json")
    doc["data"] = doc["data"][:10]
    with pytest.raises(LivePayloadError, match="10 of 94"):
        parse_option_series(json.dumps(doc).encode())


def test_a_partial_chain_answer_is_refused_and_never_passed_for_the_chain() -> None:
    # The capture as it was taken: 8 rows of the 94 the scanner says match.
    with pytest.raises(LivePayloadError, match="8 of 94"):
        parse_option_chain(_fixture("options_thyao.json"), "BIST:THYAO", captured_ns=CAPTURED_NS)
    with pytest.raises(LivePayloadError, match="6 of 3396"):
        parse_option_chain(_fixture("options_aapl.json"), "NASDAQ:AAPL", captured_ns=CAPTURED_NS)


def test_an_answer_that_states_no_row_count_cannot_be_shown_whole_and_is_refused() -> None:
    doc = _doc("options_thyao.json")
    del doc["totalCount"]
    with pytest.raises(LivePayloadError, match="no row count"):
        parse_option_chain(json.dumps(doc).encode(), "BIST:THYAO", captured_ns=CAPTURED_NS)
    series = _doc("options_series_thyao.json")
    del series["totalCount"]
    with pytest.raises(LivePayloadError, match="no row count"):
        parse_option_series(json.dumps(series).encode())


@pytest.mark.parametrize("total", ["94", None, True, [94], float("nan")])
def test_a_row_count_that_is_not_a_number_is_refused(total: object) -> None:
    doc = _doc("options_thyao.json")
    doc["totalCount"] = total
    with pytest.raises(LivePayloadError, match="no row count"):
        parse_option_chain(json.dumps(doc).encode(), "BIST:THYAO", captured_ns=CAPTURED_NS)


def test_a_row_count_written_as_a_float_is_still_compared() -> None:
    doc = _doc("options_thyao.json")
    doc["totalCount"] = 94.0
    with pytest.raises(LivePayloadError, match="8 of 94 rows"):
        parse_option_chain(json.dumps(doc).encode(), "BIST:THYAO", captured_ns=CAPTURED_NS)
    doc["totalCount"] = 8.0
    assert len(parse_option_chain(json.dumps(doc).encode(), "BIST:THYAO", captured_ns=1)) == 8


def test_an_expiry_no_date_can_hold_drops_the_row_and_does_not_crash() -> None:
    doc = _doc("options_thyao_20261130.json")
    doc["data"][0]["d"][4] = 1e15
    chain = parse_option_chain(json.dumps(doc).encode(), "BIST:THYAO", captured_ns=CAPTURED_NS)
    assert len(chain) == 31
    series = _doc("options_series_thyao.json")
    series["data"][0]["d"][0] = 2.2e13
    assert sum(row.contracts for row in parse_option_series(json.dumps(series).encode())) == 93


def test_one_whole_series_of_thyao_lists_some_strikes_on_one_side_only() -> None:
    # Captured whole on 2026-11-12 with expiry 2026-11-30 and family THYAO: 32 of 32 rows.
    chain = parse_option_chain(
        _fixture("options_thyao_20261130.json"), "BIST:THYAO", captured_ns=CAPTURED_NS
    )
    assert len(chain) == 32
    assert {record.expiry for record in chain} == {_utc_ns(date(2026, 11, 30))}
    assert [record.strike for record in chain] == sorted(record.strike for record in chain)
    sides: dict[float, list[OptType]] = {}
    for record in chain:
        sides.setdefault(record.strike, []).append(record.opt_type)
    # 21 strikes carry 32 contracts: a strike is not always a call and a put.
    assert len(sides) == 21
    assert sum(1 for rights in sides.values() if len(rights) == 2) == 11
    assert all(len(set(rights)) == len(rights) for rights in sides.values())


async def test_option_series_is_one_post_naming_every_candidate_once() -> None:
    fetch = FakeFetch((200, _fixture("options_series_thyao.json")))
    series = await _feed(fetch).option_series(["BIST:THYAO", "NYSE:THYAO", "BIST:THYAO"])
    assert [row.expiry for row in series] == [
        date(2026, 11, 30),
        date(2026, 12, 31),
        date(2027, 1, 29),
    ]
    (request,) = fetch.requests
    assert request.method == "POST"
    assert request.url == f"{SCAN_BASE}/options/scan"
    assert request.json_body == series_body(["BIST:THYAO", "NYSE:THYAO"], OPTION_CHAIN_LIMIT)


async def test_option_series_takes_one_ticker_as_a_string() -> None:
    fetch = FakeFetch((200, _fixture("options_series_thyao.json")))
    await _feed(fetch).option_series("BIST:THYAO")
    (request,) = fetch.requests
    assert request.json_body == series_body(["BIST:THYAO"], OPTION_CHAIN_LIMIT)


async def test_option_series_of_nothing_or_of_a_bad_ticker_makes_no_request() -> None:
    fetch = FakeFetch()
    assert await _feed(fetch).option_series([]) == []
    with pytest.raises(ValueError):
        await _feed(fetch).option_series(["BIST:THYAO", "bad"])
    assert fetch.requests == []


async def test_an_option_chain_of_one_series_sends_the_narrowed_scan() -> None:
    fetch = FakeFetch((200, _fixture("options_thyao_20261130.json")))
    chain = await _feed(fetch).option_chain("BIST:THYAO", expiry=date(2026, 11, 30), root="THYAO")
    assert len(chain) == 32
    (request,) = fetch.requests
    assert request.json_body == option_body(
        "BIST:THYAO", OPTION_CHAIN_LIMIT, expiry=date(2026, 11, 30), root="THYAO"
    )


async def test_an_option_chain_the_source_cut_short_raises() -> None:
    fetch = FakeFetch((200, _fixture("options_thyao.json")))
    with pytest.raises(LivePayloadError, match="8 of 94"):
        await _feed(fetch).option_chain("BIST:THYAO", limit=8)
