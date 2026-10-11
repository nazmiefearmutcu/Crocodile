import builtins
import math

import pytest

from crocodile.core.reconstruction import (
    LinearObservation,
    TradeExecutionBound,
    WindowProblem,
    duration_average,
    solve_window,
)


def problem(observations, prior=((1, 1), (1, 1)), **kwargs):
    return WindowProblem(
        times_ns=(0, 100_000_000),
        durations_ns=(100_000_000, 300_000_000),
        prior=prior,
        observations=tuple(observations),
        **kwargs,
    )


def test_duration_weighted_equation_preserves_observation_not_instantaneous_mass():
    observation = duration_average(
        [0, 1],
        [0, 1],
        [100_000_000, 300_000_000],
        levels=2,
        quantity=10,
        observed_at_ns=400_000_000,
        available_at_ns=400_000_000,
    )
    result = solve_window(problem([observation]), asof_ns=400_000_000, smoothness=0)
    assert result.status == "converged"
    x = result.values
    assert 0.25 * math.fsum(x[0]) + 0.75 * math.fsum(x[1]) == pytest.approx(10)
    assert math.fsum(x[0]) != pytest.approx(10)
    assert result.bounds[0][0].upper == pytest.approx(40)
    assert result.bounds[1][0].upper == pytest.approx(10 / 0.75)
    assert result.provenance == "synthetic"
    assert result.source_event_count is None


def test_fixed_snapshot_oracle_and_trade_does_not_force_resting_execution():
    obs = [
        LinearObservation(((0, 1), (1, 1)), 10, 0, 0, "start"),
        LinearObservation(((2, 1), (3, 1)), 6, 100_000_000, 100_000_000, "end"),
    ]
    result = solve_window(
        problem(obs, fixed={0: 4, 1: 6, 2: 2, 3: 4}, executions=(TradeExecutionBound(1, 0, 100),)),
        asof_ns=400_000_000,
    )
    assert result.values == ((4, 6), (2, 4))
    assert result.flows[0].visible_execution == 0
    assert result.execution_bounds[0].upper == 100
    assert (
        result.flows[0].additions
        - result.flows[0].cancellations
        - result.flows[0].visible_execution
        == -2
    )


def test_future_information_is_rejected_and_retrospective_revision_is_separate():
    obs = LinearObservation(((0, 1),), 5, 0, 500_000_000, "later_snapshot")
    with pytest.raises(ValueError, match="future"):
        solve_window(problem([obs]), asof_ns=400_000_000)
    result = solve_window(
        problem([obs]), asof_ns=400_000_000, mode="retrospective", revision_id="repair-1"
    )
    assert result.mode == "retrospective" and result.revision_id == "repair-1"
    assert result.information_cutoff_ns == 500_000_000


def test_overlapping_constraints_feasible_intervals_and_unobserved_unbounded():
    obs = [
        LinearObservation(((0, 1), (1, 1)), 10, 0, 0, "a"),
        LinearObservation(((1, 1), (2, 1)), 5, 0, 0, "b"),
    ]
    result = solve_window(problem(obs), asof_ns=400_000_000, smoothness=0, max_seconds=5)
    assert result.status == "converged"
    assert result.bounds[0][0].lower == pytest.approx(5)
    assert result.bounds[0][0].upper == pytest.approx(10)
    assert result.bounds[1][1].upper is None
    assert result.bounds_status == "unbounded"


def test_dependency_free_default_and_explicit_unsupported_overlap(monkeypatch):
    original = builtins.__import__

    def no_scipy(name, *args, **kwargs):
        if name.startswith("scipy"):
            raise ImportError("optional extra absent")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_scipy)
    disjoint = LinearObservation(((0, 1), (1, 1)), 10, 0, 0, "a")
    result = solve_window(problem([disjoint]), asof_ns=400_000_000, smoothness=0)
    assert result.status == "converged" and result.bounds_status == "unbounded"
    overlap = LinearObservation(((1, 1), (2, 1)), 5, 0, 0, "b")
    result = solve_window(problem([disjoint, overlap]), asof_ns=400_000_000, smoothness=0)
    assert result.bounds_status == "unavailable_optional_scipy"
    assert "crocodile[ml]" in result.notes[-1]


def test_limits_infeasible_constraints_and_nonstatistical_result():
    obs = [LinearObservation(((0, 1),), 2, 0, 0, "a"), LinearObservation(((0, 1),), 3, 0, 0, "b")]
    result = solve_window(problem(obs), asof_ns=400_000_000, max_iterations=1, max_seconds=1)
    assert result.status == "unconverged" and result.iterations <= 1
    assert "not statistical" in result.bound_interpretation
    with pytest.raises(ValueError, match="3"):
        solve_window(WindowProblem((0,), (4_000_000_000,), ((1,),), ()), asof_ns=4_000_000_000)


def test_native_l2_coarse_down_oracle_preserves_real_duration_totals_and_ambiguity():
    native = ((2, 5, 1, 3), (4, 2, 2, 5), (1, 6, 8, 2), (3, 7, 4, 1))
    durations = (100_000_000, 300_000_000, 200_000_000, 400_000_000)
    observations = []
    for start in (0, 2):
        for group in ((0, 1), (2, 3)):
            quantity = sum(
                sum(native[t][i] for i in group) * durations[t] for t in (start, start + 1)
            ) / sum(durations[start : start + 2])
            observations.append(
                duration_average(
                    [start, start + 1],
                    group,
                    durations[start : start + 2],
                    levels=4,
                    quantity=quantity,
                    observed_at_ns=1_000_000_000,
                    available_at_ns=1_000_000_000,
                )
            )
    p = WindowProblem(
        (0, 100_000_000, 400_000_000, 600_000_000),
        durations,
        ((1, 1, 1, 1),) * 4,
        tuple(observations),
    )
    result = solve_window(p, asof_ns=1_000_000_000, max_seconds=2)
    again = solve_window(p, asof_ns=1_000_000_000, max_seconds=2)
    assert result.status == "converged"
    assert result.values == again.values
    assert result.residual < 1e-8
    assert result.bounds_status == "feasible"
    for t, row in enumerate(native):
        for i, native_quantity in enumerate(row):
            interval = result.bounds[t][i]
            assert interval.lower <= native_quantity <= interval.upper
    assert result.values != native
    assert result.provenance == "synthetic" and result.source_event_count is None
