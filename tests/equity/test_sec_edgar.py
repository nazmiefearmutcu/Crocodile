"""Tests for the SEC EDGAR provider implementation."""

from __future__ import annotations

import json
import tempfile
import time
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from crocodile.core.errors import FatalConnectorError
from crocodile.core.ratelimit import TokenBucketLimiter
from crocodile.core.schema.enums import FundPeriod
from crocodile.core.schema.records import Filing, Fundamental
from crocodile.equity.providers.sec_edgar import (
    COMPANY_TICKERS_URL,
    SecEdgarClient,
    parse_company_tickers,
)

_APPLE = {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}
"""One good row, for the cases that need the parse to succeed rather than to fail."""


def test_normalize_cik() -> None:
    """Test CIK normalization logic."""
    assert SecEdgarClient.normalize_cik(320193) == "0000320193"
    assert SecEdgarClient.normalize_cik("320193") == "0000320193"
    assert SecEdgarClient.normalize_cik("CIK0000320193") == "0000320193"

    with pytest.raises(ValueError, match="Invalid CIK"):
        SecEdgarClient.normalize_cik("abc")


@pytest.mark.asyncio
async def test_token_bucket_limiter() -> None:
    """Test async TokenBucketLimiter rate limiting behavior."""
    limiter = TokenBucketLimiter(rate=100.0, capacity=2.0)

    # Acquire 2 tokens immediately
    start = time.monotonic()
    await limiter.acquire(1.0)
    await limiter.acquire(1.0)
    assert time.monotonic() - start < 0.05

    # Third token should block/sleep because capacity is 2
    await limiter.acquire(1.0)
    # At rate=100/s, 1 token takes 0.01s.
    assert time.monotonic() - start >= 0.008


@pytest.mark.asyncio
async def test_fetch_ticker_map() -> None:
    """Test fetching and building the ticker mapping."""
    mock_response = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
    }

    client = SecEdgarClient()
    with patch.object(client, "_request_json", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_response

        await client.fetch_ticker_map()

        mock_req.assert_called_once_with(COMPANY_TICKERS_URL)
        assert client._ticker_to_cik["AAPL"] == 320193
        assert client._ticker_to_cik["MSFT"] == 789019
        assert client._cik_to_primary_ticker[320193] == "AAPL"
        assert client._cik_to_primary_ticker[789019] == "MSFT"


def test_the_registrant_index_keeps_the_company_name() -> None:
    """The two ticker dicts discard ``title``, and it is the only legal name in the tree.

    Tiingo's supported-tickers file carries no name at all and OpenFIGI's is a security
    description, so the equity reference merge had nothing to fill ``Instrument.name`` from
    until this stopped being dropped at parse time.
    """
    rows = parse_company_tickers(
        {
            "0": {"cik_str": 320193, "ticker": "aapl", "title": "Apple Inc."},
            "1": {"cik_str": "789019", "ticker": "MSFT", "title": "MICROSOFT CORP"},
        }
    )
    assert [(row.cik, row.ticker, row.title) for row in rows] == [
        (320193, "AAPL", "Apple Inc."),
        (789019, "MSFT", "MICROSOFT CORP"),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="an array rather than the indexed object SEC publishes"),
        pytest.param({}, id="an object with no rows in it at all"),
        pytest.param({"0": "not a row"}, id="a row that is not an object"),
    ],
)
def test_a_payload_with_no_rows_to_read_yields_nothing_rather_than_raising(
    payload: object,
) -> None:
    """A ``TypeError`` out of a parser says nothing about what SEC changed.

    These carry no row-shaped values, so there is no field to name as the one that moved —
    the honest report is an empty index, and the emptiness is loud downstream.
    """
    assert parse_company_tickers(payload) == []


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param({"ticker": "AAPL"}, id="a row with no cik"),
        pytest.param({"cik_str": 320193, "ticker": ""}, id="a row with no ticker"),
        pytest.param({"cik_str": "not a number", "ticker": "X"}, id="an unparseable cik"),
    ],
)
def test_one_row_that_names_nothing_mergeable_is_skipped(bad: dict[str, object]) -> None:
    """The CIK and the ticker are the identity; a row missing either names nothing the
    reference merge can key on, and dropping it costs one registrant out of ten thousand."""
    rows = parse_company_tickers(
        {"0": bad, "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"}}
    )
    assert [row.ticker for row in rows] == ["MSFT"]


