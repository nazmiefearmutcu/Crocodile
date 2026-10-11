import msgspec
import pytest

from crocodile.core.schema.enums import Side
from crocodile.core.sink.memory import MemorySink
from crocodile.crypto.exchanges.binance.connector import BinanceConnector, build_sbe_channels
from crocodile.crypto.exchanges.binance.normalize_sbe import normalize_sbe_message, to_depth_event
from crocodile.crypto.exchanges.binance.sbe import SbeProtocolError, decode_sbe_frame
from crocodile.crypto.instruments.registry import InstrumentRegistry


def test_sbe_identity_trade_side_and_timestamp(sbe_fixture):
    event = decode_sbe_frame(sbe_fixture("trades.bin"))
    (rec,) = normalize_sbe_message(event, local_ts=7, registry=InstrumentRegistry())
    assert (rec.source, rec.symbol) == ("binance-spot", "binance-spot:BTCUSDT")
    assert (rec.side, rec.source_ts, rec.local_ts) == (Side.SELL, 1700000000001000000, 7)
    assert rec.price == 30000.12 and rec.amount == 1.25
    buy = msgspec.structs.replace(
        event, trades=(msgspec.structs.replace(event.trades[0], is_buyer_maker=False),)
    )
    assert (
        next(iter(normalize_sbe_message(buy, local_ts=8, registry=InstrumentRegistry()))).side
        == Side.BUY
    )


def test_depth_preserves_range_delete_and_microseconds(sbe_fixture):
    event = decode_sbe_frame(sbe_fixture("depth_diff.bin"))
    depth = to_depth_event(event)
    assert (depth.U, depth.u, depth.pu) == (99, 101, None)
    assert depth.bids == ((30000.12, 0.0),)
    assert depth.source_ts_ns == 1700000000000000000


def test_channels_deduplicate_full_depth_and_do_not_request_top20():
    assert build_sbe_channels(
        ["BTCUSDT"], ["book_delta", "book_snapshot", "trade", "book_ticker"]
    ) == ["btcusdt@depth", "btcusdt@trade", "btcusdt@bestBidAsk"]


def test_unrepresentable_canonical_value_fails(sbe_fixture):
    event = decode_sbe_frame(sbe_fixture("best_bid_ask.bin"))
    huge = msgspec.structs.replace(event, price_exponent=999)
    with pytest.raises(SbeProtocolError):
        list(normalize_sbe_message(huge, local_ts=1, registry=InstrumentRegistry()))


@pytest.mark.asyncio
async def test_partial_snapshot_cannot_reset_a_full_book(sbe_fixture):
    conn = BinanceConnector(
        ["BTCUSDT"],
        ["book_delta"],
        MemorySink(),
        InstrumentRegistry(),
        wire_format="sbe",
        api_key="fixture-key",
    )
    with pytest.raises(SbeProtocolError, match="partial"):
        await conn._handle_message(decode_sbe_frame(sbe_fixture("depth_snapshot.bin")), 1)
    assert conn.out.records == []


@pytest.mark.asyncio
async def test_sbe_transport_sends_only_key_header():
    from unittest.mock import ANY, AsyncMock, MagicMock, patch

    conn = BinanceConnector(
        ["BTCUSDT"],
        ["trade"],
        MemorySink(),
        InstrumentRegistry(),
        wire_format="sbe",
        api_key="fixture-key",
    )
    session = MagicMock()
    session.ws_connect, session.close = AsyncMock(return_value=MagicMock()), AsyncMock()
    with patch("aiohttp.ClientSession", return_value=session):
        transport = conn.make_transport()
        await transport.connect()
        await transport.close()
    session.ws_connect.assert_awaited_once_with(
        "wss://stream-sbe.binance.com/ws",
        heartbeat=20.0,
        ssl=ANY,
        headers={"X-MBX-APIKEY": "fixture-key"},
    )


@pytest.mark.parametrize(
    "kwargs", [{"market": "usdm"}, {"market": "coinm"}, {"api_key": ""}, {"wire_format": "invalid"}]
)
def test_invalid_sbe_configuration_refuses_before_transport(kwargs):
    from crocodile.core.errors import ConfigError

    config = {"wire_format": "sbe", "api_key": "fixture-key"} | kwargs
    with pytest.raises(ConfigError):
        BinanceConnector(["BTCUSDT"], ["book_delta"], MemorySink(), InstrumentRegistry(), **config)
