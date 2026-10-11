# Ham kayıt ve offline replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** JSON market frame'lerini, snapshot'ları ve uygulanma sırasını saklayan; kaydı ağsız tekrar çalıştıran required-capture katmanı.

**Architecture:** Tek socket reader kontrol yanıtlarını doğrudan route eder, market frame'lerini normalization öncesinde byte bütçeli journal'a kabul eder ve ayrı processor kuyruğuna verir. REST response ve apply kararları aynı journal'a girer. Offline decoder'lar yalnız önceden kaydedilmiş referansları çözer.

**Tech Stack:** Python 3.12, asyncio, aiohttp, msgspec, pathlib, struct, zlib.crc32, os.fsync, pytest.

**Spec:** `docs/superpowers/specs/2026-10-11-free-l2-capture-design.md`; çıktı kopyası [tasarım](../specs/2026-10-11-free-l2-capture-design.md). Ortak kısıtlar [ana planda](2026-10-11-free-l2-implementation-plan.md).

## Global Constraints

32 MiB encoded queue/inflight bütçesi; 16 MiB record sınırı; 256 MiB segment; fsync default 1.0 s. Canonical `Record` union'ı ve normalized Parquet replay API'si değişmez. Credentials/control token'ları ham journal'a giremez. Fixture'ın eksik receive metadata'sı gerçek ağ zamanı gibi doldurulamaz. Capture failure sticky/fatal'dır, reconnect veya DLQ ile yutulmaz.

## Review Focus

Clock rollback/connection değişimi görev 1/2/6; allocation öncesi length doğrulaması görev 2; idle writer failure ve fsync timer görev 3/4; bozuk auth JSON görev 4; REST/WS apply yarışı ve collect cleanup failure görev 5/7 testlerindedir.

## Dosya ve ortak arayüz haritası

Yeni `src/crocodile/core/capture/` modülleri: `schema.py` typed olaylar, `format.py` byte framing, `manifest.py` segment bağlamı, `reader.py` doğrulama/index, `writer.py` durability, `classification.py` güvenli routing, `snapshots.py` HTTP/recorded provider, `replay.py` dispatcher. `__init__.py` public import'ları sunar. Bu ayrımların hiçbiri canonical store'un formatını değiştirmez.

`schema.py` exact public types (frozen, keyword-only msgspec structs):

```text
RawFrame(payload: bytes, frame_kind: Literal["text", "binary", "unknown"],
         received_utc_ns: int | None, received_monotonic_ns: int | None,
         connection_id: str | None, receive_ordinal: int | None)
StreamContext(stream_id: str, source: str, market: str,
              symbols: tuple[str, ...], endpoint_id: str,
              decoder_id: str, decoder_version: int,
              schema_id: int | None, schema_version: int | None)
CaptureContext(capture_id: str, streams: tuple[StreamContext, ...])
MarketFrame(stream_id: str, frame: RawFrame,
            observed_utc_ns: int, observed_monotonic_ns: int)
SnapshotRequest(request_id: str, stream_id: str, source: str, symbol_raw: str,
                path: str, params: dict[str, str | int])
SnapshotResponse(request_id: str, status: int, payload: bytes,
                 received_utc_ns: int, received_monotonic_ns: int)
RegistryCheckpoint(instruments: tuple[Instrument, ...])
ApplicationDecision(origin_ordinal: int, record_indices: tuple[int, ...],
                    action: Literal["apply", "drop", "snapshot", "gap"], reason: str)
ControlMetadata(stream_id: str, kind: str, connection_id: str | None, request_id: str | None,
                status: str, observed_utc_ns: int)
JournalRecord(ordinal: int, event: CaptureEvent)
```

`CaptureEvent` MarketFrame, SnapshotRequest, SnapshotResponse, RegistryCheckpoint, ApplicationDecision ve ControlMetadata tagged union'ıdır. `RegistryCheckpoint` mevcut public `Instrument` alanlarını saklar. `StreamContext.endpoint_id` whitelist adı olmalıdır; secret/query URL kaydedilmez. Her producer context.streams içindeki stream_id ile kaydolur; aynı writer birden çok source kabul edebilir. `ControlMetadata.kind/status` ve decision reason seçilmiş sabit enum mesajlarıdır; serbest exchange error text taşınmaz.

