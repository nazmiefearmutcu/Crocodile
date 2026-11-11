"""Operator tool: measure TradingView's scanner market by market, or record EOD answers.

Thin CLI over ``crocodile.equity.universal.tradingview``. Two modes, both run with the project
interpreter from the worktree root; tests never run this script.

- Sweep (default): one ``POST /{market}/scan`` per non-sanctioned candidate in
  ``SWEEP_CANDIDATES`` (or the ``--markets`` subset) with ``UNIVERSE_COLUMNS`` and range
  ``[0, 1000]``, then the market table CSV is written to ``--out``. Sanctioned candidates get no
  request. The first wall (403, 451, a bot page, a timeout) stops the sweep: it is recorded in its
  row and the remaining candidates are marked not probed. Nothing is retried around it.
- ``--eod-only a,b``: one ``POST /{market}/scan`` per named market with ``--columns`` (default
  ``EOD_COLUMNS``) and range ``[0, 25]``. No CSV is written; the capture instant is printed.

``--record-dir`` keeps every raw answer outside the repository: ``<market>.json`` for a sweep
answer, ``<market>_eod.json`` for an EOD answer, a ``<stem>.request.json`` with the request body
and the UTC capture instant under ``_captured_at``, and one line per request in
``requests.jsonl``.

The evidence date is an argument, never the clock, so a recorded table names the day it was
measured.

Usage::

    .venv/Scripts/python.exe scripts/measure_tradingview_markets.py --evidence-date 2026-11-11 \\
        --out src/crocodile/equity/universal/data/tradingview_markets.csv --record-dir DIR
    .venv/Scripts/python.exe scripts/measure_tradingview_markets.py --eod-only turkey,vietnam \\
        --columns name,exchange,currency,open,high,low,close,volume,update_time --record-dir DIR
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

from crocodile.equity.providers.base import ProviderError
from crocodile.equity.universal import tradingview as tv
from crocodile.equity.universal.base import SourceUnservableError
from crocodile.equity.universal.http import AiohttpFetcher, HttpRequest, HttpResponse

EOD_ROWS: int = 25
_FIELDS: tuple[str, ...] = (
    "market",
    "country",
    "http_status",
    "total_count",
    "exchange_codes",
    "currencies",
    "evidence_date",
    "notes",
)


class _RecordingFetcher:
    """Wraps the shared fetcher: writes each raw scan answer and logs one line per request.

    It only observes; the client still sees the same response and decides what it means.
    """

    def __init__(self, inner: AiohttpFetcher, record_dir: Path | None, suffix: str) -> None:
        self._inner = inner
        self._dir = record_dir
        self._suffix = suffix

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        captured = datetime.now(UTC).replace(microsecond=0)
        started = time.perf_counter()
        response = await self._inner(request)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        if self._dir is not None:
            self._dir.mkdir(parents=True, exist_ok=True)
            market = request.url.split("/")[3]
            stem = f"{market}{self._suffix}"
            (self._dir / f"{stem}.json").write_bytes(response.body)
            body = dict(request.json_body or {})
            body["_captured_at"] = captured.isoformat().replace("+00:00", "Z")
            (self._dir / f"{stem}.request.json").write_text(
                json.dumps(body, ensure_ascii=True, indent=1, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            line = {
                "stem": stem,
                "method": request.method,
                "url": request.url,
                "status": response.status,
                "elapsed_ms": elapsed_ms,
                "bytes": len(response.body),
                "captured_at": body["_captured_at"],
            }
            with (self._dir / "requests.jsonl").open("a", encoding="utf-8", newline="\n") as log:
                log.write(json.dumps(line, sort_keys=True) + "\n")
        return response


def _names(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--evidence-date",
        type=date.fromisoformat,
        help="ISO date (YYYY-MM-DD) recorded as the evidence date of every probed row (sweep)",
    )
    parser.add_argument("--out", type=Path, help="CSV path to write (sweep; required there)")
    parser.add_argument("--record-dir", type=Path, help="directory for the raw answers")
    parser.add_argument("--markets", type=_names, help="comma list: sweep only these candidates")
    parser.add_argument("--eod-only", type=_names, help="comma list: record EOD pages only")
    parser.add_argument("--columns", type=_names, help="EOD columns (default: tv.EOD_COLUMNS)")
    args = parser.parse_args(argv)
    known = set(tv.SWEEP_CANDIDATES)
    if args.eod_only is not None:
        if args.record_dir is None:
            parser.error("--eod-only needs --record-dir so the raw answer is kept")
        unknown = sorted(set(args.eod_only) - known)
        if unknown:
            parser.error(f"unknown market(s) for --eod-only: {', '.join(unknown)}")
        return args
    if args.evidence_date is None or args.out is None:
        parser.error("a sweep needs --evidence-date and --out")
    if args.markets is not None:
        unknown = sorted(set(args.markets) - known)
        if unknown:
            parser.error(f"unknown candidate market(s): {', '.join(unknown)}")
    return args


def _note_only(market: str, country: str, evidence: date, note: str) -> tv.MarketMeasurement:
    return tv.MarketMeasurement(
        market=market,
        country=country,
        http_status=None,
        total_count=None,
        exchange_codes={},
        currencies=(),
        examples={},
        evidence_date=evidence,
        notes=note,
    )


async def _sweep(
    markets: list[str], evidence: date, record_dir: Path | None
) -> list[tv.MarketMeasurement]:
    inner = AiohttpFetcher()
    client = tv.TradingViewClient(fetch=_RecordingFetcher(inner, record_dir, ""))
    results: list[tv.MarketMeasurement] = []
    try:
        for index, market in enumerate(markets):
            country = tv.SWEEP_CANDIDATES[market]
            try:
                results.extend(
                    await tv.measure_markets(
                        client,
                        {market: country},
                        columns=tv.UNIVERSE_COLUMNS,
                        evidence_date=evidence,
                        probe_rows=tv.PAGE_SIZE,
                    )
                )
            except SourceUnservableError as exc:
                results.append(
                    _note_only(
                        market,
                        country,
                        evidence,
                        f"refused: {exc.reason.value} ({exc.detail}); sweep stopped here, "
                        "not retried",
                    )
                )
                for later in markets[index + 1 :]:
                    results.append(
                        _note_only(
                            later,
                            tv.SWEEP_CANDIDATES[later],
                            evidence,
                            f"not probed: sweep stopped after a refusal at {market}",
                        )
                    )
                break
            except ProviderError as exc:
                results.append(
                    _note_only(
                        market,
                        country,
                        evidence,
                        f"probe failed: {type(exc).__name__}: {str(exc)[:200]}",
                    )
                )
    finally:
        await client.close()
        await inner.close()
    return sorted(results, key=lambda item: item.market)


async def _eod(markets: list[str], columns: list[str], record_dir: Path | None) -> int:
    inner = AiohttpFetcher()
    client = tv.TradingViewClient(fetch=_RecordingFetcher(inner, record_dir, "_eod"))
    status = 0
    try:
        for market in markets:
            captured = datetime.now(UTC).replace(microsecond=0)
            try:
                result = await client.scan(market, columns, max_rows=EOD_ROWS)
            except SourceUnservableError as exc:
                print(f"{market}: refused ({exc.reason.value}, {exc.detail}); stopped, not retried")
                status = 1
                break
            print(
                f"{market}: http {result.http_status} totalCount {result.total_count} "
                f"rows {len(result.rows)} captured_at {captured.isoformat()}"
            )
    finally:
        await client.close()
        await inner.close()
    return status


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.eod_only is not None:
        columns = args.columns if args.columns is not None else list(tv.EOD_COLUMNS)
        return asyncio.run(_eod(args.eod_only, columns, args.record_dir))
    markets = args.markets if args.markets is not None else list(tv.SWEEP_CANDIDATES)
    measurements = asyncio.run(_sweep(markets, args.evidence_date, args.record_dir))
    rows = tv.market_table_rows(measurements)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_FIELDS), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    answered = sum(1 for item in measurements if item.http_status == 200)
    missing = sum(1 for item in measurements if item.http_status == 404)
    print(
        f"wrote {len(rows)} rows to {args.out}: {answered} answered 200, "
        f"{missing} answered 404, {len(rows) - answered - missing} not probed or other"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
