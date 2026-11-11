"""Spec 6 gate: every implementation of a capability whose parameters carry ``mic`` or
``country`` either filters by that dimension or refuses it.

Codemap 5.2, rule 3: a market dimension that is accepted and then ignored answers with every
market while claiming to answer for one, which is the plausible wrong answer this codebase
keeps guarding against. Equity halves must filter (spec 3.11); crypto halves have no MIC and
no ISO country, so they must refuse, and the refusal must name the dimension it refused.

Why a probe table and not one generic call: each capability needs its own offline world (a
listing master, a calendar, a registry) and its own way to read a row's MIC and country back.
A new capability that gains one of these fields fails
:func:`test_every_capability_with_a_market_dimension_has_a_probe` until someone writes its
probe, which is the point.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Literal

import msgspec
import polars as pl
import pytest

from crocodile.capabilities import load_all, market
from crocodile.core.capability import REGISTRY, AssetClass, CapabilityContext
from crocodile.core.config import Settings
from crocodile.core.errors import CapabilityUnavailable
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.master import ListingMaster, ListingObservation, ListingStatus
from crocodile.core.markets.symbols import parse_listing_symbol
from crocodile.core.schema.enums import SecurityType

_PROBE_VALUES: Final[dict[str, str]] = {"mic": "XJPX", "country": "JP"}
"""One value per dimension, chosen so the slice is non-empty and excludes other markets."""


def _dimensional() -> dict[str, tuple[str, ...]]:
    """Every registered capability whose parameters carry a market dimension, and which ones."""
    load_all()
    return {
        name: dims
        for name, cap in sorted(REGISTRY.items())
        if (
            dims := tuple(
                f.name for f in msgspec.structs.fields(cap.params) if f.name in _PROBE_VALUES
            )
        )
    }


def _country_of(mic: str) -> str:
    return ExchangeRegistry().get(mic).country


def _markets_rows(frame: pl.DataFrame) -> list[dict[str, str | None]]:
    return [{"mic": row["mic"], "country": row["country"]} for row in frame.to_dicts()]


def _universe_rows(frame: pl.DataFrame) -> list[dict[str, str | None]]:
    rows: list[dict[str, str | None]] = []
    for row in frame.to_dicts():
        _, mic = parse_listing_symbol(row["symbol"])
        rows.append({"mic": mic, "country": None if mic is None else _country_of(mic)})
    return rows


def _calendar_rows(frame: pl.DataFrame) -> list[dict[str, str | None]]:
    return [
        {"mic": row["mic"], "country": None if row["mic"] is None else _country_of(row["mic"])}
        for row in frame.to_dicts()
    ]


@dataclass(frozen=True)
class _Probe:
    build: Callable[[dict[str, str]], msgspec.Struct]
    """Params for the capability with the dimension set."""
    observe: Callable[[pl.DataFrame], list[dict[str, str | None]]] | None = None
    """Per row: ``{"mic": ..., "country": ...}``. Used for capabilities that return rows."""
    check: Callable[[object, str, str], None] | None = None
    """``(result, dimension, value)``; raises ``AssertionError`` if the slice was not applied.
    For capabilities that return work instead of rows (task 14's ``collect`` and ``backfill``
    return a Subscription or a coroutine). Exactly one of ``observe`` and ``check`` is set."""


_PROBES: dict[str, _Probe] = {
    "markets": _Probe(build=lambda dims: market.MarketsParams(**dims), observe=_markets_rows),
    "universe": _Probe(build=lambda dims: market.UniverseParams(**dims), observe=_universe_rows),
    "calendar": _Probe(
        build=lambda dims: market.CalendarParams(start="2026-01-05", end="2026-01-09", **dims),
        observe=_calendar_rows,
    ),
}
"""A later task that adds ``mic`` or ``country`` to another struct adds its probe here."""


def _filters_or_refuses(
    call: Callable[[], object], probe: _Probe, dimension: str, value: str
) -> Literal["filtered", "refused"]:
    """Run ``call`` and say whether it filtered by ``dimension`` or refused it.

    A refusal must name the dimension in its reason. A filter must return a non-empty slice
    (an empty one proves nothing about filtering) and no row outside the slice.
    """
    try:
        result = call()
    except CapabilityUnavailable as exc:
        assert dimension in exc.reason, f"refused without naming {dimension!r}: {exc.reason}"
        return "refused"
    if probe.check is not None:
        probe.check(result, dimension, value)
        return "filtered"
    assert probe.observe is not None, "a probe must set observe or check"
    assert isinstance(result, pl.DataFrame), f"expected rows, got {type(result).__name__}"
    rows = probe.observe(result)
    assert rows, (
        f"{dimension}={value!r} filtered to nothing; a probe must stand in a non-empty slice"
    )
    wrong = [row for row in rows if row[dimension] != value]
    assert not wrong, f"{dimension}={value!r} ignored: {wrong[:3]}"
    return "filtered"


def _ctx(asset_class: str) -> CapabilityContext:
    return CapabilityContext(catalog=None, settings=Settings(), asset_class=AssetClass(asset_class))


@pytest.fixture
def _listing_world(monkeypatch: pytest.MonkeyPatch) -> None:
    """7203 and 6758 on XJPX (JPY) and THYAO on XIST (TRY), so the universe probe has rows
    outside the slice that must be dropped.

    The seam returns a fresh master per call because the adapter closes what it opens.
    """

    def _fresh_master(ctx: CapabilityContext) -> ListingMaster:
        master = ListingMaster(":memory:")
        for mic, local, currency in (
            ("XJPX", "7203", "JPY"),
            ("XJPX", "6758", "JPY"),
            ("XIST", "THYAO", "TRY"),
        ):
            master.upsert(
                ListingObservation(
                    source="fixture",
                    mic=mic,
                    local_symbol=local,
                    symbol_raw=f"{mic}:{local}",
                    observed_ns=1,
                    currency=currency,
                    price_scale=0,
                    status=ListingStatus.ACTIVE,
                    security_type=SecurityType.CS,
                )
            )
        return master

    monkeypatch.setattr(market, "_listing_master", _fresh_master)


# ---------------------------------------------------------------------------
# The population and its probes
# ---------------------------------------------------------------------------


def test_the_population_is_not_empty_and_names_the_three() -> None:
    assert set(_dimensional()) >= {"markets", "universe", "calendar"}


def test_every_capability_with_a_market_dimension_has_a_probe() -> None:
    assert set(_dimensional()) == set(_PROBES), (
        f"capabilities with a market dimension {sorted(_dimensional())} do not match the "
        f"probes {sorted(_PROBES)}: add a _PROBES entry for each new one"
    )


_CASES: Final[list[tuple[str, str, str]]] = [
    (name, dimension, asset.value)
    for name, dims in _dimensional().items()
    for dimension in dims
    for asset in sorted(REGISTRY[name].impls, key=lambda a: a.value)
]


@pytest.mark.parametrize(("name", "dimension", "asset_class"), _CASES)
def test_every_implementation_filters_or_refuses_a_market_dimension(
    name: str,
    dimension: str,
    asset_class: str,
    network_forbidden: None,
    _listing_world: None,
) -> None:
    probe = _PROBES[name]
    impl = REGISTRY[name].impls[AssetClass(asset_class)]
    value = _PROBE_VALUES[dimension]
    outcome = _filters_or_refuses(
        lambda: impl.fn(_ctx(asset_class), probe.build({dimension: value})),
        probe,
        dimension,
        value,
    )
    expected = "refused" if asset_class == AssetClass.CRYPTO.value else "filtered"
    assert outcome == expected, (
        f"{name} ({asset_class}) {dimension}={value!r}: {outcome}, expected {expected}"
    )


def test_every_probe_sets_exactly_one_of_observe_and_check() -> None:
    for name, probe in _PROBES.items():
        assert (probe.observe is None) != (probe.check is None), (
            f"probe {name!r} must set exactly one of observe and check"
        )


# ---------------------------------------------------------------------------
# The gate has to be able to fail, or it proves nothing
# ---------------------------------------------------------------------------


def test_the_check_rejects_an_implementation_that_ignores_the_dimension() -> None:
    """An implementation that returns XJPX and XIST rows under ``mic="XJPX"`` must be named."""
    ctx = _ctx("equity")
    ignored = pl.concat(
        [
            market.markets_equities(ctx, market.MarketsParams(mic="XJPX")),
            market.markets_equities(ctx, market.MarketsParams(mic="XIST")),
        ]
    )
    with pytest.raises(AssertionError, match="XIST"):
        _filters_or_refuses(lambda: ignored, _PROBES["markets"], "mic", "XJPX")


def test_the_check_rejects_a_refusal_that_does_not_say_why() -> None:
    def refuse() -> object:
        raise CapabilityUnavailable("markets", "crypto", reason="not today")

    with pytest.raises(AssertionError, match="refused without naming"):
        _filters_or_refuses(refuse, _PROBES["markets"], "mic", "XJPX")


def test_the_check_rejects_a_filter_that_returns_nothing() -> None:
    empty = pl.DataFrame([], schema=market._MARKETS_SCHEMA)
    with pytest.raises(AssertionError, match="filtered to nothing"):
        _filters_or_refuses(lambda: empty, _PROBES["markets"], "mic", "XJPX")


def test_a_work_returning_probe_delegates_to_its_check() -> None:
    """Task 14's hook: a capability that returns work is judged by its own ``check``."""

    def ignored(result: object, dimension: str, value: str) -> None:
        raise AssertionError("slice ignored")

    def honoured(result: object, dimension: str, value: str) -> None:
        return None

    def build(dims: dict[str, str]) -> Any:
        return market.MarketsParams(**dims)

    with pytest.raises(AssertionError, match="slice ignored"):
        _filters_or_refuses(lambda: object(), _Probe(build=build, check=ignored), "mic", "XJPX")
    assert (
        _filters_or_refuses(lambda: object(), _Probe(build=build, check=honoured), "mic", "XJPX")
        == "filtered"
    )
