# Küresel hisse veri hattı (Paket 1+2) uygulama planı

> **Ajanlar için:** Bu plan superpowers:subagent-driven-development ile görev görev yürütülür. Adımlar `- [ ]` kutularıyla izlenir; kutuları controller işaretler.

**Hedef:** Crocodile'ın ABD dışındaki hisse piyasalarından da veri alabilmesi: piyasadan bağımsız bir referans katmanı (`core/markets`), çok para birimli şema, ABD varsayımlarından arındırılmış mevcut kaynaklar, MIC/ülke boyutlu yüzeyler, borsa takvimine bağlı zamanlayıcı, universal kaynaklar (Yahoo chart, TradingView scanner, resmî dizinler), bunları süren orkestratör, canlı ölçüm ve belgeler.

**Mimari:** kaynak → `SymbolGrammar` (kaynak sembolü ↔ operating MIC + yerel sembol) → `ListingMaster` (issuer → instrument → listing) → `provenance_fields()` ile kanonik kayıt (`currency`, `price_scale`, `adjustment`) → `ParquetSink` (`source=/channel=/date=/bucket=`, kimse kimseyi ezmez) → okuma anında (MIC, kanal) başına beyan edilmiş kaynak sırası (`CoverageResolver`). Zamanlama: `ExchangeCalendar(MIC)` → görev başına takvimli `ScheduledPullCoordinator` → universal EOD toplama.

