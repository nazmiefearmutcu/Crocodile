"""What a column's name promises about its unit, and the one suffix that promises twice.

``_pct`` means *percent* in one module of ``core.analytics`` and *fraction* in the others.
``core/analytics/slippage.py`` multiplies by 100; ``core/analytics/carry.py``'s
``basis_pct``, ``annualized_pct`` and ``carry_pct`` are fractions, as are
``crypto/analytics/peg_deviation.py``'s ``deviation_pct`` and the bands
``core/analytics/liquidity_depth.py`` measures against. That is a factor of 100 in one
direction for a consumer that treats them alike, and no surface annotates either.

``slippage.py`` is base code no branch in this phase touched, so the divergence was
inherited rather than created — but this phase added four new *fraction*-valued ``_pct``
columns into the same package under the same suffix, which is what turned a quirk into a
pattern. Nobody is renaming a shipped wire column to fix it: ``slippage_pct`` is in the
pinned pre-merge surface inventory, and changing either its name or its unit is a silent
break for every consumer that already divides or does not.

So the units are *declared*, once, where a gate can check that every one of them is
declared. The value of the table is not that it converts anything — it converts nothing —
but that the next author to write a ``_pct`` column has to write a line here saying which
of the three conventions they picked, instead of picking one by copying whichever
neighbour they happened to read.
"""

from __future__ import annotations

import pathlib
import re

import pytest

import crocodile

_SRC = pathlib.Path(crocodile.__file__).parent

_PCT_LITERAL = re.compile(r'"([a-z][a-z0-9_]*_pct)"')
"""Quoted lower-snake names ending in ``_pct``.

String literals rather than identifiers, because the subject is what reaches a *consumer*:
a Polars column name, a dict key, a parquet schema entry. A local variable spelled
``slippage_pct`` that never leaves the function is nobody's contract, and a gate that
flagged it would train people to rename locals rather than to declare units.
"""

FRACTION = "fraction"
"""0.0546 means 5.46 %."""

PERCENT = "percent"
"""5.46 means 5.46 %."""

EITHER = "either, normalised by magnitude"
"""The caller may supply 20 or 0.20 and the implementation decides by size."""


PCT_UNITS: dict[str, tuple[str, str]] = {
    "basis_pct": (
        FRACTION,
        "`spread()` returns `(rich - cheap) / cheap` and nothing scales it. Shared by both "
        "asset classes' basis capabilities, so it is the widest-reaching entry here.",
    ),
    "annualized_pct": (
        FRACTION,
        "`annualise_over_days` multiplies `basis_pct` by `365 / days`, which cannot change "
        "its unit.",
    ),
    "carry_pct": (
        FRACTION,
        "`annualized_pct - risk_free_rate`, and the Treasury par yield it subtracts is "
        "stored as a fraction (`RiskFreeQuote.rate`). A subtraction between two units "
        "would be the bug this entry exists to make visible.",
    ),
    "deviation_pct": (
        FRACTION,
        "`abs(price - 1.0)` for a stablecoin, so a 1 % depeg is 0.01. Its threshold is "
        "compared against it directly, so both sides of `peg-deviation` move together.",
    ),
    "change_pct": (
        FRACTION,
        "`ShortInterest.change_pct`. No producer in this tree writes it — the field is "
        "declared and the parquet schema carries it, and it is filled only from outside — "
        "so the fraction reading is the one every other `_pct` in the record union takes, "
        "asserted here so that whoever wires a producer has to agree or change this line.",
    ),
    "slippage_pct": (
        PERCENT,
        "`(slippage_usd / best_price) * 100.0`. THE OUTLIER, and it stays one: it is a "
        "shipped wire column named in the pre-merge surface inventory, so changing the "
        "unit under the name is a silent break for every consumer that already divides, "
        "and changing the name breaks the inventory. Declared loudly instead.",
    ),
    "haircut_pct": (
        EITHER,
        "An *input* to `lending-stress`, not an output, and it is normalised by magnitude: "
        "`haircut_pct / 100.0 if abs(haircut_pct) > 1.0 else haircut_pct`. That is a third "
        "convention and it is a legacy one the port preserved deliberately, because both "
        "spellings were already in callers' scripts. It is listed so that 'normalised by "
        "magnitude' is a stated behaviour rather than a discovered one — note it cannot "
        "express a haircut above 100 %.",
    ),
    "share_pct": (
        PERCENT,
        "`dark_pool_market_share` computes `(shares / total_shares) * 100.0` as percentage of volume.",
    ),
}
"""Every ``_pct`` name that reaches a consumer, its unit, and why it is that one.

An entry is a decision somebody made. The gate below is what makes it one: a new ``_pct``
column with no entry fails, and the failure asks the only question that matters, which is
which of the three conventions above the new column follows.
"""


