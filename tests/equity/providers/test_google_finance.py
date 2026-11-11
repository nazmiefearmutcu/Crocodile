from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crocodile.core.schema.enums import AssetClass, SecurityType, Side
from crocodile.core.schema.provenance import Provenance
from crocodile.core.schema.records import Fundamental, IndexValue, Quote, Record, Trade
from crocodile.core.sink.memory import MemorySink
from crocodile.equity.providers.google_finance.connector import (
    GoogleFinanceProvider,
    get_possible_google_symbols,
    get_spoofed_headers,
    parse_date,
    parse_val_and_unit,
)
from crocodile.equity.reference.registry import InstrumentRegistry


def test_google_finance_helpers() -> None:
    # 1. get_spoofed_headers
    headers = get_spoofed_headers()
    assert "User-Agent" in headers
    assert "Accept" in headers
    assert "Accept-Language" in headers
    assert "Cookie" in headers

    # 2. get_possible_google_symbols
    assert get_possible_google_symbols("AAPL:NASDAQ") == ["AAPL:NASDAQ"]
    assert get_possible_google_symbols("^SPX") == [".INX:INDEXSP"]
    assert get_possible_google_symbols("^IXIC") == [".IXIC:INDEXNASDAQ"]
    assert get_possible_google_symbols("^DJI") == [".DJI:INDEXDJX"]
    possibilities = get_possible_google_symbols("AAPL")
    assert "AAPL:NASDAQ" in possibilities
    assert "AAPL:NYSE" in possibilities

    # 3. parse_date
    assert parse_date("Jun 21, 2026") == "2026-06-21"
    assert parse_date("June 21, 2026") == "2026-06-21"
    assert parse_date("21 Jun 2026") == "2026-06-21"
    assert parse_date("2026-06-21") == "2026-06-21"
    assert parse_date("invalid_date") == "invalid_date"

    # 4. parse_val_and_unit
    # Currency
    val, unit = parse_val_and_unit("$150.25", "Open", listing_currency="USD")
    assert val == 150.25
    assert unit == "USD"

    val, unit = parse_val_and_unit("€120.00", "High", listing_currency="USD")
    assert val == 120.0
    assert unit == "EUR"

    val, unit = parse_val_and_unit("£90.00", "Low", listing_currency="USD")
    assert val == 90.0
    assert unit == "GBP"

    # Percentages
    val, unit = parse_val_and_unit("1.5%", "Dividend", listing_currency="USD")
    assert val == 1.5
    assert unit == "percent"

    val, unit = parse_val_and_unit("invalid%", "Dividend", listing_currency="USD")
    assert val is None
    assert unit == "percent"

    # Multipliers
    val, unit = parse_val_and_unit("2.5T", "Mkt. cap", listing_currency="USD")
    assert val == 2.5e12
    assert unit == "USD"

    val, unit = parse_val_and_unit("2.5B", "Mkt. cap", listing_currency="USD")
    assert val == 2.5e9
    assert unit == "USD"

    val, unit = parse_val_and_unit("2.5M", "Mkt. cap", listing_currency="USD")
    assert val == 2.5e6
    assert unit == "USD"

    val, unit = parse_val_and_unit("2.5K", "Volume", listing_currency="USD")
    assert val == 2500.0
    assert unit == "shares"

    # Counts and ratios
    val, unit = parse_val_and_unit("123", "No. of employees", listing_currency="USD")
    assert val == 123.0
    assert unit == "count"

    val, unit = parse_val_and_unit("15.5", "P/E ratio", listing_currency="USD")
    assert val == 15.5
    assert unit == "ratio"

    val, unit = parse_val_and_unit("invalid_num", "P/E ratio", listing_currency="USD")
    assert val is None
    assert unit == "unknown"

    val, unit = parse_val_and_unit("123", "Unknown Key", listing_currency="USD")
    assert val == 123.0
    assert unit == "unknown"


