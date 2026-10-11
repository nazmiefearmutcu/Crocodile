# Deribit raw Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ücretsiz yetkili Deribit raw L2 kanalını sessiz fallback olmadan açmak; auth/heartbeat/reconnect ve book sequence bütünlüğünü korumak.

**Architecture:** Request-ID router market processor'dan bağımsız bir Deribit session state machine'ini ilerletir. Auth ack ardından subscribe, snapshot ardından delta gate açılır. Raw capture yalnız market bytes ve sanitized lifecycle kararlarını saklar; replay auth/socket açmadan aynı pure book gate'i kullanır.

**Tech Stack:** Python 3.12, asyncio, JSON-RPC2.0, aiohttp, CaptureWriter/ReplayDecoder, pytest/pytest-asyncio.

**Spec:** [Onaylanan tasarım](../specs/2026-10-11-free-l2-capture-design.md); repo `docs/superpowers/specs/2026-10-11-free-l2-capture-design.md`. Ön koşullar: [raw capture](2026-10-11-raw-capture-plan.md) ve SBE planında tanımlanan shared Settings/factory/transport hook değişiklikleri yeşil.

## Global Constraints

Interval yalnız `100ms|raw`, default100ms. Raw credentials yokken configuration error; 100ms'e fallback yoktur. Raw 1 ms toplulaştırılmış L2'dir, L3 emir kimliği değildir ve overload'da sessiz sembol için sürekli yeni değişim garanti etmez. Her socket yeni connection-scope auth kullanır. Token'lar socket kapanınca silinir. Application heartbeat en az10 s; default30 s. Auth/control payload'ları journal/log/DLQ'da saklanmaz.

## Review Focus

Eksik key/yanlış mode ve client ID repr görev1; control ack deadlock/late response görev2; clock rollback/short expiry/idle test_request/cancellation görev3; per-symbol gap/bilinmeyen action/replay auth exclusion görev4 testleriyle kapsanır.

## Dosya haritası

Yeni `deribit/session.py` auth/request state; `deribit/book.py` pure continuity gate; `deribit/replay.py` offline adapter. Mevcut `connector.py` bunları birleştirir; `normalize.py` canonical conversion action kontrolünü güçlendirir. Shared Connector reader/control/pacing/journal kodu burada ikinci defa yazılmaz.

## Task 1: Mode/config ve subscription mapping

**Files:** Modify `deribit/connector.py`, `core/config.py`, `crypto/exchanges/factory.py`, `capabilities/ops.py`, README; modify `tests/exchanges/deribit/test_connector.py`, `tests/conformance/test_config.py`, `tests/capabilities/test_ops.py`, `tests/exchanges/test_factory.py`.

**Interfaces:** Mevcut constructor positional uyumu korunur; yeni keyword-only `interval: Literal["100ms", "raw"] = "100ms"`, `client_id: str | None = None`, `client_secret: str | None = None`, `request_timeout_seconds: float = 10.0`, `heartbeat_interval_seconds: int = 30`, `refresh_margin_seconds: float = 30.0`. `build_channels(symbols, channels, *, interval="100ms") -> list[str]`. Settings fields `deribit_interval="100ms"`, `deribit_client_id=None`, `deribit_client_secret=None` eşlemeleri sırasıyla `CROCODILE_DERIBIT_INTERVAL`, `CROCODILE_DERIBIT_CLIENT_ID`, `CROCODILE_DERIBIT_CLIENT_SECRET`.

- [ ] **Test yaz:** default channel100ms; raw book_delta/book_snapshot tek `book.BTC-PERPETUAL.raw`; missing either/empty credentials raw before-connect ConfigError; invalidinterval/heartbeat<10/nonpositive timeout error. Settings injected env/repr secret ve client_id örneğini göstermez. Factory yalnız Deribit'e ilgili config verir; explicit kwargs üstün. Collect-market Deribit raw universe/network öncesi ConfigError; default100ms davranışı korunur.

