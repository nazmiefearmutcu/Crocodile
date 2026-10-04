"""FINRA (Financial Industry Regulatory Authority) Market Transparency Client.

Ingests rare, non-public or fragmented equity market microstructure data:
1. Reg SHO Daily Short Sale & Short-Exempt Volume (off-exchange & consolidated).
2. Bi-Weekly Consolidated Short Interest across all exchange-listed & OTC equities.
3. ATS (Dark Pool) & Non-ATS OTC Wholesaler Internalization weekly volume by MPID.
4. Reg SHO Threshold Securities list.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

import aiohttp
import msgspec

from crocodile.core.ratelimit import TokenBucketLimiter
from crocodile.core.schema.enums import AssetClass
from crocodile.core.schema.records import ShortInterest, ShortVolume

log = logging.getLogger(__name__)

_DEFAULT_USER_AGENT: Final = "crocodile/0.3.0 (market-data research)"
_FINRA_API_URL: Final = "https://api.finra.org/data/group/otcMarket/name"
_FINRA_CDN_REGSHO_URL: Final = "https://cdn.finra.org/equity/regsho/daily"
_NASDAQ_THRESHOLD_URL: Final = "https://www.nasdaqtrader.com/dynamic/symdir/regsho"

_NS_PER_SECOND: Final = 1_000_000_000


def _parse_iso_date_to_ns(raw: str) -> int:
    """Convert YYYY-MM-DD string to UTC midnight nanoseconds."""
    clean = raw.strip()[:10]
    dt = datetime.strptime(clean, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(dt.timestamp()) * _NS_PER_SECOND


def _date_to_ns(d: date) -> int:
    """Convert a datetime.date object to UTC midnight nanoseconds."""
    dt = datetime(d.year, d.month, d.day, tzinfo=UTC)
    return int(dt.timestamp()) * _NS_PER_SECOND


def _ns_to_date(ns: int) -> date:
    """Convert epoch nanoseconds to UTC date."""
    seconds = ns // _NS_PER_SECOND
    # Clamp to valid datetime range
    clamped_s = max(0, min(seconds, 253402300799))
    return datetime.fromtimestamp(clamped_s, tz=UTC).date()


def _format_yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _format_iso(d: date) -> str:
    return d.strftime("%Y-%m-%d")


class FinraClient:
    """Client for FINRA market transparency feeds and API endpoints."""

    def __init__(
        self,
        session: aiohttp.ClientSession | None = None,
        rate_limit: float = 10.0,
        user_agent: str = _DEFAULT_USER_AGENT,
    ) -> None:
        self.session = session
        self._limiter = TokenBucketLimiter(rate_limit, rate_limit)
        self.user_agent = user_agent

    def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=45.0, connect=10.0, sock_read=30.0)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    async def close(self) -> None:
        if self.session is not None and not self.session.closed:
            await self.session.close()

    async def _get(self, url: str) -> tuple[int, bytes]:
        await self._limiter.acquire()
        headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        session = self._get_session()
        async with session.get(url, headers=headers) as resp:
            body = await resp.read()
            return resp.status, body

    async def _post_json(self, url: str, payload: dict[str, Any]) -> tuple[int, Any]:
        await self._limiter.acquire()
        headers = {
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        data = msgspec.json.encode(payload)
        session = self._get_session()
        async with session.post(url, headers=headers, data=data) as resp:
            body = await resp.read()
            if resp.status == 200:
                return resp.status, msgspec.json.decode(body)
            return resp.status, body

    async def get_regsho_short_volume_day(
        self, target_date: date, symbol: str | None = None
    ) -> list[ShortVolume]:
        """Fetch daily consolidated Reg SHO short sale volume for one trading date.

        URL: https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt
        """
        date_str = _format_yyyymmdd(target_date)
        iso_date = _format_iso(target_date)
        url = f"{_FINRA_CDN_REGSHO_URL}/CNMSshvol{date_str}.txt"

        status, body = await self._get(url)
        if status == 404:
            # Weekend, holiday or date before Reg SHO publication
            log.debug("FINRA Reg SHO daily file not found for %s (status 404)", date_str)
            return []
        if status != 200:
            log.warning("FINRA Reg SHO daily returned status %d for %s", status, date_str)
            return []

        text = body.decode("utf-8", errors="replace")
        local_ts = time.time_ns()
        records: list[ShortVolume] = []
        sym_filter = symbol.upper() if symbol else None

        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("Date|"):
                continue
            parts = line.split("|")
            if len(parts) < 5:
                continue

            row_symbol = parts[1].strip().upper()
            if sym_filter and row_symbol != sym_filter:
                continue

            try:
                short_vol = float(parts[2].strip() or 0.0)
                short_exempt = float(parts[3].strip() or 0.0) if len(parts) > 3 else 0.0
                total_vol = float(parts[4].strip() or 0.0) if len(parts) > 4 else short_vol
            except (ValueError, TypeError):
                continue

            records.append(
                ShortVolume(
                    source="finra",
                    symbol=row_symbol,
                    symbol_raw=row_symbol,
                    source_ts=None,
                    local_ts=local_ts,
                    asset_class=AssetClass.EQUITY,
                    date=iso_date,
                    short_volume=short_vol,
                    short_exempt_volume=short_exempt,
                    total_volume=total_vol,
                )
            )

        return records

    async def get_regsho_short_volume(
        self,
        symbol: str,
        start_date: date | None = None,
        end_date: date | None = None,
        max_days: int = 30,
    ) -> list[ShortVolume]:
        """Fetch Reg SHO daily short sale volume across a range of business days.

        Defaults to recent trading days if not specified.
        """
        now = datetime.now(UTC).date()
        end = end_date or now
        start = start_date or (end - timedelta(days=max_days))

        # Clamp max date range to prevent excessive requests
        if (end - start).days > 365:
            start = end - timedelta(days=365)

        # Collect dates (skip Saturday and Sunday)
        dates_to_fetch: list[date] = []
        curr = start
        while curr <= end:
            if curr.weekday() < 5:  # Monday to Friday
                dates_to_fetch.append(curr)
            curr += timedelta(days=1)

        tasks = [self.get_regsho_short_volume_day(d, symbol=symbol) for d in dates_to_fetch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_records: list[ShortVolume] = []
        for r in results:
            if isinstance(r, list):
                all_records.extend(r)
            elif isinstance(r, Exception):
                log.warning("Failed fetching Reg SHO daily volume: %s", r)

        all_records.sort(key=lambda rec: rec.date)
        return all_records

    async def get_short_interest(
        self,
        symbol: str,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = 5000,
    ) -> list[ShortInterest]:
        """Fetch FINRA Consolidated Short Interest for a symbol.

        URL: https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest
        """
        sym_clean = symbol.upper()
        url = f"{_FINRA_API_URL}/consolidatedShortInterest"

        payload: dict[str, Any] = {
            "limit": limit,
            "compareFilters": [
                {"compareType": "equal", "fieldName": "symbolCode", "fieldValue": sym_clean}
            ],
        }

        if start_date and end_date:
            payload["dateRangeFilters"] = [
                {
                    "fieldName": "settlementDate",
                    "startDate": _format_iso(start_date),
                    "endDate": _format_iso(end_date),
                }
            ]
        elif start_date:
            payload["compareFilters"].append(
                {
                    "compareType": "gte",
                    "fieldName": "settlementDate",
                    "fieldValue": _format_iso(start_date),
                }
            )

        status, data = await self._post_json(url, payload)
        if status != 200 or not isinstance(data, list):
            log.warning("FINRA consolidatedShortInterest returned status %d: %s", status, data)
            return []

        local_ts = time.time_ns()
        records: list[ShortInterest] = []

        for item in data:
            if not isinstance(item, dict):
                continue
            settlement = item.get("settlementDate")
            if not settlement:
                continue

            current_qty = item.get("currentShortPositionQuantity")
            if current_qty is None:
                continue

            prev_qty = item.get("previousShortPositionQuantity")
            days_to_cover = item.get("daysToCoverQuantity")
            change_pct = item.get("changePercent")

            records.append(
                ShortInterest(
                    source="finra",
                    symbol=sym_clean,
                    symbol_raw=sym_clean,
                    source_ts=None,
                    local_ts=local_ts,
                    asset_class=AssetClass.EQUITY,
                    settlement_date=str(settlement)[:10],
                    short_interest=float(current_qty),
                    prev_short_interest=float(prev_qty) if prev_qty is not None else None,
                    days_to_cover=float(days_to_cover) if days_to_cover is not None else None,
                    change_pct=float(change_pct) if change_pct is not None else None,
                )
            )

        records.sort(key=lambda rec: rec.settlement_date)
        return records

    async def get_ats_weekly_summary(
        self,
        symbol: str,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Fetch FINRA Weekly Transparency Data for ATS (dark pools) and OTC non-ATS wholesalers.

        Discloses market participant volume per MPID (e.g. Citadel Securities, Virtu MatchIt,
        Liquidnet, Two Sigma, Jane Street).
        """
        sym_clean = symbol.upper()
        url = f"{_FINRA_API_URL}/weeklySummary"

        payload: dict[str, Any] = {
            "limit": limit,
            "compareFilters": [
                {
                    "compareType": "equal",
                    "fieldName": "issueSymbolIdentifier",
                    "fieldValue": sym_clean,
                }
            ],
        }

        if start_date and end_date:
            payload["dateRangeFilters"] = [
                {
                    "fieldName": "weekStartDate",
                    "startDate": _format_iso(start_date),
                    "endDate": _format_iso(end_date),
                }
            ]
        elif start_date:
            payload["compareFilters"].append(
                {
                    "compareType": "gte",
                    "fieldName": "weekStartDate",
                    "fieldValue": _format_iso(start_date),
                }
            )

        status, data = await self._post_json(url, payload)
        if status != 200 or not isinstance(data, list):
            log.warning("FINRA weeklySummary returned status %d: %s", status, data)
            return []

        return [row for row in data if isinstance(row, dict)]

    async def get_threshold_securities(self, target_date: date | None = None) -> list[str]:
        """Fetch Nasdaq Reg SHO Threshold Securities list for a settlement date."""
        target = target_date or datetime.now(UTC).date()
        date_str = _format_yyyymmdd(target)
        url = f"{_NASDAQ_THRESHOLD_URL}/nasdaqth{date_str}.txt"

        status, body = await self._get(url)
        if status != 200:
            return []

        text = body.decode("utf-8", errors="replace")
        symbols: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("Symbol|"):
                continue
            parts = line.split("|")
            if parts and parts[0]:
                symbols.append(parts[0].strip().upper())
        return symbols
