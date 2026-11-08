# Crocodile küresel hisse veri hattı: temel ve universal katman (Paket 1+2) tasarım sözleşmesi

8 Kasım 2026. Bu belge Crocodile'ın ABD dışındaki bütün hisse piyasalarından veri alabilmesi için ilk teslim paketinin bağlayıcı tasarımıdır. Dayanakları şunlardır:

- Kullanıcıyla brainstorming görüşmesi.
- Araştırma belgesi `docs/superpowers/research/crocodile-kuresel-borsa-arastirmasi-2026-11-08.md` ve satır düzeyindeki matris `docs/superpowers/research/data/market-matrix-2026-11-08.json` (176 borsa satırı, 133 ülke).
- Ar-Ge kod haritasının bulguları.

Başlangıç noktası `origin/main` 5600c87. Çalışma `feat/global-equity` dalındaki feature worktree'de yürür. Kullanıcı Bölüm 1'i onayladı. Bölüm 2 onaya sunulduktan sonra "Çok soru sorma, teslim edene kadar devam et" dedi. Bu nedenle Bölüm 2 önerildiği biçimde onaylı sayıldı. Kalan kararlar aşağıda `Karar / Yanılırsak bedeli` biçiminde verildi. Kararlar kapsamı daraltabilir, hiçbir zaman sessizce genişletmez.

## 0. Kullanıcı kararları (bağlayıcı)

| Konu | Karar |
|---|---|
| Veri derinliği | Katmanlı. Her ülkede gün sonu (EOD) OHLCV, kurumsal işlemler ve enstrüman bilgisi alınır. Kaynak ücretsiz veriyorsa gecikmeli intraday da alınır |
| Kaynak politikası | Ücretsiz kaynak önce. Ücretsiz katmanda anahtar isteyen kaynaklar opsiyoneldir ve kullanıcı anahtar verirse açılır |
| Sıra | Önce universal katman (bu paket), sonra resmî native konnektörler (Paket 3-4). Çakışmada native kazanır |
| Kullanım şartları belirsiz (gri) kaynaklar | Varsayılan açık. Kaynak kimliği her kayıtta kalır. Erişim kontrolü (CAPTCHA, bot sayfası, proof-of-work, coğrafi engel) aşılmaz. `contrib/evasion` opt-in olarak kalır |
| Enstrüman evreni | Her şey: hisse, ETF/fon, endeks, tahvil, varant, sertifika, yapılandırılmış ürün |
| Geçmiş | Kaynağın verdiği tüm geçmiş. Backfill kaldığı yerden devam eder (resumable) |
| Mimari | Federe universal + native (kripto tarafındaki "native ∪ ccxt" modelinin equity karşılığı) |
| Bölüm 1 | Paket yerleşimi; operating MIC; `{yerel}@{MIC}` sembolü; dondurulan ABD ticker'ları; ülke/MIC'in veri boyutu olması; okuma anında kaynak önceliği; kullanım sınıfı; yaptırımlı borsaların varsayılan kapalı olması. Onaylandı |
| Bölüm 2 | Dört tablolu listing master; kayıtlara giren `currency`/`price_scale`; fiyatların kaynaktaki birimiyle saklanması; genişleyen enstrüman türleri; Yahoo tarih ve sabit USD hatalarının düzeltilmesi. Onaylı sayıldı |

## 1. Kapsam

Bu paket şunları teslim eder:

1. Piyasadan bağımsız bir temel: MIC kaydı, para birimi ve fiyat ölçeği, borsa bilgisi, takvim, kanonik sembol, kaynak sembol grameri, listing master, yaptırım listesi ve kullanım sınıfı.
2. Şemanın çok para birimli ve çok enstrüman türlü hale getirilmesi.
3. Mevcut Yahoo ve Google Finance bağlayıcılarındaki ABD varsayımlarının düzeltilmesi.
4. MIC ve ülke boyutlu yüzeyler: `markets`, `universe`, `collect`, `backfill` ve yeni `calendar`.
5. Zamanlayıcının borsa takvimine bağlanması.
6. Universal kaynaklar: Yahoo chart, TradingView scanner ve resmî evren dizinleri.
7. Bu kaynakları kullanan orkestrasyon (`universe-sync`, toplu backfill, EOD toplama, `schedule`).
8. Canlı ölçüm ve belgeler.

Kapsam dışı olanlar (sonraki paketler):

- **Paket 3, resmî native kaynaklar:** TWSE OpenAPI, Deutsche Börse MDS, ECB kurları, GLEIF, MIC kapsamlı OpenFIGI ve ESEF. Anahtarlı olanlar: J-Quants, KRX, OpenDART ve EDINET.
- **Paket 4, Türkiye native:** KAP adaptörü, BIST resmî kurumsal işlemleri ve BIST lisans kararı.
- **Paket 5, genişlik:** A sınıfı adaylar, ülke bildirim sistemleri, MSN için çok pazarlı destek, tahvil derinliği, ana birime çevrilmiş görünüm ve ham HTTP yanıt arşivi.

**Karar:** sürüm numarası değişmez, CHANGELOG `[Unreleased]` altında yazılır. 0.4 sürümü `crypcodile` ve `stockodile` sarmalayıcılarını kaldırma sözü taşıyor; o iş bu paketin konusu değil. **Yanılırsak bedeli:** sürüm çıkarmak için ayrı ve küçük bir iş gerekir.

## 2. Mimari