def test_a_payload_where_no_row_has_an_identity_names_the_column_that_moved() -> None:
    """Every row malformed is not a bad row, it is a different file.

    Base and M4 both did ``int(item["cik_str"])`` and raised ``KeyError: 'cik_str'``, which
    names the field. Skipping every row instead produced an empty map, and from there three
    quieter failures: ``_resolve_cik('AAPL')`` blamed the caller's ticker with *Unknown
    symbol or CIK: AAPL*, ``ensure_ticker_map`` re-downloaded a ~1 MB index on every call
    because ``{}`` is falsy, and ``get_13f_holdings('CIK0001067983')`` returned ``[]`` with
    no error at all.
    """
    renamed = {
        str(index): {"cik": 320193 + index, "tickerSymbol": "AAPL", "title": "Apple Inc."}
        for index in range(3)
    }
    with pytest.raises(FatalConnectorError) as excinfo:
        parse_company_tickers(renamed)
    message = str(excinfo.value)
    assert "cik_str" in message and "tickerSymbol" in message
    assert COMPANY_TICKERS_URL in message


@pytest.mark.asyncio
async def test_the_registrant_index_is_downloaded_once_even_when_it_yields_nothing() -> None:
    """``ensure_ticker_map`` asked whether the map was non-empty to decide whether it had
    been fetched. A file that legitimately lists no registrants is falsy, so four lookups
    were four downloads of a ~1 MB index under a 10 req/s bucket."""
    client = SecEdgarClient()
    with patch.object(client, "_request_json", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {}
        for _ in range(4):
            await client.ensure_ticker_map()
    assert mock_req.call_count == 1


@pytest.mark.asyncio
async def test_a_failed_fetch_is_still_retried_on_the_next_lookup() -> None:
    """The flag records a completed fetch, not an attempted one — a transport failure must
    not latch the client into an empty map for the rest of its life."""
    client = SecEdgarClient()
    with patch.object(client, "_request_json", new_callable=AsyncMock) as mock_req:
        mock_req.side_effect = [RuntimeError("connection reset"), {"0": _APPLE}]
        with pytest.raises(RuntimeError):
            await client.ensure_ticker_map()
        await client.ensure_ticker_map()
    assert mock_req.call_count == 2
    assert client._ticker_to_cik == {"AAPL": 320193}


@pytest.mark.asyncio
async def test_fetch_company_tickers_reads_the_same_keyless_file_as_the_ticker_map() -> None:
    client = SecEdgarClient()
    with patch.object(client, "_request_json", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"}}
        rows = await client.fetch_company_tickers()
    mock_req.assert_called_once_with(COMPANY_TICKERS_URL)
    assert [row.ticker for row in rows] == ["AAPL"]


@pytest.mark.asyncio
async def test_get_filings() -> None:
    """Test get_filings with mock data."""
    mock_submissions = {
        "cik": "0000320193",
        "entityName": "Apple Inc.",
        "tickers": ["AAPL"],
        "filings": {
            "recent": {
                "accessionNumber": ["0000320193-24-000006"],
                "form": ["10-Q"],
                "filingDate": ["2024-02-01"],
                "reportDate": ["2023-12-30"],
                "primaryDocument": ["aapl-20231230.htm"],
                "isXBRL": [1],
            },
            "files": [],
        },
    }

    client = SecEdgarClient()
    # Pre-populate map to avoid HTTP fetch
    client._ticker_to_cik["AAPL"] = 320193
    client._cik_to_primary_ticker[320193] = "AAPL"

    with patch.object(client, "fetch_submissions", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mock_submissions

        filings = await client.get_filings("AAPL")

        mock_fetch.assert_called_once_with(320193)
        assert len(filings) == 1
        f = filings[0]
        assert isinstance(f, Filing)
        assert f.symbol == "AAPL"
        assert f.form == "10-Q"
        assert f.filing_date == "2024-02-01"
        assert f.report_date == "2023-12-30"
        assert f.accession_number == "0000320193-24-000006"
        assert (
            f.document_url
            == "https://www.sec.gov/Archives/edgar/data/320193/000032019324000006/aapl-20231230.htm"
        )
        assert f.is_xbrl is True


@pytest.mark.asyncio
async def test_get_fundamentals() -> None:
    """Test get_fundamentals and deduplication logic."""
    mock_facts = {
        "cik": 320193,
        "entityName": "Apple Inc.",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "label": "Revenues",
                    "description": "Revenues...",
                    "units": {
                        "USD": [
                            {
                                "val": 1000000.0,
                                "end": "2020-09-30",
                                "fy": 2020,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2020-10-30",
                                "accn": "0000320193-20-000096",
                                "frame": "CY2020",
                            },
                            {
                                "val": 1200000.0,
                                "end": "2020-09-30",
                                "fy": 2020,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2021-10-30",  # Restated later
                                "accn": "0000320193-21-000100",
                                "frame": "CY2020",
                            },
                        ]
                    },
                }
            }
        },
    }

    client = SecEdgarClient()
    client._ticker_to_cik["AAPL"] = 320193
    client._cik_to_primary_ticker[320193] = "AAPL"

    with patch.object(client, "fetch_company_facts", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mock_facts

        # Test with deduplication
        facts = await client.get_fundamentals("AAPL", deduplicate=True)
        assert len(facts) == 1
        assert facts[0].val == 1200000.0

        # Test without deduplication
        facts_all = await client.get_fundamentals("AAPL", deduplicate=False)
        assert len(facts_all) == 2


@pytest.mark.asyncio
async def test_a_fact_with_no_numeric_value_is_skipped_not_filed_as_zero() -> None:
    """`_safe_float` answered 0.0 for an absent or unparseable `val`, at prov=NATIVE.

    `Fundamental.val` is required, so `SELECT sum(val) ... WHERE tag='Revenues'` added
    those zeros in and `avg(val)` was dragged toward zero by facts nobody ever filed —
    with no column on the row separating a reported zero from an unparsed one.
    """
    mock_facts = {
        "cik": 320193,
        "entityName": "Apple Inc.",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {"val": None, "end": "2020-09-30", "accn": "a", "fy": 2020},
                            {"val": "n/a", "end": "2020-12-31", "accn": "b", "fy": 2020},
                            {"val": 0.0, "end": "2021-03-31", "accn": "c", "fy": 2021},
                        ]
                    },
                }
            }
        },
    }

    client = SecEdgarClient()
    client._ticker_to_cik["AAPL"] = 320193
    client._cik_to_primary_ticker[320193] = "AAPL"

    with patch.object(client, "fetch_company_facts", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mock_facts
        facts = await client.get_fundamentals("AAPL", deduplicate=False)

    # The filed zero survives — a reported 0.0 is a measurement. The two the filing did
    # not supply do not, and that is the distinction the old default destroyed.
    assert [f.val for f in facts] == [0.0]


@pytest.mark.asyncio
async def test_parse_company_facts_zip() -> None:
    """Test parsing of bulk ZIP company facts."""
    mock_facts = {
        "cik": 320193,
        "entityName": "Apple Inc.",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "label": "Revenues",
                    "description": "Revenues...",
                    "units": {
                        "USD": [
                            {
                                "val": 1000000.0,
                                "end": "2020-09-30",
                                "fy": 2020,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2020-10-30",
                                "accn": "0000320193-20-000096",
                                "frame": "CY2020",
                            }
                        ]
                    },
                }
            }
        },
    }

    client = SecEdgarClient()
    client._cik_to_primary_ticker[320193] = "AAPL"

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "companyfacts.zip"
        with zipfile.ZipFile(zip_path, "w") as z:
            z.writestr("CIK0000320193.json", json.dumps(mock_facts))

        fundamentals = []
        async for f in client.parse_company_facts_zip(zip_path, deduplicate=True):
            fundamentals.append(f)
        assert len(fundamentals) == 1
        f = fundamentals[0]
        assert isinstance(f, Fundamental)
        assert f.symbol == "AAPL"
        assert f.taxonomy == "us-gaap"
        assert f.tag == "Revenues"
        assert f.val == 1000000.0


