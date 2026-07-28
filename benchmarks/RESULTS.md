## Crocodile data-path benchmark

Network-free, synthetic-data benchmark of the offline data path (normalize → store → query → resample → replay). Fixed RNG seed (42); all numbers measured by running the script.

| Benchmark | N | Result | Detail |
|---|---:|---:|---|
| A. Normalize (raw Deribit trades → records) | 600,000 | 3,540,000 rec/s | 282 ns/record |
| B. Write throughput (ParquetSink, zstd-5) | 500,000 | 245,000 rec/s | 2.04 s total |
| B. On-disk size (zstd-5 parquet) | 500,000 | 5.8 MB | 12.2 bytes/record |
| B. Compression vs raw JSON | 500,000 | 20.20x | 117.3 MB JSON → 5.8 MB |
| C. Query: GROUP BY symbol (count+sum) | 500,000 | 5.8 ms | median of 7 runs |
| C. Query: count(*) | 500,000 | 0.36 ms | median of 7 runs |
| D. Resample → 1m OHLCV bars | 500,000 | 17,000,000 rows/s | 42 bars out |
| E. Replay (k-way merge → Records) | 500,000 | 458,000 rec/s | 1.09 s total |

**Machine:** Darwin 25.5.0 | arm (arm64) | 10 logical CPUs | Python 3.12.12 | polars 1.41.2 | duckdb 1.5.3 | pyarrow 24.0.0

**Total benchmark runtime:** 5.6 s

Reproduce: `uv run python benchmarks/bench.py`

