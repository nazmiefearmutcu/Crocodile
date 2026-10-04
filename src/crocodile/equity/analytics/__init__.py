"""Analytics module for Stockodile.

Includes functions for returns, risk metrics, option pricing, and fundamental ratios.

The five indicator primitives are re-exported from :mod:`crocodile.core.analytics.
indicators` rather than defined here. They used to be a near-copy of that module which
differed from it on RSI warm-up and Bollinger deviation, so ``from crocodile.equity.
analytics import calculate_rsi`` and ``from crocodile.core.analytics.indicators import
calculate_rsi`` returned different numbers under one name — the collision that stops a
single ``indicators`` capability being declared. Core adopted this side's arithmetic;
the import path stays so callers do not have to move.
"""

from crocodile.core.analytics.indicators import (
    calculate_bollinger_bands,
    calculate_ema,
    calculate_macd,
    calculate_rsi,
    calculate_sma,
)
from crocodile.core.analytics.slippage import estimate_slippage
from crocodile.equity.analytics.dark_pool import (
    build_dark_pool_frame,
    dark_pool_market_share,
)
from crocodile.equity.analytics.fails_to_deliver import (
    build_ftd_frame,
    detect_ftd_spikes,
    ftd_summary,
)
from crocodile.equity.analytics.metrics import (
    calculate_beta,
    calculate_realized_volatility,
)
from crocodile.equity.analytics.options import (
    bsm_delta,
    bsm_gamma,
    bsm_greeks,
    bsm_implied_volatility,
    bsm_price,
    bsm_rho,
    bsm_theta,
    bsm_vega,
)
from crocodile.equity.analytics.ratios import (
    calculate_gross_margin,
    calculate_net_margin,
    calculate_operating_margin,
    calculate_pb_ratio,
    calculate_pe_ratio,
    calculate_roe,
)
from crocodile.equity.analytics.returns import (
    calculate_log_returns,
    calculate_simple_returns,
)
from crocodile.equity.analytics.short_interest import (
    short_interest_history,
    short_interest_metrics,
)
from crocodile.equity.analytics.short_volume import (
    regsho_short_ratio,
    short_volume_summary,
)

__all__ = [
    "bsm_delta",
    "bsm_gamma",
    "bsm_greeks",
    "bsm_implied_volatility",
    "bsm_price",
    "bsm_rho",
    "bsm_theta",
    "bsm_vega",
    "build_dark_pool_frame",
    "build_ftd_frame",
    "calculate_beta",
    "calculate_bollinger_bands",
    "calculate_ema",
    "calculate_gross_margin",
    "calculate_log_returns",
    "calculate_macd",
    "calculate_net_margin",
    "calculate_operating_margin",
    "calculate_pb_ratio",
    "calculate_pe_ratio",
    "calculate_realized_volatility",
    "calculate_roe",
    "calculate_rsi",
    "calculate_simple_returns",
    "calculate_sma",
    "dark_pool_market_share",
    "detect_ftd_spikes",
    "estimate_slippage",
    "ftd_summary",
    "regsho_short_ratio",
    "short_interest_history",
    "short_interest_metrics",
    "short_volume_summary",
]
