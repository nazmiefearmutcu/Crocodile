"""Offline tests for scripts/measure_global_equity.py (task 17). The live run is never a test."""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import pathlib
import re
import types
from typing import Any

import aiohttp
import pytest
from scripts.measure_global_equity import (
    DEFAULT_COUNTRIES,
    DEFAULT_MAX_REQUESTS,
    EXCLUDED_HOSTS,
    HOST_SOURCES,
    BudgetExceeded,
    HostClosed,
    HostNotAllowed,
    HttpLedger,
    RequestRecord,
    _cell,
    _data_dir_cleanup,
    _summary,
    acceptance_summary,
    build_plan,
    choose_samples,
    endpoint_key,
    install_http_ledger,
    main,
    percentile,
    recorded_query,
    render_markdown,
    run_phase_call,
    scrub_environment,
    source_for_host,
    summarize_sources,
)

from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.symbols import parse_listing_symbol

REPO = pathlib.Path(__file__).resolve().parents[2]
UNIVERSAL = REPO / "src" / "crocodile" / "equity" / "universal"
SCRIPT = REPO / "scripts" / "measure_global_equity.py"
FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "result_min.json"
YAHOO = "https://query2.finance.yahoo.com/v8/finance/chart/THYAO.IS"
YAHOO_B = "https://query2.finance.yahoo.com/v8/finance/chart/GARAN.IS"
TRADINGVIEW = "https://scanner.tradingview.com/turkey/scan"
NASDAQ = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"