@pytest.mark.asyncio
async def test_google_finance_list_instruments() -> None:
    registry = InstrumentRegistry()
    sink = MemorySink()
    provider = GoogleFinanceProvider(
        symbols=["AAPL", "^SPX"],
        channels=["trade"],
        out=sink,
        registry=registry,
    )

    insts = await provider.list_instruments()
    assert len(insts) == 2
    assert insts[0].symbol == "AAPL"
    assert insts[0].security_type == SecurityType.CS
    assert insts[1].symbol == "^SPX"
    assert insts[1].security_type == SecurityType.UNKNOWN


@pytest.mark.asyncio
async def test_google_finance_no_ops() -> None:
    registry = InstrumentRegistry()
    sink = MemorySink()
    provider = GoogleFinanceProvider(
        symbols=["AAPL"],
        channels=["trade"],
        out=sink,
        registry=registry,
    )

    # _subscribe is a no-op
    await provider._subscribe(None)
    # normalize returns empty iterable
    assert list(provider.normalize({}, 0)) == []


@pytest.mark.asyncio
async def test_google_finance_scrape_symbol() -> None:
    registry = InstrumentRegistry()
    sink = MemorySink()
    provider = GoogleFinanceProvider(
        symbols=["AAPL"],
        channels=["trade", "quote", "fundamental"],
        out=sink,
        registry=registry,
    )

    # Mock response HTML
    mock_html = """
    <html>
        <body>
            <div class="N6SYTe">$150.25</div>
            <div>
                <div class="SwQK7">Open</div>
                <div class="dO6ijd">$150.00</div>
            </div>
            <div>
                <div class="SwQK7">Mkt. cap</div>
                <div class="dO6ijd">2.5B</div>
            </div>
            <div>
                <div class="SwQK7">Ex-dividend date</div>
                <div class="dO6ijd">Jun 21, 2026</div>
            </div>
        </body>
    </html>
    """

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.text = AsyncMock(return_value=mock_html)

    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

    provider.session = mock_session

    records = await provider._scrape_symbol("AAPL")

    assert len(records) == 3

    # Verify Trade
    trades = [r for r in records if isinstance(r, Trade)]
    assert len(trades) == 1
    assert trades[0].symbol == "AAPL"
    assert trades[0].price == 150.25
    # The scrape observes a price and no size. 1.0 made `sum(amount)` return the poll
    # count as share volume; a structural zero contributes nothing, which is the truth.
    assert trades[0].amount == 0.0
    assert trades[0].prov is Provenance.SYNTHETIC
    assert trades[0].prov_basis == "scraped_last_price"
    assert trades[0].prov_confidence == 0.0

    # No Quote. bid_px == ask_px == last price with size 1.0 a side is a fabricated
    # zero-width two-sided quote, and it shipped as prov=native — so
    # `avg(ask_px - bid_px)` read 0.0 as a measured spread while a consumer watching for
    # `prov != NATIVE` stayed silent. There is no honest Quote to build from one number.
    assert [r for r in records if isinstance(r, Quote)] == []

    # Verify Fundamentals
    fundamentals = [r for r in records if isinstance(r, Fundamental)]
    assert len(fundamentals) == 2

    open_fund = next(f for f in fundamentals if f.tag == "open")
    assert open_fund.val == 150.0
    assert open_fund.unit == "USD"

    mcap_fund = next(f for f in fundamentals if f.tag == "market_cap")
    assert mcap_fund.val == 2.5e9
    assert mcap_fund.unit == "USD"

    # No ex-dividend-date fundamental. The page renders a date and no number, and
    # `Fundamental.val` is required, so the row shipped `val=0.0` under `unit="date"` —
    # one column admitting what `prov=native` denied in another. `SELECT avg(val) …
    # WHERE source='google_finance'` averaged that sentinel in with real facts. There is
    # no record type for a date-valued fact, so nothing is emitted rather than a zero.
    assert [f for f in fundamentals if f.tag == "ex_dividend_date"] == []


