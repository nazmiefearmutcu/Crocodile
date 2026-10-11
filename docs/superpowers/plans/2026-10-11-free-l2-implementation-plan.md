# Crocodile ücretsiz L2 ilk paket uygulama planı

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gelen gerçek piyasa verisini kaybetmeden saklayan offline replay kurmak; Binance Spot SBE ve Deribit raw erişimini açık seçimle eklemek.

**Architecture:** Ortak ham journal, protokol decoder'ından ve normalized Parquet'ten önce veri alır. Snapshot ve uygulama kararları ayrıca kaydedilir. Hızlı connector'lar aynı journal/replay sözleşmesini kullanır, ayrı piyasalardaki defterler birleşmez.

**Tech Stack:** Python 3.12+, asyncio, aiohttp, msgspec, mevcut canonical Record/Sink/InstrumentRegistry; binary framing, CRC32, os.fsync; pytest/pytest-asyncio.

**Spec:** [Onaylanan tasarım](../specs/2026-10-11-free-l2-capture-design.md). Kaynak repo karşılığı `docs/superpowers/specs/2026-10-11-free-l2-capture-design.md`.

## Kapsam ve bağımlılık sırası

Bu paket üç ayrı uygulanabilir plana bölündü. İlk plan JSON kaynaklarda bile kullanılabilir bir kayıt/replay ürünü verir. İkinci ve üçüncü plan onun frame/control/lifecycle arayüzünü tüketir; ortak dosyalara paralel değişiklik yapılmaz.

1. [Ham kayıt ve offline replay](2026-10-11-raw-capture-plan.md): transport zarfı, journal/recovery, routing, snapshot/karar kayıtları, replay ve opt-in collect entegrasyonu.
2. [Binance Spot SBE](2026-10-11-binance-sbe-plan.md): bağımsız wire decoder, Spot sequence doğruluğu, snapshot boyunca devam eden okuma, config/factory ve replay adapter.
3. [Deribit raw](2026-10-11-deribit-raw-plan.md): auth/control/heartbeat yaşam döngüsü, snapshot/delta zinciri, config/factory ve replay adapter.

Her planın adımları kendi test döngüsü ve küçük commit'iyle tamamlanır. Bir plan geçmeden bağımlı plan başlamaz. SBE ile Deribit aynı ortak sözleşmeyi kullanır; son entegrasyon testleri birlikte yapılır.

## Global Constraints

- `requires-python = ">=3.12"`; yeni temel bağımlılık eklenmez.
- Capture opt-in'dir; etkinleştirildiğinde required-capture davranışı kullanır.
- Mevcut canonical `Record` ve normalized Parquet replay API'si korunur.
- `binance-spot:BTCUSDT` aynı instrument olarak kalır. SBE taşıma biçimi session metadata'sıdır.
- Writer kuyruğu başlangıçta 32 MiB byte bütçelidir; her kayıt uzunluğu sınırı 16 MiB'dir.
- Segment hedefi 256 MiB; kayıt segment sınırında bölünmez.
- Fsync aralığı varsayılan en fazla 1 saniyedir; sınır sağlayamayan writer durable-lag ihlali bildirir.
- Kuyruğa alınma durability değildir. Donanım write-cache/power-loss için evrensel garanti verilmez.
- Credentials/token journal, manifest veya log'a yazılmaz. Yetkili canlı ölçüm için mevcut yerel credentials gerekir.
- Deribit interval varsayılan `100ms`, opt-in `raw`; Binance wire format varsayılan `json`, opt-in Spot `sbe`.
- Yeni gerçek kaynak olaylarının hızı, yerel hesaplama ve ekran yenileme hızından ayrı ölçülür.

## Review Focus

1. UTC saati geri gitmesi ve reconnect'te receive ordinal reseti: global journal ordinal bozulmamalı; raw plan görev 1/2/5.
2. Disk yazma/fsync başarısızken collect'in connector isolation davranışı: required capture CLI'ya başarılı bitiş diye dönmemeli; raw plan görev 2/6.
3. REST yavaşken WS okuma ve Deribit auth beklenirken kontrol ack'i: ayrı routing görevleri veri kaybetmeden/deadlock olmadan ilerlemeli; raw plan görev 3/4, SBE görev 2, Deribit görev 2.
4. SBE top-20 snapshot ile geniş defter kapsamının karışması; aynı isimli Spot/futures sembolleri: SBE görev 2/3.
5. Bozuk Deribit auth yanıtı ve geç kalan refresh response: secret sızıntısı ve yanlış request tamamlanması olmamalı; raw plan görev 3, Deribit görev 2.