```
kaynak (yahoo_chart | tradingview | resmî dizinler | mevcut ABD kaynakları)
  → SymbolGrammar: kaynak sembolü ↔ (operating MIC, yerel sembol)
  → ListingMaster: issuer → instrument → listing (para birimi, ölçek, durum)
  → kanonik kayıt + provenance_fields() (currency, price_scale, adjustment)
  → ParquetSink: source=/channel=/date=/bucket=   (her kaynak kendi bölümünde, kimse kimseyi ezmez)
  → okuma: (MIC, kanal) başına beyan edilmiş kaynak sırası, CoverageResolver ile
zamanlama: ExchangeCalendar(MIC) → ScheduledPullCoordinator (görev başına takvim) → universal EOD toplama
```

Paket yerleşimi:

- `src/crocodile/core/markets/` **yeni**: lint-temiz ve mypy strict. İçinde `mic.py`, `currency.py`, `exchanges.py`, `calendar.py`, `symbols.py`, `grammar.py`, `master.py`, `sanctions.py`, `usage.py` ve sürümlenmiş referans verisi `data/` bulunur.
- `src/crocodile/equity/universal/` **yeni**: lint-temiz.
  - `_CLEAN_PACKAGES` listesine eklenir.
  - `[tool.mypy]` altına `crocodile.equity.universal.*` için `ignore_errors = false` override'ı yazılır. Binance/Deribit modüllerinde yapılanın aynısıdır.
  - İçerik: `yahoo_chart.py`, `tradingview.py`, `directories/`, `order.py`, `orchestrator.py`, `data/`.
- **Değişen yerler:**
  - `core/schema` (`records.py`, `enums.py`, `provenance.py`), `core/store` parquet şeması, `core/scheduler`, `core/ratelimit`.
  - `capabilities/market.py` ve `capabilities/ops.py`.
  - `surfaces/dispatch.py` ve `surfaces/operate.py`.
  - `equity/providers` (`base.py`, `factory.py`, `yahoo/client.py`, `google_finance/connector.py`).
  - `README.md`, `CHANGELOG.md`, `pyproject.toml`, `uv.lock`, ilgili kapı testleri.
- **Katmanlama:** `core/markets` hiçbir şekilde `crocodile.crypto` ya da `crocodile.equity` import etmez. Bu bir test ile kilitlenir.

## 3. Bileşenler

### 3.1 Referans verisi (`core/markets/data/`)

- **`iso10383_mic.csv`:** iso20022.org'dan 8 Kasım 2026'da indirilen resmî CSV, değiştirilmeden. 2.889 satır, en son kayıt 2026-10-26. `MANIFEST.json` her dosyanın kaynak URL'sini, indirme tarihini, sha256'sını ve lisans/atıf notunu taşır. Test, sha256'yı doğrular.
- **`iso4217.csv`:** SIX ISO 4217 list-one (yayın 2026-09-17) XML'inden türetilir. Sütunlar: `code, minor_unit, name`. Bulgaristan'ın EUR olduğu bu listede doğrulandı.
- **`exchanges.csv`:** küratörlü borsa tablosu. Araştırma matrisinden deterministik bir betikle türetilir; betik `scripts/build_markets_reference.py` olarak repoya girer.
  - Sütunlar: `mic, name, country, timezone, currency, quote_scale, weekend, close_local, xcal_code, tier_research, sanctioned, notes`.
  - Bütün satırlar aynı kuralları sağlar ve testle doğrulanır:
    - Her MIC snapshot'ta ACTIVE ve OPRT.
    - Her saat dilimi `zoneinfo`'da var.
    - Her para birimi `iso4217.csv`'de var.
    - `quote_scale` ∈ {0, 2}.
  - ISO operating MIC'i olan 172 satır girer. MIC'siz dört borsa (Etiyopya ESX, BVMAC, Bhutan RSEB, Myanmar YSX) girmez; belgede ayrıca listelenir.
- **`sanctions.csv`:** varsayılan olarak kapalı ülkeler. Kaynak ve not sütunlarıyla RU, BY, IR, SY ve KP.
- **`grammar_<kaynak>.csv`:** kaynak başına MIC ↔ kaynak kodu tablosu. Örnekler: Yahoo soneki `.IS`, TradingView borsa kodu `BIST`, Google Finance kodu `IST`. Her satırda bir örnek yerel sembol, kanıt tarihi ve durumu bulunur.

**Karar:** borsa evreni ISO pazar kategorisine göre süzülmez, açık izin listesi `exchanges.csv` ile belirlenir. ISO'da ana ulusal borsaların çoğu NSPD olarak kayıtlı; kategori süzgeci onları kaçırır. **Yanılırsak bedeli:** listede olmayan bir borsa için gelen kaynak satırı reddedilir; bir borsayı listeye eklemek veri değişikliğidir.

**Karar:** yaptırım listesi, kapsamlı yaptırım programı olan ülkeleri içerir: RU (MOEX ve SPB dahil), BY, IR, SY, KP. Bu borsalar varsayılan olarak kapalıdır. `CROCODILE_ALLOW_SANCTIONED_MARKETS=RU,...` ile açılabilir. Venezuela, Myanmar, Sudan ve Libya hedefli yaptırımlar nedeniyle yalnız `notes` sütununda uyarılır. Bu liste hukuki görüş değildir. **Yanılırsak bedeli:** hukuki maruziyet ya da gereksiz kapatma. Liste veri olduğu için değiştirmek koda dokunmayı gerektirmez.

