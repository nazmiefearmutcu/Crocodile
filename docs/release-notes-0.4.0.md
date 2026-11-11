# Crocodile 0.4.0

Crocodile 0.4.0 collects equity prices beyond the United States, and it removes the names that 0.3.x kept alive for one minor version. It builds on the 0.3.3 market reference layer: every listing has one symbol, `SYMBOL@MIC` (`THYAO@XIST`), one currency and one exchange calendar.

## What it collects

- **Listings.** `universe-sync` builds a listing master at `<data_dir>/reference/listings.sqlite`. It draws on the official directories of Nasdaq Trader, ASX, NSE India and TWSE, and on TradingView's screener for the other markets.
- **Daily prices.** `yahoo_chart` gives daily history with dividends and splits. TradingView gives an end-of-day snapshot for each session. When two sources hold the same session, a source order per exchange decides which bar a read returns.
- **Intraday prices.** `yahoo_chart` collects intraday bars within Yahoo's windows, for listings you select.
- **Commands.**
  - `collect` and `backfill` route to these sources with `--sources universal`.
  - `schedule` runs collection after each exchange closes, using that exchange's calendar.
  - `markets`, `universe`, `collect` and `backfill` take `--mic` and `--country`.
  - A new `calendar` capability returns exchange sessions.

Prices keep the unit the exchange quotes in. A row with `price_scale = 2` (pence, ZAc, ILA) holds 100 times the major unit. Records gain nullable `currency`, `price_scale` and `adjustment` fields. Lakes written by 0.3.x read them as `NULL`, which means unknown, never USD.

## Breaking changes

- **`crypcodile` and `stockodile` are gone.** The import packages and console scripts, deprecated since 0.3.0, are removed. Import from `crocodile`, and run `crocodile`.
- **Only `CROCODILE_*` settings are read.** A `CRYPCODILE_*` or `STOCKODILE_*` setting is now ignored.
- **What does not change.** Lakes are unaffected. Readers still accept the pre-0.3 `exchange=` and `provider=` partition keys, and `migrate_lake` renames them. `CrypcodileClient`, `StockodileClient` and the `STOCKODILE_PROXIES` and `STOCKODILE_API_KEYS` settings keep their names.

## Fixed

- The legacy `yahoo` provider dated daily bars one day early for exchanges east of UTC.
- Yahoo fundamentals no longer claim USD.
- Google Finance resolves symbols against the listing's own market.
- A scheduled date that keeps failing is recorded as a gap instead of holding its task.

## Measured on 2026-11-11

A live run covered 12 countries and used 74 of its 250 allowed requests, with no refusals. Listings and Yahoo daily bars were both found for Turkey, the United Kingdom, Japan, India, Brazil, Vietnam, Australia and the United States.

The gaps:

- **Germany:** the sample symbols returned 404 on Yahoo.
- **Nigeria and Kenya:** no daily bars were returned.
- **South Africa:** no universe source is bound for the Johannesburg exchange, so the listing sync returned no listings.
- **Listing bound:** the universe reached its 1000-listing bound in Japan, Brazil, Vietnam, India and Australia, so those listing counts are incomplete.

## Known limits

- **Terms of use.** Yahoo and TradingView are grey sources and are on by default. Yahoo's terms prohibit automated access. TradingView's terms have not been reviewed. Redistribution is the user's responsibility.
- **Minor-unit quotes.** Pence and cents quotes are verified by tests only. No measured listing was quoted in a minor unit.
- **Mixed adjustment.** Daily reads can mix split-adjusted Yahoo rows with unadjusted TradingView rows for one listing when Yahoo lacks a session. A split can then show as a level break.
- **Calendar gaps.** Bars were seen on two dates the calendar lists as closed. Istanbul was closed on 2023-02-08 after the earthquake. India held a special Saturday budget session on 2025-02-01. Exchanges without a library calendar know their weekend but not their holidays.
- **Yahoo request volume.** Yahoo needs one request per listing, so a whole-universe backfill takes a long time. It resumes per listing.
- **`--data-dir` scope.**
  - `collect` and `backfill` write under `CROCODILE_DATA_DIR`.
  - `universe-sync` and `schedule` write under `--data-dir`.
  - `universe` and `markets` read from `--data-dir`.
- **Sanctions.** Exchanges in Russia, Belarus, Iran, Syria and North Korea stay closed unless `CROCODILE_ALLOW_SANCTIONED_MARKETS` opens them.

## Verification

Verified on Windows with UTF-8 mode. The suite ran 6,144 tests in 319 files with no new failure against the pre-branch baseline.

See the [CHANGELOG](../CHANGELOG.md) for the full list and [global market coverage](global-markets.md) for every exchange.
