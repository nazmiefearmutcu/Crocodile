"""Protocol oracles for the public fast feeds, including secret-free replay."""

import json
from decimal import Decimal

import msgspec
import pytest

from crocodile.core.capture.classification import classify_frame
from crocodile.core.capture.reader import CaptureReader
from crocodile.core.capture.replay import RawReplay
from crocodile.core.capture.schema import CaptureContext, RawFrame, StreamContext
from crocodile.core.capture.writer import CaptureWriter
from crocodile.core.errors import ConfigError, FatalConnectorError
from crocodile.core.ingest.transport import FakeTransport
from crocodile.core.schema.provenance import Provenance
from crocodile.core.sink.memory import MemorySink
from crocodile.crypto.instruments.registry import InstrumentRegistry


def frame(msg):
    return RawFrame(
        payload=json.dumps(msg).encode(),
        frame_kind="text",
        received_utc_ns=100,
        received_monotonic_ns=100,
        connection_id="test",
        receive_ordinal=1,
    )


@pytest.mark.parametrize("key", ["token", "jwt", "api_key", "API-Sign", "auth"])
def test_credentials_never_classified_as_market(key):
    msg = {"channel": "level3", "type": "snapshot", "data": [{key: "SENTINEL"}]}
    assert classify_frame("kraken", frame(msg), msg) == "control"


def test_bitfinex_order_move_delete_decimal_and_socket_sequence():
    from crocodile.crypto.exchanges.bitfinex.book import BitfinexBookGate

    gate = BitfinexBookGate()
    gate.map_channel(
        {
            "chanId": 7,
            "symbol": "tBTCUSD",
            "channel": "book",
            "prec": "R0",
            "freq": "F0",
            "len": "250",
        }
    )
    registry = InstrumentRegistry()
    snap = gate.apply(
        [7, [[1, "100", "0.1"], [2, "100", "0.2"], [3, "101", "-1"]], 1, 100], 99, registry
    )
    assert snap.records[0].bids == [(100.0, 0.3)]
    assert snap.records[0].prov == Provenance.DERIVED
    assert snap.records[0].sequence_id is None
    moved = gate.apply([7, [1, "99", "0.4"], 2, 101], 99, registry)
    assert dict(moved.records[0].bids) == {100.0: 0.2, 99.0: 0.4}
    assert gate.apply([7, "hb", 3, 102], 99, registry).records == ()
    removed = gate.apply([7, [2, 0, 1], 4, 103], 99, registry)
    assert removed.records[0].bids == [(100.0, 0.0)]
    assert gate.apply([7, [1, "99", "0.4"], 6, 104], 99, registry).action == "gap"
    assert gate.apply([7, [1, "99", "0.5"], 7, 105], 99, registry).action == "drop"


def test_bitfinex_unknown_channel_order_and_bad_checksum_are_visible():
    from crocodile.crypto.exchanges.bitfinex.book import BitfinexBookGate

    gate = BitfinexBookGate()
    with pytest.raises(FatalConnectorError):
        gate.apply([7, [1, 100, 1], 1, 100], 99, InstrumentRegistry())
    gate.map_channel(
        {
            "chanId": 7,
            "symbol": "tBTCUSD",
            "channel": "book",
            "prec": "R0",
            "freq": "F0",
            "len": "250",
        }
    )
    gate.apply([7, [[1, 100, 1], [2, 101, -1]], 1, 100], 99, InstrumentRegistry())
    assert gate.apply([7, "cs", 0, 2, 100], 99, InstrumentRegistry()).action == "gap"


def test_bitfinex_checksum_uses_javascript_number_spelling_for_tiny_orders():
    import zlib

    from crocodile.crypto.exchanges.bitfinex.book import BitfinexBookGate

    gate = BitfinexBookGate()
    gate.map_channel(
        {
            "chanId": 7,
            "symbol": "tBTCUSD",
            "channel": "book",
            "prec": "R0",
            "freq": "F0",
            "len": "250",
        }
    )
    gate.apply([7, [[1, 100, 1e-8], [2, 101, -1e-6]], 1, 100], 99, InstrumentRegistry())
    unsigned = zlib.crc32(b"1:1e-8:2:-0.000001")
    expected = unsigned if unsigned < 2**31 else unsigned - 2**32
    assert gate.apply([7, "cs", expected, 2, 100], 99, InstrumentRegistry()).action == "drop"
    assert not gate.invalid


