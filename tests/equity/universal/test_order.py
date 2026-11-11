"""The per-market source order: table model, validation, registry introspection and the builder.

Every test here is offline (``network_forbidden``). The packaged table is checked against the
live registries, so a drift between ``source_order.csv`` and the factory fails here first.
"""

from __future__ import annotations

import pytest

from crocodile.core.markets.exchanges import ExchangeInfo, ExchangeRegistry
from crocodile.equity.providers import factory
from crocodile.equity.universal.order import (
    ORDER_CHANNELS,
    UNIVERSAL_ALIAS,
    UNIVERSAL_ORDER,
    US_NATIVE_ORDER,
    OrderRow,
    SourceCapability,
    SourceKind,
    SourceOrder,
    SourceOrderError,
    build_default_rows,
    directory_source_names,
    is_universal_source,
    registry_capabilities,
    source_order,
    universal_source_names,
)

pytestmark = pytest.mark.usefixtures("network_forbidden")

_HEADER = "mic,channel,sources,notes"


def _cap(
    name: str,
    channels: tuple[str, ...],
    mics: tuple[str, ...] | None = None,
    kind: SourceKind = SourceKind.UNIVERSAL,
) -> SourceCapability:
    return SourceCapability(
        name=name,
        kind=kind,
        channels=frozenset(channels),
        mics=None if mics is None else frozenset(mics),
    )


def _full_rows(mics: tuple[str, ...], sources: tuple[str, ...]) -> list[OrderRow]:
    """One row per (mic, channel) naming every source, so validation sees no completeness gap."""
    return [OrderRow(mic, channel, sources) for mic in mics for channel in ORDER_CHANNELS]


# ---------------------------------------------------------------------------
# The table model
# ---------------------------------------------------------------------------


def test_an_order_row_keeps_its_sources_in_the_declared_order() -> None:
    order = SourceOrder([OrderRow("XIST", "ohlcv", ("yahoo_chart", "tradingview"))])
    assert order.order_for("XIST", "ohlcv") == ("yahoo_chart", "tradingview")


def test_a_duplicate_market_and_channel_is_refused() -> None:
    with pytest.raises(SourceOrderError) as exc:
        SourceOrder(
            [
                OrderRow("XIST", "ohlcv", ("yahoo_chart",)),
                OrderRow("XIST", "ohlcv", ("tradingview",)),
            ]
        )
    assert "XIST" in str(exc.value)
    assert "ohlcv" in str(exc.value)


def test_a_channel_outside_the_three_ordered_channels_is_refused() -> None:
    with pytest.raises(SourceOrderError) as exc:
        SourceOrder([OrderRow("XIST", "quote", ("yahoo_chart",))])
    assert "quote" in str(exc.value)


def test_a_source_named_twice_in_one_row_is_refused() -> None:
    with pytest.raises(SourceOrderError) as exc:
        SourceOrder([OrderRow("XIST", "ohlcv", ("yahoo_chart", "yahoo_chart"))])
    assert "yahoo_chart" in str(exc.value)


def test_the_universal_alias_may_not_appear_as_a_source() -> None:
    with pytest.raises(SourceOrderError) as exc:
        SourceOrder([OrderRow("XIST", "ohlcv", (UNIVERSAL_ALIAS,))])
    assert UNIVERSAL_ALIAS in str(exc.value)


def test_order_for_an_unknown_pair_raises_source_order_error() -> None:
    order = SourceOrder([OrderRow("XIST", "ohlcv", ("yahoo_chart",))])
    with pytest.raises(SourceOrderError):
        order.order_for("XNSE", "ohlcv")
    with pytest.raises(SourceOrderError):
        order.order_for("XIST", "instrument")


def test_the_csv_round_trips() -> None:
    order = SourceOrder(
        [
            OrderRow("XIST", "ohlcv", ("yahoo_chart", "tradingview")),
            OrderRow("XIST", "corp_action", ()),
            OrderRow("XIST", "instrument", ("tradingview",), notes="a note"),
        ]
    )
    text = order.to_csv()
    assert text.splitlines()[0] == _HEADER
    assert "XIST,corp_action,,\n" in text
    assert "\r" not in text
    again = SourceOrder.from_csv(text)
    assert again.rows() == order.rows()
    assert again.order_for("XIST", "corp_action") == ()


def test_a_csv_with_the_wrong_header_is_refused() -> None:
    with pytest.raises(SourceOrderError) as exc:
        SourceOrder.from_csv("mic,channel,sources\nXIST,ohlcv,yahoo_chart\n")
    assert "mic,channel,sources,notes" in str(exc.value)


# ---------------------------------------------------------------------------
# Validation against the registries
# ---------------------------------------------------------------------------

_MICS = ("XIST", "XNSE")
_KNOWN = frozenset(_MICS)


