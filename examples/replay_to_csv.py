"""Example: replay stored records from the data lake and export to CSV.

This script reads trade records for BTC-PERPETUAL from the local data lake
(written by ``collect_deribit.py`` or any other collection run), replays them
in ``local_ts`` order, and writes the result to a CSV file.

The replay engine uses the M2 k-way merge so records across multiple symbols
(if requested) are globally time-ordered.

Usage::

    uv run python examples/replay_to_csv.py [--data-dir data] [--out trades.csv]

Adjust ``START_NS`` / ``END_NS`` to narrow the time range.  The defaults
(0 and 9_223_372_036_854_775_807) cover the entire history in the lake.
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

from crocodile.crypto.client.client import CrypcodileClient  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_NS_MAX = 9_223_372_036_854_775_807  # max int64 — covers all stored history


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay trade records from the Crocodile data lake and write to CSV."
    )
    parser.add_argument("--data-dir", default="data", help="Root of the data lake (default: data)")
    parser.add_argument("--out", default="trades.csv", help="Output CSV path (default: trades.csv)")
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["deribit:BTC-PERPETUAL"],
        help="Canonical symbol(s) to include (default: deribit:BTC-PERPETUAL)",
    )
    parser.add_argument("--from-ns", type=int, default=0, help="Start nanosecond UTC (default: 0)")
    parser.add_argument(
        "--to-ns", type=int, default=_NS_MAX, help=f"End nanosecond UTC (default: {_NS_MAX})"
    )
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    out_path = Path(args.out)

    if not data_dir.exists():
        print(f"Error: data directory not found: {data_dir}", file=sys.stderr)
        print("Run collect_deribit.py first to populate the data lake.", file=sys.stderr)
        return 1

    client = CrypcodileClient(data_dir=data_dir)

    print(f"Replaying trades for {args.symbols}  [{args.from_ns} .. {args.to_ns}]")
    print(f"Writing to {out_path} ...")

    # Export directly from the catalog scan (faster than iterating via replay()
    # when we only need a single channel).
    client.export(
        channel="trade",
        symbols=args.symbols,
        frm=args.from_ns,
        to=args.to_ns,
        fmt="csv",
        dest=out_path,
    )

    # Show a summary by counting lines in the produced CSV.
    if out_path.exists():
        lines = out_path.read_text().count("\n")
        rows = max(0, lines - 1)  # subtract header line
        print(f"Done. {rows:,} trade(s) written to {out_path}")
    else:
        print("No data matched the requested range.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