### 3.2 MIC kaydı ve borsa bilgisi (`mic.py`, `exchanges.py`)

- `MicRegistry` snapshot'ı tembel ve önbellekli biçimde yükler. Sunduğu işlemler:
  - `get(mic) -> MicEntry`. `MicEntry` alanları: mic, operating_mic, is_segment, name, category, country, city, website, status, comments.
  - `operating(mic)`: segment MIC'i operating MIC'e çevirir.
  - `require_active(mic)`: bilinmeyen kodda `UnknownMarketError`, süresi dolmuş kodda `ExpiredMarketError` verir. Hata, kayıttaki yönlendirme notunu (ör. XTUR → "MIC to use is XIST") içerir.
- `ExchangeRegistry` `exchanges.csv`'yi yükler. Sunduğu işlemler:
  - `get(mic) -> ExchangeInfo`. Alanlar: mic, name, country, tz (`ZoneInfo`), currency, quote_scale, weekend, close_local, xcal_code, tier_research, sanctioned.
  - `by_country(iso2)` ve `all()`.

### 3.3 Para birimi ve fiyat ölçeği (`currency.py`)

- `require_currency(code)` ISO 4217 tablosunda bulunmayan kodu reddeder.
- `parse_quote_unit(raw) -> QuoteUnit(currency, price_scale)`. Büyük/küçük harfe duyarlıdır:
  - `GBp` / `GBX` → (`GBP`, 2)
  - `ZAc` / `ZAC` → (`ZAR`, 2)
  - `ILA` → (`ILS`, 2)
  - Geçerli bir ISO kodu → (kod, 0)
  - Diğer her şey → `UnknownCurrencyError`. Tahmin yapılmaz.
- `to_major(value, price_scale)` dönüşümü `Decimal` ile kesin yapar. Okuyucular ve doğrulamalar için bir yardımcıdır.

**Karar:** fiyatlar kaynağın yayınladığı birimle saklanır. Ana birim karşılığı `değer / 10**price_scale` ile hesaplanır. **Yanılırsak bedeli:** `price_scale`'i uygulamayan bir sorgu alt birimle kote edilen listelemeleri 100 kat yanlış okur. Bu nedenle sütun açıklamasında, README'de ve `markets` çıktısında (`quote_scale` sütunu) açıkça yazılır. Ana birime çevrilmiş görünüm Paket 5'e kaldı.

### 3.4 Kanonik sembol (`symbols.py`)

- `listing_symbol(local, mic)`: borsanın ülkesi US ise `local` aynen döner, aksi halde `f"{local}@{operating_mic}"` döner.
- `parse_listing_symbol(symbol) -> (local, mic | None)` ve `is_listing_symbol(symbol)`.
- Yerel sembol borsanın yayınladığı gibi tutulur: NFC normalize edilir, büyük/küçük harf korunur, `.` ve `-` serbesttir. Şunları içeremez: `@`, `:`, boşluk ve kontrol karakterleri.

**Karar:** ABD bare ticker'ları dondurulur. ABD dışı kanonik sembol `{yerel}@{MIC}` biçimindedir; örnekler: `THYAO@XIST`, `7203@XJPX`, `VOD@XLON`, `CERA.N0000@XCOL`. `:` dispatch'te `source:RAW` anlamına geldiği için, `.` ise `BRK.B` ve Yahoo sonekleriyle çakıştığı için kullanılmaz. **Yanılırsak bedeli:** format sonradan değişirse `mmh3(symbol) % 128` bucket'ları yer değiştirir ve lake göçü gerekir.

### 3.5 Kaynak sembol grameri (`grammar.py`)

- `SymbolGrammar` protokolü şu işlemleri tanımlar:
  - `source`: kaynak adı.
  - `to_source(local, mic) -> str`: tablosu olmayan borsada `NoGrammarForMarket` verir.
  - `from_source(raw) -> tuple[local, mic]`.
  - `supported_mics() -> frozenset[str]`.
- İki uygulama vardır, ikisi de `grammar_<kaynak>.csv` tablolarından beslenir:
  - `SuffixGrammar`: Yahoo; yerel sembol + sonek.
  - `PrefixGrammar`: TradingView; `KOD:SEMBOL`. Bir MIC'e birden çok TradingView kodu düşebilir; `to_source` için bunlardan biri `primary` işaretlidir.
- Borsaya özgü dönüşümler tablodaki kural sütunuyla açıkça tanımlanır. Örnek: ABD sınıf hisselerinde Yahoo `.` yerine `-` kullanır (`BRK.B` ↔ `BRK-B`). Tabloda olmayan dönüşüm uygulanmaz.
- Conformance testi her satırın örnek sembolünü gidiş-dönüş denetler: `from_source(to_source(local, mic)) == (local, mic)`.

### 3.6 Listing master (`master.py`)

Stdlib `sqlite3` kullanılır, WAL kipinde ve şema sürüm tablosuyla. Varsayılan yol `<data_dir>/reference/listings.sqlite` olur. `SecurityMaster` gibi `:memory:` da desteklenir. Tablolar:

| Tablo | Anahtar ve kısıt | Alanlar |
|---|---|---|
| `issuers` | `issuer_id` PK, `lei` UNIQUE NULL | legal_name, country, lei |
| `instruments` | `instrument_id` PK, `isin` UNIQUE NULL | isin, share_class_figi, security_type, name, issuer_id |
| `listings` | `listing_id` PK, `symbol` UNIQUE, **UNIQUE(mic, local_symbol)** | symbol (kanonik), mic (operating), segment_mic, local_symbol, instrument_id, currency, price_scale, status (`active`/`unseen`/`delisted`), listed_date, delisted_date, first_seen_ns, last_seen_ns |
| `symbol_aliases` | PK(source, symbol_raw, valid_from_ns) | listing_id, valid_to_ns |
| `listing_events` | — | listing_id, kind (`currency_change`, `status_change`, `alias_change`), old, new, observed_ns, source |

API:
- `upsert(observation) -> Listing`.
  - Aynı (mic, local_symbol) gözlemlerini birleştirir.
  - Daha yeni bir gözlemdeki farklı para birimi listing'i günceller ve olayı `listing_events`'e yazar.
  - Aynı kanonik sembol farklı bir (mic, local) çiftine işaret ederse `ListingConflictError` verir.
- Arama işlemleri: `get(symbol)`, `find(mic, local)`, `resolve_alias(source, raw, at_ns=None)`, `by_isin(isin) -> list[Listing]`, `listings(mic=None, country=None, status=None)`.
- `mark_unseen(mic, source, seen, at_ns)`: listede görülmeyen listing'i `unseen` yapar. `delisted` yalnız yetkili bir dizinden gelir.

**Karar:** `SecurityMaster` ABD kapsamında kalır. Tiingo'dan gelen ABD dışı borsa etiketli satırlar SecurityMaster'a yazılmaz; atlanan satır sayısı loglanır. SecurityMaster'da aynı ticker'ı farklı bir borsayla yazmaya çalışan upsert, üzerine yazmak yerine `ListingConflictError` verir. **Yanılırsak bedeli:** bugüne kadar Tiingo'nun SHE/SHG gibi satırlarını yazan bir akış bunları artık SecurityMaster'da bulamaz. Bunlar listing master'a aittir.

### 3.7 Şema (Stage B)

Aşağıdaki alanların hepsi `T | None = None` tipindedir ve kw_only kayıtların sonuna eklenir:

| Kayıt | Yeni alanlar |
|---|---|
| `OHLCV` | `currency`, `price_scale`, `adjustment` (yeni `PriceAdjustment` enum'u: `none` / `split` / `split_dividend`) |
| `Trade`, `Quote` | `currency`, `price_scale` |
| `CorporateAction` | `currency`, `price_scale` (`value` alanının birimi; bölünme oranında `None`) |
| `Instrument` | `mic`, `segment_mic`, `isin`, `country`, `currency`, `price_scale` |

- `SecurityType` enum'una şu türler eklenir: `ETN`, `GDR`, `FUND`, `BOND`, `CERTIFICATE`, `STRUCTURED`, `INDEX`.
- `FundPeriod` enum'una `H1` ve `H2` eklenir.
- Parquet aile şeması aynı commit'te genişletilir.
- `premerge_record_schema.json` paritesi korunur (alan eklemek serbest, alan kaybı yasak). `test_lake_symmetry` yeni sütunları da kapsar.
- Yeni bir test eski bölümle (sütunsuz) yeni bölümü (sütunlu) aynı sorguda okur ve eski satırlar için `NULL` bekler.
- Yardımcı `check_currency_consistent(record, listing)`: kayıttaki ve listing'deki para birimi farklıysa kayıt yazılmaz, DLQ'ya gider.

**Karar:** `currency` ve `price_scale` yalnız listing'de değil, kayıtlarda da tutulur. Para birimi zamanla değişir (Bulgaristan 2026'da BGN'den EUR'ya, Hırvatistan 2023'te EUR'ya, Zimbabve ZWL'den ZWG'ye geçti). Listing'e join, geçmiş barları yanlış birimle okur. Bu, "dondurulmuş aile şeması" kuralına bilinçli bir ektir: kanonik aileye `union_by_name` altında boş bırakılabilir sütun eklemek, `parquet_sink.py`'nin yasakladığı aileler arası genişleme değildir. **Yanılırsak bedeli:** 0.3.x ile yazılmış lake'ler bu sütunları `NULL` okur. `NULL` "bilinmiyor" demektir ve hiçbir zaman USD varsayılmaz.

### 3.8 Takvim (`calendar.py`)

`ExchangeCalendar` protokolü şunlardan oluşur:
- Özellikler: `mic`, `tz`, `quality`. `quality` değerleri: `LIBRARY`, `IN_HOUSE`, `WEEKDAY_FALLBACK`, `ALWAYS_OPEN`.
- İşlemler: `is_session(d)`, `session_open_utc(d)`, `session_close_utc(d)`, `sessions(start, end)`, `previous_session(d)`, `latest_completed_session(now_utc, delay)`.

Uygulamalar:
- **`LibraryCalendar`:** `exchange_calendars` (Apache-2.0) üzerinde çalışır ve yeni bir temel bağımlılıktır. MIC → kütüphane kodu eşlemesi `exchanges.csv`'nin `xcal_code` sütunundan gelir (XJPX→XTKS, BMEX→XMAD gibi). Kütüphanenin desteklediği tarih aralığı dışında `CalendarRangeError` verir.
- **`WeekdayFallbackCalendar`:** `exchanges.csv`'deki hafta sonu tanımını ve kapanış saatini kullanır; kapanış saati bilinmiyorsa yerel 23:00 alınır. Tatilleri bilmez, bu yüzden `quality=WEEKDAY_FALLBACK`. Böyle bir günde veri gelmezse kayıt gap değil `no_data` olur.
- **`AlwaysOpenCalendar`:** kripto içindir.
- **Mevcut `USMarketCalendar`:** `IN_HOUSE` olarak sarılır.

