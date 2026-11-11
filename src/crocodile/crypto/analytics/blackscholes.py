"""Black-76 option pricing, greeks, and implied-vol solver (Task 6.1).

Implements the **Black-76** model (European options on a forward/index ``F``
with discount factor ``D = exp(-rate * t_years)``).  This is the correct
model for crypto options traded on Deribit/OKX/Bybit where the exchange
reports ``underlying_price`` as the forward/index price.

All functions are pure standard-library ``math`` — no numpy or scipy.

Conventions
-----------
- ``forward`` : the option's underlying forward/index price (denoted F).
- ``strike``  : option strike (denoted K).
- ``t_years`` : time to expiry in years. ``t_years <= 0`` → expired.
- ``vol``     : annualised implied volatility (e.g. 0.20 for 20 %).
- ``rate``    : continuous risk-free rate (default 0.0).
- ``D``       : discount factor ``exp(-rate * t_years)``.

Greeks — natural units (callers scale as needed)
-------------------------------------------------
- ``vega``  : per 1.0 unit of vol  (divide by 100 for "per 1 %" vol).
- ``theta`` : per 1.0 year         (divide by 365 for daily theta).
- ``rho``   : per 1.0 of rate      (Black-76 rho w.r.t. ``rate``).

Expiry guard
------------
When ``t_years <= 0`` the option is expired:

- ``bs_price``  returns the **discounted intrinsic value**
  ``D * max(F - K, 0)`` for calls, ``D * max(K - F, 0)`` for puts.
- ``bs_greeks`` returns a ``Greeks`` namedtuple with **all zeros**.
- ``implied_vol`` returns ``None``.
"""

from __future__ import annotations

import math
from typing import NamedTuple, Protocol

from crocodile.core.schema.enums import OptType

__all__ = [
    "Greeks",
    "bs_greeks",
    "bs_price",
    "implied_vol",
    "norm_cdf",
    "norm_pdf",
    "GreeksSolverAdapter",
    "PureMathGreeksSolverAdapter",
    "SciPyGreeksSolverAdapter",
]

# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------


def norm_cdf(x: float) -> float:
    """Standard normal CDF: N(x) = 0.5 * (1 + erf(x / sqrt(2)))."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    """Standard normal PDF: n(x) = exp(-x²/2) / sqrt(2π)."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# ---------------------------------------------------------------------------
# Black-76 pricing
# ---------------------------------------------------------------------------


def bs_price(
    forward: float,
    strike: float,
    t_years: float,
    vol: float,
    opt_type: OptType,
    rate: float = 0.0,
) -> float:
    """Black-76 option price.

    Args:
        forward:  Forward / index price (F).
        strike:   Option strike (K).
        t_years:  Time to expiry in years (``<= 0`` → expired).
        vol:      Annualised implied vol (e.g. 0.20).
        opt_type: ``OptType.CALL`` or ``OptType.PUT``.
        rate:     Continuous risk-free rate (default 0.0).

    Returns:
        Option price as a float.  For expired options returns the
        discounted intrinsic value ``D * max(F-K, 0)`` (call) or
        ``D * max(K-F, 0)`` (put).
    """
    if vol < 0:
        raise ValueError(f"vol must be >= 0, got {vol!r}")

    D = math.exp(-rate * t_years) if t_years > 0 else 1.0

    # ---- expired guard ----
    if t_years <= 0:
        if opt_type == OptType.CALL:
            return D * max(forward - strike, 0.0)
        else:
            return D * max(strike - forward, 0.0)

    # ---- vol == 0: well-defined limit (discounted intrinsic) ----
    if vol == 0.0:
        if opt_type == OptType.CALL:
            return D * max(forward - strike, 0.0)
        else:
            return D * max(strike - forward, 0.0)

    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(forward / strike) + 0.5 * vol * vol * t_years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t

    if opt_type == OptType.CALL:
        return D * (forward * norm_cdf(d1) - strike * norm_cdf(d2))
    else:
        return D * (strike * norm_cdf(-d2) - forward * norm_cdf(-d1))


# ---------------------------------------------------------------------------
# Greeks
# ---------------------------------------------------------------------------


