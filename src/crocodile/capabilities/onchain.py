"""The two Base-mainnet pool readers, declared because the deletion had nowhere to put them.

This module is not one of the four batches the porting agents filled. It exists because the
surface-parity gate caught something at the end of Phase 2 that the port had missed: three
wire names — the MCP tools ``get_onchain_price`` and ``get_base_market_data`` on *both*
forks, and the equity route ``GET /api/v1/market-data`` — were on the wire before the
deletion and were served by nothing in :data:`~crocodile.core.capability.REGISTRY` after it.

There were three ways out and two of them were false:

- **Call them infrastructure.** ``_INFRASTRUCTURE`` in the parity gate means *never a
  capability*, and it is not true here. These take a parameter, return a measurement of a
  market, and have a provenance: a Uniswap V3 pool's ``slot0`` and an Aerodrome pool's
  ``getReserves`` are the venue reporting its own state, which is exactly what
  :attr:`Provenance.NATIVE` names. Writing "not a capability" on that would be the kind of
  exemption this project's gates exist to refuse.
- **Delete the fixture entry.** The one thing that defeats the gate.
- **Serve them.** This.

They are crypto-only and permanently so, so both are on
:data:`~crocodile.core.capability.IRREDUCIBLE` rather than
:data:`~crocodile.core.capability.PENDING_SYMMETRY`: an automated market maker is a
chain-native price venue, and there is no equity instrument whose price is a function of two
pooled reserves. That is a claim about the market, which is the bar ``IRREDUCIBLE`` sets.

Why two capabilities and not one with a flag: they were two tools, they take differently
spelled inputs, and ``base-market-data`` costs an 1 800-block log scan that ``onchain-price``
does not. Folding them would be a redesign, and what this module is doing is restoring.

The implementations are :func:`crocodile.crypto.exchanges.base_onchain.price.get_onchain_price`
and :func:`~crocodile.crypto.exchanges.base_onchain.price.get_base_market_data`, moved out of
the deleted MCP server into the connector package they read through, unchanged.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

import msgspec

from crocodile.core.capability import (
    AssetClass,
    Capability,
    CapabilityContext,
    Impl,
    ReturnKind,
    declare,
    run_to_completion,
)
from crocodile.core.errors import ConfigError
from crocodile.core.schema.provenance import Provenance

__all__ = ["BASE_MARKET_DATA", "ONCHAIN_PRICE", "BaseMarketDataParams", "OnchainPriceParams"]


class OnchainPriceParams(msgspec.Struct, frozen=True):
    """Which pool to read."""

    symbol: str
    """A pool in ``POOL_SPECS``, e.g. ``WETH-USDC``. Not a canonical ``source:RAW`` symbol:
    these two are the only capabilities that name a *pool* rather than an instrument in the
    lake, because a pool is what the contract being called is."""


class BaseMarketDataParams(msgspec.Struct, frozen=True):
    """Which pool to read, in the spelling the tool has always taken."""

    token_pair: str
    """The same pool as ``onchain-price``'s ``symbol``, written ``WETH/USDC``. The
    implementation folds ``/`` to ``-`` and upper-cases, so both spellings reach one pool;
    the field keeps its own name because that is the name the tool was called with."""


def _require_onchain() -> None:
    try:
        import_module("web3")
    except ModuleNotFoundError as exc:
        if exc.name != "web3":
            raise
        raise ConfigError(
            "On-chain pool data requires: pip install 'crocodile[onchain]'"
        ) from exc


def onchain_price(ctx: CapabilityContext, params: OnchainPriceParams) -> dict[str, Any]:
    """Current price, virtual reserves and pool address for one Base pool.

    ``ctx`` is unused and that is the honest shape: this reads the head block over RPC, not
    the lake, so there is no ``ctx.query`` to make and no stored row behind the answer. It is
    the only pair of capabilities in the registry for which that is true, and it is why the
    ``prov`` below is a claim about a contract call rather than about a Parquet file.

    :func:`~crocodile.core.capability.run_to_completion` rather than the ``asyncio.run`` this
    module shipped with. That call was justified by the claim that "both network surfaces
    call ``dispatch.invoke`` from a worker thread rather than from the event loop", and
    neither does: ``surfaces/rest.py``'s endpoint is ``async def`` and ``surfaces/stdio.py``
    calls ``handle_request`` inside an async body. So these two answered on the CLI, returned
    500 on REST, and raised ``RuntimeError: asyncio.run() cannot be called from a running
    event loop`` on MCP — a capability that works on one surface and fails on another, which
    is the asymmetry the registry exists to end, arriving through the module that was added
    to close a different one.
    """
    _require_onchain()
    from crocodile.crypto.exchanges.base_onchain.price import get_onchain_price

    return run_to_completion(lambda: get_onchain_price(params.symbol))


def base_market_data(ctx: CapabilityContext, params: BaseMarketDataParams) -> dict[str, Any]:
    """``onchain-price``, plus swap volume over the last ~1 800 blocks.

    The extra cost is a log scan, which is why this is not a default on the other one.
    """
    _require_onchain()
    from crocodile.crypto.exchanges.base_onchain.price import get_base_market_data

    return run_to_completion(lambda: get_base_market_data(params.token_pair))


ONCHAIN_PRICE = declare(
    Capability(
        name="onchain-price",
        summary="Spot price, reserves and pool address for one Base mainnet AMM pool.",
        params=OnchainPriceParams,
        returns=ReturnKind.SCALAR,
        impls={
            # NATIVE because the pool contract publishes this: `slot0` on a Uniswap V3 pool
            # and `getReserves` on an Aerodrome one are the venue's own state, read at the
            # head block. The arithmetic on top — sqrtPriceX96 to a price, liquidity to
            # virtual reserves — is the pool's own definition of those quantities rather
            # than a model of them, which is the line that keeps this off DERIVED.
            AssetClass.CRYPTO: Impl(fn=onchain_price, prov=Provenance.NATIVE, basis="native"),
        },
    )
)


BASE_MARKET_DATA = declare(
    Capability(
        name="base-market-data",
        summary="One Base pool's price and reserves, with its swap volume over the last hour.",
        params=BaseMarketDataParams,
        returns=ReturnKind.SCALAR,
        impls={
            AssetClass.CRYPTO: Impl(fn=base_market_data, prov=Provenance.NATIVE, basis="native"),
        },
    )
)