@pytest.mark.asyncio
async def test_google_finance_scrape_index() -> None:
    registry = InstrumentRegistry()
    sink = MemorySink()
    provider = GoogleFinanceProvider(
        symbols=["^SPX"],
        channels=["index_value"],
        out=sink,
        registry=registry,
    )

    mock_html = """
    <html>
        <body>
            <div class="N6SYTe">5,000.50</div>
        </body>
    </html>
    """

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.text = AsyncMock(return_value=mock_html)

    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

    provider.session = mock_session

    records = await provider._scrape_symbol("^SPX")

    assert len(records) == 1
    assert isinstance(records[0], IndexValue)
    assert records[0].symbol == "^SPX"
    assert records[0].value == 5000.50


@pytest.mark.asyncio
async def test_google_finance_scrape_failure() -> None:
    registry = InstrumentRegistry()
    sink = MemorySink()
    provider = GoogleFinanceProvider(
        symbols=["AAPL"],
        channels=["trade"],
        out=sink,
        registry=registry,
    )

    # Non-200 response
    mock_resp = MagicMock()
    mock_resp.status = 404

    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

    provider.session = mock_session
    records = await provider._scrape_symbol("AAPL")
    assert records == []

    # Connection failure
    mock_session.get.side_effect = Exception("Connection error")
    records = await provider._scrape_symbol("AAPL")
    assert records == []


@pytest.mark.asyncio
async def test_google_finance_run() -> None:
    registry = InstrumentRegistry()
    sink = MemorySink()
    provider = GoogleFinanceProvider(
        symbols=["AAPL"],
        channels=["trade"],
        out=sink,
        registry=registry,
    )

    trade_rec = Trade(
        source="google_finance",
        symbol="AAPL",
        symbol_raw="AAPL",
        source_ts=None,
        local_ts=123,
        id="",
        price=150.0,
        amount=0.0,
        asset_class=AssetClass.EQUITY,
        side=Side.UNKNOWN,
    )

    async def mock_scrape(sym: str) -> list[Record]:
        provider._running = False  # Terminate run loop immediately
        return [trade_rec]

    provider._scrape_symbol = mock_scrape  # type: ignore[assignment]

    # Patch aiohttp.ClientSession to avoid network requests
    with patch("aiohttp.ClientSession") as mock_session_cls:
        mock_session_inst = MagicMock()
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session_inst)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        await provider.run()

    # The record should be in the sink
    assert sink.records == [trade_rec]


@pytest.mark.asyncio
async def test_google_finance_caching_and_fallbacks() -> None:
    registry = InstrumentRegistry()
    sink = MemorySink()
    provider = GoogleFinanceProvider(
        symbols=["IBM"],
        channels=["trade"],
        out=sink,
        registry=registry,
    )

    # 1st call: AAPL:NASDAQ fails (404), AAPL:NYSE succeeds
    mock_resp_fail = MagicMock()
    mock_resp_fail.status = 404

    mock_resp_success = MagicMock()
    mock_resp_success.status = 200
    html_success = "<html><body><div class='N6SYTe'>$123.45</div></body></html>"
    mock_resp_success.text = AsyncMock(return_value=html_success)

    mock_session = MagicMock()

    # Track the calls to session.get
    called_urls = []

    def mock_get(url: str, *args: Any, **kwargs: Any) -> MagicMock:
        called_urls.append(url)
        resp = MagicMock()
        if "IBM:NASDAQ" in url:
            resp.status = 404
        else:
            resp.status = 200
            html_success = "<html><body><div class='N6SYTe'>$123.45</div></body></html>"
            resp.text = AsyncMock(return_value=html_success)
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=None)
        return resp

    mock_session.get.side_effect = mock_get
    provider.session = mock_session

    records = await provider._scrape_symbol("IBM")
    assert len(records) > 0
    assert provider._resolved_symbol_cache["IBM"] == "IBM:NYSE"
    assert "quote/IBM:NASDAQ" in called_urls[0]
    assert "quote/IBM:NYSE" in called_urls[1]

    # 2nd call: should hit the cache immediately and skip NASDAQ
    called_urls.clear()
    records2 = await provider._scrape_symbol("IBM")
    assert len(records2) > 0
    assert len(called_urls) == 1
    assert "quote/IBM:NYSE" in called_urls[0]

    # Invalidate: mock success fails now
    called_urls.clear()

    def mock_get_new(url: str, *args: Any, **kwargs: Any) -> MagicMock:
        called_urls.append(url)
        resp = MagicMock()
        if "IBM:NYSE" in url:
            # Let the cached symbol fail
            resp.status = 404
        else:
            resp.status = 200
            html_success = "<html><body><div class='N6SYTe'>$123.45</div></body></html>"
            resp.text = AsyncMock(return_value=html_success)
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=None)
        return resp

    mock_session.get.side_effect = mock_get_new
    records3 = await provider._scrape_symbol("IBM")
    # Cached NYSE failed -> fell back to IBM:NASDAQ (which succeeded) -> cached NASDAQ
    assert len(records3) > 0
    assert provider._resolved_symbol_cache["IBM"] == "IBM:NASDAQ"


