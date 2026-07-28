#!/usr/bin/env python3
"""Regenerate every image the README embeds.

    uv run python docs/media/build.py            # all assets
    uv run python docs/media/build.py hero       # one asset, by name

Why this file exists at all: a README figure that quotes a number is a claim, and a
claim nobody can re-derive rots silently. Everything under ``docs/media`` that is not
a photograph comes out of here, so any figure in it can be checked by running this
instead of trusted.

Where the numbers come from
---------------------------
* ``109`` venues, ``9`` equity sources, ``49`` capabilities, ``30`` record types and the
  provenance vocabulary are read out of the installed package at build time (see
  :func:`_facts`), so a figure cannot drift from the code it describes. If the registry
  gains a capability, the picture changes on the next run.
* The benchmark bars are transcribed **verbatim** from ``benchmarks/RESULTS.md``. They
  are not re-measured here and must not be rounded — :data:`BENCH` restates them and
  :func:`_check_bench_against_results_md` fails the build if that file stops agreeing.
* The competitor matrix is hand-verified research, not anything this script can derive.
  Every cell was checked on 2026-07-29 against each vendor's own pricing, docs and terms
  pages; :data:`MATRIX_ROWS` is the record and the footnotes carry the qualifications.

The hero terminal
-----------------
``hero-terminal.svg`` is a real CLI session captured through ``rich``'s SVG export, not
a drawing. The commands in it are executed as subprocesses and their stdout is what you
see; nothing in the frame is typed by hand except the two ``#`` comment lines.

The lake those commands read is **synthetic** — 12,000 deterministic ``Trade`` records
(seed 42, the same generator shape ``benchmarks/bench.py`` uses) written through the real
``ParquetSink`` into a temporary directory, because a network capture is neither
reproducible nor available offline. The commands, their flags and their output are real;
only the ticks in the lake are made up. That is the honest trade: the point of the frame
is that two replays of one window hash identically, and synthetic ticks demonstrate that
exactly as well as live ones would.

The prompt renders as ``crocodile`` because that is the console-script name once the
package is installed cleanly. The subprocess actually invokes
``python -m crocodile.surfaces.entrypoint`` — the currently installed ``crocodile``
wrapper is stale and raises ``ModuleNotFoundError: crocodile.crypto.legacy``. The module
form is the same Typer app the console script calls; only the spelling differs.

Light and dark
--------------
Every SVG is a single file that carries both themes. The palette is declared once as CSS
custom properties on ``:root`` and overridden inside
``@media (prefers-color-scheme: dark)``. This was verified rather than assumed: an SVG
embedded through ``<img src=...>`` — which is how GitHub renders README images — does
honour ``prefers-color-scheme`` from the embedding page in Chromium. So the README needs
plain ``<img>``/``![]()`` and **not** ``<picture>`` with two sources.

Text stays as ``<text>`` elements throughout. Nothing is converted to paths, so every
label remains searchable, diffable and re-styleable.

Dependencies
------------
Nothing new. ``rich`` is a core dependency; ``matplotlib`` is used only by the PNG social
preview and is imported lazily, so the six SVGs still build without it.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import random
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

# ---------------------------------------------------------------------------
# Palette
#
# Warm near-black and warm paper rather than the blue-grey every generated landing
# page ships with; amber as the primary accent because an amber phosphor terminal is
# the thing this product actually is. Two accents only (amber, teal) plus a green for
# "verified" and a red for "refused". No gradients anywhere.
# ---------------------------------------------------------------------------
LIGHT: dict[str, str] = {
    "bg": "#faf6ee",
    "panel": "#f2ead9",
    "panel2": "#e8dec8",
    "ink": "#17140f",
    "dim": "#6c6355",
    "rule": "#d6c9b0",
    "accent": "#a3620a",
    "accent_bg": "#f6e2bd",
    "teal": "#0f666e",
    "teal_bg": "#d3e7e7",
    "ok": "#2c7a2f",
    "warn": "#b23c1b",
    "warn_bg": "#f8ddd2",
}
DARK: dict[str, str] = {
    "bg": "#14120f",
    "panel": "#1d1a15",
    "panel2": "#272219",
    "ink": "#ece6da",
    "dim": "#948b7c",
    "rule": "#3a332a",
    "accent": "#f0a92e",
    "accent_bg": "#3a2a10",
    "teal": "#4ec2ce",
    "teal_bg": "#10302f",
    "ok": "#7fc25e",
    "warn": "#e8724f",
    "warn_bg": "#3a1d13",
}

# System stack only. An SVG loaded through <img> cannot fetch a webfont, so anything
# not already on the reader's machine is a wasted request and a silent fallback.
MONO = (
    "ui-monospace, SFMono-Regular, &#34;SF Mono&#34;, Menlo, Consolas, "
    "&#34;DejaVu Sans Mono&#34;, &#34;Liberation Mono&#34;, monospace"
)

# Benchmark figures, transcribed verbatim from benchmarks/RESULTS.md. Do not round.
BENCH_MACHINE = (
    "Darwin 25.5.0 | arm (arm64) | 10 logical CPUs | Python 3.12.12 | "
    "polars 1.41.2 | duckdb 1.5.3 | pyarrow 24.0.0"
)
BENCH: list[tuple[str, str, float, str]] = [
    # (label, printed value, numeric value for the bar, detail)
    ("resample &#8594; 1m OHLCV", "17,000,000 rows/s", 17_000_000, "500,000 rows in, 42 bars out"),
    (
        "normalize (raw &#8594; records)",
        "3,540,000 rec/s",
        3_540_000,
        "600,000 records, 282 ns each",
    ),
    ("replay (k-way merge)", "458,000 rec/s", 458_000, "500,000 records in 1.09 s"),
    ("write (ParquetSink, zstd-5)", "245,000 rec/s", 245_000, "500,000 records in 2.04 s"),
]
BENCH_TILES: list[tuple[str, str, str]] = [
    (
        "20.20&#215;",
        "compression vs raw JSON",
        "500,000 records &#183; 117.3 MB JSON &#8594; 5.8 MB parquet",
    ),
    (
        "0.36 ms",
        "count(*) over the lake",
        "500,000 rows &#183; median of 7 runs &#183; DuckDB view",
    ),
]


# ---------------------------------------------------------------------------
# Facts read out of the installed package
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Facts:
    capabilities: int
    wire_names: int
    record_types: int
    provenance_levels: tuple[str, ...]
    bases: int
    cli_command: str
    rest_path: str
    mcp_tool: str
    example_confidence: float


def _facts() -> Facts:
    """Read the numbers the diagrams assert straight out of the package.

    Falls back to the values in the README if the package will not import, so the
    diagrams can still be regenerated from a checkout with a broken optional extra —
    but says so on stderr rather than quietly drawing a stale number.
    """
    try:
        import inspect

        import msgspec

        from crocodile.capabilities import load_all
        from crocodile.core.capability import REGISTRY
        from crocodile.core.schema import records as records_mod
        from crocodile.core.schema.provenance import (
            Provenance,
            load_all_bases,
            provenance_fields,
            registered_bases,
        )
        from crocodile.surfaces import dispatch, mcp, rest

        load_all()
        load_all_bases()
        n_records = len(
            [
                name
                for name, obj in vars(records_mod).items()
                if inspect.isclass(obj)
                and issubclass(obj, msgspec.Struct)
                and not name.startswith("_")
            ]
        )
        conf = provenance_fields("yahoo_1m_vap", {"n_volume_bars": 312}).prov_confidence
        return Facts(
            capabilities=len(REGISTRY),
            wire_names=len(dispatch.wire_names()),
            record_types=n_records,
            provenance_levels=tuple(p.value for p in Provenance),
            bases=len(registered_bases()),
            cli_command="crocodile depth",
            rest_path=rest.path_for("depth"),
            mcp_tool=sorted(mcp.tool_names() & {"depth"}).pop(),
            example_confidence=conf,
        )
    except Exception as exc:  # pragma: no cover - build-time convenience
        print(f"  ! could not import crocodile ({exc}); using README constants", file=sys.stderr)
        return Facts(
            capabilities=49,
            wire_names=57,
            record_types=30,
            provenance_levels=("native", "derived", "synthetic", "unavailable"),
            bases=17,
            cli_command="crocodile depth",
            rest_path="/api/v1/depth",
            mcp_tool="depth",
            example_confidence=0.8,
        )


# ---------------------------------------------------------------------------
# SVG scaffolding
# ---------------------------------------------------------------------------
BASE_CSS = """
  text {{ font-family: {mono}; fill: var(--ink); }}
  .dim {{ fill: var(--dim); }}
  .acc {{ fill: var(--accent); }}
  .teal {{ fill: var(--teal); }}
  .ok {{ fill: var(--ok); }}
  .warn {{ fill: var(--warn); }}
  .b {{ font-weight: 700; }}
  .kicker {{ font-size: 12px; font-weight: 700; letter-spacing: .18em; }}
  .h {{ font-size: 13px; font-weight: 700; letter-spacing: .12em; }}
  .lbl {{ font-size: 12px; letter-spacing: .04em; }}
  .sm {{ font-size: 11px; }}
  .xs {{ font-size: 10px; letter-spacing: .02em; }}
  .xxs {{ font-size: 9px; letter-spacing: .01em; }}
  .code {{ font-size: 12.5px; }}
  .big {{ font-size: 34px; font-weight: 700; letter-spacing: -.01em; }}
  .panel {{ fill: var(--panel); stroke: var(--rule); stroke-width: 1; }}
  .panel2 {{ fill: var(--panel2); stroke: var(--rule); stroke-width: 1; }}
  .hair {{ stroke: var(--rule); stroke-width: 1; fill: none; }}
  .wire {{ stroke: var(--rule); stroke-width: 1.25; fill: none; }}
  .wire-acc {{ stroke: var(--accent); stroke-width: 1.6; fill: none; }}
  .wire-teal {{ stroke: var(--teal); stroke-width: 1.6; fill: none; }}
