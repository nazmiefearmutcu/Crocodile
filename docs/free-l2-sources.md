# Native public order-book sources

The native factory now supports `bitfinex`, `kraken`, `dydx`, `coinbase`, and
`bybit`, alongside the existing Binance and Deribit capture adapters. These
sources retain their venue and product identity. Visible L3 orders summed into
L2 are marked `Provenance.DERIVED`, basis `visible_l3_sum`; they are exact within
the visible subscription scope, which excludes hidden liquidity.

| Source and mode | Native symbols | Access and scope | Continuity |
| --- | --- | --- | --- |
| Bitfinex R0/F0/250 | `tBTCUSD`, `tETHUSD` | Public; up to 250 visible orders per side; realtime | Socket-wide SEQ_ALL including heartbeats/checksums; signed top-25-order CRC32 |
| Kraken `book` | `BTC/USD`, `ETH/USD` | Public L2; depth 10/25/100/500/1000 price levels | Top-10-price-level unsigned CRC32; no source sequence is invented |
| Kraken `level3` | `BTC/USD`, `ETH/USD` | API key with WebSocket permission; visible L3; depth 10/100/1000 price levels | Decimal order queues, prune after the complete update, then top-10-level CRC32 |
| Coinbase `exchange` | `BTC-USD`, `ETH-USD` | Default Exchange protocol; public spot L2 | Initial snapshot required in captured runs; Exchange L2 carries no book sequence |
| Coinbase `advanced` | `BTC-USD`, `ETH-USD` | Explicit public Advanced endpoint; level2, ticker/trades and heartbeats | Every envelope advances the connection counter; a gap invalidates all books |
| dYdX v4 unbatched | `BTC-USD`, `ETH-USD` | Public indexer L2; `batched:false` | Mandatory `subscribed` snapshot; subscription `message_id`, never engine sequence |
| Bybit `fast` | `BTCUSDT` | Default linear/depth50; spot/inverse also supported; option defaults depth25 | WS snapshot/reset; `u` is incremental but is not assumed consecutive |
| Bybit `full` | `BTCUSDT` | Explicit spot/linear/inverse mode; no options; delta-only stream | Bounded WS buffer + recorded REST anchor matching both `seq` and `u`; subsequent `u+1` |

Bitfinex, Kraken and dYdX native adapters currently accept book channels only:
`book_delta` and `book_snapshot`. Bybit and Coinbase retain their existing trade
and ticker channels. Kraken L3 requires credentials but does not imply a paid
data product; account eligibility and permissions still apply. The current
implementation does not measure authenticated L3 access when credentials are
absent.

## Select a mode

Factory arguments apply only to the selected venue:

```python
from crocodile.core.config import Settings
from crocodile.core.sink.memory import MemorySink
from crocodile.crypto.exchanges.factory import make_connector
from crocodile.crypto.instruments.registry import InstrumentRegistry

sink, registry = MemorySink(), InstrumentRegistry()
connector = make_connector(
    "coinbase", ["BTC-USD"], ["book_delta", "book_snapshot"], sink, registry,
    settings=Settings.from_env(), mode="advanced",
)
connector.transport = connector.make_transport()
```

Use `mode="book", depth=10` or `mode="level3", depth=100` for Kraken; use
`category="linear", book_mode="full"` for Bybit. Fast Bybit supports depths
1/50/200/1000 for spot/linear/inverse, and 25/100 for options. Full mode uses its
own endpoint identity; it must not be mixed with a fast book.

Environment configuration uses `CROCODILE_COINBASE_MODE`,
`CROCODILE_BYBIT_BOOK_MODE`, `CROCODILE_BYBIT_BOOK_DEPTH`,
`CROCODILE_KRAKEN_MODE`, `CROCODILE_KRAKEN_DEPTH`,
`CROCODILE_KRAKEN_API_KEY`, and `CROCODILE_KRAKEN_API_SECRET`. Kraken secret is
the base64 API secret. Settings redact credentials. Constructors validate modes,
depths and required credentials before opening a network connection. Each Kraken
L3 reconnect obtains a new token through the signed private REST endpoint; the
token response and outgoing authentication/subscription payload are not captured.

`list_instruments()` supplies the native public product catalogue. Coinbase uses
the selected protocol's correct public REST endpoint and Advanced spot
pagination. Kraken maps the REST catalogue's XBT spelling to v2 BTC symbols.
Subscription rejection remains visible for products unavailable on a selected
endpoint.

## Capture and offline replay