def _declared_pct_names() -> set[str]:
    found: set[str] = set()
    for path in _SRC.rglob("*.py"):
        found.update(_PCT_LITERAL.findall(path.read_text(encoding="utf-8")))
    return found


def test_every_pct_column_in_the_tree_declares_its_unit() -> None:
    """A suffix that means two things has to say which, per column.

    The sweep is over the source rather than over a list, so a column added in a branch
    that never opens this file still lands here — which is the failure mode the merge kept
    producing: two branches, one shared vocabulary, and no place where the vocabulary is
    written down.
    """
    undeclared = sorted(_declared_pct_names() - set(PCT_UNITS))
    assert not undeclared, (
        f"these `_pct` columns declare no unit: {undeclared}. The suffix means percent in "
        f"core/analytics/slippage.py and fraction everywhere else, so a reader cannot infer "
        f"it. Add an entry to PCT_UNITS saying which, and why."
    )


def test_the_unit_table_holds_no_column_the_tree_no_longer_has() -> None:
    """A table that outlives its columns starts describing a tree that has moved."""
    stale = sorted(set(PCT_UNITS) - _declared_pct_names())
    assert not stale, f"PCT_UNITS names columns nothing writes: {stale}"


@pytest.mark.parametrize("name", sorted(PCT_UNITS))
def test_each_declared_unit_carries_its_argument(name: str) -> None:
    unit, why = PCT_UNITS[name]
    assert unit in {FRACTION, PERCENT, EITHER}, f"{name} declares an unknown unit {unit!r}"
    assert why.strip(), f"{name} is declared with no argument"


def test_the_two_conventions_both_have_members() -> None:
    """Deriving the subject from a table means the test stops testing if the table
    collapses onto one convention — at which point the divergence is gone and this file
    should be deleted rather than left passing over nothing."""
    units = {unit for unit, _ in PCT_UNITS.values()}
    assert FRACTION in units, "no fraction-valued _pct column, so the majority case is gone"
    assert PERCENT in units, (
        "no percent-valued _pct column remains. If slippage_pct was converted or renamed, "
        "the divergence this file documents is over and the file should go with it."
    )


def test_the_fraction_valued_columns_really_are_fractions() -> None:
    """The table is checked against the arithmetic, not taken on trust.

    A one-percent spread on a 100.00 cash leg is 0.01 and not 1.0, and a one-cent depeg is
    0.01 and not 1.0. Both are computed here through the shipped helpers, so an entry that
    stopped being true would fail rather than sit.
    """
    from crocodile.core.analytics.carry import annualise_over_days, spread
    from crocodile.crypto.analytics.peg_deviation import peg_deviation_from_price

    _, basis_pct = spread(101.0, 100.0)
    assert basis_pct == pytest.approx(0.01)
    assert annualise_over_days(basis_pct, 365.0) == pytest.approx(0.01)
    assert peg_deviation_from_price(0.99)["deviation_pct"] == pytest.approx(0.01)


def test_slippage_pct_really_is_a_percent() -> None:
    """The outlier, asserted rather than described — a 1 % walk reports 1.0, not 0.01."""
    from crocodile.core.analytics.slippage import slippage_over_levels

    frame = slippage_over_levels(
        "deribit:BTC-PERPETUAL",
        "buy",
        2.0,
        bids=[(99.0, 10.0)],
        asks=[(100.0, 1.0), (102.0, 1.0)],
    )
    row = frame.row(0, named=True)
    assert row["best_price"] == pytest.approx(100.0)
    assert row["expected_price"] == pytest.approx(101.0)
    assert row["slippage_pct"] == pytest.approx(1.0), "one percent, spelled 1.0"
