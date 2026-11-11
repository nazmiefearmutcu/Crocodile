"""The four official listing directories (task 12): parsers, snapshots, provider contract.

Offline throughout. Every document comes from the trimmed recorded fixtures under
``fixtures/directories`` (labelled in their names) or from a variant built inside a test from one of
those fixtures. ``nasdaqlisted_synthetic.txt`` is a labelled synthetic file: the documented layout
with six hand-written rows, because the staged inputs carry only ``otherlisted``. Every test that
uses it says "synthetic" in its docstring. The network is forbidden by the ``network_forbidden``
fixture wherever a test could reach it.
"""

from __future__ import annotations

import asyncio
import csv
import io
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from crocodile.core.markets.errors import InvalidSymbolError
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.grammar import load_grammar
from crocodile.core.markets.master import ListingMaster, ListingStatus
from crocodile.core.markets.symbols import listing_symbol
from crocodile.core.markets.usage import UsageClass
from crocodile.core.ratelimit.token_bucket import TokenBucket
from crocodile.core.schema.enums import AssetClass, SecurityType
from crocodile.core.schema.provenance import Provenance
from crocodile.core.schema.records import Instrument
from crocodile.core.sink.memory import MemorySink
from crocodile.equity.providers import factory
from crocodile.equity.providers.base import (
    ProviderError,
    TransientProviderError,
    UnsupportedMarketError,
)
from crocodile.equity.reference.registry import InstrumentRegistry
from crocodile.equity.universal.base import (
    SourceUnservableError,
    UnservableReason,
    apply_universe,
    contract_violations,
)
from crocodile.equity.universal.directories import base as dbase
from crocodile.equity.universal.directories.asx import (
    ASX_HEADER,
    ASX_LISTED_URL,
    AsxProvider,
    parse_as_at,
    parse_asx_listed,
)
from crocodile.equity.universal.directories.base import (
    DirectoryFormatError,
    DirectoryParse,
    DirectoryProvider,
    directory_records,
    snapshot_from_parses,
)
from crocodile.equity.universal.directories.nasdaqtrader import (
    NASDAQ_MARKET_CATEGORY,
    NASDAQLISTED_HEADER,
    NASDAQLISTED_URL,
    OTHERLISTED_EXCHANGE,
    OTHERLISTED_HEADER,
    OTHERLISTED_URL,
    NasdaqTraderProvider,
    parse_file_creation_time,
    parse_nasdaqlisted,
    parse_otherlisted,
)
from crocodile.equity.universal.directories.nse_india import (
    EQUITY_L_HEADER,
    EQUITY_L_URL,
    EQUITY_SERIES,
    NseIndiaProvider,
    parse_equity_l,
    parse_listing_date,
)
from crocodile.equity.universal.directories.twse import (
    COL_COMPANY_CODE,
    COL_COMPANY_NAME,
    COL_ENGLISH_SHORT_NAME,
    COL_LISTING_DATE,
    COL_REPORT_DATE,
    T187AP03_URL,
    TWSE_COLUMN_COUNT,
    TwseProvider,
    parse_t187ap03,
    roc_date,
)
from crocodile.equity.universal.http import HttpRequest, HttpResponse

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "directories"
NS = 1_000_000_000
CAPTURED_NS = 1_794_100_000 * NS
AS_OF_NASDAQ = 1_794_018_660 * NS
AS_OF_ASX = 1_794_097_402 * NS
AS_OF_TWSE = 1_794_009_600 * NS
TWSE_NAME_1101 = "\u81fa\u7063\u6c34\u6ce5\u80a1\u4efd\u6709\u9650\u516c\u53f8"  # the 1101 name


# ----------------------------------------------------------------- helpers


def _read_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _read_text(name: str) -> str:
    return _read_bytes(name).decode("utf-8")


def _otherlisted() -> DirectoryParse:
    return parse_otherlisted(_read_text("otherlisted.txt"))


def _synthetic_nasdaqlisted() -> DirectoryParse:
    return parse_nasdaqlisted(_read_text("nasdaqlisted_synthetic.txt"))


def _nse() -> DirectoryParse:
    return parse_equity_l(_read_text("EQUITY_L.csv"))


def _asx() -> DirectoryParse:
    return parse_asx_listed(_read_text("ASXListedCompanies.csv"))


def _twse() -> DirectoryParse:
    return parse_t187ap03(_read_bytes("t187ap03_L.csv"))