`CaptureStats` fields: received_frames, queued_records, written_records, fsynced_records, queued_ordinal, written_ordinal, fsynced_ordinal, queued_bytes, peak_queued_bytes, durable_lag_ns, failed. `CaptureSummary` context+stats+sealed_segment_count; `RecoveryReport` recovered_records+discarded_tail_bytes; `ReplaySummary` emitted_records+last_ordinal. Başlangıç ordinal 0; ilk kabul 1. Sayaçlar heartbeat/drop ile yeni depth event sayısını karıştırmaz.

## Görev 1: Receive zarfı ve transport uyumluluğu

**Files:** Create `core/capture/schema.py`, `core/capture/__init__.py`; modify `src/crocodile/core/ingest/transport.py`; test `tests/ingest/test_transport.py`, new `tests/capture/test_schema.py`.

**Interfaces:** `Transport.__aiter__() -> AsyncIterator[bytes]` korunur. Yeni `AiohttpWsTransport.iter_frames() -> AsyncIterator[RawFrame]`; `iter_transport_frames(transport: Transport) -> AsyncIterator[RawFrame]`; constructor'a keyword-only `headers: Mapping[str, str] | None = None`. Headers None iken mevcut ws_connect test çağrısı korunur. Aynı socket aynı anda iki iterator ile okunamaz.

- [ ] **Test yaz:** `test_iter_frames_preserves_text_binary_and_receive_metadata`: TEXT UTF8 bytes ve BINARY bytes eşit, iki receive ordinal 1/2, aynı connection_id, receive clock mock değerleri eşit. `test_legacy_bytes_transport_does_not_fabricate_receive_metadata`: tür unknown, receive fields None. `test_live_close_is_not_fixture_eof`: live iterator `TransientConnectorError`, FakeTransport boş iterator normal biter. Mevcut send_str testleri korunur.

```python
async def test_legacy_bytes_transport_does_not_fabricate_receive_metadata():
    frame = await anext(iter_transport_frames(FakeTransport([b"x"])))
    assert frame.payload == b"x"
    assert frame.frame_kind == "unknown"
    assert frame.received_utc_ns is None
    assert frame.received_monotonic_ns is None
```
- [ ] **RED:** `.\.venv\Scripts\python.exe -m pytest tests/ingest/test_transport.py tests/capture/test_schema.py`; yeni API yokken import/attribute failure bekle.
- [ ] **Implement:** Zarf üretimi ws message alındığı anda; reconnect yeni connection_id/ordinal, monotonic clock UTC'den bağımsız. RawFrame ve public event/summary structs yukarıdaki exact alanlarla tanımla. Frame adapter yalnız gözlemlenen metadata'yı verir.
- [ ] **GREEN:** Aynı komut; tüm testler PASS.
- [ ] **Commit:** Yalnız görev dosyalarını stage ederek `feat: preserve inbound frame metadata`.

## Görev 2: Format, manifest ve doğrulayan reader

**Files:** Create `core/capture/{format,manifest,reader}.py`; modify `core/errors.py`; create `tests/capture/{test_format,test_reader}.py`.

**Interfaces:** `encode_record(record: JournalRecord, *, max_record_bytes: int = 16 << 20) -> bytes`; `decode_record(data: bytes, *, max_record_bytes: int = 16 << 20) -> JournalRecord`. `CaptureReader(root: Path, *, recover_active_tail: bool = False)`; `records() -> Iterator[JournalRecord]`; `read_record(ordinal: int) -> JournalRecord`; `recovery_report() -> RecoveryReport`. `CaptureIntegrityError(StoreError)`; `CaptureError(SinkError)` ileride writer/live failure için.

