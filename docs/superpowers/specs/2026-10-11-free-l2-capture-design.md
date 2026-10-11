# Crocodile ham kayıt/replay, Binance Spot SBE ve Deribit raw tasarımı

11 Ekim 2026. Kullanıcı bu ilk paketin tasarımını onayladı. Belge, kaynak incelemesiyle netleştirilen uygulama sözleşmesidir. Uygulama kodu ve canlı yetkili kanal ölçümü henüz yapılmadı.

Kaynak depo: `C:\Users\Kullanıcı\.gemini\antigravity\scratch\crocodile`. Başlangıç branch'i `main`, HEAD `9e6122e5888ec0e74ea72a1a46d02bccf17f5daa`, çalışma ağacı temiz. Remote `https://github.com/nazmiefearmutcu/crocodile`. FlowMap'in pinlediği `2e22c901dff7f0b4ec98ef7e66b8a10de4464d9c` burada mevcut; yeni HEAD yalnız equity altyapısı ekleyen bir commit ileride. Uygulama kurulu site-packages veya uv cache üzerinde yapılmayacak.

Amaç, borsanın yayımladığı ve erişilebilir kapsamda alınan L2 bilgisini Crocodile'ın gruplama/normalizasyon işleminden önce korumak; aynı defteri ağ erişimi gerektirmeden tekrar kurmak ve iki hızlı kaynağı opt-in olarak sunmaktır. Yeni gerçek kaynak olaylarının hızı, yerel hesaplama ve ekran yenileme hızından ayrı ölçülür.

Bu ilk paket dört birimden oluşur: transport frame zarfı; ham journal ve offline replay; Binance Spot SBE decoder/connector; Deribit raw authentication/sequence yaşam döngüsü. FlowMap kalıcı overview piramidi, diğer borsalar ve matematiksel tahmin ayrı devam paketleridir.

## 1. Ortak veri ve kimlik sözleşmesi

Mevcut canonical `Record` ve normalized Parquet replay API'si korunur. Raw veri ayrı bir şema ve API kullanır; byte payload mevcut `Record` union'ına eklenmez. Mevcut `NATIVE/DERIVED/SYNTHETIC` ayrımı sürer; bu pakette model kaynaklı L2 üretimi yoktur.

Transport zarfı şunları içerir: özgün payload bytes, TEXT/BINARY türü, alındığı andaki UTC ns ve monotonic ns, connection kimliği ve receive ordinal. Legacy `AsyncIterator[bytes]` transports için adapter bulunur; yalnız bytes sağlayan fixture'a ağdaki frame türü ve gerçek receive-time varmış gibi metadata atanmaz. Native live transport metadata sağlıyorsa connector aynı zarfı kullanır.

Journal kimliği capture UUID, venue/market/instrument, endpoint kimliği, schema/decoder sürümü ve global journal ordinal içerir. UTC timestamp sıralama anahtarı değildir; saat geri gitse bile ordinal monoton kalır. Eşzamanlı kaynaklar tek writer tarafından ordinal alır; per-connection receive ordinal ayrıca korunur.

`binance-spot:BTCUSDT` aynı instrument olarak kalır. SBE taşıma biçimi session metadata'sıdır; `binance-sbe` adlı yeni piyasa kimliği oluşturulmaz. USDⓈ-M aynı sembol yazısını kullansa da ayrı market/defterdir. Deribit kontrat birimleri instrument metadata'sında korunur; futures USD miktarı ile Spot base miktarı doğrudan toplanmaz.

## 2. Ham journal, snapshot ve recovery

Capture opt-in'dir; etkinleştirildiğinde required-capture davranışı kullanır. Market frame, decoder/normalizer ve downstream sink/drop kuyruklarından önce journal'a kabul edilir. Sağlıklı capture için received, queued, written ve fsynced ordinal/sayılar ayrı tutulur. UI veya normalized sink'in birleştirmesi ham kayda uygulanmaz.

Authentication request/response ve token refresh mesajları market-data journal'a girmez. Outbound credentials veya API header'ları kaydedilmez. Deribit kontrol yanıtları önce request-ID handler tarafından tüketilir; access/refresh token payload'ları kayda ve loga çıkmaz. Bilinmeyen kontrol mesajlarında ham payload yerine redacted hata metadata'sı kullanılır. Binance binary market frame'leri ve market subscription bildirimleri özgün bytes olarak korunur.

Journal yalnız WebSocket frame'lerinden oluşamaz. Binance başlangıç/resync REST snapshot response'u, receive zamanı, snapshot request kimliği ve uygulama karar sırası da kaydedilir. Instrument registry/tick/kontrat metadata'sı capture bağlamına dahil edilir. Offline replay, ilgili kayıtlı snapshot provider ile çalışır; yeni REST çağrısı yapamaz. Buffered WS mesajları, snapshot'ın alınması ve deftere uygulanması kayıtlı kontrol sırasına göre yeniden yürütülür.

