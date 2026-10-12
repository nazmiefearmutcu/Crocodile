# Crocodile 0.3.1

Bu sürüm, ücretsiz erişilebilen yerel emir defteri kaynaklarını, ham kayıt ve
çevrimdışı tekrar altyapısını, OKX arşiv araçlarını ve sınırları açıkça belirtilen
sentetik L2 tahminlerini ekler. Eski `crypcodile` ve `stockodile` importları ve
komutları çalışmaya devam eder; kaldırılmaları 0.4 için planlıdır. Python 3.12 veya
üstü gerekir.

## Kaynak farkı

Yayın hazırlığı, `nazmiefearmutcu/Crocodile` deposunda fetch ile doğrulanan
`origin/main` (`2e22c901dff7f0b4ec98ef7e66b8a10de4464d9c`) ile yerel uygulama
ucu (`72778b1829be189b7f2336d9bfc0ab1789471e0a`) arasındaki 24 commit'i kapsar.
Bu belge ve 0.3.1 sürüm metadata'sı bu değişikliklerin üzerine eklenmiştir.
Yayın kontrolünde bulunan opsiyonel bağımlılık import hatası da giderilmiştir:
temel kurulum CLI'yi ve çevrimdışı lending/transfer yardımcılarını açar; Web3
gerektiren pool işlemleri eksik kurulumda `crocodile[onchain]` kurulumunu bildirir.

## Yerel defter kaynakları

Bitfinex, Kraken ve dYdX v4 doğrudan desteklenir. Coinbase'in varsayılan Exchange
modunun yanında açıkça seçilen Advanced modu; Bybit'in varsayılan fast modunun
yanında REST başlangıç görüntüsüyle eşlenen full modu bulunur. Bitfinex, Kraken ve
dYdX adaptörleri şu anda `book_delta` ve `book_snapshot` kanallarını kabul eder.

Ham kayıtta kaynak, piyasa, yerel sembol, endpoint ve decoder kimliği birlikte
tutulur. Aşağıdaki endpoint adları uygulamanın gerçek kayıt kimlikleridir:

| Kaynak | Mod ve endpoint kimliği | Kapsam |
| --- | --- | --- |
| `binance-spot` | `binance-spot-sbe-ws` | Spot SBE depth-diff; REST başlangıç görüntüsü |
| `deribit` | `deribit-raw-ws` | Yetkili hesapla raw L2; L3 emir kimliği taşımaz |
| `bitfinex` | `bitfinex-r0-f0-250` | Her tarafta en fazla 250 görünür emir |
| `kraken` | `kraken-book-{depth}` / `kraken-level3-{depth}` | Public L2 veya yetkili görünür L3 |
| `coinbase` | `coinbase-exchange` / `coinbase-advanced` | Seçilen protokolün spot L2 defteri |
| `dydx` | `dydx-v4-unbatched` | v4 public indexer, `batched:false` |
| `bybit` | `bybit-{category}-{book_mode}-{depth}` | Spot/linear/inverse; fast ayrıca options |

Görünür L3 emirlerinin L2 toplamı `derived`, `visible_l3_sum` olarak işaretlenir.
Gizli likidite bu toplamın dışında kalır. Protokol sayacı ve checksum kontrolleri
bozulursa yeni bağlantı veya başlangıç görüntüsü beklenir. Kaynağın vermediği
defter sıra numarası oluşturulmaz. Ondalık ayrıştırma ve toplama, mevcut float
kayıt şemasına dönüşümden önce yapılır; float aralığına sığmayan değerler yayından
önce reddedilir. Ham kayıt özgün sayısal metni saklar.

Spot/linear ve dYdX miktarları `base_asset`, Bybit inverse `quote_contracts`, Bybit
options `native_contracts` olarak bildirilir. Kaynakları birleştirirken bu birimler
ve enstrüman bilgisi dikkate alınmalıdır. Mod ayarları ve örnekler:
[yerel defter kullanım belgesi](free-l2-sources.md).

## Ham kayıt ve tekrar

`CROCODILE_RAW_CAPTURE=true`, desteklenen yerel kaynaklarla `collect` sırasında
gelen piyasa frame'lerini, alım saatlerini, public enstrüman metadata'sını, REST
başlangıç yanıtlarını ve deftere uygulama kararlarını kaydeder. Kayıt defteri
kontrol mesajlarından ayrılır; API anahtarları, kimlik doğrulama payload'ları ve
tokenlar ham piyasa kaydına yazılmaz. Zorunlu kayıt, kuyruk veya disk hatasında
toplama temizlenir ve hata çağırana iletilir.

Varsayılan kuyruk bütçesi 32 MiB, tek kayıt sınırı 16 MiB, segment hedefi 256 MiB'dir.
CRC ve mühürlenmiş segment bütünlüğü denetlenir. Süreç kesintisinden toparlanma
seçeneği yalnızca aktif segmentin eksik son kaydına izin verir; tamamlanmış bozuk
kayıtları geçerli saymaz.

