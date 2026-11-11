"""Stage B of the canonical schema: unit, scale, adjustment and listing-identity fields.

The global-equities universe needs every price to say what unit it is stated in, whether the
source had already adjusted it for splits, and which listing an instrument row describes
(spec 3.7). A source that did not say leaves the field ``None``, which is unknown; it is never
read as USD or as scale 0. Every new field is a trailing ``T | None = None`` so a record built
the old way still constructs, and the enum members were appended so every value already on
disk keeps its meaning.
"""

from __future__ import annotations

import msgspec
import pytest

from crocodile.core.schema.enums import AssetClass, FundPeriod, PriceAdjustment, SecurityType
from crocodile.core.schema.records import OHLCV, CorporateAction, Instrument, Quote, Record, Trade

_STAGE_B: dict[type[msgspec.Struct], tuple[tuple[str, object], ...]] = {
    Trade: (("currency", str | None), ("price_scale", int | None)),
    Quote: (("currency", str | None), ("price_scale", int | None)),
    OHLCV: (
        ("currency", str | None),
        ("price_scale", int | None),
        ("adjustment", PriceAdjustment | None),
    ),
    CorporateAction: (("currency", str | None), ("price_scale", int | None)),
    Instrument: (
        ("mic", str | None),
        ("segment_mic", str | None),
        ("isin", str | None),
        ("country", str | None),
        ("currency", str | None),
        ("price_scale", int | None),
    ),
}


def test_price_adjustment_names_the_three_states_the_spec_allows() -> None:
    assert [(m.name, m.value) for m in PriceAdjustment] == [
        ("NONE", "none"),
        ("SPLIT", "split"),
        ("SPLIT_DIVIDEND", "split_dividend"),
    ]


def test_security_type_gains_the_global_instrument_kinds_and_keeps_every_old_value() -> None:
    assert [m.value for m in SecurityType] == [
        "CS",
        "ETF",
        "ADR",
        "REIT",
        "PFD",
        "WARRANT",
        "UNIT",
        "RIGHT",
        "ETN",
        "GDR",
        "FUND",
        "BOND",
        "CERTIFICATE",
        "STRUCTURED",
        "INDEX",
        "unknown",
    ]


def test_fund_period_gains_the_half_years_and_keeps_every_old_value() -> None:
    assert [m.value for m in FundPeriod] == ["Q1", "Q2", "Q3", "Q4", "H1", "H2", "FY", "TTM"]


@pytest.mark.parametrize("cls", list(_STAGE_B), ids=lambda c: c.__name__)
def test_stage_b_fields_trail_their_record_optional_and_unset(cls: type[msgspec.Struct]) -> None:
    expected = _STAGE_B[cls]
    tail = msgspec.structs.fields(cls)[-len(expected) :]
    assert [(f.name, f.type) for f in tail] == list(expected)
    for f in tail:
        assert not f.required, f.name
        assert f.default is None, f.name


def test_a_record_built_the_old_way_reads_every_stage_b_field_as_unknown() -> None:
    """A 0.3.x caller passes none of the new keywords; the fields must read as unknown."""
    bar = OHLCV(
        source="yahoo_chart",
        symbol="THYAO@XIST",
        symbol_raw="THYAO.IS",
        local_ts=1_793_577_600_000_000_000,
        asset_class=AssetClass.EQUITY,
        source_ts=None,
        interval="1d",
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=10.0,
    )
    listing = Instrument(
        source="tiingo",
        symbol="AAPL",
        symbol_raw="AAPL",
        local_ts=1_793_577_600_000_000_000,
        asset_class=AssetClass.EQUITY,
        source_ts=None,
    )
    for record in (bar, listing):
        for name, _ in _STAGE_B[type(record)]:
            assert getattr(record, name) is None, name


def test_stage_b_fields_survive_a_msgspec_round_trip() -> None:
    bar = OHLCV(
        source="yahoo_chart",
        symbol="VOD@XLON",
        symbol_raw="VOD.L",
        local_ts=1_793_577_600_000_000_000,
        asset_class=AssetClass.EQUITY,
        source_ts=None,
        interval="1d",
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=10.0,
        currency="GBP",
        price_scale=2,
        adjustment=PriceAdjustment.SPLIT,
    )
    listing = Instrument(
        source="tiingo",
        symbol="AAPL",
        symbol_raw="AAPL",
        local_ts=1_793_577_600_000_000_000,
        asset_class=AssetClass.EQUITY,
        source_ts=None,
        mic="XNAS",
        segment_mic="XNGS",
        isin="US0378331005",
        country="US",
        currency="USD",
        price_scale=0,
    )
    for record in (bar, listing):
        decoded = msgspec.json.decode(msgspec.json.encode(record), type=Record)
        assert decoded == record
