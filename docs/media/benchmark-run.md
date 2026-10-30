# Local Benchmark Run

Run on 30 October 2026 against source commit `ba55eb03e5a34c1879188d9b11cc42f8b65e0aac`. Command: `uv run --no-sync python -X utf8 benchmarks/bench.py`. The UTF-8 option avoids Windows console encoding errors.

The output below is retained from the completed run. Sizes labeled MB by the script use 1,048,576 bytes (MiB). The base RNG seed is 42; trade generation uses seed 43 (`SEED + 1`). The machine was in normal desktop use.

## Crocodile data-path benchmark

Network-free, synthetic-data benchmark of the offline data path (normalize → store → query → resample → replay). Fixed RNG seed (42); all numbers measured by running the script.

| Benchmark | N | Result | Detail |
|---|---:|---:|---|
| A. Normalize (raw Deribit trades → records) | 600,000 | 1,730,000 rec/s | 577 ns/record |
| B. Write throughput (ParquetSink, zstd-5) | 500,000 | 60,300 rec/s | 8.29 s total |
| B. On-disk size (zstd-5 parquet) | 500,000 | 5.8 MB | 12.2 bytes/record |
| B. Compression vs raw JSON | 500,000 | 28.87x | 167.9 MB JSON → 5.8 MB |
| C. Query: GROUP BY symbol (count+sum) | 500,000 | 14.6 ms | median of 7 runs |
| C. Query: count(*) | 500,000 | 5.42 ms | median of 7 runs |
| D. Resample → 1m OHLCV bars | 500,000 | 5,730,000 rows/s | 42 bars out |
| E. Replay (k-way merge → Records) | 500,000 | 3,950 rec/s | 126.73 s total |

**Machine:** Windows 11 | AMD64 Family 25 Model 80 Stepping 0, AuthenticAMD (AMD64) | 16 logical CPUs | Python 3.12.10 | polars 1.41.2 | duckdb 1.5.3 | pyarrow 24.0.0

**Total benchmark runtime:** 141.0 s

Reproduce: `uv run python benchmarks/bench.py`

Created By Nazmi Efe Armutcu (Aylin)