**Teknoloji:** Python 3.12, msgspec, polars/DuckDB Parquet lake, aiohttp, stdlib sqlite3, `exchange_calendars` 4.13.2 (pandas 3.0.3'te kilitli), typer, pytest/pytest-asyncio.

**Spec:** [Onaylanan tasarım](../specs/2026-11-08-global-equity-design.md); repo karşılığı `docs/superpowers/specs/2026-11-08-global-equity-design.md`. Araştırma: `docs/superpowers/research/crocodile-kuresel-borsa-arastirmasi-2026-11-08.md` ve `docs/superpowers/research/data/market-matrix-2026-11-08.json`.

**Başlangıç:** `origin/main` 5600c87 → tasarım commit'i `650e8ac`. Dal `feat/global-equity`, worktree `C:/Users/Kullanıcı/.gemini/antigravity/scratch/crocodile-global-equity`. Ana checkout ve başka worktree'lere dokunulmaz; push yapılmaz.

**Arayüz sözleşmesi:** `.superpowers/global-equity/interfaces.md` — görevler arası her arayüzün (modül yolu, imza/alanlar, sahip görev, tüketen görevler), dosya sahipliğinin ve çatışma kararlarının tek kanonik kaydı. Brief'ler bu dosyayla uyumlu hale getirildi; uyuşmazlıkta `interfaces.md` kazanır.

**Görev brief'leri / raporlar / ledger:** `.superpowers/global-equity/briefs/task-NN-brief.md` (08, 10, 11 ve 14 için dağıtılan parça brief'leri `task-NNx-brief.md`; tam brief'ten `.superpowers/global-equity/split_briefs.py` ile kesilir ve tam brief her düzenlendiğinde yeniden üretilir) · `.superpowers/global-equity/reports/task-NN-report.md` (parçalarda `task-NNx-report.md`) · `.superpowers/global-equity/ledger.md`. Hazırlanmış girdiler (fixture'lar, ISO dosyaları, dizin örnekleri, TradingView probe'ları): `.superpowers/global-equity/inputs/`; kod haritası `inputs/codemap.md`.

## Yürütme modeli

- **Subagent-driven development.** Controller görevleri aşağıdaki sırayla, tek tek dağıtır. Her görev için bir implementer brief'i baştan sona okur, testleri önce yazıp kırmızıyı görür (RED), sonra uygular (GREEN), brief'teki doğrulama komutlarını çalıştırır, brief'teki commit'leri atar ve tam raporu `reports/task-NN-report.md`'ye yazar. Dönüş değeri yalnız durum (DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED), commit'ler, tek satırlık test özeti ve kaygılardır. Implementer başka subagent çağırmaz.
- **Model politikası (ledger kararı).** Implementer'lar önce Haiku ile çalışır; 4.-5. düzeltme turunda veya akıl yürütme gerektiren BLOCKED durumunda Opus'a yükseltilir. Planlayıcı, inceleyici ve hakem Opus'tur. Sonnet kullanılmaz.
- **Önce baseline (karar 31).** Task 01 dağıtılmadan önce controller 650e8ac `run_suite` baseline'ını tamamlar (`$PY .superpowers/global-equity/run_suite.py C:/Users/Kullanıcı/.gemini/antigravity/scratch/crocodile-baseline <out> 900 3`) ve `summary.json` yolunu ve toplamlarını `ledger.md`'ye yazar. Brief'ler hataları bu baseline'la test kimliğine göre karşılaştırır; task 01'den sonra alınmış bir Step-0 baseline'ıyla asla karşılaştırmaz (önceki görevin regresyonunu yutar). Baseline'da olmayan bir başarısız kimlik, görevin başlangıç HEAD'inde zaten kırmızı olsa bile yenidir; görevin kendi sebep olmadığı biri kaygı olarak raporlanır.
- **Zorunlu bölmeler (karar 30).** 08, 10, 11 ve 14 hiçbir zaman tek parça dağıtılmaz: 08a → 08b → 08c, 10a → 10b, 11a → 11b, 14a → 14b → 14c. Her parça tam brief'ten aynı Interfaces ile kesilmiş kendi part brief'inden (`briefs/task-NNx-brief.md`) çalışır, kendi raporunu (`reports/task-NNx-report.md`) yazar ve ayrı incelenir; sonraki parça öncekinin incelemesi kapanınca başlar. Tam brief inceleyiciler ve son inceleme için referans kalır. Bir parça başka parçanın adımlarını yapmaz ve ortak bir dosyayı (`factory.py`, `ops.py`, `test_gates.py`, `MANIFEST.json`) yarım bırakmaz.
- **Görev incelemesi.** Her görevden sonra bağımsız bir inceleyici diff'i (`<önceki HEAD>..HEAD`), raporu, brief'in Kabul listesini ve `interfaces.md`'yi karşılaştırır: spec uyumu, sözleşmeye sadakat, test kalitesi (RED gerçekten görüldü mü), kalite kapıları. Bulgular aynı implementer'a düzeltme turu olarak döner; kapanınca controller ledger'a bir karar satırı ve kutuya işaret ekler.
- **Son inceleme.** 17 görev bitince bütün dal (`650e8ac..HEAD`) tek bağımsız incelemeden geçer; önemli bulgular tek düzeltme geçişinde testlerle kapatılır.
- **Ledger.** `.superpowers/global-equity/ledger.md` controller kararlarını (`Ruling: … — why: … — cost if wrong: …`), baseline'ı ve görev başına durum/commit/inceleme satırını tutar. Kullanıcının "Çok soru sorma, teslim edene kadar devam et" talimatı nedeniyle açık sorular kullanıcıya gitmez; `interfaces.md` §4'teki kararlar uygulanır ve ledger'a yazılır.
- **Commit.** Görev commit'lerini implementer atar; bu plan dosyasını, ledger'daki karar notlarını ve son yürütme raporunu controller `git add -f` ile commit eder (`docs/superpowers` gitignore'da).

## Global kısıtlar (her görevi bağlar)

- Yeni kod yalnız temiz paketlere girer: `src/crocodile/core/markets/` ve `src/crocodile/equity/universal/`. İkincisi, onu yaratan commit'te `tests/conformance/test_phase1_exit.py` içindeki `_CLEAN_PACKAGES`'a ve `pyproject.toml`'a `[[tool.mypy.overrides]] module = ["crocodile.equity.universal.*"] ignore_errors = false` olarak eklenir (görev 10). Legacy düzenleme yalnız spec'in istediği yerlerde yapılır.
- `core/markets` hiçbir koşulda `crocodile.crypto`, `crocodile.equity`, `crocodile.capabilities`, `crocodile.surfaces` veya `crocodile.contrib` import etmez (görev 01'in katmanlama kapısı).
- Kalite: temiz paketlerde ruff (E,F,I,UP,B,ASYNC,RUF; satır 100) sıfır bulgu, mypy strict temiz. Legacy ruff sayısı (`src/crocodile/crypto` + `src/crocodile/equity`) base'de 193; `_LEGACY_RUFF_BUDGET = 202` hiç değiştirilmez ve sayı büyümez.
- TDD: her görev önce başarısız testi yazar ve çalıştırır, sonra uygular. Çevrimdışı testler mevcut `network_forbidden` fixture'ını kullanır; fixture'lar `inputs/`'tan repo'nun `tests/**/fixtures/` düzenine kopyalanır. Kayıt kurucuları saat okumaz (yakalama zamanı dışarıdan verilir). Kayıtlar yalnız `provenance_fields()` ve kayıtlı bir basis ile üretilir (`@register_basis` + `tests/conformance/test_provenance.py` içindeki `_EXPECTED_BASES`).
- Korunacak/güncellenecek kapılar: `test_gates.py`, `test_prose_counts.py`, `test_provider_channels.py`, `test_channel_writability.py`, `test_record_parity.py` (+ `premerge_record_schema.json`), `test_lake_symmetry.py`, `test_surfaces.py`, `test_capability_batches.py`, `test_pending_symmetry.py`, `test_provenance.py`, `test_phase1_exit.py`, `test_evasion_quarantine.py`, `test_ratelimit_placement.py`.
- Yeni `.py` dosyaları yalnız ASCII içerir (`\uXXXX` kaçışları); her dosya okuması `encoding="utf-8"` verir.
- Canlı ağ yalnız brief'in izin verdiği yerde: uç nokta başına çalıştırma başına en çok 3 istek (keşif taramaları aday başına 1), ~1 sn aralık, genel UA `Mozilla/5.0 (crocodile)`, kişisel veri yok; CAPTCHA/bot/proof-of-work/coğrafi engel asla aşılmaz, kayıt olunmaz, anahtar kullanılmaz. Canlı gözlemler rapora girer, çevrimdışı testlerin assertion'larına girmez.
- Commit'ler İngilizce, conventional önekli (`feat:`, `fix:`, `test(scope):`, `docs:`), her mesaj boş satır + `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` ile biter. Yalnız açık yollar stage edilir (`git add -A`/`git add .` yok).
- `README.md` ve `CHANGELOG.md` yalnız görev 16'nındır; diğer görevler yapıştırılmaya hazır satırları raporlarına yazar.

## Doğrulama ortamı (ledger kararı, brief komutlarının üstündedir)

```bash
cd "C:/Users/Kullanıcı/.gemini/antigravity/scratch/crocodile-global-equity"
export PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$(cygpath -w "$PWD/.superpowers/global-equity/winshim")"
PY=.venv/Scripts/python.exe
```

- Yorumlayıcı `.venv/Scripts/python.exe`'dir; `.venv/bin` yoktur. Komutlar `$PY -m pytest|ruff|mypy` biçimindedir.
- `winshim` (no-op `fcntl`, sahte `resource.getrusage`) git dışında kalır; commit edilmez, repo kodundan import edilmez. Sonuçlar "Windows+shim" diye etiketlenir, Linux CI değildir.
- UTF-8 modu olmadan çalışması istenen komutlar `env -u PYTHONUTF8 …` ile çalışır (shim kalır).
- CLI-subprocess testleri veya bütün bir conformance dizini koşuyorsa `--timeout=1800` verilir; bütün test takımı varsayılan zaman aşımıyla hiç koşturulmaz.
- Hatalar 650e8ac per-file `run_suite` baseline'ıyla (`ledger.md`'de kayıtlı `summary.json`, `bad` listesi) **test kimliğine göre** karşılaştırılır; task 01'den sonra alınmış bir Step-0 baseline'ı karşılaştırma tabanı olamaz, yalnız görevin başlangıç durumunu kaydeder (karar 31). Brief'lerdeki shim'siz "29 failed / 1192 passed" sayıları geçersizdir.
- `test_phase1_exit._ruff` `.venv/bin/python` çağırdığı için çalışmaz; lint kapısı doğrudan çalıştırılır: temiz taraf `$PY -m ruff check --output-format=concise src/crocodile/core src/crocodile/contrib src/crocodile/capabilities src/crocodile/surfaces src/crocodile/__init__.py src/crocodile/_deprecated_cli.py src/crypcodile src/stockodile` (+ görev 10'dan itibaren `src/crocodile/equity/universal`) → `All checks passed!`; legacy taraf `$PY -m ruff check --output-format=concise src/crocodile/crypto src/crocodile/equity | grep -c ": "` → Step 0 sayısını aşmaz (≤ 202).
- Tam mypy (`$PY -m mypy -p crocodile`) yalnız `src/crocodile/surfaces/server.py`'deki iki eski Windows hatasını (`getrusage`, `RUSAGE_SELF`) gösterebilir.

## Görev sırası ve bağımlılıklar

Sıralı yürütme. Hiçbir görev kendinden sonra gelen bir görevin ürettiği şeyi tüketmez. 17, 16'dan önce koşar, çünkü belgeler canlı ölçümü kaynak gösterir. İlk satırdan önce controller 650e8ac baseline'ını tamamlar (karar 31). 08, 10, 11 ve 14 zorunlu parçalarıyla koşar (karar 30); 11b, 12 ve 13'ten önce iner, çünkü 13'ün `build_source_order.py --check`'i TradingView `supported_mics` literalini okur.

| Sıra | Görev / parça | Bağımlılıklar | Aşama |
|---|---|---|---|
| 1 | 01 core/markets paketi, referans verisi, kayıtlar, hatalar | — | A |
| 2 | 02 kanonik sembol, kullanım sınıfı, yaptırım politikası | 01 | A |
| 3 | 03 borsa takvimleri (+ `exchange-calendars` bağımlılığı) | 01 | A |
| 4 | 04 ListingMaster (+ salt-okunur açılış); SecurityMaster/Tiingo ABD kapsamı | 01, 02 | A |
| 5 | 05 kaynak sembol gramerleri ve tabloları | 01, 02 (betik için 03) | A |
| 6 | 06 şema Stage B; `check_currency_consistent` | 01 (04 yalnız yapısal) | A |
| 7 | 07 legacy Yahoo/Google düzeltmeleri; `Provider.supported_mics` | 01, 02, 05 | A |
| 8 | 08a parametreler, kripto retleri, equity `markets` ve `universe` (commit 1) | 01–05, 07 | A |
| 9 | 08b `calendar`, Gate 2 sayımı ve prose kapıları (commit 2) | 08a | A |
| 10 | 08c `LOCAL@MIC` dispatch ve market-boyut kapısı (commit 3-4) | 08b | A |
| 11 | 09 zamanlayıcı takvimleri, paylaşılan bütçe, kalıcı equity DLQ | 01, 03 | A |
| 12 | 10a `equity/universal` paketi, kapılar, `base.py`, `http.py` (commit 1-2) | 01–07, 09 | B |
| 13 | 10b `yahoo_chart`, factory, AAPL kaydı, NVDA ölçümü (commit 3-4) | 10a | B |
| 14 | 11a `session_snapshot`, `tradingview` modülü ve sağlayıcı; R&D probe'ları ve etiketli sentetik fixture'larla (commit 1-2) | 01–07, 09, 10b | B |
| 15 | 11b canlı metainfo, tarama ve EOD kayıtları, ölçülmüş pazar tablosu, gramer satırları, probe'lar, MANIFEST, `supported_mics` literali (commit 3a ve 3) | 11a | B |
| 16 | 12 resmî dizinler + `listing_directory` | 01–07, 09, 10, 11b | B |
| 17 | 13 kaynak sırası ve okuma anında federasyon | 01–04, 06, 07, 10–12 | B |
| 18 | 14a `state.py`, `fakes.py`, kapsam, `sync_universe`, `backfill` (commit 1 ve 2a) | 01–13 | B |
| 19 | 14b `collect_eod`, `collect_intraday`, `bindings.py`, `open_orchestrator` (commit 2b) | 14a | B |
| 20 | 14c `ops.py`, `test_ops_universal.py`, `collect`/`backfill` probe'ları (commit 3) | 14b | B |
| 21 | 15 `universe-sync`/`schedule`; `markets` kapsam sütunları | 01–04, 08, 09, 13, 14 | B |
| 22 | 17 canlı ölçüm betiği ve ölçülmüş çalıştırma | 01–15 | C |
| 23 | 16 README, CHANGELOG `[Unreleased]`, paket metadatası | 01–15, 17 | C |

## Ortak dosya sahipliği (özet; tam tablo `interfaces.md` §3)

- `.gitignore` negasyonları ve `.gitattributes`: 01 (10 ve 13 yalnız `git check-ignore` ile doğrular).
- `pyproject.toml`: bağımlılık satırı 03; mypy override 10; `description`/`keywords` 16; `version` 0.3.2 kalır.
- `_CLEAN_PACKAGES`: yalnız 10. `_LEGACY_RUFF_BUDGET`: kimse.
- `core/markets/errors.py` ve `currency.py`: 01; 06 sona ekler. `MANIFEST.json`: 01 üretir, 05 ve 11 yeniden üretir. `grammar_tradingview.csv`: 05; 11 satır ekler.
- `core/markets/master.py`: 04 (`open_sqlite_readonly` ve `readonly=True` dahil; 08 ve 15 tüketir). `tests/equity/fixtures/yahoo_chart/` ve `_yfinance_daily_frame`: 07; 10b yalnız `aapl.json` ekler ve yardımcıyı içe aktarır.
- `equity/providers/base.py`: 07 (`supported_mics` ailesi), 09 (`use_durable_dlq`). `factory.py`: 07 `supported_mics`, 09 `make_provider(data_dir=)`, 10/11/12 import + `_REGISTRY` + "Valid values:", 13 `provider_class`.
- `core/ratelimit/budget.py` ve altı universal kaynağın `DEFAULT_BUDGETS` satırları: 09 (10-12 dokunmaz).
- `capabilities/ops.py`: 09 DLQ bağlantısı; 14 parametreler ve universal dallar (09'un bağlantısı korunur). `capabilities/market.py`: 08; 15 `markets` sütunlarını genişletir.
- Prose sayıları: 08 (`capabilities/__init__.py`, `dispatch.py`, `mcp.py`, `rest.py`, Gate 2 nüfus sayımı, Phase 2/canlı sayı kapıları); 10-12 ("Valid values:"); 15 (`operate.py` "These seven answer questions"); 16 (README/CHANGELOG ve `test_prose_counts.py`'nin "Global equities" bloğu). `_REGISTRY_AS_SHIPPED` docstring'i "The 49 capabilities Phase 2 shipped" olarak kalır.
- `test_gates.py`: 08 Gate 2 / `_ADDED_AFTER_PHASE_2`; 11 `DERIVES_RECORDS`; 12 `CONSTANT_BY_DEFINITION`. `test_provenance.py`: 11 ve 12. `test_market_dimensions.py`: 08; 14 iki probe ekler.
- `README.md`, `CHANGELOG.md`, iki tagline: yalnız 16.

## İnceleme odağı

1. Satır sonu ve hash'ler: CRLF yayınlanan ISO dosyaları ile LF üretilen dosyaların `MANIFEST` sha256'sı (`.gitattributes -text`), `.gitignore:22 data/` yüzünden wheel'den düşen paket verisi (görev 01, 05, 11).
2. Gün atlaması: daha doğudaki borsaların günlük bar ve ex-date'leri UTC'ye çevrilip tarihlendiğinde bir gün erken düşer; legacy Yahoo (07) ve `yahoo_chart` (10) aynı yerel seans tarihini üretmeli (kabul 2) ve bunu aynı payload'ları iki yola veren parite testi kanıtlar (10b, karar 32).
3. Birim: `price_scale` hiçbir yerde USD veya 0 varsayılmaz; pence/ZAc/ILA `parse_quote_unit`'ten geçer; kayıt ile listing çelişkisi DLQ'ya gider (06, 10, 14).
4. Kimlik: `(mic, local_symbol)` tekilliği, `ListingConflictError` transaction içinde hiçbir şey yazmadan, alias geçerlilik aralıkları, `mark_unseen` asla `delisted` üretmez (04, 10, 12, 14).
5. Yaptırım: açıkça istenen yaptırımlı borsa hiçbir istekten önce reddedilir; opt-in yalnız `Settings` üzerinden okunur (02, 10, 11, 12, 14).
6. Takvim: kütüphane penceresi dışı `CalendarRangeError`, `WEEKDAY_FALLBACK` gününde eksik veri gap değil `no_data`, scheduler ile `core.markets.calendar` arasında import döngüsü yok; satır zaman damgası takvimin bir sonraki açılış korumasının yerine geçer ve atanamayan pazar işaretsiz kalıp tarih yedeğine gider (03, 09, 11, 14, 15; karar 29).
7. Yüzey simetrisi: kripto yarıları `mic`/`country`'yi görmezden gelmez, `CapabilityUnavailable` ile reddeder; okuma yetenekleri dosya yaratmaz, SQLite yan dosyası (`-wal`/`-shm`) da bırakmaz (`markets`, `universe`, preflight; `open_sqlite_readonly`, karar 27) (04, 08, 14, 15).
8. Devam edebilirlik: watermark yalnız sink flush'ından sonra commit edilir; kesilen backfill tamamlanan listing'leri yeniden çekmez (14, 17).
9. Erişim disiplini: genel UA, yönlendirme izlenmez, HTML/bot sayfası `unservable: bot_challenge`, 429'da paylaşılan bütçe geri çekilir; ölçüm defteri uç nokta başına 3 isteği aşmaz (10, 11, 12, 17).
10. Prose: README/CHANGELOG'daki her sayı, küme, komut, yol ve üye canlı nesneye karşı kapıyla doğrulanır (08, 15, 16).
11. Olay döngüsü ve saat: hiçbir orkestratör, istemci, fetcher, sink veya kova bir `asyncio.run`'dan uzun yaşamaz (17'nin faz çağrıları, 10'un `AiohttpFetcher`'ı; karar 28); `schedule`'da koordinatör ve orkestratör aynı saati okur (`_make_orchestrator` `clock=lambda: _now()`, 14'ün `fakes.make_orchestrator` varsayılanı `FIXED_NOW`) (10, 14, 15, 17).

---

## Task 01: core/markets paketi, referans verisi ve kayıtlar

**Bağımlılıklar:** yok. **Brief:** `briefs/task-01-brief.md`.

**Dosyalar:** oluştur `src/crocodile/core/markets/{__init__,errors,_data,mic,exchanges,currency}.py`, `src/crocodile/core/markets/data/{iso10383_mic.csv,iso4217.csv,exchanges.csv,sanctions.csv,MANIFEST.json}`, `scripts/build_markets_reference.py`, `scripts/markets_reference/{curation.csv,iso4217_list_one.xml}`, `.gitattributes`, `tests/core/markets/{__init__,test_market_errors,test_mic,test_exchanges,test_currency}.py`, `tests/conformance/{test_markets_reference_data,test_markets_layering}.py`; değiştir `.gitignore` (iki negasyon).

**Arayüzler:** on bir hata sınıfı (`MarketError` kökü; `UnknownMarketError`, `ExpiredMarketError`, `NoGrammarForMarket(source, mic)`, `ListingConflictError` …), `MicRegistry`/`MicEntry`, `ExchangeRegistry`/`ExchangeInfo` (`get`, `resolve`, `by_country`, `all`), `require_currency`, `QuoteUnit`, `parse_quote_unit`, `to_major` (`interfaces.md` §5.1-5.6).

- [ ] **Test yaz:** hata hiyerarşisi, mesajlar, pickle/copy; MIC kaydı (XTUR → `ExpiredMarketError` "use XIST", XEQY → XIST, INSE aktif, tembel yükleme); borsa kaydı (XIST alanları, segment reddi, `resolve`, DE 9 satır, DRSP reddi); para birimi tablosu (GBp/GBX/ZAc/ILA, büyük/küçük harf duyarlı, tahmin yok; `to_major` kesin); veri kapısı (MANIFEST sha256, 172 satır, 64 `xcal_code`, `quote_scale` 2 yalnız XJSE/XLON/XTAE, yaptırımlı satırlar, notes token'ları); katmanlama kapısı (AST + taze yorumlayıcı).
- [ ] **Implement:** önce `errors.py`; sonra `.gitattributes` ve `.gitignore` negasyonları, girdilerin birebir kopyası, `sanctions.csv`/`curation.csv`, deterministik `build_markets_reference.py` (geçici `uv run --no-project --with exchange-calendars==4.13.2 --with pandas==3.0.3`); sonra `_data.py`, `mic.py`, `exchanges.py`, `currency.py`.
- [ ] **Doğrula:** `$PY -m pytest tests/core/markets tests/conformance/test_markets_reference_data.py tests/conformance/test_markets_layering.py`, aynısı `env -u PYTHONUTF8` ile; `build_markets_reference.py --check` çıkış 0; `git ls-files --eol` → `attr/-text`; wheel kontrolü beş veri dosyasını içerir; ruff/mypy (`-p crocodile.core`).
- [ ] **Commit:** `feat(markets): add the core/markets error family` · `feat(markets): package ISO 10383, ISO 4217, exchange and sanctions reference data` · `feat(markets): add MIC and exchange registries and quote-unit parsing` · `test(markets): lock core/markets out of the market and surface layers`.

**Kabul:** hata ailesi ve kayıtlar Interfaces ile birebir; `iso10383_mic.csv` girdiyle bayt bayt aynı; üretilen veri `--check` ile tekrarlanabilir; katmanlama kapısı yeşil; legacy ruff 193.

## Task 02: kanonik sembol, kullanım sınıfı ve yaptırım politikası

**Bağımlılıklar:** 01. **Brief:** `briefs/task-02-brief.md`.

**Dosyalar:** oluştur `src/crocodile/core/markets/{symbols,usage,sanctions}.py`, `tests/core/markets/{test_symbols,test_usage,test_sanctions}.py`; değiştir `src/crocodile/core/config.py` (`Settings.allow_sanctioned_markets` + `_VARS`), `tests/conformance/test_config.py` (dört test).

**Arayüzler:** `validate_local_symbol`, `listing_symbol` (ABD → çıplak ticker, aksi `{yerel}@{operating MIC}`), `parse_listing_symbol` (yalnız kanonik), `is_listing_symbol`; `UsageClass` (`redistribution`, `display`, `internal`), `most_restrictive`; `sanctioned_countries`, `parse_allowed_countries`, `allowed_countries(settings)`, saf `is_sanctioned`/`require_not_sanctioned` (§5.7-5.9).

- [ ] **Test yaz:** `THYAO@XIST`, `7203@XJPX`, `CERA.N0000@XCOL`; segment → operating MIC; ABD çıplak ticker; `000001@XSHE ≠ 000001@XSHG` (kabul 1); NFC ve yasak karakterler; kanonik olmayan biçimlerin reddi; 172 borsanın gidiş-dönüşü; yaptırım varsayılan kapalı (segment ve süresi dolmuş MIC dahil), opt-in yalnız adı geçen ülke, `ConfigError` değişken adını söyler; `exchanges.csv` ile politika uyumu; `Settings` alanı.
- [ ] **Implement:** `usage.py`, `symbols.py`, `config.py` alanı, `sanctions.py` (ortam yalnız `Settings` üzerinden).
- [ ] **Doğrula:** `$PY -m pytest tests/core/markets tests/conformance/test_config.py tests/conformance/test_markets_layering.py tests/conformance/test_markets_reference_data.py`; yeni dosyalar `env -u PYTHONUTF8` ile de; ruff/mypy.
- [ ] **Commit:** `feat(markets): add source usage classes` · `feat(markets): add canonical local@MIC listing symbols with frozen US tickers` · `feat(markets): keep sanctioned markets closed unless CROCODILE_ALLOW_SANCTIONED_MARKETS opts in`.

**Kabul:** imzalar birebir; kanonik biçim katı; yaptırım saf ve varsayılan kapalı; `Settings.allow_sanctioned_markets` `known_names()` ve `repr`'de.

## Task 03: borsa takvimleri

**Bağımlılıklar:** 01. **Brief:** `briefs/task-03-brief.md`.

**Dosyalar:** oluştur `src/crocodile/core/markets/calendar.py`, `tests/core/markets/test_calendar.py`, `tests/conformance/test_calendar_cross_checks.py`; değiştir `pyproject.toml` (bağımlılık + yorum), `uv.lock` (`uv add --no-sync`), `scripts/build_markets_reference.py` (yalnız docstring).

**Arayüzler:** `CalendarQuality`, `ExchangeCalendar` protokolü, `LibraryCalendar`, `WeekdayFallbackCalendar`, `AlwaysOpenCalendar`, `InHouseCalendar` (tembel `USMarketCalendar`), `calendar_for(mic)` (§5.10).

- [ ] **Test yaz:** protokol uyumu; `xcal_code` varsa kütüphane, yoksa weekday fallback (XCAI Cuma-Cumartesi tatili); ucuz özellikler kütüphaneyi kurmaz; DST-doğru UTC açılış/kapanış; öğle araları; XBOG saat dilimi; XTAE hafta maskesi geçmişi; `latest_completed_session`; aralık dışı `CalendarRangeError`, seans olmayan gün `NotASessionError`, `datetime` → `TypeError`; çapraz testler: XNYS 2024-2027 farkları tam olarak {2025-01-09, 2027-12-31}, BIST 2026 kapalı ve yarım günleri boş fark sözlüğüyle (kabul 6), 64 kütüphane takviminin hepsi kurulur, saat dilimleri XBOG dışında uyumlu, `--check` çıkış 0.
- [ ] **Implement:** `uv add --no-sync "exchange-calendars>=4.13.2"`, pandas 3.0.3'te kalır, `uv sync --locked --all-extras`; `calendar.py` (tembel import, `iterrows` yok, pencere sınırlara kırpılır).
- [ ] **Doğrula:** `$PY -m pytest tests/core/markets tests/conformance/test_calendar_cross_checks.py tests/core/test_scheduler.py`; `$PY scripts/build_markets_reference.py --check`; `uv sync --dry-run --locked --all-extras` değişiklik yok; `uv lock --check`; ruff/mypy.
- [ ] **Commit:** `feat(markets): depend on exchange_calendars for exchange sessions` · `feat(markets): add exchange calendars with library, in-house, weekday and always-open quality` · `test(markets): cross-check calendars against in-house XNYS and BIST 2026`.

**Kabul:** kütüphane 4.13.2, pandas 3.0.3; çapraz testler yeşil ve farklar gerekçeli; in-house 2027-12-31 hatası düzeltilmeden listelenir (karar 12).

## Task 04: ListingMaster ve SecurityMaster'ın ABD kapsamı

**Bağımlılıklar:** 01, 02. **Brief:** `briefs/task-04-brief.md`.

**Dosyalar:** oluştur `src/crocodile/core/markets/master.py`, `tests/core/markets/test_master.py`, `tests/conformance/test_listing_identity.py`; değiştir `src/crocodile/equity/reference/{master,universe}.py`, `tests/equity/{test_reference,test_reference_universe}.py`, `tests/capabilities/test_market.py` (iki beklenti).

**Arayüzler:** `ListingMaster` (`upsert`, `get`, `find`, `resolve_alias`, `by_isin`, `listings`, `mark_unseen`, `events`, `aliases`, `for_data_dir`, `readonly=False`), `open_sqlite_readonly(path)` (WAL dosyası yoksa `?mode=ro&immutable=1`, varsa `?mode=ro`; karar 27), `ListingObservation`, `Listing`, `ListingStatus`, `default_listings_path` → `<data_dir>/reference/listings.sqlite`; legacy `TIINGO_US_EXCHANGES` (§5.11).

- [ ] **Test yaz:** iki pazarda aynı yerel sembol iki listing; segment MIC operating + segment olarak saklanır; ABD çıplak sembol; kanonik çakışma `ListingConflictError` ve hiçbir yazım yok; yeni para birimi güncelleme + tek olay, eskisi değiştirmez; alias geçerlilik aralıkları ve tarihsel `resolve_alias`; ISIN değişimi çakışma; `mark_unseen` kaynak ve MIC kapsamlı, boş taramayı reddeder; WAL ve şema sürümü; saat okumaz; salt-okunur master okuma sonrası dosya adlarını ve mtime'ları değiştirmez (`-wal`/`-shm` yok), canlı yazıcının checkpoint'lenmemiş satırını görür, yazımı ve olmayan dosyayı `StoreError` ile reddeder; SecurityMaster çapraz borsa yazımını reddeder; Tiingo ABD dışı etiketleri atlar ve sayar.
- [ ] **Implement:** `master.py` (tek bağlantı + `RLock`, `BEGIN IMMEDIATE`), legacy değişiklikler.
- [ ] **Doğrula:** `$PY -m pytest tests/core/markets tests/conformance/test_listing_identity.py tests/equity/test_reference.py tests/equity/test_reference_universe.py tests/capabilities/test_market.py` ve brief'teki kapı listesi; `ruff check src/crocodile/equity/reference` sıfır.
- [ ] **Commit:** `feat(markets): add ListingMaster keyed on operating MIC and local symbol` · `fix(reference): keep SecurityMaster and the Tiingo universe US-scoped`.

**Kabul:** DDL birebir; `000001@XSHE`/`000001@XSHG` kapı testi; salt-okunur açılış hiçbir dosya, yan dosya veya satır yaratmaz; SecurityMaster ve Tiingo evreni ABD kapsamında; legacy ruff büyümez.

## Task 05: kaynak sembol gramerleri ve tabloları

**Bağımlılıklar:** 01, 02 (betik `.venv` ile koşar: 03). **Brief:** `briefs/task-05-brief.md`.

**Dosyalar:** oluştur `src/crocodile/core/markets/grammar.py`, `src/crocodile/core/markets/data/grammar_{yahoo_chart,google_finance,tradingview}.csv`, `tests/core/markets/test_grammar.py`, `tests/conformance/test_symbol_grammar.py`, `tests/core/markets/fixtures/tradingview_probe/*.json` (12); değiştir `scripts/build_markets_reference.py`, `MANIFEST.json` (yeniden üretilir).

**Arayüzler:** `SymbolGrammar` (`to_source(local, mic, *, segment_mic=None)`, `from_source(raw, *, mic=None)`, `supported_mics()`), `SuffixGrammar`, `PrefixGrammar`, `load_grammar`, `GrammarError`, `UnknownSourceSymbol`, `AmbiguousSourceSymbol`; `NoGrammarForMarket` görev 01'in sınıfıdır ve yeniden dışa verilir (karar 1; §5.12).

- [ ] **Test yaz:** sonek ve önek gramerleri bellek içi satırlarla; bilinmeyen sonek `UnknownSourceSymbol`, ABD soneksiz `AmbiguousSourceSymbol` + `mic=` ipucu; `BRK.B` ↔ `BRK-B`; KOSDAQ `.KQ` segment satırı; tablo kapıları: her satır gidiş-dönüş, her MIC `exchanges.csv` operating MIC'i, MIC başına tek primary, Yahoo/Google tabloları matristen tekrar üretilebilir, TradingView satırları commit'lenmiş probe'larla desteklenir.
- [ ] **Implement:** `grammar.py`; betiğe saf `grammar_yahoo_chart_rows`/`grammar_google_finance_rows`; elle `grammar_tradingview.csv` (13 satır); `$PY scripts/build_markets_reference.py` ve `--check` (geçici `uv run` yalnız yedek; karar 6).
- [ ] **Doğrula:** `$PY -m pytest tests/core/markets tests/conformance/test_symbol_grammar.py tests/conformance/test_markets_reference_data.py tests/conformance/test_calendar_cross_checks.py` ve kapı listesi; `exchanges.csv`/`iso4217.csv` bayt bayt aynı; ruff/mypy.
- [ ] **Commit:** `feat(markets): add SymbolGrammar with suffix and prefix grammars` · `feat(markets): add yahoo_chart, tradingview and google_finance grammar tables`.

**Kabul:** Yahoo 55, Google 3, TradingView 13 satır beklenen bloklarla aynı; MANIFEST ve `--check` yeşil; gramer gidiş-dönüş kapısı yeşil.

## Task 06: şema Stage B ve para birimi tutarlılığı

**Bağımlılıklar:** 01 (04'ün `Listing`'i yalnız yapısal). **Brief:** `briefs/task-06-brief.md`.

**Dosyalar:** değiştir `src/crocodile/core/schema/{enums,records}.py`, `src/crocodile/core/store/parquet_sink.py` (yalnız docstring/yorum), `src/crocodile/core/markets/{errors,currency}.py` (sona ekleme), `src/crocodile/equity/reference/universe.py` (bir docstring), `tests/conformance/test_lake_symmetry.py`, `tests/capabilities/test_market.py`, `tests/equity/{test_reference_universe,test_sec_edgar}.py`, `tests/core/markets/test_market_errors.py`; oluştur `tests/schema/test_stage_b_fields.py`, `tests/store/test_stage_b_mixed_partitions.py`, `tests/core/markets/test_currency_consistency.py`.

**Arayüzler:** `PriceAdjustment` (`NONE`, `SPLIT`, `SPLIT_DIVIDEND`); yedi yeni `SecurityType` üyesi, `FundPeriod.H1/H2`; `OHLCV`/`Trade`/`Quote`/`CorporateAction`/`Instrument` sonuna `T | None = None` alanlar; `CurrencyMismatchError`; `ListingUnit`, `check_currency_consistent(record_currency, record_scale, listing)` (karar 2; §5.13).

- [ ] **Test yaz:** enum değerleri ve sırası; alanlar kaydın sonunda, opsiyonel, `None` varsayılanlı; msgspec gidiş-dönüş; eski (sütunsuz) ve yeni bölüm tek `Catalog` sorgusunda, `from_row` ile ve sıkıştırma sonrasında `NULL` okunur; yalnız eski bölümlü lake'te SQL binder hatası sabitlenir; census her `SecurityType`'ı sıfırda da raporlar; SEC H1/H2 korunur; tutarlılık tablosu (bilinmeyen taraf asla uyuşmazlık değildir).
- [ ] **Implement:** enumlar, kayıt alanları (yalnız sonda; `_Header` dokunulmaz), `parquet_sink.py`'ye "Nullable widening of the canonical family" kararı, hata sınıfı ve fonksiyon.
- [ ] **Doğrula:** `$PY -m pytest tests/schema tests/store tests/core tests/conformance/test_lake_symmetry.py tests/conformance/test_record_parity.py tests/capabilities/test_market.py tests/equity/test_reference_universe.py tests/equity/test_sec_edgar.py` ve kapı listesi; genişleyen kayıtların okur/yazarları: `$PY -m pytest --timeout=1800 tests/resample tests/sink tests/replay tests/ingest`; `$PY -m mypy -p crocodile.core.schema -p crocodile.core.store -p crocodile.core.markets`.
- [ ] **Commit:** `feat(schema): add currency, price scale, adjustment and listing identity fields` · `feat(markets): refuse records whose currency contradicts their listing`.

**Kabul:** `premerge_record_schema.json` değişmez; legacy aile tabloları dondurulmuş kalır; karışık bölüm testi beş kanal için yeşil.

## Task 07: legacy Yahoo/Google düzeltmeleri ve sağlayıcı MIC kapsamı

**Bağımlılıklar:** 01, 02, 05. **Brief:** `briefs/task-07-brief.md`.

**Dosyalar:** değiştir `src/crocodile/equity/providers/{yahoo/client.py,google_finance/connector.py,base.py,factory.py}`, yedi ABD-only konnektör (`alpaca`, `finnhub`, `finra`, `sec_edgar`, `tiingo`, `treasury`, `msn_money`), `tests/equity/test_yahoo.py`, `tests/equity/providers/test_google_finance.py`; oluştur `tests/equity/fixtures/yahoo_chart/*` (yedi payload), `tests/equity/test_yahoo_session_dates.py`, `tests/equity/providers/test_google_finance_markets.py`, `tests/conformance/test_provider_mics.py`.

**Arayüzler:** `Provider.supported_mics`, `US_EQUITY_MICS`, `UnsupportedMarketError`, `require_supported_mic`, kurucu koruması `_reject_unservable_mics` (boş `symbols` kabul); `factory.supported_mics(provider)` (karar 3); Yahoo `_session_day`/`_utc_midnight_ns`/`_daily_records`/`_financial_currency`; Google `ListingContext`, `listing_context`, MIC tablolu aday listesi, `parse_val_and_unit(..., *, listing_currency)` (§5.14).

- [ ] **Test yaz:** THYAO.IS/7203.T/2330.TW/BHP.AX günlük barları yerel seans tarihinde UTC gece yarısı (eski kod bir gün erken: RED), New York DST sınırları (sentetik çerçeve; kaydedilmiş AAPL payload'ını 10b ekler ve bu istemcinin tarihlerini parite testinde sabitler, karar 32), temettü/bölünme ex-date'leri; `financialCurrency` (BHP USD raporlar) ve yokluğunda red; `US_EQUITY_MICS == exchanges.csv` ABD satırları; ABD-only sağlayıcı `THYAO@XIST`'i düşürür, hiçbir şey kalmazsa reddeder; `THYAO@XIST` → `THYAO:IST`, tablosuz MIC `NoGrammarForMarket`; `$` yalnız dolar işaretli para birimlerinde; saat bilgisi olmayan zaman listelemenin saat diliminde (Tokyo ve New York).
- [ ] **Implement:** dört parça brief sırasıyla; `NoGrammarForMarket` `LookupError` olarak sınıfıyla yakalanır.
- [ ] **Doğrula:** `$PY -m pytest tests/equity/test_yahoo_session_dates.py tests/equity/test_yahoo.py tests/equity/test_yahoo_workers.py tests/equity/providers tests/conformance/test_provider_mics.py` ve kapı listesi; dokunulan legacy dosyalarda yeni ruff bulgusu yok.
- [ ] **Commit:** `fix(yahoo): date daily bars and ex-dates by the exchange-local session` · `fix(yahoo): take the fundamentals unit from financialCurrency or refuse` · `feat(equity): declare provider market scope with supported_mics` · `fix(google_finance): resolve candidates, dollar prices and naive times by listing MIC`.

**Kabul:** kabul 2'nin legacy yarısı fixture'la kanıtlı; sağlayıcı kapsamı kapıyla sabit; yahoo/google_finance/stooq gerekçeli istisna.

## Task 08: yüzeyler — mic/country, equity `markets`, `calendar`, `LOCAL@MIC`

**Bağımlılıklar:** 01–05, 07. **Brief:** `briefs/task-08-brief.md` (referans); dağıtım zorunlu parçalarla (karar 30): `briefs/task-08a-brief.md` (Step 0-4, commit 1: parametreler, kripto retleri, equity `markets` ve `universe`) → `briefs/task-08b-brief.md` (Step 5, commit 2: `calendar`, Gate 2 sayımı, prose kapıları) → `briefs/task-08c-brief.md` (Step 6-7, commit 3-4: `LOCAL@MIC` dispatch, market-boyut kapısı); her parçanın raporu `reports/task-08x-report.md`.

**Dosyalar:** değiştir `src/crocodile/capabilities/{market,__init__}.py`, `src/crocodile/surfaces/{dispatch,mcp,rest}.py`, `tests/capabilities/test_market.py`, `tests/conformance/{test_gates,test_prose_counts,test_pending_symmetry,test_surfaces}.py`, `tests/surfaces/{test_asset_class_inference,test_error_mapping,test_every_capability_is_driven}.py`; oluştur `tests/conformance/test_market_dimensions.py`. `factory.py`'ye dokunulmaz.

**Arayüzler:** `MarketsParams.mic/country`, `UniverseParams` (`source` opsiyonel + `mic/country`), `CalendarParams(start, end, mic=None)`; `calendar` yeteneği (kripto DERIVED/`caller_supplied`, equity DERIVED/`native`); equity `markets` NATIVE/`native`, 13 sütunlu `_MARKETS_SCHEMA`; `_listing_master(ctx)` (olmayan master'ı reddeder, olanı `ListingMaster(path, readonly=True)` ile açar; karar 27); `_equity_listing_mic`; market-boyut kapısının `_Probe`/`_PROBES` uzantı noktası (§5.15).

- [ ] **Test yaz:** kripto yarıları `mic`/`country`'yi her ağ işinden önce `CapabilityUnavailable` ile reddeder; equity `markets` 172 satır, ağsız ve SEC iletişim dizesiz, `quote_scale`, `calendar_quality`, `sources`; mic/country/search süzgeçleri ve hatalı girdiler `ValueError`; equity `universe` listing master'dan (delisted yok, unseen var, master yoksa yaratmaz, varsa okuma `-wal`/`-shm` bırakmaz, satır tavanı `dispatch.payload`'da); `calendar` iki yarı ve aynı şema, fallback satırlarında açılış/kapanış `null`; `LOCAL@MIC` equity'yi seçer; Gate 2 `_ADDED_AFTER_PHASE_2`, Phase 2 sayıları dondurulmuş nüfustan, canlı sayılar ayrı kapılarla; market-boyut kapısı ve "kapı ısırır" deneyi.
- [ ] **Implement:** parametreler ve retler; equity `markets` `ExchangeRegistry`'den (görev 07'nin `supported_mics`'i ile); listing-master dalı; `calendar`; dispatch kuralı; docstring sayıları canlı değerlerle (beklenen 50 yetenek, 58 tel adı, 31'i `--asset-class` ister).
- [ ] **Doğrula:** `$PY -m pytest --timeout=1800 tests/capabilities/test_market.py tests/conformance/test_market_dimensions.py tests/surfaces/test_asset_class_inference.py tests/surfaces/test_error_mapping.py tests/surfaces/test_end_to_end.py tests/surfaces/test_cli_arguments.py`; `$PY -m pytest --timeout=1800 tests/conformance/`; `$PY -m pytest --timeout=1800 tests/test_cli.py tests/test_cli_backfill.py tests/test_cli_repairs.py`; ağsız CLI dumanı (`markets --country TR`, `calendar --mic XIST`, kripto `--mic` reddi). Her parça kendi dosyaları için aynı komutları koşar.
- [ ] **Commit:** 08a: `feat(capabilities): mic and country on markets and universe, equity markets from the exchange table`; 08b: `feat(capabilities): calendar capability with exchange sessions by MIC`; 08c: `feat(surfaces): resolve LOCAL@MIC symbols to the equity market` · `test(conformance): every mic/country implementation filters or refuses`.

**Kabul:** kabul 4'ün kripto reddi ve MIC listesi; `_REGISTRY_AS_SHIPPED` 49 olarak kalır; görev 15 ve 14 için sözleşmeler yerinde.

## Task 09: zamanlayıcı takvimleri, paylaşılan oran bütçesi, kalıcı equity DLQ

**Bağımlılıklar:** 01, 03. **Brief:** `briefs/task-09-brief.md`.

**Dosyalar:** oluştur `src/crocodile/core/ratelimit/budget.py`, `tests/core/scheduler_doubles.py`, `tests/core/{test_scheduler_calendars,test_scheduler_failures,test_ratelimit_budget}.py`, `tests/equity/test_provider_dlq.py`; değiştir `src/crocodile/core/scheduler/{state,coordinator,__init__}.py`, `src/crocodile/core/ratelimit/__init__.py`, `src/crocodile/core/ingest/deadletter.py`, `src/crocodile/equity/providers/{base,factory}.py`, `src/crocodile/equity/client/collect.py`, `src/crocodile/capabilities/ops.py`, `tests/core/test_scheduler.py`, `tests/ingest/test_deadletter.py`, `tests/conformance/test_ratelimit_placement.py`.

**Arayüzler:** `PullTask(..., *, calendar=None, delay=45 dk)`, kalıcı `FailureRecord`/`GapEntry` (`kind` `gap`|`no_data`), `max_failures=3`, `max_dates_per_tick=5`; `budget(source, host)` (event loop başına tek `TokenBucket`) ve altı satırlı `DEFAULT_BUDGETS` (karar 4); `dlq_db_path(data_dir, source)` → `<data_dir>/dlq/<source>.sqlite`; `Provider.use_durable_dlq`; `make_provider(..., data_dir=)` (§5.16).

- [ ] **Test yaz:** NZX seansı Auckland tarihiyle etiketlenir, kapanış + gecikme sonrası vadesi gelir; tatiller ve hafta sonu atlanır; tik başına en çok beş tarih; kripto `AlwaysOpenCalendar` hafta sonu koşar; takvimsiz görev ABD Doğu saatinde kalır; üç hatadan sonra gap ve watermark ilerler, yeniden başlatmada sayaç korunur, `WEEKDAY_FALLBACK`'te `no_data`; `after_hours` kaydı kabul; iki import sırası döngüsüz; bütçe tek kova/loop, normalizasyon, varsayılanlar, çakışan spec reddi, loop dışı `RuntimeError`; DLQ çökmeden sağ çıkar ve sonraki çalıştırma raporlar; equity collect/backfill boşaltır.
- [ ] **Implement:** state store, coordinator (protokol `TYPE_CHECKING` altında, `CalendarQuality` fonksiyon içinde), `budget.py`, DLQ yolu ve `ops.py` bağlantısı (`coordinator.tasks` sözlüğü korunur).
- [ ] **Doğrula:** `$PY -m pytest tests/core/test_scheduler.py tests/core/test_scheduler_calendars.py tests/core/test_scheduler_failures.py tests/core/test_ratelimit.py tests/core/test_ratelimit_budget.py tests/ingest/test_deadletter.py tests/equity/test_provider_dlq.py tests/equity/test_ingest.py tests/capabilities/test_ops.py`; `$PY -m pytest --timeout=1800 tests/conformance/`; `$PY -m pytest --timeout=1800 tests/ingest tests/core`; üç legacy dosya sıfır ruff bulgusu.
- [ ] **Commit:** `feat(scheduler): per-task exchange calendars, persistent failures and gap entries` · `feat(ratelimit): process-wide shared budget per source and host` · `feat(equity): durable provider dead-letter queues, drained after equity runs`.

**Kabul:** spec §3.9'un dört maddesi testli; `core` `contrib` import etmez; mevcut scheduler testleri gerekçeli `max_dates_per_tick=1` pinleriyle yeşil.

## Task 10: `equity/universal` temiz paketi ve `yahoo_chart`

**Bağımlılıklar:** 01–07, 09. **Brief:** `briefs/task-10-brief.md` (referans); dağıtım zorunlu parçalarla (karar 30): `briefs/task-10a-brief.md` (Step 0-3, commit 1-2: paket, kapılar, `base.py`, `http.py`) → `briefs/task-10b-brief.md` (Step 4-12, commit 3-4: `yahoo_chart`, factory, parite testi, AAPL kaydı, NVDA ölçümü).

**Dosyalar:** oluştur `src/crocodile/equity/universal/{__init__,base,http,yahoo_chart}.py`, `src/crocodile/equity/universal/data/yahoo_chart_exchange_names.csv`, `tests/equity/universal/{__init__,test_universal_package,test_base,test_http,test_yahoo_chart,test_yahoo_session_parity}.py`, `tests/equity/universal/fixtures/yahoo_chart/*` (16 kayıtlı + 6 sentetik; kaydedilirse `AAPL_5d_1d.json` ve görev 07 dizininde bayt bayt aynı `aapl.json`); değiştir `tests/conformance/test_phase1_exit.py` (`_CLEAN_PACKAGES`), `pyproject.toml` (mypy override), `src/crocodile/equity/providers/factory.py`.

**Arayüzler:** `UnservableReason`, `SourceUnservableError`, `daily_bar_ts_ns`, `session_date_of`, `is_valid_isin`, `UniverseSnapshot`, `apply_universe`, `contract_violations`; `HttpRequest`/`HttpResponse`/`Fetcher`/`AiohttpFetcher` (genel UA, yönlendirme izlemez, oturum onu yaratan olay döngüsüne aittir ve başka döngüden çağrılınca yenilenir, karar 28); `YahooChartClient`, `YahooChartProvider` (`{ohlcv, corp_action}`, 54 MIC, `INTERNAL`, tek istekte bar + kurumsal işlem); basis `native` (§5.17-5.18).

- [ ] **Test yaz:** paket kapıları (temiz liste, strict override, ruff, `contrib` yok, ASCII, kripto adlarıyla çakışma yok); `base`/`http` sözleşmesi (yeni olay döngüsünde yeni oturum); sekiz 5 günlük fixture'da yerel seans tarihi (kabul 2), legacy `yahoo` ile `yahoo_chart`'ın aynı payload'larda aynı tarihleri ürettiği parite testi (`test_yahoo_session_parity.py`, karar 32), BHP'nin UTC kesmesi tuzağı, Yahoo saat dilimi alanlarının yok sayılması, mükerrer seans satırında sonraki kalır; sentetik `VOD.L` → `GBP`, `price_scale=2` (kabul 3); temettü ex-date'leri ve bölünme oranı; intraday pencereleri; 404/429/5xx/400/HTML/3xx/403 eşlemesi; yaptırımlı MIC hiçbir istekten önce red; KOSDAQ segment soneki; para birimi uyuşmazlığı DLQ'ya.
- [ ] **Implement:** paket + kapılar (aynı commit'te); `base.py`, `http.py`; `yahoo_chart.py` ve factory kaydı; NVDA canlı ölçümü (≤ 3 istek) ile `YAHOO_CHART_ADJUSTMENT`; AAPL chart uç noktasına tek istekle spec §3.12'nin ABD fixture'ı kaydedilir ve parite testine eklenir (kayıt olmazsa kaygı olarak raporlanır).
- [ ] **Doğrula:** `$PY -m pytest tests/equity/universal` ve brief'teki conformance listesi; `$PY -m ruff check src/crocodile/equity/universal tests/equity/universal`; `$PY -m mypy -p crocodile.equity.universal`; veri CSV'si izleniyor (`git check-ignore` çıkış 1); ASCII denetimi.
- [ ] **Commit:** 10a: `feat: add the equity.universal clean package and its lint and type gates` · `feat: add shared HTTP, unservable and universe-snapshot seams for universal sources`; 10b: `feat: add the yahoo_chart universal source with exchange-local daily dates` · `test(yahoo_chart): pin the measured Yahoo split adjustment and the AAPL session dates` (AAPL kaydı yoksa `test(yahoo_chart): pin the measured Yahoo split adjustment`).

**Kabul:** kabul 2 ve 3 fixture'la kanıtlı, kabul 2'nin legacy yarısı aynı girdiyle parite testinde; basis kararı docstring ve raporda; `_EXPECTED_BASES` değişmez.

## Task 11: `tradingview` — evren taraması ve EOD seans anlık görüntüleri

**Bağımlılıklar:** 01–07, 09, 10. **Brief:** `briefs/task-11-brief.md` (referans); dağıtım zorunlu parçalarla (karar 30): `briefs/task-11a-brief.md` (Step 0, 2-8, commit 1-2: basis, modül ve sağlayıcı; çevrimdışı testler task 05'in R&D probe'ları ve etiketli sentetik fixture'larla; pazar tablosu R&D probe'larından tohumlanır, `ROW_TIMESTAMP_FIELD = None`) → `briefs/task-11b-brief.md` (Step 1 ve 9, commit 3a ve 3: canlı metainfo, tarama ve EOD kayıtları, ölçülmüş `tradingview_markets.csv`, gramer satırları, probe dosyaları, MANIFEST, `supported_mics` literali, `test_symbol_grammar` güncellemesi). 11b, 12'den önce iner.

**Dosyalar:** oluştur `src/crocodile/equity/universal/tradingview.py`, `src/crocodile/equity/universal/data/tradingview_markets.csv`, `scripts/measure_tradingview_markets.py`, `tests/equity/universal/test_tradingview.py`, `tests/equity/universal/fixtures/tradingview/*`; değiştir `src/crocodile/core/markets/data/grammar_tradingview.csv` (+ yeni probe dosyaları, `MANIFEST.json`), `src/crocodile/core/schema/provenance.py`, `tests/conformance/{test_provenance,test_gates,test_symbol_grammar}.py`, `factory.py`.

**Arayüzler:** `TradingViewClient.scan(market, columns, *, page_size, max_rows)`, `universe_from_scan`, `eod_bars_from_scan`, `previous_snapshot_values`, `markets_for_mic`, `scannable_markets`, `TradingViewProvider` (`{ohlcv, instrument}`, `INTERNAL`, `["*"]` korumayı geçer, `backfill` reddeder); basis `session_snapshot` (DERIVED, `n_session_witnesses / 2`) (§5.19).

- [ ] **Test yaz:** basis (0/1/2 tanık → 0/0.5/1, DERIVED, girdi `ohlcv`); pazar tablosu kanıtlı, yaptırımlı adaylar sorgulanmaz; sayfalama, tekilleştirme, durum eşlemesi; satır → listing (gramer, birim, tür eşlemesi, `yahoo_chart` alias'ı); EOD: kapanış + 45 dk sonrası seans tarihi, damgasız satır bir sonraki açılıştan sonra bar üretmez, sıfır hacim ve aynı tekrar eden satır bayat, satır damgası ikinci tanık ve seans üzerindeki damga bir sonraki açılış korumasının yerine geçer (`WEEKDAY_FALLBACK`'in yerel gece yarısı yer tutucusu dahil; spec §3.10b, karar 29), `EodSnapshot.sessions` yalnız atanan MIC'leri taşır; sağlayıcı kaydı ve tek geçişli `run`.
- [ ] **Implement:** basis; modül; metainfo + keşif taraması (aday başına 1 istek) ve iki EOD kaydı; gramer satırları (HNX, HSTC için primary), `$PY scripts/build_markets_reference.py` ve `--check`.
- [ ] **Doğrula:** `$PY -m pytest tests/equity/universal` ve brief'teki conformance listesi (`test_symbol_grammar.py`, `test_markets_reference_data.py`, `test_calendar_cross_checks.py` dahil); ruff/mypy; `--check` çıkış 0.
- [ ] **Commit:** 11a: `feat: register the session_snapshot provenance basis for scanner bars` · `feat: add the tradingview universal source for universe scans and EOD snapshots`; 11b: `feat(tradingview): adopt the measured market table, scan columns and recorded fixtures` (3a) · `feat: extend the TradingView grammar with measured market codes` (3).

**Kabul:** gramer yalnız `exchanges.csv` MIC'leriyle genişler; `supported_mics` literali gramerle eşit; DERIVED basis `DERIVES_RECORDS`'ta gerekçeli.

## Task 12: resmî listeleme dizinleri

**Bağımlılıklar:** 01–07, 09, 10, 11. **Brief:** `briefs/task-12-brief.md`.

**Dosyalar:** oluştur `src/crocodile/equity/universal/directories/{__init__,base,nasdaqtrader,nse_india,asx,twse}.py`, `tests/equity/universal/test_directories.py`, `tests/equity/universal/fixtures/directories/*`; değiştir `provenance.py`, `tests/conformance/{test_provenance,test_gates}.py`, `factory.py`.

**Arayüzler:** `DirectoryProvider` (`snapshot()`, tek geçişli `run`, `backfill` reddeder, `["*"]` korumayı geçer), `snapshot_from_parses`; `nasdaqtrader` (`supported_mics = US_EQUITY_MICS`, karar 5), `nse_india`, `asx`, `twse` (`REDISTRIBUTION` + atıf); basis `listing_directory` (NATIVE, sabit 1.0) (§5.20).

- [ ] **Test yaz:** dört biçim (Nasdaq altbilgi ve ETF/test bayrakları, borsa kodu → operating + segment MIC, NSE ISIN + SERIES, ASX önsöz, TWSE BOM + Çince başlık + ROC tarihi); biçim kayması `DirectoryFormatError`; gözlemlerde para birimi yok; TXSE satırları atılır ve sayılır; yokluk `unseen`, asla `delisted`; erişilemeyen dizin `SourceUnservableError`; tek belge hatası bütün anlık görüntüyü düşürür.
- [ ] **Implement:** basis + `CONSTANT_BY_DEFINITION`; ayrıştırıcılar; sağlayıcılar ve factory kaydı; dosya başına tek canlı GET.
- [ ] **Doğrula:** `$PY -m pytest tests/equity/universal` ve brief'teki conformance listesi; ruff/mypy; ASCII denetimi.
- [ ] **Commit:** `feat: register the listing_directory provenance basis` · `feat: add official listing directories as universal instrument sources` · (yalnız başlık kayması varsa) `fix(directories): follow the live nasdaqlisted header`.

**Kabul:** dört dizin `{instrument}` ile kayıtlı, `contract_violations == []`; canlı erişilebilirlik raporda.

## Task 13: kaynak sırası ve okuma anında federasyon

**Bağımlılıklar:** 01–04, 06, 07, 10–12. **Brief:** `briefs/task-13-brief.md`.

**Dosyalar:** oluştur `src/crocodile/equity/universal/order.py`, `src/crocodile/equity/universal/data/source_order.csv`, `scripts/build_source_order.py`, `tests/equity/universal/{test_order,test_resolve_daily_bars}.py`, `tests/equity/test_carry_listing_leg.py`; değiştir `factory.py` (`provider_class`), `src/crocodile/equity/analytics/carry.py` (`price_leg` listing dalı).

**Arayüzler:** `SourceOrder` (`order_for`, `validate`), `registry_capabilities`, `is_universal_source`, `universal_source_names`, `directory_source_names`, `source_order()`, `resolve_daily_bars(listing, start, end, *, catalog, order=None, sources=None)`; `UNIVERSAL_ALIAS = "universal"` (§5.21).

- [ ] **Test yaz:** tablo modeli ve doğrulama (kayıtsız kaynak, `exchanges.csv` dışı MIC, kanal/MIC beyanı, eksiksizlik, bütün sorunlar bir arada); ABD satırlarında native'ler önce, ABD dışı `ohlcv` `yahoo_chart` → `tradingview`, `instrument` dizinler → `tradingview`; federasyon: tarih başına en yüksek kaynak, eksik günde sonraki, açık kaynak listesi, aynı kaynağın son yakalaması, intraday hariç, birimler korunur, yazım sırasından bağımsız; `price_leg` yalnız `{yerel}@{MIC}` için ve seans kapanışında damgalı, ABD çıplak ticker yolu değişmez.
- [ ] **Implement:** `order.py`, üretici betik ve CSV (`--check`), `provider_class`, `price_leg` dalı.
- [ ] **Doğrula:** `$PY -m pytest tests/equity/universal/test_order.py tests/equity/universal/test_resolve_daily_bars.py tests/equity/test_carry_listing_leg.py tests/equity/test_equity_carry.py tests/capabilities/test_analytics.py tests/core/test_coverage_resolver.py`; `$PY -m pytest --timeout=1800 tests/conformance`; `$PY -m pytest --timeout=1800 --continue-on-collection-errors tests/analytics` (`carry.py` değişti; README kapısı burada); `$PY scripts/build_source_order.py --check`.
- [ ] **Commit:** `feat(equity): declare a per-market source order for universal reads` · `feat(equity): federate listing daily bars by the declared source order`.

**Kabul:** tablo her MIC × üç kanalı kapsar, `universal` hiçbir satırda kaynak değildir; SQL parametre bağlamalı ve `source_ts` üzerinde.

## Task 14: universal orkestratör ve collect/backfill entegrasyonu

**Bağımlılıklar:** 01–13. **Brief:** `briefs/task-14-brief.md` (referans); dağıtım zorunlu parçalarla (karar 30): `briefs/task-14a-brief.md` (Step 1-8: `state.py`, `fakes.py`, kapsam ve `preflight_scope`, `sync_universe`, `backfill`; commit 1 ve 2a) → `briefs/task-14b-brief.md` (Step 9-14: `collect_eod`, `collect_intraday`, `bindings.py`, `open_orchestrator`; commit 2b) → `briefs/task-14c-brief.md` (Step 15-17: `ops.py`, `test_ops_universal.py`, `collect`/`backfill` probe'ları; commit 3).

**Dosyalar:** oluştur `src/crocodile/equity/universal/{state,orchestrator,bindings}.py`, `tests/equity/universal/{fakes,test_state,test_orchestrator,test_bindings}.py`, `tests/capabilities/test_ops_universal.py`; değiştir `src/crocodile/capabilities/ops.py`, `tests/conformance/test_market_dimensions.py` (`collect`/`backfill` probe'ları).

**Arayüzler:** `UniversalState` (`<data_dir>/state/universal.sqlite`), `Scope`, `ResolvedScope`, `OutcomeStatus`, `RunReport` (`<data_dir>/state/runs/`), `SnapshotBatch(records, attributed_mics)` (`SnapshotSource.snapshot` dönüşü; karar 29), `UniversalOrchestrator` (`sync_universe`, `backfill`, `collect_eod`, `collect_intraday`, `aclose`), `open_orchestrator(..., scan_max_rows=None, **options)`, `default_bindings(..., scan_max_rows=None)` (karar 7); test ikizleri `fakes.make_orchestrator(tmp_path, bindings, *, clock=lambda: FIXED_NOW, **kw)`; `CollectParams`/`BackfillParams` `mic`, `country`, opsiyonel `symbols`, `BackfillParams.sources`; `Subscription.mics` (§5.15, §5.22-5.23).

- [ ] **Test yaz:** kapsam çözümü (segment → operating, ülke açılımı, açıkça yaptırımlı borsa hiçbir kaynak çağrılmadan red, opt-in, alias/kanonik/çıplak semboller); `sync_universe` birim başına tek çekim, eksik okumada `unseen` yok, dizinler TradingView'dan önce; backfill: tek çağrı iki kanal, kesintiden sonra tamamlananları yeniden çekmeden devam, baş/kuyruk boşlukları, açık seansın barı yok, `bot_challenge` gerekçesi ve sonraki kaynak, geçici hatada geri çekilme, eşzamanlılık sınırı, sink flush'ından önce watermark yok, para birimi uyuşmazlığı DLQ; EOD: birim başına tek anlık görüntü, kapanmamış MIC damgalanmaz, sınırlı Yahoo yedek partisi, anlık görüntünün atayamadığı MIC işaretsiz kalır (`NO_SESSION`, `not_attributable`), yedeğe gider ve aynı orkestratörde o seans için yeniden taranmaz; intraday üst sınırları; bağlamalar ve `scan_max_rows` tek sayfa; yetenek tesisatı (kripto reddi, karışık kaynak reddi, başlamamış coroutine/Subscription, çalıştırmadan önce dosya yok); market-boyut kapısının iki probe'u.
- [ ] **Implement:** `state.py`, orkestratör, bağlamalar, `ops.py` (görev 09'un DLQ bağlantısı korunur; `channel`/`start_ns`/`end_ns` zorunlu kalır, karar 8).
- [ ] **Doğrula:** `$PY -m pytest tests/equity/universal/test_state.py tests/equity/universal/test_orchestrator.py tests/equity/universal/test_bindings.py tests/capabilities/test_ops_universal.py`; `$PY -m pytest --timeout=1800 tests/capabilities/test_ops.py tests/equity/test_shipped_writer_end_to_end.py tests/surfaces tests/test_cli_backfill.py tests/equity/universal`; `$PY -m pytest --timeout=1800 tests/test_cli.py tests/test_cli_backfill.py tests/test_cli_repairs.py`; `$PY -m pytest --timeout=1800 tests/conformance`; `backfill --help` `--sources`, `--source`, `--mic`, `--country` gösterir. Her parça kendi dosyaları için koşar; CLI ve `ops.py` komutları 14c'nindir.
- [ ] **Commit:** 14a: `feat(equity): add a durable watermark store for universal runs` · `feat(equity): add the universal orchestrator with scope resolution, universe sync and resumable backfill`; 14b: `feat(equity): add end-of-day and intraday collection and the default source bindings`; 14c: `feat(capabilities): route equity collect and backfill through universal sources`.

**Kabul:** kabul 5'in çevrimdışı "kaldığı yerden devam" testi yeşil; her erişilemeyen listing kalıcı çalıştırma raporunda gerekçesiyle.

## Task 15: `universe-sync`, `schedule` ve `markets` kapsam sütunları

**Bağımlılıklar:** 01–04, 08, 09, 13, 14. **Brief:** `briefs/task-15-brief.md`.

**Dosyalar:** değiştir `src/crocodile/surfaces/operate.py`, `src/crocodile/equity/universal/{orchestrator,state,order}.py` (ekleme), `src/crocodile/capabilities/market.py`; oluştur `tests/equity/universal/test_schedule.py`, `tests/surfaces/test_operate_universal.py`, `tests/capabilities/test_market_coverage.py`.

**Arayüzler:** `build_eod_schedule`, `EodSchedule`, `EodCollectionError`, `eod_task_name` (`equity-eod:<MIC>`); `UniversalState(..., readonly=True)` (`open_sqlite_readonly` üzerinden, karar 27), `read_market_coverage` (sayımlar `ListingMaster(..., readonly=True)` ile; `sqlite3.Error`, `StoreError`, `RuntimeError` her eşlemeyi ayrı ayrı `null`'a düşürür); `usage_class_of`; 16 sütunlu `_MARKETS_SCHEMA` (karar 9); `universe-sync` ve `schedule --asset-class equity … [--once]` operatör komutları, durum dosyası `<lake>/state/schedule-equity.json`; `_make_orchestrator` `open_orchestrator(settings, sink, clock=lambda: _now())` (iki saat birlikte hareket eder).

- [ ] **Test yaz:** MIC başına takvimli tek görev, kaynaksız ve yaptırımlı MIC atlanır, vadesi gelen pazar seans başına bir kez, hata koordinatöre yükselir, barsız seans `WEEKDAY_FALLBACK`'te `no_data`, kütüphane takviminde `gap`; kapsam okuma dosya ve yan dosya yaratmaz, okunamayan veya daha yeni şemalı durumda `null`'a düşer; `markets` sütunları spec sırasında, kripto satırları `null`, `sources` sıra tablosuna göre, `usage_classes` hizalı; CLI (iki saat de `FIXED_NOW`'da): komut tablosu ve kabuk, `universe-sync` özeti ve çıkış kodları, `schedule --once` tek tik, koordinatörün tarihlediği seansı toplar (`{"XIST": 2024-03-08}`) ve ikinci çalıştırmada aynı seans tekrar toplanmaz, kripto reddi, `--once`'sız döngü.
- [ ] **Implement:** zamanlama tesisatı, salt-okunur durum, `markets_equities` sütunları, iki komut ve "These seven answer questions" docstring'i.
- [ ] **Doğrula:** `$PY -m pytest tests/equity/universal/test_schedule.py tests/capabilities/test_market_coverage.py tests/surfaces/test_operate_universal.py`; `$PY -m pytest --timeout=1800 tests/capabilities/test_market.py tests/test_cli.py tests/surfaces/test_console_scripts.py tests/core/test_scheduler.py tests/equity/universal`; `$PY -m pytest --timeout=1800 tests/test_cli.py tests/test_cli_backfill.py tests/test_cli_repairs.py`; `$PY -m pytest --timeout=1800 tests/conformance`; `crocodile --help` iki komutu listeler.
- [ ] **Commit:** `feat(equity): wire the scheduler to universal end-of-day collection` · `feat(capabilities): report sources, listings and last success in equity markets` · `feat(surfaces): add universe-sync and schedule operator commands`.

**Kabul:** kabul 4 tam (her MIC kaynak, kullanım sınıfı ve takvim kalitesiyle); operatör komutları REST/MCP'de yok (karar spec §3.11).

## Task 17: canlı ölçüm betiği ve ölçülmüş çalıştırma

**Bağımlılıklar:** 01–15. **Brief:** `briefs/task-17-brief.md`.

**Dosyalar:** oluştur `scripts/measure_global_equity.py`, `tests/measurement/test_measure_global_equity.py`, `tests/measurement/fixtures/result_min.json`. `src/` değişmez. Çıktılar `.superpowers/global-equity/measurement/` (gitignore).

**Arayüzler:** `DEFAULT_COUNTRIES` (TR, DE, GB, JP, IN, BR, ZA, NG, KE, VN, AU, US), `PER_ENDPOINT_CAP = 3`, `HttpLedger`, `install_http_ledger`, `build_plan`, `run_measurement`, `main`; her (ülke, faz) çağrısı `run_phase_call` = `asyncio.run(asyncio.wait_for(_phase_call(...), args.country_timeout))`; `_phase_call` sink'i ve orkestratörü `open_orchestrator(settings, sink, scan_max_rows=args.universe_limit, eod_fallback_limit=args.samples)` ile coroutine içinde açar ve `finally`'de önce `sink.close()`, sonra `orch.aclose()` bekler (karar 7, 28; §5.24). Hiçbir orkestratör, istemci, fetcher, sink veya kova bir `asyncio.run`'dan uzun yaşamaz.

- [ ] **Test yaz:** 24 çevrimdışı test: uç nokta anahtarı, dördüncü istek gönderilmeden reddedilir, toplam tavan, aynı host aralığı, izin listesi dışı host ve yönlendirme reddi, 429 soğuma/kapatma, 403/yönlendirme duvarı/bot sayfası kapatır, kaydedilen sorgu anahtarları, her universal kaynak host'u listede, yalnız aiohttp, defter her yeni `ClientSession`'a takılır, plan 12 ülke, yaptırımlı ülke reddi, örnek seçimi, ortam temizliği, boş olmayan veri dizini reddi, `--dry-plan` ağsız, yüzdelikler, kabul sayımı, Markdown bölümleri, `contrib` ve aşma mekanizması yok, her faz çağrısı kendi döngüsünde kendi orkestratörünü açıp kapatır (döngü nesneleri saklanarak karşılaştırılır, `id()` değil).
- [ ] **Implement:** betik (defter `ClientSession.__init__` seviyesinde, host'lar izin listesinde; "durmak aşmak değildir, zorlamak aşmaktır"); `--dry-plan` tahmini ≤ 250.
- [ ] **Doğrula:** `$PY -m pytest tests/measurement/test_measure_global_equity.py tests/capture/test_benchmark_counters.py -o addopts=` ve aynısı `env -u PYTHONUTF8` ile; ruff ve `mypy scripts/measure_global_equity.py`; kapı listesi; tek canlı çalıştırma `run_in_background` ile; çıktı denetimi (uç nokta başına ≤ 3, atanmamış host yok, `markets` fazında istek yok).
- [ ] **Commit:** `feat: add budgeted live measurement script for global equities` · (yalnız ikinci çalıştırma gerekirse) `fix: <…>`. İkinci çalıştırma iki durumda izinlidir: ilk istekten önce betik hatasıyla durma, ya da önce yeni bir çevrimdışı testle yeniden üretilen bir düzenek hatası; rapor iki JSON'u da listeler ve uç nokta başına iki çalıştırmanın toplamını tavanın yanında gösterir (3'ü aşanları adlandırır). Toplamda en çok bir yeniden çalıştırma.

**Kabul:** atıf bloğu ve araştırma belgesi için "Research deltas" listesi raporda doldurulmuş (karar 33); kabul 5'in katı okuması ölçüldüğü gibi raporlanır, karşılanmazsa DONE_WITH_CONCERNS (karar 22).

## Task 16: README "Global equities", CHANGELOG `[Unreleased]` ve paket metadatası

**Bağımlılıklar:** 01–15, 17. **Brief:** `briefs/task-16-brief.md`.

**Dosyalar:** değiştir `README.md`, `CHANGELOG.md`, `pyproject.toml` (`description`, `keywords`), `tests/conformance/test_prose_counts.py` ("Global equities" bloğu, `_NUMBER_WORDS` 11/12), `src/crocodile/__init__.py` (tagline), `src/crocodile/surfaces/server.py` (açılış satırı); yalnız kırmızı bir eski prose kapısı varsa adı geçen docstring cümlesi (sahibi kaygı olarak raporlanır).

**Arayüzler:** README çapaları A1-A10 (borsa ve ülke sayısı, kütüphane takvimi sayısı, `Sanctioned by default:` satırı, kaynak/kullanım sınıfı tablosu, `### Run it` komutları zorunlu seçenekleriyle, 16 sütunlu `Columns:` satırı, ölçüm paragrafı), CHANGELOG çapaları C1-C6 (§5.25).

- [ ] **Test yaz:** 15 prose kapısı canlı nesnelere karşı (asla literal değil): sayılar, yaptırım kümesi, kaynak tablosu iki yönde, her kullanım sınıfı, her komut ve seçenek `build_app()`'te, her yol/modül/değişken var, `markets` sütunları, CHANGELOG bölümleri, `Class.member` token'ları, kayıtlı basis'ler, açıklamalarda "US equities" yok, anahtar kelimeler kanıtlı, `_REGISTRY_AS_SHIPPED` docstring pini (49, hemen geçer), ölçüm paragrafının betik sınırları.
- [ ] **Implement:** README bölümü (backfill örneği `--channel ohlcv --start-ns 0 --end-ns …` ile, karar 8; `Columns:` 16 sütun, karar 9; `CROCODILE_DATA_DIR` kuralı, karar 21), CHANGELOG `[Unreleased]` (0.3.x lake'lerinde SQL binder sınırı, karar 11; yüzey değişiklikleri; 2027-12-31 farkı; ISO list one sınırı; kaynak sırasının okuma anında yalnız `{yerel}@{MIC}` sembollerine uygulandığı, ABD çıplak ticker'ların yakalama zamanı yolunda kaldığı; `yahoo_chart`'ın `instrumentType` eşlemesini saklamadığı ve `instrument` zincirinde olmadığı, karar 20; görev 17'nin ölçülmüş cümlesi), metadata ve tagline'lar; her `[VERIFY]` kod veya raporla çözülür (AAPL fixture'ı yalnız 10b kaydettiyse "kaydedilmiş" diye yazılır).
- [ ] **Doğrula:** `$PY -m pytest tests/conformance/test_prose_counts.py -o addopts=`; README komşuları (`test_task66_gate.py` README testleri, `test_dummy.py`, `test_evasion_quarantine.py`); kapı listesi `--timeout=1800` ile; `grep -nE "VERIFY|FROM TASK|\{[NMK]\}" README.md CHANGELOG.md` boş; `uv lock --check --offline`.
- [ ] **Commit:** (yalnız Step 2 gerekirse) `docs: bring registry and command counts in docstrings up to date` · `docs: document global equities in README, CHANGELOG and package metadata`.

**Kabul:** kabul 8'in README ve CHANGELOG yarısı; her iddia HEAD'deki koda karşı doğru ve kapılı; sürüm 0.3.2.

---

## Son entegrasyon ve kabul (Aşama C, controller)

- [ ] Bütün görev raporları DONE veya gerekçeli DONE_WITH_CONCERNS; ledger'da her görev için commit ve inceleme satırı.
- [ ] Tam test takımı per-file runner ile HEAD'de: `$PY .superpowers/global-equity/run_suite.py <worktree> <out> 900 3`; sonuç, aynı runner'ın `crocodile-baseline` (650e8ac) üzerindeki `summary.json`'ıyla başarısız test kimliğine göre karşılaştırılır; yeni kimlik yok (spec §6 "Tam pytest sonucu değişiklik öncesi baseline ile karşılaştırılır", kabul 7; karar 31). Böylece değiştirilen kodun bütün dizinleri (`tests/ingest`, `tests/resample`, `tests/sink`, `tests/replay`, `tests/analytics`, `tests/client`, `tests/history`, üst düzey `tests/test_cli*.py`, `test_examples.py`, `test_smoke.py` dahil) kapsanır. Windows'ta önceden toplanamayan veya iptal olan dosyalar "önceden var olan sınır" olarak kaydedilir, başarı sayılmaz.
- [ ] Temiz paketlerde ruff sıfır, legacy sayı ≤ 193 (bütçe 202), `mypy -p crocodile` yalnız iki eski Windows hatası.
- [ ] Bütün dalın (`650e8ac..HEAD`) tek bağımsız son incelemesi; önemli bulgular tek düzeltme geçişinde testlerle kapatılır.
- [ ] Spec §6 kabul ölçütleri eşlemesi: 1 → görev 02 + 04 kapısı; 2 → görev 07 + 10 fixture'ları ve 10b'nin aynı payload'larla legacy/universal parite testi (karar 32); 3 → görev 10 (sentetik) + görev 17 canlı kanıtı; 4 → görev 08 + 15; 5 → görev 14 (çevrimdışı devam) + görev 17 (canlı, katı okuma); 6 → görev 03; 7 → per-file runner ile tam takım, 650e8ac baseline'ına karşı (karar 31); 8 → görev 16 + araştırma belgesi eki + yürütme raporu.
- [ ] Araştırma belgesi eki (karar 33): controller, görev 17 raporundaki "Research deltas" listesinden `docs/superpowers/research/crocodile-kuresel-borsa-arastirmasi-2026-11-08.md` sonuna tarihli bir "Ölçülmüş çalıştırma" eki yazar (doğrulanan, çelişen ve ölçülmeyen iddialar, her biri ölçüm JSON yoluyla) ve `git add -f` ile commit eder.
- [ ] Yürütme raporu `docs/superpowers/reports/2026-11-08-global-equity-execution.md` (controller yazar, `git add -f`): görev/commit tablosu, kararlar, baseline dışlamaları, görev 17'nin atıf bloğu birebir; ölçüm Markdown'ı `docs/superpowers/reports/` altına kopyalanır (karar 23).
- [ ] Bu planın kutuları işaretlenir ve "Uygulama tamamlanma kaydı" bölümü eklenir.

## Açık riskler ve önerilen bölmeler

- **Gri kaynaklar kırılgan.** Yahoo ve TradingView bot duvarı veya şema değişikliğiyle cevap verebilir; karşılığı `unservable` gerekçeleri, kaynak sırası ve canlı ölçüm. Ölçüm ancak ölçüldüğü kadarını iddia eder.
- **Kabul 5'in katı okuması** NG/KE/VN'de Yahoo geçmişi olmadığı için karşılanmayabilir; sonuç olduğu gibi raporlanır (karar 22).
- **`exchange_calendars` ve pandas.** pandas 3.1.x dört Ortodoks Paskalya takvimini bozar; kilit 3.0.3'te kalır ve görev 03'ün kapısı 64 takvimin hepsini kurar.
- **Büyük görevler: bölmeler zorunludur (karar 30).** Haiku ile başlayan bir implementer için dört görev tek oturumdan büyük: 14 (state, dört orkestratör işlemi, bağlamalar, `ops.py`, probe'lar; ~95 test, 76 KB brief), 11 (canlı metainfo, ~110 istekli tarama, modül, sağlayıcı, gramer + MANIFEST; ~60 test), 08 (10 dosya, ~60 test, 88 KB brief), 10 (paket kapıları, iki dikiş, kaynak, canlı NVDA; ~70 test). Yarıda NEEDS_CONTEXT ile duran bir görev ortak dosyaları (`factory.py`, `ops.py`, `test_gates.py`, MANIFEST) sonraki göreve yarım bırakır. Bu yüzden şu parçalar, bu sırayla, her biri tam brief'ten aynı Interfaces ile kesilmiş kendi part brief'inden, kendi raporu ve incelemesiyle koşar: 08a (parametreler, kripto retleri, equity `markets` ve `universe`; commit 1) → 08b (`calendar`, sayım ve prose kapıları; commit 2) → 08c (dispatch ve market-boyut kapısı; commit 3-4). 10a (paket, kapılar, `base.py`, `http.py`; commit 1-2) → 10b (`yahoo_chart`, factory, parite testi, AAPL kaydı, NVDA ölçümü; commit 3-4). 11a (basis, modül ve sağlayıcı kaydı; çevrimdışı testler task 05'in probe'ları ve etiketli sentetik fixture'larla; commit 1-2) → 11b (canlı metainfo, tarama ve EOD kayıtları, `tradingview_markets.csv`, gramer satırları, probe dosyaları, MANIFEST, `supported_mics` literali, `test_symbol_grammar` güncellemesi; commit 3a ve 3); 11b, 12 ve 13'ten önce iner, çünkü 13'ün `build_source_order.py --check`'i TradingView literalini okur. 14a (`state.py`, `fakes.py`, kapsam, `sync_universe`, `backfill`) → 14b (`collect_eod`, `collect_intraday`, `bindings.py`, `open_orchestrator`) → 14c (`ops.py`, `test_ops_universal.py`, `collect`/`backfill` probe'ları; commit 3). Sözleşme `interfaces.md` §5'te sabittir.
- **`--data-dir` yalnız Catalog'a ulaşır** (önceden var olan projeksiyon davranışı); yazıcılar `CROCODILE_DATA_DIR`'ı izler (karar 21). Ayrı bir düzeltme işi olarak not edilir.
- **Windows sınırları.** shim ile çalışan kapılar "Windows+shim" etiketiyle raporlanır; kilit davranışı yerelde gerçekten doğrulanmış sayılmaz.

## Self-review

- Spec'in her bileşeni bir göreve karşılık geliyor: referans verisi ve kayıtlar (01), sembol/kullanım/yaptırım (02), takvim (03), listing master (04), gramer (05), şema (06), legacy düzeltmeleri (07), yüzeyler (08, 15), zamanlayıcı/bütçe/DLQ (09), universal kaynaklar (10-12), kaynak sırası (13), orkestratör (14), canlı ölçüm (17), belgeler (16).
- Görevler arası bütün isimler `interfaces.md`'de tek kaynaktan; brief'ler onunla uyumlu hale getirildi (`NoGrammarForMarket` görev 01'in sınıfı, `factory.supported_mics` görev 07'nin, `DEFAULT_BUDGETS` görev 09'un, `US_EQUITY_MICS` yeniden kullanılır, TradingView tarama sınırı görev 14'te).
- Hiçbir görev kendinden sonra gelen bir görevin ürününü tüketmiyor; ortak dosyalarda her parçanın tek sahibi var.
- Kararlar kapsamı daraltıyor (MSN, ham HTTP arşivi, ana birime çevrilmiş görünüm, 2027-12-31 düzeltmesi, `--data-dir` düzeltmesi), genişletmiyor; tek ekleme ölçüm için `scan_max_rows` sınırıdır ve varsayılanı mevcut davranıştır.
- Ölçülmemiş hiçbir şey kabul ölçütü olarak yazılmadı; Yahoo düzeltme işareti ve TradingView satır damgası canlı ölçümle karara bağlanacak.
- İkinci inceleme (eleştirmen bulguları, 2026-11-08) uygulandı: salt-okunur SQLite açılışı yan dosya bırakmaz (karar 27; bu host'ta ölçüldü), hiçbir orkestratör bir `asyncio.run`'dan uzun yaşamaz (karar 28), satır damgası takvimin bir sonraki açılış korumasının yerine geçer (karar 29), 08/10/11/14 zorunlu parçalarla koşar (karar 30), tam takım 650e8ac per-file baseline'ına karşı koşar (karar 31), legacy/universal Yahoo tarih paritesi ve AAPL fixture'ı (karar 32), araştırma belgesi eki (karar 33), `schedule`'ın iki saati tek saate bağlandı (`_make_orchestrator` ve `fakes.make_orchestrator`).
