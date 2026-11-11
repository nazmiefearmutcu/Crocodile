"""Budgeted live measurement of the global-equity pipeline (spec section 6). Never run by pytest.

Why the HTTP layer is traced where it is. Every universal source sends its requests through
aiohttp, and this harness hooks aiohttp.ClientSession.__init__ instead of any one source. A
source paces itself; the ledger is a second, independent line of care that sees every request of
every session, including a session that a later source or phase creates, and that cannot be
bypassed by building a session some other way. A source that forgot its own pacing still meets
the ledger's spacing, the per-endpoint cap and the run-wide cap.

Why hosts are allowlisted. A universal source names its hosts in its own module, and the ledger
sends a request only to a host that a measured source names. A redirect to a consent page or to
any other host is refused before it is followed, so the run cannot wander into a host that no
source declared, and every refusal is recorded with its phase and country.

Why a 403, a bot page, a 503, a 3xx from a JSON source or a second 429 closes a host for the rest
of the run. Pausing is not bypassing; pushing is. A first 429 is a signal to wait: the ledger waits
the cooldown. Every other signal above means the source is refusing this run, so the harness does
not retry with another User-Agent, cookie, proxy or key. It records the refusal and stops sending
to that host, and the report states what the host gave, not what it could be made to give.

Outputs go to --out-dir as JSON and Markdown. The JSON carries every request, refusal and closed
host; the Markdown is rendered from the same result. Neither holds keys, cookies, crumbs or contact
strings, because every CROCODILE_*, CRYPCODILE_* and STOCKODILE_* variable is removed before any
import of the orchestrator, and only the names of the removed variables are recorded.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import hashlib
import importlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest.mock
from collections import Counter
from collections.abc import (
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    Sequence,
)
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NoReturn

import aiohttp
from yarl import URL

from crocodile.core.markets.errors import CalendarRangeError
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.sanctions import is_sanctioned, sanctioned_countries
from crocodile.core.markets.symbols import parse_listing_symbol

if TYPE_CHECKING:
    from crocodile.core.markets.calendar import ExchangeCalendar

SCHEMA: Final = "crocodile.measure_global_equity/1"
DEFAULT_COUNTRIES: Final[tuple[str, ...]] = (
    "TR",
    "DE",
    "GB",
    "JP",
    "IN",
    "BR",
    "ZA",
    "NG",
    "KE",
    "VN",
    "AU",
    "US",
)
PRIMARY_MIC: Final[Mapping[str, str]] = {
    "TR": "XIST",
    "DE": "XETR",
    "GB": "XLON",
    "JP": "XJPX",
    "IN": "XNSE",
    "BR": "BVMF",
    "ZA": "XJSE",
    "NG": "XNSA",
    "KE": "XNAI",
    "VN": "XSTC",
    "AU": "XASX",
    "US": "XNAS",
}
ANCHORS: Final[Mapping[str, tuple[str, ...]]] = {  # canonical symbols; US stays bare
    "TR": ("THYAO@XIST", "GARAN@XIST", "ASELS@XIST"),
    "DE": ("SAP@XETR", "SIE@XETR", "ALV@XETR"),
    "GB": ("VOD@XLON", "HSBA@XLON", "AZN@XLON"),  # pence: live evidence for criterion 3
    "JP": ("7203@XJPX", "6758@XJPX", "9984@XJPX"),
    "IN": ("RELIANCE@XNSE", "TCS@XNSE", "INFY@XNSE"),
    "BR": ("PETR4@BVMF", "VALE3@BVMF", "ITUB4@BVMF"),
    "ZA": ("NPN@XJSE", "SBK@XJSE", "MTN@XJSE"),  # ZAc: live evidence for criterion 3
    "NG": ("DANGCEM@XNSA", "MTNN@XNSA", "ZENITHBANK@XNSA"),
    "KE": ("SCOM@XNAI", "EQTY@XNAI", "KCB@XNAI"),
    "VN": ("VNM@XSTC", "FPT@XSTC", "VCB@XSTC"),
    "AU": ("BHP@XASX", "CBA@XASX", "CSL@XASX"),
    "US": ("AAPL", "MSFT", "NVDA"),  # NVDA: 2024-06-10 10:1 split probe
}
USER_AGENT: Final = "Mozilla/5.0 (crocodile)"
PER_ENDPOINT_CAP: Final = 3
SPACING_SECONDS: Final = 1.0
COOLDOWN_ON_429_S: Final = 60.0
DEFAULT_MAX_REQUESTS: Final = 250
DEFAULT_SAMPLES: Final = 3
DEFAULT_UNIVERSE_LIMIT: Final = 1000  # rows per market, i.e. one scan page
DEFAULT_COUNTRY_TIMEOUT_S: Final = 900.0
EOD_DELAY: Final = timedelta(minutes=45)  # scheduler default, spec 3.9
NON_SESSION_WINDOW_DAYS: Final = 5 * 366  # recent window checked against the calendar
CRITERION_MIN_COUNTRIES: Final = 10
NVDA_SPLIT_DATES: Final = (date(2024, 6, 7), date(2024, 6, 10))
HOST_SOURCES: Final[Mapping[str, str]] = {
    "query2.finance.yahoo.com": "yahoo_chart",
    "scanner.tradingview.com": "tradingview",
    "www.nasdaqtrader.com": "nasdaqtrader",
    "archives.nseindia.com": "nse_india",
    "www.asx.com.au": "asx",
}
EXCLUDED_HOSTS: Final[Mapping[str, str]] = {
    "mopsfin.twse.com.tw": "twse: TW is not among the measured countries",
}
JSON_SOURCES: Final = frozenset({"yahoo_chart", "tradingview"})  # text/html from these = bot page
QUERY_KEYS_RECORDED: Final = frozenset({"period1", "period2", "interval", "range", "events"})
DIRECTORY_FILES: Final[Mapping[str, int]] = {"US": 2, "IN": 1, "AU": 1}
PACKAGES: Final = ("aiohttp", "exchange_calendars", "msgspec", "polars", "duckdb", "yarl")
NOT_MEASURED: Final[tuple[str, ...]] = (
    "Quotas and rate limits beyond the requests this run made",
    "Long-run availability and outage frequency",
    "Data accuracy against an official record",
    "Publication latency and intraday data",
    "Whether any source's terms permit the use",
    "Listings outside the backfill samples and outside the universe-sync bound",
)
SCRUB_PREFIXES: Final = ("CROCODILE_", "CRYPCODILE_", "STOCKODILE_")

Operation = Callable[[Any], Awaitable[Any]]
PHASE: ContextVar[str] = ContextVar("measure_phase", default="setup")
COUNTRY: ContextVar[str | None] = ContextVar("measure_country", default=None)


class MeasurementRefusal(RuntimeError):
    """The ledger refused a request; nothing was sent to the host."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BudgetExceeded(MeasurementRefusal):
    """A per-endpoint or run-wide cap was reached (reason budget_endpoint or budget_total)."""


class HostClosed(MeasurementRefusal):
    """The host was closed earlier in this run (reason host_closed)."""


class HostNotAllowed(MeasurementRefusal):
    """The host is not on the allowlist (reason host_not_allowed)."""


