# Global market coverage (Crocodile 0.3.3)

Generated from `src/crocodile/core/markets/data/exchanges.csv`. That file is produced by `scripts/build_markets_reference.py`; regenerate this page when it changes.

Crocodile 0.3.3 knows **172 operating exchanges in 129 countries and territories**. For each one it knows the ISO 10383 operating MIC, country, time zone, trading currency, quote scale, weekend and trading calendar. Market sanctions are also recorded.

**What this release does not do yet:** it does not collect prices from these exchanges. 0.3.3 ships the market reference layer (`crocodile.core.markets`). Through it, listing symbols such as `THYAO@XIST`, calendars and currency handling are the same everywhere. The universal collectors (Yahoo chart history, TradingView EOD snapshots, official listing directories) are the next package and build on this layer. Existing US equity and crypto collection is unchanged.

## Summary

- Library trading calendars (`exchange_calendars` 4.13.2, holidays known): **68** exchanges. Four of them reuse a calendar they share with a sibling venue: Shenzhen and Beijing use Shanghai, CSE uses Toronto and NGM uses Stockholm.
- Weekday-fallback calendars (weekend known, holidays unknown): **104** exchanges. On these, a missing weekday bar is recorded as `no_data`, not as a gap.
- Weekends taken from evidence (library, primary or secondary source): **91**. The other **81** assume Saturday-Sunday without a source.
- Research access tiers: A 4 · B 50 · C 10 · D 108.
  - A: official free machine-readable source.
  - B: keyless aggregator (Yahoo chart) answered.
  - C: documented free key.
  - D: no verified free route.
  - Tiers measure access friction, not legal permission. Every tier-B route is Yahoo, whose terms forbid automated access.
- Default-closed (comprehensive sanctions programmes RU, BY, IR, SY, KP): BCSE, MISX, SPBE, XDSE, XTEH. Set `CROCODILE_ALLOW_SANCTIONED_MARKETS` to open them.

## Python API

```python
from datetime import date

from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.calendar import calendar_for
from crocodile.core.markets.symbols import listing_symbol, parse_listing_symbol

ExchangeRegistry().by_country("TR")            # (ExchangeInfo(mic="XIST", currency="TRY", ...),)
calendar_for("XIST").is_session(date(2026, 10, 29))  # False: Republic Day
listing_symbol("THYAO", "XIST")                 # "THYAO@XIST"
```

## Countries

