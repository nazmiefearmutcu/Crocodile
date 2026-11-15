"""``sites``: the declarative market-board engine, read from trimmed captured answers.

The shipped specs run against fixtures under ``fixtures/live/sites``; every other behaviour is
pinned with a small inline spec so that the rule under test is the only thing that varies.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.equity.universal.base import SourceUnservableError, UnservableReason
from crocodile.equity.universal.http import HttpRequest, HttpResponse
from crocodile.equity.universal.live.base import LivePayloadError
from crocodile.equity.universal.live.model import LiveListing, LiveSnapshot, LiveTier
from crocodile.equity.universal.live.sites import (
    FIELDS,
    SiteLive,
    SiteSpec,
    load_specs,
    parse_number,
    parse_site,
    site_source_classes,
    spec_request,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live" / "sites"
CAPTURED_NS = 1_794_471_000_000_000_000
NS = 1_000_000_000
SHIPPED = {spec.id: spec for spec in load_specs()}
SHIPPED_CLASS: dict[str, type[SiteLive]] = {cls.spec.id: cls for cls in site_source_classes()}
NBSP = chr(0xA0)
NNBSP = chr(0x202F)
RSQUO = chr(0x2019)


def _body(relative: str) -> bytes:
    return (FIXTURES / relative).read_bytes()


def _shipped(spec_id: str) -> SiteSpec:
    return SHIPPED[spec_id]


def _spec(**overrides: object) -> SiteSpec:
    base: dict[str, object] = {
        "id": "probe",
        "mic": "XWAR",
        "url": "https://example.invalid/board",
        "format": "json",
        "fields": {"symbol": "code", "last": "px"},
    }
    base.update(overrides)
    return SiteSpec(**base)  # type: ignore[arg-type]


def _parse(spec: SiteSpec, body: bytes | str) -> list[LiveSnapshot]:
    raw = body.encode("utf-8") if isinstance(body, str) else body
    return parse_site(spec, raw, captured_ns=CAPTURED_NS)


def _levels(levels: tuple[object, ...]) -> list[tuple[float, float | None]]:
    return [(level.price, level.size) for level in levels]  # type: ignore[attr-defined]


def _response(
    status: int, body: bytes = b"", content_type: str = "application/json", url: str = ""
) -> HttpResponse:
    return HttpResponse(status=status, headers={"content-type": content_type}, body=body, url=url)


def _fake(
    respond: Callable[[HttpRequest], HttpResponse],
) -> tuple[Callable[[HttpRequest], object], list[HttpRequest]]:
    """A fetcher that records every request it receives and answers with ``respond``."""
    seen: list[HttpRequest] = []

    async def fetch(request: HttpRequest) -> HttpResponse:
        seen.append(request)
        return respond(request)

    return fetch, seen


def _utc_ns(year: int, month: int, day: int, utc_hour: int) -> int:
    """A capture time, in nanoseconds since the epoch, at a UTC wall-clock hour."""
    return int(datetime(year, month, day, utc_hour, tzinfo=UTC).timestamp()) * NS


# --- parse_number -----------------------------------------------------------------------


def test_plain_numbers_take_comma_as_thousands_and_dot_as_decimal() -> None:
    assert parse_number("1,234.50") == 1234.5
    assert parse_number("1,234") == 1234.0
    assert parse_number("5.75") == 5.75
    assert parse_number(".5") == 0.5


def test_currency_words_percent_and_plus_signs_are_ignored() -> None:
    assert parse_number("BWP 5.75") == 5.75
    assert parse_number("8,599 UZS") == 8599.0
    assert parse_number("%0.00") == 0.0
    assert parse_number("+0.05") == 0.05
    assert parse_number("-14,00%", "european") == -14.0
    assert parse_number("-0.5") == -0.5


def test_european_numbers_take_dot_and_spaces_as_thousands_and_comma_as_decimal() -> None:
    assert parse_number("1.234,50", "european") == 1234.5
    assert parse_number("1 234,50", "european") == 1234.5
    assert parse_number(f"1{NBSP}234,50", "european") == 1234.5
    assert parse_number(f"1{NNBSP}234", "european") == 1234.0
    assert parse_number("0,196", "european") == 0.196
    assert parse_number("1.234.567", "european") == 1234567.0
    assert parse_number("1'234.50") == 1234.5


def test_a_cell_with_two_numbers_is_not_a_number() -> None:
    assert parse_number("124.663 (0.237)") is None
    assert parse_number("12 34") is None
    assert parse_number("10 20", "european") is None
    assert parse_number("3,5 4,5", "european") is None
    assert parse_number("1.5 2.5", "european") is None
    assert parse_number("1 234 56") is None


def test_empty_and_dash_cells_are_missing_not_zero() -> None:
    assert parse_number("-") is None
    assert parse_number("") is None
    assert parse_number("   ") is None
    assert parse_number("n/a") is None
    assert parse_number(None) is None


def test_bool_and_non_finite_values_are_not_numbers() -> None:
    assert parse_number(True) is None
    assert parse_number(False) is None
    assert parse_number(float("nan")) is None
    assert parse_number(float("inf")) is None
    assert parse_number(["1"]) is None


def test_python_numbers_pass_through() -> None:
    assert parse_number(3) == 3.0
    assert parse_number(2.5) == 2.5
    assert parse_number(0) == 0.0


# --- formats ----------------------------------------------------------------------------


def test_json_row_paths_read_every_listed_array() -> None:
    body = (
        b'{"data": {"main": [{"code": "AAA", "px": "1,000"}],'
        b' "grow": [{"code": "BBB", "px": "2"}]}}'
    )
    spec = _spec(rows=["data.main", "data.grow"])
    assert [s.listing.local_symbol for s in _parse(spec, body)] == ["AAA", "BBB"]


def test_json_object_grouped_by_segment_contributes_every_group() -> None:
    body = (
        b'{"segments": {"main": [{"code": "AAA", "px": 10}],'
        b' "growth": [{"code": "BBB", "px": 3}, {"code": "CCC", "px": 4}]}}'
    )
    spec = _spec(rows=["segments"])
    assert [s.listing.local_symbol for s in _parse(spec, body)] == ["AAA", "BBB", "CCC"]


def test_json_root_array_and_bom_are_read() -> None:
    body = b"\xef\xbb\xbf" + b'[{"code": "AAA", "px": 10}]'
    (snapshot,) = _parse(_spec(), body)
    assert snapshot.listing.local_symbol == "AAA"
    assert snapshot.last == 10.0


def test_json_nested_field_addresses_use_dots() -> None:
    body = b'[{"code": "AAA", "price": {"amount": "12.5", "currency": "KHR"}}]'
    spec = _spec(fields={"symbol": "code", "last": "price.amount", "currency": "price.currency"})
    (snapshot,) = _parse(spec, body)
    assert snapshot.last == 12.5
    assert snapshot.currency == "KHR"


def test_csv_header_line_and_delimiter_find_the_header() -> None:
    body = b"Board export\nCode;Name;Last;Bid;Ask\nAAA;Alpha;10,5;10,4;10,6\n"
    spec = _spec(
        format="csv",
        delimiter=";",
        header_line=1,
        number="european",
        fields={"symbol": "Code", "name": "Name", "last": "Last", "bid": "Bid", "ask": "Ask"},
    )
    (snapshot,) = _parse(spec, body)
    assert snapshot.listing.local_symbol == "AAA"
    assert snapshot.name == "Alpha"
    assert snapshot.last == 10.5
    assert _levels(snapshot.bids) == [(10.4, None)]
    assert _levels(snapshot.asks) == [(10.6, None)]


def test_csv_short_rows_leave_the_missing_cells_none() -> None:
    body = b"Code,Last,Volume\nAAA,7\nBBB,8,9\n"
    spec = _spec(format="csv", fields={"symbol": "Code", "last": "Last", "volume": "Volume"})
    first, second = _parse(spec, body)
    assert (first.last, first.volume) == (7.0, None)
    assert (second.last, second.volume) == (8.0, 9.0)


def test_xml_attributes_and_child_elements_both_make_fields() -> None:
    body = (
        b"<board>"
        b'<quote code="AAA" last="10.5" bid="10.4" ask="10.6"/>'
        b'<quote code="BBB"><last>3</last><bid>2.9</bid></quote>'
        b"</board>"
    )
    spec = _spec(
        format="xml",
        rows=["quote"],
        fields={"symbol": "code", "last": "last", "bid": "bid", "ask": "ask"},
    )
    first, second = _parse(spec, body)
    assert (first.listing.local_symbol, first.last) == ("AAA", 10.5)
    assert _levels(first.bids) == [(10.4, None)]
    assert _levels(first.asks) == [(10.6, None)]
    assert (second.listing.local_symbol, second.last) == ("BBB", 3.0)
    assert _levels(second.bids) == [(2.9, None)]
    assert second.asks == ()


def test_xml_without_a_row_element_name_is_refused() -> None:
    with pytest.raises(LivePayloadError):
        _parse(_spec(format="xml", fields={"symbol": "code"}), b"<board/>")
    with pytest.raises(LivePayloadError):
        _parse(_spec(format="xml", rows=["quote"], fields={"symbol": "code"}), b"<board")


def test_html_header_text_addresses_the_columns() -> None:
    body = (
        b"<html><body><table>"
        b"<tr><th>Code<th>Last<th>Volume</tr>"
        b"<tr><td><a href=x>AAA</a><td>1,250.5<td>12</tr>"
        b"</table></body></html>"
    )
    spec = _spec(format="html", fields={"symbol": "Code", "last": "Last", "volume": "Volume"})
    (snapshot,) = _parse(spec, body)
    assert snapshot.listing.local_symbol == "AAA"
    assert (snapshot.last, snapshot.volume) == (1250.5, 12.0)


def test_html_table_is_chosen_by_its_header_not_its_position() -> None:
    body = (
        b'<table id="nav"><tr><td>Home</td><td>News</td></tr></table>'
        b'<table id="board"><tr><td>Quotes</td></tr>'
        b"<tr><th>Code</th><th>Last</th></tr>"
        b"<tr><td>AAA</td><td>1,250.5</td></tr></table>"
    )
    spec = _spec(format="html", fields={"symbol": "Code", "last": "Last"})
    (snapshot,) = _parse(spec, body)
    assert (snapshot.listing.local_symbol, snapshot.last) == ("AAA", 1250.5)
    pinned = _spec(
        format="html", fields={"symbol": "Code", "last": "Last"}, table_id="board", header_row=1
    )
    assert _parse(pinned, body)[0].last == 1250.5


def test_html_positional_addresses_read_cells_without_a_header() -> None:
    body = (
        b"<table><tr><td>AAA</td><td>100</td><td>12.5</td></tr>"
        b"<tr><td>BBB</td><td>5</td><td>0</td></tr></table>"
    )
    spec = _spec(format="html", fields={"symbol": "#0", "volume": "#1", "last": "#2"})
    snapshots = _parse(spec, body)
    # A table with no header has no header row: its first row is data, not a header. BBB
    # reports a last price of zero, which is not a price, so that row is dropped.
    assert [s.listing.local_symbol for s in snapshots] == ["AAA"]
    assert (snapshots[0].last, snapshots[0].volume) == (12.5, 100.0)


def test_html_omitted_end_tags_still_give_one_cell_per_column() -> None:
    body = _body("afx_luse/afx_luse.html")
    first, second, *_rest = _parse(_shipped("afx_luse"), body)
    assert first.listing.local_symbol == "AECI"
    assert first.volume is None
    assert second.listing.local_symbol == "ATEL"
    assert second.volume == 1257.0


# --- symbol_pattern and field_patterns --------------------------------------------------


def test_symbol_pattern_keeps_only_matching_symbols_and_their_group() -> None:
    body = b'[{"code": "ABC.BH", "px": 10}, {"code": "XYZ", "px": 5}, {"code": "DEF.BH", "px": 0}]'
    spec = _spec(symbol_pattern=r"^([A-Z0-9]+)\.BH$")
    (snapshot,) = _parse(spec, body)
    assert snapshot.listing.local_symbol == "ABC"
    assert snapshot.symbol_raw == "ABC.BH"


def test_field_pattern_reads_the_first_figure_of_a_cell() -> None:
    snapshots = _parse(_shipped("csx_cayman"), _body("csx_cayman/TickerDataMF.json"))
    first, second, third = snapshots[:3]
    assert (first.listing.local_symbol, first.last) == ("AASALAU", 124.663)
    assert (second.listing.local_symbol, second.last) == ("AASALNU", 109.719)
    assert (third.listing.local_symbol, third.last) == ("ALBRICB", 2458.45)
    assert len(snapshots) == 42


# --- row_filter -------------------------------------------------------------------------


def test_row_filter_keeps_only_rows_holding_an_allowed_value() -> None:
    body = (
        b'[{"code": "IDX", "px": 1, "kind": "20"},'
        b' {"code": "AAA", "px": 2, "kind": "11"},'
        b' {"code": "BBB", "px": 3, "kind": "21"},'
        b' {"code": "CCC", "px": 4, "kind": "30"}]'
    )
    spec = _spec(row_filter={"kind": ["11", "21"]}, fields={"symbol": "code", "last": "px"})
    assert [s.listing.local_symbol for s in _parse(spec, body)] == ["AAA", "BBB"]


# --- the module's rulings ---------------------------------------------------------------


def test_a_row_without_a_valid_local_symbol_is_dropped() -> None:
    body = (
        b'[{"code": "", "px": 1}, {"code": "BAD SYM", "px": 2}, {"code": "A@B", "px": 3},'
        b' {"code": null, "px": 4}, {"code": 42, "px": 5}]'
    )
    snapshots = _parse(_spec(), body)
    assert [(s.listing.local_symbol, s.last) for s in snapshots] == [("42", 5.0)]


def test_a_row_without_any_price_is_dropped() -> None:
    body = b'[{"code": "AAA", "px": "-"}, {"code": "BBB", "px": "0"}, {"code": "CCC", "px": 9}]'
    assert [s.listing.local_symbol for s in _parse(_spec(), body)] == ["CCC"]
    aix = _parse(_shipped("aix"), _body("aix/ep_mw_main_records.json"))
    assert "MFKZ.1128" not in [s.listing.local_symbol for s in aix]


def test_a_row_with_only_a_previous_close_is_kept() -> None:
    body = b'[{"code": "AAA", "px": 0, "prev": 24.23}]'
    spec = _spec(fields={"symbol": "code", "last": "px", "prev_close": "prev"})
    (snapshot,) = _parse(spec, body)
    assert snapshot.last is None
    assert snapshot.prev_close == 24.23


def test_the_first_row_of_a_symbol_wins() -> None:
    body = b'[{"code": "AAA", "px": 10}, {"code": "AAA", "px": 11}]'
    (snapshot,) = _parse(_spec(), body)
    assert snapshot.last == 10.0


def test_a_priced_row_after_an_unpriced_row_of_the_same_symbol_wins() -> None:
    body = b'[{"code": "AAA", "px": 0}, {"code": "AAA", "px": 11}]'
    (snapshot,) = _parse(_spec(), body)
    assert snapshot.last == 11.0


def test_a_crossed_top_of_book_keeps_neither_side_but_keeps_the_last_price() -> None:
    spec = _spec(fields={"symbol": "code", "last": "px", "bid": "b", "ask": "a"})
    (snapshot,) = _parse(spec, b'[{"code": "AAA", "px": 10.45, "b": 10.5, "a": 10.4}]')
    assert snapshot.bids == () and snapshot.asks == ()
    assert snapshot.last == 10.45
    (locked,) = _parse(spec, b'[{"code": "AAA", "px": 10.45, "b": 10.4, "a": 10.4}]')
    assert locked.bids == () and locked.asks == ()


def test_a_sound_top_of_book_keeps_both_sides_with_their_sizes() -> None:
    spec = _spec(
        fields={
            "symbol": "code",
            "last": "px",
            "bid": "b",
            "ask": "a",
            "bid_size": "bs",
            "ask_size": "as",
        }
    )
    (snapshot,) = _parse(
        spec, b'[{"code": "AAA", "px": 10.45, "b": 10.4, "a": 10.5, "bs": 7, "as": 0}]'
    )
    assert snapshot.tier is LiveTier.L1
    assert _levels(snapshot.bids) == [(10.4, 7.0)]
    assert _levels(snapshot.asks) == [(10.5, None)]


def test_price_size_and_turnover_multipliers_scale_only_their_fields() -> None:
    spec = _spec(
        price_mult=0.01,
        size_mult=1000.0,
        turnover_mult=1000.0,
        fields={"symbol": "code", "last": "px", "volume": "vol", "turnover": "tov"},
    )
    (snapshot,) = _parse(spec, b'[{"code": "AAA", "px": "250", "vol": "7", "tov": "1.15"}]')
    assert snapshot.last == pytest.approx(2.5)
    assert snapshot.volume == 7000.0
    assert snapshot.turnover == pytest.approx(1150.0)


def test_a_non_positive_price_is_missing_and_a_negative_size_is_missing() -> None:
    spec = _spec(fields={"symbol": "code", "last": "px", "open": "op", "volume": "vol"})
    # A zero last price is missing, so the row survives only on its open price.
    (zero,) = _parse(spec, b'[{"code": "AAA", "px": "0", "op": "3", "vol": "-1"}]')
    assert zero.last is None
    assert zero.open == 3.0
    assert zero.volume is None
    (negative, zero_volume) = _parse(
        spec,
        b'[{"code": "BBB", "px": "-3", "op": "1", "vol": "0"},'
        b' {"code": "CCC", "px": "2", "vol": "0"}]',
    )
    assert negative.last is None
    assert negative.volume == 0.0
    assert zero_volume.volume == 0.0


def test_a_spec_currency_fills_only_when_the_board_maps_no_currency() -> None:
    spec = _spec(currency="EUR")
    (snapshot,) = _parse(spec, b'[{"code": "AAA", "px": 1}]')
    assert snapshot.currency == "EUR"
    mapped = _spec(currency="EUR", fields={"symbol": "code", "last": "px", "currency": "cur"})
    (blank,) = _parse(mapped, b'[{"code": "AAA", "px": 1, "cur": ""}]')
    assert blank.currency is None


def test_names_isins_and_publisher_facts_are_carried_and_stripped() -> None:
    spec = _spec(
        price_scale=100,
        delay_s=900,
        fields={"symbol": "code", "last": "px", "name": "nm", "isin": "isin"},
    )
    first, second = _parse(
        spec,
        b'[{"code": " AAA ", "px": 1, "nm": "  Alpha  ", "isin": "XS0000000001"},'
        b' {"code": "BBB", "px": 2, "nm": "   "}]',
    )
    assert first.symbol_raw == "AAA"
    assert (first.name, first.listing.isin) == ("Alpha", "XS0000000001")
    assert (first.price_scale, first.delay_s, first.source) == (100, 900, "site_probe")
    assert (second.name, second.listing.isin) == (None, None)


def test_a_missing_csv_column_is_refused_and_never_read_from_another() -> None:
    body = b"Code,Price\nAAA,10\n"
    with pytest.raises(LivePayloadError, match="Last"):
        _parse(_spec(format="csv", fields={"symbol": "Code", "last": "Last"}), body)


def test_a_repeated_csv_header_is_refused_rather_than_resolved_by_position() -> None:
    body = b"Code,Last,Last\nAAA,10,11\n"
    with pytest.raises(LivePayloadError, match="repeats"):
        _parse(_spec(format="csv", fields={"symbol": "Code", "last": "Last"}), body)


def test_a_csv_without_a_header_line_or_with_an_empty_body_is_refused() -> None:
    spec = _spec(format="csv", fields={"symbol": "Code", "last": "Last"})
    with pytest.raises(LivePayloadError):
        _parse(spec, b"")
    with pytest.raises(LivePayloadError):
        _parse(_spec(format="csv", header_line=3, fields={"symbol": "Code"}), b"Code\nAAA\n")


def test_an_html_table_without_the_addressed_columns_is_refused() -> None:
    body = b"<table><tr><th>Code<th>Price</tr><tr><td>AAA<td>10</tr></table>"
    with pytest.raises(LivePayloadError, match="addressed columns"):
        _parse(_spec(format="html", fields={"symbol": "Code", "last": "Last"}), body)


def test_a_json_row_path_that_does_not_exist_is_refused() -> None:
    with pytest.raises(LivePayloadError, match="row paths"):
        _parse(_spec(rows=["data.main"]), b'{"data": {"other": []}}')


def test_a_body_that_is_not_json_is_refused() -> None:
    with pytest.raises(LivePayloadError, match="not JSON"):
        _parse(_spec(), b"<html>wall</html>")


# --- the shipped specs ------------------------------------------------------------------


def test_every_shipped_spec_validates_and_its_ids_are_unique() -> None:
    specs = load_specs()
    assert len(specs) == 48
    ids = [spec.id for spec in specs]
    assert len(set(ids)) == len(ids)
    for spec in specs:
        assert spec.format in ("json", "csv", "xml", "html")
        assert "symbol" in spec.fields
        assert set(spec.fields) <= set(FIELDS)
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", spec.evidence), spec.id
        assert not (spec.json_body is not None and spec.form), spec.id
        assert spec.url.startswith("https://") or spec.url.startswith("http://"), spec.id


def test_every_shipped_spec_market_is_allow_listed() -> None:
    registry = ExchangeRegistry()
    for spec in load_specs():
        assert registry.get(spec.mic).mic == spec.mic, spec.id


def test_site_source_classes_gives_one_class_per_spec() -> None:
    classes = site_source_classes()
    assert len(classes) == len(load_specs())
    for cls in classes:
        assert cls.name == f"site_{cls.spec.id}"
        assert cls.supported_mics == frozenset({cls.spec.mic})
        has_book = "bid" in cls.spec.fields or "ask" in cls.spec.fields
        assert (cls.max_tier is LiveTier.L1) == has_book, cls.spec.id
        assert cls.depth_levels == (1 if has_book else 0)


def test_the_book_tier_follows_the_mapped_bid_and_ask() -> None:
    assert SHIPPED_CLASS["aix"].max_tier is LiveTier.L1
    assert SHIPPED_CLASS["cboe_europe_cxe"].max_tier is LiveTier.L1
    assert SHIPPED_CLASS["afx_luse"].max_tier is LiveTier.LAST
    assert SHIPPED_CLASS["dfm"].max_tier is LiveTier.L1


@pytest.mark.parametrize(
    ("spec_id", "relative", "expected"),
    [
        (
            "aix",
            "aix/ep_mw_main_records.json",
            [("KAP", "KZ1C00001619"), ("KAP.Y", "US63253R2013")],
        ),
        (
            "cboe_europe_cxe",
            "cboe_europe_cxe/cdnapi_cxe_symbol_data.csv",
            [("RRl", None), ("ZURNz", None), ("GLENl", None), ("HSBAl", None), ("AZNl", None)],
        ),
        (
            "afx_luse",
            "afx_luse/afx_luse.html",
            [("AECI", None), ("ATEL", None), ("BATA", None), ("BATZ", None), ("CCAF", None)],
        ),
        (
            "brvm",
            "brvm/fr_cours-actions_0.html",
            [("ABJC", None), ("BBGC", None), ("BICB", None), ("BICC", None), ("BNBC", None)],
        ),
        (
            "gpw",
            "gpw/quotations.html",
            [
                ("06N", "PLNFI0600010"),
                ("11B", "PL11BTS00015"),
                ("3RG", "PLGRNKT00019"),
                ("4MS", "PL4MASS00011"),
                ("ABE", "PLAB00000019"),
            ],
        ),
        (
            "dfm",
            "dfm/stocks.json",
            [
                ("ABTC", None),
                ("AGRBK0827USD", None),
                ("AIRARABIA", None),
                ("AIRARBF27", None),
                ("AIRARBX26", None),
            ],
        ),
        (
            "qse",
            "qse/MarketWatch.json",
            [("QNBK", None), ("QIBK", None), ("CBQK", None), ("DHBK", None), ("ABQK", None)],
        ),
        (
            "zse_zagreb",
            "zse_zagreb/price-list-2026-11-11.json",
            [
                ("ADPL", "HRADPLRA0006"),
                ("ARNT", "HRARNTRA0004"),
                ("ATGR", "HRATGRRA0003"),
                ("HT", "HRHT00RA0005"),
                ("PODR", "HRPODRRA0004"),
            ],
        ),
        (
            "bse_botswana",
            "bse_botswana/market-overview-domestic.json",
            [
                ("KNO", None),
                ("BTCL", None),
                ("LETSHEGO", None),
                ("FNBB", None),
                ("STANCHART", None),
            ],
        ),
        (
            "csx_cambodia",
            "csx_cambodia/main-and-growth-board-stocks-trades.json",
            [
                ("PWSA", "KH1000010004"),
                ("GTI", "KH1000020003"),
                ("PPAP", "KH1000040001"),
                ("DBDE", "KH1000150008"),
                ("JSL", "KH1000160007"),
            ],
        ),
        (
            "amman",
            "amman/ticker_feeds.json",
            [("ABUS", None), ("AEIV", None), ("AHLI", None), ("AIHO", None)],
        ),
        (
            "blberza",
            "blberza/defaultTicker.json",
            [
                ("BOSK-R-A", None),
                ("DUVN-R-A", None),
                ("EKBL-R-A", None),
                ("DEST-R-A", None),
                ("HEDR-R-A", None),
            ],
        ),
    ],
)
def test_shipped_spec_parses_its_captured_board(
    spec_id: str, relative: str, expected: list[tuple[str, str | None]]
) -> None:
    snapshots = _parse(_shipped(spec_id), _body(relative))
    got = [(s.listing.local_symbol, s.listing.isin) for s in snapshots[: len(expected)]]
    assert got == expected
    assert all(s.source == f"site_{spec_id}" for s in snapshots)


def test_aix_first_row_is_concrete() -> None:
    kap = _parse(_shipped("aix"), _body("aix/ep_mw_main_records.json"))[0]
    assert kap.listing == LiveListing(mic="AIXK", local_symbol="KAP", isin="KZ1C00001619")
    assert (kap.name, kap.currency) == ("KAP", "KZT")
    assert kap.last == 27810.0 and kap.last_size is None
    assert (kap.open, kap.high, kap.low, kap.prev_close) == (28000.0, 28000.0, 27810.0, 28000.0)
    assert (kap.volume, kap.turnover) == (90.0, 2513780.0)
    assert _levels(kap.bids) == [(27505.0, 30.0)]
    assert _levels(kap.asks) == [(27670.0, 400.0)]
    assert kap.tier is LiveTier.L1
    assert kap.symbol_raw == "KAP"


def test_aix_second_row_has_no_last_and_a_usd_currency() -> None:
    kap_y = _parse(_shipped("aix"), _body("aix/ep_mw_main_records.json"))[1]
    assert kap_y.listing.local_symbol == "KAP.Y"
    assert kap_y.last is None and kap_y.volume is None
    assert _levels(kap_y.bids) == [(62.0, 23.0)]
    assert _levels(kap_y.asks) == [(62.43, 400.0)]
    assert (kap_y.prev_close, kap_y.currency) == (61.82, "USD")


def test_cboe_first_row_is_concrete() -> None:
    body = _body("cboe_europe_cxe/cdnapi_cxe_symbol_data.csv")
    rr = _parse(_shipped("cboe_europe_cxe"), body)[0]
    assert rr.listing.local_symbol == "RRl"
    assert rr.name == "Rolls-Royce Holdings PLC"
    assert rr.last == 1377.4
    assert _levels(rr.bids) == [(1377.2, 216.0)]
    assert _levels(rr.asks) == [(1377.8, 408.0)]
    assert rr.volume == 337249.0
    assert rr.turnover == pytest.approx(5574695.620560166, rel=1e-12)
    assert rr.currency is None


def test_afx_luse_first_row_is_concrete() -> None:
    aeci, atel, *_rest = _parse(_shipped("afx_luse"), _body("afx_luse/afx_luse.html"))
    assert (aeci.name, aeci.last, aeci.volume, aeci.currency) == (
        "AECI Mining Explosives",
        115.0,
        None,
        "ZMW",
    )
    assert (atel.last, atel.volume) == (222.0, 1257.0)


def test_brvm_first_row_reads_french_european_numbers() -> None:
    abjc, bbgc, bicb, *_rest = _parse(_shipped("brvm"), _body("brvm/fr_cours-actions_0.html"))
    assert abjc.name == "SERVAIR ABIDJAN COTE D'IVOIRE"
    assert (abjc.prev_close, abjc.open, abjc.last, abjc.volume) == (3300.0, 3400.0, 3300.0, 1637.0)
    assert abjc.currency == "XOF"
    expected_name = "BANQUE INTERNATIONALE POUR L" + RSQUO + "INDUSTRIE ET LE COMMERCE DU BENIN"
    assert bicb.name == expected_name
    assert (bicb.prev_close, bicb.open, bicb.last, bicb.volume) == (
        8100.0,
        8000.0,
        8090.0,
        15537.0,
    )
    assert bbgc.last == 9465.0


def test_gpw_positional_row_is_concrete_and_turnover_is_in_thousands() -> None:
    first, *_rest = _parse(_shipped("gpw"), _body("gpw/quotations.html"))
    assert first.name == "06MAGNA"
    assert first.listing.isin == "PLNFI0600010"
    assert first.currency == "PLN"
    assert (first.prev_close, first.open, first.low, first.high) == (2.24, 2.18, 2.18, 2.2)
    assert first.last == 2.2
    assert first.last_size == 16.0
    assert _levels(first.bids) == [(2.17, 298.0)]
    assert _levels(first.asks) == [(2.2, 2871.0)]
    assert first.volume == 528.0
    assert first.turnover == pytest.approx(1150.0)


def test_dfm_rows_keep_previous_close_only_rows_and_drop_empty_ones() -> None:
    snapshots = _parse(_shipped("dfm"), _body("dfm/stocks.json"))
    assert [s.listing.local_symbol for s in snapshots] == [
        "ABTC",
        "AGRBK0827USD",
        "AIRARABIA",
        "AIRARBF27",
        "AIRARBX26",
    ]
    traded = snapshots[2]
    assert (traded.last, traded.last_size) == (5.1, 486.0)
    assert _levels(traded.bids) == [(5.09, 114657.0)]
    assert _levels(traded.asks) == [(5.1, 201608.0)]
    assert (traded.open, traded.high, traded.low, traded.prev_close) == (5.1, 5.1, 5.03, 5.1)
    assert (traded.volume, traded.turnover) == (519633.0, 2639817.9)
    assert snapshots[0].last is None and snapshots[0].prev_close == 24.23


def test_qse_first_row_is_concrete_and_zero_prices_are_missing() -> None:
    qnbk, qibk, *_rest = _parse(_shipped("qse"), _body("qse/MarketWatch.json"))
    assert (qnbk.name, qnbk.last, qnbk.prev_close, qnbk.currency) == ("QNB", 15.47, 15.47, "QAR")
    assert _levels(qnbk.bids) == [(15.22, 3985.0)]
    assert _levels(qnbk.asks) == [(15.52, 3900.0)]
    assert (qnbk.open, qnbk.high, qnbk.low) == (None, None, None)
    assert (qnbk.volume, qnbk.turnover) == (0.0, 0.0)
    assert qibk.name == "Qatar Islamic Bank"


def test_zse_zagreb_first_row_is_concrete() -> None:
    body = _body("zse_zagreb/price-list-2026-11-11.json")
    adpl, *_rest = _parse(_shipped("zse_zagreb"), body)
    assert (adpl.listing.local_symbol, adpl.listing.isin) == ("ADPL", "HRADPLRA0006")
    assert (adpl.open, adpl.high, adpl.low, adpl.last) == (21.5, 21.5, 21.3, 21.3)
    assert (adpl.volume, adpl.turnover, adpl.currency) == (252.0, 5383.0, "EUR")


def test_bse_botswana_first_row_reads_nested_details() -> None:
    kno, _btcl, _letshego, fnbb, _stan = _parse(
        _shipped("bse_botswana"), _body("bse_botswana/market-overview-domestic.json")
    )
    assert (kno.listing.local_symbol, kno.last, kno.volume) == ("KNO", 1.2, 22222.0)
    assert (kno.turnover, kno.currency) == (26666.4, "BWP")
    assert (fnbb.last, fnbb.currency) == (5.75, "BWP")


def test_csx_cambodia_reads_both_boards_in_file_order() -> None:
    pwsa, gti, _ppap, dbde, _jsl = _parse(
        _shipped("csx_cambodia"),
        _body("csx_cambodia/main-and-growth-board-stocks-trades.json"),
    )
    assert (pwsa.listing.local_symbol, pwsa.last, pwsa.currency) == ("PWSA", 6960.0, "KHR")
    assert (gti.listing.isin, gti.last) == ("KH1000020003", 8060.0)
    assert (dbde.listing.local_symbol, dbde.last) == ("DBDE", 2260.0)


def test_amman_row_filter_drops_the_index_row() -> None:
    abus, *_rest = _parse(_shipped("amman"), _body("amman/ticker_feeds.json"))
    assert abus.listing.local_symbol == "ABUS"
    assert abus.name == "COMPREHENSIVE MULTIPLE TRANSPORTATIONS CO."
    assert (abus.last, abus.open, abus.currency) == (0.57, 0.57, "JOD")


def test_blberza_reads_european_decimals() -> None:
    bosk, duvn, ekbl, *_rest = _parse(_shipped("blberza"), _body("blberza/defaultTicker.json"))
    assert (bosk.name, bosk.last, bosk.currency) == ("Boska RK a.d. Banja Luka", 0.43, "BAM")
    assert duvn.last == 0.24
    assert ekbl.last == 0.196


# --- spec_request -----------------------------------------------------------------------


def test_spec_request_fills_the_date_placeholder_in_the_spec_format() -> None:
    request = spec_request(_shipped("zse_zagreb"), date(2026, 11, 11))
    assert request.method == "GET"
    assert request.url.endswith("/price-list/XZAG/2026-11-11/json")
    assert request.json_body is None and request.form == ()
    assert request.source == "site_zse_zagreb"


def test_spec_request_refuses_a_date_placeholder_without_a_day() -> None:
    with pytest.raises(ValueError):
        spec_request(_shipped("zse_zagreb"))


def test_form_specs_send_their_form_and_json_specs_their_body() -> None:
    botswana = spec_request(_shipped("bse_botswana"))
    assert botswana.method == "POST"
    assert botswana.form == (("board", "domestic"),)
    assert botswana.json_body is None
    sase = spec_request(_shipped("sase"))
    assert sase.method == "POST"
    assert dict(sase.form) == {"type": "20", "symbol": "", "lng": "0"}
    colombo = spec_request(_shipped("cse_colombo"))
    assert colombo.method == "POST" and colombo.json_body == {} and colombo.form == ()
    bvl = spec_request(_shipped("bvl"))
    assert bvl.json_body == {"sector": "", "today": True, "companyCode": "", "inputCompany": ""}


# --- SiteLive ---------------------------------------------------------------------------


async def test_board_is_reused_within_min_interval_and_refetched_after_it() -> None:
    fetch, seen = _fake(lambda req: _response(200, _body("aix/ep_mw_main_records.json")))
    source = SHIPPED_CLASS["aix"](fetch)
    interval = _shipped("aix").min_interval_s
    first = await source.board("AIXK", captured_ns=CAPTURED_NS)
    again = await source.board("AIXK", captured_ns=CAPTURED_NS + int(10 * NS))
    assert again is first and len(seen) == 1
    later = await source.board("AIXK", captured_ns=CAPTURED_NS + int((interval + 1) * NS))
    assert later is not first and len(seen) == 2
    assert await source.board("XWAR", captured_ns=CAPTURED_NS) == []
    assert len(seen) == 2


async def test_snapshots_return_only_the_requested_listings_and_keep_their_isin() -> None:
    fetch, seen = _fake(lambda req: _response(200, _body("aix/ep_mw_main_records.json")))
    source = SHIPPED_CLASS["aix"](fetch)
    listings = [
        LiveListing(mic="AIXK", local_symbol="KAP", isin="KZ1C00001619"),
        LiveListing(mic="AIXK", local_symbol="NOPE"),
        LiveListing(mic="XWAR", local_symbol="KAP"),
    ]
    snapshots = await source.snapshots(listings, captured_ns=CAPTURED_NS)
    assert len(seen) == 1
    (snapshot,) = snapshots
    assert snapshot.listing == listings[0]
    assert snapshot.source == "site_aix"
    assert snapshot.listing.symbol == "KAP@AIXK"
    assert snapshot.last == 27810.0


async def test_a_listing_that_was_not_asked_for_is_absent_even_when_on_the_board() -> None:
    fetch, _seen = _fake(lambda req: _response(200, _body("aix/ep_mw_main_records.json")))
    source = SHIPPED_CLASS["aix"](fetch)
    snapshots = await source.snapshots(
        [LiveListing(mic="AIXK", local_symbol="KAP.Y")], captured_ns=CAPTURED_NS
    )
    assert [s.listing.local_symbol for s in snapshots] == ["KAP.Y"]
    assert await source.snapshots([], captured_ns=CAPTURED_NS) == []


async def test_a_date_spec_walks_back_past_days_that_404() -> None:
    zagreb_body = _body("zse_zagreb/price-list-2026-11-11.json")

    def answer(request: HttpRequest) -> HttpResponse:
        if "2026-11-13" in request.url or "2026-11-12" in request.url:
            return _response(404, b"", "text/plain", request.url)
        return _response(200, zagreb_body, "application/json", request.url)

    fetch, seen = _fake(answer)
    source = SHIPPED_CLASS["zse_zagreb"](fetch)
    # The poll is at 12:00 in Zagreb on 2026-11-13: the walk tries 11-13, 11-12, then 11-11.
    rows = await source.board("XZAG", captured_ns=_utc_ns(2026, 11, 13, 11))
    assert [request.url.rsplit("/", 2)[-2] for request in seen] == [
        "2026-11-13",
        "2026-11-12",
        "2026-11-11",
    ]
    assert len(rows) == 5
    assert rows[0].listing.local_symbol == "ADPL"


async def test_a_date_spec_walks_past_a_day_that_answers_with_no_rows() -> None:
    zagreb_body = _body("zse_zagreb/price-list-2026-11-11.json")

    def answer(request: HttpRequest) -> HttpResponse:
        if "2026-11-13" in request.url:
            return _response(200, b'{"securities": []}', "application/json", request.url)
        return _response(200, zagreb_body, "application/json", request.url)

    fetch, seen = _fake(answer)
    source = SHIPPED_CLASS["zse_zagreb"](fetch)
    rows = await source.board("XZAG", captured_ns=_utc_ns(2026, 11, 13, 11))
    assert len(seen) == 2
    assert len(rows) == 5


async def test_a_date_spec_with_every_day_missing_raises_the_last_payload_error() -> None:
    fetch, seen = _fake(lambda req: _response(404, b"", "text/plain", req.url))
    source = SHIPPED_CLASS["zse_zagreb"](fetch)
    with pytest.raises(LivePayloadError, match="HTTP 404"):
        await source.board("XZAG", captured_ns=_utc_ns(2026, 11, 13, 11))
    assert len(seen) == 6


async def test_a_non_date_spec_missing_its_board_raises_a_payload_error() -> None:
    fetch, seen = _fake(lambda req: _response(404, b"", "text/plain", req.url))
    source = SHIPPED_CLASS["aix"](fetch)
    with pytest.raises(LivePayloadError, match="HTTP 404"):
        await source.board("AIXK", captured_ns=CAPTURED_NS)
    assert len(seen) == 1


async def test_a_403_is_a_wall_and_is_not_walked_around() -> None:
    fetch, seen = _fake(lambda req: _response(403, b"denied", "text/html", req.url))
    source = SHIPPED_CLASS["aix"](fetch)
    with pytest.raises(SourceUnservableError) as caught:
        await source.snapshots([LiveListing(mic="AIXK", local_symbol="KAP")], captured_ns=1)
    assert caught.value.reason is UnservableReason.FORBIDDEN
    assert len(seen) == 1


async def test_a_403_on_the_first_date_stops_the_walk_at_once() -> None:
    fetch, seen = _fake(lambda req: _response(403, b"denied", "application/json", req.url))
    source = SHIPPED_CLASS["zse_zagreb"](fetch)
    with pytest.raises(SourceUnservableError) as caught:
        await source.board("XZAG", captured_ns=_utc_ns(2026, 11, 13, 11))
    assert caught.value.reason is UnservableReason.FORBIDDEN
    assert len(seen) == 1


async def test_a_redirect_and_a_200_bot_page_are_walls_on_a_json_spec() -> None:
    fetch, _seen = _fake(lambda req: _response(302, b"", "text/html", req.url))
    with pytest.raises(SourceUnservableError) as redirect:
        await SHIPPED_CLASS["aix"](fetch).board("AIXK", captured_ns=CAPTURED_NS)
    assert redirect.value.reason is UnservableReason.BOT_CHALLENGE
    fetch, seen = _fake(
        lambda req: _response(200, b"<html>Please verify you are human</html>", "text/html")
    )
    with pytest.raises(SourceUnservableError) as challenge:
        await SHIPPED_CLASS["zse_zagreb"](fetch).board(
            "XZAG", captured_ns=_utc_ns(2026, 11, 13, 11)
        )
    assert challenge.value.reason is UnservableReason.BOT_CHALLENGE
    assert len(seen) == 1


async def test_a_form_spec_posts_its_form_through_the_fetcher() -> None:
    fetch, seen = _fake(
        lambda req: _response(200, _body("bse_botswana/market-overview-domestic.json"))
    )
    source = SHIPPED_CLASS["bse_botswana"](fetch)
    rows = await source.board("XBOT", captured_ns=CAPTURED_NS)
    assert [r.listing.local_symbol for r in rows][:1] == ["KNO"]
    (request,) = seen
    assert request.method == "POST"
    assert request.form == (("board", "domestic"),)
    assert request.json_body is None
    assert request.source == "site_bse_botswana"


def test_a_source_built_for_a_spec_carries_the_spec_facts() -> None:
    cls = SHIPPED_CLASS["bse_botswana"]
    assert cls.name == "site_bse_botswana"
    assert cls.max_tier is LiveTier.LAST
    assert cls.supported_mics == frozenset({"XBOT"})
    assert cls.official is SHIPPED["bse_botswana"].official