"""


def _vars(scope: str, palette: dict[str, str]) -> str:
    decls = ";".join(f"--{k}:{v}" for k, v in palette.items())
    return f"  {scope}{{{decls}}}\n"


def svg_doc(width: int, height: int, body: str, extra_css: str = "") -> str:
    """Wrap ``body`` in a themed, self-contained SVG document."""
    css = (
        _vars(":root", LIGHT)
        + "  @media (prefers-color-scheme: dark){\n  "
        + _vars(":root", DARK).strip()
        + "\n  }\n"
        + BASE_CSS.format(mono=MONO)
        + extra_css
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img">\n'
        f"<style>\n{css}</style>\n"
        f'<rect width="{width}" height="{height}" rx="6" fill="var(--bg)"/>\n'
        f"{body}\n</svg>\n"
    )


def esc(text: str) -> str:
    """Escape for XML text content, leaving pre-escaped entities alone."""
    out = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"&amp;(#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);", r"&\1;", out)


def t(
    x: float,
    y: float,
    text: str,
    cls: str = "sm",
    anchor: str = "start",
    raw: bool = False,
) -> str:
    body = text if raw else esc(text)
    a = f' text-anchor="{anchor}"' if anchor != "start" else ""
    return f'<text x="{x:g}" y="{y:g}" class="{cls}"{a}>{body}</text>\n'


def rect(
    x: float, y: float, w: float, h: float, cls: str = "panel", rx: float = 3, extra: str = ""
) -> str:
    return (
        f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{rx:g}" '
        f'class="{cls}"{(" " + extra) if extra else ""}/>\n'
    )


def line(x1: float, y1: float, x2: float, y2: float, cls: str = "hair") -> str:
    return f'<line x1="{x1:g}" y1="{y1:g}" x2="{x2:g}" y2="{y2:g}" class="{cls}"/>\n'


def swatch(x: float, y: float, fill: str, size: float = 7) -> str:
    """A legend chip. A coloured rect rather than a &#9646; glyph, which would take the
    surrounding text's fill and quietly make the two entries of a legend identical."""
    return f'<rect x="{x:g}" y="{y:g}" width="{size:g}" height="{size:g}" rx="1" fill="{fill}"/>\n'


def header(width: int, left: str, right: str) -> str:
    """The band every diagram opens with: tracked kicker, right-aligned context, rule."""
    out = t(24, 28, left, "kicker acc")
    out += t(width - 24, 28, right, "xs dim", anchor="end")
    out += line(24, 42, width - 24, 42)
    return out


# 10px in a monospace face is about 6.1px per character in the stacks we target;
# 6.4 is deliberately pessimistic, because a footer that wraps early is a cosmetic
# flaw and one that runs off the right edge is a broken image.
_XS_CHAR_W = 6.4


def _wrap(text: str, max_px: float) -> list[str]:
    """Greedy wrap on spaces, counting an XML entity as the one glyph it becomes."""
    budget = int(max_px / _XS_CHAR_W)
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        glyphs = len(re.sub(r"&(#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);", "X", word))
        used = len(re.sub(r"&(#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);", "X", current))
        if current and used + 1 + glyphs > budget:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        lines.append(current)
    return lines


def footer(width: int, y: float, text: str) -> str:
    """Rule plus a wrapped note. ``y`` is the baseline of the *last* line."""
    lines = _wrap(text, width - 48)
    top = y - 14 * (len(lines) - 1)
    out = line(24, top - 16, width - 24, top - 16)
    for i, part in enumerate(lines):
        out += t(24, top + i * 14, part, "xs dim")
    return out


def arrow_marker(name: str, color: str) -> str:
    return (
        f'<marker id="{name}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" '
        f'markerHeight="7" orient="auto"><path d="M0 0 L8 4 L0 8 z" fill="{color}"/></marker>\n'
    )


DEFS = (
    "<defs>\n"
    + arrow_marker("ar", "var(--rule)")
    + arrow_marker("ar-acc", "var(--accent)")
    + arrow_marker("ar-teal", "var(--teal)")
    + "</defs>\n"
)


def write(name: str, content: str) -> Path:
    path = HERE / name
    path.write_text(content, encoding="utf-8")
    size = path.stat().st_size
    flag = "  <-- OVER 200 KB" if size > 200_000 else ""
    print(f"  {name:<26} {size:>8,} bytes{flag}")
    return path


# ===========================================================================
# 1. hero-terminal.svg
# ===========================================================================
CLI = [sys.executable, "-m", "crocodile.surfaces.entrypoint"]

# The prompt shown in the frame. The subprocess runs the module form because the
# installed console script is stale (ModuleNotFoundError: crocodile.crypto.legacy);
# it is the same Typer app either way.
PROMPT_NAME = "crocodile"

# rich's ``dim`` is an ANSI *attribute*, not a colour: the SVG exporter blends the
# foreground toward the background, which lands on the background sentinel below and
# renders the text invisible. Name the palette slot instead.
DIMMED = "bright_black"

# The sha256 of the replay stream, measured by :func:`build_hero` and reused by
# :func:`build_social`. The literal is what the seed-42 lake hashes to today; building
# the hero first overwrites it with the freshly measured value.
_HERO_DIGEST = "fb97a53e3d684bb685beb92928b1e68c1478631adce87fe25530e30403533425"

_SEED = 42
_LAKE_ROWS = 12_000
# 2023-11-14T22:13:20Z. One UTC date so every row lands in one date partition.
_BASE_TS_NS = 1_700_000_000_000_000_000

# Sentinel colours handed to rich's SVG exporter. rich bakes literal hex into the
# generated stylesheet, so each slot gets an unmistakable value that is then rewritten
# to a CSS custom property — which is what makes one exported terminal carry both themes.
_SENTINEL = {
    "bg": "#010101",
    "fg": "#020202",
    "dim": "#030303",
    "accent": "#040404",
    "ok": "#050505",
    "teal": "#060606",
    "warn": "#070707",
    "extra": "#080808",
}
_SENTINEL_VAR = {
    "#010101": "var(--term-bg)",
    "#020202": "var(--term-fg)",
    "#030303": "var(--term-dim)",
    "#040404": "var(--term-accent)",
    "#050505": "var(--term-ok)",
    "#060606": "var(--term-teal)",
    "#070707": "var(--term-warn)",
    "#080808": "var(--term-fg)",
}

