"""The README's headline claim, run rather than asserted.

The front page says you can replay a window and get the same bytes back, every run, and
the hero image shows two SHA-256 digests side by side to prove it. Until this file, the
suite proved something weaker: ``test_merge.py`` asserts the merge's *order* is invariant
to the order the input streams are handed in, which is a real and load-bearing property —
``merge.py`` calls leaving any tie to ``heapq`` a bug "for an engine whose whole claim is
determinism" — but ordering is not byte identity, and it was measured inside one process.

One process is the wrong place to measure it. Python randomises string hashing per
interpreter, so anywhere the pipeline leaned on ``set`` or ``dict`` iteration order the
answer would be stable within a run and different in the next one. That is the realistic
way this claim breaks, and it is invisible to any test that replays twice in the same
process. The same goes for a stray wall-clock read or an unseeded shuffle reaching the
encoder: repeat it in-process and you may well get the same bytes by accident.

So this replays the same window in two separate subprocesses under two different
``PYTHONHASHSEED`` values and compares the raw stdout. Bytes, not text — decoding first
would let newline translation paper over a difference. Both digests go in the failure
message, because "the bytes differed" is not a bug report and "a1b2… != c3d4…" is the
start of one.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Final

from crocodile.core.schema.enums import AssetClass, Side
from crocodile.core.schema.records import Trade
from crocodile.core.store.parquet_sink import ParquetSink

_ROWS: Final = 200
_SEED: Final = 42
_BASE_TS_NS: Final = 1_700_000_000_000_000_000

# Two seeds, not one. A single alternative seed can coincide with the default by luck on a
# small dictionary; two runs that must agree with each other rather than with a baseline
# cannot be satisfied by coincidence in the same way.
_HASH_SEEDS: Final = ("0", "1")

# Both symbols are collected into one lake so replay has more than one stream to merge.
# A single-stream replay is sorted trivially and would pass this test while proving
# nothing about the k-way merge, which is where a hash-order dependency would live.
_SYMBOLS: Final = ("deribit:BTC-PERPETUAL", "binance:BTCUSDT")


def _write_lake(data_dir: Path) -> None:
    """Write ``_ROWS`` deterministic trades across two venues through the real sink.

    Synthetic on purpose: a captured lake is not reproducible and would need the network.
    The generator matches the one in ``benchmarks/bench.py`` and ``docs/media/build.py`` —
    same seed, same ~5 ms spacing — so what lands on disk has the shape a collect run
    produces rather than a shape invented for this test.
    """
    rng = random.Random(_SEED)
    price = [37_000.0]

    def make(index: int) -> Trade:
        price[0] += rng.uniform(-5.0, 5.0)
        ts = _BASE_TS_NS + index * 5_000_000
        symbol = _SYMBOLS[index % 2]
        source, raw = symbol.split(":", 1)
        return Trade(
            source=source,
            symbol=symbol,
            symbol_raw=raw,
            source_ts=ts,
            local_ts=ts,
            asset_class=AssetClass.CRYPTO,
            id=str(index),
            price=round(price[0], 1),
            amount=round(rng.uniform(1.0, 50.0), 1),
            side=Side.BUY if rng.random() < 0.5 else Side.SELL,
        )

    async def run() -> None:
        sink = ParquetSink(
            data_dir=data_dir,
            max_buffer_rows=_ROWS + 1,
            flush_interval_seconds=9_999_999,
        )
        for index in range(_ROWS):
            await sink.put(make(index))
        await sink.flush()

    asyncio.run(run())


def _replay_stdout(data_dir: Path, hash_seed: str) -> bytes:
    """Replay the whole window in a fresh interpreter and return its stdout, verbatim.

    Invoked as ``python -m crocodile.surfaces.entrypoint`` rather than through the
    ``crocodile`` console script: the installed script is resolved at install time and a
    stale one would make this test measure the wrong package.
    """
    argv = [
        sys.executable,
        "-m",
        "crocodile.surfaces.entrypoint",
        "replay",
        "--asset-class",
        "crypto",
        "--channels",
        "trade",
        "--start-ns",
        "0",
        "--end-ns",
        str(2**63 - 1),
        "--data-dir",
        str(data_dir),
    ]
    for symbol in _SYMBOLS:
        argv += ["--symbols", symbol]

    proc = subprocess.run(
        argv,
        capture_output=True,
        env={**os.environ, "PYTHONHASHSEED": hash_seed, "NO_COLOR": "1"},
        cwd=Path(__file__).resolve().parents[2],
    )
    assert proc.returncode == 0, (
        f"replay exited {proc.returncode} under PYTHONHASHSEED={hash_seed}\n"
        f"{proc.stderr.decode(errors='replace')}"
    )
    return proc.stdout


def test_replay_is_byte_identical_across_processes() -> None:
    """Two interpreters, two hash seeds, one window: the bytes must match exactly."""
    with tempfile.TemporaryDirectory(prefix="crocodile-byte-identity-") as tmp:
        lake = Path(tmp) / "data"
        _write_lake(lake)

        first, second = (_replay_stdout(lake, seed) for seed in _HASH_SEEDS)

    assert first, "replay produced no output; the lake or the window is wrong, not the engine"
    assert first == second, (
        "replay is not byte-identical across processes — the README claims it is.\n"
        f"  PYTHONHASHSEED={_HASH_SEEDS[0]}: {len(first)} bytes, "
        f"sha256 {hashlib.sha256(first).hexdigest()}\n"
        f"  PYTHONHASHSEED={_HASH_SEEDS[1]}: {len(second)} bytes, "
        f"sha256 {hashlib.sha256(second).hexdigest()}"
    )
