"""The default source bindings of the universal orchestrator (task 14b, steps 13 and 14).

Every test runs under ``network_forbidden``. The smoke tests drive each adapter through the
offline seam its source already tests with (``YahooChartClient(fetch=...)``,
``TradingViewClient(fetch=...)``, a directory's ``fetch=``) and the recorded fixtures of tasks
10-12; nothing is copied.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from crocodile.core.config import Settings
from crocodile.core.markets.master import ListingMaster
from crocodile.core.schema.records import OHLCV
from crocodile.equity.universal import bindings as bindings_module
from crocodile.equity.universal import tradingview as tv
from crocodile.equity.universal import yahoo_chart as yc
from crocodile.equity.universal.base import daily_bar_ts_ns
from crocodile.equity.universal.directories import asx
from crocodile.equity.universal.http import HttpRequest, HttpResponse
from crocodile.equity.universal.orchestrator import SourceBindings, open_orchestrator
from crocodile.equity.universal.order import directory_source_names, universal_source_names
from tests.equity.universal.fakes import (
    FIXED_NS,
    RecordingSink,
    make_observation,
    seed_master,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TURKEY_SESSION = datetime.date(2026, 11, 11)
# After the recorded Yahoo fixture's sessions (2026-11-02..06): an earlier clock drops them as open.
YAHOO_CAPTURE_NS = int(datetime.datetime(2026, 11, 11, 18, 0, tzinfo=datetime.UTC).timestamp()) * (
    1_000_000_000
)


def _json(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


def _captured(name: str) -> int:
    """The capture instant recorded in a fixture's ``.request.json`` (ISO 8601, UTC)."""
    stamp = _json(f"tradingview/{name}.request.json")["_captured_at"]
    moment = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return int(moment.timestamp()) * 1_000_000_000


class _ScannerFake:
    """A TradingView scanner that answers each market from a recorded payload and slices pages."""

    def __init__(self, routes: Mapping[str, Any]) -> None:
        self.routes = dict(routes)
        self.requests: list[HttpRequest] = []

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        market = request.url.split("/")[3]
        payload = dict(self.routes.get(market, {}))
        if not payload:
            return HttpResponse(status=404, headers={}, body=b"", url=request.url)
        body = request.json_body or {}
        if request.method == "POST" and "range" in body and "data" in payload:
            start, end = body["range"]
            payload = {**payload, "data": list(payload["data"])[start:end]}
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(payload).encode("utf-8"),
            url=request.url,
        )


async def _yahoo_fetch(request: HttpRequest) -> HttpResponse:
    body = (FIXTURES / "yahoo_chart" / "THYAO.IS_5d_1d.json").read_bytes()
    return HttpResponse(
        status=200,
        headers={"content-type": "application/json"},
        body=body,
        url=request.url,
    )


async def _no_directory(request: HttpRequest) -> HttpResponse:
    return HttpResponse(status=404, headers={}, body=b"", url=request.url)


def _master(tmp_path: Path) -> ListingMaster:
    master = ListingMaster.for_data_dir(tmp_path)
    seed_master(
        master,
        [
            make_observation(
                "tradingview", "BIST:THYAO", "XIST", "THYAO", currency="TRY", price_scale=0
            ),
        ],
    )
    return master


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path)


async def _close_all(bindings: SourceBindings) -> None:
    for resource in bindings.resources:
        close = getattr(resource, "close", None)
        if close is not None:
            await close()


@pytest.mark.usefixtures("network_forbidden")
def test_default_bindings_cover_every_registered_universal_source(tmp_path: Path) -> None:
    master = _master(tmp_path)
    bindings = bindings_module.default_bindings(
        _settings(tmp_path),
        RecordingSink(),
        master=master,
        clock_ns=lambda: FIXED_NS,
        tradingview_client=tv.TradingViewClient(fetch=_ScannerFake({})),
        yahoo_client=yc.YahooChartClient(fetch=_yahoo_fetch),
        directory_fetch=_no_directory,
    )
    assert set(bindings.history) == {"yahoo_chart"}
    assert set(bindings.universe) >= {"tradingview"} | directory_source_names()
    assert set(bindings.snapshot) == {"tradingview"}
    master.close()


@pytest.mark.usefixtures("network_forbidden")
def test_no_native_source_is_bound(tmp_path: Path) -> None:
    master = _master(tmp_path)
    bindings = bindings_module.default_bindings(
        _settings(tmp_path),
        RecordingSink(),
        master=master,
        clock_ns=lambda: FIXED_NS,
        tradingview_client=tv.TradingViewClient(fetch=_ScannerFake({})),
        yahoo_client=yc.YahooChartClient(fetch=_yahoo_fetch),
        directory_fetch=_no_directory,
    )
    bound = set(bindings.history) | set(bindings.universe) | set(bindings.snapshot)
    assert bound <= universal_source_names()
    master.close()


