# Raw Market Capture And Offline Replay

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
targets one second, with current/peak durable-lag and overrun reporting. Disk stalls
can exceed this interval. An admitted unsynced tail may
be lost on process crash. Queue, disk or fsync failures end required capture and return an
error after cleanup. This supports process-crash recovery; power-loss guarantees depend
on the filesystem and hardware.

Raw replay is a separate library API. The existing normalized `replay` command keeps its
Parquet semantics. Replay constructs no live connector and preserves source timestamps:

```python
from pathlib import Path
from crocodile.core.capture import CaptureReader, RawReplay
from crocodile.crypto.exchanges.binance.replay import BinanceReplayDecoder, BinanceSbeReplayDecoder
from crocodile.crypto.exchanges.deribit.replay import DeribitReplayDecoder

reader = CaptureReader(Path("data/raw-capture/<capture_id>"))
replay = RawReplay(reader, {
    "binance-json": BinanceReplayDecoder(),
    "binance-sbe": BinanceSbeReplayDecoder(),
    "deribit-json": DeribitReplayDecoder(),
})
records = list(replay.records())  # consume the iterator incrementally for large captures
```

`recover_active_tail=True` permits only an incomplete final record in the active partial
segment; complete-record CRC errors and sealed segment mismatches remain integrity errors.
`await replay.run(sink, pacing="original")` needs actual monotonic receive metadata. Legacy
byte-only transports cannot provide that pacing; `pacing="fast"` works without inventing it.
`collect-market` uses unified CCXT symbols and rejects raw capture; use native symbols
through `collect` for these feeds. Missing contract units remain missing in the registry.

For Binance Spot SBE (published depth interval 20 ms), set
`CROCODILE_BINANCE_WIRE_FORMAT=sbe`, `CROCODILE_BINANCE_MARKET=spot` and
`CROCODILE_BINANCE_SBE_API_KEY` locally. The key must be registered as Ed25519 on Binance;
only its API key string is sent in `X-MBX-APIKEY`. Private keys and trading permissions
are unnecessary for this feed. JSON remains the default. `book_delta` and `book_snapshot`
both request the full-depth diff stream, anchored by REST; top-20 snapshots have a separate
50 ms scope and cannot reset that book. The 20 ms publication interval is not a guarantee
of 50 new changes per second on every symbol. REST seed coverage is limited to the chosen
snapshot depth (default 1000, maximum 5000 per side), with additional changed levels learned
afterwards. Source identity remains `binance-spot`, including in SBE captures.

For Deribit raw, use native symbols such as `BTC-PERPETUAL` with `collect`, set
`CROCODILE_DERIBIT_INTERVAL=raw` and configure `CROCODILE_DERIBIT_CLIENT_ID` /
`CROCODILE_DERIBIT_CLIENT_SECRET` locally. Raw access requires an authorized account;
there is no silent fallback when authorization fails. The default remains anonymous
`100ms`. Raw book updates use 1 ms aggregation and are L2 price levels, without L3
order identities. Contract amount units come from the instrument registry; an absent
unit is not inferred. `collect-market` rejects explicit raw mode before querying a universe.
Each new socket authenticates afresh. Request correlation, monotonic token refresh and
application heartbeats run independently of market processing. A book sequence gap
invalidates only that symbol; unsubscribe/subscribe acknowledgements and a fresh snapshot
are required before accepting its deltas again. Explicit SBE/raw failures are surfaced
even when raw capture is disabled. Replay requires no API credentials.

To benchmark a capture offline, run
`python -m scripts.benchmark_raw_capture data/raw-capture/<capture_id> --repeats 3`
from the repository with the package installed. The report counts new depth changes
separately from BBO and duplicate frames. Replay throughput is not live receive cadence;
the manifest contains seal-time queue/durability telemetry.

Created By Nazmi Efe Armutcu (Aylin)
