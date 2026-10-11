import pytest

from crocodile.core.config import Settings
from crocodile.core.errors import ConfigError
from crocodile.core.sink.memory import MemorySink
from crocodile.crypto.exchanges.deribit.connector import DeribitConnector, build_channels
from crocodile.crypto.exchanges.factory import make_connector
from crocodile.crypto.instruments.registry import InstrumentRegistry


def make(**kwargs):
    return DeribitConnector(
        ["BTC-PERPETUAL"], ["book_delta"], MemorySink(), InstrumentRegistry(), **kwargs
    )


def test_raw_channels_and_default():
    assert build_channels(["BTC-PERPETUAL"], ["book_delta", "book_snapshot"], interval="raw") == [
        "book.BTC-PERPETUAL.raw"
    ]
    assert make().subscribe_channels() == ["book.BTC-PERPETUAL.100ms"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"interval": "bad"},
        {"interval": "raw"},
        {"interval": "raw", "client_id": "id"},
        {"interval": "raw", "client_id": " ", "client_secret": "secret"},
        {"heartbeat_interval_seconds": 9},
        {"request_timeout_seconds": 0},
        {"request_timeout_seconds": float("nan")},
        {"refresh_margin_seconds": -1},
    ],
)
def test_invalid_raw_config_is_rejected_before_connect(kwargs):
    with pytest.raises(ConfigError):
        make(**kwargs)


def test_env_factory_and_redaction():
    settings = Settings.from_env(
        {
            "CROCODILE_DERIBIT_INTERVAL": "raw",
            "CROCODILE_DERIBIT_CLIENT_ID": "ID_SENTINEL",
            "CROCODILE_DERIBIT_CLIENT_SECRET": "SECRET_SENTINEL",
        }
    )
    assert settings.deribit_interval == "raw"
    assert "ID_SENTINEL" not in repr(settings)
    assert "SECRET_SENTINEL" not in repr(settings)
    conn = make_connector(
        "deribit",
        ["BTC-PERPETUAL"],
        ["book_delta"],
        MemorySink(),
        InstrumentRegistry(),
        settings=settings,
    )
    assert conn.interval == "raw"
    conn = make_connector(
        "deribit", [], [], MemorySink(), InstrumentRegistry(), settings=settings, interval="100ms"
    )
    assert conn.interval == "100ms"
    assert (
        make_connector("bybit", [], [], MemorySink(), InstrumentRegistry(), settings=settings).name
        == "bybit"
    )