```python
def test_build_channels_raw_deduplicates_book_subscription():
    assert build_channels(["BTC-PERPETUAL"], ["book_delta", "book_snapshot"],
                          interval="raw") == ["book.BTC-PERPETUAL.raw"]
    assert build_channels(["BTC-PERPETUAL"], ["book_delta"]) == [
        "book.BTC-PERPETUAL.100ms"]
```
- [ ] **RED:** `.\.venv\Scripts\python.exe -m pytest tests/exchanges/deribit/test_connector.py tests/conformance/test_config.py tests/exchanges/test_factory.py tests/capabilities/test_ops.py`; yeni mode tests FAIL.
- [ ] **Implement:** Channel map interval parametresiyle üretilir; constructor enum/credentials/timer validation. Settings `_VARS` güncellenir; `_SECRET_FIELDS` derivation'a yalnız `deribit_client_id` explicit dahil edilir, suffix `_secret` zaten korunur. Credentials published CollectParams veya README gerçek değerlerine girmez. SBE planının factory Settings injection ve transport hook API'si kullanılır; yeni venue adı eklenmez. README raw/key/100ms seçimleri ve kontrat birimleriyle ilgili scope açıklar.
- [ ] **GREEN:** Yukarıdaki test PASS; mevcut Deribit normalizer tests de PASS.
- [ ] **Commit:** `feat: configure authenticated Deribit raw subscriptions`.

## Task 2: Request correlation ve auth→subscribe lifecycle

**Files:** Create `deribit/session.py`, `tests/exchanges/deribit/test_session.py`, `tests/exchanges/deribit/conftest.py`; modify `deribit/connector.py`.

**Interfaces:**

```python
class DeribitSession:
    def __init__(self, *, interval: Literal["100ms", "raw"],
                 client_id: str | None, client_secret: str | None,
                 channels: tuple[str, ...], request_timeout_seconds: float = 10.0,
                 heartbeat_interval_seconds: int = 30,
                 refresh_margin_seconds: float = 30.0) -> None: ...
    async def begin(self, transport: Transport) -> None: ...
    async def handle_control(self, msg: dict[str, Any]) -> bool: ...
    async def resubscribe_symbol(self, symbol_raw: str) -> None: ...
    async def close(self) -> None: ...
```

State enum `DISCONNECTED|AUTH_PENDING|SUBSCRIBE_PENDING|SNAPSHOT_PENDING|LIVE|FAILED`. Pending request entries request_id, connection_epoch, method, deadline_monotonic_ns taşır. ID counter aynı connector ömrü boyunca artar, reconnect'te sıfırlanmaz. `session.ready_for_market` True ancak subscription ack başarılı olduğunda; per-symbol snapshot gate sonraki görevde.

- [ ] **Test yaz:** begin auth request gönderir ve response beklemeden döner; control router ack'i market processor beklerken tüketir. Raw subscribe auth success'ten önce gönderilmez. Out-of-order response yalnız ilgili request'i tamamlar; old connection late ack yeni future'ı tamamlamaz. Auth/subscribe rejection sanitized FatalConnectorError; unknown control raw bytes/token journal/caplog/DLQ'da yoktur. Outstanding request timeout pending state'i temizler ve connection kapanır.

`rpc_transport` fixture'ı FakeTransport benzeri, `sent: list[bytes]` tutan async send/connect/close transport'tur; gerçek ağ açmaz. Auth success ack'i test doğrudan router'a verir.