`CaptureReader` ve `RawReplay`, canlı bağlantı veya API bilgisi olmadan kayıtlı
kararları tekrarlar. `pacing="original"` için gerçek monotonic alım saati gerekir.
Normalleştirilmiş Parquet `replay` komutunun anlamı aynı kalır. `collect-market`
ham kayıt kabul etmez; bu kaynaklar için `collect` ve yerel semboller kullanılır.
`scripts/benchmark_raw_capture.py`, tekrar hızını ve farklı frame sınıflarını
raporlar; depo içinden çalıştırılan bir araçtır.

## Binance SBE ve Deribit raw erişimi

Binance SBE açıkça seçilir: `CROCODILE_BINANCE_WIRE_FORMAT=sbe`,
`CROCODILE_BINANCE_MARKET=spot`, `CROCODILE_BINANCE_SBE_API_KEY`. Ed25519 olarak
kayıtlı hesabın API anahtar metni kullanılır; private key gerekmez. Varsayılan
JSON'dur. SBE, aynı `binance-spot` kaynak kimliğini korur. Full depth-diff defteri
REST ile başlatılır; ayrı top-20 görüntüler bu defteri sıfırlamaz.

Deribit raw için `CROCODILE_DERIBIT_INTERVAL=raw`,
`CROCODILE_DERIBIT_CLIENT_ID` ve `CROCODILE_DERIBIT_CLIENT_SECRET` gerekir.
Hesabın raw kanalına yetkili olması gerekir; yetki hatasında sessiz fallback
yoktur. Varsayılan anonim `100ms` modudur. Yeni socket yeniden kimlik doğrular;
istek eşleme, token yenileme ve uygulama heartbeat'leri piyasa işleme akışından
ayrı yürütülür. Sıra boşluğu yalnızca ilgili sembolü geçersiz kılar; onaylı yeniden
abonelik ve yeni snapshot sonrasında deltalar tekrar kabul edilir. Kraken L3
ayrıca WebSocket yetkili API anahtarı ve base64 secret ister.

## OKX arşivi ve sentetik tahmin

`scripts/archive_l2.py`, public OKX kataloğunu listeler; yalnızca açıkça seçilen
objeyi indirir. İndirme byte sınırı, ETag/Range ile sürdürme ve SHA-256 manifesti
vardır. Desteklenen 400/5000 seviyeli tar/NDJSON dosyaları diske açılmadan okunur;
schema, başlangıç snapshot'ı ve kaynak saatinin sırası denetlenir. Denetimin
tamamlanması için iterator sonuna kadar tüketilmelidir. Zaman sıçraması olası
kapsam boşluğunu gösterir; kaç borsa olayı kaybolduğunu kanıtlamaz. Legacy CSV,
Bybit arşiv adayları ve araştırma veri setleri için ayrı audit gereklidir.

`scripts/reconstruct_l2.py` ve `solve_window`, açık süreli sonlu bir ızgarada
önselleri ve kısıtları kullanarak `synthetic` sonuç üretir. Nedensel mod gelecekte
erişilebilecek gözlemleri reddeder; retrospective mod ayrı `revision_id` ister.
İterasyon/zaman bütçesi, residual, çözüm durumu ve bound durumu raporlanır.
Feasible aralıklar belirtilen kısıtlara bağlıdır; istatistiksel güven aralığı
değildir. `unconverged` sonuç geçerli çözüm olarak kullanılamaz. Eksik geçmişin
tam olay dizisi, gizli emirler ve gerçek ekleme/iptal akışı bu yöntemle geri
kazanılmaz; `source_event_count` bilinmeyen olarak kalır.

## Diğer değişiklikler ve ölçüm sınırları

FINRA `short_volume` / `short_interest` backfill sağlayıcısı, ATS haftalık özet
dönüşümleri ve SEC fails-to-deliver indirme/analitik yardımcıları da bu sürümdedir.
Bunlar gecikmeli açıklama verileridir; canlı emir defteri kapsamında değildir.
Short-volume ve short-interest sorgularında sembol ve zaman filtreleri SQL
parametresi olarak bağlanır; zaman değerlerinin tam sayı olması doğrulanır.

Kaynakta belirtilen 20 ms gibi yayın aralıkları her sembolde saniyede 30 farklı
defter değişimini garanti etmez. Heartbeat, tekrar frame'i, BBO ve trade sayısı
değişen depth revizyonundan ayrı ölçülür. Offline tekrar hızı canlı alım hızı
değildir. Başlangıç snapshot'ının dışındaki değişmemiş seviyeler bilinmeyendir;
Bybit full için bildirilen 10.000 seviye sınırı gerçek dönen seviyeden ayrı tutulur.

Kuyruğa kabul edilen, yazılan ve fsync edilmiş kayıt ayrı sayaçlardır. Bir saniye
fsync hedefi disk tıkanıklığında aşılabilir; süreç çökmesi henüz fsync edilmemiş
son kısmı kaybettirebilir. Kapanışta fsync yapılması, çalışmanın tamamında bir
saniyelik kalıcılık sınırı sağlandığını göstermez. Güç kaybı sonucu dosya sistemi
ve donanıma bağlıdır. Bu yayın notu canlı cadence veya kimlik doğrulanmış kanal
erişimi için yeni bir ölçüm sonucu ileri sürmez.
