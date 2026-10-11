"""Public identities shared by native collectors and application bridges."""

from typing import TYPE_CHECKING

from crocodile.core.capture.replay import ReplayDecoder
from crocodile.core.capture.schema import StreamContext
from crocodile.core.errors import ConfigError

if TYPE_CHECKING:
    from crocodile.core.connector import Connector

SUPPORTED_RAW_SOURCES = frozenset(
    {"binance", "deribit", "bitfinex", "kraken", "dydx", "bybit", "coinbase"}
)


def stream_context(connector: "Connector", stream_id: str) -> StreamContext:
    if connector.name not in SUPPORTED_RAW_SOURCES:
        raise ConfigError("Raw capture requires a supported native public connector")
    return StreamContext(
        stream_id=stream_id,
        source=getattr(connector, "_venue", connector.name),
        market=getattr(connector, "market", "derivatives"),
        symbols=tuple(connector.symbols),
        endpoint_id=getattr(connector, "capture_endpoint_id", f"{connector.name}-public"),
        decoder_id=getattr(
            connector,
            "decoder_id",
            getattr(connector, "capture_decoder_id", f"{connector.name}-json"),
        ),
        decoder_version=1,
        schema_id=getattr(connector, "capture_schema_id", None),
        schema_version=getattr(connector, "capture_schema_version", None),
    )


def replay_decoders() -> dict[str, ReplayDecoder]:
    from crocodile.crypto.exchanges.binance.replay import (
        BinanceReplayDecoder,
        BinanceSbeReplayDecoder,
    )
    from crocodile.crypto.exchanges.bitfinex.replay import BitfinexReplayDecoder
    from crocodile.crypto.exchanges.bybit.replay import BybitReplayDecoder
    from crocodile.crypto.exchanges.coinbase.replay import CoinbaseReplayDecoder
    from crocodile.crypto.exchanges.deribit.replay import DeribitReplayDecoder
    from crocodile.crypto.exchanges.dydx.replay import DydxReplayDecoder
    from crocodile.crypto.exchanges.kraken.replay import KrakenReplayDecoder

    return {
        "binance-json": BinanceReplayDecoder(),
        "binance-sbe": BinanceSbeReplayDecoder(),
        "deribit-json": DeribitReplayDecoder(),
        "bitfinex-r0": BitfinexReplayDecoder(),
        "kraken-v2": KrakenReplayDecoder(),
        "dydx-v4": DydxReplayDecoder(),
        "coinbase-json": CoinbaseReplayDecoder(),
        "bybit-json": BybitReplayDecoder(),
    }