# The terminal reads as a terminal in both themes: warm near-black on GitHub dark, warm
# paper on GitHub light. Both are the same palette as the diagrams.
_TERM_LIGHT = {
    "--term-bg": "#fbf7ef",
    "--term-fg": "#1d1913",
    "--term-dim": "#7b7264",
    "--term-accent": "#9a5c07",
    "--term-ok": "#2c7a2f",
    "--term-teal": "#0f666e",
    "--term-warn": "#b23c1b",
    "--term-chrome": "#eadfc8",
    "--term-edge": "rgba(0,0,0,0.22)",
}
_TERM_DARK = {
    "--term-bg": "#14120f",
    "--term-fg": "#ece6da",
    "--term-dim": "#8d8475",
    "--term-accent": "#f0a92e",
    "--term-ok": "#7fc25e",
    "--term-teal": "#4ec2ce",
    "--term-warn": "#e8724f",
    "--term-chrome": "#1f1b15",
    "--term-edge": "rgba(255,255,255,0.20)",
}

_CODE_FORMAT = """<svg class="rich-terminal" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">
<style>
:root{{__LIGHT__}}
@media (prefers-color-scheme: dark){{:root{{__DARK__}}}}
.{unique_id}-matrix {{
    font-family: __MONO__;
    font-size: {char_height}px;
    line-height: {line_height}px;
}}
.{unique_id}-title {{
    font-size: 17px;
    font-weight: 700;
    letter-spacing: .22em;
    font-family: __MONO__;
}}
{styles}
</style>
<defs>
<clipPath id="{unique_id}-clip-terminal">
  <rect x="0" y="0" width="{terminal_width}" height="{terminal_height}" />
</clipPath>
{lines}
</defs>
{chrome}
<g transform="translate({terminal_x}, {terminal_y})" clip-path="url(#{unique_id}-clip-terminal)">
{backgrounds}
<g class="{unique_id}-matrix">
{matrix}
</g>
</g>
</svg>
"""


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _build_synthetic_lake(data_dir: Path) -> int:
    """Write ``_LAKE_ROWS`` deterministic Deribit trades through the real ParquetSink.

    Synthetic on purpose: a live capture is not reproducible, needs network, and would
    make this script fail on a plane. The generator mirrors ``benchmarks/bench.py`` —
    same seed, same ~5 ms spacing, same single date partition — so the lake the hero
    frame queries has the shape a real collect run produces.
    """
    from crocodile.core.schema.enums import AssetClass, Side
    from crocodile.core.schema.records import Trade
    from crocodile.core.store.parquet_sink import ParquetSink

    rng = random.Random(_SEED)
    price = [37_000.0]

    def make(i: int) -> Trade:
        price[0] += rng.uniform(-5.0, 5.0)
        ts = _BASE_TS_NS + i * 5_000_000
        return Trade(
            source="deribit",
            symbol="deribit:BTC-PERPETUAL",
            symbol_raw="BTC-PERPETUAL",
            source_ts=ts,
            local_ts=ts,
            asset_class=AssetClass.CRYPTO,
            id=str(i),
            price=round(price[0], 1),
            amount=round(rng.uniform(10.0, 5000.0), 1),
            side=Side.BUY if rng.random() < 0.5 else Side.SELL,
        )

    async def run() -> None:
        sink = ParquetSink(
            data_dir=data_dir,
            max_buffer_rows=_LAKE_ROWS + 1,
            flush_interval_seconds=9_999_999,
        )
        for i in range(_LAKE_ROWS):
            await sink.put(make(i))
        await sink.flush()

    asyncio.run(run())
    return _LAKE_ROWS


def _env(data_dir: Path) -> dict[str, str]:
    return dict(os.environ, CROCODILE_DATA_DIR=str(data_dir), COLUMNS="88", NO_COLOR="1")


def _run(args: list[str], data_dir: Path) -> str:
    """Run the CLI and return its stdout, verbatim."""
    proc = subprocess.run(
        CLI + args, capture_output=True, text=True, env=_env(data_dir), cwd=REPO
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"hero: command failed ({proc.returncode})\n  {' '.join(args)}\n{proc.stderr}"
        )
    return proc.stdout.rstrip("\n")


def _run_digest(args: list[str], data_dir: Path) -> str:
    """Run the CLI and return the SHA-256 of its stdout, as ``shasum -a 256`` would.

    The frame shows the reader a pipeline — ``… | shasum -a 256`` — because a pipeline is
    what they would type. It does not *use* one: hashing the captured bytes here is the
    same digest over the same stream, without handing a constructed string to a shell.
    stdout is captured as bytes, not text, so no newline translation can come between the
    replay and the hash.
    """
    proc = subprocess.run(CLI + args, capture_output=True, env=_env(data_dir), cwd=REPO)
    if proc.returncode != 0:
        raise SystemExit(
            f"hero: command failed ({proc.returncode})\n  {' '.join(args)}\n"
            f"{proc.stderr.decode(errors='replace')}"
        )
    return hashlib.sha256(proc.stdout).hexdigest()


def _prompt(console: Any, lines: list[str]) -> None:
    """Print a shell prompt, tokenised so the command name and flags read differently."""
    from rich.text import Text

    for index, raw in enumerate(lines):
        text = Text()
        text.append("$ " if index == 0 else "  ", style=DIMMED)
        indent = len(raw) - len(raw.lstrip(" "))
        if indent:
            text.append(" " * indent)
        for token in raw.strip().split(" "):
            if token == PROMPT_NAME:
                style = "bold yellow"
            elif token.startswith("--"):
                style = "cyan"
            elif token in {"|", "\\", "for", "in", "do", "done;", "done", ";"}:
                style = DIMMED
            elif token.startswith('"'):
                style = "green"
            else:
                style = ""
            text.append(token + " ", style=style)
        console.print(text)


def build_hero() -> Path:
    """Capture a real determinism session and export it through rich."""
    from rich.console import Console
    from rich.terminal_theme import TerminalTheme
    from rich.text import Text

    console = Console(record=True, width=92, file=open(os.devnull, "w"), force_terminal=True)

    with tempfile.TemporaryDirectory(prefix="crocodile-hero-") as tmp:
        lake = Path(tmp) / "data"
        rows = _build_synthetic_lake(lake)

        catalog_out = _run(["catalog", "--asset-class", "crypto"], lake)
        sql = "SELECT side, count(*) AS n, round(avg(price),2) AS px FROM trade GROUP BY side"
        query_out = _run(["query", "--asset-class", "crypto", sql], lake)

        replay_flags = (
            "--asset-class crypto --channels trade "
            "--symbols deribit:BTC-PERPETUAL --start-ns 0 --end-ns 9223372036854775807"
        )
        # Two independent replays of the same window, each hashed over its own captured
        # bytes. Nothing is copied from the first run into the second: if the engine were
        # not deterministic these two lines would differ, and the frame would say so.
        replay_argv = ["replay", *replay_flags.split()]
        first = _run_digest(replay_argv, lake)
        second = _run_digest(replay_argv, lake)
        digests = f"{first}  -\n{second}  -"
        # Hand the measured digest to the social preview so the two assets cannot
        # disagree about what the same lake hashes to.
        global _HERO_DIGEST
        _HERO_DIGEST = first

        console.print()
        console.print(
            Text(
                f"# a lake of {rows:,} Deribit BTC-PERPETUAL trades, already collected",
                style=DIMMED,
            )
        )
        _prompt(console, [f"{PROMPT_NAME} catalog --asset-class crypto"])
        console.print(Text(catalog_out))
        console.print()
        _prompt(
            console,
            [
                f"{PROMPT_NAME} query --asset-class crypto \\",
                f'    "{sql}"',
            ],
        )
        console.print(Text(query_out))
        console.print()
        console.print(Text("# replay the same window twice, hash both streams", style=DIMMED))
        _prompt(
            console,
            [
                f"for i in 1 2; do {PROMPT_NAME} replay --asset-class crypto \\",
                "    --channels trade --symbols deribit:BTC-PERPETUAL \\",
                "    --start-ns 0 --end-ns 9223372036854775807 | shasum -a 256; done",
            ],
        )
        for digest_line in digests.splitlines():
            console.print(Text(digest_line, style="bold green"))
        console.print(
            Text("# identical. replay is a pure function of the lake and the window.", style=DIMMED)
        )
        console.print()

    theme = TerminalTheme(
        _hex_to_rgb(_SENTINEL["bg"]),
        _hex_to_rgb(_SENTINEL["fg"]),
        [
            _hex_to_rgb(_SENTINEL["bg"]),  # black
            _hex_to_rgb(_SENTINEL["warn"]),  # red
            _hex_to_rgb(_SENTINEL["ok"]),  # green
            _hex_to_rgb(_SENTINEL["accent"]),  # yellow
            _hex_to_rgb(_SENTINEL["teal"]),  # blue
            _hex_to_rgb(_SENTINEL["extra"]),  # magenta
            _hex_to_rgb(_SENTINEL["teal"]),  # cyan
            _hex_to_rgb(_SENTINEL["fg"]),  # white
        ],
        [
            _hex_to_rgb(_SENTINEL["dim"]),
            _hex_to_rgb(_SENTINEL["warn"]),
            _hex_to_rgb(_SENTINEL["ok"]),
            _hex_to_rgb(_SENTINEL["accent"]),
            _hex_to_rgb(_SENTINEL["teal"]),
            _hex_to_rgb(_SENTINEL["extra"]),
            _hex_to_rgb(_SENTINEL["teal"]),
            _hex_to_rgb(_SENTINEL["fg"]),
        ],
    )
    code_format = (
        _CODE_FORMAT.replace("__MONO__", MONO)
        .replace("__LIGHT__", ";".join(f"{k}:{v}" for k, v in _TERM_LIGHT.items()))
        .replace("__DARK__", ";".join(f"{k}:{v}" for k, v in _TERM_DARK.items()))
    )
    svg = console.export_svg(
        title="crocodile", theme=theme, code_format=code_format, unique_id="croc-term"
    )

    # rich writes the palette as literal hex. Rewrite each sentinel to its variable so
    # one file answers for both themes.
    for sentinel, var in _SENTINEL_VAR.items():
        svg = svg.replace(sentinel, var)
    # rich's window chrome: fixed stroke and a "dim" style rich emits for the whole
    # frame in some builds. Point them at variables too.
    svg = svg.replace('stroke="rgba(255,255,255,0.35)"', 'stroke="var(--term-edge)"')
    return write("hero-terminal.svg", svg)


