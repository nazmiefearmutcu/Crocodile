from struct import pack, pack_into

import pytest

from crocodile.crypto.exchanges.binance.sbe import (
    SbeBestBidAsk,
    SbeDepthDiff,
    SbeDepthSnapshot,
    SbeProtocolError,
    SbeTrades,
    UnsupportedSbeTemplate,
    decode_sbe_frame,
)


def test_four_templates_preserve_integer_fields(sbe_fixture):
    trade = decode_sbe_frame(sbe_fixture("trades.bin"))
    assert isinstance(trade, SbeTrades)
    assert (trade.symbol_raw, trade.price_exponent, trade.qty_exponent) == ("BTCUSDT", -2, -3)
    assert (
        trade.trades[0].trade_id,
        trade.trades[0].price_mantissa,
        trade.trades[0].qty_mantissa,
        trade.trades[0].is_buyer_maker,
    ) == (1, 3000012, 1250, True)
    assert trade.transact_time_us == 1700000000001000
    bbo = decode_sbe_frame(sbe_fixture("best_bid_ask.bin"))
    assert isinstance(bbo, SbeBestBidAsk)
    assert (bbo.book_update_id, bbo.bid_price_mantissa, bbo.ask_price_mantissa) == (
        100,
        3000012,
        3000013,
    )
    snap = decode_sbe_frame(sbe_fixture("depth_snapshot.bin"))
    assert isinstance(snap, SbeDepthSnapshot)
    assert snap.bids == ((3000012, 1250),) and snap.asks == ()
    diff = decode_sbe_frame(sbe_fixture("depth_diff.bin"))
    assert isinstance(diff, SbeDepthDiff)
    assert (diff.first_update_id, diff.final_update_id) == (99, 101)
    assert diff.bids == ((3000012, 0),)


@pytest.mark.parametrize(
    "name", ["trades.bin", "best_bid_ask.bin", "depth_snapshot.bin", "depth_diff.bin"]
)
def test_every_truncated_prefix_fails(name, sbe_fixture):
    payload = sbe_fixture(name)
    for size in range(len(payload)):
        with pytest.raises(SbeProtocolError):
            decode_sbe_frame(payload[:size])


def test_root_and_group_extensions_multitrade_utf8_and_signed_positive_exponent():
    symbol = "BTCİ".encode()
    payload = pack("<HHHH", 20, 10000, 1, 0) + pack("<qqbb", 1, 2, 2, -3) + b"EX"
    payload += pack("<HI", 27, 2)
    payload += pack("<qqqB", 1, 30, 0, 0) + b"EX" + pack("<qqqB", 2, 31, 2, 1) + b"EX"
    payload += bytes([len(symbol)]) + symbol
    result = decode_sbe_frame(payload)
    assert result.symbol_raw == "BTCİ"
    assert result.price_exponent == 2
    assert len(result.trades) == 2 and result.trades[1].trade_id == 2


@pytest.mark.parametrize("offset,value", [(4, 999), (6, 1), (0, 17)])
def test_unknown_schema_version_and_short_root(offset, value, sbe_fixture):
    payload = bytearray(sbe_fixture("trades.bin"))
    pack_into("<H", payload, offset, value)
    with pytest.raises(SbeProtocolError):
        decode_sbe_frame(payload)


def test_unknown_template_and_unbounded_group_count(sbe_fixture):
    payload = bytearray(sbe_fixture("trades.bin"))
    pack_into("<H", payload, 2, 50000)
    with pytest.raises(UnsupportedSbeTemplate):
        decode_sbe_frame(payload)
    payload = bytearray(sbe_fixture("trades.bin"))
    pack_into("<I", payload, 28, 2**31 - 1)
    with pytest.raises(SbeProtocolError, match="group"):
        decode_sbe_frame(payload)
    payload = bytearray(sbe_fixture("trades.bin"))
    payload[56] = 2
    with pytest.raises(SbeProtocolError, match="bool"):
        decode_sbe_frame(payload)
