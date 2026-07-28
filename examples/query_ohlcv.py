"""Example: resample stored trade records into OHLCV bars and print them.

After collecting data with ``collect_deribit.py``, run this script to resample
BTC-PERPETUAL trades into 1-minute OHLCV bars and display the result.

Resampling is done entirely in-process against the local DuckDB catalog —
no exchange API calls are made at query time.

Usage::

    uv run python examples/query_ohlcv.py [--data-dir data] [--interval 1m]

Supported interval strings: 1s, 5s, 30s, 1m, 5m, 15m, 1h, 4h, 1d, 1w.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make this example runnable from a source checkout even without an editable
# install: put the repo's ``src/`` on sys.path so ``import crocodile`` resolves.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from crocodile.core.resample.ohlcv import resample_ohlcv  # noqa: E402
from crocodile.core.store.catalog import Catalog  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_NS_MAX = 9_223_372_036_854_775_807  # max int64 — covers full stored history


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resample Crocodile trade records into OHLCV bars."
    )
    parser.add_argument("--data-dir", default="data", help="Root of the data lake (default: data)")
    parser.add_argument(
        "--symbol",
        default="deribit:BTC-PERPETUAL",
        help="Canonical symbol (default: deribit:BTC-PERPETUAL)",
    )
    parser.add_argument(
        "--interval",
        default="1m",
        help="OHLCV bar width: 1s, 5s, 1m, 5m, 1h, 4h, 1d … (default: 1m)",
    )
    parser.add_argument("--from-ns", type=int, default=0, help="Start nanosecond UTC (default: 0)")
    parser.add_argument(
        "--to-ns", type=int, default=_NS_MAX, help=f"End nanosecond UTC (default: {_NS_MAX})"
    )
    parser.add_argument(
        "--fill-empty",
        action="store_true",
        default=False,
        help="Insert zero-volume rows for empty buckets (default: off)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum rows to display (default: 20; 0 = all)",
    )
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)

    if not data_dir.exists():
        print(
            f"Data lake not found: {data_dir}\n"
            "Run collect_deribit.py first to populate the data lake.",
            file=sys.stderr,
        )
        return 0

    catalog = Catalog(data_dir)

    print(
        f"Resampling {args.symbol!r}  interval={args.interval!r}  "
        f"[{args.from_ns} .. {args.to_ns}]"
    )

    df = resample_ohlcv(
        catalog=catalog,
        symbol=args.symbol,
        start_ns=args.from_ns,
        end_ns=args.to_ns,
        interval=args.interval,
        fill_empty=args.fill_empty,
    )

    if len(df) == 0:
        print("No trade data found for the requested symbol / time range.")
        print("Run collect_deribit.py first to populate the data lake.")
        return 0

    # Pretty-print a subset of bars.
    limit = args.limit if args.limit > 0 else len(df)
    display = df.head(limit)

    # Convert nanosecond bar timestamps to human-readable UTC strings.
    import datetime

    def _ns_to_utc(ns: int) -> str:
        return datetime.datetime.fromtimestamp(ns / 1e9, tz=datetime.UTC).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )

    print(
        f"\n{'bar (UTC)':<26} {'open':>12} {'high':>12} {'low':>12} {'close':>12} "
        f"{'volume':>14} {'trades':>8}"
    )
    print("-" * 104)
    for row in display.iter_rows(named=True):
        bar_str = _ns_to_utc(row["bar"])
        open_ = f"{row['open']:.2f}" if row["open"] is not None else "-"
        high = f"{row['high']:.2f}" if row["high"] is not None else "-"
        low = f"{row['low']:.2f}" if row["low"] is not None else "-"
        close = f"{row['close']:.2f}" if row["close"] is not None else "-"
        vol = f"{row['volume']:.4f}"
        trades = str(row["num_trades"])
        print(
            f"{bar_str:<26} {open_:>12} {high:>12} {low:>12} {close:>12} {vol:>14} {trades:>8}"
        )

    if len(df) > limit:
        print(f"... ({len(df) - limit} more bars; use --limit 0 to see all)")

    print(
        f"\nTotal bars: {len(df):,}  |  "
        f"Total volume: {df['volume'].sum():.4f}  |  "
        f"Total trades: {df['num_trades'].sum():,}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