@dataclasses.dataclass(slots=True)
class RequestRecord:
    """One request that was actually sent, with its outcome."""

    seq: int
    phase: str
    country: str | None
    source: str  # HOST_SOURCES[host] or "unattributed"
    method: str
    host: str
    path: str  # no query string
    query: dict[str, str]  # only QUERY_KEYS_RECORDED
    started_s: float  # monotonic offset from run start
    waited_s: float  # spacing or cooldown wait the ledger imposed
    redirect_of: int | None
    status: int | None = None
    content_type: str | None = None
    user_agent: str | None = None  # from response.request_info.headers (what was really sent)
    elapsed_ms: float | None = None
    error: str | None = None  # exception class name for failures that were sent


def endpoint_key(method: str, url: str | URL) -> tuple[str, str, str]:
    """(METHOD, host lowercased, path): the query string is not part of an endpoint."""
    parsed = URL(str(url))
    return (method.upper(), (parsed.host or "").lower(), parsed.path)


def source_for_host(host: str) -> str:
    """The source a host belongs to, or "unattributed" when no measured source names it."""
    return HOST_SOURCES.get(host.lower(), "unattributed")


def recorded_query(url: str | URL) -> dict[str, str]:
    """The query keys the record keeps. Anything else (a crumb, a cookie, a key) is dropped."""
    query = URL(str(url)).query
    return {key: value for key, value in query.items() if key in QUERY_KEYS_RECORDED}


def _is_html(content_type: str | None) -> bool:
    return content_type is not None and content_type.split(";", 1)[0].strip().lower() == "text/html"


class HttpLedger:
    """Admits, paces, caps and closes every request the measured sources send.

    ``admit`` runs before a request is sent and may wait; ``finish`` and ``fail`` run after it.
    A refusal raises :class:`MeasurementRefusal`, is recorded in ``refusals`` and is not counted.
    """

    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str],
        per_endpoint_cap: int = PER_ENDPOINT_CAP,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        spacing_s: float = SPACING_SECONDS,
        cooldown_s: float = COOLDOWN_ON_429_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._allowed = frozenset(host.lower() for host in allowed_hosts)
        self._cap = per_endpoint_cap
        self._max = max_requests
        self._spacing = spacing_s
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._sleep = sleep
        self._origin = clock()
        self._sent = 0
        self._records: list[RequestRecord] = []
        self._refusals: list[dict[str, object]] = []
        self._counts: dict[tuple[str, str, str], int] = {}
        self._starts: dict[str, list[float]] = {}
        self._cooldown_until: dict[str, float] = {}
        self._throttles: dict[str, int] = {}
        self._closed: dict[str, str] = {}
        self._hosts_closed: list[dict[str, object]] = []
        self._locks: dict[tuple[asyncio.AbstractEventLoop, str], asyncio.Lock] = {}
        self._configs: list[aiohttp.TraceConfig] = []

    def _lock_for(self, host: str) -> asyncio.Lock:
        """One lock per (event loop, host), created lazily so separate asyncio.run calls work."""
        key = (asyncio.get_running_loop(), host)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _refuse(
        self, error: type[MeasurementRefusal], reason: str, method: str, url: URL
    ) -> NoReturn:
        self._refusals.append(
            {
                "phase": PHASE.get(),
                "country": COUNTRY.get(),
                "method": method.upper(),
                "host": (url.host or "").lower(),
                "path": url.path,
                "reason": reason,
            }
        )
        raise error(reason)

    def _admissible(self, method: str, url: URL, key: tuple[str, str, str]) -> None:
        host = (url.host or "").lower()
        if host not in self._allowed:
            self._refuse(HostNotAllowed, "host_not_allowed", method, url)
        if host in self._closed:
            self._refuse(HostClosed, "host_closed", method, url)
        if self._sent >= self._max:
            self._refuse(BudgetExceeded, "budget_total", method, url)
        if self._counts.get(key, 0) >= self._cap:
            self._refuse(BudgetExceeded, "budget_endpoint", method, url)

    async def admit(self, method: str, url: str | URL, *, redirect_of: int | None = None) -> int:
        """Check the caps, wait for the host's spacing or cooldown, record the start, return seq."""
        target = URL(str(url))
        host = (target.host or "").lower()
        key = endpoint_key(method, target)
        async with self._lock_for(host):
            waited = 0.0
            while True:
                self._admissible(method, target, key)
                now = self._clock()
                due = self._cooldown_until.get(host, float("-inf"))
                starts = self._starts.get(host)
                if starts:
                    due = max(due, starts[-1] + self._spacing)
                wait = due - now
                if wait <= 0:
                    break
                waited += wait
                await self._sleep(wait)
            now = self._clock()
            self._sent += 1
            self._counts[key] = self._counts.get(key, 0) + 1
            self._starts.setdefault(host, []).append(now)
            self._records.append(
                RequestRecord(
                    seq=len(self._records) + 1,
                    phase=PHASE.get(),
                    country=COUNTRY.get(),
                    source=source_for_host(host),
                    method=method.upper(),
                    host=host,
                    path=target.path,
                    query=recorded_query(target),
                    started_s=now - self._origin,
                    waited_s=waited,
                    redirect_of=redirect_of,
                )
            )
            return len(self._records)

    def _close(self, host: str, reason: str, seq: int) -> None:
        if host in self._closed:
            return
        self._closed[host] = reason
        self._hosts_closed.append({"host": host, "reason": reason, "seq": seq})

    def _settle(self, seq: int) -> RequestRecord:
        record = self._records[seq - 1]
        record.elapsed_ms = (self._clock() - self._origin - record.started_s) * 1000.0
        return record

    def finish(
        self, seq: int, *, status: int, content_type: str | None, user_agent: str | None
    ) -> None:
        """Record a response status and headers, then apply the closing rules. No body is read."""
        record = self._settle(seq)
        record.status = status
        record.content_type = content_type
        record.user_agent = user_agent
        host, source = record.host, record.source
        if status == 429:
            count = self._throttles.get(host, 0) + 1
            self._throttles[host] = count
            if count == 1:
                self._cooldown_until[host] = self._clock() + self._cooldown_s
            else:
                self._close(host, "second_429", seq)
        elif status in (403, 503):
            self._close(host, f"status_{status}", seq)
        elif source in JSON_SOURCES and _is_html(content_type):
            self._close(host, "bot_page", seq)
        elif source in JSON_SOURCES and 300 <= status < 400:
            self._close(host, "redirect_wall", seq)

    def fail(self, seq: int, exc: BaseException) -> None:
        """Record a sent request that raised before any status arrived."""
        record = self._settle(seq)
        if record.status is None:
            record.error = type(exc).__name__

    def new_trace_config(self) -> aiohttp.TraceConfig:
        """A trace config for one session. Its callbacks call admit, finish and fail."""
        ledger = self
        config = aiohttp.TraceConfig()

        async def on_start(_session: Any, ctx: Any, params: Any) -> None:
            ctx.seq = await ledger.admit(params.method, params.url)

        async def on_redirect(_session: Any, ctx: Any, params: Any) -> None:
            response = params.response
            ledger.finish(
                ctx.seq,
                status=response.status,
                content_type=response.headers.get("Content-Type"),
                user_agent=response.request_info.headers.get("User-Agent"),
            )
            location = response.headers.get("Location")
            if location is None:
                return
            ctx.seq = await ledger.admit(
                params.method, params.url.join(URL(location)), redirect_of=ctx.seq
            )

        async def on_end(_session: Any, ctx: Any, params: Any) -> None:
            response = params.response
            ledger.finish(
                ctx.seq,
                status=response.status,
                content_type=response.headers.get("Content-Type"),
                user_agent=response.request_info.headers.get("User-Agent"),
            )

        async def on_exception(_session: Any, ctx: Any, params: Any) -> None:
            ledger.fail(ctx.seq, params.exception)

        config.on_request_start.append(on_start)
        config.on_request_redirect.append(on_redirect)
        config.on_request_end.append(on_end)
        config.on_request_exception.append(on_exception)
        self._configs.append(config)
        return config

    def owns(self, trace_config: aiohttp.TraceConfig) -> bool:
        return any(trace_config is config for config in self._configs)

    @property
    def records(self) -> tuple[RequestRecord, ...]:
        return tuple(self._records)

    @property
    def refusals(self) -> tuple[dict[str, object], ...]:
        return tuple(self._refusals)

    def budget_summary(self) -> dict[str, object]:
        """The budget block of the JSON, computed from what the ledger saw."""
        spacing: dict[str, float] = {}
        for host, starts in sorted(self._starts.items()):
            gaps = [later - earlier for earlier, later in pairwise(starts)]
            if gaps:
                spacing[host] = min(gaps)
        unattributed = {r.host for r in self._records if r.source == "unattributed"}
        unattributed |= {
            str(row["host"])
            for row in self._refusals
            if source_for_host(str(row["host"])) == "unattributed"
        }
        return {
            "per_endpoint_cap": self._cap,
            "spacing_s": self._spacing,
            "max_requests": self._max,
            "cooldown_on_429_s": self._cooldown_s,
            "user_agent_expected": USER_AGENT,
            "requests_sent": self._sent,
            "max_requests_per_endpoint_observed": max(self._counts.values(), default=0),
            "min_spacing_s_by_host": spacing,
            "refusals": [dict(row) for row in self._refusals],
            "hosts_closed": [dict(row) for row in self._hosts_closed],
            "non_generic_user_agents": sorted(
                {r.user_agent for r in self._records if r.user_agent and r.user_agent != USER_AGENT}
            ),
            "unattributed_hosts": sorted(unattributed),
        }