Dosya formatı versioned binary framing kullanır: file magic/version, ardından length + MsgPack envelope/payload + CRC32 kayıtları. `msgspec` mevcut bağımlılıktır. Her kayıt uzunluğu sınırı 16 MiB'dir; okuyucu bu sınırı allocation öncesi kontrol eder. İlk sürüm sıkıştırmasız immutable segmentlerle başlar; byte fidelity ve recovery doğrulandıktan sonra bağımsız block compression eklenebilir.

Writer kuyruğu başlangıçta 32 MiB byte bütçelidir, record sayısı değil gerçek payload/envelope boyutuyla hesaplanır. Overflow, write/short-write veya fsync hatası required capture'ı başarısız kılar ve ilgili live session'ı visibly fail-stop yapar. Hata parse/DLQ exception handler'ında yutulmaz. Oturum yeniden başlatıldığında yeni capture UUID ve yeniden eşitlenmiş snapshot gerekir; önceki oturum kesintisiz kabul edilmez.

Segment hedefi 256 MiB; kayıt segment sınırında bölünmez. Writer data'yı önce `.partial` dosyasına yazar, flush + `os.fsync` başarılı olduğunda durable watermark ilerler. Fsync aralığı varsayılan en fazla 1 saniyedir; sınır sağlayamayan writer durable-lag ihlali bildirir. Kuyruğa kabul edilmiş fakat fsync olmamış tail'in crash sonrası kaybolabileceği açıkça raporlanır. Kuyruğa alınma durability değildir.

Normal kapatmada kuyruk boşaltılır, fsync tamamlanır ve segment sealed olarak atomik yayımlanır. Recovery yalnız son active `.partial` segmentteki eksik tail'i ayırabilir; tam bir kaydın CRC hatası veya sealed segmentin ortasındaki corruption integrity error üretir. Bozuk kaydı atlayıp kesintisiz replay yapamaz. Bu sözleşme process-crash recovery'sidir; donanım write-cache/power-loss için evrensel garanti verilmez.

Manifest capture metadata, sealed segment listesi, byte/ordinal bounds, checksum ve decoder bağlamını taşır. Güncelleme atomik replace ile görünür olur. Kayıt ve manifest uyumsuzsa reader doğrulayıp hata raporlar; yanlış kayıt sessiz kabul edilmez. Secrets manifestte de bulunmaz.

## 3. Offline replay

Raw replay journal ordinal sırasını kullanır; normalized replay'in timestamp temelli merge davranışı ayrı kalır. Pacing seçeneği original monotonic farkları veya hızlı deterministic replay'dir. Original source/receive timestamps ve payload bytes değiştirilmez. Yeni çalışmanın execution time'ı ayrı telemetri alanıdır.

Replay decoder sürümü ve instrument metadata'sını doğrular. Desteklenmeyen şema, CRC bozukluğu veya eksik snapshot anchor'ı typed failure üretir. Ağ transport/snapshot provider kurulması replay modunda yasaktır. Recorder/replayer'ın aynı normalized canonical çıktıları ve kapsam içindeki son defteri vermesi kabul kriteridir.

Raw binary kayıtta özgün mantissa/exponent korunur. Canonical mevcut float alanlarıyla uyumlu çıktı alır; kaynak precision'ın korunması ham journal katmanına aittir. NaN/Infinity/overflow canonical validasyonunu geçemez.

## 4. Binance Spot SBE

Opt-in `spot+sbe` seçimi; Ed25519 API key yalnız bağlantının `X-MBX-APIKEY` header'ında kullanılır. Private key veya trade izni gerekmez. JSON control text ile binary market frame ayrı işlenir. Mevcut JSON modu varsayılan davranışı sürdürür.

Decoder resmî `stream_1_0.xml` şemasına göre çalışır: schemaId 1, version 0, little endian, 8-byte `<HHHH>` header. Source reference commit'i `bed5d358dbe98bfa4722b6c00c27be1e8f0ba40b` olarak audit sırasında doğrulandı; fixture metadata'sı bu upstream kimliğini taşır.

| Template | ID | Root block length | Group |
|---|---:|---:|---|
| TradesStreamEvent | 10000 | 18 | `<HI>` group header; 25-byte element |
| BestBidAskStreamEvent | 10001 | 50 | Yok |
| DepthSnapshotStreamEvent | 10002 | 18 | Bids/asks ayrı `<HH>`; 16-byte element |
| DepthDiffStreamEvent | 10003 | 26 | Bids/asks ayrı `<HH>`; 16-byte element |

