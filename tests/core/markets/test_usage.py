"""UsageClass names and the most-restrictive merge rule."""

from __future__ import annotations

import pytest

from crocodile.core.markets.usage import RESTRICTIVENESS, UsageClass, most_restrictive

pytestmark = pytest.mark.usefixtures("network_forbidden")


def test_usage_classes_are_exactly_the_three_the_spec_names() -> None:
    assert [m.value for m in UsageClass] == ["redistribution", "display", "internal"]


def test_usage_classes_compare_equal_to_their_strings() -> None:
    assert UsageClass("internal") is UsageClass.INTERNAL
    assert UsageClass.INTERNAL == "internal"


def test_restrictiveness_orders_redistribution_then_display_then_internal() -> None:
    assert RESTRICTIVENESS[UsageClass.REDISTRIBUTION] == 0
    assert RESTRICTIVENESS[UsageClass.DISPLAY] == 1
    assert RESTRICTIVENESS[UsageClass.INTERNAL] == 2


@pytest.mark.parametrize(
    ("classes", "expected"),
    [
        ([UsageClass.REDISTRIBUTION], UsageClass.REDISTRIBUTION),
        ([UsageClass.REDISTRIBUTION, UsageClass.DISPLAY], UsageClass.DISPLAY),
        ([UsageClass.DISPLAY, UsageClass.REDISTRIBUTION], UsageClass.DISPLAY),
        ([UsageClass.REDISTRIBUTION, UsageClass.INTERNAL], UsageClass.INTERNAL),
        ([UsageClass.DISPLAY, UsageClass.INTERNAL, UsageClass.REDISTRIBUTION], UsageClass.INTERNAL),
        ([UsageClass.INTERNAL, UsageClass.INTERNAL], UsageClass.INTERNAL),
        ([UsageClass.REDISTRIBUTION, UsageClass.REDISTRIBUTION], UsageClass.REDISTRIBUTION),
    ],
)
def test_most_restrictive_orders_internal_over_display_over_redistribution(
    classes: list[UsageClass], expected: UsageClass
) -> None:
    assert most_restrictive(classes) is expected


def test_most_restrictive_accepts_any_iterable() -> None:
    assert most_restrictive(iter([UsageClass.DISPLAY])) is UsageClass.DISPLAY


def test_most_restrictive_refuses_an_empty_input() -> None:
    with pytest.raises(ValueError):
        most_restrictive([])