@pytest.mark.usefixtures("network_forbidden")
def test_snapshot_and_directory_sources_are_never_history_sources(tmp_path: Path) -> None:
    master = _master(tmp_path)
    bindings = bindings_module.default_bindings(
        _settings(tmp_path),
        RecordingSink(),
        master=master,
        clock_ns=lambda: FIXED_NS,
        tradingview_client=tv.TradingViewClient(fetch=_ScannerFake({})),
        yahoo_client=yc.YahooChartClient(fetch=_yahoo_fetch),
        directory_fetch=_no_directory,
    )
    assert "tradingview" not in bindings.history
    assert not set(bindings.history) & directory_source_names()
    master.close()


@pytest.mark.usefixtures("network_forbidden")
def test_each_resource_is_listed_once(tmp_path: Path) -> None:
    master = _master(tmp_path)
    bindings = bindings_module.default_bindings(
        _settings(tmp_path),
        RecordingSink(),
        master=master,
        clock_ns=lambda: FIXED_NS,
        tradingview_client=tv.TradingViewClient(fetch=_ScannerFake({})),
        yahoo_client=yc.YahooChartClient(fetch=_yahoo_fetch),
        directory_fetch=_no_directory,
    )
    ids = [id(resource) for resource in bindings.resources]
    assert len(ids) == len(set(ids))
    assert len(ids) >= 2 + len(directory_source_names())
    master.close()


@pytest.mark.usefixtures("network_forbidden")
async def test_the_yahoo_chart_binding_yields_canonical_daily_bars_and_dividends(
    tmp_path: Path,
) -> None:
    master = _master(tmp_path)
    bindings = bindings_module.default_bindings(
        _settings(tmp_path),
        RecordingSink(),
        master=master,
        clock_ns=lambda: YAHOO_CAPTURE_NS,
        yahoo_client=yc.YahooChartClient(fetch=_yahoo_fetch),
        tradingview_client=tv.TradingViewClient(fetch=_ScannerFake({})),
        directory_fetch=_no_directory,
    )
    listing = master.get("THYAO@XIST")
    assert listing is not None
    history = bindings.history["yahoo_chart"]
    start = int(datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC).timestamp()) * 1_000_000_000
    end = int(datetime.datetime(2026, 12, 31, tzinfo=datetime.UTC).timestamp()) * 1_000_000_000
    try:
        records = [
            record
            async for record in history.history(
                listing, "ohlcv", start, end, captured_ns=YAHOO_CAPTURE_NS
            )
        ]
    finally:
        await _close_all(bindings)
        master.close()
    bars = [record for record in records if isinstance(record, OHLCV)]
    assert bars
    assert {bar.symbol for bar in bars} == {"THYAO@XIST"}
    assert {bar.interval for bar in bars} == {"1d"}


@pytest.mark.usefixtures("network_forbidden")
async def test_the_tradingview_binding_turns_a_market_scan_into_a_universe_snapshot(
    tmp_path: Path,
) -> None:
    master = _master(tmp_path)
    scanner = _ScannerFake({"turkey": _json("tradingview/universe_turkey.json")})
    bindings = bindings_module.default_bindings(
        _settings(tmp_path),
        RecordingSink(),
        master=master,
        clock_ns=lambda: FIXED_NS,
        tradingview_client=tv.TradingViewClient(fetch=scanner),
        yahoo_client=yc.YahooChartClient(fetch=_yahoo_fetch),
        directory_fetch=_no_directory,
    )
    source = bindings.universe["tradingview"]
    try:
        assert source.units_for(frozenset({"XIST"})) == {"turkey": frozenset({"XIST"})}
        snapshot = await source.universe("turkey", captured_ns=_captured("universe_turkey"))
    finally:
        await _close_all(bindings)
        master.close()
    assert snapshot.seen.get("XIST")