## Kaynak checkout ve komutlar

Kaynak: `C:\Users\Kullanıcı\.gemini\antigravity\scratch\crocodile`. Uygulama kurulu site-packages veya uv cache üzerinde yapılmaz. Başlangıç source HEAD `9e6122e5888ec0e74ea72a1a46d02bccf17f5daa`; tasarımın ilk kaydı `956df5e`. Remote'a push bu planın parçası değildir.

PowerShell komutları kaynak repo cwd'sinde çalışır. Gerçek yerel runtime `.\.venv\Scripts\python.exe`, sürüm 3.12.10. Import'un bu checkout'a gittiği kontrol edildi. 57 mevcut ilgili test `--collect-only` ile başarıyla toplandı; bu, yeni davranışların test edildiği anlamına gelmez.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ingest/test_transport.py tests/exchanges/test_base_run_loop.py tests/client/test_collect.py tests/exchanges/binance/test_connector.py tests/exchanges/binance/test_book_sync.py tests/exchanges/deribit/test_connector.py
```

Uygulama başlangıcında aynı seçim bir kez baseline olarak çalıştırılır; önceden var olan hata varsa yeni davranışın sonucu ile karıştırılmaz. Sonraki testler her görevin sahip olduğu değişiklikleri doğrular; yalnız yeni hata/değişiklik gerekçesiyle tekrar genişletilir.

## Son entegrasyon ve kabul

- [ ] Üç alt planın focused testleri ve ortak connector/collect regresyonları yeşil olmalı.
- [ ] Bir capture fixture'ı tamamen ağsız replay edilerek canonical sıra/bytes ve kapsam içindeki son defter eşitliği kanıtlanmalı.
- [ ] Queue overflow, short write ve fsync fault'ta CLI nonzero failure; SIGINT'te drain/close ve cancellation semantiği doğrulanmalı.
- [ ] `Settings` repr, günlükler, journal/manifest ve DLQ raporu örnek credentials/token içermemeli.
- [ ] Aktif ve sakin sembol ölçümleri gerçek source event, receive/write/fsync sayıları ve gap sayısını ayrı raporlamalı. Credentials yoksa canlı SBE/raw sonucu ölçülmedi diye belirtilmeli.
- [ ] Değişen yeni core modülleri ruff ve strict mypy'dan geçmeli; mevcut crypto override'ına yeni modül ekleyerek hatalar gizlenmemeli.
- [ ] İşlevsel planlar geçtikten sonra tek bağımsız bütün değişiklik incelemesi; source-only test sonucu FlowMap native FPS kanıtı sayılmamalı.

FlowMap history overview piramidi, eski coarse verinin tahmin modeli, diğer borsalar ve büyük arşiv indirme bu ilk pakette uygulanmaz. Ayrıntılı ücretsiz kaynaklar ve matematiksel devam yolu [araştırma raporunda](../research/crocodile-ucretsiz-l2-arastirmasi-2026-10-11.md) bulunur.

## Süreç ve durum

Onaylanan ilk paket tasarımı kaynak checkout'a kaydedildi. Plan, [upstream writing-plans becerisi](https://github.com/obra/superpowers/blob/main/skills/writing-plans/SKILL.md) kullanılarak tamamlandı; yerel eksik beceri engeli bu kaynakla çözüldü. Henüz uygulama kodu yazılmadı veya yeni protokol/capture testleri çalıştırılmadı. Üç alt plan self-review'dan geçti: 15 test edilebilir görev, shared interfaces/fixture ownership, snapshot/sequence/control/cleanup koşulları netleştirildi. Plan/spec incelemesinin ardından uygulama başlar. Görev başına bağımsız incelemeyle aynı oturumda yürütme önerilir; ortak transport/capture/book arayüzleri nedeniyle bir görevin hatası diğer akışlara taşınabilir.