def kraken(kind, bids, checksum=None):
    data = {
        "symbol": "BTC/USD",
        "bids": bids,
        "asks": [],
        "timestamp": "2026-10-11T12:00:00.123456789Z",
    }
    if checksum is not None:
        data["checksum"] = checksum
    return {"channel": "level3", "type": kind, "data": [data]}


def test_kraken_l3_orders_prune_precision_checksum_and_no_sequence():
    from crocodile.crypto.exchanges.kraken.book import KrakenBookGate

    gate = KrakenBookGate(mode="level3", depth=10)
    rows = [
        {
            "order_id": str(i),
            "limit_price": str(100 - i),
            "order_qty": "0.1",
            "timestamp": "2026-10-11T12:00:00.123456789Z",
        }
        for i in range(10)
    ]
    # Independent checksum oracle: asks empty, descending bids, exact digits.
    import zlib

    crc = zlib.crc32("".join(str(100 - i) + "1" for i in range(10)).encode())
    result = gate.apply(kraken("snapshot", rows, crc), 99, InstrumentRegistry())
    assert result.records[0].source_ts % 1_000_000_000 == 123456789
    assert result.records[0].prov == Provenance.DERIVED
    assert result.records[0].sequence_id is None
    update = {
        "event": "add",
        "order_id": "new",
        "limit_price": "101",
        "order_qty": "0.2",
        "timestamp": "2026-10-11T12:00:01Z",
    }
    updated_crc = zlib.crc32(("1012" + "".join(str(100 - i) + "1" for i in range(9))).encode())
    result = gate.apply(kraken("update", [update], updated_crc), 99, InstrumentRegistry())
    assert (91.0, 0.0) in result.records[0].bids
    assert len(gate.bids["BTC/USD"]) == 10
    bad = {
        "event": "modify",
        "order_id": "missing",
        "limit_price": "100",
        "order_qty": "1",
        "timestamp": "2026-10-11T12:00:01Z",
    }
    with pytest.raises(FatalConnectorError):
        gate.apply(kraken("update", [bad]), 99, InstrumentRegistry())


def test_advanced_connection_sequence_includes_heartbeats_and_other_products():
    from crocodile.crypto.exchanges.coinbase.advanced import CoinbaseAdvancedGate

    gate = CoinbaseAdvancedGate()
    reg = InstrumentRegistry()

    def message(seq, kind="snapshot", symbol="BTC-USD", qty="1"):
        return {
            "channel": "l2_data",
            "sequence_num": seq,
            "timestamp": "2026-10-11T12:00:00.123456789Z",
            "events": [
                {
                    "type": kind,
                    "product_id": symbol,
                    "updates": [{"side": "bid", "price_level": "100", "new_quantity": qty}],
                }
            ],
        }

    assert gate.apply(message(10), 99, reg).action == "snapshot"
    assert (
        gate.apply({"channel": "heartbeats", "sequence_num": 11, "events": []}, 99, reg).records
        == ()
    )
    assert gate.apply(message(12, symbol="ETH-USD"), 99, reg).action == "snapshot"
    result = gate.apply(message(13, "update", qty="2"), 99, reg)
    assert result.records[0].seq_id is None
    assert gate.apply(message(15, "update"), 99, reg).action == "gap"
    assert gate.apply(message(16), 99, reg).action == "drop"
    gate.reset()
    assert gate.apply(message(0, "update"), 99, reg).action == "drop"


def test_dydx_subscription_snapshot_absolute_delete_and_gap():
    from crocodile.crypto.exchanges.dydx.book import DydxBookGate

    gate = DydxBookGate()
    reg = InstrumentRegistry()

    def message(kind, mid, qty):
        return {
            "type": kind,
            "channel": "v4_orderbook",
            "id": "BTC-USD",
            "connection_id": "socket",
            "message_id": mid,
            "contents": {"bids": [{"price": "100", "size": qty}], "asks": []},
        }

    assert gate.apply(message("channel_data", 1, "2"), 99, reg).action == "drop"
    gate.reset()
    assert gate.apply(message("subscribed", 0, "3"), 99, reg).action == "snapshot"
    result = gate.apply(message("channel_data", 1, "0"), 99, reg)
    assert result.records[0].bids == [(100.0, 0.0)]
    assert result.records[0].seq_id is None
    assert gate.apply(message("channel_data", 3, "1"), 99, reg).action == "gap"


