# Binance Spot SBE Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Binance Spot 20 ms depth akışını binary SBE üzerinden opt-in almak, ham precision'ı saklamak ve aynı Spot defterini offline tekrar kurmak.

**Architecture:** Pure SBE decoder binary layout'u typed events'e çevirir. Canonical adaptör, raw capture planının Binance book session'ını ve registry kimliğini kullanır. JSON control aynı socket'te ayrı route edilir; SBE ayrı borsa kimliği olmaz.

**Tech Stack:** Python 3.12, struct, decimal, aiohttp, mevcut CaptureWriter/RawReplay ve pytest.

**Spec:** [Onaylanan tasarım](../specs/2026-10-11-free-l2-capture-design.md); repo `docs/superpowers/specs/2026-10-11-free-l2-capture-design.md`.

## Global Constraints

Ön koşul: [raw capture planı](2026-10-11-raw-capture-plan.md) yeşil. `market="spot"`, `wire_format="sbe"` explicit; JSON varsayılan. Ed25519 API key yalnız `X-MBX-APIKEY` header'ı; private key/trade izni yok. Source ve registry venue `binance-spot`; canonical `binance-spot:BTCUSDT`. Template/schema boyutları onaylı spec'ten aynen alınır. 20 ms nominal yayın aralığıdır, her sembolde 50 bağımsız değişim/s garantisi değildir. `@depth20` geniş defterin anchor'ı olmaz.

## Review Focus

Signed exponents/group block length/truncation görev 1; buyer-maker yönü ve µs/ns görev 2; headers/config kaybı ve CCXT fallback görev 3; shutdown/unknown depth template/clock rollback görev 4 testleriyle kapsanır.

## Dosya haritası ve dependency

Yeni `binance/sbe.py` yalnız wire parsing; `binance/normalize_sbe.py` canonical/pure depth adaptörü. Mevcut `binance/connector.py` transport mode ve routing. Raw planın `binance/book_session.py`, `core/capture/schema.py`, `SnapshotProvider`, `ReplayDecoder` API'leri tekrar tanımlanmaz; shared Spot sync düzeltmesi raw plan görev 5'te bitmiş olmalıdır. `binance/replay.py` SBE decoder registration ekler. Test fixture'ları XML'den bağımsız okunabilir field/offset açıklaması taşır.

## Task 1: Dört template pure decoder

**Files:** Create `src/crocodile/crypto/exchanges/binance/sbe.py`, `tests/exchanges/binance/test_sbe_decoder.py`, `tests/exchanges/binance/conftest.py`, `tests/exchanges/binance/fixtures/sbe/` fixtures ve fixture `README.md`.

**Interfaces:** `decode_sbe_frame(payload: bytes) -> SbeMessage`; `SbeMessage = SbeTrades | SbeBestBidAsk | SbeDepthSnapshot | SbeDepthDiff`. Typed immutable event'ler symbol_raw/event_time_us taşır; price_exponent/qty_exponent signed int8; level fiyat/miktarı int64 mantissa olarak kalır. SbeTrades transact_time_us ve `trades: tuple[SbeTrade, ...]`; SbeTrade trade_id/price_mantissa/qty_mantissa/is_buyer_maker. BBO book_update_id, bid/ask mantissa fields; snapshot book_update_id+bids/asks; diff first_update_id/final_update_id+bids/asks. `SbeProtocolError(FatalConnectorError)`, `UnsupportedSbeTemplate(SbeProtocolError)` exact typed errors.

- [ ] **Test yaz:** Dört bağımsız fixture beklenen integer alanlarla eşit. Trades group `<HI>` count uint32/25-byte element; snapshot/diff grupları `<HH>`/16-byte. Header8 `<HHHH>`, root block lengths 18/50/18/26. UTF8 symbol length bytes sayar. Positive/negative exponent, empty side, qty zero, çoklu trade, root/group extension bytes, header/root/group/symbol her truncation prefix'i. Unknown schema/version/template typed failure. Group count büyükken allocation yapılmadan bounds failure.