@pytest.mark.asyncio
async def test_dedup_keeps_both_windows_of_one_period_end() -> None:
    """A 10-Q states the quarter and the year-to-date against one `end`, in one filing.

    Both rows carry the same `taxonomy`, `tag`, `end`, `fy`, `fp` and `filed` — they are
    on the same page of the same document — and differ only in `start`. With `start` out
    of the dedup key they collapsed onto each other and the survivor was whichever the
    generator yielded last, so `NetIncomeLoss` for the quarter answered 4_308_000_000
    where the filer reported 1_828_000_000: the nine-month figure, 2.36x the quarter,
    filed under the quarter's period with nothing on the row to say so.
    """
    mock_facts = {
        "cik": 320193,
        "entityName": "Apple Inc.",
        "facts": {
            "us-gaap": {
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            {
                                "val": 4308000000.0,
                                "start": "2022-10-02",
                                "end": "2023-07-01",
                                "fy": 2023,
                                "fp": "Q3",
                                "form": "10-Q",
                                "filed": "2023-08-04",
                                "accn": "0000320193-23-000077",
                            },
                            {
                                "val": 1828000000.0,
                                "start": "2023-04-02",
                                "end": "2023-07-01",
                                "fy": 2023,
                                "fp": "Q3",
                                "form": "10-Q",
                                "filed": "2023-08-04",
                                "accn": "0000320193-23-000077",
                            },
                        ]
                    }
                }
            }
        },
    }

    client = SecEdgarClient()
    client._ticker_to_cik["AAPL"] = 320193
    client._cik_to_primary_ticker[320193] = "AAPL"

    with patch.object(client, "fetch_company_facts", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mock_facts
        facts = await client.get_fundamentals("AAPL", deduplicate=True)

    # Before: `[4308000000.0]` — one row, the nine-month figure, under the quarter's key.
    assert sorted(f.val for f in facts) == [1828000000.0, 4308000000.0]
    assert {f.start: f.val for f in facts} == {
        "2023-04-02": 1828000000.0,
        "2022-10-02": 4308000000.0,
    }


@pytest.mark.asyncio
async def test_dedup_does_not_collapse_a_per_share_figure_onto_a_dollar_one() -> None:
    """`_normalize_facts` iterates `units.items()`, so one tag arrives in several units.

    With `unit` out of the dedup key, `EarningsPerShareDiluted` in `USD/shares` and the
    same tag in `pure` shared one key and the survivor was decided by iteration order:
    diluted EPS came back as `0.0126` where the filer reported `$1.26`, a hundredfold
    understatement with nothing on the row to say the unit had changed under it. Both are
    facts the filer stated; neither is a restatement of the other.
    """
    mock_facts = {
        "cik": 320193,
        "entityName": "Apple Inc.",
        "facts": {
            "us-gaap": {
                "EarningsPerShareDiluted": {
                    "units": {
                        "pure": [
                            {
                                "val": 0.0126,
                                "start": "2023-04-02",
                                "end": "2023-07-01",
                                "fy": 2023,
                                "fp": "Q3",
                                "form": "10-Q",
                                "filed": "2023-08-04",
                            }
                        ],
                        "USD/shares": [
                            {
                                "val": 1.26,
                                "start": "2023-04-02",
                                "end": "2023-07-01",
                                "fy": 2023,
                                "fp": "Q3",
                                "form": "10-Q",
                                "filed": "2023-08-04",
                            }
                        ],
                    }
                }
            }
        },
    }

    client = SecEdgarClient()
    client._ticker_to_cik["AAPL"] = 320193
    client._cik_to_primary_ticker[320193] = "AAPL"

    with patch.object(client, "fetch_company_facts", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mock_facts
        facts = await client.get_fundamentals("AAPL", deduplicate=True)

    assert {f.unit: f.val for f in facts} == {"USD/shares": 1.26, "pure": 0.0126}


@pytest.mark.asyncio
async def test_dedup_still_drops_a_restatement_of_the_same_period() -> None:
    """The property the key exists for: same measurement twice, later `filed` wins.

    Widening the key must not turn every restatement into a second row. These two agree
    on unit and on the whole period and differ only in `val` and `filed`, which is what
    a restatement is.
    """
    mock_facts = {
        "cik": 320193,
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "val": 1000000.0,
                                "start": "2019-09-29",
                                "end": "2020-09-30",
                                "fy": 2020,
                                "fp": "FY",
                                "filed": "2020-10-30",
                            },
                            {
                                "val": 1200000.0,
                                "start": "2019-09-29",
                                "end": "2020-09-30",
                                "fy": 2020,
                                "fp": "FY",
                                "filed": "2021-10-30",
                            },
                        ]
                    }
                }
            }
        },
    }

    client = SecEdgarClient()
    client._ticker_to_cik["AAPL"] = 320193
    client._cik_to_primary_ticker[320193] = "AAPL"

    with patch.object(client, "fetch_company_facts", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mock_facts
        facts = await client.get_fundamentals("AAPL", deduplicate=True)

    assert [f.val for f in facts] == [1200000.0]


async def test_a_half_year_fiscal_period_is_kept_rather_than_dropped() -> None:
    """H1 and H2 are periods issuers outside the US report; they used to become ``fp=None``.

    An unrecognised period (``M9``) still has to come through as ``None`` rather than be
    invented, so the test pins both sides of the mapping.
    """
    mock_facts = {
        "cik": 320193,
        "entityName": "Apple Inc.",
        "facts": {
            "ifrs-full": {
                "Revenue": {
                    "label": "Revenue",
                    "description": "Revenue...",
                    "units": {
                        "USD": [
                            {
                                "val": 10.0,
                                "end": "2020-06-30",
                                "fy": 2020,
                                "fp": "H1",
                                "form": "6-K",
                                "filed": "2020-08-01",
                                "accn": "a",
                            },
                            {
                                "val": 20.0,
                                "end": "2020-12-31",
                                "fy": 2020,
                                "fp": "H2",
                                "form": "6-K",
                                "filed": "2021-02-01",
                                "accn": "b",
                            },
                            {
                                "val": 30.0,
                                "end": "2020-09-30",
                                "fy": 2020,
                                "fp": "M9",
                                "form": "10-Q",
                                "filed": "2020-11-01",
                                "accn": "c",
                            },
                        ]
                    },
                }
            }
        },
    }

    client = SecEdgarClient()
    client._ticker_to_cik["AAPL"] = 320193
    client._cik_to_primary_ticker[320193] = "AAPL"

    with patch.object(client, "fetch_company_facts", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mock_facts
        facts = await client.get_fundamentals("AAPL", deduplicate=False)

    assert {f.val: f.fp for f in facts} == {
        10.0: FundPeriod.H1,
        20.0: FundPeriod.H2,
        30.0: None,
    }