`calendar_for(mic)` sırası: `xcal_code` varsa kütüphane takvimi, yoksa weekday fallback.

Çapraz testler:
- In-house XNYS ile kütüphane XNYS, 2024-2027 arasında karşılaştırılır. Farklar testte açıkça listelenir.
- BIST'in 2026 resmî tatil ve yarım gün listesi kütüphanenin XIST takvimiyle karşılaştırılır. Kapalı günler: 1 Ocak, 20 Mart, 23 Nisan, 1 Mayıs, 19 Mayıs, 27-29 Mayıs, 15 Temmuz, 29 Ekim. Yarım günler: 19 Mart, 26 Mayıs, 28 Ekim.

**Karar:** tatil verisinin birincil kaynağı bu pakette kütüphanedir. Resmî borsa listesi yalnız çapraz testte kullanılır. **Yanılırsak bedeli:** kütüphanede eksik bir tatil yanlış bir gap kaydı üretir. Gap kaydı veriyi bozmaz, yalnız görünür bir uyarı bırakır.

### 3.9 Zamanlayıcı, oran bütçesi ve DLQ

- **`PullTask.calendar: ExchangeCalendar | None`.** `None` mevcut davranışı korur (`USMarketCalendar`). Mantıksal tarih, takvimin yerel seans tarihidir. Görev, seans kapanışı + `delay` (varsayılan 45 dk) geçince vadesi gelmiş sayılır.
- **Hata sayacı kalıcıdır** ve state store'da tutulur. `max_failures` (varsayılan 3) aşılınca o tarih için bir gap kaydı (görev, tarih, neden) yazılır ve filigran (watermark) ilerler. Böylece bir tarih görevi sonsuza kadar kilitlemez.
- **Telafi** her tikte en fazla `max_dates_per_tick` (varsayılan 5) tarihle yapılır.
- **`core/ratelimit` içinde paylaşılan bütçe kaydı:** `budget(source, host)` süreç genelinde, event loop başına tek bir `TokenBucket` döner. Varsayılan hızlar kaynak tablosundan gelir. `core` hiçbir zaman `contrib` import etmez.
- **Equity DLQ** artık `db_path` ile kalıcıdır. Universal çalıştırmanın sonunda `report_drained_dlqs` çağrılır.

### 3.10 Universal kaynaklar (`equity/universal/`)

Ortak sözleşme: her kaynak şunları beyan eder:
- `name`, `usage_class`, `grammar`, `supported_channels`, `supported_mics`.
- Erişilemez durumlar için gerekçe: `unservable`.

Kayıtlar yalnız `provenance_fields()` ve kayıtlı bir basis ile üretilir. Kayıt kurucuları saat okumaz; yakalama zamanı dışarıdan verilir. Kaynaklar factory `_REGISTRY`'ye kaydedilir. `universal` bir kaynak değil, MIC başına beyan edilmiş zinciri ifade eden bir küme takma adıdır; kayıtlar her zaman gerçek kaynak adını taşır.

**a) `yahoo_chart`:** temiz paketteki aiohttp istemcisi.
- **Uç ve istek:** `https://query2.finance.yahoo.com/v8/finance/chart/{sembol}`, parametreler `period1`, `period2`, `interval`, `events=div,split`, `includeAdjustedClose=true`. Paylaşılan oran bütçesine tabidir.
- **Günlük bar tarihi:** `source_ts`, borsa-yerel seans tarihinin UTC gece yarısıdır. Yerel tarih, ham zaman damgası `exchanges.csv` saat dilimine çevrilerek bulunur. Yahoo'nun saat dilimi alanı kullanılmaz; ölçümde güvenilmez çıktı.
- **Tüm geçmiş:** tek istekte `period1` ile en eski tarihten itibaren alınır.
- **Intraday:** Yahoo'nun pencereleri içinde alınır (1m: son 7 gün; 5m/15m/30m: 60 gün; 60m: 730 gün). Yalnız açıkça seçilen listelemeler veya `limit` ile sınırlanmış dilimler içindir. Bütün evren için intraday yapılmaz.
- **Meta:** `currency` alanı `parse_quote_unit` ile işlenir. `instrumentType` alanı `SecurityType`'a eşlenir. `exchangeName` alanı MIC ile çelişirse kayıt reddedilir.
- **Olaylar:** temettüler `CorporateAction(DIVIDEND, currency, price_scale)` olur, bölünmeler `CorporateAction(SPLIT, oran)` olur.
- **Düzeltme işareti:** `adjustment` değeri bir canlı ölçümle belirlenir. Bilinen bir bölünme (NVDA 2024-06-10, 10:1) öncesindeki kapanış, bölünmeye göre düzeltilmiş mi diye kontrol edilir. Düzeltilmişse `split` yazılır, ölçülemezse `None` yazılır. `adjclose` saklanmaz. Temettü düzeltmesi okuma anında mevcut hesaplayıcıyla, kurumsal işlemlerden yapılır.
- **Hatalar:** 404 "listeleme yok" demektir. 429 `TransientProviderError` olur ve bütçe geri çekilir. HTML ya da bot sayfası `unservable: bot_challenge` olur.
- **Kullanım sınıfı:** `internal` (Yahoo ToS otomatik erişimi yasaklıyor; kullanıcı kararıyla varsayılan açık).