def _good_capabilities() -> dict[str, SourceCapability]:
    return {
        "yahoo_chart": _cap("yahoo_chart", ("ohlcv", "corp_action")),
        "tradingview": _cap("tradingview", ("ohlcv", "instrument")),
    }


def test_validation_names_an_unregistered_source() -> None:
    order = SourceOrder(_full_rows(_MICS, ("ghost_source",)))
    with pytest.raises(SourceOrderError) as exc:
        order.validate(capabilities=_good_capabilities(), known_mics=_KNOWN)
    assert "ghost_source" in str(exc.value)
    assert "not registered" in str(exc.value)


def test_validation_names_a_mic_missing_from_exchanges_csv() -> None:
    rows = [*_full_rows(_MICS, ()), OrderRow("ZZZZ", "ohlcv", ("yahoo_chart",))]
    with pytest.raises(SourceOrderError) as exc:
        SourceOrder(rows).validate(capabilities=_good_capabilities(), known_mics=_KNOWN)
    assert "ZZZZ" in str(exc.value)
    assert "exchanges.csv" in str(exc.value)


def test_validation_names_a_source_that_does_not_declare_the_channel() -> None:
    # tradingview declares ohlcv and instrument, not corp_action.
    rows = _full_rows(_MICS, ())
    rows = [row for row in rows if not (row.channel == "corp_action")]
    rows.append(OrderRow("XIST", "corp_action", ("tradingview",)))
    rows.append(OrderRow("XNSE", "corp_action", ()))
    with pytest.raises(SourceOrderError) as exc:
        SourceOrder(rows).validate(capabilities=_good_capabilities(), known_mics=_KNOWN)
    assert "tradingview" in str(exc.value)
    assert "corp_action" in str(exc.value)


def test_validation_names_a_source_whose_declared_mics_exclude_the_market() -> None:
    capabilities = {
        "us_only": _cap("us_only", ("ohlcv",), mics=("XNAS", "XNYS")),
    }
    rows = [
        OrderRow(mic, channel, ("us_only",) if channel == "ohlcv" else ())
        for mic in _MICS
        for channel in ORDER_CHANNELS
    ]
    with pytest.raises(SourceOrderError) as exc:
        SourceOrder(rows).validate(capabilities=capabilities, known_mics=_KNOWN)
    assert "us_only" in str(exc.value)
    assert "XIST" in str(exc.value)


def test_validation_requires_every_exchange_to_have_all_three_channels() -> None:
    rows = [
        row
        for row in _full_rows(_MICS, ())
        if not (row.mic == "XNSE" and row.channel == "instrument")
    ]
    with pytest.raises(SourceOrderError) as exc:
        SourceOrder(rows).validate(capabilities=_good_capabilities(), known_mics=_KNOWN)
    assert "XNSE" in str(exc.value)
    assert "instrument" in str(exc.value)


def test_validation_reports_every_problem_at_once() -> None:
    rows = [
        row
        for row in _full_rows(_MICS, ())
        if not (row.mic == "XNSE" and row.channel == "instrument")
        and not (row.mic == "XIST" and row.channel == "ohlcv")
    ]
    rows.append(OrderRow("XIST", "ohlcv", ("ghost_source",)))
    with pytest.raises(SourceOrderError) as exc:
        SourceOrder(rows).validate(capabilities=_good_capabilities(), known_mics=_KNOWN)
    assert len(exc.value.problems) >= 2


# ---------------------------------------------------------------------------
# Registry introspection
# ---------------------------------------------------------------------------


def test_sources_are_classified_by_the_package_they_live_in() -> None:
    capabilities = registry_capabilities()
    assert capabilities["yahoo_chart"].kind is SourceKind.UNIVERSAL
    assert capabilities["tradingview"].kind is SourceKind.UNIVERSAL
    assert capabilities["stooq"].kind is SourceKind.NATIVE
    assert capabilities["alpaca"].kind is SourceKind.NATIVE
    for name in directory_source_names():
        assert capabilities[name].kind is SourceKind.DIRECTORY
    assert universal_source_names() >= {"yahoo_chart", "tradingview"}
    assert is_universal_source("stooq") is False
    assert is_universal_source("yahoo_chart") is True


def test_the_universal_alias_is_not_a_registered_provider() -> None:
    assert UNIVERSAL_ALIAS not in factory.list_providers()
    assert UNIVERSAL_ALIAS not in registry_capabilities()


def test_provider_class_returns_the_registered_class_and_refuses_an_unknown_name() -> None:
    from crocodile.equity.universal.yahoo_chart import YahooChartProvider

    assert factory.provider_class("yahoo_chart") is YahooChartProvider
    with pytest.raises(ValueError, match="Unknown provider"):
        factory.provider_class("no_such_provider")


def test_the_builder_is_deterministic() -> None:
    exchanges = ExchangeRegistry().all()
    first = build_default_rows(exchanges, registry_capabilities())
    second = build_default_rows(exchanges, registry_capabilities())
    assert first == second


