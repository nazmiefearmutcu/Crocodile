# Live market coverage (Crocodile 0.5.0)

Measured on 2026-11-12 09:24 UTC by `scripts/measure_live_coverage.py`: one listing per market,
one poll, through the live source chain of `crocodile.equity.universal.live`. A row is
what the publisher answered at that moment, not a promise: a board outside its session
shows the last session's figures, and a ladder can be shorter in an auction.

- Markets in the allow-list: **172**
- Answered with a price: **130**
  - a multi-level order book (`l2`): **10**
  - a top of book (`l1`): **15**
  - the last price and session figures (`last`): **105**
- Default-closed (sanctions): **5**
- No keyless source verified: **36**
- A source is bound but did not answer this poll: **1**

| MIC | Country | Exchange | Sample | Answered by | Tier | Levels (bid/ask) | Tape | Delay | Board rows | Note |
|---|---|---|---|---|---|---|---|---|---|---|
| AIXK | KZ | Astana International Exchange Ltd | KAP | site_aix | l1 | 1/1 |  |  | 133 |  |
| APXL | AU | Sydney Stock Exchange Limited |  | no_source |  |  |  |  |  | no keyless source verified |
| AQSE | GB | Aquis Stock Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| AQXE | GB | Aquis Exchange Plc |  | no_source |  |  |  |  |  | no keyless source verified |
| ASEX | GR | Athens Stock Exchange | TPEIR | tradingview | last | 0/0 |  | 15 min | 50 |  |
| BCSE | BY | Belarus Currency And Stock Exchange |  | closed |  |  |  |  |  | sanctioned, default-closed |
| BCXE | GB | Cboe Europe Equities | RRl | site_cboe_europe_cxe | l1 | 1/1 |  |  | 5787 |  |
| BIVA | MX | Bolsa Institucional De Valores | AMX/B | tradingview | last | 0/0 |  | 20 min | 50 |  |
| BJSE | CN | Beijing Stock Exchange | 920002 | tencent | l2 | 5/5 |  |  |  |  |
| BMEX | ES | Bme - Bolsas Y Mercados Espanoles | NYE | tradingview | last | 0/0 |  | 15 min | 50 |  |
| BSEX | AZ | Baku Stock Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| BVCA | VE | Caracas Stock Exchange | BNC | tradingview | last | 0/0 |  | 15 min | 32 |  |
| BVMF | BR | B3 S.A. - Brasil Bolsa Balcao | PETR4 | b3 | last | 0/0 |  | 15 min | 50 |  |
| CHIA | AU | Tmx Australia Exchange | BHP | yahoo_chart | last | 0/0 |  |  |  |  |
| DIFX | AE | Nasdaq Dubai | REIT | tradingview | last | 0/0 |  | 15 min | 7 |  |
| DSMD | QA | Qatar Stock Exchange | QNBK | site_qse | l1 | 1/1 |  |  | 110 |  |
| GSXL | GI | The Gibraltar Stock Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| HSTC | VN | Hanoi Stock Exchange | SHS | vps | l2 | 3/3 | yes |  | 50 |  |
| IEXG | US | Investors Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| MALX | MV | Maldives Stock Exchange | STO | site_maldives | last | 0/0 |  |  | 9 |  |
| MISX | RU | Moscow Exchange - All Markets |  | closed |  |  |  |  |  | sanctioned, default-closed |
| NEOE | CA | Cboe Canada - Neo-L (Market By Order) | SOXS | tradingview | last | 0/0 |  | 15 min | 50 |  |
| NSXB | AU | Bendigo Stock Exchange Limited |  | no_source |  |  |  |  |  | no keyless source verified |
| PFTS | UA | Pfts Stock Exchange | 236632 | site_pfts_stock_exchange | last | 0/0 |  |  | 16 |  |
| ROCO | TW | Taipei Exchange | 6488 | twse_mis | l2 | 5/5 | yes | real time | 50 |  |
| RSEX | RW | Rwanda Stock Exchange | IMR | site_rse | last | 0/0 |  |  | 10 |  |
| SIMV | AU | Sim Venture Securities Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| SPBE | RU | Spb Exchange - All Markets |  | closed |  |  |  |  |  | sanctioned, default-closed |
| TGAT | DE | Tradegate Berlin Stock Exchange | SAP | tradegate | l1 | 1/1 |  |  | 50 |  |
| TRPX | SC | Merj Exchange Limited |  | no_source |  |  |  |  |  | no keyless source verified |
| UKEX | UA | Ukrainian Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| VFEX | ZW | Victoria Falls Stock Exchange | AXIA | site_vfex_market_cap | last | 0/0 |  |  | 19 |  |
| XADS | AE | Abu Dhabi Securities Exchange | ADNOCGAS | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XALG | DZ | Algiers Stock Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| XAMM | JO | Amman Stock Exchange | ABUS | site_amman | last | 0/0 |  |  | 87 |  |
| XAMS | NL | Euronext - Euronext Amsterdam | ASML | yahoo_chart | last | 0/0 |  |  |  |  |
| XARM | AM | Armenia Stock Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| XASX | AU | Asx - All Markets | CBA | asx | l1 | 1/1 |  | 20 min | 50 |  |
| XBAA | BS | Bahamas International Securities Exchange | AML | site_bisx | last | 0/0 |  |  | 24 |  |
| XBAB | BB | Barbados Stock Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| XBAH | BH | Bahrain Bourse | TAKAFUL | tradingview | last | 0/0 |  | 15 min | 20 |  |
| XBDA | BM | Bermuda Stock Exchange Ltd | ALSH | site_bsx | l1 | 1/1 |  |  | 10 |  |
| XBDV | AO | Bolsa De Divida E Valores De Angola (Bodiva) - Angola Securities Exchange | CONFUFIA | site_bodiva | last | 0/0 |  |  | 419 |  |
| XBEL | RS | Belgrade Stock Exchange | DJRS | tradingview | last | 0/0 |  | 15 min | 12 |  |
| XBER | DE | Boerse Berlin |  | no_source |  |  |  |  |  | no keyless source verified |
| XBEY | LB | Bourse De Beyrouth - Beirut Stock Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| XBKK | TH | Stock Exchange Of Thailand | TTB | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XBLB | BA | Banja Luka Stock Exchange | BOSK-R-A | site_blberza | last | 0/0 |  |  | 10 |  |
| XBNV | CR | Bolsa Nacional De Valores, S.A. |  | no_source |  |  |  |  |  | no keyless source verified |
| XBOG | CO | Bolsa De Valores De Colombia | ECOPETROL | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XBOL | BO | Bolsa Boliviana De Valores S.A. |  | no_source |  |  |  |  |  | no keyless source verified |
| XBOM | IN | Bse Ltd | ELITECON | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XBOT | BW | Botswana Stock Exchange | KNO | site_bse_botswana | last | 0/0 |  |  | 23 |  |
| XBRN | CH | Bx Swiss Ag | DTLA | tradingview | last | 0/0 |  | real time | 50 |  |
| XBRU | BE | Euronext - Euronext Brussels | ABI | yahoo_chart | last | 0/0 |  |  |  |  |
| XBRV | CI | Bourse Regionale Des Valeurs Mobilieres | ABJC | site_brvm | last | 0/0 |  |  | 48 |  |
| XBSE | RO | Spot Regulated Market - Bvb | TLV | site_bvb | last | 0/0 |  |  | 89 |  |
| XBUD | HU | Budapest Stock Exchange | RICHTER | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XBUE | AR | Bolsa De Comercio De Buenos Aires | COME | tradingview | last | 0/0 |  | 20 min | 50 |  |
| XBUL | BG | Bulgarian Stock Exchange | HREP | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XBVC | CV | Cape Verde Stock Exchange | BCA | site_bvc_cv | last | 0/0 |  |  | 4 |  |
| XBVM | MZ | Mozambique Stock  Exchange | CDM | site_bvm_mz | last | 0/0 |  |  | 9 |  |
| XCAI | EG | Egyptian Exchange | AIH | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XCAS | MA | Casablanca Stock Exchange | ADH | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XCAY | KY | Cayman Islands Stock Exchange |  | no_sample |  |  |  |  |  | no sample symbol known |
| XCBO | US | Cboe Global Markets Inc. | SNXX | site_cboe_us_listed | l1 | 1/1 |  |  | 1672 |  |
| XCHG | BD | Chittagong Stock Exchange Ltd. |  | no_source |  |  |  |  |  | no keyless source verified |
| XCNQ | CA | Canadian Securities Exchange | NURL | tradingview | last | 0/0 |  | 20 min | 50 |  |
| XCOL | LK | Colombo Stock Exchange | ABAN.N0000 | site_cse_colombo | last | 0/0 |  |  | 294 |  |
| XCSE | DK | Nasdaq Copenhagen A/S | NOVO_B | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XCSX | KH | Cambodia Securities Exchange | PWSA | site_csx_cambodia | last | 0/0 |  |  | 12 |  |
| XCYS | CY | Cyprus Stock Exchange | PND | tradingview | last | 0/0 |  | 15 min | 30 |  |
| XDAR | TZ | Dar Es  Salaam Stock Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| XDFM | AE | Dubai Financial Market | AIRARABIA | site_dfm | l1 | 1/1 | yes |  | 474 |  |
| XDHA | BD | Dhaka Stock Exchange Ltd | IPDC | tradingview | last | 0/0 |  | 5 min | 50 |  |
| XDSE | SY | Damascus Securities Exchange |  | closed |  |  |  |  |  | sanctioned, default-closed |
| XDUB | IE | Irish Stock Exchange - All Market | RYA | yahoo_chart | last | 0/0 |  |  |  |  |
| XDUS | DE | Boerse Duesseldorf | PLUN | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XECS | KN | Eastern Caribbean Securities Exchange | BON | site_ecse_eastern_caribbean | l1 | 0/1 |  |  | 13 |  |
| XETR | DE | Xetra | SAP | boerse_frankfurt | l2 | 10/10 |  |  | 50 |  |
| XFKA | JP | Fukuoka Stock Exchange | 2974 | tradingview | last | 0/0 |  | 20 min | 32 |  |
| XFRA | DE | Deutsche Boerse Ag | SAP | boerse_frankfurt | l1 | 1/1 |  |  | 50 |  |
| XGHA | GH | Ghana Stock Exchange | AADS | site_gse_ghana | l1 | 1/0 | yes |  | 38 |  |
| XGSE | GE | Georgia Stock Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| XGUA | EC | Bolsa De Valores De Guayaquil |  | no_source |  |  |  |  |  | no keyless source verified |
| XHAM | DE | Hanseatische Wertpapierboerse Hamburg | PLUN | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XHAN | DE | Niedersaechsische Boerse Zu Hannover | TT1 | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XHEL | FI | Nasdaq Helsinki Ltd | NOKIA | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XHKG | HK | Hong Kong Exchanges And Clearing Ltd | 700 | tencent | last | 0/0 |  |  | 50 |  |
| XICE | IS | Nasdaq Iceland Hf. | ASAR | tradingview | last | 0/0 |  | 15 min | 29 |  |
| XIDX | ID | Indonesia Stock Exchange | GOTO | tradingview | last | 0/0 |  | 10 min | 50 |  |
| XIQS | IQ | Iraq Stock Exchange | HMAN | site_isx | last | 0/0 |  |  | 42 |  |
| XIST | TR | Borsa Istanbul | SASA | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XJAM | JM | Jamaica Stock Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| XJPX | JP | Japan Exchange Group | 4564 | tradingview | last | 0/0 |  | 20 min | 50 |  |
| XJSE | ZA | Johannesburg Stock Exchange | NPN | yahoo_chart | last | 0/0 |  |  |  |  |
| XKAR | PK | The Pakistan Stock Exchange Limited | BAPLR1 | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XKAZ | KZ | Kazakhstan Stock Exchange | AIRA | site_kase | last | 0/0 |  |  | 25 |  |
| XKHA | SD | Khartoum Stock Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| XKLS | MY | Bursa Malaysia | MMAG | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XKRX | KR | Korea Exchange (Stock Market) | 005930 | naver | l2 | 5/5 |  | real time | 50 |  |
| XKUW | KW | Kuwait Stock Exchange | KFH | yahoo_chart | last | 0/0 |  |  |  |  |
| XLAO | LA | Lao Securities Exchange | BCEL | site_lsx | last | 0/0 |  |  | 10 |  |
| XLIM | PE | Bolsa De Valores De Lima | AAPL | site_bvl | l1 | 1/1 |  |  | 130 |  |
| XLIS | PT | Euronext - Euronext Lisbon | GALP | yahoo_chart | last | 0/0 |  |  |  |  |
| XLIT | LT | Ab Nasdaq Vilnius | ROE1L | tradingview | last | 0/0 |  | 15 min | 24 |  |
| XLJU | SI | Ljubljana Stock Exchange (Official Market) | CICG | site_ljse | last | 0/0 |  |  | 46 |  |
| XLON | GB | London Stock Exchange | FORG | tradingview | last | 0/0 |  | real time | 50 |  |
| XLSM | LY | Libyan Stock Market |  | no_source |  |  |  |  |  | no keyless source verified |
| XLUS | ZM | Lusaka Stock Exchange | AECI | site_afx_luse | last | 0/0 |  |  | 24 |  |
| XLUX | LU | Luxembourg Stock Exchange | FTXHG | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XMAE | MK | Macedonian Stock Exchange | KMB | site_mse_macedonia | last | 0/0 |  |  | 83 |  |
| XMAL | MT | Malta Stock Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| XMAU | MU | Stock Exchange Of Mauritius Ltd |  | no_source |  |  |  |  |  | no keyless source verified |
| XMEX | MX | Bolsa Mexicana De Valores (Mexican Stock Exchange) | AMX/B | tradingview | last | 0/0 |  | 20 min | 50 |  |
| XMIL | IT | Borsa Italiana S.P.A. | BFF | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XMNT | UY | Bolsa De Valores De Montevideo |  | no_source |  |  |  |  |  | no keyless source verified |
| XMNX | ME | Montenegro Stock Exchange | 1NKBA | site_mnse | last | 0/0 |  |  | 44 |  |
| XMOL | MD | Moldova Stock Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| XMSW | MW | Malawi Stock Exchange | AIRTEL | site_mse_malawi | last | 0/0 |  |  | 17 |  |
| XMUN | DE | Boerse Muenchen | NVD | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XMUS | OM | Muscat Stock Exchange | OQGN | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XNAI | KE | Nairobi Stock Exchange | QMRT | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XNAM | NA | Namibian Stock Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| XNAS | US | Nasdaq - All Markets | AAPL | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XNEC | AU | National Stock Exchange Of Australia Limited | 218 | site_nsx_australia | last | 0/0 |  |  | 36 |  |
| XNEP | NP | Nepal Stock Exchange | ACLBSL | site_sharesansar_nepal | last | 0/0 |  |  | 336 |  |
| XNGM | SE | Nordic Growth Market | SENS | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XNGO | JP | Nagoya Stock Exchange | 2467 | tradingview | last | 0/0 |  | 20 min | 50 |  |
| XNSA | NG | The Nigerian Stock Exchange | ACCESSCORP | site_ngx | last | 0/0 |  |  | 146 |  |
| XNSE | IN | National Stock Exchange Of India | RELIANCE | groww | l2 | 5/5 | yes |  | 50 |  |
| XNYS | US | New York Stock Exchange, Inc. | IBM | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XNZE | NZ | Nzx - All Markets | AFC | site_nzx | last | 0/0 |  |  | 176 |  |
| XOSL | NO | Oslo Bors | PROXI | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XPAE | PS | Palestine Securities Exchange | PALTEL | site_pex_palestine | last | 0/0 |  |  | 44 |  |
| XPAR | FR | Euronext - Euronext Paris | MC | yahoo_chart | last | 0/0 |  |  |  |  |
| XPHS | PH | Philippine Stock Exchange, Inc. | ACPB3 | site_phisix | last | 0/0 |  |  | 387 |  |
| XPOM | PG | Pngx Markets Limited |  | no_source |  |  |  |  |  | no keyless source verified |
| XPRA | CZ | Prague Stock Exchange | CSG | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XPTY | PA | Latin American Stock Exchange, Inc. | DIESB | site_latinex | last | 0/0 |  |  | 360 |  |
| XQUI | EC | Bolsa De Valores De Quito |  | no_source |  |  |  |  |  | no keyless source verified |
| XRIS | LV | Nasdaq Riga As | DGR1R | tradingview | last | 0/0 |  | 15 min | 12 |  |
| XSAP | JP | Sapporo Securities Exchange | 2928 | tradingview | last | 0/0 |  | 20 min | 17 |  |
| XSAT | SE | Spotlight Stock Market Ab |  | no_source |  |  |  |  |  | no keyless source verified |
| XSAU | SA | Saudi Stock Exchange | 4328 | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XSES | SG | Singapore Exchange | D05 | sgx | l1 | 1/1 |  |  | 50 |  |
| XSGO | CL | Santiago Stock Exchange | LTM | tradingview | last | 0/0 |  | 20 min | 50 |  |
| XSHE | CN | Shenzhen Stock Exchange | 000001 | szse | l2 | 5/5 | yes |  | 50 |  |
| XSHG | CN | Shanghai Stock Exchange | 600519 | sse_yunhq | l2 | 5/5 | yes |  |  |  |
| XSPS | FJ | South Pacific Stock Exchange | APP | site_xsps_daily_quotes | last | 0/0 |  |  | 21 |  |
| XSSE | BA | Sarajevo Stock Exchange | ABSBRK2 | site_sase | last | 0/0 |  |  | 196 |  |
| XSTC | VN | Hochiminh Stock Exchange | VNM | vps | l2 | 3/3 | yes |  | 50 |  |
| XSTE | UZ | Republican Stock Exchange | BRBNP | site_uzse | last | 0/0 | yes |  | 19 |  |
| XSTO | SE | Nasdaq Stockholm Ab | IRLAB_A | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XSTU | DE | Boerse Stuttgart | VUL | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XSWA | SZ | Eswatini Stock Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| XSWX | CH | Six Swiss Exchange | NESN | six | l1 | 1/1 | yes | 15 min | 50 |  |
| XTAE | IL | Tel Aviv Stock Exchange | TEVA | yahoo_chart | last | 0/0 |  |  |  |  |
| XTAI | TW | Taiwan Stock Exchange | 2330 | twse_mis | l2 | 5/5 | yes | real time | 50 |  |
| XTAL | EE | Nasdaq Tallinn As | TAL1T | tradingview | last | 0/0 |  | 15 min | 30 |  |
| XTEH | IR | Tehran Stock Exchange |  | closed |  |  |  |  |  | sanctioned, default-closed |
| XTRN | TT | Trinidad And Tobago Stock Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| XTSE | CA | Toronto Stock Exchange | SHOP | tmx | last | 0/0 |  | 15 min | 50 |  |
| XTSX | CA | Tsx Venture Exchange | TNR | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XTUN | TN | Tunis Stock Exchange (Bourse De Tunis) | SFBT | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XUGA | UG | Uganda Securities Exchange | AIRTEL | site_afx_use | last | 0/0 |  |  | 19 |  |
| XULA | MN | Mongolian Stock Exchange |  | no_source |  |  |  |  |  | no keyless source verified |
| XVPA | PY | Bolsa De Valores Y Productos De Asuncion Sa |  | no_source |  |  |  |  |  | no keyless source verified |
| XWAR | PL | Warsaw Stock Exchange/Equities/Main Market | 06N | site_gpw | l1 | 1/1 | yes |  | 369 |  |
| XWBO | AT | Wiener Boerse Ag | WIE | tradingview | last | 0/0 |  | 15 min | 50 |  |
| XZAG | HR | Zagreb Stock Exchange | ADPL | site_zse_zagreb | last | 0/0 |  |  | 98 |  |
| XZIM | ZW | Zimbabwe Stock Exchange | AFDS | site_zse_zimbabwe | last | 0/0 |  |  | 40 |  |
