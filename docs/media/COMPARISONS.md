# README Comparison Methodology

## Data-layer comparison

[`data-layer-comparison.svg`](data-layer-comparison.svg) compares the default responsibilities of three projects. It does not compare measured speed, licensing rights, reliability, or data quality. An application can add storage and replay to a source-access library; those extensions are outside this comparison.

| Project | Evidence |
|---|---|
| Crocodile | [`pyproject.toml`](../../pyproject.toml), [`core/store/`](../../src/crocodile/core/store/), [`core/replay/`](../../src/crocodile/core/replay/), and [`surfaces/`](../../src/crocodile/surfaces/) define the connector, storage, replay, and access layers. |
| CCXT | The [official manual](https://docs.ccxt.com/docs/manual) documents unified public/private exchange APIs, API response structures, and order methods. |
| yfinance | The [official API reference](https://ranaroussi.github.io/yfinance/reference/index.html) documents Yahoo Finance access, ticker/history objects, downloads, and streaming interfaces. |

Upstream documentation was checked on 30 October 2026. CCXT and yfinance also serve as dependencies/connectors in Crocodile; they are not interchangeable products competing for the same entire workflow.

## Storage comparison

[`storage-comparison.svg`](storage-comparison.svg) uses the local run recorded in [`benchmark-run.md`](benchmark-run.md). The benchmark writes 500,000 canonical synthetic trades to Parquet with Zstandard level 5, then estimates the uncompressed JSON size by serializing equivalent fields. Both sides describe the same logical rows.

The comparison combines the effects of a columnar format and compression. It does not claim that Parquet is smaller than compressed JSON by the displayed ratio. The original benchmark labels sizes MB while dividing bytes by 1,048,576; the chart labels that unit correctly as MiB. Values in the report and chart are rounded.

The fixed seed controls fixture generation, not elapsed time. These are offline measurements on one Windows machine during normal desktop use. They do not measure network transport, exchange publication intervals, live capture loss, historical completeness, or another vendor's performance. Query timings are medians of seven repetitions; normalization, writing, and replay are individual stages. The full script remains at [`benchmarks/bench.py`](../../benchmarks/bench.py). Rebuild the figures with `uv run --extra ml python docs/media/build_comparisons.py` from the repository root.

Created By Aylin (Nazmi Efe Armutcu)
