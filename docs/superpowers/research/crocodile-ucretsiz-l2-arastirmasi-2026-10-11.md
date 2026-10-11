# Crocodile için ücretsiz L2/L3 kaynak araştırması ve FlowMap tasarım önerisi

11 Ekim 2026. Bu belge kaynak araştırmasının sonucunu ve değerlendirmeye hazır bir mimari öneriyi içerir. Kod değişikliği yapılmadı. Kanal özellikleri birincil belgelerden kontrol edildi; kullanıcı hesabıyla canlı bağlantı, gerçek olay hızı ve yeni native performans ölçümü yapılmadı. 9 Ekim performans sayıları verilen raporun başlangıç ölçümleridir.

Önerim: Crocodile'ın yerel borsa bağlantılarını daha ayrıntılı kanallara genişletmek, gelen özgün mesajları gruplama öncesinde kaydetmek ve FlowMap'i bu kayıttan üretilen, kalıcı çok çözünürlüklü görünümlerle beslemek. Eksik geçmiş için matematiksel yeniden kurma ayrıca kullanılabilir; bunun kesin ve tahmini sonuçları açıkça ayrılmalıdır.

Araştırma genelden özele yürütüldü: önce L1/L2/L3 ve snapshot/delta ayrımı; ardından standart kanalların binary, raw, unbatched ve emir kimlikli seçenekleri; sonra erişim şartları, ücretsiz arşivler ve mevcut Crocodile bağlantıları. Az kullanılan bir kaynağı önceliklendirmek için daha hızlı yayın, daha ayrıntılı veri, doğrulanabilir süreklilik ve erişilebilirlik birlikte arandı. Kaynağın az tanınmış olması tek başına kalite kanıtı sayılmadı.

Ücretsiz burada veri aboneliği satın almamak anlamına gelir. Bazı kanallar ücretsiz hesap ve API anahtarı gerektirir; yerel disk, ağ ve işlem maliyeti devam eder. Bölgesel/hesap uygunluğu ve uygulamadaki gerçek erişim daha sonra ölçülmelidir.

| Kaynak | Doğrulanmış kazanım | Erişim ve önemli kapsam | Tasarımdaki rol |
|---|---|---|---|
| Binance Spot SBE `<symbol>@depth` | 20 ms yayın aralığı; nominal 50 yayın aralığı/s | Ed25519 API anahtarı; ek market-data izni/VIP şartı belgede belirtilmiyor. Spot içindir. Başlangıç snapshot sınırı ve değişmeyen uzak seviyeler korunur. | Binance Spot için ilk hızlı kaynak |
| Deribit `book.{instrument}.raw` | 1 ms toplulaştırma; tam fiyat seviyesi L2 snapshot ve değişimleri, derinlik sınırı yok | Yetkilendirilmiş API oturumu; normal kanal belgelerinde ücret/hacim/VIP eşiği belirtilmiyor. L3 değildir. | Mevcut Deribit bağlantısının yüksek ayrıntı seçeneği |
| Bitfinex `book/R0/F0/250` | Gerçek zamanlı emir kimlikli raw defter | Anahtarsız public WS. 250 emirlik kapsam, 250 fiyat seviyesi değildir. | İlk anahtarsız L3 türü bağlantı |
| Kraken Spot `level3` | Throttle uygulanmayan emir olayları; 10/100/1000 fiyat seviyesi | API token; standard tier desteklenir. Görünür resting emirler; iceberg'in gizli miktarı kapsam dışı. | Daha geniş emir düzeyi görünürlüğü |
| Bybit `orderbook.50` | 50 seviye, 20 ms | Normal public WS anahtarsız. Ayrı `orderbook.full` 200 ms; iki hız aynı özellik gibi sunulamaz. | Hızlı yakın defter + daha geniş yavaş kapsam |
| Coinbase Advanced Trade `level2` | Public L2, tüm güncellemelerin teslimi | JWT gerekmez; sabit ms cadence belgelenmiyor. Exchange Full/L3 farklı, auth gerektiren kanallardır. | Gerçek olay hızını ölçmeye değer aday |
| dYdX v4 orderbook, `batched:false` | Indexer'in birleştirilmemiş mesaj seçeneği | Public kanal; sabit yayın aralığı belgelenmiyor. Subscription mesaj kimliği matching-engine olay sayacı olarak yorumlanmamalı. | İkinci aşama ölçüm adayı |

