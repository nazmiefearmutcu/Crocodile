# Crocodile 0.3.3

Crocodile 0.3.3 adds a market reference layer, `crocodile.core.markets`, for 172 operating exchanges in 129 countries and territories. For each exchange it knows the ISO 10383 operating MIC, country, time zone, trading currency and how prices are quoted. Prices quoted in a minor unit, such as London pence, Johannesburg cents and Tel Aviv agorot, convert exactly to the major unit. Listings have one canonical form, `SYMBOL@MIC` (`THYAO@XIST`); bare US tickers keep their current form.

Trading calendars share one protocol. 68 exchanges use `exchange_calendars` 4.13.2, which knows their holidays. Four of these use the calendar of a sibling venue with the same trading days and close: Shenzhen and Beijing use Shanghai, CSE uses Toronto and NGM uses Stockholm. The remaining exchanges use a weekday fallback that knows the weekend but not holidays. `quality_for(d)` tells, date by date, which kind of calendar answered. Weekends of markets that trade Sunday to Thursday are taken from retrieved sources: Qatar, Kuwait, Bahrain, Jordan, Chittagong, Palestine and Khartoum. Tehran trades Saturday to Wednesday.

Markets under comprehensive sanctions programmes (Russia, Belarus, Iran, Syria, North Korea) are closed by default. Set `CROCODILE_ALLOW_SANCTIONED_MARKETS` to the ISO country codes to open them.

This release ships reference data and calendars only: it does not yet collect prices from the new exchanges. The universal collectors are the next package. They will add Yahoo chart history, TradingView end-of-day snapshots and official listing directories, and they build on this layer. Existing US equity and crypto collection is unchanged.

Known limits:

- **Calendar end dates.** The library calendars for Shanghai, Bombay and Singapore end on 2026-12-31. Shenzhen and Beijing share Shanghai's calendar, so they end then too. Calendars without a fixed end reach one year past import. After those dates, the weekday fallback answers.
- **Unsourced weekends.** 81 exchanges still assume a Saturday-Sunday weekend without a source, including Oman, Iraq, Algeria, Libya, Maldives, Syria and Nepal.
- **NSE India.** It uses the weekday fallback until identical BSE holidays are confirmed.
- **pandas.** It is locked at 3.0.3. With pandas 3.1.x, exchange_calendars 4.13.2 cannot build four calendars: Athens, Belgrade, Bucharest and Cyprus.

See [global market coverage](global-markets.md) for the full list of exchanges.
