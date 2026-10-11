import math

import pytest

from crocodile.core.reconstruction import project_quantity


def test_nonuniform_weights_independent_analytical_oracle():
    # lambda=-8/3 gives x=(8/3, 4/3), independently from sum x=4.
    result = project_quantity([0, 0], 4, weights=[1, 2])
    assert result.status == "converged"
    assert result.values == pytest.approx([8 / 3, 4 / 3])
    assert abs(math.fsum(result.values) - 4) < 1e-10


def test_bbo_fixed_forbidden_caps_and_rigorous_ambiguous_bounds():
    result = project_quantity([2, 2, 2, 2], 10, fixed={0: 3}, forbidden=[1], upper=[9, 9, 4, 9])
    assert result.values[0:2] == (3, 0)
    assert result.values == pytest.approx([3, 0, 3.5, 3.5])
    assert [(b.lower, b.upper) for b in result.bounds] == [(3, 3), (0, 0), (0, 4), (3, 7)]
    ambiguous = project_quantity([4, 6], 10)
    assert [(b.lower, b.upper) for b in ambiguous.bounds] == [(0, 10), (0, 10)]
    assert ambiguous.provenance == "synthetic"
    assert "not statistical" in ambiguous.bound_interpretation


@pytest.mark.parametrize(
    "prior,q,kwargs",
    [
        ([1], -1, {}),
        ([float("nan")], 1, {}),
        ([1], float("inf"), {}),
        ([1], 1, {"weights": [0]}),
        ([1], 1, {"fixed": {0: -1}}),
    ],
)
def test_malformed_inputs_fail_explicitly(prior, q, kwargs):
    with pytest.raises(ValueError):
        project_quantity(prior, q, **kwargs)


def test_infeasible_empty_and_iteration_cap():
    assert project_quantity([], 0).status == "converged"
    assert project_quantity([], 1).status == "infeasible"
    assert project_quantity([0, 0], 1, fixed={0: 2}).status == "infeasible"
    assert project_quantity([0, 0], 10, upper=[2, 3]).status == "infeasible"
    result = project_quantity([3, 1, 0], 2, max_iterations=1, tolerance=1e-16)
    assert result.status == "unconverged"
    assert result.iterations <= 1