Fixture `trades.bin`: symbol BTCUSDT, event_time_us=1700000000000000, transact_time_us=1700000000001000, exponents -2/-3, tek trade ID1/price3000012/qty1250/buyer-makerTrue. `sbe_fixture` test fixture fonksiyonu adı verilen fixture bytes'ı döndürür.

```python
def test_trades_10000_preserves_integer_precision(sbe_fixture):
    event = decode_sbe_frame(sbe_fixture("trades.bin"))
    assert event.symbol_raw == "BTCUSDT"
    assert event.price_exponent == -2
    assert event.qty_exponent == -3
    assert event.trades[0].price_mantissa == 3000012
    assert event.trades[0].qty_mantissa == 1250
```
- [ ] **RED:** `.\.venv\Scripts\python.exe -m pytest tests/exchanges/binance/test_sbe_decoder.py`; import failure bekle.
- [ ] **Implement:** Resmî `stream_1_0.xml`, schemaId1/version0 little-endian. Fixture metadata upstream commit `bed5d358dbe98bfa4722b6c00c27be1e8f0ba40b` taşır. Cursor bounds her read'den önce; offsets incoming root/group blockLength ile ilerler. Desteklenen version extension bytes atlanır, minimum alan boyundan küçük blockLength reddedilir. Symbol uint8 length/UTF8. Trade Bool yalnız0/1, bilinmeyen değer protocol error. Test fixture generator production decoder'ı import etmez, expected alanları XML field tablosundan elle denetlenir.
- [ ] **GREEN:** Aynı komut PASS; fixture README byte offsets ve beklenen alanları gösterir.
- [ ] **Commit:** `feat: decode Binance Spot SBE market frames`.

## Task 2: Canonical adaptör ve ortak book session

**Files:** Create `binance/normalize_sbe.py`; modify `binance/connector.py`; create `tests/exchanges/binance/test_sbe_connector.py`.

**Interfaces:** `normalize_sbe_message(event: SbeMessage, *, local_ts: int, registry: InstrumentRegistry) -> Iterable[Record]`; `to_depth_event(event: SbeDepthDiff) -> BinanceDepthEvent`. Existing BinanceConnector positional arguments korunur; keyword-only `wire_format: Literal["json", "sbe"] = "json"`, `api_key: str | None = None`, `snapshot_provider: SnapshotProvider | None = None`. JSON build_channels varsayılanı aynı; `build_sbe_channels(symbols: list[str], channels: list[str]) -> list[str]` new pure function.

- [ ] **Test yaz:** canonical venue/source/symbol JSON ile aynı. Trade buyer-maker True→Side.SELL, False→Side.BUY; trade source_ts transact_time_us×1000, BBO/depth event_time_us×1000. Mantissa×10^exponent doğru; float overflow/NaN/Infinity failure. `book_delta`+`book_snapshot` tek `<sym>@depth`; trade `<sym>@trade`, ticker `<sym>@bestBidAsk`. Partial snapshot dispatch, geniş session'ı değiştirmez. U/u ortak BinanceDepthEvent'e eksiksiz geçer; qty0 delete korunur.