- [ ] **Test yaz:** JSON, malformed market bytes, empty payload ve non-UTF8 binary round trip bytes/fields tam eşit; UTC geri giden ikinci kayıt ordinal 2 kalır. `test_every_tail_byte_truncation_recovers_complete_prefix` her prefix için yalnız tam records verir, final partial'daki eksik bytes raporlanır. `test_middle_crc_corruption_fails`, `test_sealed_truncation_fails`, `test_manifest_segment_mismatch_fails` typed error assert eder. `test_record_length_is_checked_before_allocation` 16 MiB+1 prefix'inde payload read/allocation çağrılmadığını assert eder.

```python
def test_raw_bytes_and_metadata_round_trip():
    frame = RawFrame(payload=b"\xff\x00", frame_kind="binary",
                     received_utc_ns=100, received_monotonic_ns=7,
                     connection_id="c1", receive_ordinal=1)
    rec = JournalRecord(ordinal=1, event=MarketFrame(
        stream_id="s1", frame=frame, observed_utc_ns=101, observed_monotonic_ns=8))
    assert decode_record(encode_record(rec)) == rec
```
- [ ] **RED:** `.\.venv\Scripts\python.exe -m pytest tests/capture/test_format.py tests/capture/test_reader.py`; yeni modül import failure.
- [ ] **Implement:** File magic `CRCLRAW\0`, uint16 little-endian format version 1. Record uint32 length + MsgPack JournalRecord + uint32 CRC32; CRC MsgPack bytes üzerindedir, length body'nin boyutudur. Manifest version/context, sealed segment byte/ordinal bounds, SHA256 ve durable watermark taşır. Record ordinal'leri ardışık; sealed dosyanın tamamı doğrulanır. Reader read-only'dir; recovery dosya kesmez, yalnız son active partial'ın incomplete tail'ini raporlar. CRC hatalı tam kaydı tail diye gizlemez. Ordinal→segment offset index payload cache büyütmez.
- [ ] **GREEN:** Aynı komut PASS; version/oversize/CRC/missing segment ayrı error testleri.
- [ ] **Commit:** `feat: add validated raw journal format and reader`.

## Görev 3: Writer, byte sınırı ve durability

**Files:** Create `core/capture/writer.py`; create `tests/capture/test_writer.py`, `tests/capture/conftest.py`.

**Interfaces:**

```python
class CaptureWriter:
    @classmethod
    async def create(cls, root: Path, context: CaptureContext, *,
                     queue_bytes: int = 32 << 20,
                     segment_bytes: int = 256 << 20,
                     max_record_bytes: int = 16 << 20,
                     fsync_interval_s: float = 1.0) -> CaptureWriter: ...
    async def append(self, event: CaptureEvent) -> int: ...
    async def flush(self) -> int: ...
    async def close(self) -> CaptureSummary: ...
    def stats(self) -> CaptureStats: ...
    async def wait_failure(self) -> NoReturn: ...
```

- [ ] **Test yaz:** append ordinal 1/2, flush watermark 2; queue budget encoded framing/envelope ve inflight bytes'ı kapsar. Overflow'da sonraki append/flush/close da CaptureError üretir. Short write, disk full ve fsync mock failure watermark ilerletmez; wait_failure uyanır. Idle fsync mocked monotonic 1.0 s tick'te ilerler. 256 MiB rotation küçük test budget ile record bölünmeden kanıtlanır. Cancelled close drain tamamlar veya açık capture failure bildirir; başarısız cleanup başarı sayılmaz.

`tests/capture/conftest.py` içine `capture_context` fixture'ı ekle: UUID sentinel, tek s1/binance-spot stream, BTCUSDT, public endpoint adı, binance-json decoder version1; secrets yoktur.

