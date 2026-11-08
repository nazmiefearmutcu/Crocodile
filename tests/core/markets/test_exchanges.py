"""ExchangeRegistry behaviour against the packaged exchange allow-list."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import time
from zoneinfo import ZoneInfo

import pytest

from crocodile.core.markets import exchanges
from crocodile.core.markets.errors import (
    ReferenceDataError,
    UnknownCurrencyError,
    UnknownMarketError,
)
from crocodile.core.markets.exchanges import (
    ExchangeInfo,
    ExchangeRegistry,
    format_weekend,
    parse_exchange_row,
    parse_weekend,
)

pytestmark = pytest.mark.usefixtures("network_forbidden")


def _valid_row() -> dict[str, str]:
    return {
        "mic": "XIST",
        "name": "BORSA ISTANBUL",
        "country": "TR",
        "timezone": "Europe/Istanbul",
        "currency": "TRY",
        "quote_scale": "0",
        "weekend": "SAT,SUN",
        "close_local": "18:00",
        "xcal_code": "XIST",
        "tier_research": "B",
        "sanctioned": "false",
        "notes": "weekend:library; close_local:library; xcal:matrix",
    }


def test_get_returns_typed_fields() -> None:
    row = ExchangeRegistry().get("XIST")
    assert row.mic == "XIST"
    assert row.tz == ZoneInfo("Europe/Istanbul")
    assert row.currency == "TRY"
    assert row.quote_scale == 0
    assert row.weekend == frozenset({5, 6})
    assert row.close_local == time(18, 0)
    assert row.xcal_code == "XIST"
    assert row.tier_research == "B"
    assert row.sanctioned is False


def test_minor_unit_markets_carry_quote_scale_2() -> None:
    registry = ExchangeRegistry()
    for mic in ("XLON", "XJSE", "XTAE"):
        assert registry.get(mic).quote_scale == 2, mic
    assert registry.get("XIST").quote_scale == 0


def test_by_country_returns_every_row_for_that_country_sorted() -> None:
    registry = ExchangeRegistry()
    de = registry.by_country("DE")
    assert [row.mic for row in de] == [
        "TGAT",
        "XBER",
        "XDUS",
        "XETR",
        "XFRA",
        "XHAM",
        "XHAN",
        "XMUN",
        "XSTU",
    ]
    assert [row.mic for row in registry.by_country("US")] == ["IEXG", "XCBO", "XNAS", "XNYS"]
    assert [row.mic for row in registry.by_country("TR")] == ["XIST"]
    assert registry.by_country("SK") == ()


@pytest.mark.parametrize("bad", ["tr", "TUR", ""])
def test_by_country_rejects_malformed_codes(bad: str) -> None:
    with pytest.raises(ValueError):
        ExchangeRegistry().by_country(bad)


def test_all_returns_172_rows_sorted_by_mic() -> None:
    rows = ExchangeRegistry().all()
    assert len(rows) == 172
    mics = [row.mic for row in rows]
    assert mics == sorted(mics)
    assert len(ExchangeRegistry()) == 172


def test_segment_mics_are_refused_with_a_pointer_to_the_operating_mic() -> None:
    with pytest.raises(UnknownMarketError) as caught:
        ExchangeRegistry().get("XTKS")
    assert "XJPX" in str(caught.value)


def test_resolve_accepts_segment_mics() -> None:
    assert ExchangeRegistry().resolve("XTKS").mic == "XJPX"


def test_active_operating_mics_outside_the_allow_list_are_unknown() -> None:
    with pytest.raises(UnknownMarketError) as caught:
        ExchangeRegistry().get("DRSP")
    assert "allow-list" in str(caught.value)


def test_bulgaria_trades_in_euro() -> None:
    from crocodile.core.markets.currency import require_currency

    assert ExchangeRegistry().get("XBUL").currency == "EUR"
    with pytest.raises(UnknownCurrencyError):
        require_currency("BGN")


def test_weekend_helpers_round_trip() -> None:
    assert parse_weekend("FRI,SAT") == frozenset({4, 5})
    assert format_weekend({5, 4}) == "FRI,SAT"
    assert format_weekend(parse_weekend("SAT,SUN")) == "SAT,SUN"
    for bad in ("", "FRI,FRI", "FOO", "MON,TUE,WED,THU,FRI,SAT,SUN"):
        with pytest.raises(ValueError):
            parse_weekend(bad)
    with pytest.raises(ValueError):
        format_weekend(set())
    with pytest.raises(ValueError):
        format_weekend(range(7))


def test_isweekend_follows_the_weekend_set() -> None:
    import datetime as dt

    xist = ExchangeRegistry().get("XIST")
    assert xist.is_weekend(dt.date(2026, 11, 7)) is True  # Saturday
    assert xist.is_weekend(dt.date(2026, 11, 9)) is False  # Monday


def test_malformed_rows_raise_reference_data_error() -> None:
    bad_scale = {**_valid_row(), "quote_scale": "3"}
    bad_zone = {**_valid_row(), "timezone": "Mars/Olympus"}
    bad_currency = {**_valid_row(), "currency": "try"}
    for row in (bad_scale, bad_zone, bad_currency):
        with pytest.raises(ReferenceDataError) as caught:
            parse_exchange_row(row, line=7)
        assert str(caught.value)
        assert "line 7" in str(caught.value)
    parsed = parse_exchange_row(_valid_row(), line=2)
    assert isinstance(parsed, ExchangeInfo)


def test_the_module_exposes_the_spec_columns() -> None:
    assert exchanges.EXCHANGE_COLUMNS == (
        "mic",
        "name",
        "country",
        "timezone",
        "currency",
        "quote_scale",
        "weekend",
        "close_local",
        "xcal_code",
        "tier_research",
        "sanctioned",
        "notes",
    )
    assert exchanges.WEEKDAY_TOKENS == ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("sanctioned", "True"),
        ("close_local", "25:00"),
        ("close_local", "9:30"),
        ("tier_research", "E"),
        ("weekend", "SUN,SAT"),
        ("mic", "xist"),
        ("country", "tr"),
    ],
    ids=[
        "sanctioned-not-lower-case",
        "close-hour-out-of-range",
        "close-not-two-digit-hour",
        "tier-outside-a-to-d",
        "weekend-not-monday-first",
        "mic-lower-case",
        "country-lower-case",
    ],
)
def test_parse_exchange_row_refuses_each_bad_cell(column: str, value: str) -> None:
    with pytest.raises(ReferenceDataError) as caught:
        parse_exchange_row({**_valid_row(), column: value}, line=9)
    assert "line 9" in str(caught.value)


@pytest.fixture
def fresh_exchange_index() -> Iterator[None]:
    exchanges._packaged_exchange_index.cache_clear()
    yield
    exchanges._packaged_exchange_index.cache_clear()


def test_the_packaged_exchange_loader_refuses_a_duplicate_mic(
    fresh_exchange_index: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _valid_row()
    monkeypatch.setattr(exchanges, "read_csv", lambda name, header: [row, row])
    with pytest.raises(ReferenceDataError) as caught:
        exchanges._packaged_exchange_index()
    assert "duplicate mic XIST" in str(caught.value)
