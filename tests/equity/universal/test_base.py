"""Shared seams of the universal sources (task 10a, step 2): unservable errors, the daily-bar
timestamp rule, ISIN shape, the universe snapshot write-back and the source contract check."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from types import MappingProxyType
from zoneinfo import ZoneInfo

import pytest

from crocodile.core.markets.master import ListingMaster, ListingObservation
from crocodile.core.markets.usage import UsageClass
from crocodile.core.schema.enums import Channel
from crocodile.equity.providers.base import ProviderError
from crocodile.equity.universal.base import (
    SourceUnservableError,
    UniverseSnapshot,
    UnservableReason,
    apply_universe,
    contract_violations,
    daily_bar_ts_ns,
    is_valid_isin,
    session_date_of,
)


def test_unservable_error_names_source_and_reason() -> None:
    error = SourceUnservableError("yahoo_chart", UnservableReason.FORBIDDEN, "HTTP 403")
    assert isinstance(error, ProviderError)
    assert error.source == "yahoo_chart"
    assert error.reason is UnservableReason.FORBIDDEN
    assert error.detail == "HTTP 403"
    assert str(error) == "yahoo_chart is unservable (forbidden): HTTP 403"


def test_daily_bar_ts_is_utc_midnight_of_the_session_date() -> None:
    assert daily_bar_ts_ns(date(2026, 11, 2)) == 1_793_577_600_000_000_000


def test_session_date_uses_the_exchange_timezone() -> None:
    # 1793574000 is 2026-11-01 23:00 UTC: the Sydney session date is already 2026-11-02.
    assert session_date_of(1793574000, ZoneInfo("Australia/Sydney")) == date(2026, 11, 2)
    assert session_date_of(1793574000, ZoneInfo("UTC")) == date(2026, 11, 1)


@pytest.mark.parametrize("value", ["US0378331005", "INE002A01018"])
def test_isin_check_digit_accepts_valid_isins(value: str) -> None:
    assert is_valid_isin(value) is True


@pytest.mark.parametrize("value", ["INE002A01019", "US037833100", "us0378331005", ""])
def test_isin_check_digit_rejects_invalid_isins(value: str) -> None:
    assert is_valid_isin(value) is False


def _observation(
    local: str, mic: str, *, observed_ns: int, isin: str | None = None, source: str = "yahoo_chart"
) -> ListingObservation:
    return ListingObservation(
        source=source,
        mic=mic,
        local_symbol=local,
        symbol_raw=local,
        observed_ns=observed_ns,
        isin=isin,
    )


def _snapshot(
    observations: tuple[ListingObservation, ...],
    *,
    seen: Mapping[str, frozenset[str]],
    complete: bool,
    captured_at_ns: int = 10,
) -> UniverseSnapshot:
    return UniverseSnapshot(
        source="yahoo_chart",
        captured_at_ns=captured_at_ns,
        observations=observations,
        records=(),
        seen=seen,
        complete=complete,
        skipped={},
    )


def test_apply_universe_upserts_every_observation() -> None:
    master = ListingMaster(":memory:")
    snapshot = _snapshot(
        (
            _observation("THYAO", "XIST", observed_ns=5),
            _observation("ASELS", "XIST", observed_ns=5),
        ),
        seen={"XIST": frozenset({"THYAO", "ASELS"})},
        complete=True,
    )
    report = apply_universe(master, snapshot)
    assert report.source == "yahoo_chart"
    assert report.upserted == 2
    assert report.conflicts == ()
    assert master.find("XIST", "THYAO") is not None


def test_apply_universe_marks_unseen_only_for_a_complete_read() -> None:
    master = ListingMaster(":memory:")
    master.upsert(_observation("THYAO", "XIST", observed_ns=1))
    seen = {"XIST": frozenset({"ASELS"})}

    incomplete = apply_universe(
        master,
        _snapshot((_observation("ASELS", "XIST", observed_ns=5),), seen=seen, complete=False),
    )
    assert incomplete.unseen_marked_mics == ()
    thyao = master.find("XIST", "THYAO")
    assert thyao is not None and thyao.status == "active"

    complete = apply_universe(
        master,
        _snapshot((_observation("ASELS", "XIST", observed_ns=5),), seen=seen, complete=True),
    )
    assert complete.unseen_marked_mics == ("XIST",)
    thyao = master.find("XIST", "THYAO")
    assert thyao is not None and thyao.status == "unseen"


def test_apply_universe_records_a_conflict_and_continues() -> None:
    master = ListingMaster(":memory:")
    snapshot = _snapshot(
        (
            _observation("AAPL", "XNAS", observed_ns=5, isin="US0378331005"),
            _observation("AAPL", "XNAS", observed_ns=6, isin="US5949181045"),
            _observation("MSFT", "XNAS", observed_ns=6, isin="US5949181045"),
        ),
        seen={"XNAS": frozenset({"AAPL", "MSFT"})},
        complete=False,
    )
    report = apply_universe(master, snapshot)
    assert report.upserted == 2
    assert len(report.conflicts) == 1
    assert "AAPL" in report.conflicts[0]
    assert master.find("XNAS", "MSFT") is not None
    assert report.unseen_marked_mics == ()


def test_contract_violations_lists_each_missing_declaration() -> None:
    class Bare:
        pass

    violations = contract_violations(Bare)
    assert len(violations) == 7
    for item in (
        "name",
        "usage_class",
        "supported_channels",
        "supported_mics",
        "unservable",
        "attribution",
        "grammar",
    ):
        assert any(item in message for message in violations), item


def test_contract_violations_is_empty_for_a_declared_source() -> None:
    class Declared:
        name = "dummy_source"
        usage_class = UsageClass.INTERNAL
        supported_channels = frozenset({Channel.OHLCV.value, Channel.CORP_ACTION.value})
        supported_mics = frozenset({"XNYS"})
        unservable = MappingProxyType(
            {UnservableReason.TIMEOUT: "client-side timeout is not retried"}
        )
        attribution = None

        @classmethod
        def grammar(cls) -> None:
            return None

    assert contract_violations(Declared) == []