def test_bybit_fast_u_is_not_full_consecutive_sequence_and_full_requires_anchor():
    from crocodile.crypto.exchanges.bybit.book import BybitBookGate

    reg = InstrumentRegistry()

    def message(kind, u, seq=10):
        return {
            "topic": "orderbook.50.BTCUSDT",
            "type": kind,
            "ts": 1,
            "data": {"s": "BTCUSDT", "u": u, "seq": seq, "b": [["100", "1"]], "a": []},
        }

    gate = BybitBookGate()
    assert gate.apply(message("snapshot", 10), 99, reg).action == "snapshot"
    changed = message("delta", 15)
    changed["data"]["b"][0][1] = "2"
    assert gate.apply(changed, 99, reg).action == "apply"
    full = BybitBookGate(mode="full")
    delta = message("delta", 10)
    delta["topic"] = "orderbook.full.BTCUSDT"
    assert full.apply(delta, 99, reg).action == "drop"
    snapshot = message("snapshot", 10)["data"]
    snapshot["cts"] = 1
    assert full.anchor(snapshot, 99, reg).action == "snapshot"
    delta["data"]["u"] = 12
    assert full.apply(delta, 99, reg).action == "gap"


async def test_bitfinex_public_mapping_survives_capture_replay(tmp_path):
    from crocodile.crypto.exchanges.bitfinex.connector import BitfinexConnector
    from crocodile.crypto.exchanges.bitfinex.replay import BitfinexReplayDecoder

    messages = [
        {
            "event": "subscribed",
            "chanId": 7,
            "symbol": "tBTCUSD",
            "channel": "book",
            "prec": "R0",
            "freq": "F0",
            "len": "250",
        },
        [7, [[1, 100, 1], [2, 101, -1]], 1, 100],
        [7, "hb", 2, 101],
        [7, [1, 100, "0.5"], 3, 102],
    ]
    writer = await CaptureWriter.create(
        tmp_path,
        CaptureContext(
            capture_id="test",
            streams=(
                StreamContext(
                    stream_id="s1",
                    source="bitfinex",
                    market="spot",
                    symbols=("tBTCUSD",),
                    endpoint_id="bitfinex-r0-f0-250",
                    decoder_id="bitfinex-r0",
                    decoder_version=1,
                ),
            ),
        ),
    )
    sink = MemorySink()
    conn = BitfinexConnector(["tBTCUSD"], ["book_delta"], sink, InstrumentRegistry())
    conn.capture = writer
    conn.transport = FakeTransport([json.dumps(x).encode() for x in messages])
    await conn.run(max_reconnects=0)
    await writer.close()
    replay = RawReplay(CaptureReader(tmp_path), {"bitfinex-r0": BitfinexReplayDecoder()})
    assert [msgspec.msgpack.encode(x) for x in replay.records()] == [
        msgspec.msgpack.encode(x) for x in sink.records
    ]
    assert conn.telemetry()["changed_book_revisions"] == 1


def test_settings_native_factory_modes_and_validation():
    from crocodile.core.config import Settings
    from crocodile.crypto.exchanges.factory import list_exchanges, make_connector

    settings = Settings.from_env(
        {
            "CROCODILE_COINBASE_MODE": "advanced",
            "CROCODILE_KRAKEN_MODE": "level3",
            "CROCODILE_KRAKEN_API_KEY": "KEY_SENTINEL",
            "CROCODILE_KRAKEN_API_SECRET": "SECRET_SENTINEL",
        }
    )
    assert "SENTINEL" not in repr(settings)
    assert {"kraken", "bitfinex", "dydx"} <= set(list_exchanges())
    conn = make_connector(
        "coinbase",
        ["BTC-USD"],
        ["book_delta"],
        MemorySink(),
        InstrumentRegistry(),
        settings=settings,
    )
    assert conn.ws_url == "wss://advanced-trade-ws.coinbase.com"
    with pytest.raises(ConfigError):
        make_connector("kraken", [], [], MemorySink(), InstrumentRegistry(), mode="level3")
    with pytest.raises(ConfigError):
        make_connector(
            "bybit", [], [], MemorySink(), InstrumentRegistry(), category="option", book_depth=50
        )