Binance SBE için anahtar `X-MBX-APIKEY` bağlantı başlığında taşınır; imza gerekmeyen public market-data erişimidir. SBE binary decoder, schema sürümü ve kontrol mesajlarının JSON olarak ayrılması gerekir. Best-bid/ask kanalı aşırı yükte eski mesajları eleyebilir; kusursuz olay günlüğü için bu kanalın her değişimi koruduğu varsayılmamalıdır. [Binance SBE kanal ve erişim belgesi](https://developers.binance.com/docs/binance-spot-api-docs/sbe-market-data-streams). Başlangıç Spot defteri en fazla 5.000 seviye/yan içerir; daha uzakta değişmeyen seviyeler bilinmez. [Yerel defter senkronizasyonu](https://developers.binance.com/en/docs/products/spot/web-socket-streams#how-to-manage-a-local-order-book-correctly).

Deribit'in güncel kanal referansı `raw` için 1 ms toplulaştırma belirtir; ismi her matching-engine olayının ayrı mesaj olarak geldiği anlamına alınmamalıdır. `change_id/prev_change_id` zinciri izlenir; perp/futures miktar birimleri ile options birimleri farklıdır. İncelenen normal internet akışı ile seçilmiş müşterilere yönelik Starbase bağlantıları ayrı tutulmalıdır. [Deribit tam L2 referansı](https://docs.deribit.com/subscriptions/orderbook/bookinstrument_nameinterval), [veri toplama koşulları](https://docs.deribit.com/articles/market-data-collection-best-practices).

Bitfinex R0 defteri fiyat yerine order ID ile tutulur; L2 görünümü aynı fiyattaki görünür emirler toplanarak üretilir. Snapshot kapsamının dışında kalan emirler için miktar tamlığı iddia edilmez. [Raw Books](https://docs.bitfinex.com/reference/ws-public-raw-books). F0 gerçek zamanlı, F1 iki saniyelik yayındır. [Yayın biçimleri](https://docs.bitfinex.com/reference/ws-public-books). Sequence/timestamp bayrakları ve sınırlı checksum kapsamı ayrıca korunur. [Genel WS kuralları](https://docs.bitfinex.com/docs/ws-general), [checksum](https://docs.bitfinex.com/docs/ws-websocket-checksum).

Kraken L3 başlangıç snapshot'ından sonra otomatik akar. Ayrıntılı v2 şeması sıra alanı istemez; olmayan bir sequence alanı icat edilmemelidir. CRC32 yalnız en iyi 10 fiyat seviyesi/yanı doğrular. Abone olunan derinliğin dışına çıkan seviyeler istemcide çıkarılır. [Kraken L3](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/level3). Token için belgelenen izin WebSocket interface erişimidir. [Token koşulları](https://docs.kraken.com/api-reference/trading/get-websockets-token).

Bybit hızlı yakın alan ve geniş kapsamı ayrı tutmaya elverişlidir. Full kanalın başlangıcı REST snapshot + buffered delta eşleştirmesi gerektirir; en fazla 10.000 seviye/yan snapshot alınır. Kaynakların ayrı güncelleme kimlikleri ve alan yaşları korunmadan hızlı 50 seviyelik mesajları tüm deftere uygulamak yanlıştır. [Normal derinlik kanalları](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook), [Full Orderbook](https://bybit-exchange.github.io/docs/v5/websocket/public/full-ob).

Coinbase Advanced public L2'nin JWT istememesi ile Exchange Full/Level2/Level3'ün auth gerektirmesi farklı erişim sözleşmeleridir. Exchange anahtarsız `level2_batch` 50 ms, yani nominal 20 Hz'dir. Advanced trade kanalının 250 ms toplulaştırması raw tape diye sayılmamalıdır. [Advanced public erişim](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/websocket/websocket-endpoints), [L2](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/websocket/level2), [Exchange auth](https://docs.cdp.coinbase.com/exchange/websocket-feed/authentication), [Exchange kanalları](https://docs.cdp.coinbase.com/exchange/websocket-feed/channels). dYdX için `batched:false` kontrol edilebilen bir toplulaştırma tercihidir; gerçek hız yine ölçüm konusudur. [dYdX WS şeması](https://docs.dydx.xyz/indexer-client/websockets).

| Ücretsiz geçmiş kaynağı | Bulgular | Uygun kullanım |
|---|---|---|
| Bybit public orderbook CDN | Linear BTCUSDT günlük orderbook dosyaları mevcut; 10 Ekim 2026 `ob200` dosyasına HTTP HEAD 200 ile erişim doğrulandı. Dosya gövdesi indirilmedi. Eski `ob500` dosyaları da dizinde görünüyor. | Aynı Bybit ürünü için tarih/snapshot/sıra şeması doğrulandıktan sonra geri doldurma |
| Zenodo: An Open Book | 25 Mart 2026 yayın; açık erişim ve CC BY 4.0 lisansı metadata API'den doğrulandı. Aralık 2025 BTC/ETH/SOL emir durumu ve defter değişimleri; ayrıca daha geniş trade dönemi. | L3 dönüştürücü, model ve kayıp senaryosu için güçlü araştırma verisi |
| Tardis ücretsiz ayın ilk günü örnekleri | Belirli günlerde Binance L2 örnekleri ücretsiz erişilebilir. Kesintisiz her gün arşivi değildir. Güncel şartlar ürün içinde kullanımı sınırlıyor. | İç araştırma/QA; dağıtılan Crocodile arşiv çözümü olarak seçilmedi |
| Binance public-data `bookDepth` | Futures örnek dosyasında `timestamp,percentage,depth,notional`; tek tek fiyat/miktar ve sequence yok. Spot public-data çoğunlukla trade/aggTrade/klines sunar. | Yüzdesel likidite bağlamı; tam L2 geri kurma kaynağı değil |

Bybit kaynağı için [resmî geçmiş portalı](https://www.bybit.com/en/derivative-activity/history-data) ve [public linear BTCUSDT orderbook dizini](https://quote-saver.bycsi.com/orderbook/linear/BTCUSDT/) birlikte kontrol edildi. Doğrudan dizin günlük dosyalara ulaşmayı kolaylaştırır. İndirilen geçmişin kayıt türü, snapshot başlangıcı, kaynak zamanları, derinliği ve boşlukları ayrıca denetlenmelidir; dosya ismindeki derinlik tek başına şema kanıtı sayılmaz.

Özellikle az kullanılan güçlü bulgu [An Open Book: Hyperliquid emir verisi](https://zenodo.org/records/18184441). Lisans [metadata API](https://zenodo.org/api/records/18184441) üzerinden kontrol edildi. README/SCHEMA incelemesinde raw book diff örneklerinin açık timestamp/sequence ve açılış snapshot'ı taşımadığı görüldü; emir durumu akışı ise zaman ve sabit nokta fiyat bilgisi içeriyor. Bu nedenle ilk hedef şema/zaman/açılış-state denetimi olmalıdır. Veri Binance tarihinin yerine geçirilemez; yayımlanan kapsam ve boşluklar araştırma fixture'ının özellikleridir. Büyük arşivin tamamını indirmek başlangıç için gerekli değildir.

[Tardis örnek erişimi](https://docs.tardis.dev/downloadable-csv-files/overview) ücretsiz örnek politikasını, [Binance veri ayrıntıları](https://docs.tardis.dev/historical-data-details/binance) 100 ms kaynak ve sınırlı başlangıç snapshot'ını, [güncel şartlar](https://docs.tardis.dev/legal/terms-of-service) ürün kullanım sınırını açıklıyor. Ücretsiz örnekler iç QA adayı olarak tutuldu. [Binance public-data yayıncısı](https://github.com/binance/binance-public-data) ile küçük bir futures `bookDepth` örneğinin şeması kontrol edildi; trade verisi bekleyen likiditeyi tek başına belirlemiyor.

Ücretsiz ana plana alınmayan seçenekler: OKX'in 10 ms tam L2 kanalları güncel belgede VIP4+ istiyor; anahtarsız 10 ms BBO yalnız L1. [OKX book kanal şartları](https://app.okx.com/docs-v5/en/#order-book-trading-market-data-ws-order-book-channel). Bybit SBE MMWS/Gateway erişimine bağlı. [Bybit SBE koşulları](https://bybit-exchange.github.io/docs/v5/sbe/sbe-basic-info). Hyperliquid'in resmî S3 arşivi aktarım ücretini talep edene yüklüyor. [Hyperliquid arşiv erişimi](https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data). Bunlar koşulsuz ücretsiz kaynak olarak sayılmadı.

Mevcut kodun işaret ettiği dar boğazlar:

- FlowMap `C:\fm\server\pyproject.toml:35` içinde Crocodile Git revizyonunu `2e22c901dff7f0b4ec98ef7e66b8a10de4464d9c` olarak pinliyor. İncelenen Crocodile kodu FlowMap ortamındaki kurulu kopyadır; ayrı Crocodile checkout'unun güncel ana dalı doğrulanmış değildir.
- Kurulu `crocodile\crypto\exchanges\deribit\connector.py:28` sabit `100ms` kullanıyor. `raw` auth yaşam döngüsüyle eklenebilir.
- Kurulu `crocodile\crypto\exchanges\ccxt_universal\connector.py:109` varsayılan `poll_interval=2.0`, `:112` `use_ws=False`, `:110` `book_depth=50`. Desteklenen kanallarda WS seçmek kapsam/hız kazanımı sağlayabilir. CCXT Pro artık CCXT'nin ücretsiz WS bölümüdür. [Güncel resmî CCXT Pro belgesi](https://docs.ccxt.com/docs/pro-manual).
- Kurulu `crocodile\core\ingest\transport.py:119` binary frame taşıyabiliyor; `:67` ve `:98` bağlantı kurucusu API header desteği taşımıyor. SBE için header ve protokol decoder/şema desteği gerekli.
- Crocodile şemasında `BookSnapshot/BookDelta` sıra alanları ve `NATIVE/DERIVED/SYNTHETIC` provenance zaten var. FlowMap `feeds/base.py:41` BookState köprüsünde bu sıra bilgisi korunmuyor.
- FlowMap `core/grid.py:91` ince çekirdeği de native ticklerin katlarıyla grupluyor; `:577` sonlandırılan yoğunluğu Float16'a dönüştürüyor. Varsayılan zaman kolonu 250 ms. Mevcut “fine” çekirdek tüm native fiyat seviyelerinin kayıpsız arşivi sayılmamalı.
- `core/record.py` columns/trades/markers/epochs kaydediyor; özgün depth snapshot/delta günlüğü yok. `feeds/base.py:83` sınırlı kuyruk eski olayları düşürebiliyor. Kayıt bu kayıptan önce alınmalı.

Bu maddelerde Crocodile yollarının tam kökü `C:\fm\server\.venv\Lib\site-packages\crocodile`; FlowMap sunucu yollarının tam kökü `C:\fm\server\src\flowmap_server`.

Üç yaklaşımı karşılaştırdım:

| Yaklaşım | Kazanım | Uygunluğu |
|---|---|---|
| Mevcut bağlantıların WS/hızlı kanal ayarlarını iyileştirmek | Düşük kapsamla daha sık canlı veri | İlk kazanım; geçmişteki kaybı ve soğuk arşiv maliyetini tek başına çözmez |
| Kaynağa özgü hızlı bağlantılar + ham günlük + kalıcı özetler | Canlı ayrıntı, gelecekte kayıpsız kaynak kaydı, replay ve hızlı geniş görünüm birlikte | Ana öneri; birkaç bağımsız iş paketine ayrılır |
| Gözlemlerden kısıtlı ters problem çözmek | Eksik kayıtta miktarı/BBO'yu koruyan tahmin ve belirsizlik sınırları | İsteğe bağlı matematiksel tamamlayıcı |

Önerilen veri hattı: borsa frame'i → özgün kayıt günlüğü → snapshot/delta senkronizasyonu veya emir-ID defteri → Crocodile native kayıtları → FlowMap canlı durum / arşiv görünüm sorgusu. Her kaynak borsa, spot/perp ürün, birim, kapsam, yayın aralığı, gözlenen yeni olay hızı, son boşluk ve zaman hassasiyeti bildirir. Başka borsanın defteri aynı Binance sembolüne görünmez şekilde karıştırılmaz. Çok borsalı bileşik görünüm ayrıca seçilebilir ve birim/fiyat dönüşümlerini açık taşır.

Ham günlük gruplama, Float16 ve UI drop kuyruklarından önce oluşur. SBE mantissa/exponent veya kaynak decimal değerleri, frame türü/şema, exchange/receive time, sequence varsa özgün sequence, connection kimliği ve checksum saklanır. Bölümlü sıkıştırma ve periyodik snapshot checkpoint kullanılır. Kayıt kapasitesi aşılırsa kayıp açık gap olarak yazılır; defter yeniden eşitlenene kadar kesin kabul edilmez. UI eski görsel durumları birleştirebilir; bu işlem kayıt günlüğüne uygulanmaz.

L3 kaynaklarından L2 kendi kendine üretilebilir: her order ID'nin kalan miktarı tutulur, aynı fiyat ve yöndeki emirler toplanır. Ekleme, değişim ve silme kaynak kurallarına göre uygulanır. Bu, veri kapsamı içinde gerçek gözlemlerden türetilmiş L2'dir; görünmeyen/gizli emirleri kapsamaz.

Veri yokken önerdiğim matematiksel yol:

Her fiyat seviyesinin miktarı \(x_{i,t}\ge0\) olsun. Eksiksiz snapshot + sıralı güncellemede, kaynak yeni mutlak miktar veriyorsa ilgili seviye o miktara atanır; gerçekten miktar farkı veriyorsa fark eklenir. “Delta” adlı mesajın değerini otomatik olarak toplamak yanlıştır. Kayıt boşluğu varsa yeniden snapshot gerekir.

Coarse gözlemin doğru modeli:

\[
y_k=\sum_{t\in W_k}\omega_{k,t}A_{k,t}x_t+\varepsilon_k,\qquad \sum_t\omega_{k,t}=1.
\]

\(A\) fiziksel fiyat seviyelerini kayıt hücrelerine toplar; \(\omega\) gerçek süre ağırlığıdır. FlowMap'in 250 ms zaman ortalaması tek bir anın toplamı gibi kullanılamaz. Aynı anın güvenilir BBO'su en iyi bid üstünde bid, en iyi ask altında ask olmaması koşulunu getirir; bilinen best miktarları sabitlenir.

Dinamik korunma:

\[
x_t=x_{t-1}+a_t-c_t-e_t^{vis},\qquad a_t,c_t,e_t^{vis}\ge0.
\]

\(a\) ekleme, \(c\) iptal, \(e^{vis}\) görünen defterden gerçekleşmedir. İşlem hacmi otomatik olarak \(e^{vis}\) sayılamaz; gizli likidite, yenilenme ve zaman/yön eşlemesi dikkate alınır. Gerektiğinde \(0\le e^{vis}\le\tau\) koşulu kullanılır.

Bir hücrenin **anlık** toplamı \(q\) biliniyorsa, önceki model tahmini \(v\) için hızlı miktar koruyan düzeltme:

\[
\hat x=\arg\min_{x\ge0,\,\sum_i x_i=q}\frac12\sum_i w_i(x_i-v_i)^2,
\qquad
\boxed{\hat x_i=\max(0,v_i-\lambda/w_i)}.
\]

\(w_i>0\); \(\lambda\), \(\sum_i\max(0,v_i-\lambda/w_i)=q\) denkleminin tek boyutlu monoton aramasıyla bulunur. Bilinen seviyeler önce sabitlenir; kalan miktar kalan seviyelere dağıtılır. Bu işlem toplamı tam korur, kayıp dağılımı tahmin eder.

Geriye dönük düzeltmede son 1–3 saniyelik başlangıç deney penceresi birlikte çözülebilir:

\[
\min_{X,a,c,e}\ \sum_k\rho_\delta(H_kX-y_k)
+\lambda_f\sum_t(\|a_t\|_1+\|c_t\|_1)
+\lambda_p\sum_t\|x_t-m_t\|_2^2
+\lambda_s\sum_t\|D_px_t\|_2^2.
\]

Koşullar: nonnegativity, defter dinamiği, güvenilir snapshot/BBO ve kesin kayıtlar için \(H_kX=y_k\) ya da bilinen quantization toleransı. Huber kaybı yalnız belirsiz gözlemlerde kullanılır. Warm-start sparse ADMM/proximal çözüm worker'da çalışır; 30 Hz kapasitesi ölçülmeden vaat edilmez. Modelin ekleme/iptal tahmini aynı piyasaya ait veride kalibre edilebilir. [Queue-reactive model](https://arxiv.org/abs/1312.0563) durum temelli akış modeline, [ADMM araştırması](https://stanford.edu/~boyd/papers/admm_distr_stats.html) çözüm yöntemine dayanak sağlar; burada önerilen yeniden kurma sistemi bu araştırmanın tasarım önerisidir.

Canlı tahmin yalnız o ana kadar gelen bilgiyi kullanır. Sonraki snapshot geçmiş tahminini düzeltiyorsa bu geçmiş revizyonu olarak işaretlenir; gelecekteki bilgi canlı başarı ölçümüne sızdırılmaz.

Kesinlik için pratik sınır: iki bilinmeyen seviyenin toplamı 10 ise `(10,0)`, `(0,10)` ve `(4,6)` aynı gözlemi sağlar. \(Hz=0\) ise \(H(x+z)=Hx\); diğer kısıtlar da sağlanıyorsa tek gerçek defter belirlenemez. Her seviyeye gözlemlerle uyumlu küme \(\mathcal F\) üzerinden \(\underline x_i=\min_{X\in\mathcal F}x_i\), \(\overline x_i=\max_{X\in\mathcal F}x_i\) raporlanır. Bunlar otomatik %95 güven aralığı değildir. Yalnız zaman ortalaması varsa anlık üst sınır ek varsayım olmadan sınırsız kalabilir.

Bu model 30 Hz'de kendi hesaplanmış L2 durumunu sunabilir. Bunun sayacı model hesaplama hızıdır; kaynakta 30 bağımsız gerçek değişim/s üretildiği anlamına gelmez. Crocodile'ın mevcut provenance sözleşmesinde native gözlem, native kayıttan türetim ve modelle üretilen tahmin birbirinden ayrılmalıdır. Tahmini veri native kaydı ezmez.

Verilen FlowMap raporundaki tüm sınırlar için eşleme:

| Sınır | Önerilen çözüm | Başarı koşulu |
|---|---|---|
| Eski coarse hücreden ince seviyeler kaybolmuş | Aynı venue/ürün/tarihli özgün arşivden backfill; yoksa gözlem sınırlarıyla tahmin. Gelecekte raw günlük | Geri kazanılan bölümün kaynak/kapsamı doğrulanır; tahmin native diye sunulmaz |
| ≥30 gerçek L2 güncellemesi/s | Uygun piyasada SBE/raw/20 ms kanal; L3'ten gözleme bağlı L2 üretimi | Aktif dönemlerde özgün değişen olaylar sayılır; heartbeat ve tekrar durumlar sayılmaz |
| Soğuk tam arşiv 138,213 sn | Kayıt sırasında kalıcı çok çözünürlüklü özetler; manifest ve görünür tile sorgusu | Hazır özetli arşiv yeniden açılışı ile ilk dönüşüm ayrı ölçülür |
| Açılış 9,402 sn ve 100–133 ms kareler | Küçük doğrulanmış checkpoint; geçmiş hydration'ını canlı kritik yoldan ayırmak; küçük upload işleri | Yerel hazır olma, upstream sync ve ilk gerçek paint ayrı izlenir |
| 8 kaynak-belirsizliği aralığı | CPU/diskte kesin aralık listesi; versioned tile/RLE GPU mask | Sekiz üstü ayrık kaynak alanında belirsizlik gereksiz birleşmez |
| 16 MiB histogram indeks bütçesi | Worker tile histogramı, cache ve görünür sınırların kaynak düzeltmesi | Budget aşımı kaynak kaybına ya da uzun ana-thread taramasına dönüşmez |
| Native RAM/VRAM için eksik kanıt | Merkezi byte bütçesi, tile residency, lazy ring ve gerçek process/GPU ölçümü | Tarih uzadıkça/simge değiştikçe working set plato yapar |
| FPS sıfır veri kaybını kanıtlamıyor | Ingress→journal→replay→UI sürüm/kayıp mutabakatı | Sequence/checksum kapsamı, durable lag ve client lag ayrı raporlanır |
| Her cihaz/her tarih garantisi yok | Tanımlı desteklenen cihaz profilleri, deterministic replay, stress ve soak | Ölçülmüş destek alanında kalite hedefleri; bozulma görünür ve recovery ölçülmüş |

Kalıcı özet tasarımının kritik ayrıntısı: farklı fiyat haritalarına daha önce yeniden örneklenmiş Float32 çocuk özetleri tekrar ortalamak doğruluğu bozabilir. Özgün harita bileşenlerinde Float64 \(\sum q\Delta t\), süre, OHLC, volume toplamı, son cumulative CVD/VWAP, coverage ve boşluk bilgisi saklanmalı; aynı haritalar birleşmeli, sorgu haritasına özgün bileşenlerden bir kez projeksiyon yapılmalıdır. Native bileşen sayısının büyümesi de bütçelenmeli; görüntü tile'ı ile exact kaynak sorgusu ayrı cache'ler kullanmalıdır.

Mevcut `core/overview.py:34` yalnız 32 MiB session RAM cache'i, `:669` scalar disk indeksi tutuyor; `:777` cold miss'te kaynak yoğunluklarını yeniden okuyor. Bu nedenle kalıcı yoğunluk özeti ilk büyük kazanımdır. İlk eski arşiv dönüşümü resumable worker ile yapılır; hesaplama bedeli ortadan kalkmış gibi gösterilmez. Manifest recording UUID, şema/algoritma sürümü, geometri fingerprint, coverage generation ve watermark taşır. Yarım yazım, checksum uyuşmazlığı ve sonradan değişen part yalnız etkilenen özetleri geçersiz kılar.

Kaynak belirsizliği ve gap metadata da sayfalanmalıdır; bugünkü 1.024 gap marker sınırının çok sayıda kesintide bilgiyi gizlemesi önlenmelidir. Price profile'ın 1.024 kolonla sınırlı yaklaşık sweep'i için worker özetleri ve exact kenar düzeltmesiyle seçili aralığın tamamı kapsanmalıdır. Normalization'ın kaynak üyeliği doğru olsa da 256-bin histogram yüzdelikleri matematiksel exact quantile değildir; bağımsız quantile oracle ile sapma ölçülmelidir.

Mevcut kodda kayıt kotası 20 GB ve eski raw Parquet'leri silebiliyor (`config.py:90`, `core/record.py:1093`). Özetler raw yerine geçmişin tamamı sayılmamalı. Kota sıcak cache'e uygulanacaksa ham kaynak başka doğrulanmış yerel arşivde tutulmalıdır; sonlu diskte sınırsız kayıt taahhüt edilemez. Saklama ihtiyacı sparse delta kayıtlarının gerçek bytes/event ölçümünden hesaplanmalıdır. Sadece 4.096 satırlı Float16 bid/ask yoğunluğunu 30 kolon/s kaydetmek metadata hariç yaklaşık 42,5 GB/gün/sembol tüketir; raw sparse olay + checkpoint yaklaşımı bu yüzden gereklidir.

Uygulama öncesinde incelemeye sunulan sıra:

1. **Yetenek ve ölçüm sözleşmesi:** her kaynak için snapshot, sequence/checksum varsa kapsamı, birimler, bilinen fiyat alanı, unique-event cadence, gap ve kayıt provenance tanımlansın. Geçiş kriteri heartbeat/tekrarların yeni veri sayılmaması.
2. **Ham kayıt ve replay temeli:** gruplama öncesi frame günlüğü, versioned decoder ve checkpoint. Geçiş kriteri recorded replay ile canlı kaynak defterinin kapsam içinde eşleşmesi; crash/reconnect sonrası açık recovery.
3. **İlk hızlı kaynaklar:** Binance Spot SBE; mevcut Deribit bağlantısına auth/raw; Bybit fast50/full kapsam ayrımı; desteklenen genel bağlantılarda WS. Her biri bağımsız tamamlanabilir.
4. **L3 genişlemesi:** önce anahtarsız Bitfinex R0, sonra Kraken L3. Geçiş kriteri emir-ID durumundan L2 miktar toplamının doğrulanması.
5. **Ücretsiz geçmiş:** Bybit ve açık araştırma fixture'ları için şema/zaman/açılış-state kontrolü; aynı piyasa olmayan veri yalnız ayrı araştırma kaynağı olarak kalır.
6. **FlowMap kalıcı özetleri ve worker görünümü:** aynı native miktar/geometri/gap invariants korunarak soğuk yükleme ve bellek iyileştirilir.
7. **Matematiksel tamamlayıcı:** oracle L2'yi coarse/BBO/trade gözlemlerine indirip önerilen modeli sınamak; model verisini ayrı provenance ile sunmak.

Önerilen kabul hedefleri henüz ölçülmüş sonuç değildir: adlandırılmış referans cihazda hazır kalıcı özetle 327 bin kolonluk görünüm ≤5 sn; ilk anlamlı tarih görüntüsü ≤1 sn; upstream erişilebilirken ilk gerçek canlı quote p95 ≤3 sn. Yerel açılış, bir kerelik dönüşüm, process-cold/storage-cold ve warm ayrı ölçülür. Steady hedef 60 FPS; yük altında bir saniyelik pencerelerde ≥30 FPS. Kaynak ilgili panele ≥30 gerçek revizyon/s sağladığında panelin uyguladığı ve sunduğu ayrı revizyonlar ölçülür.

Doğrulama kapsamı: tüm görünür paneller birlikte; BTC/ETH ve sessiz bir sembol; kaynak cadence, p95/p99 event-to-presentation, journal durable lag, drop/gap/resync; değişen/geri dönen haritalar, tiny tick ve büyük fiyat; context loss, hızlı symbol/stride değişimi, gecikmiş eski cevaplar; bozuk/yarım arşiv ve ilk orijinal kolonlar; 30–60 dakikalık kombinasyon testleri ve 24 saat soak. Sıfır hesaplanan drop ile borsanın yayınlamadığı olayları bildiğimiz iddia edilmez.

İlk önerdiğim uygulama paketi **ham kayıt/replay sözleşmesi + Binance Spot SBE + Deribit raw**. Bu paket veri kaybını azaltan temel ile iki açık hız kazanımını birleştirir. Anahtarsız alternatif önceliği gerektiğinde Bybit ve Bitfinex aynı sözleşmeye takılır. Bu belgedeki tasarım onaylandıktan sonra ayrı Crocodile kaynak checkout'u ve uygulanacak revizyon doğrulanarak uygulama planı hazırlanmalıdır.