def _parse_for(name: str) -> tuple[str, DirectoryParse]:
    """(source name, the fixture's parse) for each of the four directories."""
    if name == "nasdaqtrader":
        return name, _otherlisted()
    if name == "nse_india":
        return name, _nse()
    if name == "asx":
        return name, _asx()
    if name == "twse":
        return name, _twse()
    raise AssertionError(name)


def _snapshot_for(name: str, *, allow: tuple[str, ...] = ()) -> Any:
    source, parse = _parse_for(name)
    return snapshot_from_parses(source, [parse], captured_at_ns=CAPTURED_NS, allow=allow)


class FakeFetch:
    """A fetcher that serves canned responses by URL and records every request it saw."""

    def __init__(
        self,
        responses: Mapping[str, HttpResponse] | None = None,
        *,
        raises: BaseException | None = None,
    ) -> None:
        self._responses = dict(responses or {})
        self._raises = raises
        self.requests: list[HttpRequest] = []

    @property
    def urls(self) -> list[str]:
        return [request.url for request in self.requests]

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if self._raises is not None:
            raise self._raises
        response = self._responses.get(request.url)
        if response is None:
            raise AssertionError(f"no canned response for {request.url}")
        return response


def _ok(body: bytes, url: str, content_type: str = "text/plain") -> HttpResponse:
    return HttpResponse(status=200, headers={"content-type": content_type}, body=body, url=url)


def _status(status: int, url: str, headers: Mapping[str, str] | None = None) -> HttpResponse:
    return HttpResponse(status=status, headers=dict(headers or {}), body=b"", url=url)


def _fast_bucket() -> TokenBucket:
    return TokenBucket(capacity=1000, refill_rate=1000)


class FakeBucket:
    """Duck-typed bucket that records the back-off delays it was given."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def acquire(self, tokens: float = 1.0) -> None:
        return None

    def update_backoff(self, delay: float) -> None:
        self.delays.append(delay)


def _bodies_for(name: str) -> dict[str, bytes]:
    if name == "nasdaqtrader":
        return {
            NASDAQLISTED_URL: _read_bytes("nasdaqlisted_synthetic.txt"),
            OTHERLISTED_URL: _read_bytes("otherlisted.txt"),
        }
    if name == "nse_india":
        return {EQUITY_L_URL: _read_bytes("EQUITY_L.csv")}
    if name == "asx":
        return {ASX_LISTED_URL: _read_bytes("ASXListedCompanies.csv")}
    if name == "twse":
        return {T187AP03_URL: _read_bytes("t187ap03_L.csv")}
    raise AssertionError(name)


def _fetch_all(name: str) -> FakeFetch:
    return FakeFetch({url: _ok(body, url) for url, body in _bodies_for(name).items()})


def _provider_cls(name: str) -> type[DirectoryProvider]:
    return {
        "nasdaqtrader": NasdaqTraderProvider,
        "nse_india": NseIndiaProvider,
        "asx": AsxProvider,
        "twse": TwseProvider,
    }[name]


# ------------------------------------------------- nasdaqtrader (step 3)


def test_otherlisted_maps_exchange_codes_to_operating_and_segment_mics():
    """Exchange codes resolve to the operating MIC, with the segment where the code names one."""
    by_symbol = {row.symbol_raw: row for row in _otherlisted().rows}
    assert (by_symbol["BRK.B"].mic, by_symbol["BRK.B"].segment_mic) == ("XNYS", None)
    assert (by_symbol["AAA"].mic, by_symbol["AAA"].segment_mic) == ("XNYS", "ARCX")
    assert (by_symbol["AAAA"].mic, by_symbol["AAAA"].segment_mic) == ("XCBO", "BATS")


def test_test_issues_are_skipped_and_counted():
    """Six test issues are skipped (checked before any exchange mapping); 45 rows remain."""
    parse = _otherlisted()
    assert parse.skipped["test_issue"] == 6
    assert len(parse.rows) == 45
    codes = Counter(row.mic for row in parse.rows)
    assert codes["XNYS"] == 25  # N 20 and P 5; the F rows (TXSE) are kept by the parse
    assert codes["XCBO"] == 17
    assert codes["TXSE"] == 3
    counts = Counter(
        ("ETF" if row.security_type is SecurityType.ETF else "UNKNOWN") for row in parse.rows
    )
    assert counts["ETF"] == 22


def test_the_etf_flag_is_the_only_type_the_directory_states():
    """Every P and Z row is ETF because the file says so; BRK.B is UNKNOWN, never inferred."""
    parse = _otherlisted()
    etf_rows = [row for row in parse.rows if row.security_type is SecurityType.ETF]
    assert len(etf_rows) == 22
    assert all(row.mic in {"XNYS", "XCBO"} for row in etf_rows)
    brk = next(row for row in parse.rows if row.symbol_raw == "BRK.B")
    assert brk.security_type is SecurityType.UNKNOWN


def test_act_symbols_are_kept_as_published():
    """ACT symbology keeps dollar signs, dots and the preferred notation verbatim."""
    raws = {row.symbol_raw for row in _otherlisted().rows}
    for symbol in ("ABR$D", "AAC.U", "AAC.W", "BRK.A"):
        assert symbol in raws, symbol
    assert all(row.local_symbol == row.symbol_raw for row in _otherlisted().rows)


def test_the_footer_is_the_as_of_instant():
    """The footer is the file's creation time, read as New York local time: 2026-11-07 02:31Z."""
    assert _otherlisted().as_of_ns == AS_OF_NASDAQ
    assert parse_file_creation_time("File Creation Time: 1106202621:31||||||") == AS_OF_NASDAQ


