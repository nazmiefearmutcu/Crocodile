# Küresel hisse senedi veri hattı — ara durum raporu (2026-11-08)

Dal: `feat/global-equity` · Taban: `origin/main` 5600c87 → tasarım 650e8ac → plan b2fb0fe
Spec: `docs/superpowers/specs/2026-11-08-global-equity-design.md` · Plan: `docs/superpowers/plans/2026-11-08-global-equity-plan.md`
Ar-Ge: `docs/superpowers/research/crocodile-kuresel-borsa-arastirmasi-2026-11-08.md`

Bu rapor, kullanıcının "şu ana kadar gelinen noktayı toparla" isteğiyle yazılan bir **ara durum** raporudur. Paket 1+2'nin
23 dağıtım biriminden ilk üçü ile bir kontrolör veri düzeltmesi (C1) tamamlandı; geri kalanı bekliyor. Sürüm numarası
değişmedi, CHANGELOG/README güncellemesi görev 16'da yapılacak.

## Tamamlanan işler

| Birim | Konu | Commit aralığı | İnceleme |
|---|---|---|---|
| 01 | `core/markets` paketi: hata ailesi, ISO 10383 MIC kopyası (2889 satır), ISO 4217 (165 para birimi), 172 borsalık izin listesi, yaptırım listesi, MIC/borsa kayıtları, kote birimi ayrıştırma (GBp/ZAc/ILA → ana birim, ölçek 2), referans verisini üreten ve `--check` ile doğrulayan `scripts/build_markets_reference.py` | b2fb0fe..ee96a34 | Opus: 1 düzeltme turu sonra onay |
| 02 | Kaynak kullanım sınıfları; kanonik `{yerel}@{MIC}` sembolü (ABD çıplak ticker'ları donuk); yaptırım politikası (RU, BY, IR, SY, KP varsayılan kapalı, `CROCODILE_ALLOW_SANCTIONED_MARKETS` ile açılır) | ee96a34..a8ba65b | Opus: 1 düzeltme turu sonra onay |
| 03 | Borsa takvimleri: `exchange_calendars` 4.13.2 (kütüphane), mevcut ABD takvimi (in-house), hafta içi yedeği, her zaman açık; `calendar_for(mic)` bileşik takvimi ve gün bazında `quality_for(d)` | a8ba65b..900e8de | Opus: 1 düzeltme turu sonra onay |
| C1 | Kontrolör veri düzeltmesi: Pazar–Perşembe / Cumartesi–Çarşamba pazarların hafta sonu, kardeş borsa takvimleri, yaptırım izin listesinde ham metin koruması | 900e8de..9d4b2ce | Opus: 1 önemli bulgu (kaynaksız AQSE takma adı) kontrolör tarafından 9d4b2ce ile giderildi; düzeltme turu yarıda kesildi, yeniden inceleme yapılmadı |

Toplam 21 commit, 37 dosya, yaklaşık 10.450 satır (referans verisi dahil). Push öncesi hiçbir şey uzak depoya gönderilmemişti.

### C1 ayrıntısı

- **Kaynakla doğrulanıp düzeltilen hafta sonları:** DSMD (Katar), XKUW (Kuveyt), XBAH (Bahreyn), XAMM (Ürdün), XCHG (Çittagong), XPAE (Filistin), XKHA (Hartum) → `FRI,SAT`; XTEH (Tahran) → `THU,FRI`; XDFM ve XADS (BAE, 2022'den beri Pazartesi–Cuma) → `SAT,SUN` doğrulandı.
- **Doğrulanamayıp varsayılanda bırakılanlar:** XMUS, XIQS, XALG, XLSM, MALX, XDSE (kaynaklar çelişkili ya da alınamadı), XNEP (2026 raporları çelişiyor).
- **Kardeş takvim alanlar:** XSHE→XSHG, BJSE→XSHG, XCNQ→XTSE, XNGM→XSTO. Kütüphane takvimli satır sayısı 64 → 68.
- **Kardeş takvim verilmeyenler:** XNSE (NSE ile BSE tatillerinin aynı olduğu doğrulanamadı), ROCO, NEOE, XCBO, IEXG, BIVA, Japon bölgesel borsaları, AQSE (kaynak alınamadı; kaldırıldı).
- 172 borsanın 81'inde hafta sonu hâlâ kaynaksız `SAT,SUN` varsayımı; bunların çoğu gerçekten Pazartesi–Cuma işlem görüyor.
- `is_sanctioned` / `require_not_sanctioned` artık ham bir `str` izin listesini `TypeError` ile reddediyor (önceden `"IRU"` RU ve IR'yi sessizce açıyordu).

## Doğrulama (Windows + shim)

Bu makinede Linux/WSL yok; tüm sonuçlar Windows üzerinde `PYTHONUTF8=1` ve `fcntl`/`resource` taklit katmanıyla alındı.

- `scripts/build_markets_reference.py --check` → çıkış 0 (veri dosyaları üretilenle aynı).
- `tests/core/markets`, `test_markets_reference_data`, `test_calendar_cross_checks`, `test_markets_layering`, `test_config`, `tests/core/test_scheduler.py` → hepsi geçti (9d4b2ce).
- `tests/conformance` tamamı → 1296 test; yalnız tabanda da kırık olan 3 `test_phase1_exit` testi başarısız (Windows'ta `.venv/bin/python` yok), yeni başarısız test yok.
- ruff: temiz paketler "All checks passed!", legacy sayısı 193 (taban 193, bütçe 202). mypy `-p crocodile.core` temiz.
- Tüm test setinin dosya bazında karşılaştırması (`run_suite.py`, taban 650e8ac) henüz koşulmadı; son aşamada yapılacak.

## Aldığım kararlar (maliyetiyle)

1. "Çok soru sorma, teslim edene kadar devam et" talimatı tasarımın 2. bölümünün onayı sayıldı (fiyatlar kote edildiği birimde + `currency`/`price_scale` saklanır). *Yanlışsa:* okuma yolu/şema yeniden ele alınır; yalnız yeni göller etkilenir.
2. `docs/superpowers` üst depoda gitignore'lu; önceki paket gibi `git add -f` ile takip ediliyor. *Yanlışsa:* birkaç dosya `git rm --cached`.
3. Uygulama `origin/main` 5600c87 üzerine kuruldu; yerel `main` hiç değiştirilmedi.
4. Alt ajan modeli: planlama/inceleme/hakemlik Opus, uygulayıcılar önce Haiku (CLAUDE.md "Haiku önce, Sonnet asla"). *Yanlışsa:* karmaşık görevlerde daha fazla düzeltme turu.
5. TradingView tarayıcısı evrensel EOD anlık görüntü + evren kaynağı olarak benimsendi (2026-11-08 ölçümü: 15 pazarın 12'si anahtarsız erişilebilir). *Yanlışsa:* uç nokta değişir/engellenir; sıra Yahoo'ya düşer.
6. Yaptırımlı pazarlar "gri kaynaklar varsayılan açık" kararına rağmen varsayılan kapalı (hukuki kısıt). *Yanlışsa:* kullanıcı ortam değişkeniyle açar.
7. Doğrulama ortamı Windows + shim; tüm test seti dosya bazında çalıştırıcıyla koşulur (tek bir takılma oturumu öldürmesin). *Yanlışsa:* kilit davranışı yerelde gerçekten doğrulanmış olmaz.
8. Görev 03: kütüphane takviminin üst sınırından sonra (XSHG, XBOM, XSES: 2026-12-31) hata yerine hafta içi yedeği, kalite gün bazında raporlanır. *Yanlışsa:* sınır sonrası bu üç pazarın tatilleri bilinmez (kütüphane yükseltilene kadar). Not: XSHE ve BJSE de XSHG'nin bu sınırını devraldı.
9. Görev 13: yeni okuma yolu mantığı temiz pakette; legacy `carry.py` yalnız ince bir çağrı alır.
10. Görev 09: `PullTask` sınıf düzeyi anotasyonlar alır; legacy toplama yollarında kalıcı DLQ raporlaması kabul edildi (kayıtlı genişleme).
11. Görev 16: testlerin aradığı doküman çapaları tek satırda durur. Görev 17: kuru plan istek tahmini Yahoo yedeğini MIC başına sayar.
12. Conformance/CLI testleri her zaman `--timeout=1800` ile koşar.
13. Görev 02 (hakem): yerel sembol kontrolü tek başına surrogate karakterleri de reddeder — bozuk JSON'dan gelen bir surrogate göl yazıcısında `mmh3`'ü çökertiyordu. *Yanlışsa:* maliyet ~0.
14. Görev 03 (hakem): bileşik takvim sözleşmesi sonraki görevlerin brief'lerine işlendi; kütüphane sınırı sonrası kaçan gün `gap` değil `no_data` sayılır. *Yanlışsa:* görev 09'da bir koşul ve bir test.
15. Commit imzaları commit'i gerçekten yazan modeli adlandırır (uygulayıcılar Haiku 5.5). *Yanlışsa:* push öncesi imza yeniden yazımı — bu push ile birlikte geçmişe yerleşti.
16. Uygulayıcı rapor dosyası zorunlu teslim kalemi; eksikse inceleme "önemli" bulgu sayar.
17. XSAT (Spotlight, İsveç) hafta sonu `SAT,SUN` kaldı.
18. Kontrolör görevi C1 eklendi (yukarıdaki ayrıntı). *Yanlışsa:* yanlış kaynaklı bir hafta sonu o MIC'te seansları kaydırır; mükerrer barları spec'teki bayat anlık görüntü koruması engeller.
19. C1'in kaynaksız AQSE→XLON takma adı kaldırıldı (9d4b2ce). *Yanlışsa:* AQSE hafta içi yedeğiyle kalır, UK tatillerini bilmez.

## Açık konular

- **C1 inceleme küçük bulguları (giderilmedi):** Kanada/İsveç kardeş takvimleri için tatil eşliğini gösteren kaynak ifadeleri zayıf; rapor ve hazır README satırında hatalı ifadeler var; hafta sonu testleri CSV'yi `ExchangeRegistry` yerine doğrudan okuyor; derleme betiğindeki kardeş kod kontrolü kütüphane takma adlarını (XNAS, NYSE) da kabul ediyor; `is_listing_symbol` docstring'i gitignore'lu planlama belgelerine atıf yapıyor; NSE için istek bütçesi kullanılmadan bırakıldı.
- **Görev 01–03 ertelenen küçük bulguları:** ledger'da listeli (son bütün-dal incelemesine gidecek).
- **Kalan birimler:** 04, 05, 06, 07, 08a, 08b, 08c, 09, 10a, 10b, 11a, 11b, 12, 13, 14a, 14b, 14c, 15, 17, 16; ardından bütün-dal Opus incelemesi, tek düzeltme dalgası, tüm test setinin tabanla karşılaştırması, canlı ölçüm (TR, DE, GB, JP, IN, BR, ZA, NG, KE, VN, AU, US) ve son yürütme raporu.
- **Süre:** ilk üç görev 4 sa 41 dk sürdü (14 ajan, 940 araç çağrısı). Görevler ortak dosyalara dokunduğu için sırayla çalışıyor; her görev uygulayıcı + Opus inceleme + düzeltme turu + yeniden inceleme demek ve bu Windows makinesinde conformance seti her koşuda 10–20 dakika sürüyor.
- **Disk:** C1 sırasında C: diski doldu (yazma hatası), geçici dosyalar silinerek yer açıldı; şu an ~35 GB boş (%93 dolu).

## Devam etme

Kontrolör kayıtları (ledger, arayüz kaydı, brief'ler, raporlar) depoda gitignore'lu `.superpowers/global-equity/` altında,
yalnız bu makinede durur. Kalan birimler aynı SDD iş akışıyla `["04", …]` görev listesiyle sürdürülür.
