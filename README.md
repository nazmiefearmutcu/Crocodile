<p align="center"><img src="assets/logo.svg" width="84" alt="Crocodile"></p>

<h1 align="center">Crocodile</h1>

<p align="center">
  <b>A deterministic market-data engine for crypto <i>and</i> US equities.</b><br>
  Pull order books, trades, funding, option chains, filings and on-chain DEX events from
  109 crypto venues and nine equity sources into one Parquet lake you own —<br>then replay
  any window and get the same bytes back.
</p>

<p align="center">
  <a href="LICENSE"><img alt="Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-2ea043"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3572A5">
  <img alt="3,941 tests" src="https://img.shields.io/badge/tests-3%2C941-2ea043">
  <img alt="62 CLI commands" src="https://img.shields.io/badge/CLI-62%20commands-a3620a">
  <img alt="API keys mostly optional" src="https://img.shields.io/badge/API%20keys-mostly%20optional-f0a92e">
</p>

![A crocodile session: the lake catalog, a DuckDB query over it, then the same window replayed twice with identical SHA-256 digests](docs/media/hero-terminal.svg)

Every market-data tool can fetch a price. Three things make this one worth the disk space.

**One schema for everything.** A hand-written Deribit connector, a ccxt Kraken venue, a
Uniswap V3 pool on Base and an SEC EDGAR Form 4 all emit the *same* record types — 30 of
them, defined once. "Show me every trade across all my sources" is one SQL statement, not
four codebases.

**One capability, both markets.** Crocodile is the merge of two engines — Crypcodile for
crypto, Stockodile for equities — and both histories are in this repository's `git log`.
What came out is a single registry of **49 capabilities**, 42 of which answer for both asset
classes under one name and one parameter schema. The seven that do not each carry a written
argument for why no equity analogue can exist. The CLI, the REST API and the MCP server are
*projections* of that registry, so none of them can drift from it.

**The lake is yours, and it says where every number came from.** What lands on disk is
normalized, validated and replayable. Every record also carries how it came to exist: a
provenance level, the method it rests on, and a confidence computed by a registered formula
rather than chosen by hand. A modelled answer says so before you read a row.

## How it compares

The market-data business is mostly rented. You pay monthly, you query an API, and when you
stop paying you are contractually required to delete what you pulled. Massive's Market Data
Terms §8 put it plainly:

> you agree to cease all use of the Market Data and delete all Market Data in your
> possession.

Crocodile writes Parquet into a directory you chose. There is nothing to stop paying for.

![Capability matrix comparing eleven market-data tools on crypto, US equities, a local store you own, keeping the data after you stop paying, SQL over the store, and deterministic replay](docs/media/competitor.svg)

Three honest notes, because a comparison nobody can check is worth nothing:

- **Crocodile is not a licensed archive.** Kaiko and Databento sell SLA-backed, audit-grade
  history under venue agreements. Crocodile ingests what public endpoints return. If you
  have a compliance requirement, these are not interchangeable.
- **Deterministic replay is not unique.** Tardis.dev ships four documented replay endpoints,
  and NautilusTrader has nanosecond L3-to-bar event replay. Both are good. What is unusual
  here is the *combination* — both asset classes, an arbitrary window, over a store you own,
  for nothing.
- **Neither is MCP.** CoinAPI, Massive and CryptoQuant all ship MCP servers. What is unusual
  is that Crocodile's MCP tools, REST routes and CLI commands are generated from one
  declaration, so an agent, a `curl` and a shell cannot disagree about what a capability
  does.

<sub>Verified 2026-07-29 against each vendor's own pricing, docs and terms pages. Kaiko has
acquired Amberdata; counted once. Polygon.io was renamed Massive.com on 2025-10-30.
NautilusTrader reaches US equities through a paid Databento subscription (from $199/mo) or
an Interactive Brokers account.</sub>

## Install

**Not on PyPI.** That name belongs to an unrelated project, so install from the repository:

```bash
uv pip install "crocodile[full] @ git+https://github.com/nazmiefearmutcu/Crocodile.git"
```

Drop `[full]` and you still get the whole streaming core: every native connector, the
Parquet lake, replay, and the 62-command CLI. Heavier surfaces are opt-in, so you never pay
for a dependency tree you don't use:

| Extra | Adds |
|---|---|
| `[market]` | 100+ exchanges through the universal ccxt connector |
| `[ml]` | funding prediction, Black-Scholes, 3D vol surfaces (xgboost, scipy, matplotlib) |
| `[web]` | FastAPI server + Streamlit examples |
| `[onchain]` | Base L2 / GMX / Superchain connectors (web3) |
| `[full]` | everything above |

There is no desktop extra. Nothing in this repository needs a display server.

## Five minutes

```bash
# stream Deribit BTC-perp trades + book deltas into a local lake
crocodile collect --asset-class crypto --sources deribit --symbols BTC-PERPETUAL \
    --channels trade --channels book_delta --data-dir data

# the lake is just partitioned Parquet — one DuckDB view per channel on disk
crocodile query --asset-class crypto "SELECT count(*) AS n FROM trade"

# replay that window later — same bytes, every run
crocodile replay --asset-class crypto --channels trade --symbols BTC-PERPETUAL \
    --start-ns 0 --end-ns 9223372036854775807
```

Three things in there are load-bearing and easy to get wrong:

- **`--sources`, not `--exchange`/`--provider`.** The merge collapsed the two forks'
  partitions into one `source=`, and the flag went with it. Lakes written under the old
  `exchange=` prefix want `crocodile migrate-lake` first.
- **`--asset-class` when the symbol does not name its market.** A bare `BTC-PERPETUAL` could
  belong to either fork. Guessing is how a request lands in the wrong market's
  implementation and comes back with plausible numbers, so it refuses instead.
- **There is no `records` table.** The lake registers one view per `channel=` directory it
  finds — `trade`, `book_snapshot`, `ohlcv` — so the channel is the view name rather than a
  column. `crocodile catalog-channels` lists what a given lake actually has.

`replay` and `query` read the lake you point them at and nothing else. There is no bundled
sample and no offline fallback, so both answer empty on a fresh clone until you have
collected something. There is also an interactive shell — `crocodile shell` — with history
and tab-completion, and every command runs inside it.

## Reaching the whole market

![Architecture: 109 crypto venues and nine equity sources normalize into 30 record types, land in a partitioned Parquet lake, and surface as CLI, REST and MCP](docs/media/architecture.svg)