class Greeks(NamedTuple):
    """Black-76 option greeks (natural units).

    Attributes:
        delta: Rate of change of price w.r.t. forward.
        gamma: Rate of change of delta w.r.t. forward.
        vega:  Rate of change of price w.r.t. vol (per 1.0 vol unit).
        theta: Rate of change of price w.r.t. time (per 1.0 year; negative = time decay).
        rho:   Rate of change of price w.r.t. rate (Black-76: ``-t_years * price``).
    """

    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float


def bs_greeks(
    forward: float,
    strike: float,
    t_years: float,
    vol: float,
    opt_type: OptType,
    rate: float = 0.0,
) -> Greeks:
    """Black-76 option greeks.

    Args:
        forward:  Forward / index price (F).
        strike:   Option strike (K).
        t_years:  Time to expiry in years (``<= 0`` → all zeros).
        vol:      Annualised implied vol.
        opt_type: ``OptType.CALL`` or ``OptType.PUT``.
        rate:     Continuous risk-free rate (default 0.0).

    Returns:
        A :class:`Greeks` namedtuple.  For expired options, all fields
        are 0.0.
    """
    if vol < 0:
        raise ValueError(f"vol must be >= 0, got {vol!r}")

    # ---- expired guard ----
    if t_years <= 0:
        return Greeks(delta=0.0, gamma=0.0, vega=0.0, theta=0.0, rho=0.0)

    # ---- vol == 0: well-defined limit (all Greeks are 0) ----
    if vol == 0.0:
        return Greeks(delta=0.0, gamma=0.0, vega=0.0, theta=0.0, rho=0.0)

    D = math.exp(-rate * t_years)
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(forward / strike) + 0.5 * vol * vol * t_years) / (vol * sqrt_t)
    nd1 = norm_cdf(d1)
    npd1 = norm_pdf(d1)

    # --- delta ---
    if opt_type == OptType.CALL:
        delta = D * nd1
    else:
        delta = -D * norm_cdf(-d1)

    # --- gamma (same for call and put) ---
    gamma = D * npd1 / (forward * vol * sqrt_t)

    # --- vega (same for call and put; per 1.0 vol unit) ---
    vega = D * forward * npd1 * sqrt_t

    # --- theta ---
    # Black-76 theta (per 1.0 year; negative = time value decay).
    # d(price)/d(T) = -D*F*n(d1)*vol/(2*sqrt_t) + rate*price
    #
    # The vol-decay term is always negative; the rate term is positive
    # because D = exp(-rate*T) grows the discount factor as T shrinks.
    # Sign convention: theta < 0 means the option loses value as time
    # passes (T decreases), which is the standard quoting convention.
    #
    # Note: the rate term is ADD (+), not subtract (-).
    price = bs_price(forward, strike, t_years, vol, opt_type, rate=rate)
    common_theta = -D * forward * npd1 * vol / (2.0 * sqrt_t)
    theta = common_theta + rate * price

    # --- rho (Black-76 rho w.r.t. rate) ---
    # d/d_rate [D * (…)] = -t_years * price  (chain rule on D = exp(-rate*t_years))
    rho = -t_years * price

    return Greeks(delta=delta, gamma=gamma, vega=vega, theta=theta, rho=rho)


# ---------------------------------------------------------------------------
# Implied volatility solver
# ---------------------------------------------------------------------------

_IV_MIN = 1e-6
_IV_MAX = 10.0
_TOL = 1e-6
_MAX_ITER = 100