def test_a_file_without_its_footer_is_truncated_and_refused():
    """A missing footer is a truncated download: refused, never half-read."""
    lines = _read_text("otherlisted.txt").splitlines()
    truncated = "\n".join(lines[:-1]) + "\n"
    with pytest.raises(DirectoryFormatError):
        parse_otherlisted(truncated)


def test_a_changed_header_is_refused():
    """A header that is not the documented one is format drift: refused, never misparsed."""
    text = _read_text("otherlisted.txt")
    changed = text.replace("ACT Symbol", "ACT SYMBOL", 1)
    with pytest.raises(DirectoryFormatError):
        parse_otherlisted(changed)
    assert OTHERLISTED_HEADER[0] == "ACT Symbol"


def test_crlf_and_lf_parse_identically():
    """The source is CRLF and git may rewrite fixtures to LF: both spell the same parse."""
    text = _read_text("otherlisted.txt").replace("\r\n", "\n")
    lf = parse_otherlisted(text)
    crlf = parse_otherlisted(text.replace("\n", "\r\n"))
    assert lf.rows == crlf.rows
    assert dict(lf.skipped) == dict(crlf.skipped)
    assert lf.as_of_ns == crlf.as_of_ns


def test_nasdaqlisted_market_category_is_the_segment():
    """Synthetic file: Q, G and S name the XNAS segments; the test issue and the unknown category
    are counted once each, and every row is XNAS."""
    parse = _synthetic_nasdaqlisted()
    assert dict(parse.skipped) == {"test_issue": 1, "unknown_market_category:X": 1}
    segments = {row.symbol_raw: row.segment_mic for row in parse.rows}
    assert segments == {"AAPL": "XNGS", "MSFT": "XNGS", "QQQ": "XNMS", "SNAL": "XNCM"}
    assert all(row.mic == "XNAS" for row in parse.rows)
    qqq = next(row for row in parse.rows if row.symbol_raw == "QQQ")
    assert qqq.security_type is SecurityType.ETF
    assert NASDAQLISTED_HEADER[0] == "Symbol"


def test_every_segment_belongs_to_its_operating_mic():
    """Every (operating, segment) pair in both tables resolves back to its operating MIC."""
    from crocodile.core.markets.mic import MicRegistry

    registry = MicRegistry()
    for operating, segment in OTHERLISTED_EXCHANGE.values():
        if segment is not None:
            assert registry.operating(segment) == operating, segment
    for segment in NASDAQ_MARKET_CATEGORY.values():
        assert registry.operating(segment) == "XNAS", segment


def test_texas_rows_are_dropped_because_txse_is_not_a_listed_market():
    """TXSE is not in exchanges.csv, so its three rows are dropped and counted, not guessed."""
    assert "TXSE" not in ExchangeRegistry(), (
        "TXSE joined exchanges.csv: update this test and supported_mics"
    )
    snapshot = snapshot_from_parses("nasdaqtrader", [_otherlisted()], captured_at_ns=CAPTURED_NS)
    assert snapshot.skipped["market_not_listed:TXSE"] == 3
    assert len(snapshot.seen["XNYS"]) == 25
    assert len(snapshot.seen["XCBO"]) == 17


def test_nasdaqtrader_supported_mics_are_its_listed_mapped_mics():
    """The declared scope is every operating MIC the tables map to, restricted to the registry."""
    operating = {"XNAS"} | {op for op, _ in OTHERLISTED_EXCHANGE.values()}
    expected = frozenset(mic for mic in operating if mic in ExchangeRegistry())
    assert NasdaqTraderProvider.supported_mics == expected