@pytest.mark.asyncio
async def test_google_finance_currencies_and_timestamps() -> None:
    registry = InstrumentRegistry()
    sink = MemorySink()
    provider = GoogleFinanceProvider(
        symbols=["AAPL"],
        channels=["trade", "fundamental"],
        out=sink,
        registry=registry,
    )

    mock_html = """
    <html>
        <body>
            <div class="YMlKec">€123.45</div>
            <div class="yg51Tc" data-last-normal-market-timestamp="1781913600"></div>
            <div>
                <div class="SwQK7">Open</div>
                <div class="dO6ijd">€120.00</div>
            </div>
        </body>
    </html>
    """

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.text = AsyncMock(return_value=mock_html)

    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

    provider.session = mock_session
    records = await provider._scrape_symbol("AAPL")

    assert len(records) > 0
    trade = next(r for r in records if isinstance(r, Trade))
    assert trade.price == 123.45
    assert trade.source_ts == 1781913600000000000

    # Test "As of" fuzzy parsing
    mock_html_fuzzy = """
    <html>
        <body>
            <div class="fxKbKc">£89.90</div>
            <div>As of Jul 3, 2026, 5:10 PM UTC</div>
        </body>
    </html>
    """
    mock_resp_fuzzy = MagicMock()
    mock_resp_fuzzy.status = 200
    mock_resp_fuzzy.text = AsyncMock(return_value=mock_html_fuzzy)
    mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp_fuzzy)

    records_fuzzy = await provider._scrape_symbol("AAPL")
    assert len(records_fuzzy) > 0
    trade_fuzzy = next(r for r in records_fuzzy if isinstance(r, Trade))
    assert trade_fuzzy.price == 89.90
    assert trade_fuzzy.source_ts is not None


@pytest.mark.asyncio
async def test_google_finance_key_value_misalignment() -> None:
    registry = InstrumentRegistry()
    sink = MemorySink()
    provider = GoogleFinanceProvider(
        symbols=["AAPL"],
        channels=["fundamental"],
        out=sink,
        registry=registry,
    )

    # Key "Mkt. cap" has no value element inside its parent or sibling
    # Key "Open" has a value. Misalignment should not occur.
    mock_html = """
    <html>
        <body>
            <div class="N6SYTe">$100.00</div>
            <div>
                <div class="SwQK7">Mkt. cap</div>
            </div>
            <div>
                <div class="SwQK7">Open</div>
                <div class="dO6ijd">$150.00</div>
            </div>
        </body>
    </html>
    """
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.text = AsyncMock(return_value=mock_html)

    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

    provider.session = mock_session
    records = await provider._scrape_symbol("AAPL")

    fundamentals = [r for r in records if isinstance(r, Fundamental)]
    assert len(fundamentals) == 1
    assert fundamentals[0].tag == "open"
    assert fundamentals[0].val == 150.0


def _quote_page_session() -> MagicMock:
    """A session whose every request answers with a parseable quote page."""
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.text = AsyncMock(
        return_value="<html><body><div class='N6SYTe'>$150.25</div></body></html>"
    )
    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)
    return mock_session


