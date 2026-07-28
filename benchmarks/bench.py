"""Reproducible, network-free performance benchmark for the Crocodile
(import name ``crocodile``) market-data engine.

Scope note: this exercises the offline data path with crypto-shaped records, which is
the path both asset classes share — normalize, store, query, resample, replay. It is not
a crypto-only engine; it is a crypto-shaped fixture.

Measures the offline data-path on synthetic data only — no websockets, no REST,
no exchange access. Every stage uses a fixed RNG seed so the synthetic input is
deterministic across runs. Results are printed as a Markdown table to stdout and
written to ``benchmarks/RESULTS.md``.

Run:

    uv run python benchmarks/bench.py

Stages
------
A) NORMALIZE    feed a realistic raw Deribit trades message through the real
                ``normalize_message`` entrypoint and count emitted records.
B) WRITE        synthesize Trade records and write them through the real
                ``ParquetSink`` (zstd-5); report on-disk size + compression.
                Canonical records, not the retired crypto union: that union's
                row is four columns narrower than the one the product writes,
                so timing it measured a file the sink no longer produces — and
                pointed at a real lake root it would have dropped a second file
                schema into one partition, which the sink's own docstring says
                must not happen. The JSON baseline below mirrors the same
                columns for the same reason.
C) QUERY        time representative DuckDB aggregates via ``CrypcodileClient``.
D) RESAMPLE     resample stored trades to 1m OHLCV bars (``resample_ohlcv``).
E) REPLAY       k-way replay of the stored trade partitions.

All measured numbers are produced by actually running this script; nothing is
hard-coded.
"""

from __future__ import annotations

import asyncio
import json
import platform
import random
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

import duckdb
import polars as pl
import pyarrow

from crocodile.crypto.client.client import CrypcodileClient
from crocodile.crypto.exchanges.deribit.normalize import normalize_message
from crocodile.core.resample.ohlcv import resample_ohlcv
from crocodile.core.schema.enums import AssetClass, Side
from crocodile.core.schema.records import Trade
from crocodile.core.store.catalog import Catalog
from crocodile.core.store.parquet_sink import ParquetSink

# ---------------------------------------------------------------------------
# Configuration (scale these down if a stage is slow; the actual N used is
# printed/recorded so the table is always honest about the workload).
# ---------------------------------------------------------------------------
SEED = 42
NORMALIZE_TARGET = 600_000  # ~ emitted records through normalize_message
WRITE_N = 500_000  # Trade records written to the lake
QUERY_RUNS = 7  # repetitions for median query latency
RESAMPLE_RUNS = 3  # repetitions for resample timing

SYMBOL = "deribit:BTC-PERPETUAL"
SYMBOL_RAW = "BTC-PERPETUAL"
EXCHANGE = "deribit"
# 2023-11-14T22:13:20Z — a single UTC date so all rows land in one date partition.
BASE_TS_NS = 1_700_000_000_000_000_000


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _sig3(x: float) -> str:
    """Round to 3 significant figures, with thousands separators."""
    if x == 0:
        return "0"
    import math

    digits = 3 - 1 - math.floor(math.log10(abs(x)))
    rounded = round(x, digits)
    if digits <= 0:
        return f"{int(rounded):,}"
    return f"{rounded:,.{digits}f}"


def _commas(n: int) -> str:
    return f"{n:,}"


def _mb(num_bytes: int) -> str:
    return f"{num_bytes / 1_048_576:.1f}"


# ---------------------------------------------------------------------------
# Synthetic data generators (deterministic via random.Random(SEED))
# ---------------------------------------------------------------------------
def _build_raw_deribit_trades_msg(rng: random.Random, n_per_msg: int) -> dict:
    """Mimic a realistic raw Deribit ``trades.<instrument>.raw`` websocket message.

    Shape matches what ``normalize_message`` expects: ``params.channel`` starts
    with ``trades.`` and ``params.data`` is a list of trade dicts carrying
    ``trade_id, price, amount, direction, timestamp (ms), instrument_name``.
    """
    base_ms = BASE_TS_NS // 1_000_000
    px = 37_000.0
    data = []
    for i in range(n_per_msg):
        px += rng.uniform(-5.0, 5.0)  # random walk around ~37k (realistic BTC-PERP)
        data.append(
            {
                "trade_id": f"BTC-PERP-{i}",
                "price": round(px, 1),
                "amount": round(rng.uniform(10.0, 5000.0), 1),  # Deribit contracts
                "direction": "buy" if rng.random() < 0.5 else "sell",
                "timestamp": base_ms + i,  # ms
                "instrument_name": SYMBOL_RAW,
                "index_price": round(px - rng.uniform(0.0, 2.0), 2),
                "mark_price": round(px + rng.uniform(-1.0, 1.0), 2),
            }
        )
    return {"params": {"channel": "trades.BTC-PERPETUAL.raw", "data": data}}