**b) `tradingview`:** scanner istemcisi.
- **Pazar tablosu ölçülerek kurulur.** Aday pazar adlarına birer istek atılır. Dönen borsa kodları MIC'e eşlenir. Tablo kanıt tarihini ve HTTP durumunu taşır.
- **Alanlar** `https://scanner.tradingview.com/{pazar}/metainfo` ile keşfedilir. Satır başına bir zaman damgası alanı varsa kullanılır.
- **Evren:** sayfalı `scan` istekleriyle alınır. İstenen alanlar: name, description, type, subtype, exchange, currency ve varsa isin. Sonuç ListingMaster'a ve günlük `instrument` snapshot kayıtlarına yazılır.
- **EOD snapshot:** seans kapanışından sonra (takvim) alınan OHLCV, o seans tarihine atanır. İşlem görmeyen satırlar (hacim 0 veya boş) bar üretmez. Önceki snapshot ile birebir aynı değerleri taşıyan satırlar bayat sayılır ve bar üretmez. Satır zaman damgası varsa bu sezgiler yerine o kullanılır.
- **Basis:** yeni `session_snapshot`, seviye DERIVED. Seans tarihi takvimle atanıyor; docstring bu atamanın gerekçesini yazar.
- **Tür eşlemesi:** stock → CS veya PFD; fund → ETF veya FUND; dr → ADR veya GDR; structured → STRUCTURED; bond → BOND; warrant → WARRANT.
- **Kullanım sınıfı:** `internal`.

**c) Resmî dizinler (anahtarsız):**

| Dizin | Kaynak dosya | Getirdiği ek bilgi |
|---|---|---|
| `nasdaqtrader` | `nasdaqlisted.txt`, `otherlisted.txt` | ETF ve test ihracı bayrakları |
| `nse_india` | `EQUITY_L.csv` | ISIN |
| `asx` | `ASXListedCompanies.csv` | — |
| `twse` | `t187ap03_L.csv` | — (OGDL v1 → `redistribution`, atıf zorunlu) |

Her dizin ListingMaster'a gözlem yazar ve günlük `instrument` snapshot'ı üretir. Survivorship için snapshot'lar her gün saklanır. Yeni basis `listing_directory`, seviye NATIVE. Erişilemeyen dizin `unservable` olur.

**d) Kaynak sırası (`order.py`, `data/source_order.csv`):** (mic, kanal) başına sıralı kaynak listesidir.
- Varsayılan sıralar: `ohlcv` için önce native kaynaklar, sonra `yahoo_chart`, sonra `tradingview`. `instrument` için dizinler, `tradingview`, `yahoo_chart`. `corp_action` için `yahoo_chart`.
- ABD'de mevcut native kaynaklar önde gelir.
- Doğrulama: tablodaki her kaynak factory'de kayıtlı, her MIC `exchanges.csv`'de bulunmak zorunda.
- `resolve_daily_bars(listing, start, end)`, `CoverageResolver` önceliğiyle tarih başına en yüksek sıradaki kaynağı seçer. Equity tarafının günlük bar okuyan yolu `--sources` verilmediğinde bunu kullanır.

**e) Orkestratör (`orchestrator.py`):** kapsam şunlardan biriyle verilir: `--mic`, `--country`, `--all` veya isteğe bağlı sembol listesi.

| İşlem | Davranış |
|---|---|
| `sync_universe(kapsam)` | Dizinler ve TradingView evreni → ListingMaster ve instrument snapshot'ları; görülmeyenler `unseen` olur |
| `backfill(kapsam, kanallar, start=None, end)` | Listing başına filigranla kaldığı yerden devam eder. Paralellik sınırlı, bütçeye bağlı. Yaptırımlı borsa ve `unservable` gerekçeleri listing başına raporlanır |
| `collect_eod(kapsam, now)` | Her pazar için son tamamlanmış seans toplanmamışsa TradingView snapshot'ı alınır. TradingView'ın kapsamadığı MIC'lerde sınırlı sayıda Yahoo isteği yapılır |
| `collect_intraday(listings, interval)` | Seçili listelemeler için Yahoo intraday |

### 3.11 Yüzeyler

- **Parametreler:** `MarketsParams` ve `UniverseParams` yapılarına `mic: str | None` ve `country: str | None` eklenir. Kripto yarısı bunlardan biri dolu gelirse `CapabilityUnavailable` ile gerekçesini söyleyerek reddeder. Yeni alan adları `symbol*`, `exchange`, `asset_class` ve `data_dir` adlarını kullanmaz.
- **`markets --asset-class equity`** kapsam raporudur. `exchanges.csv` satırları; gramer, kaynak sırası, listing sayısı ve son başarılı toplama bilgisiyle birleşir. Sütunlar: mic, name, country, timezone, currency, quote_scale, calendar_quality, sources, usage_classes, tier_research, sanctioned, listings_known, last_success.
- **`universe --asset-class equity --mic/--country`** ListingMaster'dan, mevcut satır sınırına uyarak döner.
- **`collect` ve `backfill`:** equity yarısı `sources=["universal"]` takma adını veya açık kaynak adlarını kabul eder. `mic`/`country` dilimleriyle çalışır. Sembol verilmediyse listeyi ListingMaster'dan çözer.
- **Yeni `calendar` capability'si:** iki yarılıdır. `calendar --mic XIST --start --end`, seansları açılış/kapanış UTC zamanları ve takvim kalitesiyle döndürür. Kripto yarısı "her zaman açık" satırları üretir; böylece Gate 2 simetrisi korunur.
- **Dispatch:** sonu `@MIC` olan ve MIC'i kayıtta bulunan bir sembol equity olarak çözülür.
- **`universe-sync` ve `schedule`** `surfaces/operate.py` içinde operatör komutlarıdır. `schedule`, `--once` ile tek tik çalışır.

