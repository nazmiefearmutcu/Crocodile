"""The ``tradingview`` universal source: market table, client, sweep, universe, EOD, provider.

Offline throughout. Every scanner answer comes from task 05's R&D probes
(``tests/core/markets/fixtures/tradingview_probe``), the staged 404 body, the trimmed live
answers of 2026-11-11 under ``fixtures/tradingview`` (``universe_*``, ``eod_*``,
``metainfo_turkey.json``, each with its ``.request.json``), or a labelled synthetic file
(``_synthetic`` marks them). The network is forbidden by the ``network_forbidden`` fixture where
a test could reach it.
"""

from __future__ import annotations

import asyncio
import csv
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from crocodile.core.config import Settings
from crocodile.core.markets.calendar import CalendarQuality, calendar_for
from crocodile.core.markets.errors import SanctionedMarketError
from crocodile.core.markets.grammar import load_grammar
from crocodile.core.markets.master import ListingMaster
from crocodile.core.markets.usage import UsageClass
from crocodile.core.ratelimit.token_bucket import TokenBucket
from crocodile.core.schema.enums import AssetClass, PriceAdjustment, SecurityType
from crocodile.core.schema.provenance import Provenance, provenance_fields
from crocodile.core.schema.records import OHLCV, Instrument
from crocodile.core.sink.memory import MemorySink
from crocodile.core.store.catalog import Catalog
from crocodile.core.store.parquet_sink import ParquetSink
from crocodile.equity.providers import factory
from crocodile.equity.providers.base import TransientProviderError, UnsupportedMarketError
from crocodile.equity.reference.registry import InstrumentRegistry
from crocodile.equity.universal import tradingview as tv
from crocodile.equity.universal.base import (
    SourceUnservableError,
    UnservableReason,
    apply_universe,
    contract_violations,
    daily_bar_ts_ns,
)
from crocodile.equity.universal.http import HttpRequest, HttpResponse

PROBES = Path(__file__).resolve().parents[2] / "core" / "markets" / "fixtures" / "tradingview_probe"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tradingview"
ISTANBUL = ZoneInfo("Europe/Istanbul")
EVIDENCE = date(2026, 11, 8)
NS = 1_000_000_000


# ---------------------------------------------------------------- helpers


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _ns(stamp: str) -> int:
    """UTC ISO instant ("...Z") to epoch ns."""
    return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()) * NS