def test_the_packaged_table_validates_against_the_live_registries() -> None:
    source_order.cache_clear()
    order = source_order()
    assert order.rows()


def test_every_exchange_has_an_ohlcv_instrument_and_corp_action_row() -> None:
    order = source_order()
    pairs = {(row.mic, row.channel) for row in order.rows()}
    expected = {
        (info.mic, channel) for info in ExchangeRegistry().all() for channel in ORDER_CHANNELS
    }
    assert pairs >= expected


def _chain(order: SourceOrder, mic: str, channel: str) -> tuple[str, ...]:
    return order.order_for(mic, channel)


def test_us_rows_put_existing_natives_before_universal_sources() -> None:
    order = source_order()
    capabilities = registry_capabilities()
    us_mics = [info.mic for info in ExchangeRegistry().all() if info.country == "US"]
    assert us_mics
    for mic in us_mics:
        chain = _chain(order, mic, "ohlcv")
        natives = [name for name in chain if capabilities[name].kind is SourceKind.NATIVE]
        universals = [name for name in chain if capabilities[name].kind is SourceKind.UNIVERSAL]
        if natives and universals:
            last_native = max(chain.index(name) for name in natives)
            first_universal = min(chain.index(name) for name in universals)
            assert last_native < first_universal, (mic, chain)
        native_order = [name for name in US_NATIVE_ORDER["ohlcv"] if name in natives]
        assert natives == native_order, (mic, chain)
    xnas = _chain(order, "XNAS", "ohlcv")
    assert xnas[0] == "alpaca", xnas


def test_non_us_ohlcv_chains_name_no_us_native_and_put_yahoo_chart_first() -> None:
    order = source_order()
    capabilities = registry_capabilities()
    others = sorted(
        name
        for name, cap in capabilities.items()
        if cap.kind is SourceKind.UNIVERSAL and name not in UNIVERSAL_ORDER["ohlcv"]
    )
    allowed = ("yahoo_chart", "tradingview", *others)
    for info in ExchangeRegistry().all():
        if info.country == "US":
            continue
        chain = _chain(order, info.mic, "ohlcv")
        assert set(chain).isdisjoint(US_NATIVE_ORDER["ohlcv"]), (info.mic, chain)
        position = 0
        for name in chain:
            found = allowed.index(name, position) if name in allowed[position:] else -1
            assert found >= 0, (info.mic, chain)
            position = found + 1


def test_instrument_chains_put_directories_before_tradingview_before_yahoo_chart() -> None:
    order = source_order()
    directories = directory_source_names()
    for info in ExchangeRegistry().all():
        chain = _chain(order, info.mic, "instrument")
        assert "yahoo_chart" not in chain, (info.mic, chain)
        dirs = [name for name in chain if name in directories]
        if "tradingview" in chain and dirs:
            assert max(chain.index(name) for name in dirs) < chain.index("tradingview")
    nse = _chain(order, "XNSE", "instrument")
    capabilities = registry_capabilities()
    owners = [
        name
        for name in directories
        if capabilities[name].mics is not None and "XNSE" in capabilities[name].mics
    ]
    assert owners, "no directory source declares XNSE"
    assert nse[0] in owners, nse


def test_corp_action_chains_outside_the_us_name_only_universal_sources() -> None:
    order = source_order()
    capabilities = registry_capabilities()
    for info in ExchangeRegistry().all():
        if info.country == "US":
            continue
        for name in _chain(order, info.mic, "corp_action"):
            assert capabilities[name].kind is SourceKind.UNIVERSAL, (info.mic, name)


def test_the_packaged_csv_names_no_alias_and_has_exactly_three_channels_per_mic() -> None:
    order = source_order()
    for row in order.rows():
        assert row.channel in ORDER_CHANNELS
        assert UNIVERSAL_ALIAS not in row.sources
    for mic in order.mics():
        channels = [row.channel for row in order.rows() if row.mic == mic]
        assert sorted(channels) == sorted(ORDER_CHANNELS), (mic, channels)


def test_the_capability_model_serves_only_declared_channels_and_markets() -> None:
    capability = _cap("x", ("ohlcv",), mics=("XIST",))
    assert capability.serves("XIST", "ohlcv") is True
    assert capability.serves("XIST", "instrument") is False
    assert capability.serves("XNSE", "ohlcv") is False
    undeclared = _cap("y", ("ohlcv",))
    assert undeclared.serves("XNSE", "ohlcv") is True


def test_the_exchange_info_type_is_the_registry_row() -> None:
    info = ExchangeRegistry().get("XIST")
    assert isinstance(info, ExchangeInfo)
    assert info.country == "TR"


def test_the_registry_helpers_return_frozensets() -> None:
    assert isinstance(universal_source_names(), frozenset)
    assert isinstance(directory_source_names(), frozenset)
