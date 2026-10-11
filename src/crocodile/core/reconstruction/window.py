"""Bounded projected-gradient/Dykstra window model, with optional linear-programming bounds."""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from .schema import FeasibleInterval, LinearObservation, ModelFlow, WindowProblem, WindowResult

Array = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class _Constraint:
    indices: NDArray[np.intp]
    coefficients: Array
    norm_squared: float
    quantity: float
    tolerance: float


def _compile(observations: tuple[LinearObservation, ...]) -> tuple[_Constraint, ...]:
    result = []
    for observation in observations:
        indices = np.array([i for i, _ in observation.coefficients], dtype=np.intp)
        coefficients = np.array([c for _, c in observation.coefficients], dtype=np.float64)
        norm = float(np.dot(coefficients, coefficients))
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError("observation coefficients exceed the supported numerical scale")
        result.append(
            _Constraint(indices, coefficients, norm, observation.quantity, observation.tolerance)
        )
    return tuple(result)


def duration_average(
    time_indices: Sequence[int],
    level_indices: Sequence[int],
    durations_ns: Sequence[int],
    *,
    levels: int,
    quantity: float,
    observed_at_ns: int,
    available_at_ns: int,
    name: str = "duration_average",
    tolerance: float = 0,
) -> LinearObservation:
    """Build y=sum_t(duration_t/total_duration)*sum_i x_ti on the explicit grid."""
    if (
        len(time_indices) != len(durations_ns)
        or not time_indices
        or not level_indices
        or any(d <= 0 for d in durations_ns)
        or levels <= 0
        or any(i < 0 for i in time_indices)
        or any(not 0 <= i < levels for i in level_indices)
        or len(set(time_indices)) != len(time_indices)
        or len(set(level_indices)) != len(level_indices)
    ):
        raise ValueError("duration-average grid must have distinct indices and positive durations")
    total = sum(durations_ns)
    coefficients = tuple(
        (t * levels + i, d / total)
        for t, d in zip(time_indices, durations_ns, strict=True)
        for i in level_indices
    )
    return LinearObservation(
        coefficients, quantity, observed_at_ns, available_at_ns, name, tolerance
    )


class _Budget:
    def __init__(self, iterations: int, seconds: float) -> None:
        self.started = time.perf_counter()
        self.deadline = self.started + seconds
        self.maximum = iterations
        self.used = 0

    def available(self) -> bool:
        return self.used < self.maximum and time.perf_counter() < self.deadline


def _residual(x: Array, lower: Array, upper: Array, observations: tuple[_Constraint, ...]) -> float:
    result = max(float(np.max(lower - x, initial=0)), float(np.max(x - upper, initial=0)), 0.0)
    for observation in observations:
        dot = float(np.dot(observation.coefficients, x[observation.indices]))
        result = max(result, abs(dot - observation.quantity) - observation.tolerance)
    return max(result, 0.0)


def _project(
    point: Array,
    lower: Array,
    upper: Array,
    observations: tuple[_Constraint, ...],
    tolerance: float,
    budget: _Budget,
) -> tuple[Array, bool]:
    """Dykstra projections onto the box and every exact/tolerance observation slab."""
    x = point.copy()
    box_correction = np.zeros_like(x)
    corrections = [np.zeros(len(o.coefficients), dtype=np.float64) for o in observations]
    while budget.available():
        budget.used += 1
        previous = x.copy()
        y = x + box_correction
        x = np.clip(y, lower, upper)
        box_correction = y - x
        for k, observation in enumerate(observations):
            indices = observation.indices
            coefficients = observation.coefficients
            y_part = x[indices] + corrections[k]
            dot = float(np.dot(coefficients, y_part))
            target = min(
                max(dot, observation.quantity - observation.tolerance),
                observation.quantity + observation.tolerance,
            )
            z_part = y_part + coefficients * ((target - dot) / observation.norm_squared)
            corrections[k] = y_part - z_part
            x[indices] = z_part
        scale = max(1.0, float(np.max(np.abs(x), initial=0)))
        if (
            float(np.max(np.abs(x - previous), initial=0)) <= tolerance * scale
            and _residual(x, lower, upper, observations) <= tolerance
        ):
            return x, True
    return x, False


