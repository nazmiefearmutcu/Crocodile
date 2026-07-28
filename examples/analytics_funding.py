"""Example: compute funding APR and cumulative funding from stored data.

Reads the ``funding`` channel from a local Parquet data lake and prints a
per-event breakdown of the annualised funding rate (APR) plus cumulative
funding.  All computation is done offline — no exchange API calls.

Positive ``funding_rate`` means longs pay shorts.

Usage::

    uv run python examples/analytics_funding.py [--data-dir data] [--symbol deribit:BTC-PERPETUAL]

Prerequisites: collect funding data first, e.g.::

    uv run crocodile collect \\
      --asset-class crypto \\
      --sources deribit \\
      --symbols BTC-PERPETUAL \\
      --channels funding \\
      --data-dir data
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

# Make this example runnable from a source checkout even without an editable
# install (e.g. plain `python examples/analytics_funding.py`, or whenever the
# editable `.pth` is not honored): put the repo's ``src/`` on sys.path so that
# ``import crocodile`` resolves in this and any spawned subprocess.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_NS_MAX = 9_223_372_036_854_775_807  # max int64 — full stored history


def _ns_to_utc(ns: int) -> str:
    """Convert a nanosecond epoch integer to a human-readable UTC string."""
    return datetime.datetime.fromtimestamp(ns / 1e9, tz=datetime.UTC).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def main(argv: list[str] | None = None) -> int:
    """Entry-point for the analytics_funding example script.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code: 0 on success (including "no data" — exit 0 with a message).
    """
    parser = argparse.ArgumentParser(
        description="Print funding APR time series from a Crocodile data lake."
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Root of the data lake (default: data)",
    )
    parser.add_argument(
        "--symbol",
        default="deribit:BTC-PERPETUAL",
        help="Canonical symbol (default: deribit:BTC-PERPETUAL)",
    )
    parser.add_argument(
        "--from-ns",
        type=int,
        default=0,
        help="Start nanosecond UTC (default: 0 = full history)",
    )
    parser.add_argument(
        "--to-ns",
        type=int,
        default=_NS_MAX,
        help=f"End nanosecond UTC (default: {_NS_MAX})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum rows to display (default: 20; 0 = all)",
    )
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)

    # Graceful message if the data directory does not exist at all.
    if not data_dir.exists():
        print(
            f"Data lake not found: {data_dir}\n"
            "Run 'crocodile collect' first to populate it.",
            file=sys.stderr,
        )
        return 0

    from crocodile.crypto.analytics.funding import funding_apr, funding_summary
    from crocodile.core.store.catalog import Catalog

    catalog = Catalog(data_dir)

    print(
        f"Funding APR: {args.symbol!r}  [{args.from_ns} .. {args.to_ns}]"
    )

    df = funding_apr(catalog, args.symbol, args.from_ns, args.to_ns)

    if len(df) == 0:
        print(
            f"\nNo funding data found for {args.symbol!r} in the requested range.\n"
            "Collect funding data first:\n"
            "  uv run crocodile collect --asset-class crypto --sources deribit "
            "--symbols BTC-PERPETUAL --channels funding --data-dir data"
        )
        return 0

    # Summary row
    summary = funding_summary(catalog, args.symbol, args.from_ns, args.to_ns)
    if len(summary) > 0:
        s = summary.row(0, named=True)
        print(
            f"\nSummary — events: {s['n_events']}  "
            f"mean_rate: {s['mean_rate']:.6f}  "
            f"mean_apr: {s['mean_apr']:.4%}  "
            f"total_funding: {s['total_funding']:.6f}"
        )

    # Per-event table
    limit = args.limit if args.limit > 0 else len(df)
    display = df.head(limit)

    print(
        f"\n{'timestamp (UTC)':<26} {'funding_rate':>14} {'interval_h':>11} "
        f"{'apr':>10} {'cumulative':>12}"
    )
    print("-" * 80)
    for row in display.iter_rows(named=True):
        ts_str = _ns_to_utc(row["funding_ts"])
        rate = f"{row['funding_rate']:+.6f}"
        ih = str(row["interval_hours"])
        apr = f"{row['apr']:.4%}"
        cum = f"{row['cumulative_funding']:+.6f}"
        print(f"{ts_str:<26} {rate:>14} {ih:>11} {apr:>10} {cum:>12}")

    if len(df) > limit:
        print(f"... ({len(df) - limit} more rows; use --limit 0 to see all)")

    print(
        f"\nTotal events: {len(df):,}  |  "
        f"Cumulative funding: {df['cumulative_funding'][-1]:.6f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
