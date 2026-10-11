"""Official anonymous OKX history catalogue; no archive download side effects."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import aiohttp

from .schema import ArchiveListing, ArchiveObject, ArchiveRequest, DownloadError

CATALOGUE_URL = "https://www.okx.com/api/v5/public/market-data-history"
OFFICIAL_DOC = "https://www.okx.com/docs-v5/en/#public-data-rest-api-get-historical-market-data"


def _month_after(day: date, count: int) -> date:
    index = day.year * 12 + day.month - 1 + count
    return date(index // 12, index % 12 + 1, 1)


def _windows(request: ArchiveRequest) -> list[tuple[date, date]]:
    cursor = request.start_date
    windows = []
    while cursor <= request.end_date:
        last = (
            cursor + timedelta(days=9)
            if request.aggregation == "daily"
            else _month_after(cursor.replace(day=1), 10) - timedelta(days=1)
        )
        last = min(last, request.end_date)
        windows.append((cursor, last))
        cursor = last + timedelta(days=1)
    return windows


def _ms(day: date) -> str:
    return str(int(datetime.combine(day, datetime.min.time(), UTC).timestamp()) * 1000)


async def list_okx_archives(
    request: ArchiveRequest,
    *,
    session: Any | None = None,
) -> ArchiveListing:
    """Split UTC date windows, stay below 5 requests/2 seconds, expose missing dates."""
    if session is None:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as owned:
            return await list_okx_archives(request, session=owned)
    objects: dict[str, ArchiveObject] = {}
    covered: set[str] = set()
    for index, (begin, end) in enumerate(_windows(request)):
        if index:
            await asyncio.sleep(0.41)
        params = {
            "module": str(request.module),
            "instType": request.product,
            "dateAggrType": request.aggregation,
            "begin": _ms(begin),
            "end": _ms(end),
            "instIdList" if request.product == "SPOT" else "instFamilyList": request.selector,
        }
        async with session.get(CATALOGUE_URL, params=params, allow_redirects=False) as response:
            if response.status != 200:
                raise DownloadError(f"OKX catalogue HTTP {response.status}")
            payload = await response.json()
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            raise DownloadError("OKX catalogue returned an error envelope")
        for envelope in payload.get("data", []):
            for detail in envelope.get("details", []):
                if detail.get("instType", request.product) != request.product:
                    raise DownloadError("OKX catalogue product mismatch")
                for item in detail.get("groupDetails", []):
                    filename = str(item["filename"])
                    symbol = str(detail.get("instId", "")) or filename.split("-L2orderbook-", 1)[0]
                    if symbol != request.symbol:
                        continue  # A family response may include other dated instruments.
                    stamp = item.get("dateTs", item.get("dataTs"))
                    if stamp is None:
                        raise DownloadError("OKX catalogue object lacks source date")
                    source_day = datetime.fromtimestamp(int(stamp) / 1000, UTC).date()
                    if (
                        request.aggregation == "daily"
                        and not request.start_date <= source_day <= request.end_date
                    ):
                        raise DownloadError("OKX catalogue returned an out-of-range date")
                    obj = ArchiveObject(
                        "okx",
                        request.product,
                        symbol,
                        request.module,
                        source_day.isoformat(),
                        filename,
                        str(item["url"]),
                        "tar-ndjson" if filename.endswith(".tar.gz") else "unsupported-legacy-csv",
                        400 if request.module == 4 else 5000,
                        int(Decimal(str(item.get("sizeMB", "0"))) * 1024 * 1024),
                        schema_status="object_not_audited",
                    )
                    objects[obj.identity] = obj
                    if request.aggregation == "daily":
                        covered.add(source_day.isoformat())
                    else:
                        # Monthly files cover a nominal source month, not proven daily continuity.
                        day = max(request.start_date, source_day.replace(day=1))
                        stop = min(
                            request.end_date,
                            _month_after(source_day.replace(day=1), 1) - timedelta(days=1),
                        )
                        while day <= stop:
                            covered.add(day.isoformat())
                            day += timedelta(days=1)
    expected = []
    day = request.start_date
    while day <= request.end_date:
        expected.append(day.isoformat())
        day += timedelta(days=1)
    return ArchiveListing(
        request,
        tuple(sorted(objects.values(), key=lambda o: (o.source_date, o.filename))),
        tuple(day for day in expected if day not in covered),
        time.time_ns(),
    )