Exponents signed int8; değer `mantissa × 10^exponent`. Microsecond timestamp canonical ns'e ×1000 aktarılır. Trailing symbol uint8 byte length ve UTF8'dir. Offset'ler root/group blockLength ile hesaplanır; uzunluk alanları payload sınırlarıyla doğrulanır. Desteklenen version içindeki extension bytes uzunluklar üzerinden atlanabilir; bilinmeyen schema/version typed protocol failure'dır. Bilinmeyen template kayda korunur ve açık unsupported-data durumu üretir; depth sürekliliği varsayılmaz.

Depth stream 20 ms yayın aralığıdır; her sessiz sembolde 50 yeni değişim/s garanti etmez. SBE BBO auto-culling uygulayabilir; BBO sayacı kusursuz olay günlüğü kanıtı olarak kullanılmaz.

Canonical `book_delta/book_snapshot` talepleri aynı `@depth` diff ve REST bootstrap yolunu kullanır. `@depth20` ayrı, sınırlı kapsamlı opt-in akıştır; geniş defter state'ine snapshot olarak uygulanmaz. Dört template'in decode edilebilmesi, tümünün aynı book state'e uygulanması anlamına gelmez.

Spot book bootstrap: WS reader kayıt/buffer işlemine devam ederken REST snapshot alınır. Snapshot ID yeterince güncel değilse yeniden alınır. `u <= local_id` stale event'ler elenir. Uygulanabilir event için `U <= local_id+1 <= u` aralığı gerekir; `U > local_id+1` gap'tir. Yeni miktarlar absolute replacement, sıfır delete'tir. Mevcut shared sync'in kesin `U==previous_u+1` şartı Spot için overlap/stale kurallarıyla düzeltilir; Futures `pu` zinciri ayrı korunur.

Sequence U/u kontrolü decoder envelope'da korunur; canonical şemaya alan eklenmeden journal ve sync bu envelope'u kullanabilir. Yalnız `seq_id/prev_seq_id` ile raw U'nun kaybedilmesine izin verilmez. Başlangıç snapshot coverage en fazla 5.000 level/side olarak bildirilir; uzak ve değişmeyen seviyeler known değildir.

24 saat bağlantı süresi ve `serverShutdown` yenilenme nedenidir. Supervisor reconnect sırasında yeniden snapshot eşitlenmesi ister. Live transport CLOSE/ERROR, fixture normal EOF'den ayırt edilir; canlı socket kapanışı sessiz başarılı bitiş sayılmaz.

## 5. Deribit raw

Connector'a explicit `interval` seçimi eklenir: varsayılan `100ms`; opt-in `raw` için credentials gerekir. Credentials yoksa başlamadan configuration error oluşur; sessiz 100ms fallback yapılmaz. Raw bu piyasada 1 ms toplulaştırmadır, L3 emir kimliği sunmaz.

Lifecycle: connect → request-ID ile `public/auth` → başarı/expiry doğrulama → raw subscription → subscription ack → live snapshot. İlk pakette connection-scope tokens kullanılır; her reconnect'te yeniden auth yapılır. Socket kapandıktan sonra önceki access/refresh token yeniden kullanılmaz. Aynı socket içinde expiry'den önce refresh yapılır; refresh failure session'ı degrade/fail eder.

Kontrol yanıtları genel normalizer'dan önce işlenir. Auth/subscription rejection fatal olarak raporlanır. Request IDs, outstanding map ve bounded timeout kullanılır; geç kalan bir response yeni request'i tamamlayamaz. Credentials sadece local config/environment veya caller injection ile alınır; test fixture/log/journal içine yazılmaz.

Heartbeat interval en az 10 saniye; `test_request` gelince `public/test` gönderilir. WS ping/pong bu application heartbeat'in yerine sayılmaz. İlk snapshot tüm yayımlanan fiyat seviyelerini başlatır. Sonraki `new/change/delete` bildirimlerinde `prev_change_id` önceki `change_id` ile eşleşmelidir. Gap'te ilgili symbol yayınlanması durur; unsubscribe/resubscribe sonrası yeni snapshot alınmadan delta uygulanmaz. Reconnect tüm eski sequence/book state'ini temizler.

## 6. Mevcut kodda uygulanacak noktalar

- `src/crocodile/core/ingest/transport.py`: frame metadata ve headers desteği; legacy bytes iteration uyumu; canlı disconnect sınıflandırması.
- `src/crocodile/core/connector.py`: capture hook, protocol decode hook, kontrol yanıtı işleme ve capture failure'ın parse/DLQ dışında yayılması.
- Yeni `src/crocodile/core/capture/` birimi: raw schema, writer, reader, manifest ve offline replay adapters. Mevcut canonical union/Parquet storage ile ayrı kalır.
- `src/crocodile/crypto/exchanges/binance/`: SBE decoder/connector seçimi, snapshot-provider injection, source registry kimliği ve Spot sync düzeltmesi.
- `src/crocodile/crypto/exchanges/deribit/connector.py`: interval/auth/control/heartbeat/sequence lifecycle.
- `src/crocodile/crypto/client/collect.py` ve ilgili CLI entrypoint: opt-in capture/config kurulumu; cancel/close sırasında journal'ın lifecycle'ı.
- FlowMap bağımlılık/config entegrasyonu daha sonra aynı source capability üzerinden yapılır; ilk kütüphane paketinin testleri native uygulama performansı kanıtı sayılmaz.