```python
async def test_flush_advances_only_durable_watermark(tmp_path, capture_context):
    writer = await CaptureWriter.create(tmp_path, capture_context)
    assert await writer.append(RegistryCheckpoint(instruments=())) == 1
    assert await writer.flush() == 1
    assert writer.stats().fsynced_ordinal == 1
    assert (await writer.close()).stats.fsynced_records == 1
```
- [ ] **RED:** `.\.venv\Scripts\python.exe -m pytest tests/capture/test_writer.py`; missing writer/API failure.
- [ ] **Implement:** Tek writer task ordinal ve manifest yönetir. Disk I/O tek serialized worker ile event loop dışındadır. Queue admission bekleyip veri yutmaz; overflow sticky failure. Testte kısa write progress denetlenir, zero-progress/failure açık hata; başarılı partial writes tüm record tamamlanana kadar written sayılmaz. Flush+fsync timer idle'da da çalışır; durable lag ihlali raporlanır. close idempotent, seal drain/fsync ardından atomik replace; manifest uyumu recovery'de kontrol edilir.
- [ ] **GREEN:** Aynı komut PASS; timer tests gerçek 1 s sleep kullanmaz, fake clock/event ile deterministik.
- [ ] **Commit:** `feat: add bounded durable raw capture writer`.

## Görev 4: Reader/control router ve ayrı market processor

**Files:** Modify `core/connector.py`; create `core/capture/classification.py`, `core/capture/emitter.py`; create `tests/ingest/test_capture_runloop.py`.

**Interfaces:** Connector'a opt-in `capture: CaptureWriter | None` property; `_decode_frame(frame: RawFrame) -> object` default JSON; `_handle_control_message(msg: object, frame: RawFrame) -> Awaitable[bool]` default False; `_classify_frame(frame: RawFrame, msg: object | None) -> Literal["market", "control", "unknown"]`. `_handle_message` mevcut API uyumluluğu korunur. Yeni `ProcessedInput(origin_ordinal: int | None, frame: RawFrame, decoded: object, local_ts: int)` internal zarfıdır. Reader controller completion için market processor'ı beklemez.

`CaptureEmitter(writer: CaptureWriter, out: Sink)`; `async emit(decision: ApplicationDecision, records: Sequence[Record]) -> None`. Her capture'ın tek emitter'ı ordered emission lock'u paylaşır; decision append ve ilgili canonical batch sink'e verme tek sıralı işlem olur. Generic JSON normalizer emission'ları da ApplicationDecision taşır; yalnız Binance book callbacks ile sınırlı değildir. Sink delivery hatası canonical materialization'dan ayrı başarısızlıktır; replay source'tan yeniden materialize edilen canonical event'leri doğrular, dış sink'e başarılı delivery iddiası yapmaz.

- [ ] **Test yaz:** normalize/sink henüz beklerken journal market frame'i içerir; sonraki control ack processor kuyruğunda takılmaz. CaptureError DLQ'ya yazılmaz/reconnect edilmez. Idle socket sırasında writer failure session'ı durdurur. Authentication/refresh token örnekleri journal, caplog ve DLQ'da yoktur. Bilinmeyen/malformed Deribit mixed-control bytes yerine metadata bulunur; Binance market-only malformed bytes birebir saklanır. Processor queue byte overflow açık failure olur, drop-oldest yoktur.

Test-local `runloop_harness` fixture'ı scripted transport, blocking normalizer, capture reader ve failure injector sunar; `run(fail_capture: bool=False)` connector.run(max_reconnects=2)'yi sürer, `connect_count` ve `dlq_records` izlenir.

```python
async def test_capture_failure_is_not_dead_lettered_or_reconnected(runloop_harness):
    with pytest.raises(CaptureError):
        await runloop_harness.run(fail_capture=True)
    assert runloop_harness.connect_count == 1
    assert runloop_harness.dlq_records == []
```
- [ ] **RED:** `.\.venv\Scripts\python.exe -m pytest tests/ingest/test_capture_runloop.py tests/exchanges/test_base_run_loop.py`; capture/routing assertions FAIL.
- [ ] **Implement:** connect/subscription/reader/processor/watchdog lifecycle tek TaskGroup sahibinde. JSON için yalnız control classification'ın ihtiyaç duyduğu parse önce; market raw append canonical decode/normalization'dan önce. Dar sınıflandırma bilinmeyen payload'ı loglamaz. Decode errors mevcut DLQ davranışını sürdürür, control secret errors DLQ dışında sanitized kalır. Writer failure ve processor fatal error birbirini iptal eder; reconnect yeni connection context/book reset ister. Processor queue için ayrıca 32 MiB encoded input bütçesi; duraklayan sink sonsuz RAM oluşturamaz.
- [ ] **GREEN:** Aynı komut PASS; cancel ve fixture EOF eski semantiklerini korur.
- [ ] **Commit:** `feat: route control and capture before market processing`.