class FakeClock:
    """A list-backed clock: sleeping advances it and records the duration."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _ledger(clock: FakeClock, **kwargs: Any) -> HttpLedger:
    return HttpLedger(
        allowed_hosts=frozenset(HOST_SOURCES), clock=clock, sleep=clock.sleep, **kwargs
    )


async def _closed_after(url: str, status: int, content_type: str | None) -> bool:
    clock = FakeClock()
    ledger = _ledger(clock)
    seq = await ledger.admit("GET", url)
    ledger.finish(seq, status=status, content_type=content_type, user_agent=None)
    try:
        await ledger.admit("GET", url)
    except HostClosed:
        return True
    return False


def test_an_endpoint_is_method_host_and_path_without_the_query() -> None:
    """Two Yahoo URLs that differ only in period1 share one endpoint key, host lowercased."""
    first = endpoint_key("GET", f"{YAHOO}?period1=1")
    second = endpoint_key(
        "get", "https://QUERY2.finance.yahoo.com/v8/finance/chart/THYAO.IS?period1=2"
    )
    assert first == second == ("GET", "query2.finance.yahoo.com", "/v8/finance/chart/THYAO.IS")


async def test_the_fourth_request_to_one_endpoint_is_refused_and_never_sent() -> None:
    """Three admits pass; the fourth is refused for its endpoint and is not counted as sent."""
    clock = FakeClock()
    ledger = _ledger(clock)
    for _ in range(3):
        await ledger.admit("GET", YAHOO)
    with pytest.raises(BudgetExceeded) as refused:
        await ledger.admit("GET", YAHOO)
    assert refused.value.reason == "budget_endpoint"
    summary = ledger.budget_summary()
    assert summary["requests_sent"] == 3
    assert [row["reason"] for row in summary["refusals"]] == ["budget_endpoint"]


async def test_the_run_wide_cap_refuses_once_reached() -> None:
    """With max_requests=2 across two endpoints, the third request is refused as budget_total."""
    clock = FakeClock()
    ledger = _ledger(clock, max_requests=2)
    await ledger.admit("GET", YAHOO)
    await ledger.admit("GET", TRADINGVIEW)
    with pytest.raises(BudgetExceeded) as refused:
        await ledger.admit("GET", NASDAQ)
    assert refused.value.reason == "budget_total"


async def test_requests_to_one_host_are_spaced_and_other_hosts_are_not() -> None:
    """A second request to one host 0.2 s later waits 0.8 s; a request to another host does not."""
    clock = FakeClock()
    ledger = _ledger(clock)
    await ledger.admit("GET", YAHOO)
    clock.now = 0.2
    await ledger.admit("GET", TRADINGVIEW)
    assert clock.sleeps == []
    await ledger.admit("GET", YAHOO_B)
    assert clock.sleeps == [pytest.approx(0.8)]


async def test_a_host_outside_the_allowlist_is_refused() -> None:
    """A host that is not allowlisted is refused and nothing is sent to it."""
    clock = FakeClock()
    ledger = _ledger(clock)
    with pytest.raises(HostNotAllowed):
        await ledger.admit("GET", "https://example.org/x")
    assert ledger.budget_summary()["requests_sent"] == 0
    assert [row["reason"] for row in ledger.refusals] == ["host_not_allowed"]


async def test_a_host_is_cooled_after_a_429_and_closed_after_the_second() -> None:
    """The first 429 makes the next request wait about 60 s; the second closes the host."""
    clock = FakeClock()
    ledger = _ledger(clock)
    seq = await ledger.admit("GET", YAHOO)
    ledger.finish(seq, status=429, content_type=None, user_agent=None)
    seq = await ledger.admit("GET", YAHOO)
    assert clock.sleeps == [pytest.approx(60.0)]
    ledger.finish(seq, status=429, content_type=None, user_agent=None)
    with pytest.raises(HostClosed):
        await ledger.admit("GET", YAHOO)


async def test_a_403_a_redirect_wall_or_a_bot_page_closes_the_host_at_once() -> None:
    """403 closes; a 302 from Yahoo closes; text/html from TradingView closes; not a directory."""
    assert await _closed_after(YAHOO, 403, "text/plain")
    assert await _closed_after(YAHOO, 302, None)
    assert await _closed_after(TRADINGVIEW, 200, "text/html; charset=utf-8")
    assert not await _closed_after(NASDAQ, 200, "text/html")


async def test_a_redirect_to_a_host_outside_the_allowlist_is_refused() -> None:
    """A redirect hop to consent.yahoo.com is refused before it is followed."""
    clock = FakeClock()
    ledger = _ledger(clock)
    seq = await ledger.admit("GET", YAHOO)
    with pytest.raises(HostNotAllowed):
        await ledger.admit("GET", "https://consent.yahoo.com/collectConsent", redirect_of=seq)


def test_only_allowlisted_query_keys_are_recorded() -> None:
    """Of period1 and crumb, only period1 is recorded; crumb values never reach the record."""
    assert recorded_query(f"{YAHOO}?period1=1&crumb=x") == {"period1": "1"}


def test_hosts_are_attributed_to_sources_and_unknown_hosts_say_so() -> None:
    """A TradingView host maps to tradingview; an unknown host is unattributed."""
    assert source_for_host("scanner.tradingview.com") == "tradingview"
    assert source_for_host("example.org") == "unattributed"


def test_every_host_a_universal_source_names_is_allowlisted_or_excluded_with_a_reason() -> None:
    """Every https host named in the universal modules is allowlisted or excluded with a reason."""
    hosts: set[str] = set()
    for path in UNIVERSAL.rglob("*.py"):
        hosts |= set(re.findall(r"https?://([A-Za-z0-9.-]+)", path.read_text(encoding="utf-8")))
    assert hosts <= set(HOST_SOURCES) | set(EXCLUDED_HOSTS), hosts - set(HOST_SOURCES)
    assert all(reason.strip() for reason in EXCLUDED_HOSTS.values())


def test_every_universal_source_speaks_aiohttp_so_the_ledger_sees_its_traffic() -> None:
    """No universal module imports a second HTTP client that the ledger would not trace."""
    forbidden = {
        "requests",
        "httpx",
        "curl_cffi",
        "pycurl",
        "yfinance",
        "urllib.request",
        "http.client",
    }
    offenders: list[str] = []
    for path in UNIVERSAL.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
            for name in names:
                if any(name == item or name.startswith(item + ".") for item in forbidden):
                    offenders.append(f"{path.name}:{name}")
    assert offenders == []


async def test_the_ledger_is_installed_on_every_new_client_session_and_removed_after() -> None:
    """Every session in the block is traced; after exit, even by exception, none is."""
    ledger = HttpLedger(allowed_hosts=frozenset(HOST_SOURCES))
    with install_http_ledger(ledger):
        async with aiohttp.ClientSession() as traced:
            assert any(ledger.owns(config) for config in traced.trace_configs)
    with pytest.raises(ValueError):
        with install_http_ledger(ledger):
            raise ValueError("leave the block by exception")
    async with aiohttp.ClientSession() as plain:
        assert not any(ledger.owns(config) for config in plain.trace_configs)


def test_the_plan_covers_the_twelve_default_countries_with_their_registered_mics() -> None:
    """Twelve plans; mics are the registered MICs sorted; each anchor's MIC is the primary."""
    plans = build_plan(DEFAULT_COUNTRIES)
    assert len(plans) == 12
    registry = ExchangeRegistry()
    for plan in plans:
        mics = sorted(info.mic for info in registry.by_country(plan.country))
        assert list(plan.mics) == mics
        assert plan.primary_mic in plan.mics
        for anchor in plan.anchors:
            _local, mic = parse_listing_symbol(anchor)
            if plan.country != "US":
                assert mic == plan.primary_mic, anchor