@contextlib.contextmanager
def install_http_ledger(ledger: HttpLedger) -> Iterator[None]:
    """Trace every aiohttp.ClientSession created in the block, including ones made by sources."""
    original = aiohttp.ClientSession.__init__

    def _patched(self: aiohttp.ClientSession, *args: Any, **kwargs: Any) -> None:
        kwargs["trace_configs"] = [
            *(kwargs.get("trace_configs") or ()),
            ledger.new_trace_config(),
        ]
        original(self, *args, **kwargs)

    with unittest.mock.patch.object(aiohttp.ClientSession, "__init__", new=_patched):
        yield


@dataclasses.dataclass(frozen=True, slots=True)
class CountryPlan:
    country: str
    mics: tuple[str, ...]  # sorted, from ExchangeRegistry
    primary_mic: str
    anchors: tuple[str, ...]


def build_plan(countries: Sequence[str]) -> tuple[CountryPlan, ...]:
    """The measured plan per country. ValueError for a bad, sanctioned or unknown code."""
    registry = ExchangeRegistry()
    sanctioned = sanctioned_countries()
    plans: list[CountryPlan] = []
    for code in countries:
        if not re.fullmatch(r"[A-Z]{2}", code):
            raise ValueError(f"not an ISO 3166 alpha-2 code: {code!r}")
        if code in sanctioned:
            raise ValueError(f"{code} is sanctioned by default and is not measured")
        infos = registry.by_country(code)
        if not infos:
            raise ValueError(f"{code} has no exchanges in the exchange table")
        mics = tuple(sorted(info.mic for info in infos))
        for mic in mics:
            if is_sanctioned(mic):
                raise ValueError(f"{mic} ({code}) is a sanctioned market and is not measured")
        primary = PRIMARY_MIC.get(code)
        anchors = ANCHORS.get(code)
        if primary is None or anchors is None:
            raise ValueError(f"{code} has no primary MIC or anchors in the measurement script")
        if primary not in mics:
            raise ValueError(f"primary MIC {primary} is not an exchange of {code}")
        for anchor in anchors:
            _local, anchor_mic = parse_listing_symbol(anchor)
            if code != "US" and anchor_mic != primary:
                raise ValueError(f"anchor {anchor} is not on the primary MIC {primary}")
        plans.append(CountryPlan(country=code, mics=mics, primary_mic=primary, anchors=anchors))
    return tuple(plans)


def _is_active(status: object) -> bool:
    return getattr(status, "value", status) == "active"


def choose_samples(plan: CountryPlan, listings: Iterable[Any], n: int) -> tuple[str, ...]:
    """Up to n symbols: active anchors first, then the primary MIC by symbol, then the rest."""
    active: dict[str, Any] = {}
    for listing in listings:
        if listing.mic in plan.mics and _is_active(listing.status):
            active.setdefault(listing.symbol, listing)
    ordered = [anchor for anchor in plan.anchors if anchor in active]
    primary = sorted(
        (item for item in active.values() if item.mic == plan.primary_mic),
        key=lambda item: item.symbol,
    )
    rest = sorted(
        (item for item in active.values() if item.mic != plan.primary_mic),
        key=lambda item: (item.mic, item.symbol),
    )
    ordered.extend(item.symbol for item in [*primary, *rest])
    return tuple(list(dict.fromkeys(ordered))[:n])


def scrub_environment(env: MutableMapping[str, str]) -> list[str]:
    """Remove every crocodile-family variable. Returns the names removed, never the values."""
    names = sorted(key for key in env if key.startswith(SCRUB_PREFIXES))
    for key in names:
        del env[key]
    return names


def prepare_data_dir(path: Path | None) -> Path:
    """A fresh data dir: a new temporary one when none is given. A non-empty one is refused."""
    if path is None:
        return Path(tempfile.mkdtemp(prefix="crocodile-measure-"))
    if path.exists():
        if not path.is_dir():
            raise ValueError(f"data dir is not a directory: {path}")
        if any(path.iterdir()):
            raise ValueError(f"data dir is not empty: {path}")
    else:
        path.mkdir(parents=True)
    return path


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile; q is a percentage in 0..100. None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def summarize_sources(records: Sequence[RequestRecord]) -> dict[str, dict[str, object]]:
    """Per source: request count, status and exception counts, elapsed percentiles and hosts."""
    grouped: dict[str, list[RequestRecord]] = {}
    for record in records:
        grouped.setdefault(record.source, []).append(record)
    summary: dict[str, dict[str, object]] = {}
    for source in sorted(grouped):
        rows = grouped[source]
        statuses: Counter[str] = Counter(str(r.status) for r in rows if r.status is not None)
        exceptions: Counter[str] = Counter(r.error for r in rows if r.error is not None)
        elapsed = [r.elapsed_ms for r in rows if r.elapsed_ms is not None]
        summary[source] = {
            "requests": len(rows),
            "status_counts": dict(statuses),
            "exceptions": dict(exceptions),
            "elapsed_ms": {
                "p50": percentile(elapsed, 50),
                "p95": percentile(elapsed, 95),
                "max": float(max(elapsed)) if elapsed else None,
            },
            "hosts": sorted({r.host for r in rows}),
        }
    return summary


