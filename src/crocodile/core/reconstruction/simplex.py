"""Dependency-free weighted simplex projection with optional fixed/BBO/support constraints."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence

from .schema import FeasibleInterval, ProjectionResult


def project_quantity(
    prior: Sequence[float],
    quantity: float,
    *,
    weights: Sequence[float] | None = None,
    fixed: Mapping[int, float] | None = None,
    forbidden: Iterable[int] = (),
    upper: Sequence[float | None] | None = None,
    tolerance: float = 1e-10,
    max_iterations: int = 128,
) -> ProjectionResult:
    """Minimise 1/2 sum w_i(x_i-v_i)^2 subject to nonnegative capped total quantity.

    The multiplier uses a bounded monotone search; fixed levels are removed first.
    Bounds describe all distributions satisfying the hard constraints, independent of
    the chosen prior. This API deliberately accepts only instantaneous quantities.
    """
    v = tuple(float(value) for value in prior)
    n = len(v)
    q = float(quantity)
    w = tuple(float(value) for value in weights) if weights is not None else (1.0,) * n
    u = (
        tuple(math.inf if value is None else float(value) for value in upper)
        if upper is not None
        else (math.inf,) * n
    )
    fixed_values = dict(fixed or {})
    forbidden_values = tuple(forbidden)
    if (
        not math.isfinite(q)
        or q < 0
        or not math.isfinite(tolerance)
        or tolerance <= 0
        or max_iterations < 1
        or len(w) != n
        or len(u) != n
    ):
        raise ValueError("invalid quantity, dimensions or projection budget")
    if any(not math.isfinite(x) for x in v) or any(not math.isfinite(x) or x <= 0 for x in w):
        raise ValueError("prior must be finite and weights finite/positive")
    if any(math.isnan(x) or x < 0 for x in u):
        raise ValueError("invalid nonnegative upper bound")
    for index, value in fixed_values.items():
        if (
            not isinstance(index, int)
            or not 0 <= index < n
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("invalid fixed level")
    for index in forbidden_values:
        if not isinstance(index, int) or not 0 <= index < n:
            raise ValueError("invalid forbidden level")
        if fixed_values.get(index, 0) != 0:
            return ProjectionResult((), (), "infeasible", q, 0)
        fixed_values[index] = 0
    if any(value > u[index] for index, value in fixed_values.items()):
        return ProjectionResult((), (), "infeasible", q, 0)
    remaining = q - math.fsum(fixed_values.values())
    free = [i for i in range(n) if i not in fixed_values]
    if remaining < 0 or math.fsum(u[i] for i in free) < remaining or (not free and remaining != 0):
        return ProjectionResult((), (), "infeasible", abs(remaining), 0)
    bounds = []
    for index in range(n):
        if index in fixed_values:
            bounds.append(FeasibleInterval(fixed_values[index], fixed_values[index]))
        else:
            other_max = math.fsum(u[i] for i in free if i != index)
            bounds.append(FeasibleInterval(max(0, remaining - other_max), min(u[index], remaining)))
    values = [fixed_values.get(i, 0.0) for i in range(n)]
    if remaining == 0:
        return ProjectionResult(tuple(values), tuple(bounds), "converged", 0, 0)
    low = min(w[i] * v[i] for i in free) - remaining * max(w[i] for i in free) - 1
    high = max(w[i] * v[i] for i in free)
    residual = math.inf
    iterations = 0
    for iterations in range(1, max_iterations + 1):
        multiplier = (low + high) / 2
        for i in free:
            values[i] = min(u[i], max(0, v[i] - multiplier / w[i]))
        residual = math.fsum(values) - q
        if abs(residual) <= tolerance:
            # Only a rounding residual is adjusted, not an unconverged model solution.
            for i in free:
                candidate = values[i] - residual
                if 0 <= candidate <= u[i]:
                    values[i] = candidate
                    break
            residual = math.fsum(values) - q
            return ProjectionResult(
                tuple(values), tuple(bounds), "converged", abs(residual), iterations
            )
        if residual > 0:
            low = multiplier
        else:
            high = multiplier
    return ProjectionResult(tuple(values), tuple(bounds), "unconverged", abs(residual), iterations)