def test_the_plan_refuses_a_sanctioned_or_unknown_country() -> None:
    """A sanctioned country and an unknown country are both refused with ValueError."""
    with pytest.raises(ValueError, match="sanction"):
        build_plan(["RU"])
    with pytest.raises(ValueError):
        build_plan(["XX"])


def test_samples_take_anchors_first_then_the_primary_mic_in_symbol_order() -> None:
    """Active anchors come first; an inactive anchor is skipped; then the primary MIC by symbol."""
    plan = build_plan(["TR"])[0]
    listings = [
        types.SimpleNamespace(symbol="GARAN@XIST", mic="XIST", status="inactive"),
        types.SimpleNamespace(symbol="BIMAS@XIST", mic="XIST", status="active"),
        types.SimpleNamespace(symbol="THYAO@XIST", mic="XIST", status="active"),
        types.SimpleNamespace(symbol="AKBNK@XIST", mic="XIST", status="active"),
    ]
    assert choose_samples(plan, listings, 3) == ("THYAO@XIST", "AKBNK@XIST", "BIMAS@XIST")


def test_the_environment_is_scrubbed_of_crocodile_variables_and_values_are_never_echoed() -> None:
    """Crocodile, crypcodile and stockodile variables are removed; only their names are returned."""
    env = {"CROCODILE_TIINGO_API_KEY": "secret", "STOCKODILE_X": "y", "PATH": "/bin"}
    removed = scrub_environment(env)
    assert sorted(removed) == ["CROCODILE_TIINGO_API_KEY", "STOCKODILE_X"]
    assert env == {"PATH": "/bin"}
    assert "secret" not in repr(removed)


def test_a_non_empty_data_dir_is_refused_before_any_network_use(
    tmp_path: pathlib.Path, network_forbidden: None
) -> None:
    """A non-empty data dir exits 2 before the output directory is created."""
    nonempty = tmp_path / "lake"
    nonempty.mkdir()
    (nonempty / "keep.txt").write_text("x", encoding="utf-8")
    out = tmp_path / "out"
    assert main(["--out-dir", str(out), "--data-dir", str(nonempty)]) == 2
    assert not out.exists()