def _d(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _samples(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        sample
        for sample in _d(entry.get("backfill")).get("samples") or []
        if isinstance(sample, dict)
    ]


def _universe_found(entry: Mapping[str, Any]) -> bool:
    sync = _d(entry.get("universe_sync"))
    return sync.get("status") == "ok" and int(_d(sync.get("listings")).get("total") or 0) > 0


def _has_yahoo_bars(entry: Mapping[str, Any]) -> bool:
    return any(
        int(_d(sample.get("bars")).get("yahoo_chart") or 0) > 0 for sample in _samples(entry)
    )


def acceptance_summary(
    countries: Mapping[str, Mapping[str, Any]], *, extras: Mapping[str, Any]
) -> dict[str, object]:
    """Criterion 5 and the live-evidence fields, computed from the per-country records.

    ``extras`` supplies ``nvda_split_probe`` and ``markets_rows_equal_exchange_table``, which come
    from the lake and the markets phase rather than from one country.
    """
    order = list(countries)
    universe = [cc for cc in order if _universe_found(countries[cc])]
    backfill = [cc for cc in order if _has_yahoo_bars(countries[cc])]
    both = [cc for cc in order if cc in universe and cc in backfill]
    checked = [
        (cc, _d(countries[cc].get("resume")))
        for cc in order
        if _d(countries[cc].get("resume")).get("symbol")
    ]
    resume_ok = bool(checked) and all(
        resume.get("status") == "ok"
        and int(resume.get("requests") or 0) <= 1
        and int(resume.get("duplicates") or 0) == 0
        for _cc, resume in checked
    )
    minor: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for cc in order:
        for sample in _samples(countries[cc]):
            scales = sample.get("price_scales") or []
            if any(scale not in (0, None) for scale in scales):
                minor.append(
                    {
                        "symbol": sample.get("symbol"),
                        "currencies": sample.get("currencies") or [],
                        "price_scales": scales,
                    }
                )
            non_session = _d(sample.get("non_session_dates"))
            if int(non_session.get("count") or 0) > 0:
                violations.append(
                    {
                        "symbol": sample.get("symbol"),
                        "count": non_session.get("count"),
                        "first": non_session.get("first") or [],
                    }
                )
    return {
        "universe_sync_countries": universe,
        "backfill_countries": backfill,
        "both_countries": both,
        "criterion5_strict_met": len(both) >= CRITERION_MIN_COUNTRIES,
        "criterion5_loose_met": len(universe) >= CRITERION_MIN_COUNTRIES and len(backfill) >= 1,
        "resume": {
            "checked": len(checked),
            "max_requests": max((int(r.get("requests") or 0) for _cc, r in checked), default=0),
            "duplicates_total": sum(int(r.get("duplicates") or 0) for _cc, r in checked),
            "ok": resume_ok,
        },
        "minor_unit_evidence": minor,
        "non_session_violations": violations,
        "nvda_split_probe": extras.get("nvda_split_probe"),
        "markets_rows_equal_exchange_table": extras.get("markets_rows_equal_exchange_table"),
    }


def _met(flag: object) -> str:
    return "met" if flag else "not met"


def _cell(value: object) -> str:
    if isinstance(value, float):
        text = f"{round(value, 6):.6f}".rstrip("0").rstrip(".")
    else:
        text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _pairs(mapping: object) -> str:
    items = _d(mapping)
    return "; ".join(f"{key} {value}" for key, value in sorted(items.items())) or "none"


def render_markdown(result: Mapping[str, Any], *, json_path: Path, json_sha256: str) -> str:
    """The Markdown report of one result. Every section is rendered even when a field is absent."""
    git = _d(result.get("git"))
    env = _d(result.get("environment"))
    budget = _d(result.get("budget"))
    countries = _d(result.get("countries"))
    lines: list[str] = [
        "# Global equities live measurement",
        "",
        f"- Run: {result.get('started_at_utc')} -> {result.get('finished_at_utc')} UTC "
        f"({result.get('duration_s')} s, {result.get('weekday_utc')}); commit {git.get('commit')} "
        f"({'dirty' if git.get('dirty') else 'clean'}); status {result.get('status')}.",
        f"- Versions: python {env.get('python')}; {_pairs(env.get('packages'))}.",
        f"- JSON: {json_path.as_posix()} (sha256 {json_sha256}).",
        f"- Budget: sent {budget.get('requests_sent')}/{budget.get('max_requests')}; "
        f"max per endpoint "
        f"{budget.get('max_requests_per_endpoint_observed')}/cap {budget.get('per_endpoint_cap')}; "
        f"min spacing per host {_pairs(budget.get('min_spacing_s_by_host'))}; refusals "
        f"{len(budget.get('refusals') or [])}; "
        f"hosts closed {len(budget.get('hosts_closed') or [])}; "
        f"non-generic UA {budget.get('non_generic_user_agents') or 'none'}; unattributed hosts "
        f"{budget.get('unattributed_hosts') or 'none'}.",
        "",
        "## Countries",
        "",
        "| Country | MICs (calendar) | Listings after sync | Backfill sample (bars) "
        "| Non-session dates "
        "| Resume (requests / duplicates) | EOD bars by source | Errors |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for cc, entry in countries.items():
        entry = _d(entry)
        sync = _d(entry.get("universe_sync"))
        resume = _d(entry.get("resume"))
        eod = _d(entry.get("collect_eod"))
        listings = _d(sync.get("listings"))
        samples = _samples(entry)
        bars = "; ".join(f"{s.get('symbol')} {_pairs(s.get('bars'))}" for s in samples) or "none"
        non_session = sum(int(_d(s.get("non_session_dates")).get("count") or 0) for s in samples)
        errors = [
            f"{phase}: {_d(entry.get(phase)).get('error')}"
            for phase in ("universe_sync", "backfill", "resume", "collect_eod", "markets")
            if _d(entry.get(phase)).get("error")
        ]
        lines.append(
            "| "
            + " | ".join(
                _cell(part)
                for part in (
                    cc,
                    ", ".join(str(m) for m in entry.get("mics") or []) or "none",
                    f"{listings.get('total', 'n/a')} ({sync.get('status', 'n/a')})",
                    bars,
                    non_session,
                    f"{resume.get('requests', 'n/a')} / {resume.get('duplicates', 'n/a')}",
                    _pairs(eod.get("bars_by_source")),
                    "; ".join(errors) or "none",
                )
            )
            + " |"
        )
    lines += [
        "",
        "## Sources",
        "",
        "| Source | Requests | Status counts | Exceptions | p50 ms | p95 ms | max ms |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, row in _d(result.get("sources")).items():
        row = _d(row)
        elapsed = _d(row.get("elapsed_ms"))
        lines.append(
            "| "
            + " | ".join(
                _cell(part)
                for part in (
                    name,
                    row.get("requests"),
                    _pairs(row.get("status_counts")),
                    _pairs(row.get("exceptions")),
                    elapsed.get("p50"),
                    elapsed.get("p95"),
                    elapsed.get("max"),
                )
            )
            + " |"
        )
    acceptance = _d(result.get("acceptance"))
    resume_block = _d(acceptance.get("resume"))
    markets_all = _d(result.get("markets_all"))
    total = len(countries)
    universe = acceptance.get("universe_sync_countries") or []
    backfill_list = acceptance.get("backfill_countries") or []
    both = acceptance.get("both_countries") or []
    lines += [
        "",
        "## Acceptance",
        "",
        f"- Criterion 5, strict reading: {_met(acceptance.get('criterion5_strict_met'))}"
        f" (both in {len(both)}/{total}: {', '.join(both) or 'none'}).",
        f"- Criterion 5, loose reading: {_met(acceptance.get('criterion5_loose_met'))}"
        f" (universe-sync {len(universe)}/{total}: {', '.join(universe) or 'none'}; backfill "
        f"{len(backfill_list)}/{total}: {', '.join(backfill_list) or 'none'}).",
        f"- Resume: {resume_block.get('checked')} listings re-run, at most "
        f"{resume_block.get('max_requests')} request each, {resume_block.get('duplicates_total')} "
        f"duplicate bars (ok: {resume_block.get('ok')}).",
    ]
    for row in acceptance.get("minor_unit_evidence") or []:
        lines.append(
            f"- Minor unit: {row.get('symbol')} "
            f"currencies {', '.join(row.get('currencies') or [])}, "
            f"price_scale {row.get('price_scales')}."
        )
    probe = acceptance.get("nvda_split_probe")
    if isinstance(probe, dict):
        lines.append(
            "- NVDA split probe: close 2024-06-07 "
            f"{probe.get('close_2024_06_07')}, close 2024-06-10 "
            f"{probe.get('close_2024_06_10')}, ratio {probe.get('ratio')}, adjustments "
            f"{probe.get('adjustments')}."
        )
    else:
        lines.append("- NVDA split probe: not measured (no NVDA bars in the lake).")
    lines.append(
        f"- Markets, all rows: {markets_all.get('rows')} rows against "
        f"{markets_all.get('exchange_table_rows')} "
        f"exchange table rows (equal: {markets_all.get('equal')})."
    )
    lines += ["", "## Failures and refusals", ""]
    failures: list[str] = []
    for cc, entry in countries.items():
        for phase in ("universe_sync", "backfill", "resume", "collect_eod", "markets"):
            block = _d(_d(entry).get(phase))
            if block.get("error"):
                failures.append(
                    f"- {cc} {phase} ({block.get('status', 'error')}): {block.get('error')}"
                )
    for row in budget.get("refusals") or []:
        failures.append(
            f"- refusal: {_d(row).get('reason')} for {_d(row).get('host')}{_d(row).get('path')} "
            f"in phase {_d(row).get('phase')}"
        )
    for row in budget.get("hosts_closed") or []:
        failures.append(f"- host closed: {_d(row).get('host')} ({_d(row).get('reason')})")
    lines += failures or ["- none"]
    lines += ["", "## Not measured", ""]
    lines += [f"- {item}" for item in result.get("not_measured") or NOT_MEASURED]
    lines.append("")
    return "\n".join(lines)


def _rows(frame: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in frame.iter_rows(named=True)]


def _channel_rows(
    catalog: Any, channel: str, sql: str, params: Sequence[object]
) -> list[dict[str, Any]]:
    """Rows of a query over one channel view; no rows when the channel is not in the lake."""
    if channel not in catalog.list_channels():
        return []
    return _rows(catalog.query(sql, params=list(params)))


def _bar_date(source_ts: object) -> date:
    return datetime.fromtimestamp(int(str(source_ts)) // 1_000_000_000, UTC).date()


def _midnight_ns(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * 1_000_000_000


def _utc_day(period1: str | None) -> str | None:
    if period1 is None or not period1.isdigit():
        return None
    return datetime.fromtimestamp(int(period1), UTC).date().isoformat()


def _status(value: object) -> str:
    return str(getattr(value, "value", value))


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:500]}"


def _lag(calendar: ExchangeCalendar, last: date, expected: date | None) -> int | None:
    if expected is None:
        return None
    if last >= expected:
        return 0
    try:
        return len(calendar.sessions(last + timedelta(days=1), expected))
    except CalendarRangeError:
        return None


async def _phase_call(
    country: str, phase: str, args: argparse.Namespace, operation: Operation
) -> Any:
    """One orchestrator and one sink, both opened and closed inside this coroutine (ruling 28)."""
    from crocodile.equity.universal import orchestrator as orchestrator_module
    from crocodile.surfaces import operate

    settings = operate._settings_for(args.data_dir)
    sink = operate._open_sink(settings.data_dir)
    orch: Any = None
    try:
        orch = orchestrator_module.open_orchestrator(
            settings,
            sink,
            scan_max_rows=args.universe_limit,
            eod_fallback_limit=args.samples,
        )
        return await operation(orch)
    finally:
        try:
            await sink.close()
        finally:
            if orch is not None:
                await orch.aclose()


def run_phase_call(country: str, phase: str, args: argparse.Namespace, operation: Operation) -> Any:
    """Run one (country, phase) call in its own event loop, bounded by --country-timeout."""
    PHASE.set(phase)
    COUNTRY.set(country)
    return asyncio.run(
        asyncio.wait_for(_phase_call(country, phase, args, operation), args.country_timeout)
    )


def _builtins(value: Any) -> Any:
    import msgspec

    try:
        return msgspec.to_builtins(value)
    except Exception:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return dataclasses.asdict(value)
        return repr(value)


def _timed_call(
    country: str, phase: str, args: argparse.Namespace, operation: Operation
) -> dict[str, Any]:
    started = time.monotonic()
    status: str
    error: str | None
    report: Any
    try:
        value = run_phase_call(country, phase, args, operation)
    except TimeoutError as exc:
        status, error, report = "timeout", _error_text(exc), None
    except Exception as exc:
        status, error, report = "error", _error_text(exc), None
    else:
        status, error, report = "ok", None, _builtins(value)
    return {
        "status": status,
        "error": error,
        "duration_s": round(time.monotonic() - started, 3),
        "report": report,
    }


def _sync_op(country: str) -> Operation:
    from crocodile.equity.universal.orchestrator import Scope

    def op(orch: Any) -> Any:
        return orch.sync_universe(Scope(country=country))

    return op


def _backfill_op(symbols: Sequence[str], end: date, channels: tuple[str, ...]) -> Operation:
    from crocodile.equity.universal.orchestrator import Scope

    def op(orch: Any) -> Any:
        return orch.backfill(
            Scope(symbols=tuple(symbols)),
            channels,
            start=None,
            end=end,
            sources=("yahoo_chart",),
        )

    return op


def _eod_op(country: str, now: datetime) -> Operation:
    from crocodile.equity.universal.orchestrator import Scope

    def op(orch: Any) -> Any:
        return orch.collect_eod(Scope(country=country), now)

    return op


def _listings(data_dir: Path, country: str) -> list[Any]:
    from crocodile.core.markets.master import ListingMaster

    with ListingMaster.for_data_dir(data_dir) as master:
        return list(master.listings(country=country))


def _listing_census(data_dir: Path, country: str) -> dict[str, Any]:
    """Counts after an orchestrator has closed: listings, aliases and instrument rows by source."""
    from crocodile.core.markets.master import ListingMaster
    from crocodile.core.store.catalog import Catalog

    out: dict[str, Any] = {}
    with ListingMaster.for_data_dir(data_dir) as master:
        listings = list(master.listings(country=country))
        out["listings"] = {
            "total": len(listings),
            "by_mic": dict(Counter(item.mic for item in listings)),
            "by_status": dict(Counter(_status(item.status) for item in listings)),
        }
        try:
            aliases: Counter[str] = Counter()
            for item in listings:
                for alias in master.aliases(item.symbol):
                    aliases[alias.source] += 1
            out["aliases_by_source"] = dict(aliases)
        except Exception as exc:
            out["aliases_by_source"] = None
            out["aliases_error"] = _error_text(exc)
    symbols = {item.symbol for item in listings}
    try:
        with Catalog(data_dir) as catalog:
            rows = _channel_rows(catalog, "instrument", "SELECT source, symbol FROM instrument", [])
        out["instrument_rows_by_source"] = dict(
            Counter(str(row["source"]) for row in rows if row["symbol"] in symbols)
        )
    except Exception as exc:
        out["instrument_rows_by_source"] = None
        out["instrument_error"] = _error_text(exc)
    return out


def _yahoo_counts(data_dir: Path, symbol: str) -> dict[str, int]:
    from crocodile.core.store.catalog import Catalog

    with Catalog(data_dir) as catalog:
        rows = _channel_rows(
            catalog,
            "ohlcv",
            "SELECT count(*) AS n, count(DISTINCT source_ts) AS d FROM ohlcv "
            "WHERE symbol = ? AND source = 'yahoo_chart' AND interval = '1d'",
            [symbol],
        )
    if not rows:
        return {"rows": 0, "duplicates": 0}
    return {"rows": int(rows[0]["n"]), "duplicates": int(rows[0]["n"]) - int(rows[0]["d"])}


def _sample_profile(
    data_dir: Path, symbol: str, plan: CountryPlan, now: datetime, master: Any
) -> dict[str, Any]:
    from crocodile.core.markets.calendar import calendar_for
    from crocodile.core.store.catalog import Catalog

    listing = master.get(symbol)
    mic = listing.mic if listing is not None else None
    profile: dict[str, Any] = {
        "symbol": symbol,
        "anchor": symbol if symbol in plan.anchors else None,
        "mic": mic,
        "bars": {},
        "first_date": None,
        "last_date": None,
        "expected_latest_session": None,
        "lag_sessions": None,
        "non_session_dates": {"count": 0, "first": []},
        "out_of_calendar_range": False,
        "currencies": [],
        "price_scales": [],
        "adjustments": [],
        "corp_actions": {},
    }
    with Catalog(data_dir) as catalog:
        rows = _channel_rows(
            catalog,
            "ohlcv",
            "SELECT source, source_ts, currency, price_scale, adjustment FROM ohlcv "
            "WHERE symbol = ? AND interval = '1d'",
            [symbol],
        )
        actions = _channel_rows(
            catalog,
            "corp_action",
            "SELECT type, count(*) AS n FROM corp_action WHERE symbol = ? GROUP BY type",
            [symbol],
        )
    profile["bars"] = dict(Counter(str(row["source"]) for row in rows))
    profile["corp_actions"] = {str(row["type"]): int(row["n"]) for row in actions}
    dates = sorted({_bar_date(row["source_ts"]) for row in rows if row["source_ts"] is not None})
    if dates and mic is not None:
        profile["first_date"] = dates[0].isoformat()
        profile["last_date"] = dates[-1].isoformat()
        profile["currencies"] = sorted(
            {str(v) for v in (r["currency"] for r in rows) if v is not None}
        )
        profile["price_scales"] = sorted(
            {int(v) for v in (r["price_scale"] for r in rows) if v is not None}
        )
        profile["adjustments"] = sorted(
            {str(getattr(v, "value", v)) for v in (r["adjustment"] for r in rows) if v is not None}
        )
        calendar = calendar_for(mic)
        expected = calendar.latest_completed_session(now, EOD_DELAY)
        profile["expected_latest_session"] = expected.isoformat() if expected else None
        profile["lag_sessions"] = _lag(calendar, dates[-1], expected)
        window_start = dates[-1] - timedelta(days=NON_SESSION_WINDOW_DAYS)
        recent = sorted({day for day in dates if day >= window_start})
        try:
            sessions = set(calendar.sessions(window_start, dates[-1]))
        except CalendarRangeError:
            profile["out_of_calendar_range"] = True
        else:
            stray = [day for day in recent if day not in sessions]
            profile["non_session_dates"] = {
                "count": len(stray),
                "first": [day.isoformat() for day in stray[:5]],
            }
    return profile


def _nvda_probe(data_dir: Path) -> dict[str, Any] | None:
    from crocodile.core.store.catalog import Catalog

    first, second = NVDA_SPLIT_DATES
    with Catalog(data_dir) as catalog:
        rows = _channel_rows(
            catalog,
            "ohlcv",
            "SELECT source_ts, close, adjustment FROM ohlcv WHERE symbol = 'NVDA' "
            "AND source = 'yahoo_chart' AND interval = '1d' AND source_ts >= ? AND source_ts <= ?",
            [_midnight_ns(first), _midnight_ns(second)],
        )
    if not rows:
        return None
    closes = {_bar_date(row["source_ts"]): row["close"] for row in rows}
    before, after = closes.get(first), closes.get(second)
    ratio = before / after if before is not None and after else None
    return {
        "close_2024_06_07": before,
        "close_2024_06_10": after,
        "ratio": ratio,
        "adjustments": sorted(
            {str(getattr(r["adjustment"], "value", r["adjustment"])) for r in rows}
        ),
    }


def _markets_rows(data_dir: Path, values: dict[str, Any]) -> list[dict[str, Any]]:
    from crocodile.core.capability import AssetClass
    from crocodile.core.store.catalog import Catalog
    from crocodile.surfaces import dispatch

    capability = dispatch.resolve("markets")
    params = dispatch.build_params(capability, values)
    with Catalog(data_dir) as catalog:
        ctx = dispatch.build_context(catalog, AssetClass.EQUITY, readonly=True, row_limit=None)
        result = dispatch.invoke(capability, ctx, params)
        result = dispatch.drive(result, row_limit=None)
        body = dispatch.payload(capability, result, row_limit=None)
    return [dict(row) for row in body.get("rows") or []]


@dataclasses.dataclass
class _State:
    args: argparse.Namespace
    ledger: HttpLedger
    data_dir: Path
    plans: tuple[CountryPlan, ...]
    countries: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    phases: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    markets_all: dict[str, Any] | None = None
    eod_now: datetime | None = None


def _phase_done(state: _State, name: str, started: float) -> None:
    state.phases.setdefault(name, {})["duration_s"] = round(time.monotonic() - started, 3)


def _universe_phase(state: _State) -> None:
    PHASE.set("universe_sync")
    started = time.monotonic()
    for plan in state.plans:
        call = _timed_call(plan.country, "universe_sync", state.args, _sync_op(plan.country))
        try:
            census = _listing_census(state.data_dir, plan.country)
        except Exception as exc:
            census = {"listings": None, "census_error": _error_text(exc)}
        state.countries[plan.country]["universe_sync"] = {**call, **census}
    _phase_done(state, "universe_sync", started)


def _profiles(
    state: _State, plan: CountryPlan, samples: Sequence[str], now: datetime
) -> list[dict[str, Any]]:
    from crocodile.core.markets.master import ListingMaster

    profiles: list[dict[str, Any]] = []
    with ListingMaster.for_data_dir(state.data_dir) as master:
        for symbol in samples:
            try:
                profiles.append(_sample_profile(state.data_dir, symbol, plan, now, master))
            except Exception as exc:
                profiles.append({"symbol": symbol, "error": _error_text(exc)})
    return profiles


def _backfill_phase(state: _State) -> None:
    PHASE.set("backfill")
    started = time.monotonic()
    end = datetime.now(UTC).date()
    for plan in state.plans:
        entry = state.countries[plan.country]
        samples = choose_samples(plan, _listings(state.data_dir, plan.country), state.args.samples)
        if not samples:
            entry["backfill"] = {
                "status": "skipped",
                "error": "no active listing in the country",
                "duration_s": 0.0,
                "report": None,
                "samples": [],
            }
            continue
        call = _timed_call(
            plan.country,
            "backfill",
            state.args,
            _backfill_op(samples, end, ("ohlcv", "corp_action")),
        )
        call["samples"] = _profiles(state, plan, samples, datetime.now(UTC))
        entry["backfill"] = call
    _phase_done(state, "backfill", started)


def _first_with_yahoo_bars(backfill: Mapping[str, Any]) -> dict[str, Any] | None:
    for sample in backfill.get("samples") or []:
        if int(_d(sample.get("bars")).get("yahoo_chart") or 0) > 0:
            return dict(sample)
    return None


def _resume_phase(state: _State) -> None:
    PHASE.set("resume")
    started = time.monotonic()
    end = datetime.now(UTC).date()
    for plan in state.plans:
        entry = state.countries[plan.country]
        target = _first_with_yahoo_bars(_d(entry.get("backfill")))
        if target is None:
            entry["resume"] = {
                "status": "skipped",
                "error": "no sample with yahoo_chart bars",
                "symbol": None,
                "requests": 0,
                "period1": None,
                "rows_before": None,
                "rows_after": None,
                "duplicates": None,
            }
            continue
        symbol = str(target["symbol"])
        before = _yahoo_counts(state.data_dir, symbol)["rows"]
        call = _timed_call(
            plan.country, "resume", state.args, _backfill_op([symbol], end, ("ohlcv",))
        )
        sent = [
            r for r in state.ledger.records if r.phase == "resume" and r.country == plan.country
        ]
        period1 = next((r.query["period1"] for r in sent if "period1" in r.query), None)
        after = _yahoo_counts(state.data_dir, symbol)
        entry["resume"] = {
            "status": call["status"],
            "error": call["error"],
            "symbol": symbol,
            "requests": len(sent),
            "period1": _utc_day(period1),
            "rows_before": before,
            "rows_after": after["rows"],
            "duplicates": after["duplicates"],
        }
    _phase_done(state, "resume", started)


def _eod_phase(state: _State) -> None:
    PHASE.set("collect_eod")
    started = time.monotonic()
    now = datetime.now(UTC)
    state.eod_now = now
    phase_start_ns = time.time_ns()
    for plan in state.plans:
        state.countries[plan.country]["collect_eod"] = _timed_call(
            plan.country, "collect_eod", state.args, _eod_op(plan.country, now)
        )
    try:
        from crocodile.core.store.catalog import Catalog

        with Catalog(state.data_dir) as catalog:
            written = _channel_rows(
                catalog,
                "ohlcv",
                "SELECT symbol, source, source_ts FROM ohlcv WHERE local_ts >= ?",
                [phase_start_ns],
            )
        for plan in state.plans:
            symbols = {item.symbol for item in _listings(state.data_dir, plan.country)}
            mine = [row for row in written if row["symbol"] in symbols]
            block = state.countries[plan.country]["collect_eod"]
            block["bars_by_source"] = dict(Counter(str(row["source"]) for row in mine))
            block["session_dates"] = sorted(
                {_bar_date(row["source_ts"]).isoformat() for row in mine}
            )
    except Exception as exc:
        for plan in state.plans:
            block = state.countries[plan.country]["collect_eod"]
            block.setdefault("bars_by_source", None)
            block.setdefault("session_dates", None)
            block["census_error"] = _error_text(exc)
    _phase_done(state, "collect_eod", started)


def _markets_phase(state: _State) -> None:
    PHASE.set("markets")
    started = time.monotonic()
    from crocodile.core.markets.exchanges import ExchangeRegistry as Registry

    for plan in state.plans:
        try:
            rows = _markets_rows(state.data_dir, {"country": plan.country})
            state.countries[plan.country]["markets"] = {"rows": rows, "error": None}
        except Exception as exc:
            state.countries[plan.country]["markets"] = {"rows": [], "error": _error_text(exc)}
    try:
        everything = _markets_rows(state.data_dir, {})
        table = len(Registry().all())
        state.markets_all = {
            "rows": len(everything),
            "exchange_table_rows": table,
            "equal": len(everything) == table,
            "error": None,
        }
    except Exception as exc:
        state.markets_all = {
            "rows": None,
            "exchange_table_rows": None,
            "equal": False,
            "error": _error_text(exc),
        }
    _phase_done(state, "markets", started)
    state.phases["markets"]["requests"] = sum(
        1 for r in state.ledger.records if r.phase == "markets"
    )


def _git(args: list[str], repo: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _versions() -> dict[str, str | None]:
    from importlib import metadata

    out: dict[str, str | None] = {}
    for name in PACKAGES:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = None
    return out


def _scrub_arguments(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "out_dir": str(args.out_dir),
        "countries": list(args.countries),
        "samples": args.samples,
        "data_dir": str(args.data_dir),
        "keep_data": args.keep_data,
        "max_requests": args.max_requests,
        "per_endpoint_cap": args.per_endpoint_cap,
        "spacing_seconds": args.spacing_seconds,
        "universe_limit": args.universe_limit,
        "country_timeout": args.country_timeout,
        "universe_bound_applied": True,
    }


def _nvda_or_none(data_dir: Path) -> dict[str, Any] | None:
    try:
        return _nvda_probe(data_dir)
    except Exception as exc:
        return {"error": _error_text(exc)}


def run_measurement(args: argparse.Namespace) -> dict[str, Any]:
    """Run the five phases with the ledger installed, then build the result. Synchronous.

    The result always carries every top-level key, even after an interruption or an abort, so the
    JSON on disk is complete about what did happen.
    """
    started = datetime.now(UTC)
    mono = time.monotonic()
    ledger = HttpLedger(
        allowed_hosts=frozenset(HOST_SOURCES),
        per_endpoint_cap=args.per_endpoint_cap,
        max_requests=args.max_requests,
        spacing_s=args.spacing_seconds,
    )
    PHASE.set("setup")
    COUNTRY.set(None)
    status = "ok"
    abort: dict[str, Any] | None = None
    state = _State(
        args=args, ledger=ledger, data_dir=args.data_dir, plans=build_plan(args.countries)
    )
    for plan in state.plans:
        state.countries[plan.country] = {"mics": list(plan.mics), "primary_mic": plan.primary_mic}
    with install_http_ledger(ledger):
        try:
            _universe_phase(state)
            _backfill_phase(state)
            _resume_phase(state)
            _eod_phase(state)
            _markets_phase(state)
        except KeyboardInterrupt:
            status = "interrupted"
            abort = {"phase": PHASE.get(), "country": COUNTRY.get(), "error": "KeyboardInterrupt"}
        except Exception as exc:
            status = "aborted"
            abort = {"phase": PHASE.get(), "country": COUNTRY.get(), "error": _error_text(exc)}
    finished = datetime.now(UTC)
    git_commit = _git(["rev-parse", "HEAD"], REPO)
    git_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], REPO)
    porcelain = _git(["status", "--porcelain"], REPO)
    nvda = _nvda_or_none(state.data_dir)
    extras: dict[str, Any] = {
        "nvda_split_probe": nvda if nvda and "error" not in nvda else None,
        "markets_rows_equal_exchange_table": (state.markets_all or {}).get("equal"),
    }
    acceptance = acceptance_summary(state.countries, extras=extras)
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "status": status,
        "abort": abort,
        "started_at_utc": started.isoformat(),
        "finished_at_utc": finished.isoformat(),
        "duration_s": round(time.monotonic() - mono, 3),
        "weekday_utc": started.strftime("%A"),
        "git": {
            "commit": git_commit,
            "dirty": None if porcelain is None else bool(porcelain),
            "branch": git_branch,
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "packages": _versions(),
        },
        "arguments": _scrub_arguments(args),
        "data_dir": str(args.data_dir),
        "data_dir_kept": bool(args.keep_data),
        "environment_scrubbed": list(getattr(args, "environment_scrubbed", [])),
        "eod_now_utc": state.eod_now.isoformat() if state.eod_now else None,
        "budget": {
            "per_endpoint_cap": args.per_endpoint_cap,
            "spacing_s": args.spacing_seconds,
            "max_requests": args.max_requests,
            **ledger.budget_summary(),
        },
        "phases": state.phases,
        "countries": state.countries,
        "markets_all": state.markets_all,
        "sources": summarize_sources(ledger.records),
        "acceptance": {**acceptance, "nvda_split_probe": extras["nvda_split_probe"]},
        "not_measured": list(NOT_MEASURED),
        "requests": [dataclasses.asdict(record) for record in ledger.records],
    }
    return result


def request_estimate(plans: Sequence[CountryPlan], samples: int) -> dict[str, int]:
    """The expected number of requests of a full run, for the dry plan.

    universe-sync: 3 per country (TradingView scan pages and the directory files of the country),
    plus DIRECTORY_FILES (US 2, IN 1, AU 1). backfill: samples x countries x 2. resume: 1 per
    country. collect_eod: 3 per country, plus the Yahoo end-of-day fallback, which spends at most
    samples requests per MIC that has no TradingView market (ruling: counted per MIC, not per
    country, so the ceiling cannot pass on paper and fail live).
    """
    tv = importlib.import_module("crocodile.equity.universal.tradingview")
    universe = sum(3 + DIRECTORY_FILES.get(plan.country, 0) for plan in plans)
    backfill = samples * len(plans) * 2
    resume = len(plans)
    fallback_mics = sum(1 for plan in plans for mic in plan.mics if not tv.markets_for_mic(mic))
    collect = 3 * len(plans) + samples * fallback_mics
    return {
        "universe_sync": universe,
        "backfill": backfill,
        "resume": resume,
        "collect_eod": collect,
        "total": universe + backfill + resume + collect,
    }


def _say(message: str) -> None:
    print(message.encode("ascii", "backslashreplace").decode("ascii"), file=sys.stderr, flush=True)


def _summary(message: str) -> None:
    """The final line of a run, on stdout (progress goes to stderr through ``_say``)."""
    print(message.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def _data_dir_cleanup(data_dir: Path, *, created_by_run: bool, keep: bool) -> str:
    """Remove the data dir only when this run created it; a directory the operator named is kept."""
    if keep:
        return "not_requested"
    if not created_by_run:
        return "kept_preexisting"
    shutil.rmtree(data_dir, ignore_errors=True)
    return "completed" if not data_dir.exists() else "incomplete"


def _csv(text: str) -> tuple[str, ...]:
    return tuple(part.strip().upper() for part in text.split(",") if part.strip())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--out-dir", type=Path, required=True, help="where the JSON and Markdown go"
    )
    parser.add_argument(
        "--countries", type=_csv, default=DEFAULT_COUNTRIES, help="ISO alpha-2 codes"
    )
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES, help="listings per country")
    parser.add_argument(
        "--data-dir", type=Path, default=None, help="fresh data dir (default: temp)"
    )
    parser.add_argument("--keep-data", action="store_true", help="keep the data dir after the run")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    parser.add_argument("--per-endpoint-cap", type=int, default=PER_ENDPOINT_CAP, help="at most 3")
    parser.add_argument(
        "--spacing-seconds", type=float, default=SPACING_SECONDS, help="at least 1.0"
    )
    parser.add_argument("--universe-limit", type=int, default=DEFAULT_UNIVERSE_LIMIT)
    parser.add_argument("--country-timeout", type=float, default=DEFAULT_COUNTRY_TIMEOUT_S)
    parser.add_argument("--dry-plan", action="store_true", help="print the plan and send nothing")
    return parser


