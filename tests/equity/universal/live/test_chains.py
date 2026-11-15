"""``chains``: whole option chains from Cboe, Bybit and Deribit, from captured answers."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from crocodile.core.ratelimit import reset_budgets
from crocodile.core.schema.enums import AssetClass, OptType
from crocodile.core.schema.records import OptionsChain
from crocodile.equity.universal.http import HttpRequest, HttpResponse
from crocodile.equity.universal.live.assets import OptionSeries
from crocodile.equity.universal.live.base import LivePayloadError
from crocodile.equity.universal.live.chains import (
    BYBIT_SOURCE,
    CBOE_DELAY_S,
    CBOE_SOURCE,
    DERIBIT_SOURCE,
    PublishedChain,
    PublishedChains,
    parse_bybit_chain,
    parse_cboe_chain,
    parse_deribit_chain,
    parse_occ_symbol,
)

CHAINS = Path(__file__).resolve().parents[1] / "fixtures" / "live" / "chains"
CAPTURED_NS = 1_794_471_000_000_000_000
NS_PER_SECOND = 1_000_000_000
NS_PER_MS = 1_000_000
CBOE_AAPL_URL = "https://cdn-api.cboe.com/api/global/delayed_quotes/options/AAPL.json"
BYBIT_URL = "https://api.bybit.com/v5/market/tickers"
DERIBIT_URL = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"


@pytest.fixture(autouse=True)
def _fresh_budgets() -> None:
    reset_budgets()


def _fixture(name: str) -> bytes:
    return (CHAINS / name).read_bytes()


def _doc(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_fixture(name))
    return loaded


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


def _chains(fetch: FakeFetch) -> PublishedChains:
    return PublishedChains(fetch=fetch, clock_ns=lambda: CAPTURED_NS)


def _by_symbol(chain: PublishedChain) -> dict[str, OptionsChain]:
    return {item.record.symbol: item.record for item in chain.contracts}


# --- parse_occ_symbol ------------------------------------------------------------------------


def test_a_good_occ_symbol_gives_its_root_expiry_right_and_strike() -> None:
    assert parse_occ_symbol("AAPL261113C00110000") == (
        "AAPL",
        date(2026, 11, 13),
        OptType.CALL,
        110.0,
    )
    assert parse_occ_symbol("AAPL261113P00110000") == (
        "AAPL",
        date(2026, 11, 13),
        OptType.PUT,
        110.0,
    )


def test_a_dotted_root_keeps_its_dot() -> None:
    assert parse_occ_symbol("BRK.B261113C00500000") == (
        "BRK.B",
        date(2026, 11, 13),
        OptType.CALL,
        500.0,
    )


def test_an_impossible_expiry_date_gives_none() -> None:
    assert parse_occ_symbol("AAPL261332C00110000") is None
    assert parse_occ_symbol("AAPL260230C00110000") is None


def test_a_non_symbol_gives_none() -> None:
    assert parse_occ_symbol("AAPL") is None
    assert parse_occ_symbol("") is None
    assert parse_occ_symbol("AAPL261113X00110000") is None
    assert parse_occ_symbol("aapl261113C00110000") is None


def test_the_strike_is_the_eight_digits_over_one_thousand() -> None:
    assert parse_occ_symbol("AAPL261113C00110000")[3] == 110.0  # type: ignore[index]
    assert parse_occ_symbol("AAPL261113C00307500")[3] == 307.5  # type: ignore[index]


# --- parse_cboe_chain ------------------------------------------------------------------------


def test_the_aapl_capture_gives_twelve_contracts_under_the_aapl_root() -> None:
    chain = parse_cboe_chain(_fixture("cboe_aapl.json"), "AAPL", captured_ns=CAPTURED_NS)
    assert len(chain.contracts) == 12
    assert {item.root for item in chain.contracts} == {"AAPL"}


def test_the_spx_capture_keeps_spx_and_spxw_apart() -> None:
    chain = parse_cboe_chain(_fixture("cboe_spx.json"), "_SPX", captured_ns=CAPTURED_NS)
    assert len(chain.contracts) == 12
    counts = Counter(item.root for item in chain.contracts)
    assert counts == {"SPX": 2, "SPXW": 10}
    assert {item.record.symbol for item in chain.contracts if item.root == "SPX"} == {
        "SPX261120C07225000",
        "SPX261120P07225000",
    }


def test_the_expiry_is_utc_midnight_in_nanoseconds() -> None:
    chain = parse_cboe_chain(_fixture("cboe_aapl.json"), "AAPL", captured_ns=CAPTURED_NS)
    by_symbol = _by_symbol(chain)
    assert by_symbol["AAPL261113C00110000"].expiry == _utc_ns(date(2026, 11, 13))
    assert by_symbol["AAPL261116C00275000"].expiry == _utc_ns(date(2026, 11, 16))


def test_bid_and_ask_carry_their_sizes() -> None:
    record = _by_symbol(
        parse_cboe_chain(_fixture("cboe_aapl.json"), "AAPL", captured_ns=CAPTURED_NS)
    )["AAPL261113C00110000"]
    assert (record.bid_px, record.bid_sz) == (244.95, 55.0)
    assert (record.ask_px, record.ask_sz) == (247.35, 62.0)


def test_a_zero_bid_with_zero_size_is_read_as_no_bid() -> None:
    record = _by_symbol(
        parse_cboe_chain(_fixture("cboe_aapl.json"), "AAPL", captured_ns=CAPTURED_NS)
    )["AAPL261113P00110000"]
    assert (record.bid_px, record.bid_sz) == (None, None)
    assert (record.ask_px, record.ask_sz) == (0.01, 2008.0)


def test_an_implied_volatility_of_zero_is_read_as_none() -> None:
    by_symbol = _by_symbol(
        parse_cboe_chain(_fixture("cboe_aapl.json"), "AAPL", captured_ns=CAPTURED_NS)
    )
    assert by_symbol["AAPL261113C00110000"].mark_iv is None
    assert by_symbol["AAPL261113P00110000"].mark_iv == 6.413


def test_a_last_trade_without_a_trade_time_is_read_as_no_trade() -> None:
    by_symbol = _by_symbol(
        parse_cboe_chain(_fixture("cboe_aapl.json"), "AAPL", captured_ns=CAPTURED_NS)
    )
    assert by_symbol["AAPL261116C00312500"].last_price is None
    assert by_symbol["AAPL261113C00110000"].last_price == 244.9


def test_volume_and_open_interest_come_from_the_row() -> None:
    record = _by_symbol(
        parse_cboe_chain(_fixture("cboe_aapl.json"), "AAPL", captured_ns=CAPTURED_NS)
    )["AAPL261116P00275000"]
    assert record.volume == 65.0
    assert record.open_interest == 209.0


def test_greeks_and_theo_come_from_the_row() -> None:
    record = _by_symbol(
        parse_cboe_chain(_fixture("cboe_aapl.json"), "AAPL", captured_ns=CAPTURED_NS)
    )["AAPL261116P00275000"]
    assert record.delta == -0.0005
    assert record.gamma == 0.0001
    assert record.vega == 0.0006
    assert record.theta == -0.0028
    assert record.rho == 0.0
    assert record.mark_price == 0.0031


def test_the_underlying_price_is_the_current_price_of_the_file() -> None:
    chain = parse_cboe_chain(_fixture("cboe_aapl.json"), "AAPL", captured_ns=CAPTURED_NS)
    assert chain.underlying_price == 355.43
    assert {item.record.underlying_price for item in chain.contracts} == {355.43}


def test_stamped_ns_is_the_utc_timestamp_of_the_file() -> None:
    chain = parse_cboe_chain(_fixture("cboe_aapl.json"), "AAPL", captured_ns=CAPTURED_NS)
    stamp = int(datetime(2026, 11, 12, 20, 33, 17, tzinfo=UTC).timestamp()) * NS_PER_SECOND
    assert chain.stamped_ns == stamp
    assert {item.record.source_ts for item in chain.contracts} == {stamp}


def test_the_chain_states_usd_fifteen_minutes_of_delay_and_the_cboe_source() -> None:
    chain = parse_cboe_chain(_fixture("cboe_aapl.json"), "AAPL", captured_ns=CAPTURED_NS)
    assert CBOE_DELAY_S == 900
    assert chain.source == CBOE_SOURCE == "cboe_delayed"
    assert chain.premium_unit == "USD"
    assert chain.delay_s == 900
    assert chain.underlying == "AAPL"
    assert chain.captured_ns == CAPTURED_NS


def test_each_record_states_the_book_basis_the_equity_class_and_the_capture_time() -> None:
    chain = parse_cboe_chain(_fixture("cboe_aapl.json"), "AAPL", captured_ns=CAPTURED_NS)
    for item in chain.contracts:
        record = item.record
        assert record.prov_basis == "polled_book_snapshot"
        assert record.asset_class is AssetClass.EQUITY
        assert record.source == CBOE_SOURCE
        assert record.local_ts == CAPTURED_NS
        assert record.symbol_raw == record.symbol
        assert record.underlying == "AAPL"


@pytest.mark.parametrize(
    "body",
    [
        b'{"symbol": "AAPL", "data": {"current_price": 1.0}}',
        b'{"symbol": "AAPL", "data": {"options": "not a list"}}',
        b'{"symbol": "AAPL", "data": null}',
        b"[]",
    ],
)
def test_a_body_without_an_options_array_is_a_payload_error(body: bytes) -> None:
    with pytest.raises(LivePayloadError):
        parse_cboe_chain(body, "AAPL", captured_ns=CAPTURED_NS)


def test_rows_that_are_not_objects_or_not_occ_symbols_are_dropped() -> None:
    doc = _doc("cboe_aapl.json")
    junk: list[Any] = [
        "junk",
        None,
        {"option": "NOTASYMBOL", "bid": 1.0},
        {"bid": 1.0},
        {"option": 42},
    ]
    doc["data"]["options"] = junk + doc["data"]["options"]
    chain = parse_cboe_chain(json.dumps(doc).encode(), "AAPL", captured_ns=CAPTURED_NS)
    assert len(chain.contracts) == 12


# --- PublishedChain.series and of_series -----------------------------------------------------


def test_series_lists_each_expiry_and_root_in_expiry_then_root_order() -> None:
    aapl = parse_cboe_chain(_fixture("cboe_aapl.json"), "AAPL", captured_ns=CAPTURED_NS)
    assert aapl.series() == [
        OptionSeries(underlying="AAPL", root="AAPL", expiry=date(2026, 11, 13), contracts=6),
        OptionSeries(underlying="AAPL", root="AAPL", expiry=date(2026, 11, 16), contracts=6),
    ]
    spx = parse_cboe_chain(_fixture("cboe_spx.json"), "_SPX", captured_ns=CAPTURED_NS)
    assert spx.series() == [
        OptionSeries(underlying="_SPX", root="SPX", expiry=date(2026, 11, 20), contracts=2),
        OptionSeries(underlying="_SPX", root="SPXW", expiry=date(2026, 11, 20), contracts=10),
    ]


def test_of_series_gives_the_contracts_in_strike_then_right_order() -> None:
    chain = parse_cboe_chain(_fixture("cboe_spx.json"), "_SPX", captured_ns=CAPTURED_NS)
    records = chain.of_series(date(2026, 11, 20), "SPXW")
    assert [(record.strike, record.opt_type) for record in records] == [
        (3200.0, OptType.CALL),
        (3200.0, OptType.PUT),
        (3400.0, OptType.CALL),
        (3400.0, OptType.PUT),
        (3600.0, OptType.CALL),
        (3600.0, OptType.PUT),
        (3700.0, OptType.CALL),
        (3700.0, OptType.PUT),
        (7225.0, OptType.CALL),
        (7225.0, OptType.PUT),
    ]


def test_of_series_leaves_another_root_on_the_same_day_out() -> None:
    chain = parse_cboe_chain(_fixture("cboe_spx.json"), "_SPX", captured_ns=CAPTURED_NS)
    records = chain.of_series(date(2026, 11, 20), "SPX")
    assert [record.symbol for record in records] == [
        "SPX261120C07225000",
        "SPX261120P07225000",
    ]


# --- parse_bybit_chain -----------------------------------------------------------------------


def test_bybit_root_is_the_base_coin_with_its_settlement_coin() -> None:
    chain = parse_bybit_chain(_fixture("bybit_btc.json"), "BTC", captured_ns=CAPTURED_NS)
    assert len(chain.contracts) == 10
    assert {item.root for item in chain.contracts} == {"BTC-USDT"}


def test_a_bybit_row_gives_its_strike_expiry_right_and_sizes() -> None:
    record = _by_symbol(
        parse_bybit_chain(_fixture("bybit_btc.json"), "BTC", captured_ns=CAPTURED_NS)
    )["BTC-13NOV26-98000-C-USDT"]
    assert record.strike == 98000.0
    assert record.opt_type is OptType.CALL
    assert record.expiry == _utc_ns(date(2026, 11, 13))
    assert record.asset_class is AssetClass.CRYPTO
    assert record.source == BYBIT_SOURCE == "bybit"
    assert record.ask_px == 5.0
    assert record.ask_sz == 6.21
    assert record.open_interest == 21.61
    assert record.last_price == 5.0
    assert record.mark_price == 0.00072719
    assert record.delta == pytest.approx(0.00000082)
    assert record.vega == pytest.approx(0.00010435)
    assert record.theta == pytest.approx(-0.00949199)


def test_a_zero_bybit_bid_is_no_bid_and_its_bid_iv_is_none() -> None:
    record = _by_symbol(
        parse_bybit_chain(_fixture("bybit_btc.json"), "BTC", captured_ns=CAPTURED_NS)
    )["BTC-13NOV26-98000-C-USDT"]
    assert (record.bid_px, record.bid_sz, record.bid_iv) == (None, None, None)
    assert record.ask_iv == 3.0321


def test_bybit_ivs_are_stated_only_when_the_quote_exists() -> None:
    record = _by_symbol(
        parse_bybit_chain(_fixture("bybit_btc.json"), "BTC", captured_ns=CAPTURED_NS)
    )["BTC-13NOV26-71750-P-USDT"]
    assert (record.bid_px, record.bid_sz) == (520.0, 5.54)
    assert record.bid_iv == 0.311
    assert (record.ask_px, record.ask_sz) == (540.0, 5.54)
    assert record.ask_iv == 0.3322


def test_bybit_mark_iv_is_the_row_figure_as_stated() -> None:
    by_symbol = _by_symbol(
        parse_bybit_chain(_fixture("bybit_btc.json"), "BTC", captured_ns=CAPTURED_NS)
    )
    assert by_symbol["BTC-13NOV26-98000-C-USDT"].mark_iv == 1.8193
    assert by_symbol["BTC-15NOV26-75750-C-USDT"].mark_iv == 0.3704


def test_bybit_states_usdt_no_delay_and_the_spot_of_its_first_index_price() -> None:
    chain = parse_bybit_chain(_fixture("bybit_btc.json"), "BTC", captured_ns=CAPTURED_NS)
    assert chain.premium_unit == "USDT"
    assert chain.delay_s == 0
    assert chain.underlying == "BTC"
    assert chain.underlying_price == 71473.66744787
    assert chain.stamped_ns == 1_794_515_838_839 * NS_PER_MS
    by_symbol = _by_symbol(chain)
    assert by_symbol["BTC-15NOV26-75750-C-USDT"].underlying_price == 71448.28907056
    assert by_symbol["BTC-13NOV26-98000-C-USDT"].underlying_price == 71407.7


def test_a_bybit_return_code_of_10001_is_an_empty_chain() -> None:
    body = json.dumps(
        {
            "retCode": 10001,
            "retMsg": "Params Error",
            "result": {},
            "retExtInfo": {},
            "time": 1_794_515_838_839,
        }
    ).encode()
    chain = parse_bybit_chain(body, "BTC", captured_ns=CAPTURED_NS)
    assert chain.contracts == ()
    assert chain.premium_unit is None


def test_another_bybit_return_code_is_a_payload_error() -> None:
    body = json.dumps({"retCode": 10006, "retMsg": "Too many visits", "time": 1}).encode()
    with pytest.raises(LivePayloadError):
        parse_bybit_chain(body, "BTC", captured_ns=CAPTURED_NS)


def test_a_bybit_ticker_of_another_base_coin_is_dropped() -> None:
    doc = _doc("bybit_btc.json")
    other = dict(doc["result"]["list"][0])
    other["symbol"] = "ETH-13NOV26-3000-C-USDT"
    doc["result"]["list"].append(other)
    chain = parse_bybit_chain(json.dumps(doc).encode(), "BTC", captured_ns=CAPTURED_NS)
    assert len(chain.contracts) == 10
    assert "ETH-13NOV26-3000-C-USDT" not in _by_symbol(chain)


# --- parse_deribit_chain ---------------------------------------------------------------------


def test_deribit_mark_iv_in_percent_becomes_a_fraction() -> None:
    by_symbol = _by_symbol(
        parse_deribit_chain(_fixture("deribit_btc.json"), "BTC", captured_ns=CAPTURED_NS)
    )
    assert by_symbol["BTC-13NOV26-84000-P"].mark_iv == pytest.approx(0.697)
    assert by_symbol["BTC-25DEC26-70000-P"].mark_iv == pytest.approx(0.3857)


def test_deribit_records_carry_no_sizes_and_no_greeks() -> None:
    chain = parse_deribit_chain(_fixture("deribit_btc.json"), "BTC", captured_ns=CAPTURED_NS)
    assert len(chain.contracts) == 10
    for item in chain.contracts:
        record = item.record
        assert record.bid_sz is None and record.ask_sz is None
        assert record.bid_iv is None and record.ask_iv is None
        assert record.delta is None and record.gamma is None
        assert record.vega is None and record.theta is None and record.rho is None


def test_deribit_states_its_currency_as_the_premium_unit_and_no_delay() -> None:
    chain = parse_deribit_chain(_fixture("deribit_btc.json"), "BTC", captured_ns=CAPTURED_NS)
    assert chain.premium_unit == "BTC"
    assert chain.delay_s == 0
    assert chain.source == DERIBIT_SOURCE == "deribit"
    assert chain.underlying == "BTC"
    assert {item.record.asset_class for item in chain.contracts} == {AssetClass.CRYPTO}
    assert {item.root for item in chain.contracts} == {"BTC"}


def test_deribit_stamped_ns_is_the_newest_creation_timestamp() -> None:
    chain = parse_deribit_chain(_fixture("deribit_btc.json"), "BTC", captured_ns=CAPTURED_NS)
    newest = max(row["creation_timestamp"] for row in _doc("deribit_btc.json")["result"])
    assert newest == 1_794_515_784_832
    assert chain.stamped_ns == newest * NS_PER_MS


def test_deribit_underlying_price_is_taken_per_row() -> None:
    by_symbol = _by_symbol(
        parse_deribit_chain(_fixture("deribit_btc.json"), "BTC", captured_ns=CAPTURED_NS)
    )
    assert by_symbol["BTC-13NOV26-84000-P"].underlying_price == 71373.74
    assert by_symbol["BTC-25DEC26-70000-P"].underlying_price == 71865.28


def test_a_deribit_null_bid_is_no_bid() -> None:
    record = _by_symbol(
        parse_deribit_chain(_fixture("deribit_btc.json"), "BTC", captured_ns=CAPTURED_NS)
    )["BTC-13NOV26-76000-C"]
    assert record.bid_px is None
    assert record.ask_px == 0.0001


def test_deribit_volume_and_open_interest_come_from_the_row() -> None:
    record = _by_symbol(
        parse_deribit_chain(_fixture("deribit_btc.json"), "BTC", captured_ns=CAPTURED_NS)
    )["BTC-25DEC26-70000-P"]
    assert record.volume == 431.7
    assert record.open_interest == 3526.4
    assert record.last_price == 0.04
    assert record.expiry == _utc_ns(date(2026, 12, 25))
    assert record.opt_type is OptType.PUT


# --- PublishedChains with a fake fetcher -----------------------------------------------------


async def test_cboe_sends_one_get_to_the_delayed_quotes_file_with_no_params() -> None:
    fetch = FakeFetch((200, _fixture("cboe_aapl.json")))
    chain = await _chains(fetch).cboe("AAPL")
    assert len(chain.contracts) == 12
    (request,) = fetch.requests
    assert request.method == "GET"
    assert request.url == CBOE_AAPL_URL
    assert request.params == ()


async def test_a_cboe_access_denied_answer_is_an_empty_chain() -> None:
    fetch = FakeFetch((403, b"<Error><Code>AccessDenied</Code></Error>"))
    chain = await _chains(fetch).cboe("AAPL")
    assert chain.contracts == ()
    assert chain.underlying == "AAPL"
    assert chain.premium_unit == "USD"
    assert chain.delay_s == CBOE_DELAY_S


async def test_an_invalid_cboe_symbol_raises_before_any_request() -> None:
    fetch = FakeFetch()
    with pytest.raises(ValueError):
        await _chains(fetch).cboe("aapl")
    assert fetch.requests == []


async def test_bybit_sends_category_option_and_the_base_coin() -> None:
    fetch = FakeFetch((200, _fixture("bybit_btc.json")))
    chain = await _chains(fetch).bybit("BTC")
    assert len(chain.contracts) == 10
    (request,) = fetch.requests
    assert request.method == "GET"
    assert request.url == BYBIT_URL
    assert request.params == (("category", "option"), ("baseCoin", "BTC"))


async def test_an_invalid_bybit_base_coin_raises_before_any_request() -> None:
    fetch = FakeFetch()
    with pytest.raises(ValueError):
        await _chains(fetch).bybit("b")
    assert fetch.requests == []


async def test_deribit_sends_the_currency_and_kind_option() -> None:
    fetch = FakeFetch((200, _fixture("deribit_btc.json")))
    chain = await _chains(fetch).deribit("BTC")
    assert len(chain.contracts) == 10
    (request,) = fetch.requests
    assert request.method == "GET"
    assert request.url == DERIBIT_URL
    assert request.params == (("currency", "BTC"), ("kind", "option"))


async def test_a_deribit_400_answer_is_an_empty_chain() -> None:
    fetch = FakeFetch((400, b'{"jsonrpc": "2.0", "error": {"code": 13020}}'))
    chain = await _chains(fetch).deribit("BTC")
    assert chain.contracts == ()
    assert chain.underlying == "BTC"
    assert chain.premium_unit == "BTC"


async def test_a_deribit_500_answer_is_a_payload_error() -> None:
    fetch = FakeFetch((500, b"{}"))
    with pytest.raises(LivePayloadError):
        await _chains(fetch).deribit("BTC")


async def test_close_is_safe_with_an_injected_fetcher() -> None:
    chains = _chains(FakeFetch())
    await chains.close()
    await chains.close()