def _disjoint_bounds(
    lower: Array,
    upper: Array,
    observations: tuple[LinearObservation, ...],
) -> tuple[list[FeasibleInterval] | None, str]:
    counts = [0] * len(lower)
    for observation in observations:
        for index, coefficient in observation.coefficients:
            counts[index] += 1
            if coefficient <= 0 or counts[index] > 1:
                return None, "overlapping"
    result = [
        FeasibleInterval(float(lo), None if math.isinf(hi) else float(hi))
        for lo, hi in zip(lower, upper, strict=True)
    ]
    for observation in observations:
        minimum = math.fsum(c * float(lower[i]) for i, c in observation.coefficients)
        finite_max = math.fsum(
            c * float(upper[i]) for i, c in observation.coefficients if math.isfinite(upper[i])
        )
        infinite_count = sum(math.isinf(upper[i]) for i, _ in observation.coefficients)
        qlo, qhi = (
            observation.quantity - observation.tolerance,
            observation.quantity + observation.tolerance,
        )
        if qhi < minimum - 1e-10 or (not infinite_count and qlo > finite_max + 1e-10):
            return [], "infeasible"
        for i, c in observation.coefficients:
            others_min = minimum - c * float(lower[i])
            others_infinite = infinite_count - int(math.isinf(upper[i]))
            others_max = (
                math.inf
                if others_infinite
                else finite_max - (c * float(upper[i]) if math.isfinite(upper[i]) else 0)
            )
            lo = max(float(lower[i]), (qlo - others_max) / c)
            hi = min(float(upper[i]), (qhi - others_min) / c)
            result[i] = FeasibleInterval(max(lo, 0), max(hi, 0))
    return result, "unbounded" if any(b.upper is None for b in result) else "feasible"


def _lp_bounds(
    lower: Array,
    upper: Array,
    observations: tuple[LinearObservation, ...],
    deadline: float,
    maximum_variables: int,
) -> tuple[list[FeasibleInterval], str, tuple[str, ...]]:
    if len(lower) > maximum_variables:
        return (
            [],
            "unsupported_bounds_size",
            ("Per-coordinate LP bounds exceed the explicit variable budget.",),
        )
    try:
        from scipy.optimize import linprog
    except ImportError:
        return (
            [],
            "unavailable_optional_scipy",
            ("Overlapping feasible bounds require the optional crocodile[ml] extra.",),
        )
    matrix = np.zeros((len(observations) * 2, len(lower)), dtype=np.float64)
    targets = np.zeros(len(observations) * 2, dtype=np.float64)
    for k, observation in enumerate(observations):
        for i, coefficient in observation.coefficients:
            matrix[2 * k, i], matrix[2 * k + 1, i] = coefficient, -coefficient
        targets[2 * k] = observation.quantity + observation.tolerance
        targets[2 * k + 1] = -observation.quantity + observation.tolerance
    bounds = [
        (float(lo), None if math.isinf(hi) else float(hi))
        for lo, hi in zip(lower, upper, strict=True)
    ]
    result = []
    for index in range(len(lower)):
        endpoints: list[float | None] = []
        for sign in (1, -1):
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return (
                    [],
                    "bounds_budget_exhausted",
                    ("Feasible-bound time budget exhausted; no partial intervals certified.",),
                )
            objective = np.zeros(len(lower), dtype=np.float64)
            objective[index] = sign
            optimized = linprog(
                objective,
                A_ub=matrix,
                b_ub=targets,
                bounds=bounds,
                method="highs",
                options={"time_limit": remaining},
            )
            if optimized.status == 3 and sign == -1:
                endpoints.append(None)
            elif optimized.status == 2:
                return (
                    [],
                    "infeasible",
                    ("Observation constraints have no feasible nonnegative state.",),
                )
            elif not optimized.success:
                return (
                    [],
                    "bounds_budget_exhausted",
                    ("LP bounds did not converge within the declared budget.",),
                )
            else:
                endpoints.append(max(0.0, float(optimized.x[index])))
        result.append(FeasibleInterval(float(endpoints[0] or 0), endpoints[1]))
    return result, "unbounded" if any(b.upper is None for b in result) else "feasible", ()