async def test_explicit_native_required_failure_propagates():
    from crocodile.crypto.client.collect import collect

    class Required:
        name = "bitfinex"
        required_feed = True

        async def run(self, **kwargs):
            raise FatalConnectorError("Required source rejected")

    with pytest.raises(FatalConnectorError):
        await collect([Required()], MemorySink(), max_reconnects=0)


def test_telemetry_counts_only_changed_depth_records():
    from crocodile.core.schema.enums import AssetClass
    from crocodile.core.schema.records import BookTicker
    from crocodile.crypto.exchanges.coinbase.connector import CoinbaseConnector
    from crocodile.crypto.exchanges.public_books import BookResult

    conn = CoinbaseConnector(
        ["BTC-USD"], ["book_delta"], MemorySink(), InstrumentRegistry(), mode="advanced"
    )
    conn._count(
        BookResult(
            "apply",
            (
                BookTicker(
                    source="coinbase",
                    symbol="coinbase:BTC-USD",
                    symbol_raw="BTC-USD",
                    local_ts=1,
                    source_ts=None,
                    asset_class=AssetClass.CRYPTO,
                    bid_px=100,
                    bid_sz=1,
                    ask_px=101,
                    ask_sz=1,
                ),
            ),
        )
    )
    assert conn.telemetry()["changed_book_revisions"] == 0


async def test_bybit_full_waits_for_fetched_anchor_without_refetch(tmp_path):
    """REST can be ahead of WS; preserve that anchor until its packet arrives."""
    import asyncio

    from crocodile.core.capture.native import replay_decoders, stream_context
    from crocodile.core.capture.schema import SnapshotResponse
    from crocodile.core.capture.snapshots import CapturedSnapshot
    from crocodile.crypto.exchanges.bybit.connector import BybitConnector

    reg, sink = InstrumentRegistry(), MemorySink()
    conn = BybitConnector(
        ["BTCUSDT"], ["book_delta"], sink, reg, book_mode="full", bootstrap_timeout=1
    )
    writer = await CaptureWriter.create(
        tmp_path, CaptureContext(capture_id="full", streams=(stream_context(conn, "s1"),))
    )
    conn.capture, conn.capture_stream_id = writer, "s1"

    def delta(u, seq):
        return {
            "topic": "orderbook.full.BTCUSDT",
            "type": "delta",
            "cts": 1,
            "data": {"s": "BTCUSDT", "u": u, "seq": seq, "b": [["100", str(u)]], "a": []},
        }

    from crocodile.core.capture.schema import MarketFrame

    async def admit(msg):
        raw = frame(msg)
        origin = await writer.append(
            MarketFrame(stream_id="s1", frame=raw, observed_utc_ns=100, observed_monotonic_ns=100)
        )
        conn._origin_ordinal = origin
        await conn._handle_message(msg, 100)
        conn._origin_ordinal = None

    await admit(delta(10, 100))

    class Ahead:
        calls = 0

        async def fetch(self, request):
            self.calls += 1
            assert self.calls == 1
            await writer.append(request)
            response = SnapshotResponse(
                request_id=request.request_id,
                status=200,
                payload=json.dumps(
                    {
                        "retCode": 0,
                        "result": {
                            "s": "BTCUSDT",
                            "u": 11,
                            "seq": 101,
                            "cts": 1,
                            "b": [["100", "11"]],
                            "a": [],
                        },
                    }
                ).encode(),
                received_utc_ns=100,
                received_monotonic_ns=100,
            )
            origin = await writer.append(response)

            async def arrive():
                await asyncio.sleep(0.01)
                await admit(delta(11, 101))

            self.arrival = asyncio.create_task(arrive())
            return CapturedSnapshot(response=response, origin_ordinal=origin)

    conn._snapshot_provider = provider = Ahead()
    try:
        await conn._bootstrap_full()
        await admit(delta(12, 105))
    finally:
        await writer.close()
    assert provider.calls == 1
    assert conn.gate.bids["BTCUSDT"] == {Decimal("100"): Decimal("12")}
    replayed = list(RawReplay(CaptureReader(tmp_path), replay_decoders()).records())
    assert [msgspec.msgpack.encode(x) for x in replayed] == [
        msgspec.msgpack.encode(x) for x in sink.records
    ]