## Görev 5: Snapshot anchor ve Binance JSON sequence oturumu

**Files:** Create `core/capture/snapshots.py`, `crypto/exchanges/binance/book_session.py`; modify `binance/connector.py`, `core/ingest/book_sync.py`; create `tests/exchanges/binance/{test_capture_snapshots,test_book_session}.py`; existing `tests/exchanges/binance/test_book_sync.py`.

**Interfaces:** `SnapshotProvider.fetch(request: SnapshotRequest) -> Awaitable[CapturedSnapshot]`; `HttpSnapshotProvider(session: aiohttp.ClientSession, capture: CaptureWriter | None, *, base_url: str)` ve `RecordedSnapshotProvider(reader: CaptureReader)` aynı arayüzü uygular. base_url connector'ın seçtiği public REST base adresidir; request.path bununla birleştirilir. `SnapshotRequest.request_id` unique capture-scoped ID; response origin journal ordinal'i `CapturedSnapshot(response: SnapshotResponse, origin_ordinal: int | None)` ile taşınır. CapturedSnapshot frozen struct `snapshots.py` içinde tanımlanır.

`BinanceDepthEvent(symbol_raw: str, source_ts_ns: int | None, U: int, u: int, pu: int | None, bids: tuple[tuple[float, float], ...], asks: tuple[tuple[float, float], ...])`. `BinanceBookSession(venue: Literal["spot", "futures"], snapshot_provider: SnapshotProvider, out: Sink, registry: InstrumentRegistry, capture: CaptureWriter | None)`; `async accept_depth(event: BinanceDepthEvent, *, origin_ordinal: int | None, local_ts: int) -> None`; `async accept_snapshot(snapshot: CapturedSnapshot) -> None`; `async reset(reason: str) -> None`. JSON ve ileride SBE aynı engine'i kullanır.

Pure `BinanceBookState.accept_snapshot(snapshot: BookSnapshot) -> tuple[Record, ...]`; `accept_depth(event: BinanceDepthEvent, *, local_ts: int) -> tuple[SyncResult, tuple[Record, ...]]`. Aynı dosyadaki bu engine network/sink içermez; live session wrapper ve offline ReplayDecoder ortak engine'i tüketir.

- [ ] **Test yaz:** HTTP response özgün bytes ve receive clocks kayıtlı; request/response/apply ref ayrı ordinal. Registry checkpoint ilk normalize'dan önce. REST future açıkken iki WS frame capture edilir. Spot anchor 100 sonrası U=99,u=101 APPLY, duplicate u=101 DROP, U=102,u=103 APPLY, U=105 gap. Resync buffer içindeki gap emit edilmez; her event yeniden feed edilir. Missing snapshot ID failure; futures anchor/subsequent pu kuralları mevcut fixture testleriyle değişmez.