def _validate(problem: WindowProblem, asof_ns: int, mode: str) -> tuple[Array, Array, Array, int]:
    frames = len(problem.times_ns)
    levels = len(problem.prior[0]) if problem.prior else 0
    if (
        not frames
        or not levels
        or len(problem.prior) != frames
        or len(problem.durations_ns) != frames
        or any(len(p) != levels for p in problem.prior)
    ):
        raise ValueError("invalid window shape")
    if frames * levels > 4096 or len(problem.observations) > 1024:
        raise ValueError("window exceeds variable/observation budget")
    if (
        any(d <= 0 for d in problem.durations_ns)
        or any(t < 0 for t in problem.times_ns)
        or any(
            problem.times_ns[i] + problem.durations_ns[i] > problem.times_ns[i + 1]
            for i in range(frames - 1)
        )
    ):
        raise ValueError("window requires nonoverlapping positive-duration intervals")
    if problem.times_ns[-1] + problem.durations_ns[-1] - problem.times_ns[0] > 3_000_000_000:
        raise ValueError("window must be bounded to at most 3 seconds")
    if mode == "causal" and problem.times_ns[-1] + problem.durations_ns[-1] > asof_ns:
        raise ValueError("causal grid extends into the future")
    prior = np.asarray(problem.prior, dtype=np.float64).reshape(-1)
    if not np.all(np.isfinite(prior)):
        raise ValueError("prior must contain only finite values")
    lower = np.zeros_like(prior)
    upper = (
        np.full_like(prior, np.inf)
        if problem.upper is None
        else np.array(
            [np.inf if value is None else value for value in problem.upper], dtype=np.float64
        )
    )
    if upper.shape != prior.shape or np.any(np.isnan(upper)) or np.any(upper < 0):
        raise ValueError("invalid window upper bounds")
    for index in problem.forbidden:
        if not 0 <= index < len(prior):
            raise ValueError("invalid forbidden index")
        upper[index] = 0
    for index, value in problem.fixed.items():
        if not 0 <= index < len(prior) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid fixed snapshot level")
        if value > upper[index]:
            raise ValueError("fixed snapshot conflicts with support/upper bound")
        lower[index] = upper[index] = value
    for observation in problem.observations:
        if (
            not observation.coefficients
            or not math.isfinite(observation.quantity)
            or not math.isfinite(observation.tolerance)
            or observation.tolerance < 0
            or observation.available_at_ns < observation.observed_at_ns
            or observation.observed_at_ns < 0
        ):
            raise ValueError("invalid linear observation")
        seen = set()
        for index, coefficient in observation.coefficients:
            if (
                not 0 <= index < len(prior)
                or index in seen
                or not math.isfinite(coefficient)
                or coefficient == 0
            ):
                raise ValueError("invalid sparse observation coefficient")
            seen.add(index)
        if mode == "causal" and (
            observation.available_at_ns > asof_ns or observation.observed_at_ns > asof_ns
        ):
            raise ValueError("causal observation contains future information")
    for execution in problem.executions:
        if (
            not 0 < execution.time_index < frames
            or not 0 <= execution.level_index < levels
            or not math.isfinite(execution.trade_quantity)
            or execution.trade_quantity < 0
        ):
            raise ValueError("invalid visible-execution upper bound")
    return prior, lower, upper, levels


