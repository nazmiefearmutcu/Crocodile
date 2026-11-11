"""Deterministic generator for the packaged per-market source order (spec 3.10d).

Builds ``src/crocodile/equity/universal/data/source_order.csv``: one row per
``(mic, channel)`` for every MIC in ``exchanges.csv`` and every channel in
``ORDER_CHANNELS``. The rows come from the live registries only (the MIC allow-list and the
equity provider factory). The script reads no clock and no network.

The rules, implemented by ``crocodile.equity.universal.order.build_default_rows``:

* ``ohlcv``: US natives first (on US rows only), then ``yahoo_chart`` and ``tradingview``,
  then any other universal source that serves the pair, sorted by name.
* ``instrument``: the directory sources that serve the MIC, sorted, then ``tradingview``,
  then ``yahoo_chart`` (filtered out while it does not declare ``instrument``).
* ``corp_action``: US natives first (US rows only), then ``yahoo_chart``, then any other
  universal source that serves the pair.

Natives are never placed in a non-US row. The table is data an operator may later edit. A
hand edit is an operator decision, and ``--check`` then reports the difference until the
table is regenerated from the registries.

Usage, from the repository root, with the project interpreter::

    .venv/Scripts/python.exe scripts/build_source_order.py          # write the table
    .venv/Scripts/python.exe scripts/build_source_order.py --check  # exit 0 when up to date

The file is written as UTF-8 with ``\\n`` line endings. ``--check`` compares it with ``\\r\\n``
normalised to ``\\n``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
TARGET: Path = (
    REPO_ROOT / "src" / "crocodile" / "equity" / "universal" / "data" / "source_order.csv"
)


def build_text() -> str:
    """The validated table as CSV text. Raises ``SourceOrderError`` when the registries disagree."""
    from crocodile.core.markets.exchanges import ExchangeRegistry
    from crocodile.equity.universal.order import (
        SourceOrder,
        build_default_rows,
        registry_capabilities,
    )

    exchanges = ExchangeRegistry().all()
    capabilities = registry_capabilities()
    order = SourceOrder(build_default_rows(exchanges, capabilities))
    order.validate(
        capabilities=capabilities,
        known_mics=frozenset(info.mic for info in exchanges),
    )
    return order.to_csv()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when the committed table differs from the generated one",
    )
    args = parser.parse_args(argv)
    text = build_text()
    if args.check:
        current = ""
        if TARGET.exists():
            current = TARGET.read_text(encoding="utf-8").replace("\r\n", "\n")
        if current != text:
            print(
                f"{TARGET} is out of date; run scripts/build_source_order.py",
                file=sys.stderr,
            )
            return 1
        print(f"{TARGET} is up to date")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