| Country | ISO | MIC | Exchange | Currency | Calendar | Weekend (evidence) | Tier | Note |
|---|---|---|---|---|---|---|---|---|
| Algeria | DZ | XALG | Algiers Stock Exchange | DZD | weekday fallback | SAT,SUN (unverified) | D |  |
| Angola | AO | XBDV | Bolsa De Divida E Valores De Angola (Bodiva) - Angola Securities Exchange | AOA | weekday fallback | SAT,SUN (unverified) | D |  |
| Argentina | AR | XBUE | Bolsa De Comercio De Buenos Aires | ARS | library | SAT,SUN (library) | B |  |
| Armenia | AM | XARM | Armenia Stock Exchange | AMD | weekday fallback | SAT,SUN (unverified) | D |  |
| Australia | AU | APXL | Sydney Stock Exchange Limited | AUD | weekday fallback | SAT,SUN (unverified) | D |  |
| Australia | AU | CHIA | Tmx Australia Exchange | AUD | weekday fallback | SAT,SUN (unverified) | D |  |
| Australia | AU | NSXB | Bendigo Stock Exchange Limited | AUD | weekday fallback | SAT,SUN (unverified) | D |  |
| Australia | AU | SIMV | Sim Venture Securities Exchange | AUD | weekday fallback | SAT,SUN (unverified) | D |  |
| Australia | AU | XASX | Asx - All Markets | AUD | library | SAT,SUN (library) | B |  |
| Australia | AU | XNEC | National Stock Exchange Of Australia Limited | AUD | weekday fallback | SAT,SUN (unverified) | D |  |
| Austria | AT | XWBO | Wiener Boerse Ag | EUR | library | SAT,SUN (library) | B |  |
| Azerbaijan | AZ | BSEX | Baku Stock Exchange | AZN | weekday fallback | SAT,SUN (unverified) | D |  |
| Bahamas | BS | XBAA | Bahamas International Securities Exchange | BSD | weekday fallback | SAT,SUN (unverified) | D |  |
| Bahrain | BH | XBAH | Bahrain Bourse | BHD | weekday fallback | FRI,SAT (secondary) | D |  |
| Bangladesh | BD | XCHG | Chittagong Stock Exchange Ltd. | BDT | weekday fallback | FRI,SAT (secondary) | D |  |
| Bangladesh | BD | XDHA | Dhaka Stock Exchange Ltd | BDT | weekday fallback | FRI,SAT (secondary) | D |  |
| Barbados | BB | XBAB | Barbados Stock Exchange | BBD | weekday fallback | SAT,SUN (unverified) | D |  |
| Belarus | BY | BCSE | Belarus Currency And Stock Exchange | BYN | weekday fallback | SAT,SUN (unverified) | D | sanctioned, default-closed |
| Belgium | BE | XBRU | Euronext - Euronext Brussels | EUR | library | SAT,SUN (library) | B |  |
| Bermuda | BM | XBDA | Bermuda Stock Exchange Ltd | BMD | library | SAT,SUN (library) | D |  |
| Bolivia | BO | XBOL | Bolsa Boliviana De Valores S.A. | BOB | weekday fallback | SAT,SUN (secondary) | D |  |
| Bosnia and Herzegovina | BA | XBLB | Banja Luka Stock Exchange | BAM | weekday fallback | SAT,SUN (unverified) | D |  |
| Bosnia and Herzegovina | BA | XSSE | Sarajevo Stock Exchange | BAM | weekday fallback | SAT,SUN (unverified) | D |  |
| Botswana | BW | XBOT | Botswana Stock Exchange | BWP | weekday fallback | SAT,SUN (unverified) | D |  |
| Brazil | BR | BVMF | B3 S.A. - Brasil Bolsa Balcao | BRL | library | SAT,SUN (library) | B |  |
| Bulgaria | BG | XBUL | Bulgarian Stock Exchange | EUR | weekday fallback | SAT,SUN (unverified) | D |  |
| Cabo Verde | CV | XBVC | Cape Verde Stock Exchange | CVE | weekday fallback | SAT,SUN (unverified) | D |  |
| Cambodia | KH | XCSX | Cambodia Securities Exchange | KHR | weekday fallback | SAT,SUN (unverified) | D |  |
| Canada | CA | NEOE | Cboe Canada - Neo-L (Market By Order) | CAD | weekday fallback | SAT,SUN (unverified) | D |  |
| Canada | CA | XCNQ | Canadian Securities Exchange | CAD | library (XTSE, shared calendar) | SAT,SUN (library) | D |  |
| Canada | CA | XTSE | Toronto Stock Exchange | CAD | library | SAT,SUN (library) | B |  |
| Canada | CA | XTSX | Tsx Venture Exchange | CAD | library (XTSE) | SAT,SUN (library) | D |  |
| Cayman Islands | KY | XCAY | Cayman Islands Stock Exchange | KYD | weekday fallback | SAT,SUN (unverified) | D |  |
| Chile | CL | XSGO | Santiago Stock Exchange | CLP | library | SAT,SUN (library) | B |  |
| China (Mainland) | CN | BJSE | Beijing Stock Exchange | CNY | library (XSHG, shared calendar) | SAT,SUN (library) | D |  |
| China (Mainland) | CN | XSHE | Shenzhen Stock Exchange | CNY | library (XSHG, shared calendar) | SAT,SUN (library) | B |  |
| China (Mainland) | CN | XSHG | Shanghai Stock Exchange | CNY | library | SAT,SUN (library) | B |  |
| Colombia | CO | XBOG | Bolsa De Valores De Colombia | COP | library | SAT,SUN (library) | B |  |
| Costa Rica | CR | XBNV | Bolsa Nacional De Valores, S.A. | CRC | weekday fallback | SAT,SUN (unverified) | D |  |
| Cote d'Ivoire and WAEMU region (BRVM) | CI | XBRV | Bourse Regionale Des Valeurs Mobilieres | XOF | weekday fallback | SAT,SUN (unverified) | D |  |
| Croatia | HR | XZAG | Zagreb Stock Exchange | EUR | library | SAT,SUN (library) | D |  |
| Cyprus | CY | XCYS | Cyprus Stock Exchange | EUR | library | SAT,SUN (library) | D |  |
| Czechia | CZ | XPRA | Prague Stock Exchange | CZK | library | SAT,SUN (library) | B |  |
| Denmark | DK | XCSE | Nasdaq Copenhagen A/S | DKK | library | SAT,SUN (library) | B |  |
| Ecuador (Quito) | EC | XGUA | Bolsa De Valores De Guayaquil | USD | weekday fallback | SAT,SUN (secondary) | D |  |
| Ecuador (Quito) | EC | XQUI | Bolsa De Valores De Quito | USD | weekday fallback | SAT,SUN (unverified) | D |  |
| Egypt | EG | XCAI | Egyptian Exchange | EGP | weekday fallback | FRI,SAT (secondary) | B |  |
| Estonia | EE | XTAL | Nasdaq Tallinn As | EUR | library | SAT,SUN (library) | D |  |
| Eswatini | SZ | XSWA | Eswatini Stock Exchange | SZL | weekday fallback | SAT,SUN (unverified) | D |  |
| Fiji | FJ | XSPS | South Pacific Stock Exchange | FJD | weekday fallback | SAT,SUN (unverified) | D |  |
| Finland | FI | XHEL | Nasdaq Helsinki Ltd | EUR | library | SAT,SUN (library) | B |  |
| France | FR | XPAR | Euronext - Euronext Paris | EUR | library | SAT,SUN (library) | B |  |
| Georgia | GE | XGSE | Georgia Stock Exchange | GEL | weekday fallback | SAT,SUN (unverified) | D |  |
| Germany | DE | TGAT | Tradegate Berlin Stock Exchange | EUR | weekday fallback | SAT,SUN (unverified) | D |  |
| Germany | DE | XBER | Boerse Berlin | EUR | weekday fallback | SAT,SUN (unverified) | D |  |
| Germany | DE | XDUS | Boerse Duesseldorf | EUR | library | SAT,SUN (library) | D |  |
| Germany | DE | XETR | Xetra | EUR | library | SAT,SUN (library) | A |  |
| Germany | DE | XFRA | Deutsche Boerse Ag | EUR | library | SAT,SUN (library) | A |  |
| Germany | DE | XHAM | Hanseatische Wertpapierboerse Hamburg | EUR | library | SAT,SUN (library) | D |  |
| Germany | DE | XHAN | Niedersaechsische Boerse Zu Hannover | EUR | weekday fallback | SAT,SUN (unverified) | D |  |
| Germany | DE | XMUN | Boerse Muenchen | EUR | weekday fallback | SAT,SUN (unverified) | D |  |
| Germany | DE | XSTU | Boerse Stuttgart | EUR | library | SAT,SUN (library) | B |  |
| Ghana | GH | XGHA | Ghana Stock Exchange | GHS | weekday fallback | SAT,SUN (secondary) | D |  |
| Gibraltar | GI | GSXL | The Gibraltar Stock Exchange | GBP | weekday fallback | SAT,SUN (unverified) | D |  |
| Greece | GR | ASEX | Athens Stock Exchange | EUR | library | SAT,SUN (library) | B |  |
| Hong Kong SAR | HK | XHKG | Hong Kong Exchanges And Clearing Ltd | HKD | library | SAT,SUN (library) | B |  |
| Hungary | HU | XBUD | Budapest Stock Exchange | HUF | library | SAT,SUN (library) | B |  |
| Iceland | IS | XICE | Nasdaq Iceland Hf. | ISK | library | SAT,SUN (library) | D |  |
| India | IN | XBOM | Bse Ltd | INR | library | SAT,SUN (library) | B |  |
| India | IN | XNSE | National Stock Exchange Of India | INR | weekday fallback | SAT,SUN (unverified) | B |  |
| Indonesia | ID | XIDX | Indonesia Stock Exchange | IDR | library | SAT,SUN (library) | B |  |
| Iran | IR | XTEH | Tehran Stock Exchange | IRR | weekday fallback | THU,FRI (secondary) | D | sanctioned, default-closed |
| Iraq | IQ | XIQS | Iraq Stock Exchange | IQD | weekday fallback | SAT,SUN (unverified) | D |  |
| Ireland | IE | XDUB | Irish Stock Exchange - All Market | EUR | library | SAT,SUN (library) | B |  |
| Israel | IL | XTAE | Tel Aviv Stock Exchange | ILS | library | SAT,SUN (library) | B | quoted in minor unit |
| Italy | IT | XMIL | Borsa Italiana S.P.A. | EUR | library | SAT,SUN (library) | B |  |
| Jamaica | JM | XJAM | Jamaica Stock Exchange | JMD | weekday fallback | SAT,SUN (unverified) | D |  |
| Japan | JP | XFKA | Fukuoka Stock Exchange | JPY | weekday fallback | SAT,SUN (unverified) | D |  |
| Japan | JP | XJPX | Japan Exchange Group | JPY | library | SAT,SUN (library) | B |  |
| Japan | JP | XNGO | Nagoya Stock Exchange | JPY | weekday fallback | SAT,SUN (unverified) | D |  |
| Japan | JP | XSAP | Sapporo Securities Exchange | JPY | weekday fallback | SAT,SUN (unverified) | D |  |
| Jordan | JO | XAMM | Amman Stock Exchange | JOD | weekday fallback | FRI,SAT (secondary) | D |  |
| Kazakhstan | KZ | AIXK | Astana International Exchange Ltd | KZT | library | SAT,SUN (library) | D |  |
| Kazakhstan | KZ | XKAZ | Kazakhstan Stock Exchange | KZT | weekday fallback | SAT,SUN (unverified) | D |  |
| Kenya | KE | XNAI | Nairobi Stock Exchange | KES | weekday fallback | SAT,SUN (unverified) | D |  |
| Kuwait | KW | XKUW | Kuwait Stock Exchange | KWD | weekday fallback | FRI,SAT (secondary) | D |  |
| Laos | LA | XLAO | Lao Securities Exchange | LAK | weekday fallback | SAT,SUN (unverified) | D |  |
| Latvia | LV | XRIS | Nasdaq Riga As | EUR | library | SAT,SUN (library) | D |  |
| Lebanon | LB | XBEY | Bourse De Beyrouth - Beirut Stock Exchange | LBP | weekday fallback | SAT,SUN (unverified) | D |  |
| Libya | LY | XLSM | Libyan Stock Market | LYD | weekday fallback | SAT,SUN (unverified) | D | targeted sanctions: check |
| Lithuania | LT | XLIT | Ab Nasdaq Vilnius | EUR | library | SAT,SUN (library) | D |  |
| Luxembourg | LU | XLUX | Luxembourg Stock Exchange | EUR | library | SAT,SUN (library) | C |  |
| Malawi | MW | XMSW | Malawi Stock Exchange | MWK | weekday fallback | SAT,SUN (secondary) | D |  |
| Malaysia | MY | XKLS | Bursa Malaysia | MYR | library | SAT,SUN (library) | B |  |
| Maldives | MV | MALX | Maldives Stock Exchange | MVR | weekday fallback | SAT,SUN (unverified) | D |  |
| Malta | MT | XMAL | Malta Stock Exchange | EUR | weekday fallback | SAT,SUN (unverified) | D |  |
| Mauritius | MU | XMAU | Stock Exchange Of Mauritius Ltd | MUR | weekday fallback | SAT,SUN (secondary) | D |  |
| Mexico | MX | BIVA | Bolsa Institucional De Valores | MXN | weekday fallback | SAT,SUN (unverified) | D |  |
| Mexico | MX | XMEX | Bolsa Mexicana De Valores (Mexican Stock Exchange) | MXN | library | SAT,SUN (library) | B |  |
| Moldova | MD | XMOL | Moldova Stock Exchange | MDL | weekday fallback | SAT,SUN (unverified) | D |  |
| Mongolia | MN | XULA | Mongolian Stock Exchange | MNT | weekday fallback | SAT,SUN (secondary) | D |  |
| Montenegro | ME | XMNX | Montenegro Stock Exchange | EUR | weekday fallback | SAT,SUN (unverified) | D |  |
| Morocco | MA | XCAS | Casablanca Stock Exchange | MAD | weekday fallback | SAT,SUN (unverified) | D |  |
| Mozambique | MZ | XBVM | Mozambique Stock  Exchange | MZN | weekday fallback | SAT,SUN (unverified) | D |  |
| Namibia | NA | XNAM | Namibian Stock Exchange | NAD | weekday fallback | SAT,SUN (unverified) | D |  |
| Nepal | NP | XNEP | Nepal Stock Exchange | NPR | weekday fallback | SAT,SUN (unverified) | D |  |
| Netherlands | NL | XAMS | Euronext - Euronext Amsterdam | EUR | library | SAT,SUN (library) | B |  |
| New Zealand | NZ | XNZE | Nzx - All Markets | NZD | library | SAT,SUN (library) | B |  |
| Nigeria | NG | XNSA | The Nigerian Stock Exchange | NGN | weekday fallback | SAT,SUN (unverified) | D |  |
| North Macedonia | MK | XMAE | Macedonian Stock Exchange | MKD | weekday fallback | SAT,SUN (unverified) | D |  |
| Norway | NO | XOSL | Oslo Bors | NOK | library | SAT,SUN (library) | B |  |
| Oman | OM | XMUS | Muscat Stock Exchange | OMR | weekday fallback | SAT,SUN (unverified) | D |  |
| Pakistan | PK | XKAR | The Pakistan Stock Exchange Limited | PKR | library | SAT,SUN (library) | B |  |
| Palestine | PS | XPAE | Palestine Securities Exchange | ILS | weekday fallback | FRI,SAT (secondary) | D |  |
| Panama | PA | XPTY | Latin American Stock Exchange, Inc. | USD | weekday fallback | SAT,SUN (unverified) | D |  |
| Papua New Guinea | PG | XPOM | Pngx Markets Limited | PGK | weekday fallback | SAT,SUN (unverified) | D |  |
| Paraguay | PY | XVPA | Bolsa De Valores Y Productos De Asuncion Sa | PYG | weekday fallback | SAT,SUN (unverified) | D |  |
| Peru | PE | XLIM | Bolsa De Valores De Lima | PEN | library | SAT,SUN (library) | B |  |
| Philippines | PH | XPHS | Philippine Stock Exchange, Inc. | PHP | library | SAT,SUN (library) | D |  |
| Poland | PL | XWAR | Warsaw Stock Exchange/Equities/Main Market | PLN | library | SAT,SUN (library) | B |  |
| Portugal | PT | XLIS | Euronext - Euronext Lisbon | EUR | library | SAT,SUN (library) | B |  |
| Qatar | QA | DSMD | Qatar Stock Exchange | QAR | weekday fallback | FRI,SAT (secondary) | D |  |
| Romania | RO | XBSE | Spot Regulated Market - Bvb | RON | library | SAT,SUN (library) | B |  |
| Russia | RU | MISX | Moscow Exchange - All Markets | RUB | library | SAT,SUN (library) | A | sanctioned, default-closed |
| Russia | RU | SPBE | Spb Exchange - All Markets | RUB | weekday fallback | SAT,SUN (unverified) | D | sanctioned, default-closed |
| Rwanda | RW | RSEX | Rwanda Stock Exchange | RWF | weekday fallback | SAT,SUN (secondary) | C |  |
| Saint Kitts and Nevis (ECSE, OECS region) | KN | XECS | Eastern Caribbean Securities Exchange | XCD | weekday fallback | SAT,SUN (unverified) | D |  |
| Saudi Arabia | SA | XSAU | Saudi Stock Exchange | SAR | library | FRI,SAT (library) | B |  |
| Serbia | RS | XBEL | Belgrade Stock Exchange | RSD | library | SAT,SUN (library) | D |  |
| Seychelles | SC | TRPX | Merj Exchange Limited | SCR | weekday fallback | SAT,SUN (unverified) | D |  |
| Singapore | SG | XSES | Singapore Exchange | SGD | library | SAT,SUN (library) | B |  |
| Slovenia | SI | XLJU | Ljubljana Stock Exchange (Official Market) | EUR | library | SAT,SUN (library) | D |  |
| South Africa | ZA | XJSE | Johannesburg Stock Exchange | ZAR | library | SAT,SUN (library) | B | quoted in minor unit |
| South Korea | KR | XKRX | Korea Exchange (Stock Market) | KRW | library | SAT,SUN (library) | B |  |
| Spain | ES | BMEX | Bme - Bolsas Y Mercados Espanoles | EUR | library | SAT,SUN (library) | B |  |
| Sri Lanka | LK | XCOL | Colombo Stock Exchange | LKR | weekday fallback | SAT,SUN (unverified) | D |  |
| Sudan | SD | XKHA | Khartoum Stock Exchange | SDG | weekday fallback | FRI,SAT (secondary) | D | targeted sanctions: check |
| Sweden | SE | XNGM | Nordic Growth Market | SEK | library (XSTO, shared calendar) | SAT,SUN (library) | D |  |
| Sweden | SE | XSAT | Spotlight Stock Market Ab | SEK | weekday fallback | SAT,SUN (secondary) | D |  |
| Sweden | SE | XSTO | Nasdaq Stockholm Ab | SEK | library | SAT,SUN (library) | B |  |
| Switzerland | CH | XBRN | Bx Swiss Ag | CHF | weekday fallback | SAT,SUN (unverified) | D |  |
| Switzerland | CH | XSWX | Six Swiss Exchange | CHF | library | SAT,SUN (library) | B |  |
| Syria | SY | XDSE | Damascus Securities Exchange | SYP | weekday fallback | SAT,SUN (unverified) | D | sanctioned, default-closed |
| Taiwan | TW | ROCO | Taipei Exchange | TWD | weekday fallback | SAT,SUN (unverified) | B |  |
| Taiwan | TW | XTAI | Taiwan Stock Exchange | TWD | library | SAT,SUN (library) | A |  |
| Tanzania | TZ | XDAR | Dar Es  Salaam Stock Exchange | TZS | weekday fallback | SAT,SUN (secondary) | C |  |
| Thailand | TH | XBKK | Stock Exchange Of Thailand | THB | library | SAT,SUN (library) | B |  |
| Trinidad and Tobago | TT | XTRN | Trinidad And Tobago Stock Exchange | TTD | weekday fallback | SAT,SUN (secondary) | D |  |
| Tunisia | TN | XTUN | Tunis Stock Exchange (Bourse De Tunis) | TND | weekday fallback | SAT,SUN (secondary) | D |  |
| Türkiye | TR | XIST | Borsa Istanbul | TRY | library | SAT,SUN (library) | B |  |
| Uganda | UG | XUGA | Uganda Securities Exchange | UGX | weekday fallback | SAT,SUN (unverified) | D |  |
| Ukraine | UA | PFTS | Pfts Stock Exchange | UAH | weekday fallback | SAT,SUN (unverified) | D |  |
| Ukraine | UA | UKEX | Ukrainian Exchange | UAH | weekday fallback | SAT,SUN (unverified) | D |  |
| United Arab Emirates | AE | DIFX | Nasdaq Dubai | USD | weekday fallback | SAT,SUN (unverified) | D |  |
| United Arab Emirates | AE | XADS | Abu Dhabi Securities Exchange | AED | weekday fallback | SAT,SUN (secondary) | D |  |
| United Arab Emirates | AE | XDFM | Dubai Financial Market | AED | weekday fallback | SAT,SUN (secondary) | D |  |
| United Kingdom | GB | AQSE | Aquis Stock Exchange | GBP | weekday fallback | SAT,SUN (unverified) | C |  |
| United Kingdom | GB | AQXE | Aquis Exchange Plc | GBP | weekday fallback | SAT,SUN (unverified) | C |  |
| United Kingdom | GB | BCXE | Cboe Europe Equities | EUR | weekday fallback | SAT,SUN (unverified) | D |  |
| United Kingdom | GB | XLON | London Stock Exchange | GBP | library | SAT,SUN (library) | B | quoted in minor unit |
| United States | US | IEXG | Investors Exchange | USD | weekday fallback | SAT,SUN (unverified) | C |  |
| United States | US | XCBO | Cboe Global Markets Inc. | USD | weekday fallback | SAT,SUN (unverified) | C |  |
| United States | US | XNAS | Nasdaq - All Markets | USD | library (XNYS) | SAT,SUN (library) | B |  |
| United States | US | XNYS | New York Stock Exchange, Inc. | USD | library | SAT,SUN (library) | B |  |
| Uruguay | UY | XMNT | Bolsa De Valores De Montevideo | UYU | weekday fallback | SAT,SUN (unverified) | D |  |
| Uzbekistan | UZ | XSTE | Republican Stock Exchange | UZS | weekday fallback | SAT,SUN (unverified) | C |  |
| Venezuela | VE | BVCA | Caracas Stock Exchange | VES | weekday fallback | SAT,SUN (unverified) | D | targeted sanctions: check |
| Vietnam | VN | HSTC | Hanoi Stock Exchange | VND | weekday fallback | SAT,SUN (unverified) | D |  |
| Vietnam | VN | XSTC | Hochiminh Stock Exchange | VND | weekday fallback | SAT,SUN (unverified) | B |  |
| Zambia | ZM | XLUS | Lusaka Stock Exchange | ZMW | weekday fallback | SAT,SUN (unverified) | D |  |
| Zimbabwe | ZW | VFEX | Victoria Falls Stock Exchange | USD | weekday fallback | SAT,SUN (unverified) | C |  |
| Zimbabwe | ZW | XZIM | Zimbabwe Stock Exchange | ZWG | weekday fallback | SAT,SUN (unverified) | C |  |

## Known limits

- XSHG, XBOM and XSES library calendars (and XSHE/BJSE, which share XSHG) end on 2026-12-31 in exchange_calendars 4.13.2. After that date they answer with the weekday fallback, and `quality_for(d)` reports it.
- Library MICs without a fixed upper bound cover up to one year after the process imports the library; later dates use the weekday fallback.
- Markets believed to trade Sunday-Thursday whose weekend could not be sourced keep Saturday-Sunday: Oman (XMUS), Iraq (XIQS), Algeria (XALG), Libya (XLSM), Maldives (MALX), Syria (XDSE) and Nepal (XNEP, sources conflict).
- NSE India (XNSE) uses the weekday fallback. It was not given the BSE calendar because no retrieved source confirmed identical holidays.
- pandas is locked (not pinned) at 3.0.3. An install without the lock can resolve pandas 3.1.x, where exchange_calendars 4.13.2 cannot build ASEX, XBEL, XBSE or XCYS.