```python
async def test_subscribe_is_sent_only_after_auth_success(rpc_transport):
    session = DeribitSession(interval="raw", client_id="id-sentinel",
        client_secret="secret-sentinel", channels=("book.BTC-PERPETUAL.raw",))
    await session.begin(rpc_transport)
    requests = [json.loads(b) for b in rpc_transport.sent]
    auth, = [r for r in requests if r["method"] == "public/auth"]
    assert not any(r["method"] == "public/subscribe" for r in requests)
    await session.handle_control({"id": auth["id"], "result": {
        "access_token": "access-sentinel", "refresh_token": "refresh-sentinel",
        "expires_in": 900, "token_type": "bearer"}})
    assert any(json.loads(b)["method"] == "public/subscribe"
               for b in rpc_transport.sent)
    await session.close()
```
- [ ] **RED:** `.\.venv\Scripts\python.exe -m pytest tests/exchanges/deribit/test_session.py`; missing session/interface failure.
- [ ] **Implement:** `_subscribe` session.begin çağırıp döner; auth future bekleyen market processor yoktur. Raw `public/auth` grant_type client_credentials ve connection scope kullanır; token başarı/expiry metadata doğrulanır. Known auth/subscribe responses `_handle_control_message` içinde generic msg.error handling'den önce tüketilir. Başarı state ilerletir ve subscription JSON-RPC id'li text gönderilir. 100ms auth gerektirmez; subscription ack ayrı control path'tir. Timer task TaskGroup ile owned; request timeout transient bağlantı hatası olarak supervisor'a çıkar, server credential/access rejection fataldır. Kapanma pending futures/watchdog temizler; auth errors token içeren exchange data'yı string olarak loglamaz.
- [ ] **GREEN:** Aynı komut ve `tests/exchanges/test_base_run_loop.py`, `tests/ingest/test_capture_runloop.py` PASS. FakeTransport fixture'ları auth/ack gerektiğinde scripted, default public compatibility mevcut testlerle korunur.
- [ ] **Commit:** `feat: correlate Deribit control requests without blocking ingest`.

## Task 3: Expiry, refresh ve application heartbeat

**Files:** Modify `deribit/session.py`; create `tests/exchanges/deribit/test_session_timers.py`.

**Interfaces:** Session constructor timer değerleri yukarıdaki exact alanlar; private clock injection `clock_ns: Callable[[], int] = time.monotonic_ns` test-only keyword. `_watchdog() -> Awaitable[NoReturn]` pending deadlines/refresh'i yönetir; `close` task'ı cancel+await eder. Token values private session fields, repr redacted. `handle_control` test_request'e `public/test` request üretir.

- [ ] **Test yaz:** expires_in bool/zero/negative/invalid reddedilir; UTC geriye gitse de monotonic expiry değişmez. Short token lifetime'da refresh margin `min(30s, lifetime/2)` ve lifetime içinde kalır. Refresh before-expiry, new access/refresh token atomic replacement; rejected/timeout refresh LIVE iddiasını kapatır. Socket close tokens None; reconnect fresh public/auth, eski refresh grant gönderilmez. Idle test_request market frame beklemeden public/test üretir. Duplicate control reply/heartbeat yeni depth event sayılmaz. Cancellation pending timers ve futures temizler.

`authenticated_session` fixture'ı yukarıdaki auth ack senaryosunu subscription/heartbeat ack'leriyle tamamlar ve (session, rpc_transport) verir; finalizer session.close çağırır.

```python
async def test_test_request_sends_public_test_without_market_data(authenticated_session):
    session, transport = authenticated_session
    before = len(transport.sent)
    handled = await session.handle_control({"method": "heartbeat",
        "params": {"type": "test_request"}})
    assert handled is True
    assert any(json.loads(b)["method"] == "public/test"
               for b in transport.sent[before:])
```
- [ ] **RED:** `.\.venv\Scripts\python.exe -m pytest tests/exchanges/deribit/test_session_timers.py`; missing expiry/heartbeat assertions FAIL.
- [ ] **Implement:** Auth positive expires_in seconds monotonic deadline. Aynı socket'te public/auth refresh_token grant; expiry öncesi correlation map ve watchdog çalışır. Refresh server rejection FatalConnectorError, transport timeout TransientConnectorError; ikisinde token/live readiness geçersiz olur. `public/set_heartbeat` interval30 correlated request; `test_request` public/test immediate. WS ping/pong ayrı mekanizma, application heartbeat yerine sayılmaz. Timer task errors supervised loop'a çıkar; background task exception loguyla kaybolmaz.
- [ ] **GREEN:** Yukarıdaki timer testleri fake clock ile PASS; gerçek kısa sleep/credentials gerektirmez.
- [ ] **Commit:** `feat: refresh Deribit sessions and answer application heartbeats`.

## Task 4: Per-symbol book gate ve offline raw replay