```python
def test_buyer_maker_trade_has_sell_aggressor_side(sbe_fixture):
    event = decode_sbe_frame(sbe_fixture("trades.bin"))
    rec, = normalize_sbe_message(event, local_ts=7, registry=InstrumentRegistry())
    assert rec.symbol == "binance-spot:BTCUSDT"
    assert rec.side == Side.SELL
    assert rec.source_ts == 1700000000001000000
    assert rec.price == 30000.12
    assert rec.amount == 1.25
```
- [ ] **RED:** `.\.venv\Scripts\python.exe -m pytest tests/exchanges/binance/test_sbe_connector.py`; mode/API assertions FAIL.
- [ ] **Implement:** SBE binary frame `_decode_frame` override; TEXT yalnız JSON control. DepthDiff canonical doğrudan sink'e atılmaz, raw planın BinanceBookSession'ına verilir. Other typed events normalizer adaptörüne gider. Integer/exponent raw journal/decoded events'te kalır; canonical floats mevcut contract'tır. Top20 snapshot sadece kapsamlı ayrı partial subscription isteyen caller için decode edilebilir; ilk paket canonical channel mapping buna bağlanmaz. Geniş state'e yanlış template dispatch typed failure'dır. Unknown canonical channel SBE'de başlamadan ConfigError, sessiz boş subscription yoktur.
- [ ] **GREEN:** Yukarıdaki test + `.\.venv\Scripts\python.exe -m pytest tests/exchanges/binance/test_connector.py tests/exchanges/binance/test_normalize_spot.py tests/exchanges/binance/test_normalize_usdm.py tests/exchanges/binance/test_book_session.py` PASS.
- [ ] **Commit:** `feat: ingest SBE through the existing Spot book identity`.

## Task 3: Settings, factory ve doğru transport

**Files:** Modify `core/config.py`, `crypto/exchanges/factory.py`, `capabilities/ops.py`; modify `tests/conformance/test_config.py`, `tests/exchanges/test_factory.py`, `tests/capabilities/test_ops.py`; update README.

**Interfaces:**

| Settings alanı | Default | Env |
|---|---|---|
| binance_market: str | spot | CROCODILE_BINANCE_MARKET |
| binance_wire_format: str | json | CROCODILE_BINANCE_WIRE_FORMAT |
| binance_sbe_api_key: str or None | None | CROCODILE_BINANCE_SBE_API_KEY |

`make_connector(..., *, settings: Settings | None = None, **kw: Any) -> Connector`: settings factory tarafından tüketilir; explicit constructor kwargs override eder; yalnız ilgili native connector'a ilgili değerler verilir. `collect` context.settings geçirir. Connector `make_transport() -> Transport` hook'u kullanır; generic default AiohttpWsTransport, SBE override URL+header taşır. Bu hook raw plan transport'un headers API'sini tüketir.

- [ ] **Test yaz:** injected mapping env alanları yüklenir; repr secret key içermez. SBE+USDM/COINM, empty key ve invalid mode network öncesi ConfigError. SBE ws_connect key header alır ve signature/private key parametresi yoktur. Factory explicit kw Settings'i override eder; diğer venue'ye settings aktarılmaz. Collect configured transport'u ezmez. Collect-market SBE seçili Binance kaynağında universe/network çağırmadan ConfigError; JSON default halen CCXT yolunu kullanabilir.

```python
def test_sbe_mode_rejects_non_spot_market_before_connect():
    with pytest.raises(ConfigError):
        BinanceConnector(["BTCUSDT"], ["book_delta"], MemorySink(),
                         InstrumentRegistry(), market="usdm",
                         wire_format="sbe", api_key="fixture-key")
```
- [ ] **RED:** `.\.venv\Scripts\python.exe -m pytest tests/conformance/test_config.py tests/exchanges/test_factory.py tests/capabilities/test_ops.py`; yeni tests FAIL.
- [ ] **Implement:** Factory/constructor enum ve required key validation; Settings.from_env'in string parsing kuralını değiştirme. Key tipini string'den tahmin etme; yanlış key/access borsa response'u sanitized fatal rejection'dır. Endpoint `wss://stream-sbe.binance.com/ws`; subscription JSON text. `collect-market` raw/SBE için ilk sürümde native-symbol `collect` yönlendirme mesajı verir, otomatik CCXT fallback yapmaz. Secrets CLI/REST/MCP params içine eklenmez. README iki env mode örneği ve Ed25519 key şartı.
- [ ] **GREEN:** Yukarıdaki test PASS; `.\.venv\Scripts\python.exe -m pytest tests/surfaces/test_cli_arguments.py tests/surfaces/test_unknown_parameters.py tests/conformance/test_phase2_surface_parity.py` generated surface regressions PASS.
- [ ] **Commit:** `feat: configure Spot SBE without changing collect defaults`.