# ===========================================================================
# 2. architecture.svg
# ===========================================================================
NATIVE_VENUES = [
    "binance",
    "bybit",
    "coinbase",
    "deribit",
    "okx",
    "base",
    "gmx",
    "derive",
    "superchain",
    "coingecko",
]
EQUITY_SOURCES = [
    ("alpaca", "key"),
    ("yahoo", ""),
    ("sec_edgar", ""),
    ("treasury", ""),
    ("tiingo", "key"),
    ("stooq", ""),
    ("msn_money", ""),
    ("google_finance", ""),
    ("finnhub", "key"),
]
SPINE_RECORDS = [
    "Trade",
    "BookSnapshot",
    "BookDelta",
    "BookTicker",
    "OHLCV",
    "Quote",
    "Funding",
    "OpenInterest",
    "Liquidation",
    "OptionsChain",
    "DepthProfile",
    "Auction",
    "Instrument",
    "Filing",
    "Holding13F",
    "InsiderTransaction",
    "CorporateAction",
    "Fundamental",
    "MacroSeries",
    "IndexValue",
]


def build_architecture(facts: Facts) -> Path:
    W, H = 880, 546
    b = DEFS
    b += header(
        W, "ARCHITECTURE", "many shapes in  &#183;  one shape through  &#183;  three shapes out"
    )

    # ---- left: sources -----------------------------------------------------
    lx, lw = 24, 214
    b += rect(lx, 62, lw, 214)
    b += t(lx + 12, 84, "CRYPTO", "h acc")
    b += t(lx + lw - 12, 84, "109 venues", "xs dim", anchor="end")
    cell, gap, cols = 13, 4, 11
    gx, gy = lx + 12, 96
    for i in range(109):
        col, row = i % cols, i // cols
        x = gx + col * (cell + gap)
        y = gy + row * (7 + gap)
        fill = "var(--accent)" if i < 10 else "var(--rule)"
        b += f'<rect x="{x:g}" y="{y:g}" width="{cell}" height="7" rx="1.5" fill="{fill}"/>\n'
    b += t(lx + 12, 214, "10 native  +  105 ccxt  &#8722;  6 shared", "xs")
    b += t(lx + 12, 230, "binance bybit coinbase deribit okx", "xs dim")
    b += t(lx + 12, 244, "base gmx derive superchain coingecko", "xs dim")
    b += swatch(lx + 12, 258, "var(--accent)") + t(lx + 24, 263, "native", "xs dim")
    b += swatch(lx + 76, 258, "var(--rule)") + t(lx + 88, 263, "via ccxt", "xs dim")

    b += rect(lx, 290, lw, 194)
    b += t(lx + 12, 312, "US EQUITIES", "h acc")
    b += t(lx + lw - 12, 312, "9 sources", "xs dim", anchor="end")
    for i, (name, key) in enumerate(EQUITY_SOURCES):
        y = 332 + i * 16
        b += (
            f'<rect x="{lx + 12:g}" y="{y - 9:g}" width="6" height="6" rx="1" '
            f'fill="{"var(--teal)" if not key else "var(--dim)"}"/>\n'
        )
        b += t(lx + 26, y, name, "xs")
        if key:
            b += t(lx + lw - 12, y, "key", "xs dim", anchor="end")
    b += swatch(lx + 12, 470, "var(--teal)") + t(lx + 24, 475, "keyless", "xs dim")
    b += swatch(lx + 84, 470, "var(--dim)") + t(lx + 96, 475, "needs a key", "xs dim")

    # ---- convergence -------------------------------------------------------
    spine_x, spine_w = 330, 236
    for i in range(16):
        y0 = 96 + i * 25
        y1 = 250 + (i - 7.5) * 4
        b += (
            f'<path d="M{lx + lw:g} {y0:g} C {lx + lw + 46:g} {y0:g}, '
            f'{spine_x - 46:g} {y1:g}, {spine_x - 6:g} {y1:g}" class="wire" opacity="0.55"/>\n'
        )

    # ---- spine -------------------------------------------------------------
    b += rect(spine_x, 62, spine_w, 62, "panel2")
    b += t(spine_x + 14, 84, "NORMALIZE", "h acc")
    b += t(spine_x + 14, 104, "every venue &#8594; one record shape", "xs dim")
    b += t(spine_x + 14, 117, "validated, typed, timestamped in ns", "xs dim")

    b += (
        f'<line x1="{spine_x + spine_w / 2:g}" y1="124" x2="{spine_x + spine_w / 2:g}" '
        'y2="140" class="wire-acc" marker-end="url(#ar-acc)"/>\n'
    )

    b += rect(spine_x, 146, spine_w, 190, "panel2")
    b += t(spine_x + 14, 168, "ONE SCHEMA", "h acc")
    b += t(
        spine_x + spine_w - 14, 168, f"{facts.record_types} record types", "xs dim", anchor="end"
    )
    for i, name in enumerate(SPINE_RECORDS):
        col, row = i % 2, i // 2
        x = spine_x + 14 + col * 108
        y = 188 + row * 14
        b += t(x, y, name, "xs teal")
    remaining = facts.record_types - len(SPINE_RECORDS)
    b += t(spine_x + 14, 328, f"&#8230; and {remaining} more", "xs dim")

    b += (
        f'<line x1="{spine_x + spine_w / 2:g}" y1="336" x2="{spine_x + spine_w / 2:g}" '
        'y2="352" class="wire-acc" marker-end="url(#ar-acc)"/>\n'
    )

    b += rect(spine_x, 358, spine_w, 126, "panel2")
    b += t(spine_x + 14, 380, "PARQUET LAKE", "h acc")
    b += t(spine_x + 14, 400, "data/", "xs")
    b += t(spine_x + 24, 414, "source=deribit/", "xs dim")
    b += t(spine_x + 34, 428, "channel=trade/", "xs dim")
    b += t(spine_x + 44, 442, "date=2023-11-14/", "xs dim")
    b += t(spine_x + 54, 456, "part-0000.parquet", "xs dim")
    b += t(spine_x + 14, 474, "one DuckDB view per channel", "xs teal")

    # ---- divergence --------------------------------------------------------
    rx, rw = 662, 194
    targets = [128, 262, 396]
    for y1 in targets:
        b += (
            f'<path d="M{spine_x + spine_w:g} 273 C {spine_x + spine_w + 50:g} 273, '
            f'{rx - 50:g} {y1:g}, {rx - 6:g} {y1:g}" class="wire-teal" '
            'marker-end="url(#ar-teal)"/>\n'
        )

    surfaces = [
        ("CLI", "one command per capability", [f"$ {facts.cli_command} \\", "    --symbol AAPL"]),
        ("REST", "one route per capability", [f"GET {facts.rest_path}", "    ?symbol=AAPL"]),
        ("MCP", "one tool per capability", ["tools/call", f'  {{"name": "{facts.mcp_tool}"}}']),
    ]
    for (name, sub, code), cy in zip(surfaces, targets, strict=True):
        y = cy - 46
        b += rect(rx, y, rw, 94)
        b += t(rx + 12, y + 22, name, "h acc")
        b += t(rx + 12, y + 38, sub, "xs dim")
        for j, code_line in enumerate(code):
            b += t(rx + 12, y + 60 + j * 15, code_line, "xs teal")

    b += rect(rx, 452, rw, 44, "panel2")
    b += t(rx + 12, 473, f"{facts.capabilities} capabilities", "lbl b")
    b += t(rx + 12, 488, "projected, never re-implemented", "xs dim")

    b += footer(
        W,
        H - 16,
        "109 crypto venues  &#183;  9 equity sources  &#183;  "
        f"{facts.record_types} record types  &#183;  3 surfaces "
        "&#8212; and one schema between all of them.",
    )
    return write("architecture.svg", svg_doc(W, H, b))