```python
def test_overlapping_spot_range_and_duplicate():
    sync = OrderBookSync(venue="spot")
    sync.set_snapshot(last_update_id=100)
    assert sync.feed(U=99, u=101, pu=None) == SyncResult.APPLY
    assert sync.feed(U=99, u=101, pu=None) == SyncResult.DROP
    assert sync.feed(U=100, u=103, pu=None) == SyncResult.APPLY
    assert sync.feed(U=105, u=106, pu=None) == SyncResult.RESYNC
```
- [ ] **RED:** `.\.venv\Scripts\python.exe -m pytest tests/exchanges/binance/test_capture_snapshots.py tests/exchanges/binance/test_book_session.py tests/exchanges/binance/test_book_sync.py`; missing provider/session veya eski overlap test failure.
- [ ] **Implement:** HTTP body parse edilmeden capture, sanitized request params yalnız public depth request; registry checkpoint açık instrument listesiyle alınır. Binance bootstrap/resync tek symbol task, market input FIFO buffer; 32 MiB session buffer sınırı overflow fail-stop. Shared Spot sync stale/overlap/gap kuralları spec'e göre düzeltilir; futures pu değişmez. Binance bu yeni session yolunu kullanır; diğer venue'lerin shared bridge refactor'ı yapılmaz. Her apply/drop/snapshot/gap kararı raw origin ref ve canonical emission index'leriyle kaydedilir. Replay adapter aynı pure sequence engine'i kullanabilir; live REST scheduling ayrı wrapper'dır.
- [ ] **GREEN:** Aynı komut ve `.\.venv\Scripts\python.exe -m pytest tests/ingest/test_gap_bridge.py tests/exchanges/binance/test_book_resync_bridge.py tests/equity/test_ingest.py`; legacy bridge ve equity shared sync regressions PASS.
- [ ] **Commit:** `fix: preserve snapshot anchors and validate buffered depth ranges`.

## Görev 6: Deterministik ağsız replay

**Files:** Create `core/capture/replay.py`, `crypto/exchanges/binance/replay.py`; create `tests/capture/test_replay.py`; modify `tests/conftest.py` ile yalnız opt-in `network_forbidden` fixture; `tests/capture/conftest.py` içine recorded_json_session fixture ekle.

**Interfaces:** `ReplayDecoder.apply(decision: ApplicationDecision, origin: JournalRecord, registry: InstrumentRegistry) -> Iterable[Record]`; `RawReplay(reader: CaptureReader, decoders: Mapping[str, ReplayDecoder])`; `records() -> Iterator[Record]`; `async run(out: Sink, *, pacing: Literal["fast", "original"] = "fast") -> ReplaySummary`. Registry checkpoint ve sanitized lifecycle/gap metadata ordinal sırasında decoder'a `observe(event: CaptureEvent) -> None` ile iletilir. JSON decoder ID `binance-json`, version 1; diğer pure JSON normalizer'lar explicit registered decoder ID gerektirir, otomatik ağ connector'ı oluşturulmaz.

`CaptureReader.context` CaptureContext döndürür; dispatcher origin'in stream_id'sini context.streams ile eşleştirir ve decoder_id/version kontrolünden sonra uygun decoder instance'ını seçer. Aynı decoder türünün farklı stream'leri ayrı state instance'larıdır; book state paylaşılmaz.

- [ ] **Test yaz:** live JSON fixture canonical output serialized bytes ve final book eşit; UTC rollback sıra değiştirmez. HTTP/WS constructor/request calls monkeypatch ile forbidden. Missing anchor, future/missing origin, unsupported decoder version typed CaptureIntegrityError. Original pacing monotonik farkı kullanır; reconnect monotonic metadata olmayan frame'lerde pacing unavailable açık durumdur, uydurulmuş receive-clock yoktur.

`recorded_json_session` fixture'ı canlı scripted snapshot+WS+clock rollback run'ından reader, registered replay decoder mapping ve saved expected Record listesi döndürür. Network constructors fixture tamamlandıktan sonra `network_forbidden` fixture'ı tarafından AssertionError üretecek şekilde patch edilir.

```python
def test_replay_matches_live_canonical_records(recorded_json_session, network_forbidden):
    reader, decoders, expected = recorded_json_session
    actual = list(RawReplay(reader, decoders).records())
    assert [msgspec.msgpack.encode(r) for r in actual] == [
        msgspec.msgpack.encode(r) for r in expected]
```
- [ ] **RED:** `.\.venv\Scripts\python.exe -m pytest tests/capture/test_replay.py`; missing replay API failure.
- [ ] **Implement:** Journal ordinal yürü; karar önceki raw origin'i reader index ile çözer. Decoder action/indices ve sequence'ı yeniden doğrular; uygulanmış state'leri gelecekteki anchor ile onarmaz. ReplayDecoder kayıtlı registry/source precision bilgisiyle pure materialization yapar. Pacing execute-time'ı ayrı alandır; kaynak/receive timestamp'ları değiştirilmez. Payload cache bounded; network disabled; captured control secrets olmadan book replay mümkündür.
- [ ] **GREEN:** Aynı komut ve mevcut `tests/client/test_replay.py` PASS.
- [ ] **Commit:** `feat: replay captured market data without network access`.

