#!/usr/bin/env python
"""Measure the live layer against every allow-listed market. Real network, never run by tests.

For each open market with a live source chain the script picks one listing (the first row of
the market's board when a source can enumerate it, otherwise the sample symbol of the source
grammar), polls it once through :class:`crocodile.equity.universal.live.feed.LiveFeed`, and
records what came back: which source answered, the tier the snapshot actually carried, how many
levels, and which session figures were stated. Nothing is retried and no wall is worked around:
a market whose sources all refuse is recorded as such.

It writes ``<out>.json`` (one object per market) and ``<out>.md`` (the table for
``docs/live-coverage.md``). Requests are sequential per market and bounded by ``--max-markets``.

Usage::

    python scripts/measure_live_coverage.py --out docs/live-coverage
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import importlib.resources
import io
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.grammar import PrefixGrammar, SuffixGrammar, load_grammar
from crocodile.equity.universal.live.feed import LiveFeed
from crocodile.equity.universal.live.model import LiveListing
from crocodile.equity.universal.live.registry import coverage, sources_for
from crocodile.equity.universal.live.sources.yahoo_chart import extra_suffixes

# Listings asked of the dedicated sources, whose markets have no enumerating source ahead of
# them in the chain or whose first board row is not a liquid name.
SAMPLES: dict[str, str] = {
    "XTAI": "2330",
    "ROCO": "6488",
    "XSHG": "600519",
    "XSHE": "000001",
    "BJSE": "920002",
    "XHKG": "700",
    "XKRX": "005930",
    "XSTC": "VNM",
    "HSTC": "SHS",
    "XNSE": "RELIANCE",
    "XETR": "SAP",
    "XFRA": "SAP",
    "TGAT": "SAP",
    "XSES": "D05",
    "XASX": "CBA",
    "BVMF": "PETR4",
    "XSWX": "NESN",
    "XTSE": "SHOP",
    "XTSX": "TNR",
    "XNAS": "AAPL",
    "XNYS": "IBM",
}


@dataclass(frozen=True, slots=True)
class Row:
    mic: str
    country: str
    name: str
    declared_tier: str
    sample: str
    status: str
    source: str
    tier: str
    bid_levels: int
    ask_levels: int
    has_last: bool
    has_prev_close: bool
    has_volume: bool
    has_tape: bool
    delay_s: int | None
    board_rows: int
    chain: list[str]
    note: str


def _extra_sample(mic: str) -> str | None:
    """The sample symbol the live layer's own Yahoo table was verified with."""
    raw = (
        importlib.resources.files("crocodile.equity.universal.live")
        .joinpath("data/yahoo_extra.csv")
        .read_text("ascii")
    )
    for row in csv.DictReader(io.StringIO(raw)):
        if row["mic"] == mic:
            return row["sample_local"]
    return None


def _grammar_sample(mic: str) -> str | None:
    tradingview = load_grammar("tradingview")
    if isinstance(tradingview, PrefixGrammar):
        for row in tradingview.rows:
            if row.mic == mic and row.sample_local:
                return row.sample_local
    yahoo = load_grammar("yahoo_chart")
    if isinstance(yahoo, SuffixGrammar) and mic in yahoo.supported_mics():
        for row in yahoo.rows:
            if row.mic == mic and row.sample_local:
                return row.sample_local
    return None