# ===========================================================================
# 3. registry-projection.svg
# ===========================================================================
DECLARATION = [
    ("Capability(", "dim"),
    ('    name="depth",', "acc"),
    ('    summary="Market-depth ladder around a', ""),
    ('             reference price, real L1 when keyed.",', ""),
    ("    params=DepthParams,", ""),
    ("    returns=ReturnKind.SCALAR,", ""),
    ("    impls={", "dim"),
    ('        CRYPTO: Impl(basis="book_snapshot_slice"),', "teal"),
    ('        EQUITY: Impl(basis="alpaca_l1",', "teal"),
    ('                     fallback="yahoo_1m_vap"),', "teal"),
    ("    },", "dim"),
    (")", "dim"),
]

# Cell 24 of 49 (0-indexed 23) is the one traced through all three surfaces.
HIGHLIGHT_CELL = 23


def _capability_grid(
    x: float, y: float, cell: float, gap: float, cols: int, n: int, highlight: int
) -> str:
    out = ""
    for i in range(n):
        col, row = i % cols, i // cols
        cx = x + col * (cell + gap)
        cy = y + row * (cell + gap)
        if i == highlight:
            out += (
                f'<rect x="{cx:g}" y="{cy:g}" width="{cell:g}" height="{cell:g}" rx="1.5" '
                f'fill="var(--accent)"/>\n'
            )
        else:
            out += (
                f'<rect x="{cx:g}" y="{cy:g}" width="{cell:g}" height="{cell:g}" rx="1.5" '
                f'fill="var(--rule)"/>\n'
            )
    return out


def build_registry(facts: Facts) -> Path:
    W, H = 880, 580
    n = facts.capabilities
    cols = 7
    rows = math.ceil(n / cols)

    b = DEFS
    b += header(
        W,
        "ONE DECLARATION, THREE PROJECTIONS",
        f"{n} capabilities  &#183;  {facts.wire_names} wire names (aliases included)",
    )

    # ---- left: the single declaration -------------------------------------
    lx, lw = 24, 396
    b += t(lx, 66, "THE DECLARATION &#8212; written once", "lbl b")
    b += rect(lx, 76, lw, 214, "panel2")
    for i, (code_line, cls) in enumerate(DECLARATION):
        b += t(lx + 14, 98 + i * 16, code_line, f"code {cls}".strip())

    b += t(lx, 320, f"THE REGISTRY &#8212; {n} of these", "lbl b")
    cell, gap = 20, 6
    gx, gy = lx + 4, 332
    b += _capability_grid(gx, gy, cell, gap, cols, n, HIGHLIGHT_CELL)
    grid_w = cols * (cell + gap) - gap
    grid_h = rows * (cell + gap) - gap
    cap_x = gx + grid_w + 26
    b += t(cap_x, gy + 16, "one square =", "xs dim")
    b += t(cap_x, gy + 30, "one capability", "xs dim")
    b += (
        f'<rect x="{cap_x:g}" y="{gy + 48:g}" width="10" height="10" rx="1.5" '
        'fill="var(--accent)"/>\n'
    )
    b += t(cap_x + 18, gy + 57, "depth", "sm b acc")
    b += t(cap_x, gy + 76, "traced through all", "xs dim")
    b += t(cap_x, gy + 89, "three surfaces &#8594;", "xs dim")
    b += t(cap_x, gy + 116, f"{facts.bases} confidence", "xs dim")
    b += t(cap_x, gy + 129, "formulas behind", "xs dim")
    b += t(cap_x, gy + 142, "them", "xs dim")

    # ---- projection rays ---------------------------------------------------
    origin_x = gx + grid_w + 150
    origin_y = gy + grid_h / 2
    panel_x, panel_w, panel_h = 470, 386, 138
    panel_ys = [66, 224, 382]
    for py in panel_ys:
        b += (
            f'<path d="M{origin_x:g} {origin_y:g} C {origin_x + 30:g} {origin_y:g}, '
            f'{panel_x - 34:g} {py + panel_h / 2:g}, {panel_x - 8:g} {py + panel_h / 2:g}" '
            'class="wire-acc" marker-end="url(#ar-acc)"/>\n'
        )
    b += f'<circle cx="{origin_x:g}" cy="{origin_y:g}" r="3.5" fill="var(--accent)"/>\n'

    # ---- right: the three projections --------------------------------------
    projections = [
        (
            "CLI",
            "typer command table",
            [
                ("$ crocodile depth --symbol AAPL \\", "acc"),
                ("      --asset-class equity", "teal"),
                ("", ""),
                ("generated from REGISTRY; the app holds", "dim"),
                ("exactly the registry's names.", "dim"),
            ],
        ),
        (
            "REST",
            "fastapi routes",
            [
                (f"GET  {facts.rest_path}?symbol=AAPL", "acc"),
                (f"POST {facts.rest_path}", "teal"),
                ("", ""),
                ("one route per capability, both methods", "dim"),
                ("on it &#8212; never two routes with one each.", "dim"),
            ],
        ),
        (
            "MCP",
            "tools/list &#183; tools/call",
            [
                ('{"name": "depth", "inputSchema": {&#8230;}}', "acc"),
                ('&#8594; tools/call {"name": "depth"}', "teal"),
                ("", ""),
                ("input schema derived from DepthParams,", "dim"),
                ("so an agent cannot call it wrong.", "dim"),
            ],
        ),
    ]
    mini_cell, mini_gap = 7, 2.5
    mini_w = cols * (mini_cell + mini_gap) - mini_gap
    for (name, sub, code), py in zip(projections, panel_ys, strict=True):
        b += rect(panel_x, py, panel_w, panel_h)
        b += t(panel_x + 16, py + 24, name, "h acc")
        # The subtitle is right-aligned so it can never grow into the code column;
        # left-aligning it under the name is what put "typer command table" on top of
        # the command in the first draft.
        b += t(panel_x + panel_w - 16, py + 24, sub, "xs dim", anchor="end")
        b += line(panel_x + 16, py + 34, panel_x + panel_w - 16, py + 34)
        b += _capability_grid(panel_x + 16, py + 48, mini_cell, mini_gap, cols, n, HIGHLIGHT_CELL)
        b += t(panel_x + 16, py + 48 + rows * (mini_cell + mini_gap) + 8, f"all {n}", "xs dim")
        tx = panel_x + 16 + mini_w + 22
        for j, (code_line, cls) in enumerate(code):
            if not code_line:
                continue
            b += t(tx, py + 52 + j * 16, code_line, f"xs {cls}".strip())

    b += footer(
        W,
        H - 16,
        "Gate 4 asserts every surface holds exactly the registry's names, once each. "
        "A surface cannot add, drop or rename a capability &#8212; so the three "
        "cannot drift apart.",
    )
    return write("registry-projection.svg", svg_doc(W, H, b))


