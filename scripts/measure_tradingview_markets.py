"""Operator tool: probe TradingView's scanner market by market and write the market table.

Thin CLI over ``crocodile.equity.universal.tradingview.measure_markets``. It sends one scan per
non-sanctioned candidate in ``SWEEP_CANDIDATES`` (the shared budget spaces the requests about one
second apart), then writes ``data/tradingview_markets.csv`` with the measured status, total,
exchange codes and currencies of each candidate. Sanctioned candidates are never requested.

The evidence date is an argument, never the clock, so a recorded table names the day it was
measured. Tests do not run this script; the live sweep is an operator action.

Usage (from the worktree root, with the project interpreter)::

    .venv/Scripts/python.exe scripts/measure_tradingview_markets.py --evidence-date 2026-11-11
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from datetime import date
from pathlib import Path

from crocodile.equity.universal import tradingview as tv

_DEFAULT_OUT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "crocodile"
    / "equity"
    / "universal"
    / "data"
    / "tradingview_markets.csv"
)
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


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--evidence-date",
        type=date.fromisoformat,
        required=True,
        help="ISO date (YYYY-MM-DD) recorded as the evidence date of every probed row",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_OUT,
        help="CSV path to write (default: the packaged data file)",
    )
    return parser.parse_args(argv)


async def _sweep(evidence: date) -> list[tv.MarketMeasurement]:
    client = tv.TradingViewClient()
    try:
        return await tv.measure_markets(
            client,
            tv.SWEEP_CANDIDATES,
            columns=tv.PROBE_COLUMNS,
            evidence_date=evidence,
            probe_rows=tv.PAGE_SIZE,
        )
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    measurements = asyncio.run(_sweep(args.evidence_date))
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
