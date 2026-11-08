# Crocodile için küresel hisse piyasası veri hattı araştırması

8 Kasım 2026. Bu belge, Crocodile'ın ABD dışındaki hisse piyasalarından veri alabilmesi için yapılan Ar-Ge çalışmasının sonucunu ve değerlendirmeye hazır bir mimari öneriyi içerir. Kod değişikliği yapılmadı. Çalışma 15 bölge/konu dosyasından oluştu. Her dosyada önce bir araştırmacı geçişi, ardından karşıt doğrulayıcı geçişi yapıldı. Bu belge doğrulanmış dosyaları, kod haritasını ve yerel ISO 10383 kaydını birleştirir. Birleştirilmiş satır düzeyindeki veri `market-matrix.json` dosyasındadır.

Aynı gün sekiz konulu bir boşluk doldurma turu yapıldı (`gaps/1-G1` … `gaps/8-G8`): Tier A bütünlüğü ve geçmiş derinliği, BIST resmî dosyaları, kapsam açıkları ve WFE, kurumsal işlemler, evren dizinleri, KAP/MKK erişimi, kaynak şartları ve Yahoo/Stooq artıkları. Rapordaki bir iddiayla çelişen her bulgu uygulanmadan önce yeniden kontrol edildi. Sonuçlar §3.1'de özetleniyor, satır değişiklikleri `meta.gap_fill` alanında. Boşluk turunda da web araması bütçesi tükenmişti, bu yüzden bulgular doğrudan sayfa okumalarına ve problara dayanıyor.

Canlı doğrulama, anahtarsız HTTP isteğinin durum kodunu ve gerektiğinde yanıt gövdesindeki alanları okumak anlamına gelir. Hiçbir yerde hesap açılmadı, API anahtarı alınmadı, oturum açılmadı. CAPTCHA, bot sayfası ve coğrafi engel aşılmadı. Web araması bütçesi doğrulama turlarının çoğunda tükendi. Aramaya dayanan iddialar bu nedenle "doğrulanmadı" olarak bırakıldı ve yükseltilmedi. Uç nokta başına 1-3 istek atıldı. Ölçülen şey erişilebilirliktir; gerçek kota, kesinti sıklığı ve veri doğruluğu ölçülmedi.

"Ücretsiz" burada veri aboneliği satın almamak demektir. Ücretsiz hesap veya anahtar isteyen kaynaklar ayrı bir sınıftır (C). Ücretsiz erişim, verinin yeniden dağıtılabileceği anlamına gelmez. Disk, ağ ve işlem maliyeti de sürer.

## 1. Özet ve öneri

Kısa cevap: Bütün dünya borsalarını ücretsiz ve hukuken temiz biçimde tek hattan almak bugün mümkün görünmüyor. Ulaşılabilir olan ise şu: ISO 10383 temelli küresel bir listeleme ana kaydı, borsa başına dürüst bir erişim sınıfı ve sınıfına göre seçilen kaynak bağdaştırıcıları. Bu yapı, lisans alınan veya yeni açılan her kaynağı tek bir sözleşmeye takmayı sağlar.