def solve_window(
    problem: WindowProblem,
    *,
    asof_ns: int,
    mode: Literal["causal", "retrospective"] = "causal",
    revision_id: str | None = None,
    max_iterations: int = 2000,
    max_seconds: float = 1,
    tolerance: float = 1e-8,
    prior_weight: float = 1,
    smoothness: float = 0.1,
    compute_bounds: bool = True,
    max_bounds_variables: int = 64,
) -> WindowResult:
    """Choose a constrained estimate with a positive prior and optional temporal penalty.

    Fixed snapshot/BBO levels and forbidden support live in the box. Linear observations
    use their actual duration coefficients. Every iterative sweep and all optional LP
    bounds share the time budget. Numerical tolerance is reported through residual;
    unconverged estimates are diagnostics and must not be treated as a feasible solution.
    """
    if (
        mode not in {"causal", "retrospective"}
        or (mode == "retrospective" and not revision_id)
        or (mode == "causal" and revision_id is not None)
    ):
        raise ValueError("retrospective output requires a separate revision_id")
    if (
        max_iterations < 1
        or not math.isfinite(max_seconds)
        or max_seconds <= 0
        or not math.isfinite(tolerance)
        or tolerance <= 0
        or not math.isfinite(prior_weight)
        or prior_weight <= 0
        or not math.isfinite(smoothness)
        or smoothness < 0
    ):
        raise ValueError("invalid solver budget or positive prior/smoothness weights")
    budget = _Budget(max_iterations, max_seconds)
    prior, lower, upper, levels = _validate(problem, asof_ns, mode)
    compiled = _compile(problem.observations)
    analytic_bounds, analytic_status = _disjoint_bounds(lower, upper, problem.observations)
    x, converged = _project(prior, lower, upper, compiled, tolerance, budget)
    if converged and smoothness:
        step = 1 / (2 * prior_weight + 8 * smoothness)
        while budget.available():
            gradient = 2 * prior_weight * (x - prior)
            states = x.reshape((-1, levels))
            difference = states[1:] - states[:-1]
            temporal = np.zeros_like(states)
            temporal[1:] += difference
            temporal[:-1] -= difference
            gradient += 2 * smoothness * temporal.reshape(-1)
            updated, projected = _project(
                x - step * gradient, lower, upper, compiled, tolerance, budget
            )
            stable = float(np.max(np.abs(updated - x), initial=0)) <= tolerance * max(
                1, float(np.max(np.abs(x), initial=0))
            )
            x = updated
            if not projected:
                converged = False
                break
            if stable:
                break
        else:
            converged = False
    status: Literal["converged", "unconverged", "infeasible"] = (
        "converged" if converged else "unconverged"
    )
    bounds: list[FeasibleInterval] = []
    bounds_status = "not_computed"
    notes: tuple[str, ...] = ()
    if analytic_status == "infeasible":
        status, bounds_status = "infeasible", "infeasible"
    elif compute_bounds and converged:
        if analytic_bounds is not None:
            bounds, bounds_status = analytic_bounds, analytic_status
        else:
            bounds, bounds_status, notes = _lp_bounds(
                lower, upper, problem.observations, budget.deadline, max_bounds_variables
            )
            if bounds_status == "infeasible":
                status = "infeasible"
    states_tuple = tuple(tuple(float(value) for value in row) for row in x.reshape((-1, levels)))
    bounds_tuple = tuple(tuple(bounds[i : i + levels]) for i in range(0, len(bounds), levels))
    flows = []
    for t in range(1, len(states_tuple)):
        for i in range(levels):
            change = states_tuple[t][i] - states_tuple[t - 1][i]
            flows.append(ModelFlow(t, i, max(change, 0), max(-change, 0), 0))
    execution_bounds = tuple(FeasibleInterval(0, e.trade_quantity) for e in problem.executions)
    information_cutoff = max((o.available_at_ns for o in problem.observations), default=asof_ns)
    objective = prior_weight * float(np.sum((x - prior) ** 2))
    objective += smoothness * float(np.sum(np.diff(x.reshape((-1, levels)), axis=0) ** 2))
    observed = {i for o in problem.observations for i, _ in o.coefficients} | set(problem.fixed)
    return WindowResult(
        states_tuple,
        bounds_tuple,
        status,
        bounds_status,
        _residual(x, lower, upper, compiled),
        budget.used,
        time.perf_counter() - budget.started,
        mode,
        asof_ns,
        information_cutoff,
        revision_id,
        tuple(flows),
        execution_bounds,
        len(observed),
        len(prior),
        objective,
        notes=notes,
    )