def implied_vol(
    price: float,
    forward: float,
    strike: float,
    t_years: float,
    opt_type: OptType,
    rate: float = 0.0,
) -> float | None:
    """Solve for the implied volatility that reproduces ``price``.

    Uses Newton-Raphson seeded at ``vol=0.5`` with vega steps, falling back
    to bisection on ``[1e-6, 10.0]`` when Newton steps outside bounds or
    vega ≈ 0.  Tolerance is ``1e-6`` on the option price; max 100 iterations.

    Returns ``None`` when:

    - ``t_years <= 0`` (expired option).
    - ``price <= discounted_intrinsic`` (below no-arb lower bound).
    - ``price >= D * forward`` for calls (above theoretical maximum).
    - ``price >= D * strike``  for puts  (above theoretical maximum).
    - The solver does not converge within ``_MAX_ITER`` iterations.

    Args:
        price:    Market or model option price.
        forward:  Forward / index price (F).
        strike:   Option strike (K).
        t_years:  Time to expiry in years.
        opt_type: ``OptType.CALL`` or ``OptType.PUT``.
        rate:     Continuous risk-free rate (default 0.0).

    Returns:
        Implied vol as a float, or ``None`` if no-arb bounds are violated
        or the solver does not converge.
    """
    try:
        # --- expired guard ---
        if t_years <= 0:
            return None

        D = math.exp(-rate * t_years)

        # --- no-arb bounds ---
        if opt_type == OptType.CALL:
            intrinsic = D * max(forward - strike, 0.0)
            upper = D * forward
        else:
            intrinsic = D * max(strike - forward, 0.0)
            upper = D * strike

        if price <= intrinsic or price >= upper:
            return None

        # --- Newton-Raphson, seeded at vol=0.5 ---
        vol = 0.5
        for _ in range(_MAX_ITER):
            p = bs_price(forward, strike, t_years, vol, opt_type, rate=rate)
            diff = p - price
            if abs(diff) < _TOL:
                return vol
            # vega = dP/dvol
            v = bs_greeks(forward, strike, t_years, vol, opt_type, rate=rate).vega
            if abs(v) < 1e-10:
                # vega ≈ 0 → fall back to bisection
                break
            vol_new = vol - diff / v
            if vol_new < _IV_MIN or vol_new > _IV_MAX:
                # Newton stepped outside bounds → switch to bisection
                break
            vol = vol_new

        # --- bisection fallback on [_IV_MIN, _IV_MAX] ---
        lo, hi = _IV_MIN, _IV_MAX

        p_lo = bs_price(forward, strike, t_years, lo, opt_type, rate=rate)
        p_hi = bs_price(forward, strike, t_years, hi, opt_type, rate=rate)

        # price must be bracketed
        if not (p_lo <= price <= p_hi):
            return None

        for _ in range(_MAX_ITER):
            mid = 0.5 * (lo + hi)
            p_mid = bs_price(forward, strike, t_years, mid, opt_type, rate=rate)
            if abs(p_mid - price) < _TOL:
                return mid
            if p_mid < price:
                lo = mid
            else:
                hi = mid

        # Did not converge
        return None
    except (OverflowError, ZeroDivisionError, ValueError):
        return None


class GreeksSolverAdapter(Protocol):
    """Protocol defining the standard interface for Black-76 Greeks and pricing solvers."""

    def price(
        self,
        forward: float,
        strike: float,
        t_years: float,
        vol: float,
        opt_type: OptType,
        rate: float = 0.0,
    ) -> float:
        """Calculate option price."""
        ...

    def greeks(
        self,
        forward: float,
        strike: float,
        t_years: float,
        vol: float,
        opt_type: OptType,
        rate: float = 0.0,
    ) -> Greeks:
        """Calculate option Greeks."""
        ...

    def implied_vol(
        self,
        price: float,
        forward: float,
        strike: float,
        t_years: float,
        opt_type: OptType,
        rate: float = 0.0,
    ) -> float | None:
        """Solve for implied volatility."""
        ...


class PureMathGreeksSolverAdapter(GreeksSolverAdapter):
    """GreeksSolverAdapter implementation using the original pure math solver."""

    def price(
        self,
        forward: float,
        strike: float,
        t_years: float,
        vol: float,
        opt_type: OptType,
        rate: float = 0.0,
    ) -> float:
        return bs_price(forward, strike, t_years, vol, opt_type, rate=rate)

    def greeks(
        self,
        forward: float,
        strike: float,
        t_years: float,
        vol: float,
        opt_type: OptType,
        rate: float = 0.0,
    ) -> Greeks:
        return bs_greeks(forward, strike, t_years, vol, opt_type, rate=rate)

    def implied_vol(
        self,
        price: float,
        forward: float,
        strike: float,
        t_years: float,
        opt_type: OptType,
        rate: float = 0.0,
    ) -> float | None:
        try:
            return implied_vol(price, forward, strike, t_years, opt_type, rate=rate)
        except (OverflowError, ZeroDivisionError, ValueError):
            return None