**Karar:** `universe-sync` ve `schedule`, `migrate-lake` gibi yerel durumu değiştiren veya uzun süre çalışan işler oldukları için capability değil operatör komutudur. **Yanılırsak bedeli:** REST ve MCP'de görünmezler; ileride iki yarılı capability'e terfi edilebilirler.

### 3.12 Mevcut kaynak düzeltmeleri

- **Yahoo (legacy, yfinance tabanlı).** Günlük barın tarihi, yfinance indeksinin kendi saat diliminde alınır; bu yerel seans tarihidir. `source_ts` bu tarihin UTC gece yarısı olur ve `ex_date` de bu tarihten gelir. `Fundamental.unit` için `financialCurrency` kullanılır; bu alan yoksa kayıt reddedilir. Sabitlenen fixture örnekleri: THYAO.IS, 7203.T, 2330.TW, BHP.AX ve ABD için AAPL.
- **Google Finance.** Aday listesi MIC → Google kodu tablosundan kurulur. `$` işareti MIC'in para birimine göre çözülür. Saat bilgisi olmayan zamanlar host'un değil listelemenin saat dilimine yerleştirilir.
- **`Provider.supported_mics`.** Varsayılan `None` (beyan yok) olur. ABD'ye özgü kaynaklar (alpaca, finnhub, finra, sec_edgar, tiingo, treasury) ABD MIC'lerini beyan eder. MSN de bu pakette ABD-only olarak beyan edilir.

**Karar:** MSN'in çok pazarlı desteği bu pakette yok. **Yanılırsak bedeli:** MSN yedeği yok; Yahoo ve TradingView yetmezse MSN Paket 5'te eklenir.

### 3.13 Provenance basis'leri

Yeni basis'ler:

| Basis | Seviye | Not |
|---|---|---|
| `session_snapshot` | DERIVED | — |
| `listing_directory` | NATIVE | — |
| `yahoo_chart_daily` | NATIVE | Mevcut `native` basis'i yeterliyse yeni basis açılmaz ve gerekçe ledger'a yazılır |

`_EXPECTED_BASES` güncellenir. `register_basis` kuralları DERIVED için kanal girdisi zorunlu tutuyorsa, seviye seçimi gerekçesiyle ledger'a yazılır.

## 4. Veri akışı örneği: THYAO

1. `crocodile universe-sync --country TR`:
   - TradingView `turkey` taraması ölçümde 650 satır döndürdü.
   - `BIST:THYAO` → (THYAO, XIST) → listing `THYAO@XIST` (TRY, ölçek 0).
   - Alias'lar: `tradingview:BIST:THYAO` ve gramerden hesaplanan `yahoo_chart:THYAO.IS`.
   - Ayrıca bir instrument snapshot kaydı yazılır.
2. `crocodile backfill --asset-class equity --sources universal --country TR`: Yahoo'dan tüm günlük geçmiş alınır ve `OHLCV(symbol=THYAO@XIST, interval=1d, currency=TRY, price_scale=0, source_ts=İstanbul seans tarihinin UTC gece yarısı)` kayıtları yazılır. Temettü ve bölünme olayları da kaydedilir.
3. `crocodile schedule --asset-class equity --country TR`: her seansta XIST kapanışından 45 dk sonra TradingView snapshot'ı alınır ve DERIVED günlük bar yazılır.
4. Okuma:
   - `crocodile query --asset-class equity "SELECT * FROM ohlcv WHERE symbol='THYAO@XIST'"`
   - `crocodile markets --asset-class equity --country TR`

## 5. Hata yönetimi

| Durum | Davranış |
|---|---|
| 403, bot sayfası, proof-of-work, Cloudflare, zaman aşımı | Aşılmaz. `unservable` ve gerekçe; çalıştırma raporunda sayılır |
| 429 | Paylaşılan bütçe geri çekilir, `TransientProviderError` |
| Bilinmeyen MIC, para birimi veya sonek | `UnknownMarketError`, `UnknownCurrencyError` veya `NoGrammarForMarket`; tahmin yapılmaz |
| Yaptırımlı borsa | Opt-in yoksa `SanctionedMarketError` |
| Kimlik çakışması | `ListingConflictError` |
| Kayıt ile listing para birimi çelişkisi | Kayıt yazılmaz, DLQ'ya gider |
| Zamanlayıcıda tekrarlayan hata | N denemeden sonra gap kaydı yazılır ve filigran ilerler |

## 6. Test ve kabul

Her görev testi önce yazar (RED→GREEN). Çevrimdışı testler `network_forbidden` fixture'ı altında çalışır. Fixture kaynakları:
- Ar-Ge'nin ham Yahoo chart yanıtları: THYAO.IS, 7203.T, 2330.TW, BHP.AX, PTT.BK, NOVO-B.CO, ALV.F, NOKIA.HE.
- TradingView scanner yanıtları.
- Kısaltılmış dizin örnekleri.