async def measure(max_markets: int | None) -> list[Row]:
    registry = ExchangeRegistry()
    feed = LiveFeed()
    rows: list[Row] = []
    try:
        for index, cover in enumerate(coverage()):
            if max_markets is not None and index >= max_markets:
                break
            info = registry.get(cover.mic)
            chain = [cls.name for cls in sources_for(cover.mic)] if not cover.closed else []
            base = dict(
                mic=cover.mic,
                country=info.country,
                name=info.name.title(),
                chain=chain,
                declared_tier=cover.tier.value if cover.tier else "",
            )
            if cover.closed:
                rows.append(
                    Row(
                        **base,
                        sample="",
                        status="closed",
                        source="",
                        tier="",
                        bid_levels=0,
                        ask_levels=0,
                        has_last=False,
                        has_prev_close=False,
                        has_volume=False,
                        has_tape=False,
                        delay_s=None,
                        board_rows=0,
                        note="sanctioned, default-closed",
                    )
                )
                continue
            if not chain:
                rows.append(
                    Row(
                        **base,
                        sample="",
                        status="no_source",
                        source="",
                        tier="",
                        bid_levels=0,
                        ask_levels=0,
                        has_last=False,
                        has_prev_close=False,
                        has_volume=False,
                        has_tape=False,
                        delay_s=None,
                        board_rows=0,
                        note="no keyless source verified",
                    )
                )
                continue
            board_rows = 0
            sample = SAMPLES.get(cover.mic)
            note = ""
            if cover.enumerates:
                try:
                    board = await feed.board(cover.mic, limit=50)
                except Exception as exc:
                    board, note = [], f"board failed: {type(exc).__name__}"
                board_rows = len(board)
                if sample is None and board:
                    priced = [row for row in board if row.last is not None]
                    sample = (priced or board)[0].listing.local_symbol
            if sample is None:
                sample = _grammar_sample(cover.mic)
            if sample is None and cover.mic in extra_suffixes():
                sample = _extra_sample(cover.mic)
            if sample is None:
                rows.append(
                    Row(
                        **base,
                        sample="",
                        status="no_sample",
                        source="",
                        tier="",
                        bid_levels=0,
                        ask_levels=0,
                        has_last=False,
                        has_prev_close=False,
                        has_volume=False,
                        has_tape=False,
                        delay_s=None,
                        board_rows=board_rows,
                        note=note or "no sample symbol known",
                    )
                )
                continue
            listing = await feed.identify(LiveListing(mic=cover.mic, local_symbol=sample))
            try:
                snapshot = await feed.poll(listing)
            except Exception as exc:
                snapshot, note = None, f"poll failed: {type(exc).__name__}: {exc}"[:120]
            if snapshot is None:
                walls = "; ".join(f"{w.source}={w.reason}" for w in feed.walls if w.source in chain)
                rows.append(
                    Row(
                        **base,
                        sample=sample,
                        status="no_answer",
                        source="",
                        tier="",
                        bid_levels=0,
                        ask_levels=0,
                        has_last=False,
                        has_prev_close=False,
                        has_volume=False,
                        has_tape=False,
                        delay_s=None,
                        board_rows=board_rows,
                        note=(note + " " + walls).strip(),
                    )
                )
                continue
            rows.append(
                Row(
                    **base,
                    sample=sample,
                    status="ok",
                    source=snapshot.source,
                    tier=snapshot.tier.value,
                    bid_levels=len(snapshot.bids),
                    ask_levels=len(snapshot.asks),
                    has_last=snapshot.last is not None,
                    has_prev_close=snapshot.prev_close is not None,
                    has_volume=snapshot.volume is not None,
                    has_tape=bool(snapshot.prints) or snapshot.last_size is not None,
                    delay_s=snapshot.delay_s,
                    board_rows=board_rows,
                    note=note,
                )
            )
            print(
                f"{cover.mic} {sample:14} {rows[-1].status} {rows[-1].source} {rows[-1].tier}",
                flush=True,
            )
    finally:
        await feed.close()
    return rows


def markdown(rows: list[Row], when: str) -> str:
    ok = [row for row in rows if row.status == "ok"]
    by_tier = {tier: sum(1 for row in ok if row.tier == tier) for tier in ("l2", "l1", "last")}
    lines = [
        "# Live market coverage (Crocodile 0.5.0)",
        "",
        f"Measured on {when} by `scripts/measure_live_coverage.py`: one listing per market,",
        "one poll, through the live source chain of `crocodile.equity.universal.live`. A row is",
        "what the publisher answered at that moment, not a promise: a board outside its session",
        "shows the last session's figures, and a ladder can be shorter in an auction.",
        "",
        f"- Markets in the allow-list: **{len(rows)}**",
        f"- Answered with a price: **{len(ok)}**",
        f"  - a multi-level order book (`l2`): **{by_tier['l2']}**",
        f"  - a top of book (`l1`): **{by_tier['l1']}**",
        f"  - the last price and session figures (`last`): **{by_tier['last']}**",
        f"- Default-closed (sanctions): **{sum(1 for row in rows if row.status == 'closed')}**",
        f"- No keyless source verified: **{sum(1 for row in rows if row.status == 'no_source')}**",
        f"- A source is bound but did not answer this poll: "
        f"**{sum(1 for row in rows if row.status in ('no_answer', 'no_sample'))}**",
        "",
        "| MIC | Country | Exchange | Sample | Answered by | Tier | Levels (bid/ask) | Tape "
        "| Delay | Board rows | Note |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        delay = (
            ""
            if row.delay_s is None
            else ("real time" if row.delay_s == 0 else f"{row.delay_s // 60} min")
        )
        levels = f"{row.bid_levels}/{row.ask_levels}" if row.status == "ok" else ""
        lines.append(
            f"| {row.mic} | {row.country} | {row.name} | {row.sample} "
            f"| {row.source or row.status} | "
            f"{row.tier} | {levels} | {'yes' if row.has_tape else ''} | {delay} | "
            f"{row.board_rows or ''} | {row.note} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="live-coverage", help="output path without extension")
    parser.add_argument("--max-markets", type=int, default=None)
    args = parser.parse_args()
    rows = asyncio.run(measure(args.max_markets))
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    out = Path(args.out)
    out.with_suffix(".json").write_text(
        json.dumps({"measured": when, "rows": [asdict(row) for row in rows]}, indent=1) + "\n",
        encoding="utf-8",
    )
    out.with_suffix(".md").write_text(markdown(rows, when), encoding="utf-8")
    ok = sum(1 for row in rows if row.status == "ok")
    print(f"{ok} of {len(rows)} markets answered; wrote {out.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
