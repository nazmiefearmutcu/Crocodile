"""The three provenance bases of the live layer: levels, formulas and descriptions."""

from __future__ import annotations

import pytest

from crocodile.core.schema.provenance import (
    ConfidenceInputError,
    Provenance,
    confidence_for,
    describe,
    level_for,
    load_all_bases,
    provenance_fields,
    registered_bases,
)
from crocodile.equity.universal.live import bases
from crocodile.equity.universal.live.bases import (
    BASIS_POLLED_BOOK,
    BASIS_POLLED_PRINT,
    BASIS_VOLUME_DELTA,
)

_LIVE_BASES = (BASIS_POLLED_BOOK, BASIS_POLLED_PRINT, BASIS_VOLUME_DELTA)


def test_basis_names_are_the_documented_strings() -> None:
    assert BASIS_POLLED_BOOK == "polled_book_snapshot"
    assert BASIS_POLLED_PRINT == "polled_last_print"
    assert BASIS_VOLUME_DELTA == "volume_delta_print"


def test_the_three_bases_are_registered_in_the_shipped_registry() -> None:
    load_all_bases()
    assert set(_LIVE_BASES) <= registered_bases()


def test_documented_levels() -> None:
    assert level_for(BASIS_POLLED_BOOK) is Provenance.NATIVE
    assert level_for(BASIS_POLLED_PRINT) is Provenance.NATIVE
    assert level_for(BASIS_VOLUME_DELTA) is Provenance.DERIVED


def test_polled_bases_are_a_constant_one() -> None:
    assert confidence_for(BASIS_POLLED_BOOK, {}) == 1.0
    assert confidence_for(BASIS_POLLED_PRINT, {}) == 1.0
    assert confidence_for(BASIS_POLLED_BOOK, {"cadence_s": 10.0, "gap_s": 500.0}) == 1.0


def test_polled_book_record_tail_is_native_with_no_inputs() -> None:
    tail = provenance_fields(BASIS_POLLED_BOOK)
    assert tail.prov is Provenance.NATIVE
    assert tail.prov_basis == BASIS_POLLED_BOOK
    assert tail.prov_confidence == 1.0
    assert tail.prov_inputs == []


def test_volume_delta_confidence_is_cadence_over_gap() -> None:
    assert confidence_for(BASIS_VOLUME_DELTA, {"cadence_s": 10.0, "gap_s": 10.0}) == 1.0
    assert confidence_for(BASIS_VOLUME_DELTA, {"cadence_s": 10.0, "gap_s": 20.0}) == 0.5
    assert confidence_for(BASIS_VOLUME_DELTA, {"cadence_s": 1.5, "gap_s": 3.0}) == 0.5
    assert confidence_for(BASIS_VOLUME_DELTA, {"cadence_s": 10, "gap_s": 120}) == pytest.approx(
        1 / 12
    )


def test_volume_delta_confidence_is_capped_at_one() -> None:
    assert confidence_for(BASIS_VOLUME_DELTA, {"cadence_s": 10.0, "gap_s": 4.0}) == 1.0


def test_volume_delta_tail_carries_the_trade_input() -> None:
    tail = provenance_fields(BASIS_VOLUME_DELTA, {"cadence_s": 10.0, "gap_s": 40.0})
    assert tail.prov is Provenance.DERIVED
    assert tail.prov_basis == BASIS_VOLUME_DELTA
    assert tail.prov_confidence == 0.25
    assert tail.prov_inputs == ["trade"]


def test_volume_delta_missing_inputs_are_refused() -> None:
    with pytest.raises(ConfidenceInputError):
        confidence_for(BASIS_VOLUME_DELTA, {})
    with pytest.raises(ConfidenceInputError):
        confidence_for(BASIS_VOLUME_DELTA, {"cadence_s": 10.0})
    with pytest.raises(ConfidenceInputError):
        confidence_for(BASIS_VOLUME_DELTA, {"gap_s": 10.0})


def test_volume_delta_non_numeric_inputs_are_refused() -> None:
    with pytest.raises(ConfidenceInputError):
        confidence_for(BASIS_VOLUME_DELTA, {"cadence_s": "ten", "gap_s": 10.0})
    with pytest.raises(ConfidenceInputError):
        confidence_for(BASIS_VOLUME_DELTA, {"cadence_s": 10.0, "gap_s": None})
    with pytest.raises(ConfidenceInputError):
        confidence_for(BASIS_VOLUME_DELTA, {"cadence_s": [10.0], "gap_s": 10.0})


def test_volume_delta_non_positive_inputs_are_refused() -> None:
    for cadence, gap in ((0.0, 10.0), (-5.0, 10.0), (10.0, 0.0), (10.0, -1.0)):
        with pytest.raises(ConfidenceInputError):
            confidence_for(BASIS_VOLUME_DELTA, {"cadence_s": cadence, "gap_s": gap})


def test_volume_delta_non_finite_inputs_are_refused() -> None:
    nan = float("nan")
    inf = float("inf")
    for cadence, gap in ((nan, 10.0), (10.0, nan), (inf, 10.0), (10.0, inf)):
        with pytest.raises(ConfidenceInputError):
            confidence_for(BASIS_VOLUME_DELTA, {"cadence_s": cadence, "gap_s": gap})


def test_every_live_basis_has_a_description() -> None:
    for basis in _LIVE_BASES:
        assert describe(basis).strip() != ""


def test_volume_delta_description_names_its_formula() -> None:
    assert "min(1, cadence_s / gap_s)" in describe(BASIS_VOLUME_DELTA)


def test_the_module_exports_the_three_names() -> None:
    assert set(bases.__all__) == {"BASIS_POLLED_BOOK", "BASIS_POLLED_PRINT", "BASIS_VOLUME_DELTA"}