def test_dry_plan_prints_the_plan_and_makes_no_request(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], network_forbidden: None
) -> None:
    """--dry-plan prints twelve countries and an estimate within the cap, and writes nothing."""
    out = tmp_path / "out"
    assert main(["--dry-plan", "--out-dir", str(out)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["countries"]) == 12
    assert data["request_estimate"]["total"] <= DEFAULT_MAX_REQUESTS
    assert not out.exists()


def test_sources_are_summarised_with_nearest_rank_percentiles() -> None:
    """Elapsed 10, 20, 30, 40 give p50 20, p95 40 and max 40; only sent records are summarised."""
    records = [
        RequestRecord(
            seq=index,
            phase="backfill",
            country="TR",
            source="yahoo_chart",
            method="GET",
            host="query2.finance.yahoo.com",
            path="/v8/finance/chart/THYAO.IS",
            query={},
            started_s=0.0,
            waited_s=0.0,
            redirect_of=None,
            status=200,
            elapsed_ms=float(value),
        )
        for index, value in enumerate([10, 20, 30, 40], start=1)
    ]
    assert percentile([10, 20, 30, 40], 50) == 20
    summary = summarize_sources(records)["yahoo_chart"]
    assert summary["requests"] == 4
    assert summary["elapsed_ms"] == {"p50": 20.0, "p95": 40.0, "max": 40.0}


def _country(total: int, bars: int) -> dict[str, Any]:
    return {
        "universe_sync": {"status": "ok", "listings": {"total": total}},
        "backfill": {"status": "ok", "samples": [{"symbol": "X", "bars": {"yahoo_chart": bars}}]},
    }


def test_acceptance_counts_countries_meeting_each_condition_and_both() -> None:
    """Ten countries with listings and bars meet both readings; nine meet neither."""
    ten = {f"C{i:02d}": _country(5, 10) for i in range(10)}
    met = acceptance_summary(ten, extras={})
    assert met["criterion5_strict_met"] is True
    assert met["criterion5_loose_met"] is True
    assert len(met["both_countries"]) == 10
    nine = {f"C{i:02d}": _country(5, 10) for i in range(9)}
    nine["C09"] = _country(0, 10)
    short = acceptance_summary(nine, extras={})
    assert short["criterion5_strict_met"] is False
    assert short["criterion5_loose_met"] is False
    assert len(short["universe_sync_countries"]) == 9
    assert len(short["backfill_countries"]) == 10


def test_the_markdown_report_renders_every_section_from_a_recorded_result() -> None:
    """The rendered page has the six sections, both table headers and the JSON sha256."""
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    sha = "ab" * 32
    markdown = render_markdown(
        fixture, json_path=pathlib.Path("global-equity-20261112T100000Z.json"), json_sha256=sha
    )
    for heading in (
        "# Global equities live measurement",
        "## Countries",
        "## Sources",
        "## Acceptance",
        "## Failures and refusals",
        "## Not measured",
    ):
        assert heading in markdown
    assert (
        "| Country | MICs (calendar) | Listings after sync | Backfill sample (bars) | "
        "Non-session dates | Resume (requests / duplicates) | EOD bars by source | Errors |"
    ) in markdown
    assert (
        "| Source | Requests | Status counts | Exceptions | p50 ms | p95 ms | max ms |" in markdown
    )
    assert sha in markdown


def test_the_script_imports_nothing_from_contrib_and_no_bypass_machinery() -> None:
    """No crocodile.contrib import, and none of the banned bypass names appear in the source."""
    source = SCRIPT.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("crocodile.contrib")
        if isinstance(node, ast.Import):
            assert not any(alias.name.startswith("crocodile.contrib") for alias in node.names)
    for banned in (
        "solve_captcha_2captcha",
        "solve_captcha_anticaptcha",
        "_mine_pow",
        "_solve_pow",
    ):
        assert banned not in source


def test_every_phase_call_opens_and_closes_its_own_orchestrator_on_its_own_loop(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, network_forbidden: None
) -> None:
    """Two phase calls each open and close their own orchestrator and sink, on distinct loops."""
    from crocodile.equity.universal import orchestrator as orchestrator_module
    from crocodile.surfaces import operate

    loops: list[asyncio.AbstractEventLoop] = []
    events: list[tuple[str, int]] = []

    class FakeSink:
        def __init__(self, index: int) -> None:
            self.index = index

        async def close(self) -> None:
            events.append(("sink", self.index))

    class FakeOrchestrator:
        def __init__(self, index: int, loop: asyncio.AbstractEventLoop) -> None:
            self.index = index
            self.loop = loop

        async def aclose(self) -> None:
            assert asyncio.get_running_loop() is self.loop
            events.append(("orchestrator", self.index))

    def fake_open(settings: Any, sink: Any, **options: Any) -> FakeOrchestrator:
        loop = asyncio.get_running_loop()
        loops.append(loop)
        return FakeOrchestrator(len(loops) - 1, loop)

    sinks: list[FakeSink] = []

    def fake_sink(lake: Any) -> FakeSink:
        sinks.append(FakeSink(len(sinks)))
        return sinks[-1]

    async def operation(orch: Any) -> dict[str, bool]:
        return {"ok": True}

    monkeypatch.setattr(orchestrator_module, "open_orchestrator", fake_open)
    monkeypatch.setattr(operate, "_open_sink", fake_sink)
    args = argparse.Namespace(
        data_dir=tmp_path, samples=3, universe_limit=1000, country_timeout=60.0
    )
    run_phase_call("TR", "universe_sync", args, operation)
    run_phase_call("TR", "collect_eod", args, operation)
    assert len(loops) == 2
    assert loops[0] is not loops[1]
    assert events == [("sink", 0), ("orchestrator", 0), ("sink", 1), ("orchestrator", 1)]


def test_a_data_dir_this_run_created_is_removed_and_a_preexisting_one_is_kept(
    tmp_path: pathlib.Path,
) -> None:
    created = tmp_path / "made"
    created.mkdir()
    (created / "lake.txt").write_text("x", encoding="utf-8")
    assert _data_dir_cleanup(created, created_by_run=True, keep=False) == "completed"
    assert not created.exists()
    given = tmp_path / "given"
    given.mkdir()
    assert _data_dir_cleanup(given, created_by_run=False, keep=False) == "kept_preexisting"
    assert given.exists()
    assert _data_dir_cleanup(given, created_by_run=True, keep=True) == "not_requested"
    assert given.exists()


def test_markdown_cells_round_floats_for_display() -> None:
    assert _cell(2343.0000000007567) == "2343"
    assert _cell(0.1234567) == "0.123457"
    assert _cell(1234567.5) == "1234567.5"
    assert _cell(None) == ""


def test_the_final_summary_goes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    _summary("wrote a.json and a.md")
    captured = capsys.readouterr()
    assert captured.out == "wrote a.json and a.md\n"
    assert captured.err == ""
