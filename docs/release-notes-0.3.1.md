# Crocodile 0.3.1

This release adds native order-book sources with free public access, raw capture
and offline replay, OKX archive tools, and synthetic L2 estimates with explicit
limits. Deprecated `crypcodile` and `stockodile` imports and commands remain
available until 0.4. Python 3.12 or later is required.

## Native order-book sources

Bitfinex, Kraken, and dYdX v4 are supported directly. Coinbase Advanced is an
explicit alternative to the default Exchange mode. Bybit full mode starts from
a REST snapshot; fast remains the default. The Bitfinex, Kraken, and dYdX
adapters currently accept `book_delta` and `book_snapshot` channels.

Raw captures retain the source, market, native symbol, endpoint, and decoder
identity. These endpoint names are the actual capture identities:

| Source | Mode and endpoint identity | Coverage |
| --- | --- | --- |
| `binance-spot` | `binance-spot-sbe-ws` | Spot SBE depth-diff with a REST snapshot anchor |
| `deribit` | `deribit-raw-ws` | Authorized raw L2; no L3 order IDs |
| `bitfinex` | `bitfinex-r0-f0-250` | Up to 250 visible orders per side |
| `kraken` | `kraken-book-{depth}` / `kraken-level3-{depth}` | Public L2 or authenticated visible L3 |
| `coinbase` | `coinbase-exchange` / `coinbase-advanced` | Spot L2 from the selected protocol |
| `dydx` | `dydx-v4-unbatched` | Public v4 indexer with `batched:false` |
| `bybit` | `bybit-{category}-{book_mode}-{depth}` | Spot/linear/inverse; fast also supports options |

L2 sums of visible L3 orders are labeled `derived` and `visible_l3_sum`. Hidden
liquidity is excluded. A failed protocol-counter or checksum check requires a
new connection or snapshot. The adapters do not invent sequence numbers that
the source does not provide. Decimal parsing and aggregation happen before
conversion to the existing float record schema; values outside its range are
rejected before publication. Raw captures preserve the original numeric text.

Spot/linear and dYdX quantities use `base_asset`; Bybit inverse uses
`quote_contracts`, and Bybit options uses `native_contracts`. Account for these
units and instrument metadata when combining sources. See
[native book usage](free-l2-sources.md) for settings and examples.

## Raw capture and replay

With `CROCODILE_RAW_CAPTURE=true`, `collect` records incoming market frames,
receive clocks, public instrument metadata, REST snapshot responses, and book
application decisions for supported native sources. Market journals are
separate from control messages: API keys, authentication payloads, and tokens
are excluded. If required capture fails because of a queue or disk error,
collection is cleaned up and the error reaches the caller.

The default queue budget is 32 MiB, the single-record limit is 16 MiB, and the
segment target is 256 MiB. CRCs and sealed-segment integrity are checked.
Crash recovery can accept an incomplete final record in the active segment;
it does not accept corrupt completed records.

`CaptureReader` and `RawReplay` reproduce recorded decisions without a live
connection or API credentials. `pacing="original"` requires a recorded monotonic
receive clock. The existing normalized Parquet `replay` command keeps its
semantics. `collect-market` does not accept raw capture; use `collect` with
native symbols for these sources. `scripts/benchmark_raw_capture.py` reports
replay speed and frame classes and is run from the repository.

## Binance SBE and authenticated feeds

Binance SBE is opt-in through `CROCODILE_BINANCE_WIRE_FORMAT=sbe`,
`CROCODILE_BINANCE_MARKET=spot`, and `CROCODILE_BINANCE_SBE_API_KEY`. Supply the
API key text registered for an Ed25519 account; a private key is not required.
JSON remains the default. SBE retains the `binance-spot` source identity. The
full depth-diff book starts from REST; separate top-20 snapshots do not reset it.

Deribit raw requires `CROCODILE_DERIBIT_INTERVAL=raw`,
`CROCODILE_DERIBIT_CLIENT_ID`, and `CROCODILE_DERIBIT_CLIENT_SECRET`, plus an
account authorized for the raw channel. Authorization errors fail visibly;
there is no silent fallback. Anonymous `100ms` remains the default. New sockets
authenticate again. Request matching, token renewal, and application heartbeats
run separately from market processing. A sequence gap invalidates only the
affected symbol; deltas resume after an acknowledged resubscription and a new
snapshot. Kraken L3 also requires a WebSocket-authorized API key and base64
secret.

## OKX archives and synthetic estimates

`scripts/archive_l2.py` lists the public OKX catalog and downloads only an
explicitly selected object. Downloads have a byte limit, ETag/Range resume
support, and a SHA-256 manifest. Supported 400/5000-level tar/NDJSON files are
read without extraction; schema, initial snapshot, and source-time ordering are
audited. Consume the iterator to completion to finish the audit. A time jump
can indicate a coverage gap, but does not prove how many exchange events were
lost. Legacy CSV, Bybit archive candidates, and research datasets need separate
audits.

`scripts/reconstruct_l2.py` and `solve_window` use priors and constraints on a
finite grid over an explicit time window to produce `synthetic` estimates.
Causal mode rejects observations available only in the future; retrospective
mode requires a separate `revision_id`. Results report iteration/time budgets,
residuals, solution status, and bound status. Feasible ranges depend on the
stated constraints; they are not statistical confidence intervals. An
`unconverged` result cannot be used as a valid solution. This method cannot
recover the exact missing event sequence, hidden orders, or actual add/cancel
flow. `source_event_count` remains unknown.

## Other changes and limits

The release also includes FINRA `short_volume` / `short_interest` backfill, ATS
weekly-summary transforms, and SEC fails-to-deliver download and analytics
helpers. These are delayed disclosure datasets, separate from live order books.
Short-volume and short-interest queries bind symbol and time filters as SQL
parameters and reject non-integer timestamps. Dark-pool participant shares are
calculated within the matching week and symbol.

Base installations can load the CLI and run offline lending/transfer helpers
without the optional on-chain dependencies. Pool operations that require Web3
provide a `crocodile[onchain]` installation hint when it is absent. On Windows,
set `PYTHONUTF8=1` if Python selects cp1254 for redirected table-query output.
This makes Python emit UTF-8; the table output contains Unicode characters.

Published intervals such as 20 ms do not guarantee 30 distinct book changes per
second for every symbol. Heartbeats, repeated frames, BBO updates, and trades
are counted separately from changed depth revisions. Offline replay throughput
is not live receive cadence. Unchanged levels beyond the initial snapshot's
coverage remain unknown. Bybit full's advertised 10,000-level limit is reported
separately from the levels actually returned.

Admitted, written, and fsynced records have separate counters. Storage stalls
can exceed the one-second fsync target, and a process crash can lose the tail
that has not yet been fsynced. Fsync at shutdown does not establish a
one-second durability bound throughout the run. Power-loss behavior depends on
the filesystem and hardware. These notes make no new measurement claim about
live cadence or authenticated channel access.