class SciPyGreeksSolverAdapter(GreeksSolverAdapter):
    """GreeksSolverAdapter implementation using the SciPy library for CDF/PDF and optimization."""

    def price(
        self,
        forward: float,
        strike: float,
        t_years: float,
        vol: float,
        opt_type: OptType,
        rate: float = 0.0,
    ) -> float:
        if vol < 0:
            raise ValueError(f"vol must be >= 0, got {vol!r}")

        D = math.exp(-rate * t_years) if t_years > 0 else 1.0

        if t_years <= 0 or vol == 0.0:
            if opt_type == OptType.CALL:
                return D * max(forward - strike, 0.0)
            else:
                return D * max(strike - forward, 0.0)

        try:
            import scipy.stats
        except ModuleNotFoundError as e:
            raise ModuleNotFoundError(
                "scipy is required for Black-Scholes analytics — install 'crocodile[ml]'"
            ) from e

        sqrt_t = math.sqrt(t_years)
        d1 = (math.log(forward / strike) + 0.5 * vol * vol * t_years) / (vol * sqrt_t)
        d2 = d1 - vol * sqrt_t

        if opt_type == OptType.CALL:
            return D * (forward * scipy.stats.norm.cdf(d1) - strike * scipy.stats.norm.cdf(d2))
        else:
            return D * (strike * scipy.stats.norm.cdf(-d2) - forward * scipy.stats.norm.cdf(-d1))

    def greeks(
        self,
        forward: float,
        strike: float,
        t_years: float,
        vol: float,
        opt_type: OptType,
        rate: float = 0.0,
    ) -> Greeks:
        if vol < 0:
            raise ValueError(f"vol must be >= 0, got {vol!r}")

        if t_years <= 0 or vol == 0.0:
            return Greeks(delta=0.0, gamma=0.0, vega=0.0, theta=0.0, rho=0.0)

        try:
            import scipy.stats
        except ModuleNotFoundError as e:
            raise ModuleNotFoundError(
                "scipy is required for Black-Scholes analytics — install 'crocodile[ml]'"
            ) from e

        D = math.exp(-rate * t_years)
        sqrt_t = math.sqrt(t_years)
        d1 = (math.log(forward / strike) + 0.5 * vol * vol * t_years) / (vol * sqrt_t)
        nd1 = scipy.stats.norm.cdf(d1)
        npd1 = scipy.stats.norm.pdf(d1)

        if opt_type == OptType.CALL:
            delta = D * nd1
        else:
            delta = -D * scipy.stats.norm.cdf(-d1)

        gamma = D * npd1 / (forward * vol * sqrt_t)
        vega = D * forward * npd1 * sqrt_t

        price = self.price(forward, strike, t_years, vol, opt_type, rate=rate)
        common_theta = -D * forward * npd1 * vol / (2.0 * sqrt_t)
        theta = common_theta + rate * price
        rho = -t_years * price

        return Greeks(delta=delta, gamma=gamma, vega=vega, theta=theta, rho=rho)

    def implied_vol(
        self,
        price: float,
        forward: float,
        strike: float,
        t_years: float,
        opt_type: OptType,
        rate: float = 0.0,
    ) -> float | None:
        try:
            if t_years <= 0:
                return None

            D = math.exp(-rate * t_years)
            if opt_type == OptType.CALL:
                intrinsic = D * max(forward - strike, 0.0)
                upper = D * forward
            else:
                intrinsic = D * max(strike - forward, 0.0)
                upper = D * strike

            if price <= intrinsic or price >= upper:
                return None

            try:
                import scipy.optimize
            except ModuleNotFoundError as e:
                raise ModuleNotFoundError(
                    "scipy is required for Black-Scholes analytics — install 'crocodile[ml]'"
                ) from e

            def f(v: float) -> float:
                return self.price(forward, strike, t_years, v, opt_type, rate) - price

            lo, hi = 1e-6, 10.0
            try:
                f_lo = f(lo)
                f_hi = f(hi)
                if f_lo * f_hi > 0:
                    return None
                res = scipy.optimize.brentq(f, lo, hi, xtol=1e-6)
                return float(res)
            except Exception:
                return None
        except (OverflowError, ZeroDivisionError, ValueError):
            return None