def _utc(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _scan(
    payload: Mapping[str, Any],
    columns: Sequence[str],
    *,
    market: str = "turkey",
    complete: bool | None = None,
) -> tv.ScanResult:
    """A ScanResult built from a recorded or synthetic page, bypassing the HTTP client."""
    rows = tuple(
        tv.ScanRow(ticker=item["s"], values=dict(zip(columns, item["d"], strict=True)))
        for item in payload["data"]
    )
    total = int(payload["totalCount"])
    return tv.ScanResult(
        market=market,
        columns=tuple(columns),
        http_status=200,
        total_count=total,
        rows=rows,
        complete=(len(rows) == total) if complete is None else complete,
    )


def _probe_scan(name: str, columns: Sequence[str] = tv.PROBE_COLUMNS) -> tv.ScanResult:
    """One R&D probe page, a partial read: its totalCount is never reached by its rows."""
    return _scan(_json(PROBES / f"{name}.json"), columns, market=name, complete=False)


def _fixture_scan(name: str, columns: Sequence[str]) -> tv.ScanResult:
    return _scan(_json(FIXTURES / name), columns)


def _row(ticker: str, columns: Sequence[str], values: Sequence[Any]) -> tv.ScanRow:
    return tv.ScanRow(ticker=ticker, values=dict(zip(columns, values, strict=True)))


def _one_row_scan(
    market: str, columns: Sequence[str], rows: Sequence[tv.ScanRow], *, complete: bool = True
) -> tv.ScanResult:
    return tv.ScanResult(
        market=market,
        columns=tuple(columns),
        http_status=200,
        total_count=len(rows),
        rows=tuple(rows),
        complete=complete,
    )


class FakeCalendar:
    """An exchange calendar with explicit sessions: the protocol members the module calls."""

    def __init__(
        self,
        days: Mapping[date, tuple[datetime, datetime]],
        *,
        mic: str = "XIST",
        quality: CalendarQuality = CalendarQuality.LIBRARY,
        tz: ZoneInfo = ISTANBUL,
    ) -> None:
        self._days = dict(days)
        self._mic = mic
        self._quality = quality
        self._tz = tz

    @property
    def mic(self) -> str:
        return self._mic

    @property
    def tz(self) -> ZoneInfo:
        return self._tz

    @property
    def quality(self) -> CalendarQuality:
        return self._quality

    def quality_for(self, d: date) -> CalendarQuality:
        return self._quality

    def is_session(self, d: date) -> bool:
        return d in self._days

    def session_open_utc(self, d: date) -> datetime:
        return self._days[d][0]

    def session_close_utc(self, d: date) -> datetime:
        return self._days[d][1]

    def session_break_utc(self, d: date) -> tuple[datetime, datetime] | None:
        return None

    def sessions(self, start: date, end: date) -> list[date]:
        return sorted(d for d in self._days if start <= d <= end)

    def previous_session(self, d: date) -> date:
        return max(day for day in self._days if day < d)

    def latest_completed_session(self, now_utc: datetime, delay: timedelta) -> date | None:
        for d in sorted(self._days, reverse=True):
            if self._days[d][1] + delay <= now_utc:
                return d
        return None


def _istanbul_days(*days: str) -> dict[date, tuple[datetime, datetime]]:
    """Exchange hours 10:00-18:00 Istanbul, i.e. 07:00Z-15:00Z, for each named day."""
    return {date.fromisoformat(d): (_utc(f"{d}T07:00:00Z"), _utc(f"{d}T15:00:00Z")) for d in days}


def _turkey(*days: str) -> FakeCalendar:
    return FakeCalendar(_istanbul_days(*days))


class FakeScanner:
    """A Fetcher that answers by market, slices scan pages by their range, and counts requests.

    A route is a payload dict (200 JSON), an int (a bare status with the staged 404 body), an
    ``HttpResponse``, or a callable from the request to a response. An unrouted market is a 404.
    """

    def __init__(self, routes: Mapping[str, Any] | None = None) -> None:
        self.routes: dict[str, Any] = dict(routes or {})
        self.requests: list[HttpRequest] = []

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        market = request.url.split("/")[3]
        route = self.routes.get(market, 404)
        if isinstance(route, HttpResponse):
            return route
        if callable(route):
            return route(request)
        if isinstance(route, int):
            return _text(route, (FIXTURES / "ghana_404.txt").read_bytes(), request)
        payload = dict(route)
        body = request.json_body or {}
        if request.method == "POST" and "range" in body and "data" in payload:
            start, end = body["range"]
            payload = {**payload, "data": list(payload["data"])[start:end]}
        return _json_response(payload, request)


def _json_response(payload: Any, request: HttpRequest, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers={"content-type": "application/json"},
        body=json.dumps(payload).encode("utf-8"),
        url=request.url,
    )


def _text(status: int, body: bytes, request: HttpRequest) -> HttpResponse:
    return HttpResponse(
        status=status, headers={"content-type": "text/plain"}, body=body, url=request.url
    )


def _client(fetch: FakeScanner | None = None) -> tv.TradingViewClient:
    bucket = TokenBucket(capacity=1000, refill_rate=1000)
    return tv.TradingViewClient(fetch=fetch or FakeScanner(), bucket=bucket)


def _columns_of(request: HttpRequest) -> tuple[str, ...]:
    assert request.json_body is not None
    return tuple(request.json_body["columns"])


# ------------------------------------------------ Step 3: market table and loaders


def test_every_market_row_carries_evidence():
    markets = tv.load_markets()
    assert markets, "the market table is empty"
    assert [m.market for m in markets] == sorted(m.market for m in markets)
    for market in markets:
        assert market.market in tv.SWEEP_CANDIDATES
        assert len(market.country) == 2 and market.country.isupper()
        assert market.country == tv.SWEEP_CANDIDATES[market.market]
        assert isinstance(market.evidence_date, date)
        if market.http_status is None:
            assert market.notes.startswith("not probed"), market.market


def test_the_staged_measurements_are_in_the_table():
    table = {m.market: m for m in tv.load_markets()}
    expected = {
        "turkey": ("BIST",),
        "nigeria": ("NSENG",),
        "kenya": ("NSEKE",),
        "morocco": ("CSEMA",),
        "egypt": ("EGX",),
        "bangladesh": ("DSEBD",),
        "srilanka": ("CSELK",),
        "vietnam": ("HOSE", "UPCOM"),
        "pakistan": ("PSX",),
        "peru": ("BVL",),
        "colombia": ("BVC",),
        "serbia": ("BELEX",),
    }
    for market, codes in expected.items():
        assert table[market].http_status == 200, market
        assert set(codes) <= set(table[market].exchange_codes), market
    for market in ("ghana", "kazakhstan", "mongolia"):
        assert table[market].http_status == 404, market


def test_sanctioned_markets_are_not_probed_and_not_scannable():
    table = {m.market: m for m in tv.load_markets()}
    scannable = {m.market for m in tv.scannable_markets()}
    for market in ("russia", "belarus", "iran", "syria"):
        assert table[market].http_status is None, market
        assert table[market].notes.startswith("not probed: sanctioned"), market
        assert market not in scannable


def test_markets_resolve_from_a_mic_and_a_country():
    assert [m.market for m in tv.markets_for_mic("XIST")] == ["turkey"]
    assert [m.market for m in tv.markets_for_mic("HSTC")] == ["vietnam"]
    assert [m.market for m in tv.markets_for_country("VN")] == ["vietnam"]


def test_every_grammar_row_names_a_measured_market():
    table = {m.market: m for m in tv.load_markets()}
    grammar = load_grammar("tradingview")
    for row in grammar.rows:
        market = table[row.market]
        assert market.http_status == 200, row.market
        assert row.code in market.exchange_codes, (row.code, row.market)


def test_supported_mics_are_exactly_the_tradingview_grammar_mics():
    grammar = load_grammar("tradingview")
    assert tv.TradingViewProvider.supported_mics == grammar.supported_mics()


def test_the_market_parser_refuses_a_duplicate_market():
    text = (
        "market,country,http_status,total_count,exchange_codes,currencies,evidence_date,notes\n"
        "turkey,TR,200,650,BIST,TRY,2026-11-08,\n"
        "turkey,TR,200,650,BIST,TRY,2026-11-08,\n"
    )
    with pytest.raises(ValueError):
        tv.parse_markets_csv(text)


# ------------------------------------------------------------ Step 4: the client


def test_scan_posts_the_documented_body():
    fetch = FakeScanner({"turkey": _json(PROBES / "turkey.json")})
    client = _client(fetch)
    columns = tv.PROBE_COLUMNS
    result = asyncio.run(client.scan("turkey", columns))
    (request,) = fetch.requests
    assert request.method == "POST"
    assert request.url == tv.SCAN_URL.format(market="turkey")
    assert request.source == "tradingview"
    assert request.json_body == tv.scan_body("turkey", columns, 0, tv.PAGE_SIZE)
    assert request.headers == (), "no Origin, Referer or cookie header may be sent"
    assert result.http_status == 200
    assert [row.ticker for row in result.rows] == ["BIST:A1YEN", "BIST:TSGYO"]


def test_scan_pages_until_the_total_is_reached():
    payload = {
        "totalCount": 5,
        "data": [{"s": f"BIST:T{i}", "d": [f"T{i}", 1.0, "TRY", "BIST"]} for i in range(5)],
    }
    fetch = FakeScanner({"turkey": payload})
    result = asyncio.run(_client(fetch).scan("turkey", tv.PROBE_COLUMNS, page_size=2))
    ranges = [tuple(r.json_body["range"]) for r in fetch.requests]  # type: ignore[index]
    assert ranges == [(0, 2), (2, 4), (4, 6)]
    assert len(result.rows) == 5
    assert result.complete is True


def test_a_short_page_stops_paging_and_marks_the_scan_incomplete():
    payload = {
        "totalCount": 5,
        "data": [{"s": f"BIST:T{i}", "d": [f"T{i}", 1.0, "TRY", "BIST"]} for i in range(3)],
    }
    fetch = FakeScanner({"turkey": payload})
    result = asyncio.run(_client(fetch).scan("turkey", tv.PROBE_COLUMNS, page_size=2))
    assert len(fetch.requests) == 3, "page three answers zero rows and stops the loop"
    assert len(result.rows) == 3
    assert result.complete is False


def test_duplicate_tickers_across_pages_are_kept_once():
    payload = {
        "totalCount": 5,
        "data": [
            {"s": t, "d": [t[5:], 1.0, "TRY", "BIST"]}
            for t in ("BIST:AAAA", "BIST:BBBB", "BIST:BBBB", "BIST:CCCC", "BIST:DDDD")
        ],
    }
    fetch = FakeScanner({"turkey": payload})
    result = asyncio.run(_client(fetch).scan("turkey", tv.PROBE_COLUMNS, page_size=2))
    assert [row.ticker for row in result.rows] == [
        "BIST:AAAA",
        "BIST:BBBB",
        "BIST:CCCC",
        "BIST:DDDD",
    ]


def test_a_row_whose_values_do_not_match_the_columns_is_a_payload_error():
    payload = {"totalCount": 1, "data": [{"s": "BIST:X", "d": ["X", 1.0, "TRY"]}]}
    with pytest.raises(tv.ScanPayloadError):
        asyncio.run(_client(FakeScanner({"turkey": payload})).scan("turkey", tv.PROBE_COLUMNS))


def test_a_page_without_data_is_a_payload_error():
    with pytest.raises(tv.ScanPayloadError):
        asyncio.run(
            _client(FakeScanner({"turkey": {"totalCount": 1}})).scan("turkey", tv.PROBE_COLUMNS)
        )


def test_an_unknown_market_is_a_404_result_not_an_error():
    fetch = FakeScanner({"ghana": 404})
    result = asyncio.run(_client(fetch).scan("ghana", tv.PROBE_COLUMNS))
    assert result.http_status == 404
    assert result.total_count == 0
    assert result.rows == ()
    assert result.complete is False
    assert (FIXTURES / "ghana_404.txt").read_bytes().strip() == b"404 page not found"


def test_429_backs_off_the_shared_budget_and_raises_transient():
    bucket = TokenBucket(capacity=1000, refill_rate=1000)

    def limited(request: HttpRequest) -> HttpResponse:
        return HttpResponse(
            status=429, headers={"retry-after": "7"}, body=b"slow down", url=request.url
        )

    client = tv.TradingViewClient(fetch=FakeScanner({"turkey": limited}), bucket=bucket)
    with pytest.raises(TransientProviderError):
        asyncio.run(client.scan("turkey", tv.PROBE_COLUMNS))
    assert bucket.is_backed_off
    assert bucket.backoff_remaining > 6


def test_5xx_is_transient():
    client = _client(FakeScanner({"turkey": 503}))
    with pytest.raises(TransientProviderError):
        asyncio.run(client.scan("turkey", tv.PROBE_COLUMNS))


def test_400_is_a_request_error_carrying_the_body():
    def rejected(request: HttpRequest) -> HttpResponse:
        return _text(400, b"unknown column: bogus", request)

    with pytest.raises(tv.ScanRequestError, match="unknown column: bogus"):
        asyncio.run(_client(FakeScanner({"turkey": rejected})).scan("turkey", ("bogus",)))


def test_html_is_a_bot_challenge():
    def wall(request: HttpRequest) -> HttpResponse:
        return HttpResponse(
            status=200,
            headers={"content-type": "text/html"},
            body=b"<html>verify you are human</html>",
            url=request.url,
        )

    with pytest.raises(SourceUnservableError) as caught:
        asyncio.run(_client(FakeScanner({"turkey": wall})).scan("turkey", tv.PROBE_COLUMNS))
    assert caught.value.reason is UnservableReason.BOT_CHALLENGE


def test_403_is_forbidden():
    def refused(request: HttpRequest) -> HttpResponse:
        return _text(403, b"forbidden", request)

    with pytest.raises(SourceUnservableError) as caught:
        asyncio.run(_client(FakeScanner({"turkey": refused})).scan("turkey", tv.PROBE_COLUMNS))
    assert caught.value.reason is UnservableReason.FORBIDDEN


def test_metainfo_falls_back_to_post_once():
    def no_get(request: HttpRequest) -> HttpResponse:
        return _text(405, b"method not allowed", request)

    def answered(request: HttpRequest) -> HttpResponse:
        return _json_response({"columns": ["name"]}, request)

    fetch = FakeScanner(
        {
            "turkey": lambda request: (
                no_get(request) if request.method == "GET" else answered(request)
            )
        }
    )
    info = asyncio.run(_client(fetch).metainfo("turkey"))
    assert info == {"columns": ["name"]}
    assert [r.method for r in fetch.requests] == ["GET", "POST"]
    assert fetch.requests[1].json_body == {}


def test_the_default_bucket_comes_from_the_shared_budget(monkeypatch):
    calls: list[tuple[str, str]] = []
    bucket = TokenBucket(capacity=1000, refill_rate=1000)

    def fake_budget(source: str, host: str) -> TokenBucket:
        calls.append((source, host))
        return bucket

    monkeypatch.setattr(tv, "budget", fake_budget)
    fetch = FakeScanner({"turkey": _json(PROBES / "turkey.json")})
    client = tv.TradingViewClient(fetch=fetch)
    asyncio.run(client.scan("turkey", tv.PROBE_COLUMNS))
    assert calls == [("tradingview", "scanner.tradingview.com")]


# ------------------------------------------------------------- Step 5: the sweep


def test_measure_markets_records_codes_currencies_and_examples():
    fetch = FakeScanner({"vietnam": _json(PROBES / "vietnam.json")})
    (measurement,) = asyncio.run(
        tv.measure_markets(
            _client(fetch),
            {"vietnam": "VN"},
            columns=tv.PROBE_COLUMNS,
            evidence_date=EVIDENCE,
        )
    )
    probe = _json(PROBES / "vietnam.json")
    counts: dict[str, int] = {}
    for item in probe["data"]:
        counts[item["d"][3]] = counts.get(item["d"][3], 0) + 1
    assert dict(measurement.exchange_codes) == dict(sorted(counts.items()))
    assert set(measurement.exchange_codes) == {"HNX", "HOSE", "UPCOM"}
    assert measurement.currencies == ("VND",)
    assert measurement.total_count == probe["totalCount"] == 1252
    assert measurement.http_status == 200
    first: dict[str, str] = {}
    for item in probe["data"]:
        first.setdefault(item["d"][3], item["s"])
    assert dict(measurement.examples) == dict(sorted(first.items()))


def test_measure_markets_records_a_404():
    (measurement,) = asyncio.run(
        tv.measure_markets(
            _client(FakeScanner({"ghana": 404})),
            {"ghana": "GH"},
            columns=tv.PROBE_COLUMNS,
            evidence_date=EVIDENCE,
        )
    )
    assert measurement.http_status == 404
    assert measurement.total_count == 0
    assert dict(measurement.exchange_codes) == {}


def test_measure_markets_skips_sanctioned_candidates_without_a_request():
    fetch = FakeScanner()
    (measurement,) = asyncio.run(
        tv.measure_markets(
            _client(fetch),
            {"russia": "RU"},
            columns=tv.PROBE_COLUMNS,
            evidence_date=EVIDENCE,
        )
    )
    assert fetch.requests == []
    assert measurement.http_status is None
    assert measurement.notes == "not probed: sanctioned (RU)"


def test_market_table_rows_round_trip_through_the_loader(tmp_path):
    fetch = FakeScanner(
        {"turkey": _json(PROBES / "turkey.json"), "vietnam": _json(PROBES / "vietnam.json")}
    )
    measurements = asyncio.run(
        tv.measure_markets(
            _client(fetch),
            {"turkey": "TR", "vietnam": "VN", "russia": "RU"},
            columns=tv.PROBE_COLUMNS,
            evidence_date=EVIDENCE,
        )
    )
    rows = tv.market_table_rows(measurements)
    path = tmp_path / "markets.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    loaded = {m.market: m for m in tv.parse_markets_csv(path.read_text(encoding="utf-8"))}
    assert loaded["turkey"].http_status == 200
    assert loaded["turkey"].exchange_codes == ("BIST",)
    assert loaded["vietnam"].exchange_codes == ("HNX", "HOSE", "UPCOM")
    assert loaded["vietnam"].currencies == ("VND",)
    assert loaded["russia"].http_status is None
    assert loaded["russia"].notes == "not probed: sanctioned (RU)"


def test_measurements_are_sorted_by_market():
    measurements = asyncio.run(
        tv.measure_markets(
            _client(FakeScanner({"turkey": _json(PROBES / "turkey.json")})),
            {"vietnam": "VN", "turkey": "TR", "russia": "RU"},
            columns=tv.PROBE_COLUMNS,
            evidence_date=EVIDENCE,
        )
    )
    assert [m.market for m in measurements] == ["russia", "turkey", "vietnam"]


# ------------------------------------------------------------ Step 6: the universe


def test_a_bist_row_becomes_a_listing_observation_and_an_instrument_record():
    """Recorded (universe_turkey.json, sweep of 2026-11-11): BIST:A1YEN becomes A1YEN@XIST."""
    scan = _fixture_scan("universe_turkey.json", tv.UNIVERSE_COLUMNS)
    captured = _captured_ns("universe_turkey")
    snapshot = tv.universe_from_scan(scan, captured_at_ns=captured)
    (obs,) = [o for o in snapshot.observations if o.local_symbol == "A1YEN"]
    assert obs.symbol_raw == "BIST:A1YEN"
    assert (obs.currency, obs.price_scale) == ("TRY", 0)
    assert obs.security_type is SecurityType.CS
    assert obs.name == _description("BIST:A1YEN")
    (record,) = [r for r in snapshot.records if r.symbol == "A1YEN@XIST"]
    assert isinstance(record, Instrument)
    assert record.exchange == "XIST"
    assert record.mic == "XIST"
    assert record.country == "TR"
    assert record.currency == "TRY"
    assert record.price_scale == 0
    assert record.prov is Provenance.NATIVE
    assert record.prov_basis == "native"
    assert record.local_ts == captured
    assert record.source_ts is None
    assert record.asset_class is AssetClass.EQUITY


def test_a_dotted_local_symbol_survives():
    snapshot = tv.universe_from_scan(
        _probe_scan("srilanka"), captured_at_ns=_ns("2026-11-05T16:00:00Z")
    )
    symbols = {record.symbol for record in snapshot.records}
    assert "CERA.N0000@XCOL" in symbols


def test_observations_carry_the_spec_aliases():
    captured = _ns("2026-11-05T16:00:00Z")
    turkey = tv.universe_from_scan(_probe_scan("turkey"), captured_at_ns=captured)
    (a1yen,) = [o for o in turkey.observations if o.symbol_raw == "BIST:A1YEN"]
    assert a1yen.extra_aliases == (("yahoo_chart", "A1YEN.IS"),)
    srilanka = tv.universe_from_scan(_probe_scan("srilanka"), captured_at_ns=captured)
    assert srilanka.observations
    assert all(o.extra_aliases == () for o in srilanka.observations)


def test_one_market_can_hold_two_mics():
    snapshot = tv.universe_from_scan(
        _probe_scan("vietnam"), captured_at_ns=_ns("2026-11-05T16:00:00Z")
    )
    assert set(snapshot.seen) == {"XSTC", "HSTC"}
    rows = _json(PROBES / "vietnam.json")["data"]
    hose = frozenset(item["d"][0] for item in rows if item["d"][3] == "HOSE")
    hanoi = frozenset(item["d"][0] for item in rows if item["d"][3] in ("HNX", "UPCOM"))
    assert "HT1" in hose and "LKW" in hanoi
    assert snapshot.seen["XSTC"] == hose
    assert snapshot.seen["HSTC"] == hanoi


def test_a_market_can_mix_quote_currencies():
    snapshot = tv.universe_from_scan(
        _probe_scan("peru"), captured_at_ns=_ns("2026-11-05T16:00:00Z")
    )
    currencies = {o.local_symbol: o.currency for o in snapshot.observations}
    assert currencies == {"RIMSEGC1": "PEN", "ABXUS": "USD"}


@pytest.mark.parametrize(
    ("type_", "subtype", "expected"),
    [
        ("stock", "common", SecurityType.CS),
        ("stock", "preferred", SecurityType.PFD),
        ("Stock", "Preferred", SecurityType.PFD),
        ("fund", "etf", SecurityType.ETF),
        ("fund", "mutual", SecurityType.FUND),
        ("dr", "adr", SecurityType.ADR),
        ("dr", "gdr", SecurityType.GDR),
        ("structured", None, SecurityType.STRUCTURED),
        ("bond", None, SecurityType.BOND),
        ("warrant", None, SecurityType.WARRANT),
        ("index", None, SecurityType.UNKNOWN),
        (None, None, SecurityType.UNKNOWN),
    ],
)
def test_types_map_to_security_types(type_, subtype, expected):
    assert tv.security_type_for(type_, subtype) is expected


def test_unknown_codes_and_currencies_are_skipped_and_counted():
    columns = tv.UNIVERSE_COLUMNS
    rows = [
        _row("ZZZ:ABC", columns, ["ABC", 1.0, "TRY", "ZZZ", "x", "stock", "common"]),
        _row("BIST:ODD", columns, ["ODD", 1.0, "???", "BIST", "x", "stock", "common"]),
        _row("BIST:", columns, ["", 1.0, "TRY", "BIST", "x", "stock", "common"]),
        _row("BIST:OK", columns, ["OK", 1.0, "TRY", "BIST", "x", "stock", "common"]),
    ]
    snapshot = tv.universe_from_scan(
        _one_row_scan("turkey", columns, rows), captured_at_ns=_ns("2026-11-05T16:00:00Z")
    )
    assert snapshot.skipped == {
        "unknown_exchange_code:ZZZ": 1,
        "unknown_currency:???": 1,
        "malformed_ticker": 1,
    }
    assert [r.symbol for r in snapshot.records] == ["OK@XIST"]


def test_a_complete_scan_says_so_and_an_incomplete_one_does_not():
    columns = tv.PROBE_COLUMNS
    row = [_row("BIST:OK", columns, ["OK", 1.0, "TRY", "BIST"])]
    captured = _ns("2026-11-05T16:00:00Z")
    complete = tv.universe_from_scan(
        _one_row_scan("turkey", columns, row, complete=True), captured_at_ns=captured
    )
    partial = tv.universe_from_scan(
        _one_row_scan("turkey", columns, row, complete=False), captured_at_ns=captured
    )
    assert complete.complete is True
    assert partial.complete is False


def test_universe_records_are_deterministic_and_sorted():
    scan = _fixture_scan("universe_turkey.json", tv.UNIVERSE_COLUMNS)
    captured = _captured_ns("universe_turkey")
    first = tv.universe_from_scan(scan, captured_at_ns=captured)
    second = tv.universe_from_scan(scan, captured_at_ns=captured)
    assert first == second
    symbols = [r.symbol for r in first.records]
    assert symbols == sorted(symbols)


def test_apply_universe_writes_the_scan_into_the_master():
    scan = _fixture_scan("universe_turkey.json", tv.UNIVERSE_COLUMNS)
    snapshot = tv.universe_from_scan(scan, captured_at_ns=_captured_ns("universe_turkey"))
    master = ListingMaster(":memory:")
    try:
        report = apply_universe(master, snapshot)
        assert report.upserted == len(snapshot.observations)
        listing = master.find("XIST", "A1YEN")
        assert listing is not None
        assert listing.currency == "TRY"
        assert master.resolve_alias("tradingview", "BIST:A1YEN") is not None
    finally:
        master.close()


# --------------------------------------------------------------- Step 7: EOD


def _eod_turkey_scan(name: str = "eod_turkey.json") -> tv.ScanResult:
    return _fixture_scan(name, tv.EOD_COLUMNS)


def _captured_ns(name: str) -> int:
    """The capture instant of a recorded fixture: ``_captured_at`` in its ``.request.json``."""
    return _ns(_json(FIXTURES / f"{name}.request.json")["_captured_at"])


def _description(ticker: str) -> str:
    """The recorded description (UNIVERSE_COLUMNS[4]) of one ticker in universe_turkey.json."""
    for item in _json(FIXTURES / "universe_turkey.json")["data"]:
        if item["s"] == ticker:
            return str(item["d"][4])
    raise AssertionError(f"{ticker} is not in the recorded universe page")


_EOD_BASE = (
    "name",
    "exchange",
    "currency",
    "open",
    "high",
    "low",
    "close",
    "volume",
)
_STAMPED_COLUMNS = (
    "name",
    "exchange",
    "currency",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "stamp",
)


def _values_by_symbol(payload: Mapping[str, Any]) -> dict[str, tv.SnapshotValues]:
    out: dict[str, tv.SnapshotValues] = {}
    for item in payload["data"]:
        local = item["s"].split(":", 1)[1]
        open_, high, low, close, volume = item["d"][3:8]
        out[f"{local}@XIST"] = tv.SnapshotValues(
            open=float(open_),
            high=float(high),
            low=float(low),
            close=float(close),
            volume=float(volume),
        )
    return out


def test_the_recorded_evening_snapshot_is_attributed_to_its_session():
    """Recorded (eod_turkey.json) captured 2026-11-11 17:13Z, 20:13 Istanbul, after the 18:00 close.

    Without a row stamp (row_timestamp_field=None) the session is 2026-11-11: all 25 rows are
    emitted with one witness (0.5), and the MIC is attributed to that day.
    """
    calendar = _turkey("2026-11-10", "2026-11-11", "2026-11-12")
    eod = tv.eod_bars_from_scan(
        _eod_turkey_scan("eod_turkey.json"),
        captured_at_ns=_captured_ns("eod_turkey"),
        previous={},
        calendars=lambda mic: calendar,
        row_timestamp_field=None,
    )
    day = date(2026, 11, 11)
    assert len(eod.bars) == 25
    for bar in eod.bars:
        assert isinstance(bar, OHLCV)
        assert bar.source_ts == daily_bar_ts_ns(day)
        assert bar.interval == "1d"
        assert bar.prov is Provenance.DERIVED
        assert bar.prov_basis == "session_snapshot"
        assert bar.prov_confidence == 0.5
        assert bar.adjustment is PriceAdjustment.NONE
        assert bar.asset_class is AssetClass.EQUITY
    assert eod.sessions == {"XIST": day}


def test_the_recorded_turkey_stamps_confirm_the_session_and_score_two_witnesses():
    """With the measured stamp every recorded turkey row's last price is on 2026-11-11."""
    eod = tv.eod_bars_from_scan(
        _eod_turkey_scan("eod_turkey.json"),
        captured_at_ns=_captured_ns("eod_turkey"),
        previous={},
        calendars=lambda mic: _turkey("2026-11-10", "2026-11-11", "2026-11-12"),
    )
    assert len(eod.bars) == 25
    assert {bar.prov_confidence for bar in eod.bars} == {1.0}
    assert {bar.source_ts for bar in eod.bars} == {daily_bar_ts_ns(date(2026, 11, 11))}
    assert eod.sessions == {"XIST": date(2026, 11, 11)}


def test_the_recorded_vietnam_page_skips_its_stale_prices_by_their_stamp():
    """Recorded (eod_vietnam.json, Ho Chi Minh City): four rows last traded on earlier days.

    Their stamps are before the 2026-11-11 session, so the stamp rule skips them as
    row_timestamp_before_session; the other 21 rows are the current session.
    """
    ho_chi_minh = FakeCalendar(
        {
            date.fromisoformat(d): (_utc(f"{d}T02:00:00Z"), _utc(f"{d}T08:00:00Z"))
            for d in ("2026-11-10", "2026-11-11", "2026-11-12")
        },
        mic="XSTC",
        tz=ZoneInfo("Asia/Ho_Chi_Minh"),
    )
    eod = tv.eod_bars_from_scan(
        _eod_turkey_scan("eod_vietnam.json"),
        captured_at_ns=_captured_ns("eod_vietnam"),
        previous={},
        calendars=lambda mic: ho_chi_minh,
    )
    assert eod.skipped == {"row_timestamp_before_session": 4}
    assert len(eod.bars) == 21
    assert {bar.source_ts for bar in eod.bars} == {daily_bar_ts_ns(date(2026, 11, 11))}
    assert set(eod.sessions) <= {"XSTC", "HSTC"}
    assert eod.sessions["XSTC"] == date(2026, 11, 11)


def test_synthetic_rows_on_a_friday_evening_belong_to_friday():
    captured = _ns("2026-11-06T16:00:00Z")
    calendar = _turkey("2026-11-05", "2026-11-06", "2026-11-09")
    eod = tv.eod_bars_from_scan(
        _eod_turkey_scan("eod_turkey_next_day_synthetic.json"),
        captured_at_ns=captured,
        previous={},
        calendars=lambda mic: calendar,
        row_timestamp_field=None,
    )
    assert eod.bars
    assert {bar.source_ts for bar in eod.bars} == {daily_bar_ts_ns(date(2026, 11, 6))}
    assert eod.sessions == {"XIST": date(2026, 11, 6)}


def test_a_snapshot_inside_the_settle_delay_emits_nothing():
    # 11-06 closes at 15:00Z and settles at 15:45Z; at 15:30Z 11-05 is the latest completed
    # session while 11-06 has already opened, so every unstamped row is skipped; the guard runs
    # before the volume rule, so the zero-volume row is counted as session_open too.
    calendar = _turkey("2026-11-05", "2026-11-06", "2026-11-09")
    eod = tv.eod_bars_from_scan(
        _eod_turkey_scan("eod_turkey_next_day_synthetic.json"),
        captured_at_ns=_ns("2026-11-06T15:30:00Z"),
        previous={},
        calendars=lambda mic: calendar,
        row_timestamp_field=None,
    )
    assert eod.bars == ()
    assert eod.skipped == {"session_open": 3}
    assert eod.sessions == {}


def test_a_snapshot_during_a_session_emits_nothing():
    calendar = _turkey("2026-11-05", "2026-11-06", "2026-11-09")
    eod = tv.eod_bars_from_scan(
        _eod_turkey_scan("eod_turkey_next_day_synthetic.json"),
        captured_at_ns=_ns("2026-11-06T10:00:00Z"),
        previous={},
        calendars=lambda mic: calendar,
        row_timestamp_field=None,
    )
    assert eod.bars == ()
    assert eod.skipped == {"session_open": 3}
    assert eod.sessions == {}


def test_a_stamped_row_on_the_session_is_emitted_after_the_next_open_placeholder():
    """Weekday-fallback calendar: the next open is a local-midnight placeholder that has passed.

    Thursday 11-05 is the latest completed session; Friday 11-06 10:00 local is past Friday's
    placeholder open, so an unstamped row is held back, while a row stamped 11-05 18:00 local
    is attributed to Thursday by its own stamp (spec 3.10b: the stamp replaces the next-open
    guard).
    """
    days = {
        date(2026, 11, 5): (_utc("2026-11-04T21:00:00Z"), _utc("2026-11-05T20:00:00Z")),
        date(2026, 11, 6): (_utc("2026-11-05T21:00:00Z"), _utc("2026-11-06T20:00:00Z")),
    }
    calendar = FakeCalendar(days, quality=CalendarQuality.WEEKDAY_FALLBACK)
    stamp_thu = _ns("2026-11-05T15:00:00Z") // NS
    stamp_fri = _ns("2026-11-06T06:30:00Z") // NS
    columns = (*_EOD_BASE, "stamp")
    rows = [
        _row(
            "BIST:STAMP", columns, ["STAMP", "BIST", "TRY", 10.0, 11.0, 9.5, 10.5, 500, stamp_thu]
        ),
        _row("BIST:LATE", columns, ["LATE", "BIST", "TRY", 10.0, 11.0, 9.5, 10.5, 500, stamp_fri]),
        _row("BIST:PLAIN", columns, ["PLAIN", "BIST", "TRY", 10.0, 11.0, 9.5, 10.5, 500, None]),
    ]
    eod = tv.eod_bars_from_scan(
        _one_row_scan("turkey", columns, rows),
        captured_at_ns=_ns("2026-11-06T07:00:00Z"),
        previous={},
        calendars=lambda mic: calendar,
        row_timestamp_field="stamp",
    )
    assert [bar.symbol for bar in eod.bars] == ["STAMP@XIST"]
    (bar,) = eod.bars
    assert bar.source_ts == daily_bar_ts_ns(date(2026, 11, 5))
    assert bar.prov_confidence == 1.0
    assert eod.skipped == {"row_timestamp_after_session": 1, "session_open": 1}
    assert eod.sessions == {"XIST": date(2026, 11, 5)}


def test_zero_or_missing_volume_emits_no_bar():
    columns = _EOD_BASE
    rows = [
        _row("BIST:ZERO", columns, ["ZERO", "BIST", "TRY", 1.0, 1.0, 1.0, 1.0, 0]),
        _row("BIST:NONE", columns, ["NONE", "BIST", "TRY", 1.0, 1.0, 1.0, 1.0, None]),
    ]
    eod = tv.eod_bars_from_scan(
        _one_row_scan("turkey", columns, rows),
        captured_at_ns=_ns("2026-11-06T16:00:00Z"),
        previous={},
        calendars=lambda mic: _turkey("2026-11-05", "2026-11-06", "2026-11-09"),
        row_timestamp_field=None,
    )
    assert eod.bars == ()
    assert eod.skipped == {"no_volume": 2}
    # Both rows passed the calendar guard, so the MIC is attributed even though no bar survived.
    assert eod.sessions == {"XIST": date(2026, 11, 6)}


def test_a_row_identical_to_the_previous_snapshot_is_stale():
    """Synthetic next-day page against the recorded turkey page (row_timestamp_field=None).

    Row 0 equals its recorded values (stale), row 1 changed prices (emitted), row 2 has no volume.
    """
    previous = _values_by_symbol(_json(FIXTURES / "eod_turkey.json"))
    eod = tv.eod_bars_from_scan(
        _eod_turkey_scan("eod_turkey_next_day_synthetic.json"),
        captured_at_ns=_ns("2026-11-06T16:00:00Z"),
        previous=previous,
        calendars=lambda mic: _turkey("2026-11-05", "2026-11-06", "2026-11-09"),
        row_timestamp_field=None,
    )
    assert [bar.symbol for bar in eod.bars] == ["A1YEN@XIST"]
    assert eod.skipped == {"stale_identical": 1, "no_volume": 1}


def test_a_row_stamp_on_the_session_overrides_the_stale_rule_and_scores_two_witnesses():
    """Synthetic stamped page (stamp column): the on-session row equals its recorded values."""
    previous = _values_by_symbol(_json(FIXTURES / "eod_turkey.json"))
    eod = tv.eod_bars_from_scan(
        _fixture_scan("stamped_rows_synthetic.json", _STAMPED_COLUMNS),
        captured_at_ns=_captured_ns("eod_turkey"),
        previous=previous,
        calendars=lambda mic: _turkey("2026-11-10", "2026-11-11", "2026-11-12"),
        row_timestamp_field="stamp",
    )
    on_session = [bar for bar in eod.bars if bar.symbol == "A1CAP@XIST"]
    assert len(on_session) == 1
    assert on_session[0].prov_confidence == 1.0
    assert eod.sessions == {"XIST": date(2026, 11, 11)}


def test_a_row_stamp_before_the_session_is_stale():
    eod = tv.eod_bars_from_scan(
        _fixture_scan("stamped_rows_synthetic.json", _STAMPED_COLUMNS),
        captured_at_ns=_captured_ns("eod_turkey"),
        previous={},
        calendars=lambda mic: _turkey("2026-11-10", "2026-11-11", "2026-11-12"),
        row_timestamp_field="stamp",
    )
    assert "A1YEN@XIST" not in {bar.symbol for bar in eod.bars}
    assert eod.skipped.get("row_timestamp_before_session") == 1


def test_the_real_bist_calendar_attributes_a_friday_evening_to_friday():
    eod = tv.eod_bars_from_scan(
        _eod_turkey_scan("eod_turkey_next_day_synthetic.json"),
        captured_at_ns=_ns("2026-11-06T16:00:00Z"),
        previous={},
        row_timestamp_field=None,
    )
    assert eod.sessions["XIST"] == date(2026, 11, 6)
    assert calendar_for("XIST").quality is CalendarQuality.LIBRARY


def test_the_row_timestamp_field_is_the_measured_one():
    """The EOD row stamp is ``last-price-update-time``, the field the 2026-11-11 recordings confirm.

    Live check (report section ROW_TIMESTAMP_FIELD): turkey's 25 recorded rows are all stamped on
    the 2026-11-11 Istanbul date, at or before the 18:00 close; vietnam's recorded rows are
    stamped 2026-11-11 Ho Chi Minh City except four stale rows stamped 11-05 to 11-10, which the
    stamp rule must skip. The field is epoch seconds. ``update_time`` is not the witness: it is
    row metadata that moves independently of the price (on the stale rows it lands on other dates
    than the price: HNX:ADC has price 2026-11-05 and update 2026-11-09), so a row updated today
    with an old price would be attributed to today.
    """
    assert tv.ROW_TIMESTAMP_FIELD == "last-price-update-time"
    assert tv.EOD_COLUMNS == (
        "name",
        "exchange",
        "currency",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "last-price-update-time",
    )


def test_eod_bars_carry_the_row_unit():
    columns = _EOD_BASE
    rows = [_row("BIST:PENCE", columns, ["PENCE", "BIST", "GBX", 100.0, 110.0, 95.0, 105.0, 900])]
    eod = tv.eod_bars_from_scan(
        _one_row_scan("turkey", columns, rows),
        captured_at_ns=_ns("2026-11-06T16:00:00Z"),
        previous={},
        calendars=lambda mic: _turkey("2026-11-05", "2026-11-06", "2026-11-09"),
        row_timestamp_field=None,
    )
    (bar,) = eod.bars
    assert (bar.currency, bar.price_scale) == ("GBP", 2)


def test_eod_is_deterministic():
    calendar = _turkey("2026-11-05", "2026-11-06")

    def run() -> tv.EodSnapshot:
        return tv.eod_bars_from_scan(
            _eod_turkey_scan(),
            captured_at_ns=_ns("2026-11-05T16:00:00Z"),
            previous={},
            calendars=lambda mic: calendar,
            row_timestamp_field=None,
        )

    assert run() == run()


def test_previous_snapshot_values_reads_the_latest_tradingview_bar(tmp_path):
    def bar(symbol: str, source: str, day: date, close: float) -> OHLCV:
        tail = provenance_fields("native")
        return OHLCV(
            source=source,
            symbol=symbol,
            symbol_raw=symbol,
            source_ts=daily_bar_ts_ns(day),
            local_ts=_ns("2026-11-10T00:00:00Z") + int(close),
            asset_class=AssetClass.EQUITY,
            prov=tail.prov,
            prov_basis=tail.prov_basis,
            prov_confidence=tail.prov_confidence,
            prov_inputs=tail.prov_inputs,
            interval="1d",
            open=1.0,
            high=2.0,
            low=0.5,
            close=close,
            volume=100.0,
        )

    sink = ParquetSink(data_dir=tmp_path, max_buffer_rows=1000, flush_interval_seconds=9999)
    asyncio.run(sink.put(bar("THYAO@XIST", "tradingview", date(2026, 11, 4), 10.0)))
    asyncio.run(sink.put(bar("THYAO@XIST", "tradingview", date(2026, 11, 5), 11.0)))
    asyncio.run(sink.put(bar("THYAO@XIST", "yahoo_chart", date(2026, 11, 6), 99.0)))
    asyncio.run(sink.close())
    values = tv.previous_snapshot_values(Catalog(tmp_path), ["THYAO@XIST"])
    assert values == {
        "THYAO@XIST": tv.SnapshotValues(open=1.0, high=2.0, low=0.5, close=11.0, volume=100.0)
    }


def test_previous_snapshot_values_is_empty_on_an_empty_lake(tmp_path):
    assert tv.previous_snapshot_values(Catalog(tmp_path), ["THYAO@XIST"]) == {}


# ---------------------------------------------- Step 8: provider, registration, gate


def test_the_provider_is_registered_with_its_declared_channels():
    provider = factory.make_provider(
        "tradingview",
        [],
        ["ohlcv", "instrument"],
        MemorySink(),
        InstrumentRegistry(),
        client=_client(),
        settings=Settings(),
    )
    assert isinstance(provider, tv.TradingViewProvider)
    assert provider.supported_channels == frozenset({"ohlcv", "instrument"})
    assert contract_violations(tv.TradingViewProvider) == []
    assert tv.TradingViewProvider.usage_class is UsageClass.INTERNAL
    assert "tradingview" in factory._REGISTRY


def test_run_takes_one_universe_snapshot_and_returns(network_forbidden):
    fetch = FakeScanner({"turkey": _json(FIXTURES / "universe_turkey.json")})
    sink = MemorySink()
    master = ListingMaster(":memory:")
    try:
        provider = tv.TradingViewProvider(
            ["*"],
            ["instrument"],
            sink,
            InstrumentRegistry(),
            client=tv.TradingViewClient(
                fetch=fetch, bucket=TokenBucket(capacity=1000, refill_rate=1000)
            ),
            settings=Settings(),
            master=master,
            clock=lambda: _ns("2026-11-05T16:00:00Z"),
        )
        asyncio.run(provider.run())
        symbols = {record.symbol for record in sink.records if isinstance(record, Instrument)}
        assert "A1YEN@XIST" in symbols
        assert master.find("XIST", "A1YEN") is not None
        assert any(req.json_body and req.json_body.get("columns") for req in fetch.requests)
    finally:
        master.close()


def test_run_for_named_symbols_leaves_the_master_alone():
    fetch = FakeScanner({"turkey": _json(FIXTURES / "universe_turkey.json")})
    sink = MemorySink()
    master = ListingMaster(":memory:")
    try:
        provider = tv.TradingViewProvider(
            ["A1YEN@XIST"],
            ["instrument"],
            sink,
            InstrumentRegistry(),
            client=tv.TradingViewClient(
                fetch=fetch, bucket=TokenBucket(capacity=1000, refill_rate=1000)
            ),
            settings=Settings(),
            master=master,
            clock=lambda: _ns("2026-11-05T16:00:00Z"),
        )
        asyncio.run(provider.run())
        assert [r.symbol for r in sink.records] == ["A1YEN@XIST"]
        assert master.find("XIST", "A1YEN") is None
    finally:
        master.close()


def test_run_for_ohlcv_without_a_lake_refuses_before_any_request():
    fetch = FakeScanner({"turkey": _json(FIXTURES / "eod_turkey.json")})
    provider = tv.TradingViewProvider(
        ["A1YEN@XIST"],
        ["ohlcv"],
        MemorySink(),
        InstrumentRegistry(),
        client=tv.TradingViewClient(
            fetch=fetch, bucket=TokenBucket(capacity=1000, refill_rate=1000)
        ),
        settings=None,
    )
    with pytest.raises(ValueError, match="previous snapshot"):
        asyncio.run(provider.run())
    assert fetch.requests == []


def test_run_for_ohlcv_uses_the_previous_loader():
    previous = _values_by_symbol(_json(FIXTURES / "eod_turkey.json"))
    seen_symbols: list[list[str]] = []

    def loader(symbols: Sequence[str]) -> Mapping[str, tv.SnapshotValues]:
        seen_symbols.append(list(symbols))
        return previous

    sink = MemorySink()
    fetch = FakeScanner({"turkey": _json(FIXTURES / "eod_turkey_next_day_synthetic.json")})
    provider = tv.TradingViewProvider(
        ["A1YEN@XIST"],
        ["ohlcv"],
        sink,
        InstrumentRegistry(),
        client=tv.TradingViewClient(
            fetch=fetch, bucket=TokenBucket(capacity=1000, refill_rate=1000)
        ),
        settings=Settings(),
        previous_loader=loader,
        clock=lambda: _ns("2026-11-06T16:00:00Z"),
    )
    asyncio.run(provider.run())
    assert seen_symbols
    assert [bar.symbol for bar in sink.records if isinstance(bar, OHLCV)] == ["A1YEN@XIST"]


def test_a_sanctioned_symbol_is_refused_before_any_request():
    fetch = FakeScanner()
    with pytest.raises(SanctionedMarketError):
        provider = tv.TradingViewProvider(
            ["SBER@MISX"],
            ["instrument"],
            MemorySink(),
            InstrumentRegistry(),
            client=tv.TradingViewClient(
                fetch=fetch, bucket=TokenBucket(capacity=1000, refill_rate=1000)
            ),
            settings=Settings(),
        )
        asyncio.run(provider.run())
    assert fetch.requests == []


def test_the_star_symbol_passes_the_market_scope_guard(monkeypatch):
    monkeypatch.setattr(tv.TradingViewProvider, "supported_mics", frozenset({"XIST"}))
    provider = factory.make_provider(
        "tradingview",
        ["*"],
        ["instrument"],
        MemorySink(),
        InstrumentRegistry(),
        client=_client(),
        settings=Settings(),
    )
    assert provider.symbols == ["*"]
    with pytest.raises(UnsupportedMarketError):
        factory.make_provider(
            "tradingview",
            ["7203@XJPX"],
            ["instrument"],
            MemorySink(),
            InstrumentRegistry(),
            client=_client(),
            settings=Settings(),
        )


def test_backfill_refuses_and_names_the_snapshot_verbs():
    provider = tv.TradingViewProvider(
        ["THYAO@XIST"],
        ["ohlcv"],
        MemorySink(),
        InstrumentRegistry(),
        client=_client(),
        settings=Settings(),
    )

    async def drain() -> None:
        async for _ in provider.backfill("ohlcv", "THYAO@XIST", 0, 1):
            pass

    with pytest.raises(ValueError, match="collect --sources tradingview"):
        asyncio.run(drain())