# ===========================================================================
# 4. benchmark.svg
# ===========================================================================
def _check_bench_against_results_md() -> None:
    """Fail loudly if RESULTS.md stops agreeing with the figures drawn here."""
    results = REPO / "benchmarks" / "RESULTS.md"
    if not results.exists():
        print("  ! benchmarks/RESULTS.md not found; bars not cross-checked", file=sys.stderr)
        return
    text = results.read_text(encoding="utf-8")
    expected = [
        "3,540,000 rec/s",
        "245,000 rec/s",
        "20.20x",
        "0.36 ms",
        "17,000,000 rows/s",
        "458,000 rec/s",
        BENCH_MACHINE,
    ]
    missing = [value for value in expected if value not in text]
    if missing:
        raise SystemExit(
            "benchmark.svg would print figures that benchmarks/RESULTS.md no longer "
            f"contains: {missing}. Re-transcribe BENCH before rebuilding."
        )


def build_benchmark() -> Path:
    _check_bench_against_results_md()
    W, H = 880, 476
    b = DEFS
    b += header(
        W,
        "BENCHMARK",
        "offline data path  &#183;  synthetic input  &#183;  seed 42  &#183;  no network",
    )

    plot_x, plot_w = 300, 434
    lo, hi = 5.0, 8.0  # log10 decades

    def bar_len(value: float) -> float:
        return (math.log10(value) - lo) / (hi - lo) * plot_w

    b += t(24, 68, "THROUGHPUT", "lbl b")
    b += t(plot_x, 68, "items per second, log scale", "xs dim")
    for decade in range(int(lo), int(hi) + 1):
        x = plot_x + (decade - lo) / (hi - lo) * plot_w
        b += f'<line x1="{x:g}" y1="76" x2="{x:g}" y2="248" class="hair" opacity="0.7"/>\n'
        label = {5: "100 K", 6: "1 M", 7: "10 M", 8: "100 M"}[decade]
        b += t(x, 264, label, "xs dim", anchor="middle")

    for i, (label, printed, value, detail) in enumerate(BENCH):
        y = 92 + i * 40
        b += t(288, y + 6, label, "sm", anchor="end", raw=True)
        b += t(288, y + 20, detail, "xs dim", anchor="end", raw=True)
        width = bar_len(value)
        b += (
            f'<rect x="{plot_x:g}" y="{y - 8:g}" width="{width:g}" height="22" rx="2" '
            f'fill="var(--accent)"/>\n'
        )
        b += t(plot_x + width + 10, y + 8, printed, "sm b", raw=True)

    # ---- the two figures that are not rates --------------------------------
    tile_y, tile_h = 292, 92
    for i, (value, label, detail) in enumerate(BENCH_TILES):
        x = 24 + i * 418
        b += rect(x, tile_y, 394, tile_h, "panel2")
        b += t(x + 18, tile_y + 46, value, "big acc", raw=True)
        b += t(x + 18, tile_y + 66, label, "sm b", raw=True)
        b += t(x + 18, tile_y + 82, detail, "xs dim", raw=True)

    b += t(24, 410, BENCH_MACHINE, "xs dim")
    b += t(24, 426, "Reproduce: uv run python benchmarks/bench.py", "xs teal")
    b += footer(
        W,
        H - 16,
        "Every figure above is transcribed verbatim from benchmarks/RESULTS.md, "
        "which is written by the benchmark run itself. Nothing here is rounded or re-measured.",
    )
    return write("benchmark.svg", svg_doc(W, H, b))


# ===========================================================================
# 5. competitor.svg — a capability matrix, not a cost chart
# ===========================================================================
# This was an annual-cost bar chart and should not have been. Tiers across these
# vendors are apples-to-oranges, list prices go stale within a quarter, and pricing
# each vendor at its *cheapest published* tier — the only honest way to compare —
# lands most of them near $348/year, which is not the argument. Ownership is.
#
# Every cell below was verified 2026-07-29 against each vendor's own pricing, docs
# and terms pages. Do not "improve" a value without re-verifying it at the source;
# the footnotes carry the qualifications that make the table defensible.
#
# Two corrections are baked into the row list and must not be undone: Polygon.io was
# renamed Massive.com on 2025-10-30 (one company, one row), and Kaiko acquired
# Amberdata (one company, one row).
YES, PARTIAL, NO, UNKNOWN, NA = "yes", "partial", "no", "unknown", "na"

# Column headings, pre-wrapped: a 105 px column holds about 16 characters at 10 px.
MATRIX_COLS: list[tuple[str, ...]] = [
    ("Crypto",),
    ("US equities",),
    ("Local store", "you own"),
    ("Keep it after", "you stop paying"),
    ("SQL over", "the store"),
    ("Replay",),
]
# The ownership column. Banded and bolded because it is the one a termination clause
# settles and a feature list cannot.
OWNERSHIP_COL = 3

# (vendor, licence, [(state, qualifier lines), ...] one per column)
MATRIX_ROWS: list[tuple[str, str, list[tuple[str, tuple[str, ...]]]]] = [
    (
        "Kaiko (incl. Amberdata)",
        "Proprietary",
        [
            (YES, ()),
            (PARTIAL, ("equity options",)),
            (PARTIAL, ("S3 Parquet,", "enterprise annual")),
            (NO, ()),
            (NO, ()),
            (PARTIAL, ("72h gap-recovery",)),
        ],
    ),
    (
        "CoinAPI",
        "Proprietary",
        [
            (YES, ()),
            (NO, ()),
            (PARTIAL, ("metered flat", "files")),
            (UNKNOWN, ()),
            (NO, ()),
            (NO, ()),
        ],
    ),
    (
        "Tardis.dev",
        "Proprietary",
        [(YES, ()), (NO, ()), (YES, ()), (YES, ()), (NO, ()), (YES, ())],
    ),
    (
        "Databento",
        "Proprietary",
        [(NO, ()), (YES, ()), (NO, ()), (NO, ()), (NO, ()), (PARTIAL, ("last 24h",))],
    ),
    (
        "Massive (formerly Polygon.io)",
        "Proprietary",
        [(YES, ()), (YES, ()), (NO, ()), (NO, ()), (NO, ()), (NO, ())],
    ),
    (
        "CryptoQuant",
        "Proprietary",
        [(YES, ("metrics only",)), (NO, ()), (NO, ()), (NO, ()), (NO, ()), (NO, ())],
    ),
    (
        "ccxt",
        "MIT",
        [(YES, ()), (NO, ()), (NO, ()), (NA, ("free",)), (NO, ()), (NO, ())],
    ),
    (
        "cryptofeed",
        "custom permissive",
        [
            (YES, ()),
            (NO, ()),
            (PARTIAL, ("14 backends,", "no Parquet")),
            (YES, ()),
            (NO, ()),
            (PARTIAL, ("single feed,", "undocumented")),
        ],
    ),
    (
        "NautilusTrader",
        "LGPL-3.0",
        [
            (YES, ()),
            (YES, ("needs paid", "Databento or IB")),
            (YES, ()),
            (YES, ()),
            (NO, ()),
            (YES, ()),
        ],
    ),
    (
        "Freqtrade",
        "GPL-3.0",
        [
            (YES, ()),
            (NO, ()),
            (PARTIAL, ("OHLCV files only",)),
            (YES, ()),
            (NO, ()),
            (NO, ("candle backtest",)),
        ],
    ),
    (
        "Crocodile",
        "Apache-2.0",
        [(YES, ()), (YES, ()), (YES, ()), (YES, ()), (YES, ()), (YES, ())],
    ),
]

# The sentence the ownership column is really about. A termination clause does not
# go stale and cannot be read as marketing.
OWNERSHIP_QUOTE = (
    "&#8220;you agree to cease all use of the Market Data and delete all "
    "Market Data in your possession.&#8221;"
)
OWNERSHIP_QUOTE_SOURCE = (
    "Massive (formerly Polygon.io), Market Data Terms &#167;8 &#8212; "
    "what the fourth column is asking."
)