@pytest.mark.usefixtures("network_forbidden")
async def test_the_tradingview_binding_returns_only_bars_of_the_sessions_asked(
    tmp_path: Path,
) -> None:
    master = _master(tmp_path)
    scanner = _ScannerFake({"turkey": _json("tradingview/eod_turkey.json")})
    bindings = bindings_module.default_bindings(
        _settings(tmp_path),
        RecordingSink(),
        master=master,
        clock_ns=lambda: FIXED_NS,
        tradingview_client=tv.TradingViewClient(fetch=scanner),
        yahoo_client=yc.YahooChartClient(fetch=_yahoo_fetch),
        directory_fetch=_no_directory,
    )
    source = bindings.snapshot["tradingview"]
    captured = _captured("eod_turkey")
    try:
        asked = await source.snapshot("turkey", {"XIST": TURKEY_SESSION}, captured_ns=captured)
        other = await source.snapshot(
            "turkey", {"XIST": TURKEY_SESSION - datetime.timedelta(days=1)}, captured_ns=captured
        )
    finally:
        await _close_all(bindings)
        master.close()
    assert asked.attributed_mics == frozenset({"XIST"})
    assert asked.records
    assert {record.symbol.split("@")[-1] for record in asked.records} == {"XIST"}
    assert {record.source_ts for record in asked.records} == {daily_bar_ts_ns(TURKEY_SESSION)}
    assert other.attributed_mics == frozenset()
    assert other.records == ()


@pytest.mark.usefixtures("network_forbidden")
async def test_a_directory_binding_returns_its_snapshot(tmp_path: Path) -> None:
    body = (FIXTURES / "directories" / "ASXListedCompanies.csv").read_bytes()

    async def fetch(request: HttpRequest) -> HttpResponse:
        if request.url == asx.ASX_LISTED_URL:
            return HttpResponse(
                status=200, headers={"content-type": "text/csv"}, body=body, url=request.url
            )
        return HttpResponse(status=404, headers={}, body=b"", url=request.url)

    master = _master(tmp_path)
    bindings = bindings_module.default_bindings(
        _settings(tmp_path),
        RecordingSink(),
        master=master,
        clock_ns=lambda: FIXED_NS,
        directory_fetch=fetch,
        tradingview_client=tv.TradingViewClient(fetch=_ScannerFake({})),
        yahoo_client=yc.YahooChartClient(fetch=_yahoo_fetch),
    )
    source = bindings.universe["asx"]
    try:
        assert source.units_for(frozenset({"XASX"})) == {"asx": frozenset({"XASX"})}
        snapshot = await source.universe("asx", captured_ns=FIXED_NS)
    finally:
        await _close_all(bindings)
        master.close()
    assert snapshot.seen.get("XASX")


@pytest.mark.usefixtures("network_forbidden")
async def test_a_scan_bound_requests_one_page_and_reports_an_incomplete_universe(
    tmp_path: Path,
) -> None:
    master = _master(tmp_path)
    scanner = _ScannerFake({"turkey": _json("tradingview/universe_turkey.json")})
    bindings = bindings_module.default_bindings(
        _settings(tmp_path),
        RecordingSink(),
        master=master,
        clock_ns=lambda: FIXED_NS,
        scan_max_rows=2,
        tradingview_client=tv.TradingViewClient(fetch=scanner),
        yahoo_client=yc.YahooChartClient(fetch=_yahoo_fetch),
        directory_fetch=_no_directory,
    )
    try:
        snapshot = await bindings.universe["tradingview"].universe(
            "turkey", captured_ns=_captured("universe_turkey")
        )
    finally:
        await _close_all(bindings)
        master.close()
    assert len(scanner.requests) == 1
    body = scanner.requests[0].json_body or {}
    assert body["range"] == [0, 2]
    assert snapshot.complete is False


@pytest.mark.usefixtures("network_forbidden")
def test_scan_max_rows_must_be_a_positive_int(tmp_path: Path) -> None:
    master = _master(tmp_path)
    try:
        with pytest.raises(ValueError):
            bindings_module.default_bindings(
                _settings(tmp_path),
                RecordingSink(),
                master=master,
                scan_max_rows=0,
            )
    finally:
        master.close()


@pytest.mark.usefixtures("network_forbidden")
def test_open_orchestrator_refuses_scan_max_rows_with_explicit_bindings(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="scan_max_rows"):
        open_orchestrator(
            _settings(tmp_path),
            RecordingSink(),
            bindings=SourceBindings(),
            scan_max_rows=2,
        )
    assert not (tmp_path / "reference" / "listings.sqlite").exists()


@pytest.mark.usefixtures("network_forbidden")
async def test_open_orchestrator_wires_the_default_bindings_offline(tmp_path: Path) -> None:
    orch = open_orchestrator(_settings(tmp_path), RecordingSink(), scan_max_rows=2)
    try:
        assert set(orch.bindings.history) == {"yahoo_chart"}
        assert set(orch.bindings.snapshot) == {"tradingview"}
    finally:
        await orch.aclose()