def test_a_malformed_row_is_skipped_and_counted_not_guessed():
    """A data line that is not 8 fields is a malformed row, counted; the rest still parse."""
    text = _read_text("otherlisted.txt")
    lines = text.splitlines()
    lines.insert(2, "BROKEN|ONLY|THREE")
    parse = parse_otherlisted("\n".join(lines) + "\n")
    assert parse.skipped["malformed_row"] == 1
    assert parse.skipped["test_issue"] == 6


# ----------------------------------------------------------- nse_india (step 4)


def test_equity_l_rows_carry_isin_and_listing_date():
    """RELIANCE carries its valid ISIN and its listing date; the file has 35 rows."""
    parse = _nse()
    assert len(parse.rows) == 35
    reliance = next(row for row in parse.rows if row.local_symbol == "RELIANCE")
    assert reliance.isin == "INE002A01018"
    assert reliance.listing_date == "1995-11-29"
    assert reliance.mic == "XNSE"
    assert reliance.security_type is SecurityType.UNKNOWN
    assert EQUITY_L_HEADER[0] == "SYMBOL"


def test_series_are_kept_on_the_row_and_validated():
    """EQ 28, BE 5, BZ 2 on the rows; an unlisted series is counted, never persisted as a guess."""
    parse = _nse()
    counts = Counter(row.series for row in parse.rows)
    assert counts == Counter({"EQ": 28, "BE": 5, "BZ": 2})
    assert set(EQUITY_SERIES) == {"EQ", "BE", "BZ"}
    extra = _read_text("EQUITY_L.csv") + (
        "FOOBAR,Foo Limited,SM,01-JAN-2000,10,1,INE002A01018,10\n"
    )
    with_sm = parse_equity_l(extra)
    assert with_sm.skipped["series:SM"] == 1
    assert len(with_sm.rows) == 35


def test_ampersand_and_hyphen_symbols_survive():
    """Local symbols keep '&' and '-' exactly as published."""
    snapshot = _snapshot_for("nse_india")
    symbols = {record.symbol for record in snapshot.records}
    assert "M&M@XNSE" in symbols
    assert "BAJAJ-AUTO@XNSE" in symbols


def test_an_invalid_isin_is_dropped_not_guessed():
    """A failing check digit is dropped to None and counted; the row itself is kept."""
    text = _read_text("EQUITY_L.csv").replace("INE002A01018", "INE002A01019")
    parse = parse_equity_l(text)
    reliance = next(row for row in parse.rows if row.local_symbol == "RELIANCE")
    assert reliance.isin is None
    assert parse.dropped_fields["isin"] == 1
    assert len(parse.rows) == 35


def test_nse_states_no_as_of_instant():
    """EQUITY_L.csv states no date: the parse and every record carry no source timestamp."""
    assert _nse().as_of_ns is None
    snapshot = _snapshot_for("nse_india")
    assert snapshot.records
    assert all(record.source_ts is None for record in snapshot.records)


def test_a_changed_nse_header_is_refused():
    """A renamed column is format drift: refused."""
    text = _read_text("EQUITY_L.csv").replace("ISIN NUMBER", "ISIN NO", 1)
    with pytest.raises(DirectoryFormatError):
        parse_equity_l(text)


def test_listing_dates_are_parsed_with_an_explicit_month_map():
    """DD-MMM-YYYY with an explicit English month map; anything else is None, never guessed."""
    assert parse_listing_date("06-OCT-2008") == "2008-10-06"
    assert parse_listing_date("01-JAN-1995") == "1995-01-01"
    assert parse_listing_date("31-FEB-2000") is None
    assert parse_listing_date("2008-10-06") is None
    assert parse_listing_date("06-XXX-2008") is None


# ------------------------------------------------------------- asx (step 5)


def test_the_preamble_is_the_as_of_instant():
    """'as at Sun Nov 08 11:23:22 AEDT 2026' is 2026-11-08 00:23:22Z."""
    assert _asx().as_of_ns == AS_OF_ASX
    assert parse_as_at("ASX listed companies as at Sun Nov 08 11:23:22 AEDT 2026") == AS_OF_ASX