On the crypto side Crocodile speaks to **109 venues**: ten native connectors, hand-written
for fidelity, plus the entire [ccxt](https://github.com/ccxt/ccxt) family behind one
universal connector. Six names exist in both — binance, bybit, coinbase, deribit, derive,
okx — and the native connector wins, so the total is a union rather than a sum:
10 ∪ 105 = 109.

That 105 is not a fixed number. The dependency is `ccxt>=4.5`, and 105 is what the version
resolved here ships. `crocodile markets --asset-class crypto` prints the count your install
actually has.

| | Venues |
|---|---|
| **Native** | Binance · Bybit · Coinbase · Deribit · OKX · Base on-chain (Uniswap V3, Aerodrome) · GMX/Synthetix · Derive · Superchain · CoinGecko |
| **Universal** | every ccxt exchange id — Kraken, KuCoin, MEXC, Gate, HTX, Bitget, … |

On the equity side there are **nine sources**, and what each may be asked for is derived from
the source itself rather than from a shared menu — so a channel is offered for the providers
that serve it and nowhere else:

| Source | Writes | Key |
|---|---|---|
| `alpaca` | `trade` `quote` `ohlcv` | yes |
| `yahoo` | `options_chain` `ohlcv` `corp_action` `insider` | no |
| `sec_edgar` | `insider` `holding_13f` `filing` `fundamental` | no (contact string) |
| `treasury` | `macro_series` | no |
| `tiingo` | `ohlcv` `corp_action` | yes (free tier) |
| `stooq` | `ohlcv` `index_value` | no |
| `msn_money` | `ohlcv` `corp_action` | no |
| `google_finance` | `trade` `index_value` `fundamental` | no |
| `finnhub` | `trade` | yes |

`openfigi` is deliberately not a source: it enriches an instrument universe and writes no
channel of its own.

You don't have to name symbols. Name a *slice of the market* and Crocodile resolves the
concrete list from the live universe:

```bash
# the 200 most-liquid spot pairs on Binance, streamed over a single WebSocket
crocodile collect-market --asset-class crypto --sources binance --top 200 \
    --kinds spot --use-ws --channels trade --channels book_ticker

# every USDT perpetual across three venues at once, order books included
crocodile collect-market --asset-class crypto --sources bybit,okx,mexc \
    --all-symbols --quote USDT --kinds perpetual --channels book_snapshot \
    --max-symbols 400

# the whole coin universe — 17k+ coins, including the long tail no CEX lists
crocodile collect --asset-class crypto --sources coingecko --symbols _ \
    --channels ohlcv
```

Two design choices make that scale honestly. The ccxt path is **REST-poll-first**, which
works on every venue, but upgrades to a **single multi-symbol WebSocket** per channel where
the exchange supports it (`watchTradesForSymbols` / `watchTickers`) — the difference between
one socket for three symbols and one socket for a whole exchange's book. And `universe` ranks
any venue's markets by live 24h volume, so `--top N` covers the liquid core instead of ten
thousand dead pairs.

### The whole market, on one screen

`crocodile census` measures the market live and prints one JSON document — connector reach,
venue market counts, the coin universe, market cap, dominance, and total value locked. Every
figure comes from a keyless public feed. A run over the twelve major venues, 2026-07-28:

> **109** reachable venues · **34,208** markets across the majors · **17,863** active coins ·
> **$2.28T** market cap · **$75.4B** DeFi TVL

```bash
crocodile census --asset-class crypto \
    --venues binance,bybit,okx,coinbase,kraken,kucoin,mexc,gate,htx,bitget,bingx,cryptocom
```

Name the venues explicitly. A bare `crocodile census` reports zero of them today — an empty
tuple default reaches the parameter as a literal string — and zero venues is
indistinguishable from a census that ran and found nothing. The same bug makes `--kinds`
mandatory on `collect-market`.

## One declaration, three projections

![One capability declared once, projected into a CLI command, a REST route and an MCP tool](docs/media/registry-projection.svg)

Three numbers describe the same CLI, and which one you want depends on the question. **62**
is what `--help` lists. **54** folds away eight retired spellings kept resolving for callers
already wired to them. **49** is the capability registry — every command below except the
five launchers is a projection of one declaration, which is also what the REST route and the
MCP tool are projections of.

| Cluster | Commands |
|---|---|
| **Lake** | `collect` · `collect-market` · `backfill` · `replay` · `query` · `export` · `resample` |
| **Discovery** | `census` · `markets` · `universe` · `search` · `resolve-symbols` · `data-coverage` · `list-exchanges` · `catalog` · `catalog-channels` · `catalog-dates` · `catalog-exchanges` · `catalog-inventory` · `catalog-scan` · `catalog-stats` · `catalog-summary` · `catalog-symbols` |
| **Options & funding** | `iv-surface` · `term-structure` · `vol-skew` · `risk-reversal` · `funding-apr` · `funding-predict` · `basis` · `perp-basis` · `spot-future-basis` · `open-interest` |
| **Microstructure** | `ofi` · `slippage` · `whale-alerts` · `liquidity-depth` · `depth` · `indicators` |
| **On-chain / L2 risk** | `sequencer-latency` · `peg-deviation` · `chaos-score` · `lending-stress` · `gas-vol` · `smart-money` · `label-transfers` · `mev-sandwich` · `onchain-price` · `base-market-data` |
| **Servers** | `mcp` · `api` |
| **Operator** | `shell` · `update` · `migrate-lake` |
| **Retired spellings** | `inventory_snapshot` · `list_data_channels` · `list_dates` · `list_exchanges_on_disk` · `list_symbols` · `query_market_data` · `search_symbols` · `simulate-price-impact` |

The last row is a ledger that only shrinks. Each entry was on the wire under one of the two
forks and resolves to a single declaration, so there is never a second implementation behind
a second name.

Ingest survives disconnects with sequence-gap bridging and a dead-letter queue
([`src/crocodile/core/ingest/`](src/crocodile/core/ingest/)); whatever reaches disk is
normalized against the [30-record schema](src/crocodile/core/schema/records.py) and
replayable.

## Where every number came from

![Provenance: a record's level, the method behind it, and a confidence computed by a registered formula](docs/media/provenance.svg)

Four fields ride along on every record. `prov` says what class of thing the value is —
`native` when the venue reported it, `derived` when it was computed from native records,
`synthetic` when it was modelled from another data class, `unavailable` when it is a typed
hole rather than a zero. `prov_basis` names the registered method. `prov_inputs` lists what
that method consumed. `prov_confidence` is filled by the basis's own pure, documented
formula — there is no supported way to type a confidence in by hand.

On a join, `worst_provenance()` wins, so a modelled input cannot launder itself into a
native-looking answer.

## Analytics

The lake feeds an options + microstructure library, reachable three ways — CLI, MCP tool, or
plain Python over a `Catalog`. Nine runnable scripts in [`examples/`](examples/):

```python
from crocodile.crypto.analytics.funding import funding_apr
from crocodile.crypto.analytics.volsurface import iv_surface
from crocodile.core.store.catalog import Catalog

catalog = Catalog(data_dir="data")
apr     = funding_apr(catalog, "BTCUSDT", from_ns, to_ns)  # Polars DataFrame
surface = iv_surface(catalog, "BTC", at_ns, rate=0.0)      # strike × expiry × IV
```

Symbols here are the venue's own spelling, as stored. The canonical `source:RAW` form the CLI
reads to work out which market you mean is not what the lake's `symbol` column holds, so
passing `binance:BTCUSDT` to a reader matches nothing and returns an empty frame rather than
raising.

The full set spans OFI, slippage, whale alerts, term structure, vol skew, risk reversal,
spot–perp / spot–future basis, open interest, and an L2/DeFi-risk family (sequencer latency,
peg deviation, lending stress, MEV-sandwich detection). Each reads the same normalized
records — native venue or ccxt, it can't tell the difference.

## For agents (MCP)

```bash
crocodile mcp --data-dir data     # Model Context Protocol server over stdio
```

Every tool is read-only and deterministic — answers come from the lake and the chain, never
the model's imagination. Tools cover market-data reads (bounded DuckDB SQL, on-chain prices),
catalog discovery, and the analytics library. Because the tool list is generated from the
same registry as the CLI, an agent cannot call a capability the shell doesn't have, and each
input schema is derived from the capability's parameter type rather than written out a second
time. Works with Claude, Cursor, or anything that speaks MCP.

## REST API

`crocodile api` serves the same lake over FastAPI at `/api/v1/*` — ops and catalog discovery,
a bounded read-only `POST /query`, the derivatives and microstructure analytics, and a
payment-gated demo route over the x402 protocol.

## Base L2

`BaseOnchainConnector` reads Uniswap V3 and Aerodrome swap/reserve events from Base RPC logs
and emits the same record types as the CEX connectors, so a cross-venue query is one SQL
statement instead of two codebases. Start with
[docs/base_quickstart.md](docs/base_quickstart.md); there is a Streamlit dashboard and a
Farcaster frame server under [`examples/`](examples/). Public data needs no keys; on-chain
reads use a default Base RPC you can override.

## Performance

![Benchmark: 3.54M records/s normalize, 17M rows/s resample, 458k records/s replay, 20.2x compression, 0.36 ms count over 500k rows](docs/media/benchmark.svg)

A network-free, synthetic-data benchmark of the offline path (normalize → store → query →
resample → replay), fixed seed, every number written by the run itself:
[`benchmarks/RESULTS.md`](benchmarks/RESULTS.md). Reproduce with
`uv run python benchmarks/bench.py`.

There is no GUI in this repository, and therefore no frame rate to quote.

## Tests

```bash
uv sync --all-extras
pytest tests/
```

2,682 test functions across 240 files, **3,941 cases** once parametrisation expands them.
Under that headline: a local mock-RPC server for degraded-network E2E runs (`tests/e2e/`),
adversarial payload suites, a regression file seeded by real exchange API anomalies, and
`tests/conformance/`, which asserts the *arguments* this codebase runs on rather than its
behaviour. CI-friendly — Matplotlib runs headless and BLAS thread caps keep imports fast on
Apple Silicon.

One of them is worth naming. `tests/replay/test_byte_identity.py` replays the same window in
two subprocesses under two different `PYTHONHASHSEED` values and compares raw stdout, because
the claim at the top of this page is about bytes and a same-process repeat would not notice a
silent dependency on `set` iteration order. It is the gate under the hero image.

Neither `mypy --strict` nor Ruff gates the whole of `src/`, and saying otherwise was the more
expensive kind of wrong — it described a gate nobody would re-derive. What actually holds:

- **Zero Ruff findings** in `core`, `contrib`, `capabilities`, `surfaces` and the two
  deprecation shims — every line the merge wrote.
- **A ratchet, currently 202,** over `crypto` and `equity`, the two inherited trees. It only
  ever falls; `tests/conformance/test_phase1_exit.py` fails if it rises, and a third test
  fails if any file lands under neither gate. Deleting the desktop layer took it from 222 to
  202 on its own — a gate that tightens when you remove code needs nobody to remember to
  tighten it.
- **`mypy --strict` clean** across 229 files, with `crocodile.crypto.*` and
  `crocodile.equity.*` exempted in `pyproject.toml` until they are migrated.

There is also a conformance gate that fails when a sentence in the source states a number the
code no longer holds. It is the reason the figures on this page can be trusted: they are not
maintained by hand.

## What it isn't

- **Not a trading bot.** There is no order-execution path, by design.
- **Not a hosted service.** Everything runs on your machine, against your lake.
- **Not a licensed data archive.** It ingests what public endpoints return.
- **Not a chart.** Crocodile has no GUI.
  [FlowMap](https://github.com/nazmiefearmutcu/flowmap) is a separate dual-market order-flow
  visualizer that reads a lake like this one.
- **Not magic.** Options analytics need options data — point `iv-surface` at a lake with
  Deribit snapshots in it, not an empty directory.

## Raw market capture and offline replay

Set `CROCODILE_RAW_CAPTURE=true` to preserve native Binance or Deribit market frames,
public instrument metadata, REST depth response bytes and book application decisions:

```powershell
$env:CROCODILE_RAW_CAPTURE = "true"
crocodile collect --asset-class crypto --sources binance --symbols BTCUSDT --channels book_delta --duration-seconds 60
```

Captures are written beneath the configured lake at `raw-capture/<capture_id>/`.
Authentication payloads, API headers and refresh tokens are excluded. Unknown or malformed
Deribit control messages retain only metadata, because that socket carries credentials.
The raw byte queue includes encoding and in-flight writes, defaults to 32 MiB, and can be
lowered with `CROCODILE_RAW_CAPTURE_QUEUE_BYTES`. Records are limited to 16 MiB; segments
target 256 MiB. Admission, written and fsynced counters differ: the default fsync interval
is at most one second, with explicit durable-lag reporting. An admitted unsynced tail may
be lost on process crash. Queue, disk or fsync failures end required capture and return an
error after cleanup. This supports process-crash recovery; power-loss guarantees depend
on the filesystem and hardware.

Raw replay is a separate library API. The existing normalized `replay` command keeps its
Parquet semantics. Replay constructs no live connector and preserves source timestamps:

```python
from pathlib import Path
from crocodile.core.capture import CaptureReader, RawReplay
from crocodile.crypto.exchanges.binance.replay import BinanceReplayDecoder

reader = CaptureReader(Path("data/raw-capture/<capture_id>"))
replay = RawReplay(reader, {"binance-json": BinanceReplayDecoder()})
records = list(replay.records())  # consume the iterator incrementally for large captures
```

`recover_active_tail=True` permits only an incomplete final record in the active partial
segment; complete-record CRC errors and sealed segment mismatches remain integrity errors.
`await replay.run(sink, pacing="original")` needs actual monotonic receive metadata. Legacy
byte-only transports cannot provide that pacing; `pacing="fast"` works without inventing it.
`collect-market` uses unified CCXT symbols and rejects raw capture; use native symbols
through `collect` for these feeds. Missing contract units remain missing in the registry.

## Contributing

PRs welcome. Skim [`CHANGELOG.md`](CHANGELOG.md) for direction, keep the `mypy --strict` and
Ruff gates described above green — new code goes in the zero-findings half, and the ratchet
over the inherited half never rises — and make sure the E2E and adversarial suites pass
before opening one.

Apache-2.0 — see [LICENSE](LICENSE).