| Bulgu | Durum |
|---|---|
| Matris 176 borsa satırı, 133 ülke/bölge kodu içeriyor; 8 yetki alanında borsa yok (toplam 141) | Birleştirme bu çalışmada yapıldı |
| Tier A (resmî, ücretsiz, anahtarsız, makine-okunur, probla erişildi): yalnız 4 borsa. Xetra ve Börse Frankfurt (15 dk gecikmeli işlem bildirimi dosyaları, 1 iş günü saklama, OHLC yok), TWSE (2010-01-04'ten günlük OHLCV), MOEX (OHLCV; yaptırım incelemesi şart). Geçmiş fiyat veren tek yaptırımsız A borsası TWSE | Canlı prob; dosya uçları boşluk turunda ve yeniden kontrolde problandı |
| Tier B (anahtarsız toplayıcı çalışıyor): 50 borsa. 46'sında Yahoo yanıtının meta alanları okundu, 4'ünde yalnız HTTP 200 görüldü | Canlı prob |
| B'nin tek rotası Yahoo chart uç noktası. Yahoo kullanım şartları robot ve kazıyıcıyla otomatik erişimi yasaklıyor. Yahoo `close` temettüye göre düzeltilmemiş, `adjclose` düzeltilmiş | ToS sayfası okundu; 8 sonekte prob |
| Stooq bu ortamdan veri vermiyor: bölge turlarında JavaScript bot sayfası, boşluk turunda ve yeniden kontrolde bağlantı zaman aşımı. Crocodile'ın mevcut stooq bağlayıcısı bu ortamda veri alamaz. Stooq'un başka ortamlarda çalışıp çalışmadığı belirlenmedi | Canlı prob (15+ ve 4 istek) |
| Ücretsiz anahtarlı toplayıcılar ABD dışı için ücretsiz değil: Twelve Data Basic yalnız ABD'yi kapsıyor ve ticari kullanımı yasaklıyor; EODHD ücretsiz paketi yalnız kişisel kullanım. Tier C 13'ten 10'a indi | Fiyat ve şart sayfaları okundu |
| Türkiye: BIST verisi lisanslı dağıtıcılar üzerinden yayılıyor. Resmî ücretsiz toplu dosya bulunamadı; 2021 tarihli ücret tarifesinde gecikmeli veri ayda yaklaşık 900 TL / 190 USD. KAP'ın belgelenmiş API'si yok, robots.txt ve şartları okunamadı. Yahoo `.IS` çalışıyor | Ayrıntı §4.1 |
| Resmî ücretsiz anahtarlı en iyi rotalar: J-Quants (Japonya, 12 hafta gecikmeli, 5 istek/dk, şartları bulunamadı) ve KRX Open API (Kore, ticari olmayan kullanım, üçüncü kişiye sağlanamaz) | Birincil sayfa okundu; anahtar alınmadı |
| Yeniden dağıtımı açıkça serbest olan kaynaklar yalnız referans verisi: GLEIF (CC0) ve OpenFIGI (FIGI kamu malı). TWSE OpenAPI, atıf şartıyla ticari kullanıma açık lisansa (OGDL v1) bağlı | Şart metinleri okundu |
| Crocodile'da küresel genişlemenin önündeki ilk engel veri kaynağı değil, kimlik modeli. Menkul kıymetler ticker'a göre anahtarlanıyor; aynı ticker'lı ikinci listeleme sessizce üzerine yazılıyor | Kod haritası, satırlar yeniden okundu |
| Yahoo günlük barları yfinance'ta yerel gece yarısına çekiliyor, Crocodile bunu UTC'ye çevirip tarihi UTC'den alıyor. UTC'nin doğusundaki her borsada (BIST dahil; yaz saatinde Londra da) bar ve temettü tarihi bir gün erkene kayıyor | Crocodile ve yfinance 1.5.2 kaynağı okundu, çalıştırılmadı; fixture testiyle sabitlenmeli |

Önerim **katmanlı, resmî kaynak öncelikli federasyon** (§10'daki B yaklaşımı). Sıra:

1. Önce veri getirmeyen ama her şeyi taşıyan temel. Listeleme ana kaydı (issuer → instrument → listing, anahtar `(MIC, yerel sembol)`), MIC kaydı, para birimi ve fiyat ölçeği (GBX/ZAc/ILA), borsa takvimi protokolü, kaynak başına `SymbolGrammar`, Yahoo tarih hatasının düzeltilmesi.
2. Sonra probla doğrulanmış resmî ücretsiz kaynaklar: TWSE (2010'dan geçmiş), Deutsche Börse MDS (1 günlük saklama nedeniyle hemen her gün toplanmaya başlanmalı), ESEF/filings.xbrl.org, GLEIF, OpenFIGI, ECB kurları. Ardından ücretsiz anahtarlı resmî API'ler: J-Quants, KRX Open API.
3. Yahoo yalnız "araştırma modu" bayrağıyla, çapraz doğrulama ve QA kaynağı olarak tutulur. Üretim hattına bağlanmaz.
4. Türkiye için üretim rotası bir lisans kararıdır: BIST yetkili dağıtıcısı veya BIST Tarihsel Veri Satışı. Bu karar kullanıcıya aittir.

## 2. Kapsam ölçüsü

### 2.1 Sayılar

| Ölçü | Değer | Kaynak ve not |
|---|---|---|
| ISO 10383 yerel CSV satırı | 2.889 (2.313 ACTIVE, 574 EXPIRED, 2 UPDATED) | `ref-mic/ISO10383_MIC.csv`, bu çalışmada yeniden sayıldı. Son kayıt tarihi 2026-10-26 |
| ACTIVE operating (OPRT) MIC | 1.336 | Bunların 648'i NSPD, 244'ü SINT (sistematik içselleştirici), 101'i RMKT, 78'i MLTF |
| ACTIVE OPRT MIC bulunan ülke | 149 | Tüm kategoriler |
| RMKT/MLTF/NSPD kategorili OPRT MIC bulunan ülke | 147 | Yalnız RMKT/MLTF süzülürse 66'ya düşer |
| Matristeki borsa satırı | 176 | 172'si ISO operating MIC'li |
| MIC'siz borsa satırı | 4 | Etiyopya ESX, BVMAC (Orta Afrika), Bhutan RSEB, Myanmar YSX |
| Borsa satırı olan ülke/bölge kodu | 133 | BRVM 8 UEMOA ülkesine, ECSE 8 OECS üyesine hizmet eder; bunlar tek kodla sayıldı |
| Borsası olmayan yetki alanı | 8 | Andorra, Makao, Brunei, Doğu Timor, Samoa, Tonga, Afganistan; Lihtenştayn'da ulusal borsa yok, yalnız MTF'ler (XNOM, ARTX) |
| 147 ülkelik kümede olup matriste bulunmayan | 15 | AL (XALS, XTIR), CW (DCSX), DO (XBVR), FO (VMFX), GG (XCIE), GT (XGTG), GY (GSCI), HN (XBCV), KG (XKSE), MG (XMDG), NI (XMAN), SK (XBRA, SPXE), SV (XSVA), VC (SVXI), VU (GXMA, POTC). Boşluk turunda ISO bilgileri çıkarıldı (§2.1 sonu); hisse listeleyip listelemedikleri yine doğrulanmadı |
| Kapsanan ülkelerde satırı olmayan ACTIVE RMKT yerler | 5 | NEEQ (Çin), IFBX (İran), TXSE (ABD), XNXC (Hollanda), XRMZ (Çekya). Ayrıca XJSE satırında not edilen CTS3, ZARX, EESX, 4AXE ve XKRX satırında not edilen XKFB |
| ISO'da hiç satırı olmayan ve borsasız listesinde de bulunmayan | 6 | SL, LS, SR, TJ, TM, SO. Bu ülkelerde borsa olup olmadığı doğrulanmadı |
| WFE üye sayısı ve borsa başına şirket sayısı | **Doğrulanmadı** | `world-exchanges.org/our-members` 404. Üye ve istatistik portalları giriş istiyor. 6 Mart 2026 tarihli factsheet PDF'i şirket sayısı içermiyor. Factsheet seçicisindeki 54 borsa adının 51'i matrisle ad eşleşiyor; AIX, Buenos Aires borsası ve Gana el ile kontrol edilmeli. Kapsama oranı hesaplanmadı. ">45.000 şirket" gibi toplamlar kullanılmadı |

Kategori filtresi önemli bir tuzak. ISO 10383'te birçok ana ulusal borsa NSPD koduyla kayıtlı: XNYS, XHKG, XSHG, XSHE, XNSE, XKRX, XTAI, XSAU, XIST, XJSE, XFRA, XETR, XTAE, XASX. RMKT/MLTF süzgeci bu borsaları kaçırır. NSPD'nin anlamı CSV'de tanımlı değil ve ISO spesifikasyonundan doğrulanmadı. Bu nedenle borsa evrenini kategoriyle süzmek yerine açık bir MIC izin listesi kullanılmalı.

Kapsam açığı olarak en dikkat çeken ülke Slovakya (Bratislava, XBRA). Bratislava AB içinde ve `exchange_calendars` takviminde de yer alıyor, ama bölge araştırmalarına girmedi. Boşluk turunda (G3) bu açıklar için yalnız ISO kaydı ve ana sayfa erişimi kontrol edildi:

- RMKT kategorili operating MIC'ler: XBRA (Bratislava, ana sayfa 200), XALS (Arnavutluk, 200), XBVR (Dominik Cumhuriyeti), XBCV (Honduras; XHON EXPIRED ve kaydı XBCV'yi gösteriyor), SVXI (Saint Vincent).
- NSPD kategorili: XCIE (TISE, Guernsey; sitesi 4.500'den fazla menkul kıymet diyor, çoğu borçlanma aracı), DCSX (Curaçao, bağlantı yok), VMFX (Faroe), XGTG (Guatemala), XSVA (El Salvador), XMAN (Nikaragua), XKSE (Kırgızistan), GSCI (Guyana), GXMA ve POTC (Vanuatu), XTIR (Tirana; CSV'de site yok, faaliyeti şüpheli).
- Dışlananlar: XMDG (Madagaskar döviz piyasası) ve SCOT (Scottish Stock Exchange; 2026'da kurulan, Anjouan lisansına atıf yapan bir platform; el ile kontrol edilmeden eklenmemeli).

Hiçbir yerde Yahoo probu yapılmadı ve şirket sayısı bulunamadı. Bu yerler satır olarak eklenmedi ve sayımlara girmedi. Listeleri `meta.coverage_candidates` alanında.

ABD bölge araştırmalarının kapsamında değildi. Crocodile ABD için zaten on kaynakla çalışıyor. Matristeki dört ABD satırı (XNYS, XNAS, XCBO, IEXG) referans ve toplayıcı dosyalarından kuruldu.

### 2.2 Erişim sınıfları (tier) ve dağılım

Bölge doğrulayıcıları aynı harfleri biraz farklı yorumlamıştı. Örneğin Japonya dosyası resmî anahtarlı J-Quants'ı esas alıp C dedi, toplayıcı dosyası aynı borsaya Yahoo üzerinden B dedi. Birleştirmede tek rubrik uygulandı:

| Tier | Tanım |
|---|---|
| A | Resmî, ücretsiz, makine-okunur fiyat/EOD kaynağı anahtarsız probla erişildi (HTTP 200 ve veri gözlendi) |
| B | Anahtarsız toplayıcı (fiilen yalnız Yahoo chart) bu borsanın bir sembolü için HTTP 200 döndü. `verified` = meta alanları okundu, `provisional` = yalnız durum kodu |
| C | Ücretsiz anahtarla erişim birincil bir sayfada belgelenmiş. Anahtar alınmadı, veri çekilmedi |
| D | Ücretsiz rota doğrulanmadı: ücretli, engelli ya da bilinmiyor. `tier_candidate` tek ek kontrolle ulaşılabilecek seviyeyi gösterir |

Tier erişim sürtünmesini ölçer, hukuki uygunluğu ölçmez. B sınıfındaki her borsanın rotası Yahoo'dur ve Yahoo şartları otomatik erişimi yasaklar. Bu durum matriste her satırın `legal_flags` alanında yazılıdır.

| Tier | Satır | Durum ayrımı | Borsalar |
|---|---|---|---|
| A | 4 | 4 doğrulandı | XETR, XFRA, XTAI, MISX |
| B | 50 | 46 doğrulandı, 4 geçici | Geçici olanlar: XLIM, XCAI, XKLS, XSTC. XCSE, XHEL ve XBKK boşluk turunda doğrulandı |
| C | 10 | 10 belgelenmiş | RSEX, XDAR, XZIM, VFEX (Afrika; EODHD borsa sayfaları, ücretsiz paket yalnız kişisel kullanım), AQXE, AQSE, XLUX, XSTE, XCBO, IEXG |
| D | 112 | 28'inin adayı var | Aday A: XZAG, XLJU, XKAZ. Aday B: Kanada'da TSXV/CSE/NEO, Alman bölge borsaları, BX Swiss, Kazablanka, Gana, Botsvana, Malavi, Nijerya, Dakka, Kolombo, Filipinler, Hanoi, İzlanda, Abu Dabi, Pekin. Aday C: XALG, XNAM, XTUN |

Boşluk turunun tier etkisi:

- **XFRA A'da kaldı.** G1, DFRA dosyaları problanmadığı için B önermişti. Yeniden kontrolde DFRA-posttrade dakikalık dosyası (84 kayıt) ve günlük dosyası (182.573 kayıt) HTTP 200 ve veriyle döndü. Öneri uygulanmadı.
- **XETR, XTAI ve MISX A'da kaldı, dayanakları güçlendi.** XETR'de dosya ucu problandı. TWSE'de 2010-01-04'e uzanan günlük uçlar bulundu. MOEX'te ISS history ve candles uçları OHLCV döndürdü (daha önce yalnız SECID/SHORTNAME satırları vardı).
- **XCSE, XHEL, XBKK: B geçici → B doğrulandı.** G8 fixture'larında meta alanları okundu (CPH/DKK, HEL/EUR, SET/THB).
- **XALG, XNAM, XTUN: C → D (aday C).** C notları Twelve Data veya EODHD ücretsiz anahtarına dayanıyordu. Twelve Data Basic ABD dışını kapsamıyor (G7; fiyat tablosu bu birleştirmede yeniden okundu). Bu üç borsa için EODHD borsa sayfası görülmedi. XALG ve XNAM satırlarında zaten hiçbir toplayıcı atfı yoktu.
- AQXE, AQSE ve XLUX'un C notu rubriğe göre zayıf: ücretsiz anahtarlı yol birincil sayfada gösterilmemiş. Boşluk turu bunlara dokunmadı; bir sonraki geçişte D'ye inmeleri muhtemel.

Uyumlaştırma 24 borsanın harfini değiştirdi. Tam liste `meta.tier_changes_by_harmonisation` alanındadır. Örnekler:

- **Yükselenler.** Japonya (C→B), Kore (C→B) ve İsrail (C→B): Yahoo yanıtları meta alanlarıyla doğrulanmıştı. Resmî anahtarlı rotalar `keyed_free_options` alanında korunuyor. Budapeşte ve Bükreş (D→B): CEE doğrulayıcısı yalnız resmî kaynağa bakmıştı, oysa `.BD` ve `.RO` yanıtları doğrulanmıştı. Buenos Aires (D→B): `GGAL.BA` çalışıyor; bölge dosyasının dayandığı yeniden dağıtım yasağı hukuki risk alanına taşındı.
- **Düşenler.** TSXV, CSE ve NEO (B→D, aday B): sonek yalnız Yahoo yardım tablosunda vardı, geçerli sembolle başarılı prob yapılmadı. Zagreb, Ljubljana ve KASE (B→D, aday A): resmî ücretsiz EOD/web verisi var, ama makine-okunur rota indirilip doğrulanmadı. Kazablanka, Gana, Botsvana, Malavi (B→D, aday B): veri çağrısı doğrulanmamıştı. Hong Kong (A→B): A notu yalnız resmî menkul kıymet listesine (fiyatsız) dayanıyordu.

### 2.3 Prob istatistiği

Bölge dosyalarındaki yapılandırılmış prob kayıtlarından 293 benzersiz uç nokta/durum çifti toplandı. 209'u 2xx, 75'i başarısız (4xx, 5xx veya bağlantı yok "000"), 7'si yönlendirme döndü. 2 tanesi 200 döndüğü halde bot sayfası olarak işaretliydi. Stooq problarının çoğu düz metin alanlarda kayıtlı olduğundan bu sayıma girmedi. Hepsi bot sayfası döndürmüştü. Yahoo için sonek bazında 71 MIC/sonek kaydı var: boşluk turundan sonra 50'sinde meta okundu, 4'ünde yalnız 200 görüldü, 9'u yalnız yardım tablosunda listeli, 8'i 404/429 ile sonuçsuz kaldı (geçersiz sembol tahminleri dahil). Boşluk turunda 16 Yahoo isteği daha yapıldı (G4'te 8, G8'de 8), yaklaşık 1 sn arayla; hepsi 200 döndü, 429 görülmedi. Bu sayılar erişilebilirlik ölçüsüdür. Kota ve gecikme ölçülmedi. G8'in istek sınırı yüzünden 21 artık MIC (XCAI, XKLS, XLIM, XSTC, BJSE, Kanada ve Alman bölge borsaları, Dakka, Kolombo, Filipinler, Hanoi, İzlanda, Abu Dabi, Kazablanka, Nijerya) yine problanmadı.

## 3. Doğrulama yöntemi ve bu belgede eklenenler

Her bölge dosyasında araştırmacının iddiaları ayrı bir doğrulayıcıyla karşılaştırıldı. Girdi listesine göre toplam 285 düzeltme yapıldı, 127 iddia çürütüldü. Sık görülen çürütmeler şunlar: yanlış veya süresi dolmuş MIC (XJKT, XMCE, XBSP, XTUR EXPIRED), segmentin ayrı borsa sanılması ya da tersi, yanlış ISO 4217 alt birimi (UGX ve RWF 0, ZWL listede yok), Yahoo soneği varsayımları (`.SAU` yerine `.SR`, Brüksel `.BR`), Stooq'un çalıştığı varsayımı ve kanıtsız tier'ler.

Birleştirme sırasında bu belgede yeni yapılanlar:

- BIST Data Dissemination PDF'inin metni yerel kopyadan (`probes/bist.pdf`) çıkarıldı. Doğrulayıcılar bunu yapamamıştı. Bulgular §4.1'de.
- Bulgaristan'ın para birimi düzeltildi. SIX ISO 4217 list-one (yayın 2026-09-17) Bulgaristan için EUR gösteriyor ve BGN listede yok. Bölge dosyası euro geçişini "doğrulanmadı" diye bırakmıştı.
- `exchange_calendars` 4.13.2 PyPI tablosu ayrıştırıldı: 68 takvim kodu. Matristeki 62 borsa bir koda eşleniyor. ISO operating MIC'ten farklı kodlar kullanılan üç yer var: XJPX→XTKS, BMEX→XMAD, MISX→XMOS.
- Twelve Data `/exchanges` yanıtı (97 kod, 58 ülke) matrisle eşlendi. 76 operating MIC örtüşüyor.
- Tier'ler §2.2'deki rubrikle uyumlaştırıldı.

### 3.1 Boşluk doldurma turu ve çelişkilerin yeniden kontrolü

Sekiz boşluk dosyasının bulguları bu belgeye ve matrise işlendi. Rapordaki bir iddiayla çelişen veya bir tier'i değiştirecek her bulgu, uygulanmadan önce birleştirme sırasında yeniden kontrol edildi:

| Bulgu | Rapordaki iddia | Yeniden kontrol | Sonuç |
|---|---|---|---|
| G1: XFRA B'ye inmeli (DFRA dosyası problanmadı) | XFRA A | DFRA dosya listesi 200; `DFRA-posttrade-2026-11-06T10_00` 200, 84 kayıt; `DFRA-posttrade-daily-2026-11-06` 200, 5,6 MB gz, 182.573 kayıt | Öneri uygulanmadı, XFRA A |
| G1: MDS günlük dosyası 404 | — | Yalnız pre-trade adı içindi. `DETR-posttrade-daily-2026-11-06` aralık isteğine 206 döndü (23,1 MB). İndirme imzalı bir depolama URL'sine 301 ile yönleniyor, yönlendirme izlenmeli | Günlük post-trade dosyası var |
| G8: "UTC'nin doğusundaki her borsada tarih kayar" iddiası fazla geniş | Kod çıkarımı | Crocodile `yahoo/client.py:378-383` ve kurulu yfinance 1.5.2 `scrapers/history.py:421` okundu: günlük indeks borsa saat diliminde yerel gece yarısına çekiliyor, Crocodile bunu UTC'ye çevirip tarihi UTC'den alıyor | İki bulgu farklı katmanda, ikisi de doğru (§8). Rapor iddiası korundu, kanıt düzeyi yükseldi |
| G8: Stooq bağlantı zaman aşımı, bot sayfası görülmedi | Bot sayfası | 1 istek: 000, yaklaşık 21 sn | İki gözlem birlikte yazıldı; bu ortamda veri yok |
| G7: Twelve Data Basic yalnız ABD | "Uluslararası kapsam doğrulanmadı" | Kayıtlı `pricing.md` tablosu yeniden okundu: Basic satırında Markets = US | 73 satırda Twelve Data seçeneği `keyed_free_options`'tan `paid_options`'a taşındı; 3 Afrika borsası C'den D'ye |
| G5: XMOS operating MIC listesinde | XMOS bir CCP | ISO CSV: XMOS "Central Counterparty Clearing Center", NSPD | Rapor doğru, G5 listesi grep artefaktı |
| G2: 13-14 Temmuz ve 30 Ağustos belirsiz | — | Tatil PDF metni: 13-14 Temmuz yalnız takas kaydırma satırlarında; 30 Ağustos Pazar | Çözüldü (§4.1) |
| G8 fixture'ları ve G7 alıntıları | — | Fixture meta alanları ve kayıtlı sayfalardaki alıntılar yeniden okundu | Tutarlı |

Boşluk turunun genel sınırları: web araması hiçbir alt görevde çalışmadı; WFE üye listesi, endeks lisans maddeleri, KAP şartları ve çoğu dizin bağlantısı bu yüzden açık kaldı. Hiçbir yerde hesap açılmadı, anahtar alınmadı, bot sayfası aşılmadı. BIST'in `DDA.rar` arşivi güvenilmeyen arşiv olduğu için indirilmedi.

## 4. Bölge bölge bulgular

Tablolarda "Rota" sütunu en iyi ücretsiz yolu gösterir. Örnek semboller probda kullanılan sembollerdir. Yahoo gecikmeleri Yahoo yardım sayfası SLN2310'daki ifadelerdir, ölçülmedi.

### 4.1 Türkiye ve Borsa İstanbul

| Konu | Bulgu | Doğrulama düzeyi |
|---|---|---|
| Operating MIC | XIST: ACTIVE, OPRT, NSPD, son güncelleme 2022-10-24 | ISO CSV |
| Segment MIC'leri | XEQY (pay piyasası), XFNO (VİOP), XDSM (borçlanma araçları), XPMS (kıymetli madenler). Hepsinin operating MIC'i XIST | ISO CSV |
| Hariç tutulacaklar | EXTR, XEDA, XEID (EPİAŞ enerji, ACTIVE ama hisse değil). XTUR (VOB) EXPIRED, kayıt notu "MIC to use is XIST". XIAB (İstanbul Altın Borsası) EXPIRED | ISO CSV |
| Para birimi | TRY, ISO 4217 alt birimi 2 (kuruş). GBX benzeri bir alt birim kotasyonu yok | SIX ISO 4217 listesi |
| Saat dilimi | `Europe/Istanbul` (IANA zone.tab'da var) | zone.tab |
| Seans saatleri | **Doğrulanmadı.** 10:00-18:00 bilgisi tarihsiz bir banka sayfasından geliyor; resmî kaynakta ne doğrulandı ne çürütüldü. BIST işlem saatleri sayfası (HTTP 200) seans tablosunu statik HTML'de vermiyor; altbilgi "2013-2025", önbellek tarihleri 2024-01-29. Yalnız dipnotlar okundu: emir toplama belirtilen saatten sonraki 30 sn içinde rastgele biter, ETF'ler için çağrı aşaması 09:40-09:56, birincil piyasa işlemleri 10:30-13:00. Açılış ve kapanış müzayede saatleri okunamadı | Sayfa okundu (G2); tablo tarayıcıyla okunmalı |
| Resmî veri dağıtımı | Data Dissemination PDF (yerel kopya, tarihsiz): veri, BIST'in yetkilendirdiği dağıtıcı ve alt dağıtıcılar üzerinden gerçek zamanlı ve gecikmeli yayılıyor. Paketler: Limited Level 1 (son fiyat, değişim), Level 1, Level 1+ (endeksler eklenir), Level 2 (Level 1+ üzerine en iyi 5 kademedeki bekleyen emir miktarları, iptal edilen emir istatistikleri; metin başka yerde "5 veya 10 kademe" de diyor). "Açık dağıtım ortamı" üyelik istemeyen web ve TV, "kapalı" kullanıcı adı ve şifreyle erişilen abonelik ortamı olarak tanımlanıyor | Metin bu çalışmada çıkarıldı. Paket/sütun tablosu `pdftotext` çıktısında kaydığı için hangi paketin hangi ortamda açık olduğu kesin okunamadı |
| Resmî dosya sayfaları | `/en/data/equity-market-data` günlük, haftalık ve aylık dosyaları sıralıyor (ör. "Daily Closing Prices"), ama 1 Ağustos 2015'ten beri bu dosyaların yalnız datastore'da olduğunu ve bir kısmının siteden kaldırıldığını söylüyor. Sayfada doğrudan ücretsiz indirme bağlantısı yok. `/en/data/data-dissemination`: veri "lisanslı veri dağıtıcıları" üzerinden gerçek zamanlı, gecikmeli ve gün sonu yayılıyor | Sayfalar okundu (G2) |
| Tarihsel veri | BIST Tarihsel Veri Satışı ayrı bir hizmet: EOD fiyat ve işlem değeri, EOD endeks, gün içi emir defteri, kurum veya kişinin kendi işlem ve emirleri, gün içi endeks, bilanço ve gelir tablosu. Tarihsel veri sayfası veriyi datastore'a yönlendiriyor ve bir "Taahhütname" (Letter of Undertaking) istiyor | PDF metni; sayfa okundu (G2) |
| Lisans | Duyuru 13712: gecikmeli veya gün sonu veriyle endeks hesaplamak 1 Ekim 2014'ten beri ücrete ve lisansa tabi | Birincil duyuru okundu |
| Ücret tarifesi | Data Distribution Agreement Ek-3, sürüm 15.a, **01.01.2021 tarihli (eski)**; TL ve USD, KDV hariç. Aylık sabit: gecikmeli bilgi yaklaşık 900 TL / 190 USD; gün sonu verisi 900-1.100 TL / 350 USD; referans verisi 825 TL / 250 USD; kurumsal işlem verisi 75 TL / 32 USD; gerçek zamanlı kalemler 1.100-3.350 TL / 325-750 USD; display dışı türetilmiş veri 925-1.650 TL / 325-600 USD. Bağlantı: veri merkezi 16.000 TL / 3.500 USD, ilk teknik bağlantı 43.000 TL / 8.000 USD. GTEND türetilmiş endeks verisi ayrı izin ve sözleşmeye tabi. Hisse verisi için EUR tarifesi yok (EUR yalnız SASE bölümünde) | PDF metni çıkarıldı (G2); sütun eşlemesi kısmen belirsiz, güncel fiyat BIST'ten istenmeli |
| Datastore | `datastore.borsaistanbul.com` tek sayfalık uygulama. `/api/category` anahtarsız JSON döndürüyor: 42 kategori, ör. GSFIHB "Daily Closing Prices and Trade Volumes", GSDFV93 "Daily Adjusted Closing Price Data (Starting from 1993)", GSEIHB "Daily Index Values and Trade Volumes", ITCHV (ITCH akışı). Yanıtta fiyat ya da ücretsiz/ücretli alanı yok. Ürün-tipi ucu denenen üç parametreyle 204 döndü. İstemci kodu `/api/is-subscribed`, `/api/payment-profile`, `/api/payment` uçlarını içeriyor; bu bir abonelik ve ödeme katmanına işaret ediyor (çıkarım) | Canlı prob (G2); ürün başına ücretsizlik ve oturum şartı doğrulanmadı |
| Gecikme durumu | **Çözülmedi.** Eylül 2026 tarihli bir Foreks banner'ı, ForInvest'in 15 dakikalık gecikme döneminin bittiğini ve BIST verisinin gerçek zamanlı ücretsiz olduğunu söylüyor. Bu bir satıcı açıklaması, BIST açıklaması değil. Araştırmacının diğer kaynaklarıyla çelişiyor. BIST duyurular sayfasının görünen kısmında (2026 duyuruları, ör. 22/09/2026 seansından itibaren yukarı adım kuralı) gecikme kaldırma kararı yok; tam arşiv taranamadı | İkincil kaynak; duyuru sayfası okundu (G2) |
| Toplayıcılar | Yahoo `THYAO.IS`: HTTP 200, para birimi TRY, exchangeName IST. Yardım sayfasında `.IS` 15 dk gecikmeli. Google Finance `THYAO:IST` sayfası HTTP 200. Twelve Data XIST'i listeliyor (Europe/Istanbul; veri için anahtar gerekli; ücretsiz Basic plan ABD dışını kapsamıyor). Stooq `.tr` bot sayfası döndü. Boşluk turundaki THYAO.IS fixture'ında günlük zaman damgaları 06:30 UTC (yerel 09:30) EODHD kodu doğrulanmadı | Canlı prob |
| Ücretli referans | ICE Developer katalogunda BIST var, dxFeed Türkiye kapsamı var. İşbank sayfası MKK saklama verisini lisans paketi olarak satıyor | Sayfa erişimi; fiyatlar okunmadı |
| KAP (dosyalama) | `kap.org.tr`: doğrulayıcı turunda 307, araştırmacı turunda 200; boşluk turunda ana sayfa 200 ve `/en`'e yönleniyor. `robots.txt` standart dışı **HTTP 666** ve Türkçe bir hata sayfası döndürdü (kapdestek@mkk.com.tr adresini gösteriyor); robots dosyası okunamadı. Kullanım şartları sayfası ana sayfa bağlantılarında bulunamadı, tahmin edilen URL 404. HTML/PDF portal; belgelenmiş public API ya da RSS yok. `/en/bildirim-sorgu` sayfası bildirim kategorilerini, içerik ve ek araması ile EXCEL/PDF dışa aktarımı gösteriyor; varsayılan pencere 14 veya 30 gün. MKK bağlantıları yatırımcı ve şirket portalları, veri API'si değil (çıkarım). PyPI'daki resmî olmayan sarmalayıcılar (pykapfinance, kap-sdk) büyük olasılıkla siteyi kazıyor | Canlı prob (G6). Otomatik erişim izni **belirsiz**; şartlar okunana kadar kazıma izinli sayılmamalı |
| Kurumsal işlemler | Listelenebilir resmî kaynak bulunamadı. KAP bildirim sorgusunda Özel Durum Açıklaması, Finansal Rapor, Sermaye Artırımı/Azaltımı gibi kategoriler görüldü; temettü (kâr payı), bedelli/bedelsiz, işlem sırası durdurma, kotasyondan çıkma ve kod değişikliği etiketleri sayfanın HTML'inde bulunamadı. BIST sayfalarında durdurma ve fiyat düzeltme duyuruları liste hâlinde yok. SPK bültenleri yıl bazlı sayfalarda (2026, 2025, arşiv), PDF'ler `spk.gov.tr/data/...` altında; sayı başına URL deseni ve bültenlerdeki sermaye artırımı/temettü onayları kontrol edilmedi | Canlı prob (G6) |
| Evren dizini | `kap.org.tr/tr/bist-sirketler` HTTP 200, 1,5 MB, JavaScript ile oluşturuluyor; alanlar statik ayrıştırmayla çıkmıyor | Canlı prob (G5, G6) |
| Takvim | `exchange_calendars` 4.13.2'de XIST var (sürüm 1.10'da eklenmiş) ve `exchange_calendars_extensions` 0.10.4'te de var. **BIST 2026 pay piyasası tatil çizelgesi** (resmî PDF, HTTP 200): kapalı 1 Ocak, 20 Mart, 23 Nisan, 1 Mayıs, 19 Mayıs, 27-29 Mayıs, 15 Temmuz, 29 Ekim; hafta sonuna denk gelen 21-22 Mart, 30 Mayıs, 30 Ağustos; yarım gün 19 Mart, 26 Mayıs, 28 Ekim. 13-14 Temmuz yalnız takas kaydırma satırlarında geçiyor, tatil değil. Yarım günün kapanış saati çizelgede yok. 2027 çizelgesi yayımlanmamış ya da bulunamadı | PyPI tablosu; PDF metni çıkarıldı (G2), bu birleştirmede yeniden okundu |
| Pazar segmentleri | Yıldız Pazar, Ana Pazar, Alt Pazar ve GİP eşikleri eski bir aracı kurum belgesinden geliyor; güncel olmayabilir | Doğrulanmadı |
| Düzenleyici | SPK. Birincil kamuyu aydınlatma mevzuatı çekilmedi | — |
| Tier | **B (doğrulandı)**, Yahoo `.IS` üzerinden. Resmî ücretsiz rota bulunamadı (datastore'da ücretsiz ürün olup olmadığı test edilemedi). A adayı yok | Rubrik |

Crocodile için Türkiye'ye özgü sonuçlar:

- **Üretim rotası lisans gerektirir.** BIST verisinin resmî yolu yetkili dağıtıcıdır. Geçmiş veri için resmî yol BIST Tarihsel Veri Satışı'dır (datastore; GSDFV93 1993'ten düzeltilmiş kapanış sunuyor). 2021 tarifesi gecikmeli ve gün sonu dağıtım için ayda yaklaşık 190-350 USD gösteriyor; bu yalnız bir dayanak noktasıdır, güncel teklif istenmeli. Yahoo `.IS` yalnız araştırma ve çapraz doğrulama içindir. Gecikmeli veya EOD veriyle endeks hesaplamak da ayrıca lisansa tabidir. Crocodile kendi bileşik endekslerini veya göstergelerini dağıtacaksa bu madde önemlidir.
- **Foreks/ForInvest iddiası doğrulanmaya değer.** BIST verisi bir perakende kanaldan gerçekten gerçek zamanlı ve ücretsiz sunuluyorsa, bu yine de o kanalın son kullanıcı şartlarıyla bağlıdır. Crocodile'ın makine erişimi için bir izin sayılmaz.
- **KAP için iki seçenek var.** Sitenin otomatik erişim şartları okunup uygun bulunursa nazik bir HTML/PDF bağdaştırıcısı yazılabilir. Aksi halde bir lisanslı veri sağlayıcı kullanılır. Boşluk turunda robots.txt 666 hata sayfası döndürdü ve şartlar sayfası bulunamadı; karar hâlâ verilemiyor. Resmî olmayan PyPI sarmalayıcıları Crocodile'ın `evasion` ekindeki gibi varsayılan kurulumun dışında kalmalıdır.
- **Takvim için resmî kaynak var.** 2026 tatil ve yarım gün listesi BIST PDF'inden alınabilir ve `exchange_calendars` XIST takvimiyle çapraz test edilebilir. Yarım gün kapanış saati ve seans tablosu ayrıca okunmalı.
- **Tarih hatası BIST'i etkiler.** İstanbul UTC'nin doğusunda. yfinance günlük barı İstanbul gece yarısına çekiyor; Crocodile bunu UTC'ye çevirip tarihi UTC'den aldığı için BIST günlük barı ve temettü tarihi bir gün önceye yazılıyor (`yahoo/client.py:378-383` ve yfinance 1.5.2 `history.py:421` okundu, çalıştırılmadı; §8).
- **MIC tasarımı.** `listing.mic = XIST` (operating) ve `segment_mic = XEQY` kullanılmalı. VİOP, borçlanma ve kıymetli maden verisi aynı operating MIC altında segmentle ayrılır. EPİAŞ MIC'leri hisse evreninden açıkça dışlanmalı.

### 4.2 Orta Doğu

| Borsa | MIC | Tier | Rota ve not |
|---|---|---|---|
| Saudi Exchange (Tadawul) | XSAU | B | `2222.SR` (SAR). Yardım sayfası `.SAU` diyor ama çalışan sonek `.SR`. Resmî uygulama 5 dk gecikme belirtiyor (App Store, arama yoluyla). Site bu istemciye 403. Nomu için ayrı MIC yok, XSAU altında segment etiketi kullanılmalı |
| Tel Aviv (TASE) | XTAE | B | `TEVA.TA`: para birimi **ILA** (agorot, 1/100 ILS). DataHub kök adresi 200 döndü; anahtar şartı yalnız topluluk SDK'sından biliniyor |
| ADX, DFM, Nasdaq Dubai | XADS, XDFM, DIFX | D | `FAB.AD` 404 döndü (sembol veya sonek yanlış olabilir). DFM problanmadı |
| Katar, Kuveyt, Bahreyn, Umman, Ürdün, Lübnan, Irak, Filistin | DSMD, XKUW, XBAH, XMUS, XAMM, XBEY, XIQS, XPAE | D | Siteler çoğunlukla ulaşılamaz (000) veya 403. Kuveyt'in kayıttaki sitesi kuwaitse.com. Bahreyn'in MIC kısaltması BSE, Filistin'inki PSE. KWD, BHD, OMR, JOD ve IQD 3 ondalıklı |
| İran, Suriye | XTEH, XDSE | D | Yaptırım riski; SDN listesiyle karşılaştırılmadı |

### 4.3 Batı Avrupa

| Borsa | MIC | Tier | Rota ve not |
|---|---|---|---|
| Xetra / Börse Frankfurt | XETR / XFRA | **A** | Deutsche Börse MDS: ücretsiz, 15 dk gecikmeli pre/post-trade dosyaları. Dosya listesi `mfs.deutsche-boerse.com/api/<önek>` (önekler DETR-pretrade, DETR-posttrade, DFRA-pretrade, DFRA-posttrade), indirme `/api/download/<dosya>.json.gz` (imzalı depolama URL'sine 301). Dakikalık ve günlük post-trade dosyaları her iki borsa için 200/206 döndü: XETR 10:00 dosyası 1.183 kayıt, günlük dosya 23,1 MB gz; XFRA günlük dosyası 182.573 kayıt, 55.490 ISIN (FRAB Freiverkehr 141.004, FRAA regüle piyasa 41.440). Biçim satır başına bir MiFID II işlem bildirimi: ISIN, fiyat, miktar, para birimi, UTC zaman damgası. **OHLC ve EOD alanı yok**, günlük bar işlemlerden türetilmeli. **Saklama 1 iş günü**, geçmiş yok. Veri kullanım beyanı: ileri dağıtım Deutsche Börse'nin ön onayına tabi; ifade gerçek zamanlı veri için yazılmış, gecikmeli dosyalar için ayrı şart bulunamadı. Yahoo `SAP.DE` ve `ALV.F` de çalışıyor |
| Euronext (Paris, Amsterdam, Brüksel, Lizbon, Dublin, Milano, Oslo) | XPAR, XAMS, XBRU, XLIS, XDUB, XMIL, XOSL | B | Yedisinde de Yahoo meta doğrulandı (`.PA`, `.AS`, `.BR`, `.LS`, `.IR`, `.MI`, `.OL`). Euronext gecikmeli veri şartları (PDF okundu): yayından 15 dk sonra kullanım ücretsiz, üçüncü kişilere dağıtım lisans ve ücrete tabi. Makine-okunur ücretsiz uç doğrulanmadı |
| London Stock Exchange | XLON | B | `VOD.L`: para birimi **GBp** (pence). LSEG gecikmeli veri yalnız Workspace masaüstünde ücretsiz; programatik erişim borsa ücretine tabi olabilir. DMD şartları 403 |
| SIX Swiss Exchange | XSWX | B | `NESN.SW` (CHF). Atıf yapılan Exfeed fiyat listesi 404 döndü, ücret iddiaları desteklenmedi |
| Wiener Börse | XWBO | B | `OMV.VI`. XVIE ACTIVE bir segment (araştırmacının "XVIE yok" iddiası çürütüldü) |
| Börse Stuttgart | XSTU | B | `BMW.SG` |
| Alman bölge borsaları (München/gettex, Düsseldorf, Hamburg, Hannover, Berlin), Tradegate, BX Swiss | XMUN, XDUS, XHAM, XHAN, XBER, TGAT, XBRN | D (aday B) | Problanmadı. MDS bunları kapsamıyor; bunlar Deutsche Börse Grubu borsası değil |
| Aquis, LuxSE | AQXE, AQSE, XLUX | C (geçici) | Ücretsiz yol kanıtlanmadı. LuxSE tarihsel verisi lisanslı. aquis.eu 429 döndü |
| Cboe Europe | BCXE | D | Ticari erişim |

### 4.4 Kuzey Avrupa ve İberya

| Borsa | MIC | Tier | Rota ve not |
|---|---|---|---|
| Nasdaq Stockholm | XSTO | B | `VOLV-B.ST`, gövde doğrulandı. 12 aktif segment MIC'i (First North vb.) |
| Nasdaq Kopenhag, Helsinki | XCSE, XHEL | B | `NOVO-B.CO` (CPH, DKK) ve `NOKIA.HE` (HEL, EUR): boşluk turunda meta okundu, fixture'lar `fixtures/g8/` altında |
| BME (Madrid, Barselona, Bilbao, Valensiya) | BMEX | B | `SAN.MC`, gövde doğrulandı. Operating MIC BMEX; XMAD, XBAR, XBIL, XVAL segment. XMCE EXPIRED. BME Growth (GROW, MABX) bu satıra katıldı |
| Nasdaq İzlanda, Tallinn, Riga, Vilnius | XICE, XTAL, XRIS, XLIT | D | İzlanda için `MARL.IC` 404 (tahmini sembol). Baltık sonekleri problanmadı |
| Spotlight, NGM, Cebelitarık | XSAT, XNGM, GSXL | D | — |

Nasdaq `tradereports.nasdaq.com` erişilebilir, ama Nasdaq bunun MiFIR madde 13(1) ücretsiz veri yükümlülüğünün karşılığı olmadığını belirtiyor. Dosya biçimi doğrulanmadı.

### 4.5 Orta ve Doğu Avrupa, BDT

| Borsa | MIC | Tier | Rota ve not |
|---|---|---|---|
| Moskova (MOEX) | MISX | **A** (yaptırım bayrağı) | ISS anahtarsız JSON. Boşluk turunda `history/.../boards/TQBR/securities/SBER.json` ve `candles.json?interval=24` OHLCV döndürdü, iki uç aynı günlük değerleri veriyor. En erken tarih belirlenmedi (2000-01 ve 2010-01 aralıkları 0 satır). 15 dk gecikme iddiası doğrulanmadı; ISS sayfalarında gecikme veya lisans ifadesi yok, sözleşme PDF'ine bağlanılamadı. Temettü ucu temettü tablosu döndürmedi. `SBER.ME` de çalışıyor. Hukuk incelemesi olmadan üretime alınmamalı. Takvim kütüphanesi XMOS kullanıyor; ISO'da XMOS bir merkezi karşı taraf (CCP) |
| Varşova (GPW) | XWAR | B | `PKO.WA`. NewConnect (XNCO) bu satıra katıldı |
| Prag | XPRA | B (aday A) | `CEZ.PR`. PSE anonim FTP dizini listelendi (226). Readme `Results.ak` dosyasını ücretsiz diyor (120 dk gecikme), ama aralık isteği 0 bayt döndü; içerik doğrulanmadı |
| Budapeşte, Bükreş | XBUD, XBSE | B | `OTP.BD`, `TLV.RO` |
| Atina (Euronext Athens) | ASEX | B | `ETE.AT` (OPAP.AT 404). Borsa fiyat listesi: 15 dk gecikmeli veri borsa ücretinden muaf, BBO1 gecikmeli yeniden dağıtımı da muaf. Takvim kütüphanesi ASEX kodunu kullanıyor, nakit piyasa segmenti XATH |
| Zagreb, Ljubljana, KASE | XZAG, XLJU, XKAZ | D (aday A) | Resmî ücretsiz EOD ve web verisi var. Zagreb EOD'yi e-postayla Excel olarak veriyor, toplu teslim aylık 125 EUR. KASE 1 Ağustos 2024'ten beri günlük XLS/TXT/XML yayınlıyor, API lisanslı. Dosyalar çekilmedi |
| Taşkent | XSTE | C (geçici) | Açık veri portalı yalnız bir R paketi üzerinden biliniyor |
| Bulgaristan | XBUL | D | Para birimi EUR (ISO 4217 listesiyle doğrulandı, §3) |
| SPB, Ukrayna (PFTS, UX), Belarus | SPBE, PFTS, UKEX, BCSE | D | Yaptırım veya savaş riski |
| Bosna-Hersek (Saraybosna, Banja Luka), Belgrad, Üsküp, Podgorica, Kıbrıs, Malta, Gürcistan, Ermenistan, Bakü, Moldova, AIX | çeşitli | D | Ücretsiz rota bulunamadı |

### 4.6 Kuzey Amerika (ABD hariç) ve Karayipler

| Borsa | MIC | Tier | Rota ve not |
|---|---|---|---|
| TSX | XTSE | B | `RY.TO` (CAD). Yahoo yardım sayfası gerçek zamanlı diyor (ICE) |
| TSXV, CSE, Cboe Canada | XTSX, XCNQ, NEOE | D (aday B) | Sonekler (`.V`, `.CN`, `.NE`) yalnız yardım tablosunda. Problanan `SVM.V` ve `SVM.CN` geçersiz semboldü (404) |
| BMV | XMEX | B | `WALMEX.MX`, 20 dk. Twelve Data XMEX saat dilimini hatalı veriyor (America/Swift_Current) |
| BIVA, Bermuda, Jamaika, Trinidad, Barbados, Bahamalar, ECSE, Latinex, Kosta Rika, Cayman | çeşitli | D | Yahoo yardım tablosunda sonek yok. ECSE günlük PDF raporları erişilebilir |

Kanada dosyalamaları: SEDAR+ genel kamu araması ücretsiz, belgelenmiş API yok.

### 4.7 Güney Amerika

| Borsa | MIC | Tier | Rota ve not |
|---|---|---|---|
| B3 | BVMF | B | `PETR4.SA`, `VALE3.SA`. Ücretsiz COTAHIST türü dosya doğrulanmadı. CVM açık veri portalı iki kez Cloudflare 403 döndü. Twelve Data B3 saat dilimini hatalı veriyor (America/Paramaribo) |
| BYMA (Buenos Aires) | XBUE | B (hukuki bayrak) | `GGAL.BA`. BYMA'nın `llms.txt` dosyası gecikmeli web verisinin yeniden dağıtılamayacağını söylüyor. Gerçek zamanlı veri ve tarihsel API lisanslı |
| Santiago, Kolombiya (BVC) | XSGO, XBOG | B | `BSANTANDER.SN`, `ECOPETROL.CL`. ECOPETROL.CL için Yahoo meta `America/New_York` saat dilimi döndürdü; Yahoo saat dilimi alanı güvenilmez |
| Lima (BVL) | XLIM | B (geçici) | `BAP.LM` yalnız durum kodu |
| Montevideo, Asunción, Bolivya, Guayaquil, Quito, Karakas | çeşitli | D | Karakas için yaptırım riski. CLP ve PYG 0 ondalıklı |

### 4.8 Afrika

| Borsa | MIC | Tier | Rota ve not |
|---|---|---|---|
| Johannesburg (JSE) | XJSE | B | `NPN.JO`: para birimi **ZAc** (cent). jse.co.za 403. Resmî gecikmeli veri ücretli (şartlar okunmadı). A2X, CTS3, ZARX ve EESX ayrı operating MIC olarak not edildi |
| Mısır (EGX) | XCAI | B (geçici) | `COMI.CA` yalnız durum kodu. egx.com.eg ulaşılamadı |
| Ruanda, Darüsselam, Zimbabve (ZSE ve VFEX) | RSEX, XDAR, XZIM, VFEX | C | EODHD borsa sayfaları görüldü. EODHD ücretsiz paketi: anahtar, 20 çağrı/gün, 1 yıllık EOD; **yalnız kişisel kullanım**. Twelve Data seçeneği ücretsiz değil (Basic yalnız ABD). ZSE para birimi ZWG (ZiG), VFEX USD (yeniden doğrulanmadı) |
| Cezayir, Namibya, Tunus | XALG, XNAM, XTUN | D (aday C) | Boşluk turunda C'den indi: ücretsiz anahtarlı rota kalmadı. XTUN yalnız Twelve Data'ya dayanıyordu; XALG ve XNAM satırlarında hiç toplayıcı atfı yoktu. EODHD bu borsaları kapsıyorsa C'ye dönerler |
| Kazablanka, Gana, Botsvana, Malavi, Nijerya | XCAS, XGHA, XBOT, XMSW, XNSA | D (aday B) | `IAM.CS` 429, `DANGCEM.LG` 404 (sonuçsuz). kwayisi resmî olmayan API belgeleri var ama veri çağrısı doğrulanmadı. NGX Market Data API kayıt ve token istiyor, abonelik ücretli |
| Nairobi, BRVM, Mauritius, Lusaka, Uganda, MERJ, Mozambik, Esvatini, Yeşil Burun, Libya, Hartum, Angola | çeşitli | D | Nairobi gecikmeli veri sayfası 200 döndü, lisanslı. Uganda veri satış PDF'i 404. UGX ve RWF ISO'ya göre 0 ondalıklı |
| Etiyopya (ESX), BVMAC | MIC yok | D | ESX için ISO satırı yok. BVMAC için de yok; eski Douala MIC'i XDSX hâlâ ACTIVE |

### 4.9 Büyük Çin

| Borsa | MIC | Tier | Rota ve not |
|---|---|---|---|
| Tayvan (TWSE) | XTAI | **A** | OpenAPI `exchangeReport/STOCK_DAY_ALL` anahtarsız JSON (323 KB), ama yalnız son günün anlık görüntüsü. **Geçmiş** `www.twse.com.tw` uçlarında: `MI_INDEX?date=YYYYMMDD&type=ALLBUT0999` tarih başına tüm hisseleri OHLCV ile veriyor (2026-11-06 için 1.389 satır), `STOCK_DAY?date=&stockNo=` hisse başına bir ay. 2010-01-04'ten eski tarihler bir hata mesajıyla reddediliyor; en erken tarih bu mesajdan çıkarıldı. `TWT49U` tarih başına hak kullanımı referans fiyatlarını veriyor (stockNo yok sayılıyor). Fiyatlar düzeltilmemiş. **Lisans:** OpenAPI swagger'ı Government Open Data License v1'e bağlanıyor: amaç, süre ve yer sınırı yok, ticari kullanım ve türev ürün serbest, atıf zorunlu, atıfsız kullanımda lisans hiç verilmemiş sayılıyor. `www.twse.com.tw` site şartları ise yazılı izin istiyor, Open Data Platform'a yetkilendirilen veri hariç; per-date uçların bu istisnaya girdiği doğrulanmadı. Yaklaşık 8 istek 1 sn arayla 429 almadı, limit ölçülmedi. `t187ap03_L` şirket listesi 1.099 satır, ISIN ve ETF yok. TWSE sayfası 2015'ten beri %10 fiyat limiti ve 1.000 hisselik lot ile uyumlu ifadeler içeriyor |
| Taipei Exchange (TPEx) | ROCO | B | `6488.TWO`. TPEx OpenAPI belgeli, ama tüm TPEx uçları bu istemciye 403 döndü. data.gov.tw veri seti sayfası OGDL 1.0 lisansını gösteriyor; kaynak URL'si yine 403 |
| Şanghay, Shenzhen | XSHG, XSHE | B | `600519.SS`, `000001.SZ`; yardım sayfasında 30 dk. SSE veri hizmeti sayfası: Level-1 ve Level-2 lisanslı, yeniden dağıtım yalnız nitelikli satıcılara, anakara dışı başvurular CIIS üzerinden. CNINFO `szse_stock.json` anahtarsız (592 KB) ama belgesiz. SZSE sayfası 09:30-11:30, 13:00-14:57 ve 14:57-15:00 kapanış müzayedesini, ChiNext için %20 limiti doğruluyor |
| Pekin (BSE) | BJSE | D (aday B) | bse.cn 345 baytlık bir yanıt döndü; veri ucu problanmadı |
| Hong Kong (HKEX) | XHKG | B | `0700.HK`. Resmî `ListOfSecurities.xlsx` anahtarsız (1,3 MB): referans verisi, fiyat yok. Son kullanıcı lisans sözleşmesi PDF'i erişilebilir, tam özetlenmedi |

AKShare (MIT kodu; veri Eastmoney ve Sina'dan; README akademik kullanım diyor), Tushare Pro (token ve puan) ve Baostock (lisans doğrulanmadı) topluluk kaynaklarıdır. Lisanslı veri kaynağı sayılmamalıdır.

### 4.10 Japonya, Kore, Moğolistan

| Borsa | MIC | Tier | Rota ve not |
|---|---|---|---|
| Tokyo (JPX) | XJPX | B; resmî rota C | `7203.T`, `6758.T`. **J-Quants Free**: ücretsiz hesap ve anahtar; Listed Issue Master ve OHLC için son 12 hafta hariç 2 yıl; 5 istek/dk; Free planda CSV yok (canlı fiyat sayfası boşluk turunda yeniden okundu). Ücretli planlar: Light 1.650 JPY/ay (60/dk, 5 yıl), Standard 3.300 JPY (120/dk, 10 yıl), Premium 16.500 JPY (500/dk, 20 yıl). Kullanım şartları sayfası bulunamadı; ticari kullanım ve dağıtım açık. JPX şirket listesi `data_j.xls` bağlantısı 404 (eski). `api.jpx-jquants.com` bu sandbox'tan üç denemede de ulaşılamadı (000); bu API hakkında bir kanıt değildir. Prime, Standard ve Growth ISO MIC değil; segment bilgisi J-Quants'ın market alanından gelmeli. XTKS, XTAM ve XTK1-3, XJPX'in segmentleri |
| Kore (KRX) | XKRX | B; resmî rota C | `005930.KS`, `247540.KQ` (KOSDAQ). **KRX Open API**: anahtar başvurusu, ticari olmayan kullanım, üçüncü kişiden ücret alınamaz, KRX'ten alınan bilgi üçüncü kişiye sağlanamaz, anahtar başına 10.000 istek/gün (şartlar okundu). data.go.kr geliştirme hesabı 10.000 çağrı veriyor; veri seti KOGL 4. tip (atıf, ticari kullanım yasak, değiştirme yasak), üçüncü kişiye sağlama ve yeniden dağıtım amaçtan bağımsız olarak yasak, ticari kullanım için KRX Data Marketplace'ten ücretli satın alma gerekiyor. KIND şirket listesi indirmesi 200 döndü (EUC-KR, yaklaşık 2.766 satır, ayrıştırılmadı). XKOS ve XKON segment; XKFB (Freeboard) ve NXTE (Nextrade ATS) ayrı operating MIC |
| Nagoya, Fukuoka, Sapporo | XNGO, XFKA, XSAP | D | Sonek kanıtı yok. `.F` Frankfurt'tur, Fukuoka için kullanılmamalı |
| Moğolistan | XULA | D | Yalnız web |

Bölgede eksik aday olarak XJAX (Japan Alternative Market, ATS) ve SBIJ (Japannext) not edildi.

### 4.11 Güney Asya

| Borsa | MIC | Tier | Rota ve not |
|---|---|---|---|
| NSE, BSE | XNSE, XBOM | B | `TCS.NS`, `RELIANCE.BO`. NSE'nin All Reports sayfası 200 döndü, ama bhavcopy bağlantıları JavaScript ile üretiliyor. Boşluk turunda `archives.nseindia.com/content/equities/EQUITY_L.csv` 200 döndü: 2.632 satır, ISIN var, SERIES (EQ 2.398, BE 200, BZ 34), ETF yok. `nseindia.com/api/corporates-corporateActions?index=equities` 200 ve JSON (exDate, recDate, isin, faceVal); belgesiz iç uç, şartları okunmadı. niftyindices Nifty 50 bileşen CSV'si 500 döndü. Ana sayfa ve BSE bhavcopy sayfası 403. Standart BSE bhavcopy dosyası 8 Temmuz 2024'te kaldırılmış (önceki tur). XNSE `exchange_calendars` tablosunda yok, XBOM var |
| Pakistan (PSX) | XKAR | B | `HBL.KA`. dps.psx.com.pk portalı 200, ama sembol başına EOD yolu 404 |
| Dakka, Chittagong, Kolombo, Nepal, Maldivler | çeşitli | D | `.DH` ve `.CM` 404. "CSE" kısaltması hem Chittagong hem Kolombo için kullanılıyor; anahtar her zaman MIC olmalı |
| Bhutan RSEB, Afganistan | MIC yok | D / borsa yok | ISO'da BT ve AF satırı yok |

### 4.12 Güneydoğu Asya

| Borsa | MIC | Tier | Rota ve not |
|---|---|---|---|
| SGX | XSES | B | `D05.SI` (SGD). Gerçek zamanlı yeniden dağıtım SGX lisansı istiyor |
| Endonezya (IDX) | XIDX | B | `BBCA.JK` (IDR), 10 dk. XJKT 2008'de EXPIRED. IDX veri katalogunda 1 Ocak 2027'den geçerli ücret revizyonu var |
| SET | XBKK | B | `PTT.BK`: boşluk turunda meta okundu (exchangeName SET, THB, Asia/Bangkok). SETSMART ücretli (2023: 250 THB/ay) |
| Bursa Malaysia, HOSE | XKLS, XSTC | B (geçici) | `1155.KL`, `FPT.VN` yalnız durum kodu. vnstock SSI, TCBS ve VNDirect uçlarını kullanıyor; bir veri lisansı değil |
| Filipinler, Hanoi | XPHS, HSTC | D (aday B) | `BDO.PS`, `TEL.PS`, `SMPH.PS`, `SHS.HN` 404 |
| Kamboçya, Laos, Myanmar (MIC yok), Brunei ve Doğu Timor (borsa yok) | — | D | — |

### 4.13 Okyanusya

| Borsa | MIC | Tier | Rota ve not |
|---|---|---|---|
| ASX | XASX | B | `CBA.AX`, `BHP.AX`. `asx.api.markitdigital.com` anahtarsız JSON döndü, ama belgesiz ve lisanssız. Resmî kaynak sayılmadı. `ASXListedCompanies.csv` 200: 2.060 satır (ad, kod, GICS grubu), ISIN yok. BHP.AX günlük zaman damgası önceki gün 23:00 UTC, yani ham UTC tarihi seans tarihinden bir gün önce |
| NZX | XNZE | B | `AIA.NZ`. Yahoo exchangeName NZE kullanıyor. Resmî veri lisanslı |
| TMX Australia (eski Cboe Australia), NSX, SSX, SIM Venture, Bendigo, PNGX, SPX | çeşitli | D | PNGX 403 döndü, aşılmadı. NZX SME pazarı NXT'nin MIC'i yok |

### 4.14 ABD (referans)

XNYS ve XNAS için Yahoo `AAPL` doğrulandı (B). Crocodile'da zaten alpaca, finnhub, tiingo (ayda 500 benzersiz sembol), sec_edgar ve finra sağlayıcıları var. `nasdaqlisted.txt` sembol dizini 200 döndü; boşluk turunda `otherlisted.txt` de 200 döndü (7.666 satır, ETF sütununda 4.495 "Y", borsa kodları N=NYSE, P=NYSE Arca, Z=Cboe BZX, A=NYSE American, V=IEX). Twelve Data'nın ücretsiz Basic planı yalnız ABD'yi kapsıyor ve dahili, ticari olmayan kullanımla sınırlı. sec.gov bu çalışmanın User-Agent'ına 403 döndü; Crocodile'ın kendi beyan edilmiş UA'sıyla erişim ayrıca test edilmeli. XCBO ve IEXG, mevcut alpaca IEX beslemesi nedeniyle C olarak işaretlendi.

## 5. Çok ülkeli kaynakların karşılaştırması

| Kaynak | Anahtar | Matristeki kapsam | Gecikme | Limit ve ToS | Rol |
|---|---|---|---|---|---|
| Yahoo chart (resmî olmayan) | Yok | 50 MIC'de meta doğrulandı, 4'ünde yalnız durum kodu, 9'u listeli ama problanmadı, 8'i sonuçsuz | Yardım sayfasında sonek başına (.IS 15m, .L 20m, .T 20m, .SS/.SZ 30m, .NS gerçek zamanlı); ölçülmedi | ToS otomatik erişimi yasaklıyor (sayfa okundu). Kota belgesiz; `IAM.CS` 429 döndü, boşluk turundaki 16 istekte 429 yok. Meta alt birim döndürüyor (GBp, ZAc, ILA). Saat dilimi alanı güvenilmez. `close` temettüye göre düzeltilmemiş, `adjclose` düzeltilmiş (8 sonekte görüldü); bölünme kapsamı test edilmedi | Ar-Ge ve QA oracle'ı; üretim değil |
| Twelve Data | Veri uçları için var; `/exchanges` anahtarsız | 97 kod, 58 ülke; 76 operating MIC matrisle örtüşüyor. XTKS, XJKT ve XSGX yok, XJPX var | Doğrulanmadı | **Basic (0 USD) yalnız ABD**, 8 kredi/dk, 800/gün, dahili non-display; şartlar ücretsiz katman verisinin ticari kullanımını yasaklıyor. Bireysel Grow 29 USD (20+ piyasa, küresel EOD), Pro 99 USD (70+), Ultra 329 USD (tümü); ticari kullanım için Venture 149 USD (70+). Yeniden dağıtım yalnız ek lisansla. BVMF ve XMEX saat dilimleri hatalı | MIC referansı; ABD dışı için ücretli seçenek |
| EODHD | Var | "70 borsa" satıcı iddiası; EGX, AU, KAR, GPW, KLSE, DSE, RSE ve VFEX sayfaları görüldü | Doğrulanmadı | Ücretsiz paket: 20 çağrı/gün, 20 istek/dk, EOD geçmişi 1 yıl. Fiyat sayfasındaki paketler **yalnız kişisel kullanım**; ticari kullanım ayrı lisans. Ücretsiz planın borsa listesi doğrulanmadı | Yalnız ücretli senaryoda |
| Stooq | Yok | Hiçbiri | — | Bölge turlarında JavaScript proof-of-work sayfası, boşluk turunda ve yeniden kontrolde bağlantı zaman aşımı (000). Şartlar okunmadı | Bu ortamda kullanılamaz; mevcut bağlayıcı en az bir başka ortamda yeniden değerlendirilmeli |
| Google Finance | Yok | Yalnız `THYAO:IST` doğrulandı | — | API yok; otomatik erişim şartları okunmadı | Kullanılmamalı |
| MSN Money | Yok | Problanmadı (Crocodile bağlayıcısı var) | — | `market=en-us` sabit | Değerlendirilmedi |
| TradingView scanner | Yok | Problanmadı | — | Garanti edilmiş public API değil; ToS okunmadı; hukuk incelemesi olmadan entegre edilmemeli | Dışarıda |
| Investing.com | Yok | Problanmadı | — | ToS okunmadı | Dışarıda |
| Massive (eski Polygon) | Var | Hisse ve opsiyon yalnız ABD (KB sayfası okundu) | — | — | Küresel için uygun değil |
| Finnhub, FMP, Alpha Vantage, Tiingo, Marketstack | Var | Ücretsiz katman kapsamı tartışmalı veya doğrulanmadı. FMP sayfaları 403 döndü. Tiingo ağırlıklı ABD | — | Kota iddiaları çelişkili | Ayrı doğrulama gerekir |
| Nasdaq Data Link | Hesap | Uluslararası fiyat ürünleri premium; iki belge bağlantısı 404 | — | — | Dışarıda |
| OpenBB | Sağlayıcıya göre | Kütüphane; her sağlayıcının şartları geçerli | — | — | Doğrudan kaynak değil |

Toplayıcı tarafındaki sonuç şu: ücretsiz ve anahtarsız çalışan tek yaygın yol Yahoo, ve şartları bu kullanımı yasaklıyor. Ücretsiz anahtarlı toplayıcılar da bir çıkış sunmuyor: Twelve Data'nın ücretsiz planı ABD dışını kapsamıyor, EODHD'ninki kişisel kullanımla sınırlı. Crocodile'ın dürüstlük kuralı gereği bu kaynaktan gelen her kayıt etiketlenmeli. Bu kayıtlar yeniden dağıtılan bir veri ürününe girmemeli.

## 6. Başvuru verisi

**MIC (ISO 10383).** Listeleme tanecikli olarak operating MIC kullanılmalı, segment MIC bir öznitelik olarak tutulmalı. Örnekler: XIST/XEQY, XJPX/XTKS, XKRX/XKOS, BMEX/XMAD, ASEX/XATH, XWBO/XVIE, HSTC/XHNX. Kütüphaneler ve satıcılar farklı seçimler yapıyor. `exchange_calendars` XTKS, XMAD ve XMOS kullanıyor; XMOS ISO'da bir CCP. Twelve Data XJPX kullanıyor. OpenFIGI'nin `exchCode` değerleri Bloomberg kodlarıdır (UW, LN, JT), MIC değildir. Bu nedenle kaynak başına test edilmiş eşleme tabloları gerekir. Süresi dolmuş kodlar (XJKT, XMCE, XBSP, XTUR, XIAB, XNDX, XNMR) reddedilmelidir. Pazar kategorisine göre süzme yapılmamalıdır (§2.1).

**ISIN (ISO 6166).** 2 harf ülke kodu, 9 karakter NSIN ve Luhn kontrol hanesinden oluşur (A=10 … Z=35). Örnek: US0378331005. ISIN öneki ihraç ülkesidir, listeleme ülkesi değildir. ADR'ler US, İrlanda menşeli ETF'ler IE taşır, bir ISIN'in birden çok listelemesi olur. Bu yüzden ISIN instrument düzeyini anahtarlar, listeleme düzeyini anahtarlamaz.

**FIGI ve LEI.** OpenFIGI belge sayfası 200 döndü. `/v3/mapping` GET isteğine 405 verdi, yani POST bekliyor. Oran limitleri boşluk turunda netleşti: eşleme anahtarsız 25 istek/dk, anahtarlı 250 istek/dk (önceki "25/6 sn" ifadesiyle aynı); arama ve filtre anahtarsız 5/dk, anahtarlı 20/dk; günlük, haftalık ve aylık limit yok. Şartlar FIGI tanımlayıcılarını kamu malına adıyor, yani tanımlayıcılar serbestçe saklanıp dağıtılabilir. `exchCode` ile `micCode` aynı işte birleştirilemez. Composite FIGI ülke başına, venue FIGI MIC başına tanımlıdır. GLEIF API anahtarsız JSON döndü: 3.488.078 kayıt, golden copy 2026-11-07. GLEIF verisi CC0 1.0 lisanslı (şart sayfası okundu): ticari kullanım, depolama ve yeniden dağıtım serbest.

**Para birimi ve fiyat ölçeği.** SIX ISO 4217 list-one (2026-09-17) birincil kaynak olarak doğrulandı. Alt birim tuzakları:

| Durum | Örnek | Kanıt |
|---|---|---|
| Alt birimle kotasyon | LSE GBp/GBX, JSE ZAc, TASE ILA | Yahoo meta (VOD.L, HSBA.L, NPN.JO, TEVA.TA). Borsaların birincil belgeleri okunmadı |
| 0 ondalık | JPY, KRW, VND, CLP, ISK, PYG, UGX, RWF, XOF, XAF | ISO listesi |
| 3 ondalık | KWD, BHD, OMR, JOD, TND, LYD, IQD | ISO listesi |
| Değişen para birimi | Bulgaristan EUR (BGN listede yok), Zimbabve ZWG (ZWL listede yok), Hırvatistan EUR | ISO listesi |
| ICU ve ISO farkı | Node Intl HUF için 0 ondalık gösteriyor, ISO 2 diyor | Bölge notu |

Para birimi alanı ölçeksiz tutulursa alt birimle kotasyon yapan listelemeler 100 kat yanlış fiyatlanır. Kod haritası da bunu ana risklerden biri olarak sayıyor.

**Takvim.** `exchange_calendars` lisansı Apache-2.0 (doğrulandı); 4.13.2 tablosunda 68 takvim var, matristeki 62 borsaya eşleniyor. XNSE, XSHE ve XNAS tabloda yok. `pandas_market_calendars` MIT lisanslı (doğrulandı), PyPI açıklamasında MIC listesi yok; `exchange_calendars`'ı yansıttığı iddiası doğrulanmadı. `exchange_calendars_extensions` 0.10.4'te 25 kod var, lisans alanı boş. Öneri: birincil kaynak borsanın kendi takvim sayfası ya da duyurusu olsun, kütüphane ikincil kalsın, ve Crocodile'ın kendi XNYS kuralları kütüphaneyle bir yıl aralığında çapraz test edilsin. Hiçbir borsanın makine-okunur tatil beslemesi doğrulanmadı. Resmî PDF olarak okunan tek çizelge BIST 2026 (§4.1). Hafta tanımı her yerde Pazartesi-Cuma değil. Tadawul'un hafta yapısı borsa başına teyit edilmeli.

**Saat dilimi.** Matristeki bütün saat dilimleri IANA zone.tab'da bulunuyor. Kanonik adlar saklanmalı. ICU, Asia/Kolkata, Asia/Ho_Chi_Minh ve Buenos Aires için eski takma adları tercih ediyor. Yahoo `exchangeTimezoneName` alanı ECOPETROL.CL için America/New_York döndürdü. Twelve Data B3 ve BMV için yanlış dilim veriyor. Saat dilimi her zaman MIC'ten türetilmeli.

**Döviz kuru.** ECB SDMX anahtarsız: EXR.D.USD.EUR, 2026-11-06 için 1.1165. ECB yasal uyarı sayfası, yeniden üretilen verinin doğru verilmesini ve ECB'nin kaynak gösterilmesini istiyor; istatistik yeniden kullanım politikası URL'si 404 döndü. Frankfurter v2 anahtarsız, ECB kaynaklı ve harmanlanmış kurlar veriyor. FRED grafik CSV'si anahtarsız çalışıyor, JSON API anahtar istiyor (anahtarsız istek 400 döndü). Rusya Merkez Bankası XML anahtarsız (yaptırım bağlamı). Kod haritasının kuralı geçerli: kur dönüşümü her zaman DERIVED'dır, en kötü girdi seviyesini taşır ve sessiz yapılmaz.

## 7. Dosyalama ve temel veri sistemleri

| Pazar | Sistem | Erişim | Durum |
|---|---|---|---|
| AB (ESEF kapsamı) | filings.xbrl.org | Anahtarsız JSON API | **Doğrulandı** (`/api/filings` 200). Ülke OAM'leri ayrıca test edilmeli. Ana sayfada "Terms of use" bağlantısı var, adresi bulunamadı; lisans doğrulanmadı |
| Tayvan | MOPS ve TWSE OpenAPI | OpenAPI anahtarsız | `t187ap03_L` doğrulandı; MOPS JavaScript yönlendirmesi |
| Küresel ihraççı | GLEIF | Anahtarsız | Doğrulandı |
| Japonya | EDINET API v2, TDnet | EDINET Subscription-Key istiyor; TDnet API ücretli (5 yıl geçmiş) | EDINET anahtarsız istekte 401 döndü; kılavuz sayfası 404, şartlar okunmadı. `release.tdnet.info` bu sandbox'tan 000 |
| Kore | DART / OpenDART | Kayıtla anahtar | Ana sayfa 200; oran limitleri doğrulanmadı. Şartlar sayfası okundu: OpenAPI telif hakkı FSS'de, şartlarda olmayan konularda kamu verisi yasası geçerli; ticari kullanım ve dağıtım maddesi bulunamadı |
| Birleşik Krallık | Companies House API, FCA NSM, RNS | Companies House ücretsiz anahtar; NSM web araması ve CSV | Belge düzeyinde |
| Türkiye | KAP | HTML/PDF portal, API yok; bildirim sorgusu EXCEL/PDF dışa aktarıyor | Ana sayfa 200; robots.txt 666 hata sayfası; şartlar bulunamadı |
| Çin | CNINFO | HTML/PDF; `szse_stock.json` anahtarsız ama belgesiz | 200 |
| Hong Kong | HKEXnews | Web | Problanmadı |
| Avustralya | ASX Announcements (markitdigital) | Belgesiz JSON | 200, lisanssız |
| Brezilya | CVM Dados Abertos | CKAN CSV ZIP | Cloudflare 403 |
| Kanada | SEDAR+ | Ücretsiz genel arama, API yok | 200 |
| ABD | SEC EDGAR | Crocodile sec_edgar | Bu çalışmanın UA'sına 403 |
| Güney Afrika | SENS | Web PDF | Cloudflare challenge |
| Diğerleri | SGXNet, Bursa, IDX, SET, PSE EDGE, HOSE/SSC, CMF, SMV, Emisnet, Tadawul/Argaam, SIX SER | Web | Problanmadı |

Temel veri (finansal tablo) için tek bir ücretsiz küresel kaynak yok. ESEF, AB'de makine-okunur iXBRL sunduğu için en büyük kazanımdır. Japonya (EDINET XBRL) ve Kore (OpenDART) ücretsiz anahtarla yapılandırılmış veri sunar. Mevcut Yahoo temel veri yolu birimi `"USD"` olarak sabit yazıyor (`yahoo/client.py:775`). ABD dışında bu doğrudan yanlış veri üretir. Ayrıca `FundPeriod` yarıyıllık dönemleri içermiyor.

### 7.1 Kurumsal işlemler

Boşluk turunda (G4) 21 büyük MIC için temettü, bölünme, kod değişikliği, kotasyondan çıkma ve işlem durdurma kaynakları arandı. Kanıt düzeyleri: P1 = 200 ve beklenen veri alanları görüldü; P0 = 200 ama HTML ya da ana sayfa; N = bulunamadı.

| MIC | Temettü / bölünme | Kod değişikliği, kotasyondan çıkma, durdurma |
|---|---|---|
| XNSE | **P1**: `nseindia.com/api/corporates-corporateActions?index=equities` JSON (exDate, recDate, isin, faceVal, defter kapanış tarihleri). Belgesiz iç uç; olay türü bazında ayrıştırılmadı | N |
| XTAI | **P1**: `www.twse.com.tw/exchangeReport/TWT49U` tarih başına hak kullanımı referans fiyatları (G1). OpenAPI'deki TWT48U yolu HTML 404, TWT49U yolu JSON değil | N |
| MISX | P0: `/iss/securities/SBER/dividends.json` 200, ama yanıtta temettü tablosu yok (bu birleştirmede dosya yeniden okundu) | N |
| XKRX, XHKG, XASX, BVMF | P0: KIND, HKEXnews, ASX ve B3 ana sayfaları erişilebilir; veri uçları problanmadı | N |
| XIST | KAP bildirim sorgusu (kategori eşlemesi açık), SPK bültenleri; ayrıntı §4.1 | N |
| XJPX, XBOM, XSHG, XSHE, XLON, Euronext, XETR/XFRA, XSWX, XTSE, XSAU | N | N |

Toplayıcı tarafında Yahoo chart `events=div,split` 8 sonekte (.TW .T .KS .NS .HK .SS .DE .AX) temettü olayları döndürdü. `close` her temettü ödeyicide `adjclose`'tan farklıydı: `close` düzeltilmemiş, `adjclose` temettüye göre düzeltilmiş. İki yıllık pencerede hiçbir sembolde bölünme yoktu, bu yüzden bölünme kapsamı test edilmedi. Crocodile için sonuç: Yahoo `close` toplam getiri serisi olarak kullanılmamalı; düzeltilmiş seri seçimi kayıtta açıkça belirtilmeli; durdurma ile veri boşluğunu ayırmak için MIC başına resmî bir durdurma kaynağı gerekiyor ve hiçbiri doğrulanmadı. Kod değişikliği, kotasyondan çıkma ve durdurma için hiçbir resmî liste doğrulanmadı.

### 7.2 Evren dizinleri, ETF işaretleri ve endeksler

Anahtarsız resmî listeleme dizinleri (G5, birer kopya indirildi):

| MIC | Dizin | Satır | ISIN | Tür / ETF |
|---|---|---|---|---|
| XNSE | `archives.nseindia.com/content/equities/EQUITY_L.csv` | 2.632 | Var | SERIES sütunu (EQ, BE, BZ); ETF yok |
| XASX | `asx.com.au/asx/research/ASXListedCompanies.csv` | 2.060 | Yok | GICS grubu; ETF yok |
| ABD | `nasdaqtrader.com/dynamic/SymDir/otherlisted.txt` ve `nasdaqlisted.txt` | 7.666 (otherlisted) | Yok | ETF sütunu (4.495 "Y"), test ihracı 29 |
| XTAI | `mopsfin.twse.com.tw/opendata/t187ap03_L.csv` | 1.099 | Yok | Sektör; ETF ve fon yok |
| XKRX | `kind.krx.co.kr/corpgeneral/corpList.do?method=download` | yaklaşık 2.766 | Kontrol edilmedi | EUC-KR HTML tablo, ayrıştırılmadı |

Dosya bağlantısı doğrulanamayanlar: JPX (`data_j.xls` 404), Xetra (sayfa 200, CSV bağlantısı çıkarılmadı; tahmin edilen URL 404), TMX (iki yol 404), LSE, Euronext ve B3 (sayfa 200, indirme bağlantısı bulunamadı), SIX (404), MOEX (404), Tadawul (403), SSE (sayfa 200, ayrıştırılmadı), SZSE (000), BIST (KAP sayfası JavaScript). Hiçbir dizinin geçmiş anlık görüntü arşivi bulunamadı; hayatta kalma yanlılığı (survivorship) için dizinler bugünden itibaren günlük saklanmalı.

ETF segment MIC'leri (ISO CSV): ARCX ve XASE (XNYS segmenti), BATS (XCBO), XNMS (XNAS), XETA (XETR regüle piyasa), XETF (Euronext ETF Access, XPAR), XQOD (SIX ETF QOD), ESTO (Nasdaq Stockholm Norway ETF), ETFP (Borsa Italiana ETF/ETC/ETN piyasası, XMIL segmenti). Xetra için "XTF" diye bir MIC kayıtta yok, LSE ETF segmentinin ayrı MIC'i de bulunamadı. Bu nedenle ETF işareti MIC'ten değil dizinden ya da enstrüman türünden gelmeli.

Endeks bileşenleri ve lisansları hiçbir pazar için doğrulanmadı (BIST 100, Nifty 50, TOPIX, DAX, FTSE 100, IMOEX, S&P 500, Nasdaq-100). niftyindices CSV'si 500, BIST endeks bileşenleri sayfası 000 döndü; MOEX IMOEX ve FTSE Russell sayfaları 200 ama ayrıştırılmadı. Endeks verisi çoğu yerde ayrı lisansa tabidir (BIST için duyuru 13712); Crocodile endeks üyeliğini kullanmadan önce her endeks sağlayıcısının şartları okunmalı.

## 8. Crocodile koduna entegrasyon noktaları

Kaynak: `codemap.md`, Crocodile HEAD `f8f2800` (0.3.1). [v] işaretli satırlar kod haritası yazarınca yeniden okundu. [inf] işaretli olanlar fixture testi gerektiren çıkarımlardır.

| Konu | Bugün | Gereken değişiklik |
|---|---|---|
| Kimlik | `securities.symbol TEXT PRIMARY KEY COLLATE NOCASE` (`equity/reference/master.py:52` [v]); evren büyük harfe çevrilmiş ticker'la birleşiyor (`universe.py:493` [v]). Aynı ticker'lı ikinci listeleme sessizce üzerine yazılıyor | `issuers`, `instruments`, `listings (mic, local_symbol)` ve `symbol_aliases` tabloları eklenmeli, `securities` ABD için uyumluluk görünümü olarak kalmalı. `resolve_ticker` venue argümanı almalı veya belirsizlikte reddetmeli. **İlk iş** |
| Para birimi | Hiçbir kayıtta yok; tek kaynağı Tiingo'dan bellekte kurulan harita (`universe.py:709-713` [v]); Yahoo temel verisi `unit="USD"` (`yahoo/client.py:775` [v]) | Stage A: para birimi ve fiyat ölçeği listeleme ana kaydında. Stage B: `OHLCV`, `Trade`, `Quote`, `CorporateAction` üzerinde kw_only nullable alan |
| Tarih kodlaması | Yahoo günlük barı yerel gece yarısından UTC'ye çevriliyor, ex_date de bundan alınıyor (`yahoo/client.py:378-383`, ~435, ~452 [v]). Kurulu yfinance 1.5.2 günlük indeksi `exchangeTimezoneName` diliminde yerel gece yarısına çekiyor (`scrapers/history.py:421`, birleştirmede okundu; temettü ve bölünme indeksleri de aynı). Stooq UTC gece yarısı kullanıyor (`stooq/connector.py:590-597` [v]) | Karar: günlük barın `source_ts` değeri, borsa-yerel seans tarihinin UTC gece yarısıdır. Yerel tarih MIC'ten türetilen saat diliminde alınmalı (Yahoo'nun saat dilimi alanı güvenilmez). "UTC'nin doğusu" kuralına göre değil, yerel tarihe göre düzeltilmeli. Fixture testi: THYAO.IS, 7203.T, 2330.TW, BHP.AX (`fixtures/g8/`) |
| Takvim ve zamanlayıcı | `MARKET_TZ = America/New_York` (`core/scheduler/calendar.py:7` [v]); tek `USMarketCalendar`; saat ET (`coordinator.py:228-230` [v]); `src/` içinde zamanlayıcıyı çağıran yok [v] | MIC anahtarlı `ExchangeCalendar` protokolü (öğle arası, yarım gün, hafta tanımı), `exchange_calendars` bağdaştırıcısı ve eşleme tablosu (XJPX→XTKS, BMEX→XMAD, MISX→XMOS), kripto için `AlwaysOpenCalendar`, görev başına takvim, kalıcı hata sayacı, boşluk kaydı |
| Pazar boyutu | `AssetClass` yalnız crypto ve equity (`enums.py:29-42` [v]); `resolve_asset_class` iki pazar varsayıyor (`surfaces/dispatch.py:287-361` [v]) | Ülke ve MIC birer veri boyutu olmalı: `MarketsParams`/`UniverseParams` üzerinde `mic` ve `country` opsiyonel alanları. Ülke başına `AssetClass` eklenmemeli. Kripto yarısı `mic` aldığında açıkça reddetmeli |
| Sembol ayırıcı | `:` dispatch'te `source:RAW` anlamına geliyor | ABD dışı kanonik sembol `:` ve `.` içermemeli. Aday `{yerel}@{MIC}` (ör. `7203@XTKS` veya kütüphaneye göre `7203@XJPX`). Açık soru §11 |
| Kaynak ekleme | `_REGISTRY` (`equity/providers/factory.py:23-34` [v]) ve `supported_channels` (`base.py:60` [v]) | Kaynak başına `SymbolGrammar` (`to_source(listing)`, `from_source(raw)`, MIC anahtarlı sonek tablosu, gidiş-dönüş testi) ve `supported_mics` beyanı. Fabrika ABD'ye özgü bir kaynağı ABD dışı listeleme için reddetmeli |
| Mevcut bağlayıcılar | Stooq tek pazar yönlendiricisi ve sezgisel (`stooq/connector.py:166-184` [v]); Google adayları yalnız NASDAQ/NYSE (`google_finance/connector.py:50-58` [v]); `$` koşulsuz USD (`:78-87` [v]); MSN `market=en-us` (`msn_money/connector.py:441-450` [v]) | Stooq bu ortamdan veri vermiyor (önce bot sayfası, boşluk turunda bağlantı zaman aşımı); küresel planda yer almamalı. Google ve MSN için MIC→kod tabloları ve ISO 4217 ayrıştırması gerekir; ancak bunlar kazıma kaynaklarıdır |
| OpenFIGI | `id_type="TICKER"` sabit (`universe.py:757` [v]); ilk eşleşme tutuluyor (`:523-528` [v]); `FigiRecord` MIC ve para birimini düşürüyor [m] | ISIN ve MIC kapsamlı işler, tüm venue eşleşmeleri, exchCode→MIC tablosu. Anahtarsız 25/dk bütçe paylaşılmalı |
| Referans birleştirme | `REFERENCE_PRIORITY = (tiingo, sec_edgar, openfigi)` (`universe.py:101` [v]); `reference_merge` sabit payda 3 (`provenance.py:619-645` [v]) | Pazar başına öncelik tablosu ve yeni bir temel `reference_merge_market` (payda = `len(declared_sources(mic))`) |
| Oran ve hata | Kova istemci başına; equity DLQ hiç boşaltılmıyor (`base.py:114` [v]) | Kaynak ve host anahtarlı paylaşılan `TokenBucket` kaydı `core/ratelimit` içinde; DLQ için `db_path` ve `report_drained_dlqs` |
| Satır sınırı | `NETWORK_ROW_LIMIT = 10_000` (`dispatch.py:72` [v]) | `universe`, `markets` ve `census` için `mic`/`country` süzgeci veya sayfalama |
| Paket yeri | `equity/` 202 bulguluk ruff bütçesinde eski borç (`test_phase1_exit.py:488` [v]) | Yeni kod temiz bir pakette olmalı, ör. `src/crocodile/core/markets/` (MIC kaydı, takvim, sembol grameri). Yeni üst paketler aynı commit'te `_CLEAN_PACKAGES`'e eklenmeli |
| Katmanlama | `core/connector.py:40` ve `core/capture/schema.py:7` crypto'yu import ediyor [v] | Pazar-nötr listeleme tipi core'a girmeden önce temizlenmeli |

Tarih hatasının iki katmanı var. Boşluk turundaki ham chart fixture'larında (G8) `timestamp[]` seans açılış anı: THYAO.IS 06:30 UTC, 7203.T 00:00 UTC, 2330.TW 01:00 UTC, BHP.AX önceki gün 23:00 UTC. Ham zaman damgasının UTC tarihi yalnız yerel açılış saati UTC farkından küçükse yanlış (bu örneklerde yalnız ASX). Crocodile ise ham yanıtı değil yfinance çıktısını kullanıyor; yfinance tarihi yerel gece yarısına çektiği için, Crocodile'ın UTC'ye çevirip tarihi UTC'den alması UTC'nin doğusundaki her dilimde (yaz saatinde Londra dahil) bir gün erken tarih üretiyor. Kod ve kütüphane okundu, çalıştırılmadı; G8 fixture'larıyla bir test bunu sabitlemeli.

Crocodile standardı gereği her yeni kaynak için: önce başarısız test; offline fixture ve `network_forbidden`; `provenance_fields()` ve kayıtlı bir temel; ölçülmüş canlı erişim ve oran limiti ya da açıkça "ölçülmedi"; yeni kapılar. Eklenmesi gereken kapılar: `(mic, local_symbol)` tekilliği, kaynak başına `SymbolGrammar` gidiş-dönüşü, kayıt para biriminin listeleme para birimine eşitliği, her uygulamanın `mic`/`country` süzmesi veya reddetmesi, takvim çapraz testi. Windows'ta yaklaşık 42 modül toplanamıyor ve lint kapısı `.venv/bin/python` arıyor; bu yerel kısıt geçmiş bir boşluk olarak kaydedilmeli, başarı olarak değil.

## 9. Hukuki, ToS ve yaptırım riskleri

| Risk | Kanıt | Önerilen kural |
|---|---|---|
| Yahoo otomatik erişim yasağı | legal.yahoo.com ToS, robot, spider ve scraper ile erişimi yasaklıyor (okundu); yeniden dağıtım maddesi çıkarılmadı | Yalnız araştırma modu; kayıtlar etiketli; dağıtılan ürüne girmez; paylaşılan küçük oran bütçesi |
| Bot korumalı kaynaklar | Stooq proof-of-work ve sonra bağlantı zaman aşımı; TPEx, NSE, BSE, JSE, Saudi ve PNGX 403; CVM Cloudflare; NEEQ JavaScript sınaması; KAP robots.txt 666 | Aşılmaz. Bağdaştırıcı açık "erişilemez" nedeniyle reddeder (`unservable_channels`) |
| Borsa lisansları | BIST: gecikmeli/EOD veriyle endeks hesaplamak 2014'ten beri lisanslı; dağıtım yetkili dağıtıcıyla; 2021 tarifesinde gecikmeli ve EOD dağıtım ücretli. Deutsche Börse MDS: ileri dağıtım ön onaya tabi. Euronext: dağıtım lisanslı. SSE: yeniden dağıtım nitelikli satıcılara. HKEX son kullanıcı sözleşmesi. KRX Open API: ticari olmayan kullanım, üçüncü kişiye sağlama yok. data.go.kr KRX fiyatları: KOGL 4. tip, yeniden dağıtım kesinlikle yasak. TWSE: OpenAPI OGDL v1 (atıfla ticari serbest), www uçları site şartlarına tabi. BYMA: gecikmeli web verisi yeniden dağıtılamaz. LSEG: ücretsiz gecikmeli veri yalnız Workspace'te. MOEX: sözleşme okunamadı | Her kaynak bağdaştırıcısı kullanım sınıfını (`internal`, `display`, `redistribution`) beyan eder; Crocodile dışa veri sunduğunda bu sınıf denetlenir |
| Ücretsiz katman şartları | Twelve Data Basic: yalnız ABD, dahili non-display, ticari kullanım yasak. EODHD ücretsiz: yalnız kişisel kullanım. J-Quants, EDINET, OpenDART: ticari kullanım maddesi bulunamadı | Bu kaynaklar varsayılan olarak `internal` sınıfında; ticari kullanım için ücretli plan ya da yazılı izin |
| Serbest referans verisi | GLEIF CC0; OpenFIGI tanımlayıcıları kamu malı | `redistribution` sınıfına girebilen tek kaynaklar |
| Belgesiz resmî uçlar | ASX markitdigital, CNINFO JSON, NSE iç uçlar | Resmî kaynak sayılmaz; değişebilir; lisans yok |
| Topluluk kazıyıcıları | AKShare, Tushare, Baostock, vnstock, kwayisi, casased/BVCscrap, belex, opendatauzb, tasepy, KAP sarmalayıcıları | Varsayılan kurulumun dışında, `evasion` eki gibi karantinada; `test_evasion_quarantine.py` sınırı korunur |
| Yaptırımlar | Rusya (MOEX, SPB), Belarus, İran (IFBX dahil), Suriye, Venezuela, Sudan, Libya, Myanmar; Ukrayna'da savaş koşulları. OFAC SDN listesiyle karşılaştırma yapılmadı; boşluk turunda da araştırılmadı | Bu MIC'ler için varsayılan reddetme; hukuk onayı olmadan açılmaz. MOEX ISS'in OHLCV döndürmesi bu kararı değiştirmez |
| Kimlik bilgileri | J-Quants, KRX, EDINET, OpenDART, Companies House anahtarları | `Settings.from_env` üzerinden; günlük, manifest ve journal'a asla yazılmaz (README kuralı) |

## 10. Mimari yaklaşımlar

| Yaklaşım | Kazanım | Bedel | Uygunluk |
|---|---|---|---|
| **A. Toplayıcı öncelikli.** Yahoo'yu küresel omurga yapmak; mevcut bağlayıcılara sonek tabloları eklemek | En hızlı kapsam: yaklaşık 50 borsa, az kod | ToS ihlali; tek arıza noktası; belgesiz kota; alt birim ve saat dilimi tuzakları; yeniden dağıtılamaz. Crocodile'ın "makul ama yanlış cevap verme" ilkesiyle çelişir | Yalnız araştırma ve QA katmanı olarak |
| **B. Katmanlı, resmî kaynak öncelikli federasyon.** ISO 10383 listeleme ana kaydı; MIC başına beyan edilmiş kaynak sırası (resmî A → resmî anahtarlı C → lisanslı → araştırma modu B); kaynak başına `SymbolGrammar`; provenance ile çapraz doğrulama | Hukuken temiz büyüme; her borsanın erişim sınıfı dürüstçe görünür; lisans alındığında yalnız bağdaştırıcı eklenir; Crocodile'ın tek-kayıt ve dürüstlük kurallarına uyar | Başlangıçta az sayıda borsada gerçek veri (4 A ve 2 resmî C); temel iş (kimlik, para birimi, takvim) uzun sürer | **Önerilen** |
| **C. Tek ticari toplayıcı.** Twelve Data, EODHD veya benzeri tek ücretli satıcı | Tek şema, hızlı geniş kapsam, ödeme yapılırsa sade bakım | Maliyet ve satıcıya bağımlılık; ücretsiz planlar ABD dışı için işe yaramıyor (Twelve Data Basic yalnız ABD, EODHD ücretsiz yalnız kişisel); Twelve Data'da ticari kullanım Venture 149 USD/ay'dan başlıyor, yeniden dağıtım ek lisansla; satıcı saat dilimi hataları | B'nin içinde ileride takılabilecek bir lisanslı kaynak olarak |

Önerilen veri hattı: kaynak bağdaştırıcısı → ham yanıt kaydı (kaynak, MIC, istek zamanı, HTTP durumu) → `SymbolGrammar` ile listeleme çözümü → listeleme ana kaydı (para birimi, ölçek, saat dilimi, takvim) → kanonik kayıt ve provenance → parquet. Bir listeleme için birden çok kaynak varsa beyan edilmiş sıra uygulanır. Araştırma modundaki Yahoo kaydı resmî kaydı ezmez, yalnız karşılaştırma için ayrı tutulur.

Önerilen kararlar (Ruling biçiminde):

- **Karar:** listeleme taneciği operating MIC'tir, segment MIC bir özniteliktir. **Yanılırsak bedeli:** segment düzeyinde ayrışan kaynaklar (XTKS kullanan takvim kütüphanesi gibi) eşleme tablosuna ihtiyaç duyar. Bu bedel bir tabloyla sınırlı kalır.
- **Karar:** ABD bare ticker'ları dondurulur, ABD dışı kanonik sembol `{yerel}@{MIC}` olur. **Yanılırsak bedeli:** sonradan değişen format `mmh3` bucket'larını taşır ve göç gerektirir.
- **Karar:** Yahoo yalnız `research` kullanım sınıfında çalışır. **Yanılırsak bedeli:** ToS ihlali ve dağıtılan veride lisans riski.
- **Karar:** günlük barın `source_ts` değeri borsa-yerel seans tarihinin UTC gece yarısıdır. **Yanılırsak bedeli:** ABD dışı her çapraz kaynak karşılaştırması bir gün kayar.
- **Karar:** yaptırım listesindeki MIC'ler varsayılan olarak reddedilir. **Yanılırsak bedeli:** hukuki maruziyet.

Uygulama sırası ve kabul ölçütleri:

1. **Temel (yeni veri yok).** `core/markets` paketi: ISO 10383 kaydı yükleyici (ACTIVE/EXPIRED ve operating/segment), ISO 4217 ve fiyat ölçeği, IANA saat dilimi, `ExchangeCalendar` ve `exchange_calendars` bağdaştırıcısı, listeleme ana kaydı tabloları, `SymbolGrammar` protokolü, Yahoo tarih düzeltmesi. *Geçiş ölçütü:* aynı ticker'lı iki listeleme ayrı tutulur; XTKS→XJPX ve XMOS→MISX eşlemeleri test edilir; GBX/ZAc/ILA ölçeği uygulanır; `fixtures/g8/` altındaki THYAO.IS, 7203.T, 2330.TW ve BHP.AX barları doğru yerel tarihe düşer; BIST 2026 resmî tatil listesi takvim bağdaştırıcısıyla çapraz test edilir.
2. **Resmî ücretsiz kaynaklar.** TWSE (XTAI EOD: OpenAPI günlük anlık görüntü; 2010'dan geriye doldurma için www uçları, şart kapsamı teyit edilene kadar yalnız `internal`), Deutsche Börse MDS (XETR/XFRA: günlük post-trade dosyası her iş günü indirilip saklanır, OHLCV işlemlerden türetilir; 1 günlük saklama yüzünden bu iş ertelenirse geçmiş kaybolur), filings.xbrl.org (ESEF), GLEIF (CC0), MIC kapsamlı OpenFIGI, ECB kurları, evren dizinleri (NSE EQUITY_L, ASX, nasdaqtrader, TWSE t187ap03_L; günlük anlık görüntü saklanır). *Geçiş ölçütü:* her kaynak için ölçülmüş erişim ve oran limiti kaydedilir; şart metinleri okunup kullanım sınıfı beyan edilir.
3. **Ücretsiz anahtarlı resmî API'ler.** J-Quants (XJPX, 12 hafta gecikmeli EOD ve listeleme ana verisi), KRX Open API ve data.go.kr (XKRX, ticari olmayan). *Geçiş ölçütü:* kimlik bilgileri günlüklere sızmaz; ticari olmayan şartlar kullanım sınıfında zorlanır.
4. **Türkiye kararı.** BIST yetkili dağıtıcı teklifi, güncel ücret tarifesi (eldeki 2021 tarihli) ve Tarihsel Veri Satışı fiyatı kullanıcıya sunulur. Datastore ürünlerinin ücretsiz olup olmadığı bir tarayıcıyla kontrol edilir. KAP robots.txt ve şartları okunur. Foreks/ForInvest iddiası BIST'ten teyit edilir. O zamana kadar `.IS` yalnız araştırma modunda.
5. **A adaylarının doğrulanması.** PSE FTP içeriği, KASE günlük dosyaları, Zagreb ve Ljubljana EOD, HKEX listesi (referans), Euronext dosyaları. MOEX için sözleşme metni ve hukuk görüşü. TWSE www uçlarının OGDL kapsamına girip girmediği TWSE'den teyit edilir.
6. **Genişlik.** Lisans kararlarına göre ticari bağdaştırıcılar (C yaklaşımı, B'nin içinde).

## 11. Açık sorular

1. Hangi pazarlar öncelikli? Türkiye dışında ilk dalga (ör. AB, Japonya, Kore, Hindistan) kullanıcı kararıdır.
2. BIST için lisans bütçesi var mı: gerçek zamanlı/gecikmeli dağıtıcı mı, yalnız tarihsel veri mi? Crocodile verisi dışarı dağıtılacak mı, endeks veya gösterge hesaplanacak mı?
3. Yahoo'nun araştırma modunda tutulması kabul mü, yoksa hiç kullanılmasın mı?
4. Kanonik sembol formatı (`{yerel}@{MIC}` mi, opak listing id mi) ve ABD bare ticker'larının dondurulması.
5. Para birimi yalnız listelemede mi kalacak (Stage A), yoksa kayıtlara da girecek mi (Stage B)? Alt birim `price_scale` ile mi, GBX gibi kodlarla mı?
6. Tatil verisinin sahibi kim: kütüphane mi, borsa duyuruları mı?
7. Yaptırım politikası: MOEX, SPB ve diğerleri için varsayılan reddetme yeterli mi, yoksa hiç kod yazılmasın mı?
8. Doğrulanmamış kalan kritik noktalar: WFE üye listesi ve şirket sayıları (portallar giriş istiyor); NSPD kategorisinin anlamı; J-Quants kullanım şartları; TWSE www uçlarının OGDL kapsamı; Deutsche Börse MDS gecikmeli dosyalarının depolama, ticari kullanım ve dağıtım şartları; MOEX sözleşmesi ve gecikme düzeyi, ISS'te en erken tarih; BIST güncel ücret tarifesi ve datastore ürünlerinin ücretsizliği; BIST seans ve müzayede saatleri; KAP robots.txt ve kullanım şartları; Yahoo bölünme olayları; endeks bileşen kaynakları ve lisansları; filings.xbrl.org, EDINET ve OpenDART'ın ticari kullanım şartları. Boşluk turunda çözülenler: Twelve Data Basic kapsamı (yalnız ABD), MDS dosya biçimi, TWSE OpenAPI lisansı (OGDL v1), GLEIF lisansı (CC0), OpenFIGI oran limitleri, BIST 2021 ücret tarifesi içeriği ve 2026 tatilleri.
9. Kapsam açıkları: Slovakya (XBRA), Arnavutluk (XALS), Kırgızistan, Dominik Cumhuriyeti, Orta Amerika (Honduras XBCV, Guatemala, El Salvador, Nikaragua), Guernsey (TISE), Saint Vincent (SVXI), Faroe, Curaçao, Guyana, Vanuatu, Japonya'daki ATS'ler (XJAX, SBIJ); kapsanan ülkelerde NEEQ, TXSE, Nxchange, RM-System. ISO'da hiç satırı olmayan SL, LS, SR, TJ, TM, SO. Bu yerler için Yahoo probu ve şirket sayısı yok (`meta.coverage_candidates`).
11. Deutsche Börse MDS dosyaları için günlük toplama işi hemen başlatılmalı mı? Saklama 1 iş günü olduğu için her geciken gün kalıcı olarak kaybolur; ancak dağıtım şartları netleşmeden veri yalnız `internal` sınıfında tutulabilir.
10. Sağlayıcıların `supported_mics` beyan etmesi zorunlu mu olacak? Zamanlayıcı bir operatör komutu mu olacak, iki yarılı bir capability mi?

## 12. Kaynakça

Satır düzeyindeki tüm atıflar `market-matrix.json` içindeki `citations` alanlarındadır. Burada yalnız belirleyici birincil kaynaklar listelenir.

**Kayıtlar ve standartlar**
- [ISO 20022 MIC sayfası](https://www.iso20022.org/market-identifier-codes), yerel kopya `ref-mic/ISO10383_MIC.csv` (2.889 satır)
- [SIX ISO 4217 list-one](https://www.six-group.com/dam/download/financial-information/data-center/iso-currrency/lists/list-one.xml) (yayın 2026-09-17)
- [IANA tz zone.tab](https://raw.githubusercontent.com/eggert/tz/main/zone.tab)
- [OpenFIGI API belgesi](https://www.openfigi.com/api/documentation), [GLEIF API](https://api.gleif.org/api/v1/lei-records)
- [exchange_calendars lisansı](https://raw.githubusercontent.com/gerrymanoim/exchange_calendars/master/LICENSE), [pandas_market_calendars lisansı](https://raw.githubusercontent.com/rsheftel/pandas_market_calendars/master/LICENSE), [exchange-calendars-extensions](https://pypi.org/project/exchange-calendars-extensions/)
- [ECB SDMX EXR](https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A?lastNObservations=1&format=csvdata), [Frankfurter v2](https://api.frankfurter.dev/v2/rates?base=EUR&quotes=USD), [FRED grafik CSV](https://fred.stlouisfed.org/graph/fredgraph.csv?id=DEXUSEU)
- [WFE istatistik portalı](https://focus.world-exchanges.org/statistics/monthly-insights/8) (içerik ayrıştırılmadı), [WFE exchange factsheets](https://www.world-exchanges.org/our-work/statistics/exchange-factsheets) (54 borsa adı; şirket sayısı yok)
- [GLEIF LEI veri kullanım şartları (CC0)](https://www.gleif.org/en/meta/lei-data-terms-of-use), [OpenFIGI hizmet şartları](https://www.openfigi.com/docs/terms-of-service), [ECB yasal uyarı](https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html)

**Türkiye**
- [BIST Data Dissemination PDF](https://borsaistanbul.com/files/Data_Dissemination.pdf), [BIST veri ücret tarifesi](https://www.borsaistanbul.com/files/borsa-istanbul-data-fee-schedule.pdf), [BIST duyuru 13712](https://borsaistanbul.com/en/announcement/13712/statement-about-index-calculation-borsa-istanbul-delayed-and-end-day-data), [BIST Datastore](https://datastore.borsaistanbul.com), [BIST piyasa veri ürünleri](https://borsaistanbul.com/en/data/data-dissemination/market-data-products)
- [BIST pay piyasası veri dosyaları](https://www.borsaistanbul.com/en/data/equity-market-data), [BIST tarihsel veri satışı](https://www.borsaistanbul.com/en/data/historical-data-sales), [BIST 2026 pay piyasası tatil çizelgesi](https://www.borsaistanbul.com/files/equity-market-2026-holiday-schedule.pdf), [BIST işlem saatleri](https://www.borsaistanbul.com/en/markets/equity-market/trading-hours), [BIST Datastore kategori API'si](https://datastore.borsaistanbul.com/api/category)
- [KAP](https://www.kap.org.tr), [KAP bildirim sorgu](https://www.kap.org.tr/en/bildirim-sorgu), [SPK 2026 bültenleri](https://spk.gov.tr/spk-bultenleri/2026-yili-spk-bultenleri), [İşbank BIST veri lisans paketleri](https://www.isbank.com.tr/borsa-istanbul-veri-lisansi-paketleri), [dxFeed Türkiye](https://dxfeed.com/coverage/turkey/), [ICE katalog: Borsa İstanbul](https://developer.ice.com/fixed-income-data-services/catalog/borsa-istanbul)

**Resmî veri kaynakları**
- [Deutsche Börse MDS gecikmeli veri](https://mds.deutsche-boerse.com/mds-en/real-time-data/Delayed-data), [MDS dosya listesi (DFRA-posttrade)](https://mfs.deutsche-boerse.com/api/DFRA-posttrade), [MDS veri kullanım beyanı](https://mds.deutsche-boerse.com/mds-en/real-time-data/data-usage-declaration), [Euronext gecikmeli veri şartları](https://www.euronext.com/sites/default/files/stld/terms_and_conditions_for_delayed_data.pdf), [LSEG Data Guide bölüm 10](https://developers.lseg.com/)
- [TWSE OpenAPI STOCK_DAY_ALL](https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL), [TWSE MI_INDEX](https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date=20261106&type=ALLBUT0999), [TWSE site şartları](https://www.twse.com.tw/zh/page/terms/use.html), [Government Open Data License v1](https://data.gov.tw/license), [TPEx OpenAPI](https://www.tpex.org.tw/openapi/), [HKEX ListOfSecurities.xlsx](https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx), [SSE veri hizmetleri](https://english.sse.com.cn/markets/dataservice/products/), [SZSE işlem özeti](https://www.szse.cn/English/services/trading/tradOverview/)
- [J-Quants veri spesifikasyonu](https://jpx-jquants.com/en/spec/data-spec), [J-Quants oran limitleri](https://jpx-jquants.com/en/spec/rate-limits), [J-Quants fiyat planları](https://jpx-jquants.com/ja), [KRX Open API şartları](https://openapi.krx.co.kr/contents/OPP/INFO/OPPINFO005.jsp), [KRX Open API kullanım sözleşmesi](https://openapi.krx.co.kr/contents/OPP/INFO/OPPINFO002.jsp), [data.go.kr 주식시세정보](https://www.data.go.kr/data/15094808/openapi.do)
- [MOEX ISS](https://iss.moex.com/iss/reference/), [MOEX ISS history (SBER)](https://iss.moex.com/iss/history/engines/stock/markets/shares/boards/TQBR/securities/SBER.json), [PSE FTP readme](https://ftp.pse.cz/Readmeen.txt), [ZSE son kullanıcı sayfası](https://zse.hr/en/end-users/32), [LJSE piyasa verisi](https://ljse.si/en/market-data-33/32), [Euronext Athens fiyat listesi](https://athens.euronext.com/en/market-data/data-services/real-time-feed/data-vendors/price), [KASE günlük dosyalar duyurusu](https://kase.kz/en/news/show/1526341)
- [BYMA llms.txt](https://byma.com.ar/llms.txt), [NSE All Reports](https://www.nseindia.com/all-reports), [PSX Data Portal](https://dps.psx.com.pk/), [IDX veri hizmetleri](https://www.idx.id/en/products/idx-data-services), [SET SMART Marketplace](https://www.set.or.th/en/services/connectivity-and-data/data/smart-marketplace), [NZX piyasa verisi](https://www.nzx.com/services/products-tools/data-connectivity/nzx-market-data), [ASX gecikmeli fiyat verisi](https://www.asx.com.au/content/asx/home/connectivity-and-data/information-services/price-data/delayed-price-data.html), [NGX API portalı](https://marketdataapiv3.ngxgroup.com/portal/getstarted/index), [NSE Kenya gecikmeli veri](https://www.nse.co.ke/dataservices/delayed-data/)

**Dosyalama**
- [filings.xbrl.org API](https://filings.xbrl.org/api/filings), [EDINET API v2](https://api.edinet-fsa.go.jp/api/v2/documents.json), [OpenDART](https://opendart.fss.or.kr/), [TDnet (JPX)](https://www.jpx.co.jp/english/markets/paid-info-listing/tdnet/index.html), [Companies House](https://developer.company-information.service.gov.uk/developer-guidelines), [FCA NSM](https://www.fca.org.uk/markets/primary-markets/regulatory-disclosures/national-storage-mechanism), [SEDAR+](https://www.sedarplus.ca), [CVM Dados Abertos](https://dados.cvm.gov.br/), [CNINFO](https://www.cninfo.com.cn/), [HKEXnews](https://www1.hkexnews.hk/), [MOPS](https://mops.twse.com.tw/mops/web/index)

**Toplayıcılar**
- [Yahoo kullanım şartları](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html), [Yahoo borsa ve sonek yardım sayfası](https://help.yahoo.com/kb/SLN2310.html), [Twelve Data /exchanges](https://api.twelvedata.com/exchanges), [Twelve Data fiyatlar](https://twelvedata.com/pricing), [Twelve Data şartlar](https://twelvedata.com/terms), [EODHD fiyatlar](https://eodhd.com/pricing), [EODHD ticari ve kişisel lisans](https://eodhd.com/financial-apis/commercial-vs-personal-license-use), [Massive uluslararası veri KB](https://massive.com/knowledge-base/article/does-massive-offer-international-data), [Stooq CSV ucu](https://stooq.com/q/d/l/?s=aapl.us&i=d)

**Kurumsal işlemler ve dizinler**
- [NSE kurumsal işlemler JSON](https://www.nseindia.com/api/corporates-corporateActions?index=equities), [NSE EQUITY_L.csv](https://archives.nseindia.com/content/equities/EQUITY_L.csv), [ASX listelenmiş şirketler](https://www.asx.com.au/asx/research/ASXListedCompanies.csv), [nasdaqtrader otherlisted.txt](https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt), [TWSE t187ap03_L.csv](https://mopsfin.twse.com.tw/opendata/t187ap03_L.csv), [KIND şirket listesi](https://kind.krx.co.kr/corpgeneral/corpList.do?method=download), [TWSE TWT49U](https://www.twse.com.tw/exchangeReport/TWT49U?response=json&date=20261106)

**İç girdiler**
- Bölge dosyaları: `scratchpad/global-rd/regions/*.verified.json` (15 dosya)
- Boşluk dosyaları: `scratchpad/global-rd/gaps/1-G1` … `8-G8` (.md) ve ham problar (`gaps/mds`, `gaps/twse`, `gaps/moex`, `gaps/raw`, `gaps/raw5`, `gaps/g3-raw`); birleştirme yeniden kontrolleri `gaps/recheck/`
- Yahoo fixture'ları: `scratchpad/global-rd/fixtures/g8/` (THYAO.IS, 7203.T, 2330.TW, BHP.AX ve dört artık MIC)
- Kod haritası: `scratchpad/global-rd/codemap.md` (Crocodile HEAD `f8f2800`)
- Birleştirme betikleri: `scratchpad/global-rd/scripts/build_market_matrix.py`, `build_market_matrix_stage2.py` ve boşluk turu için `apply_gap_fill.py` (ilk sürümün yedeği `market-matrix.pre-gapfill.json`)
- Birleştirilmiş matris: `scratchpad/global-rd/market-matrix.json`

## Ek: TradingView scanner ölçümü (8 Kasım 2026, birleştirmeden sonra)

Kullanıcı kullanım şartları belirsiz kaynakları varsayılan olarak açık tutmayı seçti. Bunun üzerine Ar-Ge turlarında problanmamış olan TradingView scanner ölçüldü. Her pazar adına bir istek atıldı: `POST https://scanner.tradingview.com/{pazar}/scan`, anahtarsız, `range [0,2]`, istekler arasında 1 sn.

| Pazar adı | HTTP | Satır (totalCount) | Görülen borsa kodu ve para birimi |
|---|---|---|---|
| turkey | 200 | 650 | BIST, TRY |
| nigeria | 200 | 139 | NSENG, NGN |
| kenya | 200 | 60 | NSEKE, KES |
| morocco | 200 | 76 | CSEMA, MAD |
| egypt | 200 | 256 | EGX, EGP |
| bangladesh | 200 | 382 | DSEBD, BDT |
| srilanka | 200 | 295 | CSELK, LKR |
| vietnam | 200 | 1.265 | HOSE, UPCOM, VND |
| pakistan | 200 | 485 | PSX, PKR |
| peru | 200 | 310 | BVL, PEN ve USD |
| colombia | 200 | 222 | BVC, COP |
| serbia | 200 | 17 | BELEX, RSD |
| ghana, kazakhstan, mongolia | 404 | — | Pazar adı farklı olabilir. Doğrulanmadı |

Scanner yalnız güncel anlık görüntü döndürüyor, geçmiş bar vermiyor. Ölçüm bir şeyi gösteriyor: D sınıfındaki birçok borsa için anahtarsız bir evren listesi ve günlük veri rotası var. Tier dağılımı bu belgede yeniden hesaplanmadı. TradingView'ın kullanım şartları okunmadı. Kaynak, kullanıcı kararıyla varsayılan olarak açık bir gri kaynak olarak kullanılacak. Tasarım: `docs/superpowers/specs/2026-11-08-global-equity-design.md`.
