"""The live source registry: chain order, closed markets, the coverage table and class lookup."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from crocodile.core.markets import exchanges as exchanges_module
from crocodile.core.markets.errors import UnknownMarketError
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.sanctions import is_sanctioned
from crocodile.equity.universal.live import registry
from crocodile.equity.universal.live.base import LiveSource
from crocodile.equity.universal.live.model import LiveListing, LiveSnapshot, LiveTier
from crocodile.equity.universal.live.registry import (
    coverage,
    source_class,
    sources_for,
)
from crocodile.equity.universal.live.sources.boerse_frankfurt import BoerseFrankfurtLive
from crocodile.equity.universal.live.sources.six import SixLive
from crocodile.equity.universal.live.sources.sse_yunhq import SseYunhqLive
from crocodile.equity.universal.live.sources.tencent import TencentLive
from crocodile.equity.universal.live.sources.tradingview import TradingViewLive
from crocodile.equity.universal.live.sources.twse_mis import TwseMisLive
from crocodile.equity.universal.live.sources.yahoo_chart import YahooChartLive

CSV_PATH = Path(exchanges_module.__file__).resolve().parent / "data" / "exchanges.csv"


def _names(mic: str, **kwargs: Any) -> tuple[str, ...]:
    return tuple(cls.name for cls in sources_for(mic, **kwargs))


class _Stub(LiveSource):
    """A source that never answers: only its declared attributes matter to the chain order."""

    async def snapshots(
        self, listings: Sequence[LiveListing], *, captured_ns: int
    ) -> list[LiveSnapshot]:
        raise NotImplementedError


def _stub(
    name: str,
    *,
    tier: LiveTier,
    official: bool = False,
    delay_s: int | None = None,
    mics: frozenset[str] = frozenset({"XIST"}),
) -> type[LiveSource]:
    return type(
        f"Stub_{name}",
        (_Stub,),
        {
            "name": name,
            "supported_mics": mics,
            "max_tier": tier,
            "official": official,
            "delay_s": delay_s,
        },
    )


# Chains of real markets, deepest view first.


def test_taiwan_chain_asks_the_exchange_first() -> None:
    assert _names("XTAI") == ("twse_mis", "tradingview", "yahoo_chart")
    assert sources_for("XTAI")[0] is TwseMisLive


def test_shanghai_chain_asks_the_exchange_before_tencent() -> None:
    assert _names("XSHG") == ("sse_yunhq", "tencent", "yahoo_chart")
    assert SseYunhqLive.tier_for("XSHG") is LiveTier.L2
    assert TencentLive.tier_for("XSHG") is LiveTier.L2


def test_hong_kong_chain_has_tencent_at_last_tier_by_override() -> None:
    assert _names("XHKG") == ("tencent", "tradingview", "yahoo_chart")
    # tier_overrides pins Tencent to LAST on XHKG, while the same source is L2 on XSHG.
    assert TencentLive.tier_for("XHKG") is LiveTier.LAST
    assert TencentLive.depth_for("XHKG") == 0
    assert sources_for("XHKG")[0].tier_for("XHKG") is LiveTier.LAST


def test_xetra_is_l2_and_frankfurt_floor_is_l1() -> None:
    assert _names("XETR") == ("boerse_frankfurt", "tradingview", "yahoo_chart")
    assert BoerseFrankfurtLive.tier_for("XETR") is LiveTier.L2
    assert BoerseFrankfurtLive.depth_for("XETR") == 10
    assert _names("XFRA") == ("boerse_frankfurt", "tradingview", "yahoo_chart")
    assert BoerseFrankfurtLive.tier_for("XFRA") is LiveTier.L1
    assert BoerseFrankfurtLive.depth_for("XFRA") == 1


def test_swiss_chain_is_the_delayed_official_top_of_book() -> None:
    assert _names("XSWX") == ("six", "tradingview", "yahoo_chart")
    assert SixLive.tier_for("XSWX") is LiveTier.L1
    assert SixLive.delay_s == 900


def test_istanbul_chain_has_only_the_aggregators() -> None:
    assert _names("XIST") == ("tradingview", "yahoo_chart")
    assert sources_for("XIST")[0] is TradingViewLive


# Sanctioned markets are default-closed.


def test_sanctioned_markets_have_an_empty_chain() -> None:
    assert sources_for("MISX") == ()
    assert sources_for("XTEH") == ()
    assert sources_for("MISX", allow={"IR"}) == ()


def test_opening_a_sanctioned_market_by_its_country_gives_a_chain() -> None:
    assert _names("MISX", allow={"RU"}) == ("yahoo_chart",)


def test_an_unknown_mic_raises() -> None:
    with pytest.raises(UnknownMarketError):
        sources_for("ZZZZ")


# Chain ordering rules, on stub classes so that each precedence is isolated.


def test_order_is_tier_then_publisher_then_delay_then_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classes = (
        _stub("l1_agg_delayed", tier=LiveTier.L1, delay_s=60),
        _stub("l1_off_delayed", tier=LiveTier.L1, official=True, delay_s=900),
        _stub("l1_agg_none", tier=LiveTier.L1),
        _stub("t_l2_agg", tier=LiveTier.L2),
        _stub("l1_off_rt", tier=LiveTier.L1, official=True, delay_s=0),
        _stub("l1_off_rt2", tier=LiveTier.L1, official=True),
        _stub("last_official", tier=LiveTier.LAST, official=True),
    )
    monkeypatch.setattr(registry, "_all_classes", lambda: classes)
    assert _names("XIST") == (
        "t_l2_agg",  # tier L2 beats every L1 source, aggregator or not
        "l1_off_rt",  # official first; among officials, real-time (delay 0) before delayed
        "l1_off_rt2",  # same publisher and delay: registration order
        "l1_off_delayed",
        "l1_agg_none",  # then aggregators; undeclared delay counts as real-time
        "l1_agg_delayed",
        "last_official",  # a LAST source is last even when it is official
    )


def test_chain_keeps_only_sources_declaring_the_market(monkeypatch: pytest.MonkeyPatch) -> None:
    classes = (
        _stub("elsewhere", tier=LiveTier.L2, mics=frozenset({"XHKG"})),
        _stub("here", tier=LiveTier.LAST),
    )
    monkeypatch.setattr(registry, "_all_classes", lambda: classes)
    assert _names("XIST") == ("here",)


# The coverage table.


def test_csv_has_one_coverage_row_per_exchange() -> None:
    with CSV_PATH.open(encoding="utf-8", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    rows = coverage()
    assert len(csv_rows) == 172
    assert len(rows) == len(csv_rows) == len(ExchangeRegistry().all())
    assert [row.mic for row in rows] == sorted(row.mic for row in rows)
    assert {row.mic for row in rows} == {row["mic"] for row in csv_rows}


def test_taiwan_coverage_row_is_the_exchange_fixture() -> None:
    row = next(item for item in coverage() if item.mic == "XTAI")
    assert row.name == "TAIWAN STOCK EXCHANGE"
    assert row.country == "TW"
    assert row.closed is False
    assert row.tier is LiveTier.L2
    assert row.depth_levels == 5
    assert row.has_tape is True
    assert row.official is True
    assert row.enumerates is True
    assert row.sources == ("twse_mis", "tradingview", "yahoo_chart")


def test_hong_kong_coverage_row_declares_last_tier() -> None:
    row = next(item for item in coverage() if item.mic == "XHKG")
    assert row.tier is LiveTier.LAST
    assert row.depth_levels == 0
    assert row.official is False
    assert row.sources == ("tencent", "tradingview", "yahoo_chart")


def test_sanctioned_market_is_marked_closed() -> None:
    row = next(item for item in coverage() if item.mic == "MISX")
    assert row.closed is True
    assert row.tier is None
    assert row.depth_levels == 0
    assert row.has_tape is False
    assert row.official is False
    assert row.enumerates is False
    assert row.sources == ()


def test_allowing_a_sanctioned_country_opens_its_row() -> None:
    row = next(item for item in coverage(allow={"RU"}) if item.mic == "MISX")
    assert row.closed is False
    assert row.sources == ("yahoo_chart",)
    assert row.tier is LiveTier.LAST


def test_every_row_agrees_with_its_chain() -> None:
    for row in coverage():
        chain = sources_for(row.mic)
        assert row.sources == tuple(cls.name for cls in chain)
        assert row.closed is is_sanctioned(row.mic)
        if not chain:
            # No source declares the market: a missing tier is None and a missing depth is 0.
            assert row.tier is None and row.depth_levels == 0 and row.official is False
            continue
        assert row.closed is False
        first = chain[0]
        assert row.tier is first.tier_for(row.mic)
        assert row.depth_levels == first.depth_for(row.mic)
        assert row.official is first.official


# Registry integrity and lookup.


def test_registered_source_names_are_unique() -> None:
    names = [cls.name for cls in registry.SOURCE_CLASSES]
    assert len(names) == len(set(names))
    assert "tradingview" in names and "yahoo_chart" in names


def test_duplicate_names_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_DEDICATED", (TwseMisLive, TwseMisLive))
    with pytest.raises(ValueError, match="not unique"):
        registry._all_classes.__wrapped__()  # type: ignore[attr-defined]


def test_registration_order_puts_dedicated_sources_first_and_floor_last() -> None:
    classes = registry.SOURCE_CLASSES
    assert classes[: len(registry._DEDICATED)] == registry._DEDICATED
    assert classes[-2:] == (TradingViewLive, YahooChartLive)


def test_source_class_finds_a_registered_name() -> None:
    assert source_class("twse_mis") is TwseMisLive
    assert source_class("tradingview") is TradingViewLive
    assert source_class("yahoo_chart") is YahooChartLive


def test_source_class_refuses_an_unknown_name() -> None:
    with pytest.raises(KeyError):
        source_class("no_such_source")