def test_asx_rows_and_quoted_commas():
    """32 rows; quoted names keep their commas and their trailing dots as published."""
    parse = _asx()
    assert len(parse.rows) == 32
    by_code = {row.local_symbol: row for row in parse.rows}
    assert by_code["BHP"].name == "BHP GROUP LIMITED"
    additive = [row for row in parse.rows if row.name and "ADDITIVE" in row.name]
    assert additive, "the quoted 6K ADDITIVE row is missing"
    assert additive[0].name == "6K ADDITIVE, INC."
    assert by_code["CBA"].name == "COMMONWEALTH BANK OF AUSTRALIA."
    assert ASX_HEADER[0] == "Company name"


def test_a_file_without_the_preamble_is_refused():
    """The preamble is the first line: its absence is format drift, refused."""
    text = _read_text("ASXListedCompanies.csv")
    without = "\n".join(text.splitlines()[2:]) + "\n"
    with pytest.raises(DirectoryFormatError):
        parse_asx_listed(without)


def test_an_aest_preamble_parses():
    """AEST (standard time) parses with its own offset: a June date in Sydney is UTC+10."""
    instant = parse_as_at("ASX listed companies as at Mon Jun 08 11:23:22 AEST 2026")
    expected = datetime(2026, 6, 8, 1, 23, 22, tzinfo=UTC)
    assert instant == int(expected.timestamp()) * NS


# ------------------------------------------------------------- twse (step 6)


def test_the_bom_and_chinese_headers_parse():
    """BOM and the Chinese headers parse; 12 rows; 1101 carries its full name and listing date."""
    parse = _twse()
    assert len(parse.rows) == 12
    first = parse.rows[0]
    assert first.local_symbol == "1101"
    assert first.name == TWSE_NAME_1101
    assert first.listing_date == "1962-02-09"
    assert first.mic == "XTAI"
    locals_ = {row.local_symbol for row in parse.rows}
    assert {"2330", "9958"} <= locals_


def test_the_header_escapes_match_the_file():
    """Every COL_* constant is a cell of the fixture header, and the header has 33 columns."""
    text = _read_bytes("t187ap03_L.csv").decode("utf-8-sig")
    header = next(csv.reader(io.StringIO(text, newline="")))
    assert len(header) == TWSE_COLUMN_COUNT
    for column in (
        COL_REPORT_DATE,
        COL_COMPANY_CODE,
        COL_COMPANY_NAME,
        COL_LISTING_DATE,
        COL_ENGLISH_SHORT_NAME,
    ):
        assert column in header, column
    assert header[0] == COL_REPORT_DATE
    assert TWSE_COLUMN_COUNT == 33


def test_the_roc_report_date_is_the_as_of():
    """ROC 115 is 2026: report date 1151107 is 2026-11-07, UTC midnight of it."""
    assert roc_date("1151107") == date(2026, 11, 7)
    assert _twse().as_of_ns == AS_OF_TWSE


def test_a_file_without_a_bom_parses_the_same():
    """The BOM is optional: the same bytes without it parse to the same rows."""
    raw = _read_bytes("t187ap03_L.csv")
    assert raw.startswith(b"\xef\xbb\xbf")
    without = parse_t187ap03(raw[3:])
    assert without.rows == _twse().rows
    assert without.as_of_ns == _twse().as_of_ns


def test_mixed_report_dates_are_refused():
    """One row with a different report date means a mixed download: refused."""
    raw = _read_bytes("t187ap03_L.csv")
    changed = raw.replace(b'"1151107","1102",', b'"1151108","1102",', 1)
    assert changed != raw
    with pytest.raises(DirectoryFormatError):
        parse_t187ap03(changed)


def test_twse_is_redistributable_with_attribution():
    """The one directory with a read licence: redistribution, and the attribution names it."""
    assert TwseProvider.usage_class is UsageClass.REDISTRIBUTION
    assert "Open Government Data License" in (TwseProvider.attribution or "")


# ---------------------------------------- snapshot, provider and registration (step 7)


@pytest.mark.parametrize("name", ["nasdaqtrader", "nse_india", "asx", "twse"])
def test_directory_records_are_listing_directory_snapshots(name: str):
    """Every record is a NATIVE listing_directory snapshot with its own exchange and no currency."""
    snapshot = _snapshot_for(name)
    assert snapshot.records
    by_symbol = {record.symbol: record for record in snapshot.records}
    registry = ExchangeRegistry()
    for observation in snapshot.observations:
        symbol = listing_symbol(observation.local_symbol, observation.mic)
        record = by_symbol[symbol]
        assert isinstance(record, Instrument)
        assert record.prov is Provenance.NATIVE
        assert record.prov_basis == "listing_directory"
        assert record.prov_confidence == 1.0
        assert record.exchange == (observation.segment_mic or observation.mic)
        assert record.currency is None
        assert record.price_scale is None
        assert record.country == registry.get(observation.mic).country
        assert record.local_ts == CAPTURED_NS
        assert record.asset_class is AssetClass.EQUITY
    _, parse = _parse_for(name)
    assert all(record.source_ts == parse.as_of_ns for record in snapshot.records)