def test_bybit_full_decreasing_cross_sequence_does_not_mutate_book():
    from crocodile.crypto.exchanges.bybit.book import BybitBookGate

    gate, registry = BybitBookGate(mode="full"), InstrumentRegistry()
    gate.anchor(
        {"s": "BTCUSDT", "u": 10, "seq": 100, "cts": 1, "b": [["100", "1"]], "a": []}, 99, registry
    )
    msg = {
        "topic": "orderbook.full.BTCUSDT",
        "type": "delta",
        "cts": 1,
        "data": {"s": "BTCUSDT", "u": 11, "seq": 99, "b": [["100", "2"]], "a": []},
    }
    assert gate.apply(msg, 99, registry).action == "drop"
    assert gate.bids["BTCUSDT"][Decimal("100")] == 1


async def test_kraken_credentials_absent_from_capture_controls_and_dlq(tmp_path):
    import zlib

    from crocodile.core.capture.native import replay_decoders, stream_context
    from crocodile.crypto.exchanges.kraken.connector import KrakenConnector

    sink = MemorySink()
    conn = KrakenConnector(["BTC/USD"], ["book_delta"], sink, InstrumentRegistry())
    writer = await CaptureWriter.create(
        tmp_path, CaptureContext(capture_id="secret", streams=(stream_context(conn, "s1"),))
    )
    conn.capture = writer
    messages = [
        {"method": "subscribe", "result": {"token": "AUTH_SENTINEL"}},
        {"channel": "book", "type": "update", "data": [{"API-Sign": "AUTH_SENTINEL"}]},
        {
            "channel": "book",
            "type": "snapshot",
            "data": [
                {
                    "symbol": "BTC/USD",
                    "bids": [{"price": "100", "qty": "1"}],
                    "asks": [],
                    "checksum": zlib.crc32(b"1001"),
                }
            ],
        },
        {
            "channel": "book",
            "type": "update",
            "data": [
                {
                    "symbol": "BTC/USD",
                    "bids": [{"price": "100", "qty": "2"}],
                    "asks": [],
                    "checksum": zlib.crc32(b"1002"),
                    "timestamp": "2026-10-11T12:00:00.123456789Z",
                }
            ],
        },
    ]
    conn.transport = FakeTransport([json.dumps(msg).encode() for msg in messages])
    try:
        await conn.run(max_reconnects=0)
    finally:
        await writer.close()
    assert conn._dlq.drain() == []
    assert all(
        b"AUTH_SENTINEL" not in file.read_bytes() for file in tmp_path.rglob("*") if file.is_file()
    )
    replayed = list(RawReplay(CaptureReader(tmp_path), replay_decoders()).records())
    assert [msgspec.msgpack.encode(r) for r in replayed] == [
        msgspec.msgpack.encode(r) for r in sink.records
    ]


def test_advanced_connector_normalize_uses_selected_protocol():
    from crocodile.crypto.exchanges.coinbase.connector import CoinbaseConnector

    conn = CoinbaseConnector(
        ["BTC-USD"], ["book_delta"], MemorySink(), InstrumentRegistry(), mode="advanced"
    )
    message = {
        "channel": "l2_data",
        "sequence_num": 0,
        "timestamp": None,
        "events": [
            {
                "type": "snapshot",
                "product_id": "BTC-USD",
                "updates": [{"side": "bid", "price_level": "100", "new_quantity": "1"}],
            }
        ],
    }
    assert len(list(conn.normalize(message, 99))) == 1


def test_dydx_foreign_subscription_cannot_update_old_book():
    from crocodile.crypto.exchanges.dydx.book import DydxBookGate

    gate, registry = DydxBookGate(), InstrumentRegistry()
    message = {
        "type": "subscribed",
        "channel": "v4_orderbook",
        "id": "BTC-USD",
        "connection_id": "old",
        "message_id": 0,
        "contents": {"bids": [["100", "1"]], "asks": []},
    }
    gate.apply(message, 99, registry)
    message = {
        **message,
        "type": "channel_data",
        "connection_id": "new",
        "message_id": 1,
        "contents": {"bids": [["100", "2"]], "asks": []},
    }
    assert gate.apply(message, 99, registry).action == "gap"


