"""A connector must register instruments under the key its own live path looks up.

The registry keys on ``(Instrument.exchange, Instrument.symbol_raw)``; the normalizer
looks up ``registry.get_raw(venue, raw)`` where ``venue`` is whatever that connector
stamps into ``Record.source``.  Nothing in the type system ties the two together, and
when they drifted apart the failure was silent: the normalizer's fallback canonical
(``f"{venue}:{raw}"``) kept records flowing, so prices looked fine while every lookup
missed and no tick size, base/quote or :class:`Kind` ever reached a consumer.

That is exactly what ``BinanceConnector`` did — it registered under the bare
``"binance"`` while normalizing (and looking up) under ``"binance-spot"`` /
``"binance-usdm"``.  So these tests drive the real thing end to end rather than
asserting a spelling: enumerate the connector's instruments, register them, push a
real wire fixture through ``connector.normalize``, and require that the record that
comes out resolves in the registry **under the source it itself carries**.  A
connector whose registration key and lookup key disagree fails here no matter which
spelling either one picks.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from crocodile.core.schema.records import Record
from crocodile.core.sink.memory import MemorySink
from crocodile.crypto.instruments.registry import Instrument, InstrumentRegistry

FIX = pathlib.Path(__file__).parent


def _load(*parts: str) -> Any:
    return json.loads((FIX.joinpath(*parts)).read_text())


def _patch_http_get(conn: Any, payload: Any) -> None:
    """Make every REST call on *conn* return *payload* (no network)."""

    async def fake_http_get(url: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        return payload

    conn.http_get = fake_http_get  # type: ignore[method-assign]


def _assert_live_lookup_resolves(
    registry: InstrumentRegistry,
    records: list[Record],
    *,
    expect_raw: str,
) -> Instrument:
    """The record's own ``source`` + ``symbol_raw`` must find the registered instrument.

    ``source`` is read off the record rather than passed in because it is the venue the
    normalizer actually used; hardcoding it would let a connector rename its venue and
    keep the two halves disagreeing while this still passed.
    """
    assert records, "fixture produced no records — the test is not exercising anything"
    rec = next((r for r in records if r.symbol_raw == expect_raw), None)
    assert rec is not None, f"no record for {expect_raw!r} in {[r.symbol_raw for r in records]}"

    inst = registry.get_raw(rec.source, rec.symbol_raw)
    assert inst is not None, (
        f"registry miss: {type(rec).__name__} carries source={rec.source!r} "
        f"symbol_raw={rec.symbol_raw!r}, but the instruments were registered under "
        f"{sorted({i.exchange for i in registry._by_raw.values()})} — registration and "
        f"lookup disagree, so no instrument metadata can ever reach a consumer"
    )
    # The hit must also be the canonical the record carries, or a registry that resolves
    # still emits a different symbol string than the one it resolved to.
    assert rec.symbol == inst.canonical
    return inst


# ---------------------------------------------------------------------------
# Binance — the connector that had the bug (spot and USD-M)
# ---------------------------------------------------------------------------

_BINANCE_SPOT_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "status": "TRADING",
            "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"}],
        }
    ]
}

_BINANCE_USDM_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.10"}],
        }
    ]
}


async def _binance_registry(market: str, info: dict[str, Any]) -> tuple[Any, InstrumentRegistry]:
    from crocodile.crypto.exchanges.binance.connector import BinanceConnector

    registry = InstrumentRegistry()
    conn = BinanceConnector(
        symbols=["BTCUSDT"],
        channels=["trade"],
        out=MemorySink(),
        registry=registry,
        market=market,
    )
    _patch_http_get(conn, info)
    for inst in await conn.list_instruments():
        registry.add(inst)
    return conn, registry


async def test_binance_spot_registration_matches_live_lookup() -> None:
    conn, registry = await _binance_registry("spot", _BINANCE_SPOT_INFO)
    msg = _load("binance", "fixtures", "spot_aggtrade.json")
    records = list(conn.normalize(msg, local_ts=1))
    inst = _assert_live_lookup_resolves(registry, records, expect_raw="BTCUSDT")
    # Metadata genuinely arrived — the thing the silent miss was withholding.
    assert inst.tick_size == 0.01
    assert (inst.base, inst.quote) == ("BTC", "USDT")


async def test_binance_usdm_registration_matches_live_lookup() -> None:
    conn, registry = await _binance_registry("usdm", _BINANCE_USDM_INFO)
    msg = _load("binance", "fixtures", "usdm_markprice.json")
    records = list(conn.normalize(msg, local_ts=1))
    inst = _assert_live_lookup_resolves(registry, records, expect_raw="BTCUSDT")
    assert inst.tick_size == 0.10


async def test_binance_spot_and_usdm_do_not_collide_in_one_registry() -> None:
    """Same raw symbol, two markets, one shared registry — both must survive.

    ``collect`` hands every connector the same :class:`InstrumentRegistry`, so keying on
    the bare exchange name is not merely inconsistent with the lookup: it is not unique.
    Spot ``BTCUSDT`` and USD-M ``BTCUSDT`` are different instruments with different tick
    sizes, and one would have overwritten the other.
    """
    from crocodile.crypto.exchanges.binance.connector import BinanceConnector

    registry = InstrumentRegistry()
    conns = []
    for market, info in (("spot", _BINANCE_SPOT_INFO), ("usdm", _BINANCE_USDM_INFO)):
        conn = BinanceConnector(
            symbols=["BTCUSDT"],
            channels=["trade"],
            out=MemorySink(),
            registry=registry,
            market=market,
        )
        _patch_http_get(conn, info)
        for inst in await conn.list_instruments():
            registry.add(inst)
        conns.append(conn)

    spot = registry.get_raw(conns[0]._venue, "BTCUSDT")
    usdm = registry.get_raw(conns[1]._venue, "BTCUSDT")
    assert spot is not None and usdm is not None
    assert spot.tick_size == 0.01
    assert usdm.tick_size == 0.10
    assert spot.canonical != usdm.canonical


# ---------------------------------------------------------------------------
# The other native connectors — asserted the same way, so a future market/category/
# region tag on any of them cannot reintroduce the split unnoticed.
# ---------------------------------------------------------------------------


async def test_bybit_registration_matches_live_lookup() -> None:
    from crocodile.crypto.exchanges.bybit.connector import BybitConnector

    registry = InstrumentRegistry()
    conn = BybitConnector(
        symbols=["BTCUSDT"],
        channels=["trade"],
        out=MemorySink(),
        registry=registry,
        category="linear",
    )
    _patch_http_get(
        conn,
        {
            "result": {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "baseCoin": "BTC",
                        "quoteCoin": "USDT",
                        "settleCoin": "USDT",
                        "contractType": "LinearPerpetual",
                        "priceFilter": {"tickSize": "0.10"},
                    }
                ],
                "nextPageCursor": "",
            }
        },
    )
    for inst in await conn.list_instruments():
        registry.add(inst)

    msg = _load("bybit", "fixtures", "trade.json")
    records = list(conn.normalize(msg, local_ts=1))
    inst = _assert_live_lookup_resolves(registry, records, expect_raw="BTCUSDT")
    assert inst.tick_size == 0.10


async def test_okx_registration_matches_live_lookup() -> None:
    from crocodile.crypto.exchanges.okx.connector import OKXConnector

    registry = InstrumentRegistry()
    conn = OKXConnector(
        symbols=["BTC-USDT"],
        channels=["trade"],
        out=MemorySink(),
        registry=registry,
        region="global",
    )
    _patch_http_get(
        conn,
        {
            "code": "0",
            "data": [
                {
                    "instType": "SPOT",
                    "instId": "BTC-USDT",
                    "baseCcy": "BTC",
                    "quoteCcy": "USDT",
                    "tickSz": "0.1",
                }
            ],
            "msg": "",
        },
    )
    for inst in await conn.list_instruments():
        registry.add(inst)

    msg = _load("okx", "fixtures", "trade.json")
    records = list(conn.normalize(msg, local_ts=1))
    inst = _assert_live_lookup_resolves(registry, records, expect_raw="BTC-USDT")
    assert inst.tick_size == 0.1


async def test_deribit_registration_matches_live_lookup() -> None:
    from crocodile.crypto.exchanges.deribit.connector import DeribitConnector

    registry = InstrumentRegistry()
    conn = DeribitConnector(
        symbols=["BTC-PERPETUAL"],
        channels=["trade"],
        out=MemorySink(),
        registry=registry,
    )
    _patch_http_get(conn, _load("deribit", "fixtures", "get_instruments.json"))
    for inst in await conn.list_instruments():
        registry.add(inst)

    msg = _load("deribit", "fixtures", "trades.json")
    records = list(conn.normalize(msg, local_ts=1))
    inst = _assert_live_lookup_resolves(registry, records, expect_raw="BTC-PERPETUAL")
    assert inst.tick_size == 0.5


async def test_coinbase_registration_matches_live_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Coinbase enumerates over its own ``aiohttp`` session, so the session is faked."""
    from crocodile.crypto.exchanges.coinbase import connector as cb

    products = _load("coinbase", "fixtures", "products.json")

    class _FakeResponse:
        def raise_for_status(self) -> None: ...

        async def json(self) -> Any:
            return products

        async def __aenter__(self) -> _FakeResponse:
            return self

        async def __aexit__(self, *exc: object) -> None: ...

    class _FakeSession:
        def get(self, url: str, **kw: Any) -> _FakeResponse:
            return _FakeResponse()

        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *exc: object) -> None: ...

    monkeypatch.setattr(cb.aiohttp, "ClientSession", lambda *a, **k: _FakeSession())

    registry = InstrumentRegistry()
    conn = cb.CoinbaseConnector(
        symbols=["BTC-USD"],
        channels=["trade"],
        out=MemorySink(),
        registry=registry,
    )
    for inst in await conn.list_instruments():
        registry.add(inst)

    msg = _load("coinbase", "fixtures", "match.json")
    records = list(conn.normalize(msg, local_ts=1))
    inst = _assert_live_lookup_resolves(registry, records, expect_raw="BTC-USD")
    assert inst.tick_size == 0.01


async def test_coingecko_poll_cycle_resolves_its_own_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CoinGecko has no WS frame; its live path is ``_poll_once``, which does both halves.

    Driving the poll cycle rather than a fixture means registration and lookup are the
    same two calls the collector makes, one after the other.
    """
    from crocodile.crypto.exchanges.coingecko import connector as cg

    coins = [
        {
            "id": "bitcoin",
            "symbol": "btc",
            "current_price": 50000.0,
            "high_24h": 51000.0,
            "low_24h": 49000.0,
            "price_change_percentage_24h": 1.0,
            "total_volume": 1234.0,
        }
    ]

    async def fake_fetch_markets(session: Any, **kw: Any) -> list[dict[str, Any]]:
        return coins

    monkeypatch.setattr(cg, "fetch_markets", fake_fetch_markets)

    registry = InstrumentRegistry()
    sink = MemorySink()
    conn = cg.CoinGeckoConnector(
        symbols=[], channels=["ohlcv"], out=sink, registry=registry
    )
    written = await conn._poll_once()
    assert written == 1
    _assert_live_lookup_resolves(registry, list(sink.records), expect_raw="bitcoin")
