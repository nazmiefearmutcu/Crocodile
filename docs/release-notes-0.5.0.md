# Crocodile 0.5.0

Crocodile 0.5.0 gives equities a live side. Until now a listing outside the United States had daily bars and nothing else, while every crypto venue had trades, quotes and an order book. This release adds `crocodile.equity.universal.live`: one call that answers, for a listing of any covered exchange, what that listing is doing now, as deep as a public source shows it.

## What it reads

```python
import asyncio

from crocodile.equity.universal.live.feed import LiveFeed, listing_of
from crocodile.equity.universal.live.model import snapshot_records


async def main() -> None:
    feed = LiveFeed()
    snapshot = await feed.poll(listing_of("2330@XTAI"))
    print(snapshot.tier, snapshot.last, snapshot.bids[:2], snapshot.asks[:2])
    records = snapshot_records(snapshot)  # BookSnapshot, Quote, Trade
    await feed.close()


asyncio.run(main())
```

- **A snapshot** holds the last trade, the session's open, high, low and cumulative volume, the previous close, the resting bid and ask levels the publisher exposes, and its recent prints. Nothing is filled in: an absent number is `None`.
- **Three tiers.** `l2` is a ladder of two or more levels with sizes, `l1` is the best bid and ask, `last` is the last price and the session figures. The tier is measured on each snapshot, not promised by the source.
- **A chain per market.** `sources_for(mic)` orders a market's sources: deeper view first, the exchange's own service before an aggregator, real time before delayed. `LiveFeed` walks the chain and returns the first snapshot that carries a price.
- **Records.** `snapshot_records` builds a `BookSnapshot` from sized levels, a `Quote` when both sides of the top of book carry a size, and a `Trade` per published print. Their provenance is `polled_book_snapshot` and `polled_last_print`, both NATIVE. `volume_delta_trade` reports the trading between two polls of a publisher that states only a cumulative volume as one DERIVED trade (`volume_delta_print`).
- **Whole boards.** `LiveFeed.board(mic)` lists a market in one request where a source can enumerate it: 48 exchange boards described as data in `live/data/sites.json`, and TradingView's scanner for the markets its grammar covers.

## Futures, options, currencies, indices and bonds

A crypto venue lists perpetuals, dated futures and options beside its spot pairs. `crocodile.equity.universal.live.assets` reads the same kinds for the rest of the world, from TradingView's scanner:

```python
from crocodile.equity.universal.live.assets import AssetFeed, AssetKind, asset_records

feed = AssetFeed()
contracts = await feed.board(AssetKind.FUTURE, "EUREX", limit=50)   # most traded first
rows = await feed.quotes(["CME_MINI:ES1!", "FX_IDC:EURUSD", "TVC:US10Y", "BIST:XU100"])
chain = await feed.option_chain("NASDAQ:AAPL")                      # OptionsChain records
series = await feed.option_series("SP:SPX")                         # 62 series, 31,016 contracts
near = await feed.option_chain("SP:SPX", expiry=series[0].expiry, root=series[0].root)
bars = await feed.minute_bars(AssetKind.FUTURE, "CME_MINI:ES1!")
records = asset_records(rows[0])                                    # DerivativeTicker, OpenInterest
```

| Kind | Measured on 2026-11-12 | Records |
|---|---|---|
| Futures | 52,421 contracts on 52 derivatives venues | `DerivativeTicker`, `OpenInterest` |
| Options | a chain per underlying (Apple 3,396 contracts, Nifty 4,653, THYAO 94, S&P 500 31,016 in 62 series) | `OptionsChain` with bid, ask, implied volatility and greeks |
| Currencies | 4,000 pairs of the composite feed, with bid and ask | none (the pair stays on the quote) |
| Indices | 188 | `IndexValue` |
| Government bonds and yields | 1,163 | none |