**Files:** Create `deribit/book.py`, `deribit/replay.py`; modify `deribit/connector.py`, `deribit/normalize.py`; create `tests/exchanges/deribit/test_book_sequence.py`, `tests/exchanges/deribit/test_raw_replay.py`.

**Interfaces:** `DeribitBookGate.evaluate(data: dict[str, Any]) -> Literal["snapshot", "apply", "drop", "gap"]`; `reset(symbol_raw: str | None = None) -> None`; `last_change_id(symbol_raw: str) -> int | None`. Gate explicit snapshot readiness/ID state tutar. `DeribitReplayDecoder` raw planın ReplayDecoder.apply/observe API'sini uygular; decoder ID `deribit-json`, version1, stream metadata interval raw/100ms ayrı. Subscription ack sonrası snapshot expectation gate'ine bağlanır.

- [ ] **Test yaz:** initial snapshot gate açar; change matching prev_change_id gerektirir. Mismatch sadece affected symbol'ü kapatır; diğer symbol ilerler. Gap unsubscribe ack→resubscribe→fresh snapshot; aradaki deltas DROP, journal bytes yine korunur. Delete canonical qty0, new/change absolute qty; unknown action protocol failure. Reconnect tüm book IDs/readiness temizler. Raw capture replay canonical serialized sıra/final book eşit ve auth/HTTP/WS calls forbidden. Journal/manifest/DLQ/caplog sentinel ID/secret/access/refresh token içermiyor. Snapshot öncesi control/data yarışı market çıkışı üretmez.

```python
def test_change_requires_matching_previous_change_id():
    gate = DeribitBookGate()
    base = {"instrument_name": "BTC-PERPETUAL", "bids": [], "asks": []}
    assert gate.evaluate({**base, "type": "snapshot", "change_id": 10}) == "snapshot"
    assert gate.evaluate({**base, "type": "change", "change_id": 12,
                          "prev_change_id": 11}) == "gap"
    assert gate.last_change_id("BTC-PERPETUAL") is None
    assert gate.evaluate({**base, "type": "change", "change_id": 13,
                          "prev_change_id": 12}) == "drop"
```
- [ ] **RED:** `.\.venv\Scripts\python.exe -m pytest tests/exchanges/deribit/test_book_sequence.py tests/exchanges/deribit/test_raw_replay.py`; missing gate/replay failure.
- [ ] **Implement:** Validated market frame capture sonra pure normalize/gate; apply/drop/gap/snapshot ApplicationDecision origin ref kaydı. Gap'in raw delta bilgisi saklanır ama fiyat defterine uygulanmaz. `resubscribe_symbol` outstanding requests ile unsubscribe/subscribe ack sırasını yürütür; auth tokens router dışında dolaşmaz. Same symbol snapshot yeni epoch'u başlatır. Replay raw decisions+gate tekrar değerlendirmesiyle yalnız geçerli canonical records üretir; canlı session/auth yaşam döngüsünü yürütmez. Malformed mixed-control JSON'un payload saklanmaması raw plan classification sözleşmesidir.
- [ ] **GREEN:** Yukarıdaki tests + `.\.venv\Scripts\python.exe -m pytest tests/exchanges/deribit tests/capture tests/client/test_collect.py tests/client/test_collect_capture.py` PASS. `.\.venv\Scripts\python.exe -m ruff check src/crocodile/crypto/exchanges/deribit/session.py src/crocodile/crypto/exchanges/deribit/book.py src/crocodile/crypto/exchanges/deribit/replay.py`.
- [ ] **Commit:** `feat: validate and replay Deribit raw book sequences`.

## Self-review

Mode/credentials, control correlation, nonblocking auth, expiry/refresh/heartbeat, book chain/reconnect, safe raw capture ve offline replay görevlerle kapsandı. Timer/session ve market gate sorumlulukları ayrıdır. Explicit requested raw hiçbir yolda100ms/CCXT fallback yapmaz. Gerçek API auth ve receive cadence hazır local credentials ile ayrı kabul ölçümüdür; fixture-only sonuç bunu kanıtlamaz.
