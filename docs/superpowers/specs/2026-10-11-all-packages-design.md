# Crocodile ve FlowMap kalan paketler — onaylı uygulama sözleşmesi

Kullanıcı 11 Ekim 2026 tarihinde bütün paketleri onayladı; doğrudan uygulama
ve sonunda bağımsız inceleme tercihi sürer. Önceki capture/SBE/Deribit paketi
yerel Crocodile main `9ab2aef` üzerinde tamamlandı. Kalan çalışma aşağıdaki
bağımsız paketleri birlikte teslim eder.

## 1. Ücretsiz canlı kaynaklar

Bitfinex public R0/F0/250 emir-ID defteri, Kraken public L2 ve yetkili L3,
dYdX v4 unbatched L2, Coinbase public Advanced L2, Bybit hızlı yakın defter
ve ayrı full kapsamı eklenir. Aynı venue/ürün kimliği korunur; başka borsa
verisi Binance geçmişi gibi sunulmaz. L3 görünür emirlerden Decimal ile
türetilen L2 DERIVED olarak işaretlenir. Channel ve connection sayacı
canonical book sequence'e yanlış çevrilmez; heartbeat yeni depth sayılmaz.
Raw capture ve offline replay, public channel eşlemeleri ve süreklilik
durumunu yeniden oluşturabilir; auth payload/header/token saklanmaz.

## 2. Ücretsiz geçmiş ve matematiksel tamamlayıcı

Anonim OKX public market-data-history listesi, bounded/resumable dosya
indirme, hash/provenance manifest, güvenli tar NDJSON okuma ve source-time
iterasyonu sağlar. Şema ve açılış snapshot'ı doğrulanmadan defter kesin
sayılmaz. Kaynak sequence yoksa alan icat edilmez. Bybit/Zenodo adayları
ayrı kaynak denetimi olarak kataloglanır; büyük arşiv indirme zorunlu değildir.

Anlık coarse miktar için ağırlıklı simplex projeksiyonu toplamı korur.
Zaman ortalaması için gerçek süre ağırlıklı H modeli ve nonnegative,
bilinen snapshot/BBO kısıtları kullanılır. Pencere çözücüsü iterasyon ve
zaman bütçelidir; yakınsamayan sonuç kesin sayılmaz. Feasible miktar
sınırları istatistiksel güven aralığı değildir. Causal çıktı gelecekteki
snapshot'ı kullanmaz; sonradan gelen bilgi retrospective revision olarak
ayrı çıkar. Model çıktılarını native olay sayacı ve journal üzerine yazmaz.

## 3. FlowMap kalıcı geçmiş ve kaynak belirsizliği

Kalıcı dyadic özetler kaynak harita başına Float64 miktar×süre,
integer süre, coverage, OHLC/volume, terminal cumulative değerler ve
gap metadata taşır. Child projeksiyonlarını tekrar ortalamaz. Version,
recording ID, geometry/source fingerprint, watermark ve atomic publish
ile bozuk/eskimiş cache raw kaynağa güvenli döner. Yeni kayıt ve eski
arşiv dönüşümü bounded worker'da ilerler. Soğuk ilk dönüşüm ile hazır
özetli process-cold yeniden açılış ayrı ölçülür.

Sekiz kaynak-belirsizliği aralığı ve 1.024 gap sınırı kaynak bilgisini
sessizce birleştirmemeli veya kesmemeli: exact CPU/disk aralıkları,
GPU mask ve metadata pagination kullanılır. Saklama kotası authoritative
kaynak kaybını görünür raporlar; özet kaynak yerine geçirilmez.

## 4. FlowMap runtime ve frontend

Gerçek yeni Crocodile revizyonu yeniden üretilebilir yerel wheel olarak
FlowMap dependency/bundle yoluna bağlanır. SBE seçimi 100ms subclass
override'ını atlar. Raw journal, UI bounded queue'dan önce bağlanır;
feed metadata sequence/provenance/coverage ve ingress/UI farkını korur.
Kaynak bağlantısı, journal, panel uygulama ve render hızları ayrı ölçülür.
16 MiB indeks fallback ve tam görünür profile işleri bounded worker/
zaman dilimlerine taşınır; eksik/eskimiş sonuç kesin ve tam gösterilmez.
Kaynak arşivleri silinmeden RAM/VRAM residency sınırları ölçülür.

## Kabul ve uygulama sınırları

Her paket meaningful RED→GREEN testlerle yürütülür; tek bağımsız son
inceleme bütün yeni diff'i kapsar. Tüm yeni kaynaklar için fixture
capture/replay ve gap/reconnect testleri; anahtarsız canlı kanallarda
kısa ölçüm; yetkili kanallarda yerel credential yoksa erişim ölçülmedi
durumu; eski arşivde raw-vs-summary miktar oracle; frontend/GL ve kurulu
native FPS/latency/history/RSS ölçümü gerekir. Hazır özetle 327 bin
kolonun ≤5sn açılması ve ilk anlamlı tarih ≤1sn hedef olarak ölçülür;
gerçek sonuç hedefi karşılamazsa rapor açıkça bunu belirtir.

Başlangıç FlowMap primary `C:/fm` dirty olduğundan mevcut tracked ve
untracked kaynak durumu izole `work/flowmap-l2` snapshot `604c914` içinde
korundu. Primary'ye yalnız bu snapshot üzerindeki yeni farklar, hedef
dosyaların başlangıç hash'leri doğrulanarak aktarılır. Mevcut kullanıcı
değişiklikleri commit veya reset edilmez. Crocodile temiz feature
worktree'sinde ilerler. Remote push/PR bu onaydan çıkarılmaz.

Self-review: paket sahiplikleri ayrıldı; auth/gap/source-time, tahmin
provenance, map projection, dirty-source entegrasyon ve cold/warm ölçüm
koşulları açık. Eksik geçmişin kesin matching-engine olaylarını üretme
ve bütün cihazlarda sınırsız hız/bellek garantisi yoktur.