def _print_dry_plan(plans: Sequence[CountryPlan], args: argparse.Namespace) -> None:
    document = {
        "countries": [
            {
                "country": plan.country,
                "mics": list(plan.mics),
                "primary_mic": plan.primary_mic,
                "anchors": list(plan.anchors),
            }
            for plan in plans
        ],
        "allowed_hosts": sorted(HOST_SOURCES),
        "budget": {
            "per_endpoint_cap": args.per_endpoint_cap,
            "spacing_s": args.spacing_seconds,
            "max_requests": args.max_requests,
            "cooldown_on_429_s": COOLDOWN_ON_429_S,
            "user_agent": USER_AGENT,
        },
        "request_estimate": request_estimate(plans, args.samples),
    }
    print(json.dumps(document, indent=2, ensure_ascii=True))


def main(argv: Sequence[str] | None = None) -> int:
    """0 when the run finished (even if sources failed); 1 when it aborted; 2 usage or preflight;
    130 when it was interrupted (the partial JSON and Markdown are still written)."""
    args = _parser().parse_args(argv)
    if not 1 <= args.per_endpoint_cap <= PER_ENDPOINT_CAP:
        _say(f"error: --per-endpoint-cap must be between 1 and {PER_ENDPOINT_CAP}")
        return 2
    if args.spacing_seconds < SPACING_SECONDS:
        _say(f"error: --spacing-seconds must be at least {SPACING_SECONDS}")
        return 2
    if args.samples < 1:
        _say("error: --samples must be at least 1")
        return 2
    try:
        plans = build_plan(args.countries)
    except ValueError as exc:
        _say(f"error: {exc}")
        return 2
    if args.dry_plan:
        _print_dry_plan(plans, args)
        return 0
    created_by_run = args.data_dir is None or not args.data_dir.exists()
    try:
        data_dir = prepare_data_dir(args.data_dir)
    except ValueError as exc:
        _say(f"error: {exc}")
        return 2
    scrubbed = scrub_environment(os.environ)
    os.environ["CROCODILE_DATA_DIR"] = str(data_dir)
    os.environ["CROCODILE_HOME"] = str(data_dir / "home")
    args.data_dir = data_dir
    args.environment_scrubbed = scrubbed
    args.out_dir.mkdir(parents=True, exist_ok=True)
    result = run_measurement(args)
    cleanup = _data_dir_cleanup(data_dir, created_by_run=created_by_run, keep=args.keep_data)
    result["arguments"]["data_dir_cleanup"] = cleanup
    result["data_dir_kept"] = cleanup != "completed"
    stamp = datetime.fromisoformat(result["started_at_utc"]).strftime("%Y%m%dT%H%M%SZ")
    json_path = args.out_dir / f"global-equity-{stamp}.json"
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False, default=str)
    json_bytes = payload.encode("utf-8")
    json_path.write_bytes(json_bytes)
    sha = hashlib.sha256(json_bytes).hexdigest()
    md_path = json_path.with_suffix(".md")
    md_path.write_text(
        render_markdown(result, json_path=json_path, json_sha256=sha), encoding="utf-8"
    )
    _summary(f"wrote {json_path} and {md_path}")
    return {"interrupted": 130, "aborted": 1}.get(str(result["status"]), 0)


REPO: Final = Path(__file__).resolve().parents[1]


if __name__ == "__main__":
    sys.exit(main())
