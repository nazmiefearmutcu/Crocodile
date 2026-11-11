"""Unit tests for the Yahoo Finance provider client."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from yfinance.exceptions import YFRateLimitError

from crocodile.core.schema.enums import CorpActionType, OptType
from crocodile.core.schema.records import (
    OHLCV,
    CorporateAction,
    Fundamental,
    InsiderTransaction,
    OptionsChain,
)
from crocodile.equity.providers.yahoo.client import YahooClient


@pytest.fixture
def mock_history_df() -> pd.DataFrame:
    """Fixture for historical bars data."""
    dates = pd.to_datetime(["2026-06-15", "2026-06-16"])
    data = {
        "Open": [100.0, 102.0],
        "High": [105.0, 103.0],
        "Low": [99.0, 101.0],
        "Close": [102.0, 102.5],
        "Volume": [1000.0, 1500.0],
        "Dividends": [0.5, 0.0],
        "Stock Splits": [0.0, 2.0],
    }
    return pd.DataFrame(data, index=dates)


@pytest.fixture
def mock_option_chain() -> MagicMock:
    """Fixture for option chain object."""
    chain = MagicMock()

    calls_data = {
        "contractSymbol": ["AAPL260626C00150000"],
        "lastTradeDate": [pd.Timestamp("2026-06-19 19:00:00", tz=UTC)],
        "strike": [150.0],
        "lastPrice": [5.0],
        "bid": [4.8],
        "ask": [5.2],
        "volume": [100.0],
        "openInterest": [500.0],
        "impliedVolatility": [0.25],
    }
    chain.calls = pd.DataFrame(calls_data)

    puts_data = {
        "contractSymbol": ["AAPL260626P00150000"],
        "lastTradeDate": [pd.Timestamp("2026-06-19 19:05:00", tz=UTC)],
        "strike": [150.0],
        "lastPrice": [3.0],
        "bid": [2.9],
        "ask": [3.1],
        "volume": [80.0],
        "openInterest": [400.0],
        "impliedVolatility": [0.22],
    }
    chain.puts = pd.DataFrame(puts_data)

    # yfinance's `option_chain()` returns `(calls, puts, underlying)`, and the third
    # element is Yahoo's own quote block out of the same `optionChain` response. Modelled
    # here as the real payload's dict rather than left as a MagicMock attribute, because
    # the spot in it is what every moneyness and every solved vol is computed against.
    chain.underlying = {
        "regularMarketPrice": 152.0,
        "regularMarketPreviousClose": 149.0,
        "symbol": "AAPL",
    }

    return chain


@pytest.fixture
def mock_financials() -> pd.DataFrame:
    """Fixture for financials statements."""
    dates = pd.to_datetime(["2025-12-31"])
    data = {
        dates[0]: [1000000.0],
    }
    return pd.DataFrame(data, index=["Total Revenue"])


@pytest.fixture
def mock_insider_df() -> pd.DataFrame:
    """Fixture for insider transactions DataFrame."""
    data = {
        "Insider": ["John Doe"],
        "Position": ["CEO"],
        "Transaction": ["Buy"],
        "Start Date": ["2026-06-16"],
        "Shares": [100.0],
        "Value": [10000.0],
        "Ownership": ["D"],
    }
    return pd.DataFrame(data)


@pytest.mark.asyncio
async def test_fetch_eod_history(mock_history_df: pd.DataFrame) -> None:
    """Test fetching EOD history produces correct OHLCV and CorporateAction records."""
    client = YahooClient()

    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = mock_history_df
        mock_ticker_cls.return_value = mock_ticker

        records = await client.fetch_eod_history("AAPL")

        # Two days of history -> 2 Bars, 1 Dividend, 1 Split = 4 records
        assert len(records) == 4

        bars = [r for r in records if isinstance(r, OHLCV)]
        corp_actions = [r for r in records if isinstance(r, CorporateAction)]

        assert len(bars) == 2
        assert len(corp_actions) == 2

        assert bars[0].symbol == "AAPL"
        assert bars[0].open == 100.0
        assert bars[0].close == 102.0
        assert bars[0].volume == 1000.0

        div = next(c for c in corp_actions if c.type == CorpActionType.DIVIDEND_CASH)
        assert div.ex_date == "2026-06-15"
        assert div.value == 0.5

        split = next(c for c in corp_actions if c.type == CorpActionType.SPLIT)
        assert split.ex_date == "2026-06-16"
        assert split.value == 2.0

    await client.close()


@pytest.mark.asyncio
async def test_fetch_intraday_bars(mock_history_df: pd.DataFrame) -> None:
    """Test fetching intraday bars produces correct OHLCV records."""
    client = YahooClient()

    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = mock_history_df
        mock_ticker_cls.return_value = mock_ticker

        records = await client.fetch_intraday_bars("AAPL", interval="1m")

        assert len(records) == 2
        assert all(isinstance(r, OHLCV) for r in records)
        assert records[0].open == 100.0
        assert records[1].close == 102.5

    await client.close()


@pytest.mark.asyncio
async def test_fetch_option_chain(mock_option_chain: MagicMock) -> None:
    """Test fetching option chains produces correct OptionsChain records."""
    client = YahooClient()

    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker.options = ("2026-06-26",)
        mock_ticker.option_chain.return_value = mock_option_chain
        mock_ticker_cls.return_value = mock_ticker

        records = await client.fetch_option_chain("AAPL")

        assert len(records) == 2
        assert all(isinstance(r, OptionsChain) for r in records)

        call = next(r for r in records if r.opt_type == OptType.CALL)
        assert call.symbol == "AAPL260626C00150000"
        assert call.strike == 150.0
        assert call.last_price == 5.0
        assert call.bid_px == 4.8
        assert call.ask_px == 5.2
        assert call.mark_iv == 0.25

        # Yahoo publishes a date; the record wants UTC epoch nanoseconds. Midnight UTC
        # is the instant, so the date the feed gave comes back by the same UTC
        # truncation the lake partitions with — see `_expiry_to_ns` for why not a close.
        assert call.expiry == 1782432000000000000
        assert (
            datetime.fromtimestamp(call.expiry / 1e9, tz=UTC).strftime("%Y-%m-%d")
            == "2026-06-26"
        )
        # The spot comes off the same payload as the strikes — `regularMarketPrice` in the
        # quote block `option_chain()` returns third — so the moneyness a surface reports
        # is a ratio of two numbers from one instant. `regularMarketPreviousClose` sits
        # beside it in the same block and is deliberately not read: it is yesterday's
        # close, a different measurement, and there is no column to say which one it was.
        assert call.underlying_price == 152.0

        put = next(r for r in records if r.opt_type == OptType.PUT)
        assert put.symbol == "AAPL260626P00150000"
        assert put.last_price == 3.0
        assert put.mark_iv == 0.22
        assert put.underlying_price == 152.0

    await client.close()


@pytest.mark.asyncio
async def test_a_chain_payload_with_no_quote_block_leaves_the_spot_unstated(
    mock_option_chain: MagicMock,
) -> None:
    """`None` means the feed did not publish a spot, which is not a price of zero.

    yfinance returns `underlying=None` when Yahoo answers with no quote block, and the
    honest record of that is an absent price. The downstream cost is visible rather than
    silent: the surface reports a `NaN` moneyness and an `unavailable` vol for any contract
    whose IV would have had to be solved, instead of inverting a mid against a spot
    somebody assumed.
    """
    client = YahooClient()
    mock_option_chain.underlying = None

    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker.options = ("2026-06-26",)
        mock_ticker.option_chain.return_value = mock_option_chain
        mock_ticker_cls.return_value = mock_ticker

        records = await client.fetch_option_chain("AAPL")
        assert records
        assert all(record.underlying_price is None for record in records)

    await client.close()


@pytest.mark.asyncio
async def test_a_non_positive_spot_is_read_as_unpublished(
    mock_option_chain: MagicMock,
) -> None:
    """A zero in `regularMarketPrice` is Yahoo saying nothing, not the stock being free.

    Carried through as a price it would divide every strike by zero on the way to a
    moneyness, and would be handed to the IV solver as the underlying's value.
    """
    client = YahooClient()
    mock_option_chain.underlying = {"regularMarketPrice": 0.0}

    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker.options = ("2026-06-26",)
        mock_ticker.option_chain.return_value = mock_option_chain
        mock_ticker_cls.return_value = mock_ticker

        records = await client.fetch_option_chain("AAPL")
        assert records
        assert all(record.underlying_price is None for record in records)

    await client.close()


def test_an_expiry_date_widens_to_midnight_utc_and_back() -> None:
    """The conversion is total in one direction only, so the other one is pinned.

    A date has to become an instant to live in an `int` field, and the instant chosen is
    the only one from which the date is recoverable by the UTC truncation everything else
    here uses. A malformed date raises rather than landing an option on the wrong day of
    a term structure.
    """
    from crocodile.equity.providers.yahoo.client import _expiry_to_ns

    ns = _expiry_to_ns("2026-06-18")
    assert ns == 1781740800000000000
    assert ns % 86_400_000_000_000 == 0, "midnight UTC, so a UTC date truncation is exact"
    assert datetime.fromtimestamp(ns / 1e9, tz=UTC).strftime("%Y-%m-%d") == "2026-06-18"

    with pytest.raises(ValueError):
        _expiry_to_ns("18/06/2026")


@pytest.mark.asyncio
async def test_fetch_financial_statements(mock_financials: pd.DataFrame) -> None:
    """Test fetching financials produces Fundamental records."""
    client = YahooClient()

    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker.financials = mock_financials
        mock_ticker.quarterly_financials = pd.DataFrame()
        mock_ticker.balance_sheet = pd.DataFrame()
        mock_ticker.quarterly_balance_sheet = pd.DataFrame()
        mock_ticker.cashflow = pd.DataFrame()
        mock_ticker.quarterly_cashflow = pd.DataFrame()
        mock_ticker.info = {"financialCurrency": "USD"}
        mock_ticker_cls.return_value = mock_ticker

        records = await client.fetch_financial_statements("AAPL")

        assert len(records) == 1
        fund = records[0]
        assert isinstance(fund, Fundamental)
        assert fund.symbol == "AAPL"
        assert fund.tag == "Total Revenue"
        assert fund.val == 1000000.0
        assert fund.end == "2025-12-31"
        assert fund.start == "2024-12-31"  # Derived 1 year back for annual duration
        assert fund.fp == "FY"
        assert fund.form == "10-K"
        assert fund.unit == "USD"

    await client.close()


@pytest.mark.asyncio
async def test_fetch_insider_transactions(mock_insider_df: pd.DataFrame) -> None:
    """Test fetching insider transactions produces correct InsiderTransaction records."""
    client = YahooClient()

    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker.insider_transactions = mock_insider_df
        mock_ticker_cls.return_value = mock_ticker

        records = await client.fetch_insider_transactions("AAPL")

        assert len(records) == 1
        txn = records[0]
        assert isinstance(txn, InsiderTransaction)
        assert txn.insider_name == "John Doe"
        assert txn.position == "CEO"
        assert txn.transaction_type == "Buy"
        assert txn.transaction_date == "2026-06-16"
        assert txn.shares == 100.0
        assert txn.value == 10000.0
        assert txn.price == 100.0  # Computed value / shares
        assert txn.ownership == "D"

    await client.close()


@pytest.mark.asyncio
async def test_rate_limiting_and_session_reset() -> None:
    """Test that RateLimitError triggers backoff and resets requests session."""
    client = YahooClient(backoff_delay=0.01)

    call_count = 0
    original_session = client.session

    def flaky_call(*args: Any, **kwargs: Any) -> pd.DataFrame:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise YFRateLimitError()
        return pd.DataFrame()

    with (
        patch("yfinance.Ticker") as mock_ticker_cls,
        patch.object(client, "_check_health", AsyncMock(return_value=True)),
    ):
        mock_ticker = MagicMock()
        mock_ticker.history = flaky_call
        mock_ticker_cls.return_value = mock_ticker

        # This should hit rate limit on first call, trigger reset, then succeed on second call
        records = await client.fetch_intraday_bars("AAPL", "1d")

        assert call_count == 2
        assert len(records) == 0
        assert client.session is not original_session

    await client.close()


def test_reset_session_clears_yfdata_crumb() -> None:
    """reset_session must clear YfData singleton crumb/cookie (yfinance 1.4.x)."""
    client = YahooClient()
    mock_yf_data = MagicMock()
    mock_yf_data._crumb = "stale"
    mock_yf_data._cookie = "stale-cookie"
    mock_yf_data._cookie_lock = MagicMock()
    mock_yf_data._cookie_lock.__enter__ = MagicMock(return_value=None)
    mock_yf_data._cookie_lock.__exit__ = MagicMock(return_value=False)

    with patch("yfinance.data.YfData", return_value=mock_yf_data):
        client.reset_session()

    assert mock_yf_data._crumb is None
    assert mock_yf_data._cookie is None
    client.session.close()


@pytest.mark.asyncio
async def test_fetch_option_chain_handles_none_calls_puts() -> None:
    """yfinance can return calls=None, puts=None for empty expiry payloads."""
    client = YahooClient()
    empty_chain = MagicMock()
    empty_chain.calls = None
    empty_chain.puts = None

    async def exec_side_effect(func: Any, *args: Any, **kwargs: Any) -> Any:
        # Invoke the callable so option_chain path returns empty_chain
        return func(client.session)

    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker.options = ["2026-07-18"]
        mock_ticker.option_chain.return_value = empty_chain
        mock_ticker_cls.return_value = mock_ticker

        with patch.object(
            client, "_execute_yf_call", new_callable=AsyncMock, side_effect=exec_side_effect
        ):
            records = await client.fetch_option_chain("AAPL")
            assert records == []

    await client.close()