`crocodile.equity.universal.live.chains` reads whole chains from the publishers themselves, one keyless request each: `PublishedChains().cboe("AAPL")` (Cboe's delayed file: sizes, last trade, volume, open interest, fifteen minutes behind), `.bybit("BTC")` and `.deribit("BTC")`. A `PublishedChain` lists its series and returns one series' records; a contract keeps its family, so `SPX` and `SPXW` are never merged.

An option scan is whole or it raises: the scanner states how many rows match, and an answer that carries fewer is refused. Their basis is `scanner_snapshot` (NATIVE). `ES1!` is the continuous front contract and `ESZ2026` a dated one, as the scanner spells them.

## Coverage

Measured on 2026-11-12, one listing per market ([full table](live-coverage.md)):

| | Markets |
|---|---|
| Multi-level order book | 10: Taiwan, Taipei Exchange, Shanghai, Shenzhen, Beijing, Korea, India NSE, Ho Chi Minh, Hanoi, Xetra |
| Top of book | 15: Singapore, Zurich, Sydney, Warsaw, Dubai, Qatar, Lima, Astana, Ghana, Bermuda, the Eastern Caribbean, Frankfurt floor, Tradegate, Cboe Europe, Cboe US listings (NSX Australia declares one too; its sample listing had no quote) |
| Last price and session figures | 105 |
| A board is bound but did not answer that poll | 1 (Cayman Islands) |
| Open by default, no keyless source | 36 |
| Closed by default (sanctions) | 5 |

Before this release 79 of the 172 markets had no source of any kind (75 of them open by default). 39 of those now have one.

## What does not change

- The daily pipeline of 0.4.0 (`universe-sync`, `collect`, `backfill`, `schedule`) and its lake are untouched. A live read returns records to the caller and writes nothing.
- No source sends a cookie, a Referer or an Origin, and none follows a redirect. A consent page, a bot challenge, a 401, a 403 or a geo block is a wall: the source rests for fifteen minutes and the chain moves on.
- Sanctioned markets stay closed unless `CROCODILE_ALLOW_SANCTIONED_MARKETS` opens them. Their chains are empty.

## Known limits

- **Sampled, not streamed.** A polled ladder misses the orders that rest and cancel between two polls, and it is as deep as the publisher shows: three to ten levels, never the full book. A polled tape misses prints between polls.
- **Delayed where the publisher says so.** Zurich, Sao Paulo and Toronto state fifteen minutes, Sydney twenty, and TradingView states a delay per row. The delay is carried on the snapshot.
- **Aggregators are grey sources.** `naver`, `groww`, `vps`, `tencent`, `tradingview` and `yahoo_chart` have usage class `internal`. Their terms were not reviewed further.
- **36 markets have no keyless source.** Some are behind a bot wall (Jamaica, Trinidad, Armenia, Ukraine's UX, Mongolia), some publish only spreadsheet or PDF files (Barbados, Namibia, Quito), some key their board by company name with no ticker (Mauritius, Guayaquil, Baku, Beirut), some could not be reached, and four have ceased trading (Gibraltar, Bendigo, SIM Venture, Khartoum). [Markets without a live source](live-coverage-gaps.md) records what answered for each.
- **ISIN-keyed services.** Xetra, the Frankfurt floor and Tradegate are keyed by ISIN. `LiveFeed.identify` asks the scanner for it once per listing.
- **Board specs age.** A board whose publisher changes its page yields no rows or a payload error, never rows from the wrong column. Each spec carries the date it was last seen answering.
- **Futures, options, currencies, indices and bonds have no order book.** The scanner publishes no resting size for them, so there is no ladder, no `Quote` and no tape: a currency pair's bid and ask are prices without sizes. A row carries the delay the scanner states (most futures venues are delayed). Option greeks and implied volatility are the scanner's model; option volume and open interest are not published. One-minute bars exist only where Yahoo spells the instrument: currency pairs, continuous contracts of the US venues and 20 verified indices. A price or yield that is not positive is read as no price. Options are listed for US shares and indices, NSE and Borsa Istanbul underlyings; the scanner listed none for the Xetra, London, Tokyo, Hong Kong, Eurex and CME underlyings probed on 2026-11-12. Corporate bonds are not covered.

## Verification

616 tests cover the live layer: every source parses a captured answer of its publisher, and the engine, the chains and the feed are tested with fakes. The clean-package gates hold: `mypy --strict` and `ruff` report nothing for `crocodile.equity.universal`. The live measurement and the smoke runs used the network; the test suite does not.

See the [CHANGELOG](../CHANGELOG.md) for the full list.