async def test_kraken_l3_auth_token_is_renewed_and_only_sent_to_subscription(monkeypatch):
    from crocodile.crypto.exchanges.kraken.connector import KrakenConnector

    conn = KrakenConnector(
        ["BTC/USD"],
        ["book_delta"],
        MemorySink(),
        InstrumentRegistry(),
        mode="level3",
        api_key="KEY",
        api_secret="c2VjcmV0",
    )
    requests = []

    async def fresh():
        token = f"AUTH_SENTINEL_{len(requests)}"
        requests.append(token)
        return token

    monkeypatch.setattr(conn, "_token", fresh)

    class RecordingTransport(FakeTransport):
        def __init__(self):
            super().__init__([])
            self.sent = []

        async def send(self, data):
            self.sent.append(data)

    transport = RecordingTransport()
    await conn._subscribe(transport)
    await conn._prepare_connection()
    await conn._subscribe(transport)
    assert len(requests) == 2
    assert "token" not in conn.__dict__
    assert [json.loads(payload)["params"]["token"] for payload in transport.sent] == requests


def test_bitfinex_multisymbol_socket_counter_and_reconnect_mapping():
    from crocodile.crypto.exchanges.bitfinex.book import BitfinexBookGate

    gate, registry = BitfinexBookGate(), InstrumentRegistry()
    for channel, symbol in [(7, "tBTCUSD"), (8, "tETHUSD")]:
        gate.map_channel(
            {
                "chanId": channel,
                "symbol": symbol,
                "channel": "book",
                "prec": "R0",
                "freq": "F0",
                "len": "250",
            }
        )
    gate.apply([7, [[1, 100, 1]], 1, 100], 99, registry)
    gate.apply([8, [[2, 50, 1]], 2, 100], 99, registry)
    assert gate.apply([7, [1, 100, 2], 3, 100], 99, registry).action == "apply"
    gate.reset()
    assert not gate.ready
    with pytest.raises(FatalConnectorError):
        gate.apply([7, [[1, 100, 1]], 1, 100], 99, registry)


def test_coinbase_advanced_product_currency_and_price_tick_fields():
    from crocodile.crypto.exchanges.coinbase.connector import parse_products

    instrument = parse_products(
        {
            "products": [
                {
                    "product_id": "BTC-USD",
                    "base_currency_id": "BTC",
                    "quote_currency_id": "USD",
                    "price_increment": "0.01",
                    "quote_increment": "0.001",
                }
            ]
        }
    )[0]
    assert (instrument.base, instrument.quote, instrument.tick_size) == ("BTC", "USD", 0.01)


async def test_coinbase_instrument_discovery_uses_protocol_rest_endpoints(monkeypatch):
    from crocodile.crypto.exchanges.coinbase.connector import CoinbaseConnector

    conn = CoinbaseConnector(["BTC-USD"], ["book_delta"], MemorySink(), InstrumentRegistry())
    calls = []

    async def products(url, params=None):
        calls.append(url)
        return [
            {
                "id": "BTC-USD",
                "base_currency": "BTC",
                "quote_currency": "USD",
                "quote_increment": "0.01",
            }
        ]

    monkeypatch.setattr(conn, "http_get", products)
    assert (await conn.list_instruments())[0].symbol_raw == "BTC-USD"
    assert calls == ["https://api.exchange.coinbase.com/products"]


