"""Finite-grid L2 estimates with conserved observations and explicit feasible ambiguity."""

from .schema import (
    FeasibleInterval,
    LinearObservation,
    ModelFlow,
    ProjectionResult,
    TradeExecutionBound,
    WindowProblem,
    WindowResult,
)
from .simplex import project_quantity
from .window import duration_average, solve_window

__all__ = [
    "FeasibleInterval",
    "LinearObservation",
    "ModelFlow",
    "ProjectionResult",
    "TradeExecutionBound",
    "WindowProblem",
    "WindowResult",
    "duration_average",
    "project_quantity",
    "solve_window",
]