## 7. Kabul ve test sözleşmesi

1. Raw byte-for-byte round trip: JSON, malformed market JSON, TEXT/BINARY, non-UTF8 binary, empty payload; original UTC/monotonic metadata ve clock rollback dahil sıra eşitliği.
2. Writer: kısa write, disk/full queue/fsync failure, cancellation ve rotation. Her tail-byte truncation'da yalnız tamamlanmış prefix kurtarılır; sealed/middle CRC hatası açık failure'dır.
3. Offline snapshot+delta replay: HTTP/WS network calls testte yasakken canlı ingest ile aynı canonical output ve kapsam içindeki final book. Registry ve initial/resync anchors kullanılır.
4. SBE dört template; positive/negative exponent; zero quantity; çoklu trade; empty side; UTF8 symbol; her header/root/group/symbol truncation; extension bytes ve unsupported version.
5. Binance JSON/SBE aynı Spot kimliği; Spot/USDM ayrımı; snapshot stale/overlap/duplicate/gap; REST sırasında reader/capture ilerlemesi; reconnect ve shutdown.
6. Deribit auth-before-subscribe, request correlation, reject/timeout, token refresh, fresh auth on reconnect, heartbeat test_request, sequence gap'ten sonra yeni snapshot şartı.
7. Credentials/token örneklerinin journal, manifest ve log çıktısında bulunmaması.
8. Mevcut JSON connectors, FakeTransport bytes API, text subscription gönderimi, normalized Parquet/replay ve collect cancellation regresyonları.
9. Deterministik yüksek oranlı replay benchmark: received/queued/written/fsynced sayıları, durable lag, queue peak, decoder throughput ve kaynak precision. Duplicate/heartbeat yeni depth event sayılmaz.
10. Canlı yetkili kanal doğrulaması yalnız hazır local credentials ile yapılır. Credentials yoksa fixture testlerinin gerçek erişim/Hz ölçümü olmadığı açık raporlanır. Aktif ve sakin sembol davranışı ayrılır; API hızını sağlayamadığımızda repeated state üretilip yeni olay sayılmaz.

İlgili mevcut test başlangıçları: `tests/ingest/test_transport.py`, `tests/exchanges/test_base_run_loop.py`, `tests/exchanges/binance/test_connector.py`, `tests/exchanges/deribit/test_connector.py`, `tests/client/test_collect.py`, `tests/client/test_replay.py`, `tests/store/test_parquet_sink.py`. Yeni capture ve SBE doğruluk fixture'ları bağımsız kaynak layout/reference ile hazırlanır; encoder/decoder'ın aynı hatayı paylaşması tek başına geçer test oluşturamaz.

## 8. Kaynaklar ve geçiş durumu

[Binance SBE erişimi](https://developers.binance.com/docs/binance-spot-api-docs/sbe-market-data-streams), [resmî stream XML](https://github.com/binance/binance-spot-api-docs/blob/master/sbe/schemas/stream_1_0.xml), [Spot local-book sözleşmesi](https://developers.binance.com/en/docs/products/spot/web-socket-streams), [Deribit raw book](https://docs.deribit.com/subscriptions/orderbook/bookinstrument_nameinterval), [auth](https://docs.deribit.com/api-reference/authentication/public-auth), [connection lifecycle](https://docs.deribit.com/articles/connection-management-best-practices), [application heartbeat](https://docs.deribit.com/api-reference/session-management/public-set_heartbeat).

Self-review: kapsam dört birime ayrıldı; source identity ve units tutarlı; raw bytes canonical şemadan ayrıldı; REST anchors ve auth exclusion netleştirildi; durability window, overflow ve corruption davranışı tanımlandı; native FPS ve fixture doğrulaması ayrıldı. Placeholder bulunmuyor.

Sonraki süreç adımı, brainstorming becerisinin istediği `writing-plans` becerisiyle bu sözleşmeden dosya/test bazında uygulama planı üretmektir. Yerel kurulumda bulunmayan becerinin [Superpowers kaynak deposundaki dosyası](https://github.com/obra/superpowers/blob/main/skills/writing-plans/SKILL.md) doğrulandı ve planlama için kullanılıyor. Bu belge bir tasarımdır; uygulama kodu yazıldığı veya canlı veri hızının ölçüldüğü anlamına gelmez.