def _make_trade(i: int, rng: random.Random, px_state: list[float]) -> Trade:
    """Create one realistic BTC-PERPETUAL Trade with monotonic local_ts (ns)."""
    px_state[0] += rng.uniform(-5.0, 5.0)
    price = round(px_state[0], 1)
    amount = round(rng.uniform(10.0, 5000.0), 1)
    side = Side.BUY if rng.random() < 0.5 else Side.SELL
    # Monotonic local_ts spaced ~5ms apart so a 500k stream spans ~42 min, all
    # within one UTC date partition.
    ts = BASE_TS_NS + i * 5_000_000
    return Trade(
        source=EXCHANGE,
        symbol=SYMBOL,
        symbol_raw=SYMBOL_RAW,
        source_ts=ts,
        local_ts=ts,
        asset_class=AssetClass.CRYPTO,
        id=str(i),
        price=price,
        amount=amount,
        side=side,
    )


# ---------------------------------------------------------------------------
# Stage A — normalize throughput
# ---------------------------------------------------------------------------
def bench_normalize() -> dict:
    rng = random.Random(SEED)
    n_per_msg = 1000
    # Pre-build a batch of distinct raw messages so we're not re-normalizing a
    # single cached dict (which would be unrealistically cache-friendly).
    n_msgs = max(1, NORMALIZE_TARGET // n_per_msg)
    msgs = [_build_raw_deribit_trades_msg(rng, n_per_msg) for _ in range(n_msgs)]
    local_ts = BASE_TS_NS

    # Warmup (count emitted so we know records/msg, also primes interpreter).
    warm = sum(1 for _ in normalize_message(msgs[0], local_ts))
    emitted_per_msg = warm

    total_emitted = 0
    t0 = time.perf_counter()
    for msg in msgs:
        for _rec in normalize_message(msg, local_ts):
            total_emitted += 1
    elapsed = time.perf_counter() - t0

    rec_per_s = total_emitted / elapsed
    ns_per_rec = elapsed / total_emitted * 1e9
    return {
        "n": total_emitted,
        "rec_per_s": rec_per_s,
        "ns_per_rec": ns_per_rec,
        "emitted_per_msg": emitted_per_msg,
        "elapsed": elapsed,
    }


# ---------------------------------------------------------------------------
# Stage B — write + compression
# ---------------------------------------------------------------------------
async def _write_trades(data_dir: Path, trades: list[Trade]) -> float:
    # Single flush at the end so we time the full buffered write + parquet encode.
    sink = ParquetSink(
        data_dir=data_dir,
        max_buffer_rows=WRITE_N + 1,  # avoid mid-stream auto-flush
        flush_interval_seconds=9_999_999,  # disable time-based flush
    )
    t0 = time.perf_counter()
    for t in trades:
        await sink.put(t)
    await sink.flush()
    return time.perf_counter() - t0


def bench_write(data_dir: Path) -> tuple[dict, list[Trade]]:
    rng = random.Random(SEED + 1)
    px_state = [37_000.0]
    trades = [_make_trade(i, rng, px_state) for i in range(WRITE_N)]

    elapsed = asyncio.run(_write_trades(data_dir, trades))

    parquet_files = list(data_dir.rglob("channel=trade/**/part-*.parquet"))
    on_disk = sum(p.stat().st_size for p in parquet_files)

    # Raw uncompressed estimate: JSON-encode the same logical rows (the canonical
    # wire/serialized form), summed byte length. This is an honest "what the same
    # data costs as uncompressed JSON" baseline for a compression ratio.
    raw_bytes = 0
    enc = json.dumps
    for t in trades:
        raw_bytes += len(
            enc(
                {
                    "source": t.source,
                    "symbol": t.symbol,
                    "symbol_raw": t.symbol_raw,
                    "asset_class": t.asset_class.value,
                    "source_ts": t.source_ts,
                    "local_ts": t.local_ts,
                    "prov": t.prov.value,
                    "prov_basis": t.prov_basis,
                    "prov_confidence": t.prov_confidence,
                    "prov_inputs": t.prov_inputs,
                    "id": t.id,
                    "price": t.price,
                    "amount": t.amount,
                    "side": t.side.value,
                    "liquidation": t.liquidation,
                }
            ).encode("utf-8")
        )

    rec_per_s = WRITE_N / elapsed
    return (
        {
            "n": WRITE_N,
            "on_disk": on_disk,
            "bytes_per_rec": on_disk / WRITE_N,
            "raw_bytes": raw_bytes,
            "ratio": raw_bytes / on_disk if on_disk else 0.0,
            "rec_per_s": rec_per_s,
            "elapsed": elapsed,
            "n_files": len(parquet_files),
        },
        trades,
    )


# ---------------------------------------------------------------------------
# Stage C — DuckDB query latency
# ---------------------------------------------------------------------------
def bench_query(data_dir: Path) -> dict:
    client = CrypcodileClient(data_dir=data_dir)

    sql_groupby = (
        "SELECT symbol, count(*) AS n, sum(amount) AS total_amount "
        "FROM trade GROUP BY symbol"
    )
    sql_count = "SELECT count(*) AS n FROM trade"

    # Warmup (also forces view registration + first parquet open).
    _ = client.query(sql_count)
    _ = client.query(sql_groupby)

    def _time(sql: str) -> tuple[list[float], pl.DataFrame]:
        times = []
        last = None
        for _ in range(QUERY_RUNS):
            t0 = time.perf_counter()
            last = client.query(sql)
            times.append((time.perf_counter() - t0) * 1000.0)  # ms
        return times, last

    gb_times, gb_df = _time(sql_groupby)
    ct_times, ct_df = _time(sql_count)

    rows_scanned = int(ct_df["n"][0])
    return {
        "groupby_ms": statistics.median(gb_times),
        "count_ms": statistics.median(ct_times),
        "rows_scanned": rows_scanned,
        "groupby_total_amount": float(gb_df["total_amount"][0]),
    }


# ---------------------------------------------------------------------------
# Stage D — resample throughput
# ---------------------------------------------------------------------------
def bench_resample(data_dir: Path, n_input: int) -> dict:
    catalog = Catalog(data_dir)
    start_ns = BASE_TS_NS
    end_ns = BASE_TS_NS + (WRITE_N + 1) * 5_000_000

    # Warmup.
    _ = resample_ohlcv(catalog, SYMBOL, start_ns, end_ns, "1m")

    times = []
    bars_df = None
    for _ in range(RESAMPLE_RUNS):
        t0 = time.perf_counter()
        bars_df = resample_ohlcv(catalog, SYMBOL, start_ns, end_ns, "1m")
        times.append(time.perf_counter() - t0)
    elapsed = statistics.median(times)
    out_bars = len(bars_df)
    return {
        "n_input": n_input,
        "out_bars": out_bars,
        "rows_per_s": n_input / elapsed,
        "elapsed": elapsed,
    }


# ---------------------------------------------------------------------------
# Stage E — replay throughput
# ---------------------------------------------------------------------------
def bench_replay(data_dir: Path) -> dict:
    client = CrypcodileClient(data_dir=data_dir)
    start_ns = BASE_TS_NS
    end_ns = BASE_TS_NS + (WRITE_N + 1) * 5_000_000

    # Warmup (small, just primes the path; full count below is the measurement).
    _ = next(client.replay(["trade"], [SYMBOL], start_ns, start_ns + 1_000_000), None)

    t0 = time.perf_counter()
    count = 0
    for _rec in client.replay(["trade"], [SYMBOL], start_ns, end_ns):
        count += 1
    elapsed = time.perf_counter() - t0
    return {
        "n": count,
        "rec_per_s": count / elapsed,
        "elapsed": elapsed,
    }


# ---------------------------------------------------------------------------
# Machine info
# ---------------------------------------------------------------------------
def machine_info() -> str:
    proc = platform.processor() or platform.machine()
    py = platform.python_version()
    cpus = __import__("os").cpu_count()
    return (
        f"{platform.system()} {platform.release()} | "
        f"{proc} ({platform.machine()}) | {cpus} logical CPUs | "
        f"Python {py} | polars {pl.__version__} | "
        f"duckdb {duckdb.__version__} | pyarrow {pyarrow.__version__}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    overall_t0 = time.perf_counter()
    data_dir = Path(tempfile.mkdtemp(prefix="crypcodile_bench_"))
    try:
        print(f"[bench] temp lake: {data_dir}", file=sys.stderr)

        print("[bench] A) normalize ...", file=sys.stderr)
        a = bench_normalize()

        print("[bench] B) write + compression ...", file=sys.stderr)
        b, _trades = bench_write(data_dir)

        print("[bench] C) query ...", file=sys.stderr)
        c = bench_query(data_dir)

        print("[bench] D) resample ...", file=sys.stderr)
        d = bench_resample(data_dir, n_input=b["n"])

        print("[bench] E) replay ...", file=sys.stderr)
        e = bench_replay(data_dir)

        total_runtime = time.perf_counter() - overall_t0

        rows = [
            (
                "A. Normalize (raw Deribit trades → records)",
                _commas(a["n"]),
                f"{_sig3(a['rec_per_s'])} rec/s",
                f"{a['ns_per_rec']:.0f} ns/record",
            ),
            (
                "B. Write throughput (ParquetSink, zstd-5)",
                _commas(b["n"]),
                f"{_sig3(b['rec_per_s'])} rec/s",
                f"{b['elapsed']:.2f} s total",
            ),
            (
                "B. On-disk size (zstd-5 parquet)",
                _commas(b["n"]),
                f"{_mb(b['on_disk'])} MB",
                f"{b['bytes_per_rec']:.1f} bytes/record",
            ),
            (
                "B. Compression vs raw JSON",
                _commas(b["n"]),
                f"{b['ratio']:.2f}x",
                f"{_mb(b['raw_bytes'])} MB JSON → {_mb(b['on_disk'])} MB",
            ),
            (
                "C. Query: GROUP BY symbol (count+sum)",
                _commas(c["rows_scanned"]),
                f"{c['groupby_ms']:.1f} ms",
                "median of 7 runs",
            ),
            (
                "C. Query: count(*)",
                _commas(c["rows_scanned"]),
                f"{c['count_ms']:.2f} ms",
                "median of 7 runs",
            ),
            (
                "D. Resample → 1m OHLCV bars",
                _commas(d["n_input"]),
                f"{_sig3(d['rows_per_s'])} rows/s",
                f"{_commas(d['out_bars'])} bars out",
            ),
            (
                "E. Replay (k-way merge → Records)",
                _commas(e["n"]),
                f"{_sig3(e['rec_per_s'])} rec/s",
                f"{e['elapsed']:.2f} s total",
            ),
        ]

        header = "| Benchmark | N | Result | Detail |\n|---|---:|---:|---|"
        body = "\n".join(
            f"| {name} | {n} | {result} | {detail} |"
            for (name, n, result, detail) in rows
        )
        mach = machine_info()

        md_lines = [
            "## Crocodile data-path benchmark",
            "",
            "Network-free, synthetic-data benchmark of the offline data path "
            "(normalize → store → query → resample → replay). "
            "Fixed RNG seed (42); all numbers measured by running the script.",
            "",
            header,
            body,
            "",
            f"**Machine:** {mach}",
            "",
            f"**Total benchmark runtime:** {total_runtime:.1f} s",
            "",
            "Reproduce: `uv run python benchmarks/bench.py`",
            "",
        ]
        md = "\n".join(md_lines)

        # stdout
        print()
        print(md)

        # file
        out_path = Path(__file__).resolve().parent / "RESULTS.md"
        out_path.write_text(md + "\n", encoding="utf-8")
        print(f"\n[bench] wrote {out_path}", file=sys.stderr)

        # Sanity gates — fail loudly if a number is implausible.
        assert a["rec_per_s"] > 0
        assert b["rec_per_s"] > 0
        assert b["ratio"] > 1.0, f"compression ratio not > 1: {b['ratio']}"
        assert c["groupby_ms"] > 0 and c["count_ms"] > 0
        assert d["rows_per_s"] > 0
        assert e["rec_per_s"] > 0
        assert e["n"] == b["n"], f"replay count {e['n']} != written {b['n']}"
        assert c["rows_scanned"] == b["n"], (
            f"query scanned {c['rows_scanned']} != written {b['n']}"
        )

        return 0
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