Eklenecek kapı testleri:
- `exchanges.csv` bütünlüğü ve MANIFEST sha256 doğrulaması.
- Gramer gidiş-dönüşü.
- `(mic, local_symbol)` tekilliği.
- Para birimi tutarlılığı.
- Her uygulamanın `mic`/`country` parametresini süzmesi ya da reddetmesi.
- Takvim çapraz testleri (XNYS, BIST 2026).
- Yeni basis'lerin kaydı.
- Lake simetrisi ve eski/yeni bölümlerin karışık okunması.
- `core/markets` katmanlama testi.
- Prose sayıları.

Kalite kapıları:
- Yeni temiz paketlerde ruff 0 bulgu.
- `core` ve yeni paketlerde mypy strict temiz.
- Legacy ruff bütçesi (202) aşılmaz.
- Tam pytest sonucu değişiklik öncesi baseline ile karşılaştırılır. Windows'ta önceden var olan toplanamama durumları "önceden var olan sınır" olarak kaydedilir, başarı sayılmaz.

Canlı ölçüm CI'da değil, yürütme raporunda yer alır. Kapsam:
- Ülkeler: TR, DE, GB, JP, IN, BR, ZA, NG, KE, VN, AU ve US.
- İşlemler: universe-sync, listing başına küçük örneklerle backfill ve EOD toplama.
- Kaynak başına kaydedilenler: HTTP durumları, süreler, sayılar, hatalar.
- Kapsam iddiaları yalnız ölçülen çalıştırmalardan çıkarılır.

Kabul ölçütleri (hepsi sağlanmalı):

1. Aynı yerel sembollü iki listeleme ayrı tutulur (`000001@XSHE` ≠ `000001@XSHG`).
2. `THYAO@XIST`, `7203@XJPX`, `2330@XTAI` ve `BHP@XASX` günlük barları doğru yerel tarihe düşer (fixture). Legacy Yahoo da aynı tarihi üretir.
3. Pence ile kote edilen bir LSE listelemesi `currency=GBP`, `price_scale=2` olarak yazılır (fixture).
4. `markets --asset-class equity`, `exchanges.csv`'deki tüm MIC'leri kaynak, kullanım sınıfı ve takvim kalitesiyle listeler. Kripto yarısı `mic` aldığında reddeder.
5. Canlı ölçümde en az 10 ülkede universe-sync listeleme sayısı sıfırdan büyüktür ve backfill örneklerinde bar yazılır. Yeniden başlatılan backfill kaldığı yerden devam eder (çevrimdışı test ve canlı gözlem).
6. BIST 2026 resmî tatilleri takvimle uyumludur. Fark varsa açıkça listelenir.
7. Tüm kapılar yeşildir ya da önceden var olan bir Windows sınırı olarak kayıtlıdır.
8. Belgeler tamamdır: README "Global equities" bölümü, CHANGELOG `[Unreleased]`, araştırma belgesi ve yürütme raporu.

## 7. Riskler

- **Gri kaynaklar kırılgandır.** Yahoo ve TradingView uç noktaları değişebilir veya erişimi engelleyebilir. Karşılığı: kaynak sırası, `unservable` gerekçeleri ve canlı ölçüm.
- **Yahoo listing başına bir istek gerektirir.** Bütün evrenin backfill'i bir günden uzun sürebilir (≈60-70 bin listing × ~1,5 sn). Bu yüzden backfill resumable'dır ve günlük EOD için TradingView'ın pazar başına snapshot'ı kullanılır.
- **TradingView'ın seans ataması sezgiseldir.** Bu yüzden DERIVED olarak işaretlenir ve gerekçesi yazılır.
- **Takvim kütüphanesi kısmi kapsar.** 176 borsanın 62'sini kapsıyor; kalanlar weekday fallback ile çalışır ve kalite sütununda görünür.
- **Kullanım şartları:** gri kaynaklar kullanıcı kararıyla varsayılan açık. Kullanım sınıfı beyan edilir; verinin dışarıya dağıtılmasının sorumluluğu kullanıcıdadır.
- **Windows:** bazı test modülleri toplanamıyor. Durum baseline'da kayıtlı.

## 8. Uygulama sırası

| Aşama | İçerik |
|---|---|
| A, temel | Referans verisi ve `core/markets`, takvim, listing master, gramer, şema, legacy Yahoo/Google düzeltmeleri, yüzey parametreleri, zamanlayıcı |
| B, universal | `yahoo_chart`, `tradingview`, dizinler, kaynak sırası, orkestratör, operatör komutları |
| C, doğrulama | Canlı ölçüm, tam kapılar, bütün dalın son incelemesi, belgeler ve yürütme raporu |

Plan: `docs/superpowers/plans/2026-11-08-global-equity-plan.md`.

## Self-review

- Kullanıcının her kararı bir bileşene karşılık geliyor.
- Kapsam dışı işler paket numarasıyla adlandırıldı.
- Ölçülmemiş hiçbir şey kabul ölçütü olarak yazılmadı. TradingView sayıları 8 Kasım ölçümüdür, Yahoo'nun bölünme davranışı ölçülerek karara bağlanacak.
- Gri kaynakların varsayılan açık olması ile erişim kontrolünü aşmama kuralı birbiriyle çelişmiyor.
- Kararlar kapsamı daralttı (MSN, ham HTTP arşivi, ana birime çevrilmiş görünüm), genişletmedi.
- Açık kalan tek şey, ölçümle karara bağlanacak olan Yahoo `adjustment` değeri.