@pytest.mark.parametrize("name", ["nasdaqtrader", "nse_india", "asx", "twse"])
def test_snapshots_are_complete_sorted_and_deterministic(name: str):
    """The same input gives the same snapshot; records sort by symbol, observations by key."""
    first = _snapshot_for(name)
    second = _snapshot_for(name)
    assert first == second
    assert first.complete is True
    symbols = [record.symbol for record in first.records]
    assert symbols == sorted(symbols)
    keys = [(item.mic, item.local_symbol) for item in first.observations]
    assert keys == sorted(keys)
    assert set(first.seen) == {item.mic for item in first.observations}
    for mic, locals_ in first.seen.items():
        assert locals_ == frozenset(
            item.local_symbol for item in first.observations if item.mic == mic
        )


def test_observations_carry_the_yahoo_alias_where_the_grammar_has_one():
    """Aliases only where the Yahoo grammar covers the MIC and the local spells cleanly."""
    assert load_grammar("yahoo_chart") is not None
    nse = {(item.local_symbol, item.mic): item for item in _snapshot_for("nse_india").observations}
    asx = {(item.local_symbol, item.mic): item for item in _snapshot_for("asx").observations}
    twse = {(item.local_symbol, item.mic): item for item in _snapshot_for("twse").observations}
    nasdaq = _snapshot_for("nasdaqtrader").observations

    assert nse[("RELIANCE", "XNSE")].extra_aliases == (("yahoo_chart", "RELIANCE.NS"),)
    assert asx[("BHP", "XASX")].extra_aliases == (("yahoo_chart", "BHP.AX"),)
    assert twse[("1101", "XTAI")].extra_aliases == (("yahoo_chart", "1101.TW"),)
    brk = next(item for item in nasdaq if item.local_symbol == "BRK.B")
    assert brk.mic == "XNYS"
    assert brk.extra_aliases == (("yahoo_chart", "BRK-B"),)
    bzx_etf = next(item for item in nasdaq if item.mic == "XCBO")
    assert bzx_etf.extra_aliases == ()
    abr = next(item for item in nasdaq if item.local_symbol == "ABR$D")
    assert abr.extra_aliases == ()
    assert nse[("M&M", "XNSE")].extra_aliases == ()
    for snapshot_name in ("nasdaqtrader", "nse_india", "asx", "twse"):
        for item in _snapshot_for(snapshot_name).observations:
            assert item.currency is None
            assert item.price_scale is None


def test_the_star_symbol_passes_the_market_scope_guard():
    """["*"] means the whole directory: it is not a US symbol and does not trip the scope guard."""
    provider = factory.make_provider(
        "nse_india",
        symbols=["*"],
        channels=["instrument"],
        out=MemorySink(),
        registry=InstrumentRegistry(),
        fetch=FakeFetch(),
    )
    assert isinstance(provider, NseIndiaProvider)
    assert provider.symbols == ["*"]
    with pytest.raises(UnsupportedMarketError):
        factory.make_provider(
            "nse_india",
            symbols=["7203@XJPX"],
            channels=["instrument"],
            out=MemorySink(),
            registry=InstrumentRegistry(),
            fetch=FakeFetch(),
        )


def test_apply_universe_writes_directory_rows_into_the_master():
    """A complete snapshot writes its rows; a later complete snapshot without BHP marks it unseen,
    never delisted."""
    master = ListingMaster(":memory:")
    try:
        first = _snapshot_for("asx")
        report = apply_universe(master, first)
        assert report.upserted == 32
        listing = master.find("XASX", "BHP")
        assert listing is not None
        assert listing.status is ListingStatus.ACTIVE
        assert listing.currency is None

        text = _read_text("ASXListedCompanies.csv")
        without_bhp = (
            "\n".join(
                line for line in text.splitlines() if not line.startswith('"BHP GROUP LIMITED"')
            )
            + "\n"
        )
        second = snapshot_from_parses(
            "asx", [parse_asx_listed(without_bhp)], captured_at_ns=CAPTURED_NS + NS
        )
        apply_universe(master, second)
        after = master.find("XASX", "BHP")
        assert after is not None
        assert after.status is ListingStatus.UNSEEN
        assert after.status is not ListingStatus.DELISTED
    finally:
        master.close()


