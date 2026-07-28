# Base quickstart: find the whales in ten lines

Crocodile reads Uniswap V3 and Aerodrome swap events straight off Base L2 RPC logs and
normalizes them into the same `Trade` records a CEX connector emits. That is the whole point
of this page: once the on-chain data is in the lake, finding large swaps is a SQL query, not
a second codebase.

## Prerequisites

The Base connector reads L2 RPC logs through `web3`, which ships in the `onchain` extra — a
bare install gives you the core CEX and lake engine only.

```bash
uv pip install "crocodile[onchain] @ git+https://github.com/nazmiefearmutcu/Crocodile.git"
```

Then collect something. `replay` and `query` read the lake you point them at and nothing
else, so a fresh install answers empty until you have:

```bash
crocodile collect --asset-class crypto --sources base_onchain --symbols _ \
    --channels trade --data-dir data
```

Public Base data needs no API key. On-chain reads use a default RPC endpoint you can
override.

## The query

```python
from crocodile.crypto.client.client import CrypcodileClient

client = CrypcodileClient(data_dir="data")

whales = """
    SELECT symbol, price, amount, side, (amount * price) AS usd_value, local_ts
    FROM trade
    WHERE source = 'base_onchain' AND (amount * price) > 50000
    ORDER BY local_ts DESC
    LIMIT 10
"""
print(client.query(whales).to_pandas())
```

Two things in there are easy to get wrong:

- **The column is `source`, not `exchange`.** The merge collapsed the two forks' partitions
  into one `source=`, and the column went with it. A query written against `exchange` does
  not error — it just matches nothing, which is the more expensive failure.
- **The class is still `CrypcodileClient`.** The product is Crocodile; this public class kept
  its name because renaming it would break every caller for no gain. It is not a typo.

## What happens underneath

1. **RPC extraction** — the ingestion engine polls Base mainnet RPC for swap and reserve
   events on Uniswap V3 and Aerodrome pools.
2. **Decode and normalize** — raw Ethereum logs are decoded into the same `Trade` record type
   a Binance or Deribit connector produces. A cross-venue query does not know the difference.
3. **Provenance** — each record carries how it came to exist, so a modelled value cannot pass
   itself off as one the chain reported.
4. **Parquet storage** — records land in partitioned `.parquet` under your data directory.
5. **DuckDB** — the client registers one view per channel over those files and queries them
   in place. No import step, no server.

## Runnable examples

- [`examples/monitor_base_volume.py`](../examples/monitor_base_volume.py) — real-time
  on-chain log parser
- [`examples/base_dashboard.py`](../examples/base_dashboard.py) — Streamlit dashboard for Base
  DEX volume
- [`examples/farcaster_frame.py`](../examples/farcaster_frame.py) — deployable Farcaster frame
  endpoint
- [`examples/collect_base_onchain.py`](../examples/collect_base_onchain.py) — the collection
  loop on its own
