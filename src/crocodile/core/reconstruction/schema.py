"""Estimated states are a separate wire product, never canonical exchange events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

BOUND_INTERPRETATION = (
    "Feasible intervals conditional on stated constraints; not statistical confidence intervals."
)


@dataclass(frozen=True, slots=True)
class FeasibleInterval:
    lower: float
    upper: float | None
    """None means unbounded, or unavailable when bounds_status says unavailable."""


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    values: tuple[float, ...]
    bounds: tuple[FeasibleInterval, ...]
    status: Literal["converged", "unconverged", "infeasible"]
    residual: float
    iterations: int
    provenance: str = "synthetic"
    basis: str = "bounded_instantaneous_quantity_projection_v1"
    bound_interpretation: str = BOUND_INTERPRETATION
    assumptions: tuple[str, ...] = (
        "The supplied quantity is an instantaneous total, not a duration average.",
    )


@dataclass(frozen=True, slots=True)
class LinearObservation:
    coefficients: tuple[tuple[int, float], ...]
    quantity: float
    observed_at_ns: int
    available_at_ns: int
    name: str
    tolerance: float = 0


@dataclass(frozen=True, slots=True)
class TradeExecutionBound:
    time_index: int
    level_index: int
    trade_quantity: float


@dataclass(frozen=True, slots=True)
class WindowProblem:
    times_ns: tuple[int, ...]
    durations_ns: tuple[int, ...]
    prior: tuple[tuple[float, ...], ...]
    observations: tuple[LinearObservation, ...]
    fixed: dict[int, float] = field(default_factory=dict)
    forbidden: tuple[int, ...] = ()
    upper: tuple[float | None, ...] | None = None
    executions: tuple[TradeExecutionBound, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelFlow:
    time_index: int
    level_index: int
    additions: float
    cancellations: float
    visible_execution: float


@dataclass(frozen=True, slots=True)
class WindowResult:
    values: tuple[tuple[float, ...], ...]
    bounds: tuple[tuple[FeasibleInterval, ...], ...]
    status: Literal["converged", "unconverged", "infeasible"]
    bounds_status: str
    residual: float
    iterations: int
    elapsed_seconds: float
    mode: str
    asof_ns: int
    information_cutoff_ns: int
    revision_id: str | None
    flows: tuple[ModelFlow, ...]
    execution_bounds: tuple[FeasibleInterval, ...]
    observed_variables: int
    variables: int
    objective: float
    provenance: str = "synthetic"
    basis: str = "bounded_duration_constrained_l2_v1"
    source_event_count: None = None
    bound_interpretation: str = BOUND_INTERPRETATION
    assumptions: tuple[str, ...] = (
        "States are constant on the explicit positive-duration finite grid.",
        "Priors and temporal smoothness select one estimate, not the unique true history.",
        "Net-flow decomposition is modelled; visible trade execution is not forced by tape volume.",
        "Continuous-time instantaneous bounds without a minimum state duration may be unbounded.",
    )
    notes: tuple[str, ...] = ()