The reusable integration functions are
`crocodile.core.capture.native.stream_context(connector, stream_id)` and
`replay_decoders()`. Context stores the public source/market, native symbols,
endpoint mode, decoder version and optional wire schema; it stores no credentials.

```python
import asyncio
from pathlib import Path
from crocodile.core.capture.native import stream_context, replay_decoders
from crocodile.core.capture.reader import CaptureReader
from crocodile.core.capture.replay import RawReplay
from crocodile.core.capture.schema import CaptureContext
from crocodile.core.capture.writer import CaptureWriter
from crocodile.crypto.client.collect import collect

async def record_for_30_seconds(connector, sink):
    directory = Path("recordings/example")  # must be empty
    connector.capture_stream_id = "s1"
    capture = await CaptureWriter.create(directory, CaptureContext(
        capture_id="example", streams=(stream_context(connector, "s1"),)))
    try:
        async with asyncio.timeout(30):
            await collect([connector], sink, capture=capture)
    except TimeoutError:
        pass  # collect has closed the sink and sealed the journal
    return list(RawReplay(CaptureReader(directory), replay_decoders()).records())
```

Raw ingress is captured before book processing. Replay follows recorded
application decisions and original receive timestamps instead of sorting source
clocks. The Bitfinex acknowledgement is reconstructed from a strict whitelist of
public channel mapping fields; arbitrary acknowledgement objects are never
retained. Recursive token/JWT/API-key/signature/auth fields are classified as
controls, outside the market journal and DLQ. Decoder source, market, mode and
symbols must match the recorded context.

Canonical book records intentionally omit connection/subscription counters.
The raw journal retains the actual counters. A continuity/checksum failure
records a gap decision and forces a reconnect/new snapshot; the gate stops
applying deltas until recovery. Unknown L3 orders and malformed protocol fields
fail visibly. A selected required native source or capture failure propagates
through collection after cleanup.

Bybit full bootstrap is limited to 8 MiB/4096 buffered frames, eight REST attempts
and a configurable positive timeout of at most 30 seconds. It waits for a future REST anchor to
appear on WS rather than continually replacing it. Bybit resets reconnect and
obtain another anchor. Empty anchors produce an empty book and telemetry
`book_states[symbol]="no_book"`. Decreasing full cross-sequences are discarded.

## Coverage and measurement limits

`connector.telemetry()` separates changed `BookDelta` revisions, snapshots,
gaps and unchanged/other frames. Trade/ticker messages are not changed depth
revisions. Raw journal `CaptureStats.received_frames` is the authoritative
admitted-frame count, including frames buffered before an anchor. Telemetry's
processing counts can differ during bootstrap or failure.

`coverage` reports subscription scope and documented cadence. A 20 ms published
interval does not guarantee fifty unique changed books per second; idle/duplicate
messages and heartbeats add no depth events. No fixed cadence is claimed for
Coinbase or dYdX. Bybit full's documented initial 10,000-price-level limit is
reported separately from telemetry `known_book_levels`: the live server may
return a different number. Unchanged levels outside the actual REST snapshot
remain unknown even when the stream is called full.

`quantity_unit` is `base_asset` for the public spot/linear books and dYdX;
Bybit inverse is `quote_contracts` and options retain `native_contracts`. Quantities
are not silently converted across products. Consumers must inspect this field
and the native instrument before combining sources or calculating notionals.

Decimal parsing and aggregation precede conversion to the existing canonical
float schema. Exact raw decimal values remain in the journal. The feature does
not reconstruct hidden orders, missing matching-engine events, or other venues'
history. Raw durability is measured separately from queue admission: fsync at
close does not establish a one-second durability bound during a contended run.

## Official protocol references

- [Bitfinex raw books](https://docs.bitfinex.com/reference/ws-public-raw-books),
  [flags](https://docs.bitfinex.com/docs/ws-general),
  [checksums](https://docs.bitfinex.com/docs/ws-websocket-checksum).
- [Kraken L3 schema](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/level3),
  [L3 checksum](https://docs.kraken.com/exchange/guides/websockets/l3-checksum-v2),
  [WebSocket token](https://docs.kraken.com/api-reference/trading/get-websockets-token).
- [Coinbase Advanced level2](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/websocket/level2),
  [current AsyncAPI counter and trade semantics](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/advanced-trade-asyncapi.json),
  [public products](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/public/list-public-products).
- [dYdX subscriptions](https://docs.dydx.xyz/indexer-client/websockets).
- [Bybit fast book](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook),
  [full synchronization](https://bybit-exchange.github.io/docs/v5/websocket/public/full-ob),
  [full REST anchor](https://bybit-exchange.github.io/docs/v5/market/full-ob).