@pytest.mark.parametrize("name", ["nasdaqtrader", "nse_india", "asx", "twse"])
def test_each_directory_is_registered_and_declares_the_contract(name: str):
    """Registered under its name, with the instrument channel and no contract violations."""
    provider = factory.make_provider(
        name,
        symbols=["*"],
        channels=["instrument"],
        out=MemorySink(),
        registry=InstrumentRegistry(),
        fetch=_fetch_all(name),
    )
    assert isinstance(provider, _provider_cls(name))
    assert contract_violations(type(provider)) == []
    assert factory.supported_channels(name) == frozenset({"instrument"})
    assert name in factory._REGISTRY


def test_fetch_requests_each_document_once_through_the_shared_budget(
    monkeypatch, network_forbidden
):
    """Two GETs in urls order, and budget() is called inside the coroutine with the host."""
    calls: list[tuple[str, str]] = []

    def fake_budget(source: str, host: str) -> TokenBucket:
        calls.append((source, host))
        return _fast_bucket()

    monkeypatch.setattr(dbase, "budget", fake_budget)
    fetch = _fetch_all("nasdaqtrader")
    provider = NasdaqTraderProvider(
        ["*"], ["instrument"], MemorySink(), InstrumentRegistry(), fetch=fetch
    )
    asyncio.run(provider.snapshot())
    assert fetch.urls == [NASDAQLISTED_URL, OTHERLISTED_URL]
    assert calls == [("nasdaqtrader", "www.nasdaqtrader.com")]


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (_status(404, NASDAQLISTED_URL), UnservableReason.UNREACHABLE),
        (_status(403, NASDAQLISTED_URL), UnservableReason.FORBIDDEN),
        (
            _status(302, NASDAQLISTED_URL, {"location": "https://example.invalid/"}),
            UnservableReason.BOT_CHALLENGE,
        ),
        (
            _ok(b"<html><body>wall</body></html>", NASDAQLISTED_URL, "text/html"),
            UnservableReason.BOT_CHALLENGE,
        ),
    ],
)
def test_an_unreachable_directory_is_unservable(response: HttpResponse, reason: UnservableReason):
    """Each wall maps to its reason; the request is not retried or bypassed."""
    fetch = FakeFetch({NASDAQLISTED_URL: response, OTHERLISTED_URL: _status(404, OTHERLISTED_URL)})
    provider = NasdaqTraderProvider(
        ["*"],
        ["instrument"],
        MemorySink(),
        InstrumentRegistry(),
        fetch=fetch,
        bucket=_fast_bucket(),
    )
    with pytest.raises(SourceUnservableError) as excinfo:
        asyncio.run(provider.snapshot())
    assert excinfo.value.reason is reason
    assert fetch.urls == [NASDAQLISTED_URL]


def test_a_transport_timeout_is_unservable_and_propagates():
    """A client-side timeout raised by the fetcher reaches the caller as TIMEOUT."""
    fetch = FakeFetch(
        raises=SourceUnservableError("nasdaqtrader", UnservableReason.TIMEOUT, "slow")
    )
    provider = NasdaqTraderProvider(
        ["*"],
        ["instrument"],
        MemorySink(),
        InstrumentRegistry(),
        fetch=fetch,
        bucket=_fast_bucket(),
    )
    with pytest.raises(SourceUnservableError) as excinfo:
        asyncio.run(provider.snapshot())
    assert excinfo.value.reason is UnservableReason.TIMEOUT


@pytest.mark.parametrize(
    ("headers", "delay"),
    [({}, 5.0), ({"retry-after": "7"}, 7.0)],
)
def test_429_backs_off_the_bucket_and_raises_transient(headers: dict[str, str], delay: float):
    """A rate limit backs the shared bucket off by Retry-After (5 s if absent); it is transient."""
    bucket = FakeBucket()
    fetch = FakeFetch({NASDAQLISTED_URL: _status(429, NASDAQLISTED_URL, headers)})
    provider = NasdaqTraderProvider(
        ["*"],
        ["instrument"],
        MemorySink(),
        InstrumentRegistry(),
        fetch=fetch,
        bucket=bucket,
    )
    with pytest.raises(TransientProviderError):
        asyncio.run(provider.snapshot())
    assert bucket.delays == [delay]