@pytest.mark.parametrize("source", ["coinbase", "dydx", "bybit"])
async def test_native_gap_reconnect_capture_fixture_replays_identically(source, tmp_path):
    from crocodile.core.capture.native import replay_decoders, stream_context
    from crocodile.core.errors import TransientConnectorError
    from crocodile.crypto.exchanges.factory import make_connector

    symbol = "BTCUSDT" if source == "bybit" else "BTC-USD"
    kwargs = {"mode": "advanced"} if source == "coinbase" else {}
    sink = MemorySink()
    conn = make_connector(source, [symbol], ["book_delta"], sink, InstrumentRegistry(), **kwargs)
    writer = await CaptureWriter.create(
        tmp_path, CaptureContext(capture_id="recover", streams=(stream_context(conn, "s1"),))
    )
    conn.capture, conn.capture_stream_id = writer, "s1"

    def message(snapshot, counter, qty):
        if source == "coinbase":
            return {
                "channel": "l2_data",
                "sequence_num": counter,
                "timestamp": None,
                "events": [
                    {
                        "type": "snapshot" if snapshot else "update",
                        "product_id": symbol,
                        "updates": [{"side": "bid", "price_level": "100", "new_quantity": qty}],
                    }
                ],
            }
        if source == "dydx":
            return {
                "type": "subscribed" if snapshot else "channel_data",
                "channel": "v4_orderbook",
                "id": symbol,
                "message_id": counter,
                "connection_id": "subscription",
                "contents": {"bids": [["100", qty]], "asks": []},
            }
        return {
            "type": "snapshot" if snapshot else "delta",
            "topic": f"orderbook.50.{symbol}",
            "cts": 1,
            "data": {
                "s": symbol,
                "u": counter + 10,
                "seq": counter + 100,
                "b": [["100", qty]],
                "a": [],
            },
        }

    first = [message(True, 0, "1"), message(False, 1, "2"), message(False, 3, "3")]
    if source == "bybit":
        first[-1]["data"]["u"] = 1  # reset; fast u does not otherwise require +1
    conn.transport = FakeTransport([json.dumps(msg).encode() for msg in first])
    try:
        with pytest.raises(TransientConnectorError):
            await conn.run(max_reconnects=0)
        second = [message(False, 0, "999"), message(True, 1, "4"), message(False, 2, "5")]
        conn.transport = FakeTransport([json.dumps(msg).encode() for msg in second])
        await conn.run(max_reconnects=0)
    finally:
        await writer.close()
    replayed = list(RawReplay(CaptureReader(tmp_path), replay_decoders()).records())
    assert [msgspec.msgpack.encode(r) for r in replayed] == [
        msgspec.msgpack.encode(r) for r in sink.records
    ]
    assert len(sink.records) == 4
    assert sink.records[-1].bids == [(100.0, 5.0)]


async def test_kraken_authenticated_l3_fixture_capture_replay_and_deletion(monkeypatch, tmp_path):
    import zlib

    from crocodile.core.capture.native import replay_decoders, stream_context
    from crocodile.crypto.exchanges.kraken.connector import KrakenConnector

    sink = MemorySink()
    conn = KrakenConnector(
        ["BTC/USD"],
        ["book_delta"],
        sink,
        InstrumentRegistry(),
        mode="level3",
        api_key="KEY",
        api_secret="c2VjcmV0",
    )

    async def token():
        return "L3_TOKEN_SENTINEL"

    monkeypatch.setattr(conn, "_token", token)
    writer = await CaptureWriter.create(
        tmp_path, CaptureContext(capture_id="l3", streams=(stream_context(conn, "s1"),))
    )
    conn.capture = writer
    snapshot = kraken(
        "snapshot",
        [{"order_id": "first", "limit_price": "100", "order_qty": "0.1"}],
        zlib.crc32(b"1001"),
    )
    modify = kraken(
        "update",
        [{"event": "modify", "order_id": "first", "limit_price": "100", "order_qty": "0.3"}],
        zlib.crc32(b"1003"),
    )
    remove = kraken("update", [{"event": "delete", "order_id": "first"}], zlib.crc32(b""))
    conn.transport = FakeTransport(
        [
            json.dumps(msg).encode()
            for msg in [
                {"method": "subscribe", "result": {"token": "L3_TOKEN_SENTINEL"}},
                snapshot,
                modify,
                remove,
            ]
        ]
    )
    try:
        await conn.run(max_reconnects=0)
    finally:
        await writer.close()
    replayed = list(RawReplay(CaptureReader(tmp_path), replay_decoders()).records())
    assert [msgspec.msgpack.encode(r) for r in replayed] == [
        msgspec.msgpack.encode(r) for r in sink.records
    ]
    assert sink.records[-1].bids == [(100.0, 0.0)]
    assert all(record.prov is Provenance.DERIVED for record in sink.records)
    assert all(
        b"L3_TOKEN_SENTINEL" not in file.read_bytes()
        for file in tmp_path.rglob("*")
        if file.is_file()
    )