def test_asking_only_for_quotes_is_refused_before_a_single_request_is_made(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A configured channel that never errors and never produces a row looks like a quiet market.

    Following the precedent `FinnhubProvider` set: declare the channels the connector
    can serve and refuse the run when none of the requested ones survives. The reason is
    on `unservable_channels`, so a user who asks for quotes is told why rather than left
    with an empty lake and a poll loop.
    """
    with caplog.at_level("WARNING"), pytest.raises(ValueError, match="no supported channels"):
        GoogleFinanceProvider(
            symbols=["AAPL"],
            channels=["quote"],
            out=MemorySink(),
            registry=InstrumentRegistry(),
        )

    assert any("cannot serve the 'quote' channel" in r.message for r in caplog.records)
    assert any("no bid/ask" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_a_quote_only_symbol_still_resolves_once_instead_of_refetching_forever(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """I5: an empty record list was read as a failed symbol resolution.

    `_scrape_symbol` treats a falsy result as "this is not the right Google symbol" and
    moves to the next of four candidates. Once the `quote` channel stopped emitting a
    record, a *correctly resolved* page returned `[]`, so the cache never populated and
    all four candidates were refetched every poll — forever, at four times the rate,
    returning nothing.
    """
    with caplog.at_level("WARNING"):
        provider = GoogleFinanceProvider(
            symbols=["AAPL"],
            channels=["trade", "quote"],
            out=MemorySink(),
            registry=InstrumentRegistry(),
        )
    session = _quote_page_session()
    provider.session = session

    first = await provider._scrape_symbol("AAPL")
    calls_after_first = session.get.call_count
    second = await provider._scrape_symbol("AAPL")

    assert len(first) == 1  # the trade; the quote is not invented
    assert len(second) == 1
    assert provider._resolved_symbol_cache["AAPL"] == "AAPL:NASDAQ"
    assert calls_after_first == 1, "the first candidate resolved; the other three are not tried"
    assert session.get.call_count == 2, "the second poll hits the cache, not four candidates"

    warnings = [r for r in caplog.records if "cannot serve the 'quote' channel" in r.message]
    assert len(warnings) == 1, "warned once at construction, not once per poll"


@pytest.mark.asyncio
async def test_an_index_asked_for_quotes_is_warned_at_rather_than_left_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """I5: the warning sat inside the `else` of `if is_index`, and fired per scrape.

    So `--symbols ^SPX --channels quote` produced zero rows *and* zero warnings, which is
    the exact outcome the warning existed to avoid. It is now a declaration on the
    provider, emitted once at construction, before a single request is made.
    """
    with caplog.at_level("WARNING"):
        provider = GoogleFinanceProvider(
            symbols=["^SPX"],
            channels=["index_value", "quote"],
            out=MemorySink(),
            registry=InstrumentRegistry(),
        )
    provider.session = _quote_page_session()

    records = await provider._scrape_symbol("^SPX")

    assert [type(r).__name__ for r in records] == ["IndexValue"]
    assert [r for r in caplog.records if "cannot serve the 'quote' channel" in r.message]


@pytest.mark.asyncio
async def test_a_page_that_does_not_resolve_is_still_told_apart_from_one_with_nothing_on_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The distinction the cache rests on, asserted directly."""
    provider = GoogleFinanceProvider(
        symbols=["AAPL"],
        channels=["fundamental"],
        out=MemorySink(),
        registry=InstrumentRegistry(),
    )
    provider.session = _quote_page_session()

    # The page parses, and holds no fundamentals: resolved, nothing to emit.
    assert await provider._scrape_with_g_sym("AAPL", "AAPL:NASDAQ", 1) == []

    missing = MagicMock()
    missing.status = 404
    session = MagicMock()
    session.get.return_value.__aenter__ = AsyncMock(return_value=missing)
    session.get.return_value.__aexit__ = AsyncMock(return_value=None)
    provider.session = session

    assert await provider._scrape_with_g_sym("AAPL", "AAPL:NASDAQ", 1) is None