## Görev 7: Collect/config/CLI ve kullanıcı dokümanı

**Files:** Modify `crypto/client/collect.py`, `capabilities/ops.py`, `core/config.py`; create `tests/client/test_collect_capture.py`, `tests/surfaces/test_collect_capture_cli.py`; modify `README.md`. Gerekirse `surfaces/dispatch.py` mevcut CrocodileError mapping'i için dar değişiklik.

**Interfaces:** `collect(..., capture: CaptureWriter | None = None)` keyword-only. Settings'e `raw_capture: bool = False` / `CROCODILE_RAW_CAPTURE`, `raw_capture_queue_bytes: str = "33554432"` / `CROCODILE_RAW_CAPTURE_QUEUE_BYTES`; queue config int validation collect başlamadan. Public caller output path almaz: capture root `{settings.data_dir}/raw-capture/{capture_id}`. Fast source mode config'leri sonraki alt planların görevidir. Mevcut generated CLI `collect` route'u kullanılır; ayrı deprecated CLI eklenmez. RawReplay önce library API'dir; normalized `replay` komutu anlam değiştirmez.

- [ ] **Test yaz:** CaptureError cleanup sonrası tek CrocodileError olarak çıkar; ordinary connector error eskisi gibi isolated. Writer/close fsync failure başarılı collect sayılmaz. CLI failure exit 1 ve başarı summary'si yok. Capture cancellation drain/close sonrası CancelledError reraises. Invalid/zero/negative queue byte config network açmadan ConfigError. Capture off iken mevcut collect output/CLI schema değişmez.

Test-local `collect_capture_harness` ordinary/special failure inject eden fake connector, writer ve MemorySink içerir; run(failure) gerçek collect çağırır ve cleanup flags ölçer.

```python
async def test_collect_capture_failure_propagates_after_cleanup(collect_capture_harness):
    with pytest.raises(CaptureError):
        await collect_capture_harness.run(failure="fsync")
    assert collect_capture_harness.sink_closed
    assert collect_capture_harness.writer_closed
```
- [ ] **RED:** `.\.venv\Scripts\python.exe -m pytest tests/client/test_collect_capture.py tests/surfaces/test_collect_capture_cli.py tests/client/test_collect.py`; swallowed error/config assertions FAIL.
- [ ] **Implement:** `_run_isolated` yalnız CaptureError'ı dışarı bırakır. `except* Exception` safety net bu hatayı yutmaz; TaskGroup errors cleanup sonrası tek CaptureError'a map edilir. Shared writer failure bütün capture producers'ı durdurur; sink/writer ayrı cleanup hataları orijinal hatayı gizlemeden raporlanır. Close cancellation bounded cleanup watchdog 10 s; zaman aşımı açık CaptureError, başarılı sealing iddiası yok. README opt-in env, kayıt alanı/durability penceresi, library replay örneği ve secret exclusion anlatır.
- [ ] **GREEN:** Yukarıdaki seçim ve ana plandaki 57 mevcut test PASS. `.\.venv\Scripts\python.exe -m ruff check src/crocodile/core/capture tests/capture`; `.\.venv\Scripts\python.exe -m mypy src/crocodile/core/capture`. Dokümandaki library çağrısı fixture capture üzerinde smoke-run edilir.
- [ ] **Commit:** `feat: expose required raw capture in collect`.

## Self-review

Capture schema, queue/durability/recovery, safe control, snapshots/registry/application decisions, replay ve collect propagation görevlerle kapsandı. Canonical ve raw types ayrı. Provider dönüşü tüm görevlerde `CapturedSnapshot`; ham response metadata'sı `SnapshotResponse` içindedir. Yeni SBE/Deribit decoder'ları bu exact arayüzleri tüketir. Son canlı erişim ve native FlowMap performansı burada ölçülmez.