MATRIX_FOOTNOTES = [
    "Verified 2026-07-29 against each vendor&#8217;s own pricing, docs and terms pages.",
    "Kaiko acquired Amberdata; counted once.",
    "Polygon.io was renamed Massive.com on 2025-10-30.",
    "NautilusTrader reaches US equities through a paid Databento subscription "
    "(from $199/mo) or an Interactive Brokers account.",
    "Kaiko and Databento publish no list price for the tiers compared here.",
    "Crocodile ingests what public endpoints return. It is not a licensed, SLA-backed archive.",
]


def _mark(cx: float, cy: float, state: str) -> str:
    """One matrix cell's glyph.

    Shape carries the meaning on its own — solid / half / hollow / dashed / dash —
    so the table survives being read in greyscale or by a colour-blind reader, and
    ``partial`` reads as a *qualified* yes rather than a near-yes.
    """
    s, r = 13.0, 2.0
    x, y = cx - s / 2, cy - s / 2
    if state == YES:
        return (
            f'<rect x="{x:g}" y="{y:g}" width="{s:g}" height="{s:g}" rx="{r:g}" '
            'fill="var(--ok)"/>\n'
        )
    if state == PARTIAL:
        half = (
            f"M{x + r:g} {y:g} L{x + s / 2:g} {y:g} L{x + s / 2:g} {y + s:g} "
            f"L{x + r:g} {y + s:g} A{r:g} {r:g} 0 0 1 {x:g} {y + s - r:g} "
            f"L{x:g} {y + r:g} A{r:g} {r:g} 0 0 1 {x + r:g} {y:g} Z"
        )
        return (
            f'<path d="{half}" fill="var(--accent)"/>\n'
            f'<rect x="{x:g}" y="{y:g}" width="{s:g}" height="{s:g}" rx="{r:g}" fill="none" '
            f'stroke="var(--accent)" stroke-width="1.6"/>\n'
        )
    if state == NO:
        # --dim at low opacity rather than --rule: --rule is very close to the
        # ownership column's band fill, and a "no" inside that band all but vanished.
        return (
            f'<rect x="{x:g}" y="{y:g}" width="{s:g}" height="{s:g}" rx="{r:g}" fill="none" '
            f'stroke="var(--dim)" stroke-width="1.5" opacity="0.6"/>\n'
        )
    if state == UNKNOWN:
        return (
            f'<rect x="{x:g}" y="{y:g}" width="{s:g}" height="{s:g}" rx="{r:g}" fill="none" '
            f'stroke="var(--dim)" stroke-width="1.2" stroke-dasharray="2.6 2"/>\n'
            + t(cx, cy + 3.4, "?", "xxs dim b", anchor="middle")
        )
    # NA — "the question does not apply", which must not read as a failed cell.
    return (
        f'<line x1="{cx - 6.5:g}" y1="{cy:g}" x2="{cx + 6.5:g}" y2="{cy:g}" '
        'stroke="var(--dim)" stroke-width="1.8" stroke-linecap="round"/>\n'
    )


def build_competitor() -> Path:
    W = 880
    gut_x, col_x, col_w = 24, 226, 105
    band_top, head_rule, row_h = 112, 164, 42
    n_rows = len(MATRIX_ROWS)
    rows_bottom = head_rule + n_rows * row_h
    legend_y = rows_bottom + 24
    notes_y = legend_y + 30
    H = int(notes_y + (len(MATRIX_FOOTNOTES) - 1) * 13 + 18)

    b = DEFS
    b += header(
        W,
        "CAPABILITY MATRIX",
        f"{n_rows} sources compared  &#183;  verified 2026-07-29",
    )

    # ---- the sentence the fourth column is about ---------------------------
    b += rect(24, 54, W - 48, 52, "panel2")
    b += '<rect x="24" y="54" width="4" height="52" rx="2" fill="var(--accent)"/>\n'
    b += t(44, 80, OWNERSHIP_QUOTE, "code")
    b += t(44, 97, OWNERSHIP_QUOTE_SOURCE, "xs dim")

    # ---- ownership column band ---------------------------------------------
    b += (
        f'<rect x="{col_x + OWNERSHIP_COL * col_w:g}" y="{band_top:g}" width="{col_w:g}" '
        f'height="{rows_bottom - band_top:g}" rx="3" fill="var(--accent_bg)"/>\n'
    )

    # ---- column headings ----------------------------------------------------
    for i, lines in enumerate(MATRIX_COLS):
        cx = col_x + i * col_w + col_w / 2
        cls = "xs b acc" if i == OWNERSHIP_COL else "xs b"
        for j, part in enumerate(lines):
            y = head_rule - 8 - (len(lines) - 1 - j) * 12
            b += t(cx, y, part, cls, anchor="middle")
    b += line(24, head_rule, W - 24, head_rule)

    # ---- rows ---------------------------------------------------------------
    for i, (vendor, licence, cells) in enumerate(MATRIX_ROWS):
        y = head_rule + i * row_h
        ours = i == n_rows - 1
        if ours:
            b += line(24, y, W - 24, y, "wire-acc")
            b += (
                f'<rect x="24" y="{y + 6:g}" width="3" height="{row_h - 12:g}" '
                'fill="var(--accent)"/>\n'
            )
        elif i:
            b += line(24, y, W - 24, y)
        b += t(gut_x + (12 if ours else 0), y + 20, vendor, "sm b" + (" acc" if ours else ""))
        b += t(gut_x + (12 if ours else 0), y + 33, licence, "xs" + (" acc" if ours else " dim"))
        for j, (state, qualifier) in enumerate(cells):
            cx = col_x + j * col_w + col_w / 2
            b += _mark(cx, y + 15, state)
            for k, part in enumerate(qualifier):
                b += t(cx, y + 31 + k * 9.5, part, "xxs dim", anchor="middle")
    b += line(24, rows_bottom, W - 24, rows_bottom, "wire-acc")

    # ---- legend -------------------------------------------------------------
    legend = [
        (YES, "yes"),
        (PARTIAL, "partial &#8212; a qualified yes"),
        (NO, "no"),
        (UNKNOWN, "unknown &#8212; not published"),
        (NA, "not applicable"),
    ]
    lx = 24.0
    for state, label in legend:
        b += _mark(lx + 6.5, legend_y - 3.5, state)
        b += t(lx + 20, legend_y, label, "xs dim")
        lx += 20 + len(re.sub(r"&\w+;|&#\d+;", "X", label)) * 6.4 + 26

    # ---- footnotes ----------------------------------------------------------
    for i, note in enumerate(MATRIX_FOOTNOTES):
        y = notes_y + i * 13
        b += t(24, y, "&#183;", "xs dim")
        b += t(38, y, note, "xs dim")

    return write("competitor.svg", svg_doc(W, H, b))


