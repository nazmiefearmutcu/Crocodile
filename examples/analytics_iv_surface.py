"""Example: compute the IV surface and ATM term structure from stored options data.

Reads the ``options_chain`` channel from a local Parquet data lake, takes a
snapshot at the latest stored instant, and prints:

  1. The implied-vol surface (expiry x strike x opt_type grid).
  2. The ATM IV term structure (per-expiry ATM vol vs days to expiry).

All computation is done offline — no exchange API calls.  IV is taken directly
from ``mark_iv`` when available; otherwise it is computed via the Black-76 solver
using ``mark_price + underlying_price``.

Usage::

    uv run python examples/analytics_iv_surface.py [--data-dir data] [--underlying BTC]

Prerequisites: collect options data first, e.g.::

    uv run crocodile collect \\
      --asset-class crypto \\
      --sources deribit \\
      --symbols BTC-PERPETUAL \\
      --channels options_chain \\
      --data-dir data
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

# Make this example runnable from a source checkout even without an editable
# install (e.g. plain `python examples/analytics_iv_surface.py`, or whenever the
# editable `.pth` is not honored): put the repo's ``src/`` on sys.path so that
# ``import crocodile`` resolves in this and any spawned subprocess.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_NS_MAX = 9_223_372_036_854_775_807


def _ns_to_utc_date(ns: int) -> str:
    """Convert a nanosecond epoch integer to a date string (UTC)."""
    return datetime.datetime.fromtimestamp(ns / 1e9, tz=datetime.UTC).strftime("%Y-%m-%d")


def main(argv: list[str] | None = None) -> int:
    """Entry-point for the analytics_iv_surface example script.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code: 0 on success (including "no data" — exit 0 with a message).
    """
    parser = argparse.ArgumentParser(
        description="Print IV surface and term structure from a Crocodile data lake."
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Root of the data lake (default: data)",
    )
    parser.add_argument(
        "--underlying",
        default="BTC",
        help="Underlying asset identifier (default: BTC)",
    )
    parser.add_argument(
        "--at-ns",
        type=int,
        default=None,
        help=(
            "Snapshot instant (nanoseconds UTC).  "
            "Defaults to the latest available row for the underlying."
        ),
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=0.0,
        help="Continuous risk-free rate for the IV solver (default: 0.0)",
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

    from crocodile.crypto.analytics.volsurface import iv_surface, term_structure
    from crocodile.core.store.catalog import Catalog

    catalog = Catalog(data_dir)

    # Determine the snapshot instant.
    at_ns: int
    if args.at_ns is not None:
        at_ns = args.at_ns
    else:
        # Default: latest local_ts in the options_chain table for this underlying.
        # Use the public catalog.query() API with the underlying literal inlined
        # as a safe SQL string (no user-controlled input reaches this path;
        # args.underlying comes from --underlying which defaults to "BTC").
        try:
            underlying_escaped = args.underlying.replace("'", "''")
            sql = (
                f"SELECT MAX(local_ts) FROM options_chain"
                f" WHERE underlying = '{underlying_escaped}'"
            )
            result_df = catalog.query(sql)
            val = result_df[0, 0] if len(result_df) > 0 else None
            at_ns = int(val) if val is not None else _NS_MAX
        except Exception:
            at_ns = _NS_MAX

    at_str = _ns_to_utc_date(at_ns) if at_ns < _NS_MAX else "latest"
    print(
        f"IV Surface — underlying: {args.underlying!r}  "
        f"at: {at_str}  rate: {args.rate}"
    )

    # ---------- IV surface ----------
    surface = iv_surface(catalog, args.underlying, at_ns, rate=args.rate)

    if len(surface) == 0:
        print(
            f"\nNo options data found for underlying {args.underlying!r}.\n"
            "Collect options_chain data first:\n"
            "  uv run crocodile collect --asset-class crypto --sources deribit "
            "--channels options_chain --data-dir data"
        )
        return 0

    print(f"\n{'expiry (UTC)':<14} {'strike':>10} {'type':>5} {'moneyness':>10} "
          f"{'iv':>8} {'source':>12}")
    print("-" * 68)
    for row in surface.sort(["expiry", "strike"]).to_dicts():
        expiry_str = _ns_to_utc_date(row["expiry"])
        strike = f"{row['strike']:.1f}"
        opt_type = str(row["opt_type"])
        moneyness = f"{row['moneyness']:.4f}" if row["moneyness"] is not None else "-"
        iv_val = f"{row['iv']:.4f}" if row["iv"] is not None else "N/A"
        source = str(row["source"])
        print(
            f"{expiry_str:<14} {strike:>10} {opt_type:>5} {moneyness:>10} "
            f"{iv_val:>8} {source:>12}"
        )

    print(f"\nTotal options rows: {len(surface):,}")

    # ---------- ATM term structure ----------
    ts_df = term_structure(catalog, args.underlying, at_ns, rate=args.rate)

    if len(ts_df) == 0:
        print("\nCould not compute term structure (no ATM data).")
        return 0

    print(
        f"\n{'expiry (UTC)':<14} {'days':>8} {'atm_strike':>12} {'atm_iv':>10}"
    )
    print("-" * 50)
    for row in ts_df.to_dicts():
        expiry_str = _ns_to_utc_date(row["expiry"])
        days = f"{row['days_to_expiry']:.1f}"
        atm_strike = f"{row['atm_strike']:.1f}"
        atm_iv = f"{row['atm_iv']:.4f}" if row["atm_iv"] is not None else "N/A"
        print(f"{expiry_str:<14} {days:>8} {atm_strike:>12} {atm_iv:>10}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
