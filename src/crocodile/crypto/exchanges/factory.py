"""Connector factory — maps exchange name to the correct Connector subclass.

Usage::

    from crocodile.crypto.exchanges.factory import make_connector

    conn = make_connector(
        exchange="binance",
        symbols=["BTCUSDT"],
        channels=["trade", "book_ticker"],
        out=sink,
        registry=registry,
        market="usdm",          # forwarded as **kw to BinanceConnector
    )

Valid exchange names: ``base_onchain``, ``binance``, ``bitfinex``, ``bybit``, ``coinbase``,
``coingecko``, ``deribit``, ``derive``, ``dydx``, ``gmx_synthetix``, ``kraken``,
``okx``, ``superchain``.
That list is ``_REGISTRY`` below spelled out, and
``tests/conformance/test_prose_counts.py`` asserts the two agree, because the
version of this sentence that omitted ``coingecko`` read as a complete
enumeration and was not one.
Extra keyword arguments (e.g. ``market`` for Binance, ``category`` for Bybit,
``region`` for OKX, ``rpc_url`` / ``viewer_address`` for Derive) are
forwarded to the connector constructor unchanged.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from crocodile.core.config import Settings
from crocodile.core.connector import Connector
from crocodile.core.sink.base import Sink
from crocodile.crypto.instruments.registry import InstrumentRegistry

# Connectors are imported lazily so that pulling in one exchange never pays
# for another's dependency tree (e.g. derive -> analytics -> numpy).
_REGISTRY: dict[str, tuple[str, str]] = {
    "binance": ("crocodile.crypto.exchanges.binance.connector", "BinanceConnector"),
    "bitfinex": ("crocodile.crypto.exchanges.bitfinex.connector", "BitfinexConnector"),
    "kraken": ("crocodile.crypto.exchanges.kraken.connector", "KrakenConnector"),
    "dydx": ("crocodile.crypto.exchanges.dydx.connector", "DydxConnector"),
    "bybit": ("crocodile.crypto.exchanges.bybit.connector", "BybitConnector"),
    "coinbase": ("crocodile.crypto.exchanges.coinbase.connector", "CoinbaseConnector"),
    "deribit": ("crocodile.crypto.exchanges.deribit.connector", "DeribitConnector"),
    "derive": ("crocodile.crypto.exchanges.derive.connector", "DerivePollConnector"),
    "okx": ("crocodile.crypto.exchanges.okx.connector", "OKXConnector"),
    "base_onchain": ("crocodile.crypto.exchanges.base_onchain.connector", "BaseOnchainConnector"),
    "gmx_synthetix": (
        "crocodile.crypto.exchanges.gmx_synthetix.connector",
        "GMXSynthetixConnector",
    ),
    "superchain": ("crocodile.crypto.exchanges.superchain.connector", "SuperchainConnector"),
    "coingecko": ("crocodile.crypto.exchanges.coingecko.connector", "CoinGeckoConnector"),
}


_VALID_NAMES = sorted(_REGISTRY)

# Connectors whose lazily-imported modules require an optional dependency
# group ("extra").  Used to turn a bare ModuleNotFoundError into an
# actionable install hint.
_CONNECTOR_EXTRAS: dict[str, str] = {
    "base_onchain": "onchain",
    "derive": "onchain",
    "gmx_synthetix": "onchain",
    "superchain": "onchain",
}


def list_exchanges() -> list[str]:
    """Sorted names of the **native** (hand-written) connectors.

    Kept native-only for backward compatibility (help text, existing tests).
    For the full set including every ccxt venue, use :func:`list_all_exchanges`.
    """
    return list(_VALID_NAMES)


def list_ccxt_exchanges() -> list[str]:
    """Sorted ccxt exchange ids available via the universal connector.

    Returns ``[]`` when ccxt is not installed (the ``market`` extra), so callers
    can treat "no ccxt" as simply "no extra venues" rather than an error.
    """
    try:
        import ccxt
    except ModuleNotFoundError:
        return []
    return sorted(ccxt.exchanges)


def list_all_exchanges() -> list[str]:
    """Sorted union of native connectors and every ccxt venue.

    Native names take precedence conceptually (they route to the higher-fidelity
    hand-written connector), but a name appearing in both is listed once.
    """
    return sorted(set(_VALID_NAMES) | set(list_ccxt_exchanges()))


def is_ccxt_exchange(name: str) -> bool:
    """True when *name* is served by the universal ccxt connector (and not native)."""
    if name in _REGISTRY:
        return False
    return name in set(list_ccxt_exchanges())


def make_connector(
    exchange: str,
    symbols: list[str],
    channels: list[str],
    out: Sink,
    registry: InstrumentRegistry,
    *,
    settings: Settings | None = None,
    **kw: Any,
) -> Connector:
    """Instantiate and return the correct :class:`~.base.Connector` subclass.

    Parameters
    ----------
    exchange:
        Lowercase exchange name.  Valid values: ``base_onchain``, ``binance``,
        ``bitfinex``, ``bybit``, ``coinbase``, ``coingecko``, ``deribit``, ``derive``,
        ``dydx``, ``gmx_synthetix``, ``kraken``, ``okx``, ``superchain``.  Any other name falls
        through to the universal ccxt connector rather than raising here.
    symbols:
        List of symbol strings to subscribe to (exchange-native format).
    channels:
        List of canonical channel names (e.g. ``"trade"``, ``"book_ticker"``).
    out:
        Sink to receive normalised records.
    registry:
        Instrument registry for symbol resolution.
    **kw:
        Extra keyword arguments forwarded verbatim to the connector constructor
        (e.g. ``market="usdm"`` for Binance, ``category="spot"`` for Bybit,
        ``region="us"`` for OKX, ``rpc_url`` / ``viewer_address`` for Derive).

    Raises
    ------
    ValueError
        If *exchange* is not a recognised name.  The error message lists all
        valid names.
    """
    if settings is not None and exchange == "binance":
        kw.setdefault("market", settings.binance_market)
        kw.setdefault("wire_format", settings.binance_wire_format)
        kw.setdefault("api_key", settings.binance_sbe_api_key)
    if settings is not None and exchange == "deribit":
        kw.setdefault("interval", settings.deribit_interval)
        kw.setdefault("client_id", settings.deribit_client_id)
        kw.setdefault("client_secret", settings.deribit_client_secret)
    if settings is not None and exchange == "coinbase":
        kw.setdefault("mode", settings.coinbase_mode)
    if settings is not None and exchange == "bybit":
        kw.setdefault("book_mode", settings.bybit_book_mode)
        if settings.bybit_book_depth is not None:
            kw.setdefault("book_depth", _depth(settings.bybit_book_depth))
    if settings is not None and exchange == "kraken":
        kw.setdefault("mode", settings.kraken_mode)
        kw.setdefault("depth", _depth(settings.kraken_depth))
        kw.setdefault("api_key", settings.kraken_api_key)
        kw.setdefault("api_secret", settings.kraken_api_secret)
    entry = _REGISTRY.get(exchange)
    if entry is None:
        # Not a native connector — fall through to the universal ccxt venue set.
        return _make_ccxt_connector(exchange, symbols, channels, out, registry, **kw)
    module_path, class_name = entry
    # module_path is a fixed value from the literal `_REGISTRY` whitelist above
    # (never raw user input — unknown keys raise ValueError before this point),
    # so the dynamic import cannot load attacker-controlled modules.
    try:
        cls: type[Connector] = getattr(import_module(module_path), class_name)  # nosemgrep
    except ModuleNotFoundError as e:
        extra = _CONNECTOR_EXTRAS.get(exchange)
        if extra is not None:
            raise ModuleNotFoundError(
                f"Connector {exchange!r} requires optional dependencies "
                f"(missing: {e.name}) — install with: pip install 'crocodile[{extra}]'"
            ) from e
        raise
    return cls(
        symbols=symbols,
        channels=channels,
        out=out,
        registry=registry,
        **kw,
    )


def _depth(value: str) -> int:
    from crocodile.core.errors import ConfigError

    try:
        return int(value)
    except (ValueError, TypeError):
        raise ConfigError("Native book depth must be an integer") from None


def _make_ccxt_connector(
    exchange: str,
    symbols: list[str],
    channels: list[str],
    out: Sink,
    registry: InstrumentRegistry,
    **kw: Any,
) -> Connector:
    """Build a :class:`CCXTConnector` for any ccxt exchange id.

    Raises
    ------
    ModuleNotFoundError
        When ccxt is not installed — with the ``crocodile[market]`` install hint.
    ValueError
        When *exchange* is neither a native connector nor a ccxt exchange id.
    """
    try:
        import ccxt
    except ModuleNotFoundError as e:
        raise ModuleNotFoundError(
            f"Exchange {exchange!r} is served by the universal ccxt connector, "
            f"which needs the 'market' extra — install with: "
            f"pip install 'crocodile[market]'"
        ) from e

    if exchange not in ccxt.exchanges:
        raise ValueError(
            f"Unknown exchange {exchange!r}. Native: {_VALID_NAMES}. "
            f"Or pass any of the {len(ccxt.exchanges)} ccxt exchange ids "
            f"(see `crypcodile markets`)."
        )

    from crocodile.crypto.exchanges.ccxt_universal.connector import CCXTConnector

    return CCXTConnector(
        symbols=symbols,
        channels=channels,
        out=out,
        registry=registry,
        ccxt_id=exchange,
        **kw,
    )