def test_a_server_error_is_transient():
    """A 5xx is a transient failure: the snapshot is retried by its caller, not half-read."""
    fetch = FakeFetch({NASDAQLISTED_URL: _status(503, NASDAQLISTED_URL)})
    provider = NasdaqTraderProvider(
        ["*"],
        ["instrument"],
        MemorySink(),
        InstrumentRegistry(),
        fetch=fetch,
        bucket=_fast_bucket(),
    )
    with pytest.raises(TransientProviderError):
        asyncio.run(provider.snapshot())


def test_one_failed_document_fails_the_whole_snapshot():
    """The otherlisted 404 fails the whole snapshot, so a half-read directory never drives
    mark_unseen."""
    fetch = FakeFetch(
        {
            NASDAQLISTED_URL: _ok(_read_bytes("nasdaqlisted_synthetic.txt"), NASDAQLISTED_URL),
            OTHERLISTED_URL: _status(404, OTHERLISTED_URL),
        }
    )
    provider = NasdaqTraderProvider(
        ["*"],
        ["instrument"],
        MemorySink(),
        InstrumentRegistry(),
        fetch=fetch,
        bucket=_fast_bucket(),
    )
    with pytest.raises(SourceUnservableError) as excinfo:
        asyncio.run(provider.snapshot())
    assert excinfo.value.reason is UnservableReason.UNREACHABLE


def test_run_for_star_writes_every_record_and_applies_the_master(network_forbidden):
    """["*"] puts every record and applies the snapshot to the explicit master."""
    sink = MemorySink()
    master = ListingMaster(":memory:")
    try:
        provider = NseIndiaProvider(
            ["*"],
            ["instrument"],
            sink,
            InstrumentRegistry(),
            fetch=_fetch_all("nse_india"),
            master=master,
            bucket=_fast_bucket(),
        )
        asyncio.run(provider.run())
        assert len(sink.records) == len(_snapshot_for("nse_india").records)
        assert master.find("XNSE", "RELIANCE") is not None
    finally:
        master.close()


def test_run_for_named_symbols_writes_only_those_and_leaves_the_master_alone(network_forbidden):
    """Named symbols put only their own records and never touch the master."""
    sink = MemorySink()
    master = ListingMaster(":memory:")
    try:
        provider = NasdaqTraderProvider(
            ["BRK.B"],
            ["instrument"],
            sink,
            InstrumentRegistry(),
            fetch=_fetch_all("nasdaqtrader"),
            master=master,
            bucket=_fast_bucket(),
        )
        asyncio.run(provider.run())
        assert [record.symbol for record in sink.records] == ["BRK.B"]
        assert master.find("XNYS", "BRK.B") is None
    finally:
        master.close()


def test_backfill_refuses_and_names_collect():
    """A directory is today's snapshot and keeps no history: backfill refuses with the verbs."""
    provider = NseIndiaProvider(
        ["*"],
        ["instrument"],
        MemorySink(),
        InstrumentRegistry(),
        fetch=_fetch_all("nse_india"),
        bucket=_fast_bucket(),
    )

    async def drain() -> None:
        async for _ in provider.backfill("instrument", "RELIANCE", 0, 1):
            pass

    with pytest.raises(ValueError, match="collect --sources"):
        asyncio.run(drain())


def test_an_unknown_symbol_format_is_refused_by_the_scope_guard_not_by_the_parser():
    """A malformed LOCAL@MIC symbol is refused at construction, not sent to the directory."""
    with pytest.raises((UnsupportedMarketError, ProviderError, InvalidSymbolError, ValueError)):
        factory.make_provider(
            "asx",
            symbols=["BHP@NOTAMIC"],
            channels=["instrument"],
            out=MemorySink(),
            registry=InstrumentRegistry(),
            fetch=FakeFetch(),
        )


def test_directory_records_filters_by_symbol_and_star_takes_all():
    """The filter helper: '*' is the whole snapshot; named symbols select only themselves."""
    snapshot = _snapshot_for("nasdaqtrader")
    assert directory_records(snapshot, ["*"]) == snapshot.records
    picked = directory_records(snapshot, ["BRK.B"])
    assert [record.symbol for record in picked] == ["BRK.B"]


def test_a_directory_row_is_the_only_place_an_instrument_is_built():
    """Every directory module builds Instrument with the bare name: the snapshot builder."""
    package = Path(dbase.__file__).resolve().parent
    offenders = []
    for module in sorted(package.glob("*.py")):
        if module.name == "base.py":
            continue
        if "Instrument(" in module.read_text(encoding="utf-8"):
            offenders.append(module.name)
    assert offenders == []
