# Markets without a live source (Crocodile 0.5.0)

36 markets of the allow-list are open by default and have no live source. Each was looked at twice on 2026-11-12 with the same access the engine uses: a plain keyless request, a generic User-Agent, no cookie, no Referer, no redirect followed. This page records what answered, so that the next attempt starts from evidence and not from the home page.

"No source" here means no keyless source that answers for the whole market in one request and names its securities by ticker. It does not mean the exchange publishes nothing.

| MIC | Exchange | What answered |
|---|---|---|
| APXL | Sydney Stock Exchange | The prices page is served, with no security rows: nothing is quoted. |
| AQSE | Aquis Stock Exchange | A board exists (bid, ask and previous close for 66 securities), but only at a URL that embeds the site's build id, which changes on every deploy. Not shipped. |
| AQXE | Aquis Exchange | The site answers 429 behind a security checkpoint. The exchange sells this data through vendors. |
| BSEX | Baku Stock Exchange | The equity table names securities by ISIN and issuer; it has no ticker column. |
| GSXL | Gibraltar Stock Exchange | The domain no longer resolves; the exchange is reported closed since 2023. |
| IEXG | Investors Exchange | No keyless price board. US listings are read through the US chains. |
| NSXB | Bendigo Stock Exchange | The exchange closed in 2012; the domain now serves an unrelated site. |
| SIMV | SIM Venture Securities Exchange | Its market licence was cancelled in 2018; the site has no price pages. |
| TRPX | MERJ Exchange | Prices are served only after login. |
| UKEX | Ukrainian Exchange | Bot challenge (403) on every path. |
| XALG | Algiers Stock Exchange | The session board's address carries a session number that changes every session, so no fixed request reaches it. |
| XARM | Armenia Securities Exchange | Bot challenge (403) on every path. |
| XBAB | Barbados Stock Exchange | The daily summary is published only as a spreadsheet and a PDF. |
| XBER | Boerse Berlin | No price board on its own domains; floor trading ended on 2025-12-30 and the exchange is merging into Tradegate BSX. |
| XBEY | Beirut Stock Exchange | The daily bulletin is a readable table, but it names securities by company name with no ticker. |
| XBNV | Bolsa Nacional de Valores (Costa Rica) | Prices are shown only inside embedded dashboards. |
| XBOL | Bolsa Boliviana de Valores | The host could not be reached. |
| XCHG | Chittagong Stock Exchange | 503 on every path. |
| XDAR | Dar es Salaam Stock Exchange | The whole board is in the page source, as markup that is not a table. The board engine reads tables only. |
| XGSE | Georgian Stock Exchange | Trades are listed as markup that is not a table; there is no board. |
| XGUA | Bolsa de Valores de Guayaquil | The price table names issuers by company name only. |
| XJAM | Jamaica Stock Exchange | Request blocked (403) on every path. |
| XKHA | Khartoum Stock Exchange | The domain no longer resolves. |
| XLSM | Libyan Stock Market | The connection times out. |
| XMAL | Malta Stock Exchange | The latest-prices block is markup that is not a table. |
| XMAU | Stock Exchange of Mauritius | The board is fresh and keyless, but it names securities by company name only. |
| XMNT | Bolsa de Valores de Montevideo | Only the session's executed trades are listed, mostly bonds named by ISIN; there is no equity board. |
| XMOL | Moldova Stock Exchange | The connection fails. |
| XNAM | Namibian Stock Exchange | Per-security prices are published only in a weekly spreadsheet. |
| XPOM | PNGX Markets | One CSV file per security answers; no single request returns the board. |
| XQUI | Bolsa de Valores de Quito | Daily prices are published only as a spreadsheet. |
| XSAT | Spotlight Stock Market | The company list has names and internal ids, no prices; prices are per security. |
| XSWA | Eswatini Stock Exchange | Ten tickers scroll in a marquee that is not a table. |
| XTRN | Trinidad and Tobago Stock Exchange | Firewall page (403). |
| XULA | Mongolian Stock Exchange | The price feed sits behind the site's bot detection. |
| XVPA | Bolsa de Valores de Asuncion | Its boards name securities by issuer, not by ticker. |

## What would close which gap

- **A table-free reader.** Dar es Salaam, Malta, Georgia and Eswatini publish their figures in the page source as markup that is not a `<table>`. A row pattern in the board engine would reach them.
- **A spreadsheet reader.** Barbados, Namibia and Quito publish spreadsheets. The engine reads JSON, CSV, XML and HTML only.
- **A per-security source.** PNGX answers one CSV per security, and Spotlight a per-security history. Each would be a dedicated source, like the ones that already exist for Xetra or Singapore.
- **A ticker.** Baku, Beirut, Guayaquil, Mauritius and Asuncion publish boards keyed by company name. A listing is identified by the exchange's local symbol, so these stay out until a source states one.
- **Nothing.** Walled sites stay walled: a bot challenge, a firewall page or a login is recorded and not worked around. Gibraltar, Bendigo, SIM Venture and Khartoum have ceased trading or are unreachable at their registered address.