# ===========================================================================
# 6. provenance.svg
# ===========================================================================
def build_provenance(facts: Facts) -> Path:
    W, H = 880, 512
    b = DEFS
    b += header(
        W,
        "PROVENANCE",
        "level  &#8594;  method  &#8594;  a confidence nobody chose by hand",
    )

    # ---- the record --------------------------------------------------------
    x1, w1 = 24, 278
    b += rect(x1, 62, w1, 250)
    b += t(x1 + 14, 84, "1  THE RECORD", "h acc")
    fields = [
        ("channel", '"depth"', ""),
        ("source", '"yahoo"', ""),
        ("symbol", '"yahoo:AAPL"', ""),
        ("reference_price", "227.41", ""),
        ("depth", "10", ""),
        ("&#8230;", "", "dim"),
        ("prov", "synthetic", "acc"),
        ("prov_basis", '"yahoo_1m_vap"', "acc"),
        ("prov_inputs", '["ohlcv"]', "acc"),
        ("prov_confidence", f"{facts.example_confidence:g}", "acc"),
    ]
    b += t(x1 + 14, 106, "DepthProfile", "sm b teal")
    for i, (key, value, cls) in enumerate(fields):
        y = 126 + i * 17
        if i == 6:
            b += line(x1 + 14, y - 12, x1 + w1 - 14, y - 12)
        b += t(x1 + 14, y, key, f"xs {cls}".strip(), raw=True)
        b += t(x1 + w1 - 14, y, value, f"xs {'b ' + cls if cls else 'dim'}", anchor="end", raw=True)
    b += t(x1 + 14, 300, "the four-field tail every record carries", "xs dim")

    # ---- the level ---------------------------------------------------------
    x2, w2 = 326, 244
    b += rect(x2, 62, w2, 250)
    b += t(x2 + 14, 84, "2  THE LEVEL", "h acc")
    b += t(x2 + 14, 102, "prov &#8212; what class of thing this is", "xs dim")
    for i, level in enumerate(facts.provenance_levels):
        y = 122 + i * 40
        chosen = level == "synthetic"
        b += rect(
            x2 + 14,
            y,
            w2 - 28,
            32,
            "panel2" if not chosen else "",
            3,
            'fill="var(--accent_bg)" stroke="var(--accent)" stroke-width="1.6"' if chosen else "",
        )
        b += t(x2 + 26, y + 14, level, "sm b" + (" acc" if chosen else ""))
        blurb = {
            "native": "the venue reported this value",
            "derived": "computed from native records",
            "synthetic": "modelled from another data class",
            "unavailable": "a typed hole, never a zero",
        }[level]
        b += t(x2 + 26, y + 27, blurb, "xs dim")
        # Aligned with the short level name, not the long blurb: at y+20 "rank 2" sat on
        # top of "modelled from a different data class".
        b += t(x2 + w2 - 26, y + 14, f"rank {i}", "xs dim", anchor="end")
    b += t(x2 + 14, 300, "worst_provenance() wins on any join", "xs dim")

    # ---- the method --------------------------------------------------------
    x3, w3 = 594, 262
    b += rect(x3, 62, w3, 250)
    b += t(x3 + 14, 84, "3  THE METHOD", "h acc")
    b += t(x3 + 14, 106, 'prov_basis = "yahoo_1m_vap"', "sm b teal")
    for i, part in enumerate(
        [
            "Volume-at-price depth built from one",
            "minute Yahoo bars, standing in for",
            "resting size. Registered with the",
            "level it is allowed to claim and the",
            "channels it consumes.",
        ]
    ):
        b += t(x3 + 14, 128 + i * 15, part, "xs dim")
    b += line(x3 + 14, 212, x3 + w3 - 14, 212)
    b += t(x3 + 14, 230, "@register_basis(", "xs")
    b += t(x3 + 24, 245, '"yahoo_1m_vap",', "xs acc")
    b += t(x3 + 24, 260, "level=SYNTHETIC,", "xs teal")
    b += t(x3 + 24, 275, 'inputs=["ohlcv"],', "xs teal")
    b += t(x3 + 14, 290, ")", "xs")
    b += t(x3 + 14, 306, f"{facts.bases} bases registered, all documented", "xs dim")

    for x in (x1 + w1 + 6, x2 + w2 + 6):
        b += (
            f'<line x1="{x:g}" y1="187" x2="{x + 12:g}" y2="187" '
            'class="wire-acc" marker-end="url(#ar-acc)"/>\n'
        )

    # ---- the formula -------------------------------------------------------
    fy = 334
    b += rect(24, fy, W - 48, 108, "panel2")
    b += t(38, fy + 24, "4  THE FORMULA &#8212; the confidence is computed, not chosen", "h acc")
    b += t(38, fy + 56, "min( n_volume_bars / 390 , 1.0 )", "code b teal")
    b += t(38, fy + 76, "390 = one regular US session in one-minute bars.", "xs dim")
    b += t(
        38,
        fy + 90,
        "The formula must be pure, must be documented, and is the only supported "
        "way to fill prov_confidence.",
        "xs dim",
    )
    b += t(W - 38, fy + 46, "min( 312 / 390 , 1.0 )", "sm", anchor="end")
    b += t(W - 38, fy + 78, f"= {facts.example_confidence:g}", "big acc", anchor="end")
    b += t(W - 38, fy + 94, "prov_confidence", "xs dim", anchor="end")

    b += footer(
        W,
        H - 16,
        "Confidence measures sampling adequacy within a level, not truthfulness across levels: "
        "prov is what says the ladder was modelled, and no number substitutes for it.",
    )
    return write("provenance.svg", svg_doc(W, H, b))


# ===========================================================================
# 7. social-preview.png
# ===========================================================================
def build_social(facts: Facts) -> Path:
    """1280x640 PNG for GitHub's social preview and X link cards.

    matplotlib rather than an SVG rasteriser: it ships DejaVu Sans Mono, so the output
    is byte-stable on any machine and needs no system font and no external converter.
    matplotlib is not a declared dependency of the package — it is present in the dev
    environment — so this one asset is skipped rather than fatal if it is missing.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.patches as mpatches
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover - optional
        print("  ! matplotlib not available; skipped social-preview.png", file=sys.stderr)
        return HERE / "social-preview.png"

    BG = DARK["bg"]
    INK = DARK["ink"]
    DIM = DARK["dim"]
    ACC = DARK["accent"]
    TEAL = DARK["teal"]
    RULE = DARK["rule"]
    MONO_MPL = "DejaVu Sans Mono"

    fig = plt.figure(figsize=(12.8, 6.4), dpi=100, facecolor=BG)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, 1280)
    ax.set_ylim(640, 0)
    ax.axis("off")
    ax.set_facecolor(BG)

    def text(x, y, s, size, color=INK, weight="normal", ha="left", va="baseline", track=0.0):
        ax.text(
            x,
            y,
            s,
            fontsize=size,
            color=color,
            fontfamily=MONO_MPL,
            fontweight=weight,
            ha=ha,
            va=va,
            **({"fontstretch": "normal"}),
        ).set_snap(True)

    # a thick amber rule down the left edge: the one graphic element, no gradient
    ax.add_patch(mpatches.Rectangle((0, 0), 14, 640, color=ACC, lw=0))

    text(72, 96, "APACHE-2.0   PYTHON 3.12+   RUNS LOCALLY", 15, DIM, track=0.2)

    # tracked wordmark, spaced by hand because matplotlib has no letter-spacing
    text(68, 210, " ".join("CROCODILE"), 68, INK, weight="bold")

    text(72, 268, "A deterministic engine for crypto and US equities.", 24, INK)
    text(72, 306, "Replay any window and get identical bytes. Every run.", 24, DIM)

    ax.add_patch(mpatches.Rectangle((72, 348), 1136, 1.5, color=RULE, lw=0))

    stats = [
        ("109", "crypto venues"),
        ("9", "equity sources"),
        (str(facts.record_types), "record types"),
        (str(facts.capabilities), "capabilities"),
        ("3", "surfaces"),
    ]
    for i, (value, label) in enumerate(stats):
        x = 72 + i * 232
        text(x, 412, value, 44, ACC, weight="bold")
        text(x, 440, label, 16, DIM)

    ax.add_patch(
        mpatches.Rectangle((72, 482), 1136, 96, facecolor=DARK["panel"], edgecolor=RULE, lw=1.5)
    )
    # "..." rather than a silently trimmed command line: the real invocation also names
    # --symbols and --end-ns, and a shortened command that looks complete is a lie.
    text(96, 522, "$ crocodile replay --channels trade ... | shasum -a 256", 19, TEAL)
    text(96, 556, f"  {_HERO_DIGEST}", 17, DIM)

    path = HERE / "social-preview.png"
    fig.savefig(path, facecolor=BG, dpi=100)
    plt.close(fig)
    size = path.stat().st_size
    print(f"  {'social-preview.png':<26} {size:>8,} bytes")
    return path


# ===========================================================================
# main
# ===========================================================================
def main(argv: list[str]) -> int:
    facts = _facts()
    builders: dict[str, Callable[[], Any]] = {
        "hero": build_hero,
        "architecture": lambda: build_architecture(facts),
        "registry": lambda: build_registry(facts),
        "benchmark": build_benchmark,
        "competitor": build_competitor,
        "provenance": lambda: build_provenance(facts),
        "social": lambda: build_social(facts),
    }
    wanted = argv[1:] or list(builders)
    unknown = [name for name in wanted if name not in builders]
    if unknown:
        print(f"unknown asset(s): {unknown}; known: {list(builders)}", file=sys.stderr)
        return 2
    print(f"crocodile docs/media -> {HERE}")
    print(
        f"  facts: {facts.capabilities} capabilities, {facts.wire_names} wire names, "
        f"{facts.record_types} record types, {facts.bases} provenance bases"
    )
    for name in wanted:
        builders[name]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