## Task 4: Shutdown, SBE replay ve ölçüm

**Files:** Modify `binance/connector.py`, `binance/replay.py`, `tests/exchanges/binance/conftest.py`; create `tests/exchanges/binance/test_sbe_replay.py`, new `scripts/benchmark_raw_capture.py`; create `tests/capture/test_benchmark_counters.py`.

**Interfaces:** Decoder ID `binance-sbe`, decoder version1, upstream schema1/version0, stream metadata. `BinanceSbeReplayDecoder` raw planın `ReplayDecoder.apply/observe` API'sini uygular; pure canonical adaptör ve aynı sequence engine'i kullanır. Benchmark `benchmark_capture(reader: CaptureReader, *, repeats: int = 1) -> dict[str, int | float | str]` read-only saved fixture üzerinde throughput/counter üretir. CLI script capture_dir positional; key/env output yoktur.

- [ ] **Test yaz:** binary SBE capture + raw REST snapshot offline live canonical serialized output/final book eşit; all network calls forbidden. UTC rollback/snapshot in-flight order korunur. `serverShutdown`→reconnect→registry/snapshot reset; live close retry, fixture EOF normal bitiş. Unknown depth template raw bytes korunur, session continuity iddiası/fresh output yoktur. BBO autoculling/new depth count farklı; duplicate/heartbeat depth counters'ı artırmaz. Benchmark received/queued/written/fsynced ve known coverage raporlar.

`recorded_sbe_session` fixture'ı bağımsız binary/REST scripted run'dan reader, decoder mapping ve expected Record listesi döndürür; raw planın network_forbidden fixture'ı kullanılır.

```python
def test_sbe_capture_replay_matches_output(recorded_sbe_session, network_forbidden):
    reader, decoders, expected = recorded_sbe_session
    actual = list(RawReplay(reader, decoders).records())
    assert [msgspec.msgpack.encode(r) for r in actual] == [
        msgspec.msgpack.encode(r) for r in expected]
```
- [ ] **RED:** `.\.venv\Scripts\python.exe -m pytest tests/exchanges/binance/test_sbe_replay.py tests/capture/test_benchmark_counters.py`; missing replay decoder assertions FAIL.
- [ ] **Implement:** serverShutdown sanitized control router transient failure; her socket reset sonrası snapshot eşitlenmesi. Unsupported binary template capture edilir ama decoder session fatal durumu görünür olur. Replay live transport/auth oluşturmaz. Fixture benchmark exact source event counts + wall throughput ayrı; credentials varsa active/sakin sembol canlı ölçümü farklı raporda, yoksa fixture-only sonucu. 20 ms interval ölçülmüş receive cadence yerine yazılmaz.
- [ ] **GREEN:** Yeni testler + ana plan connector/collect selection PASS; `.\.venv\Scripts\python.exe -m ruff check src/crocodile/crypto/exchanges/binance/sbe.py src/crocodile/crypto/exchanges/binance/normalize_sbe.py scripts/benchmark_raw_capture.py`.
- [ ] **Commit:** `feat: replay and validate SBE captures`.

## Self-review

Pure decode, normalization/units, shared sequence/bootstrap, source identity, key transport, factory/default routing, lifecycle/replay ve ölçüm görevlerle kapsandı. Shared book_session oluşturma raw plana aittir; burada ikinci state machine yoktur. dördüncü decoder template desteği, geniş deftere partial snapshot uygulanacağı anlamına gelmez. Gerçek erişim/live cadence hazır credentials olmadan doğrulanmış sayılmaz.
