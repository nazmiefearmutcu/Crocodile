"""Behaviour of the listing master: identity, merge, currency, status, aliases and reads.

Every test uses the real registries and, for file databases, ``tmp_path``. Each master is closed
through a ``with`` block, because an open WAL database keeps its sidecar files locked on Windows.
"""

import ast
from pathlib import Path
from typing import Any

import pytest

from crocodile.core.errors import StoreError
from crocodile.core.markets import master as master_module
from crocodile.core.markets.errors import (
    ExpiredMarketError,
    InvalidSymbolError,
    ListingConflictError,
    UnknownCurrencyError,
    UnknownMarketError,
)
from crocodile.core.markets.master import (
    LISTINGS_DB_NAME,
    REFERENCE_DIR_NAME,
    ListingEvent,
    ListingEventKind,
    ListingMaster,
    ListingObservation,
    ListingStatus,
    SymbolAlias,
    default_listings_path,
)
from crocodile.core.schema.enums import SecurityType

pytestmark = pytest.mark.usefixtures("network_forbidden")

DAY = 86_400_000_000_000
T1 = 1_762_560_000_000_000_000  # 2025-11-08T00:00Z
T2 = T1 + DAY
T3 = T2 + DAY


def _obs(
    *,
    mic: str = "XIST",
    local: str = "THYAO",
    at: int = T1,
    source: str = "tradingview",
    raw: str | None = None,
    **fields: Any,
) -> ListingObservation:
    return ListingObservation(
        source=source,
        mic=mic,
        local_symbol=local,
        symbol_raw=raw if raw is not None else f"{mic}:{local}",
        observed_ns=at,
        **fields,
    )


def test_one_local_symbol_on_two_markets_is_two_listings() -> None:
    with ListingMaster() as master:
        master.upsert(_obs(mic="XSHE", local="000001", raw="SZSE:000001"))
        master.upsert(_obs(mic="XSHG", local="000001", raw="SSE:000001"))
        shenzhen = master.get("000001@XSHE")
        shanghai = master.get("000001@XSHG")
        assert shenzhen is not None and shanghai is not None
        assert shenzhen.mic == "XSHE"
        assert shanghai.mic == "XSHG"
        assert shenzhen.listing_id != shanghai.listing_id
        assert len(master.listings()) == 2


def test_reobserving_a_listing_merges_it_and_widens_its_seen_window() -> None:
    with ListingMaster() as master:
        for at in (T2, T1, T3):
            master.upsert(_obs(at=at))
        listing = master.get("THYAO@XIST")
        assert listing is not None
        assert len(master.listings()) == 1
        assert listing.first_seen_ns == T1
        assert listing.last_seen_ns == T3


def test_a_segment_mic_is_stored_as_its_operating_mic_plus_the_segment() -> None:
    with ListingMaster() as master:
        listing = master.upsert(_obs(mic="XKOS", local="247540", raw="KRX:247540"))
        assert listing.symbol == "247540@XKRX"
        assert listing.mic == "XKRX"
        assert listing.segment_mic == "XKOS"
        by_segment = master.find("XKOS", "247540")
        by_operator = master.find("XKRX", "247540")
        assert by_segment is not None and by_segment.listing_id == listing.listing_id
        assert by_operator is not None and by_operator.listing_id == listing.listing_id

    with ListingMaster() as other:
        again = other.upsert(_obs(mic="XKRX", segment_mic="XKOS", local="247540", raw="K:247540"))
        assert again.symbol == "247540@XKRX"
        assert again.segment_mic == "XKOS"

    with ListingMaster() as third, pytest.raises(ValueError):
        third.upsert(_obs(mic="XIST", segment_mic="XKOS", local="247540"))


def test_a_us_listing_keeps_its_bare_ticker_as_the_canonical_symbol() -> None:
    with ListingMaster() as master:
        listing = master.upsert(_obs(mic="XNAS", local="AAPL", raw="NASDAQ:AAPL"))
        assert listing.symbol == "AAPL"


def test_the_same_canonical_symbol_on_another_market_is_a_listing_conflict() -> None:
    with ListingMaster() as master:
        master.upsert(_obs(mic="XNAS", local="AAPL", raw="NASDAQ:AAPL"))
        with pytest.raises(ListingConflictError) as info:
            master.upsert(_obs(mic="XNYS", local="AAPL", raw="NYSE:AAPL"))
        assert info.value.symbol == "AAPL"
        assert info.value.existing == "AAPL@XNAS"
        assert info.value.incoming == "AAPL@XNYS"
        assert master.find("XNYS", "AAPL") is None
        assert master.resolve_alias("tradingview", "NYSE:AAPL") is None


def test_an_unknown_expired_or_unlisted_market_is_refused() -> None:
    with ListingMaster() as master:
        with pytest.raises(UnknownMarketError):
            master.upsert(_obs(mic="ZZZZ", local="ABC"))
        with pytest.raises(ExpiredMarketError):
            master.upsert(_obs(mic="XTUR", local="ABC"))
        with pytest.raises(UnknownMarketError):
            master.upsert(_obs(mic="OTCM", local="ABC"))


def test_an_invalid_local_symbol_is_refused() -> None:
    with ListingMaster() as master, pytest.raises(InvalidSymbolError):
        master.upsert(_obs(local="THY AO"))


def test_a_newer_observation_with_another_currency_updates_the_listing_and_logs_one_event() -> None:
    with ListingMaster() as master:
        master.upsert(_obs(at=T1, currency="TRY", price_scale=0))
        listing = master.upsert(_obs(at=T2, currency="USD", price_scale=0))
        assert listing.currency == "USD"
        assert master.events("THYAO@XIST") == [
            ListingEvent(
                kind=ListingEventKind.CURRENCY_CHANGE,
                old="TRY/0",
                new="USD/0",
                observed_ns=T2,
                source="tradingview",
            )
        ]


def test_an_older_observation_never_overwrites_a_newer_currency() -> None:
    with ListingMaster() as master:
        master.upsert(_obs(at=T1, currency="TRY", price_scale=0))
        master.upsert(_obs(at=T2, currency="USD", price_scale=0))
        listing = master.upsert(_obs(at=T1, currency="EUR", price_scale=0))
        assert listing.currency == "USD"
        assert len(master.events("THYAO@XIST")) == 1


def test_a_first_currency_fills_a_null_without_an_event() -> None:
    with ListingMaster() as master:
        master.upsert(_obs(at=T1))
        listing = master.upsert(_obs(at=T2, currency="TRY", price_scale=0))
        assert listing.currency == "TRY"
        assert listing.price_scale == 0
        assert master.events("THYAO@XIST") == []


def test_a_currency_needs_its_price_scale_and_an_iso_code() -> None:
    with ListingMaster() as master:
        with pytest.raises(ValueError):
            master.upsert(_obs(currency="TRY"))
        with pytest.raises(ValueError):
            master.upsert(_obs(price_scale=0))
        with pytest.raises(UnknownCurrencyError):
            master.upsert(_obs(currency="GBX", price_scale=0))
        with pytest.raises(ValueError):
            master.upsert(_obs(currency="TRY", price_scale=-1))


def test_the_observing_sources_spelling_and_extra_aliases_resolve_to_the_listing() -> None:
    with ListingMaster() as master:
        master.upsert(
            _obs(
                raw="BIST:THYAO",
                extra_aliases=(("yahoo_chart", "THYAO.IS"),),
            )
        )
        by_tradingview = master.resolve_alias("tradingview", "BIST:THYAO")
        by_yahoo = master.resolve_alias("yahoo_chart", "THYAO.IS")
        assert by_tradingview is not None and by_tradingview.symbol == "THYAO@XIST"
        assert by_yahoo is not None and by_yahoo.symbol == "THYAO@XIST"


def test_a_changed_spelling_closes_the_old_alias_at_the_observation_instant() -> None:
    source = "tradingview"
    with ListingMaster() as master:
        master.upsert(_obs(at=T1, raw="R1"))
        master.upsert(_obs(at=T2, raw="R2"))
        at_first = master.resolve_alias(source, "R1", at_ns=T1 + 1)
        assert at_first is not None and at_first.symbol == "THYAO@XIST"
        assert master.resolve_alias(source, "R1") is None
        assert master.resolve_alias(source, "R2", at_ns=T1 + 1) is None
        current = master.resolve_alias(source, "R2")
        assert current is not None and current.symbol == "THYAO@XIST"

        closed = [alias for alias in master.aliases("THYAO@XIST") if alias.symbol_raw == "R1"]
        assert closed == [
            SymbolAlias(source=source, symbol_raw="R1", valid_from_ns=T1, valid_to_ns=T2)
        ]
        changes = [
            event
            for event in master.events("THYAO@XIST")
            if event.kind is ListingEventKind.ALIAS_CHANGE
        ]
        assert changes == [
            ListingEvent(
                kind=ListingEventKind.ALIAS_CHANGE,
                old="R1",
                new="R2",
                observed_ns=T2,
                source=source,
            )
        ]


def test_a_spelling_reassigned_to_another_listing_keeps_its_history() -> None:
    source = "tradingview"
    with ListingMaster() as master:
        master.upsert(_obs(local="THYAO", at=T1, raw="R"))
        master.upsert(_obs(local="GARAN", at=T2, raw="R"))
        before = master.resolve_alias(source, "R", at_ns=T1 + 1)
        assert before is not None and before.symbol == "THYAO@XIST"
        current = master.resolve_alias(source, "R")
        assert current is not None and current.symbol == "GARAN@XIST"
        assert ListingEvent(
            kind=ListingEventKind.ALIAS_CHANGE,
            old="R",
            new=None,
            observed_ns=T2,
            source=source,
        ) in master.events("THYAO@XIST")


def test_an_alias_observation_older_than_the_open_alias_changes_nothing() -> None:
    source = "tradingview"
    with ListingMaster() as master:
        master.upsert(_obs(local="THYAO", at=T2, raw="R"))
        master.upsert(_obs(local="GARAN", at=T1, raw="R"))
        current = master.resolve_alias(source, "R")
        assert current is not None and current.symbol == "THYAO@XIST"
        assert master.aliases("GARAN@XIST") == []
        assert master.events("GARAN@XIST") == []


def test_one_observation_may_not_name_two_spellings_for_one_source() -> None:
    with ListingMaster() as master, pytest.raises(ValueError):
        master.upsert(_obs(extra_aliases=(("tradingview", "OTHER"),)))


def test_by_isin_returns_every_listing_of_one_instrument() -> None:
    with ListingMaster() as master:
        master.upsert(_obs(mic="XETR", local="ALV", isin="DE0008404005", raw="XETRA:ALV"))
        master.upsert(_obs(mic="XFRA", local="ALV", isin="DE0008404005", raw="FRA:ALV"))
        found = master.by_isin("DE0008404005")
        assert [listing.symbol for listing in found] == ["ALV@XETR", "ALV@XFRA"]
        assert found[0].instrument_id == found[1].instrument_id


def test_a_listing_whose_isin_changes_is_a_listing_conflict() -> None:
    with ListingMaster() as master:
        master.upsert(_obs(isin="TRATHYAO91M5"))
        with pytest.raises(ListingConflictError):
            master.upsert(_obs(isin="US0378331005"))


def test_an_isin_learned_later_relinks_the_listing_to_the_existing_instrument() -> None:
    with ListingMaster() as master:
        master.upsert(_obs(mic="XFRA", local="ALV", raw="FRA:ALV"))
        master.upsert(_obs(mic="XETR", local="ALV", isin="DE0008404005", raw="XETRA:ALV"))
        frankfurt = master.upsert(_obs(mic="XFRA", local="ALV", isin="DE0008404005", raw="FRA:ALV"))
        xetra = master.get("ALV@XETR")
        assert xetra is not None
        assert frankfurt.instrument_id == xetra.instrument_id
        count = master._conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
        assert count == 1


def test_a_malformed_isin_is_refused() -> None:
    with ListingMaster() as master, pytest.raises(ValueError):
        master.upsert(_obs(isin="US0378331006"))


def test_instrument_fields_are_filled_and_never_overwritten() -> None:
    with ListingMaster() as master:
        master.upsert(_obs(at=T1, security_type=SecurityType.UNKNOWN))
        master.upsert(_obs(at=T2, name="Turk Hava Yollari"))
        listing = master.upsert(_obs(at=T3, name="OTHER", security_type=SecurityType.CS))
        assert listing.name == "Turk Hava Yollari"
        assert listing.security_type is SecurityType.CS


def test_issuers_are_created_from_an_lei_and_shared() -> None:
    with ListingMaster() as master:
        master.upsert(
            _obs(
                mic="XETR",
                local="ALV",
                raw="XETRA:ALV",
                issuer_lei="529900K9B0N5BT694847",
                issuer_name="Allianz SE",
                issuer_country="DE",
            )
        )
        master.upsert(
            _obs(mic="XFRA", local="ALV", raw="FRA:ALV", issuer_lei="529900K9B0N5BT694847")
        )
        issuers = master._conn.execute("SELECT COUNT(*) FROM issuers").fetchone()[0]
        assert issuers == 1
        master.upsert(_obs(local="THYAO", raw="XIST:THYAO", issuer_name="Turk Hava Yollari"))
        issuers = master._conn.execute("SELECT COUNT(*) FROM issuers").fetchone()[0]
        assert issuers == 1


def test_mark_unseen_marks_only_what_this_source_saw_before_and_missed_now() -> None:
    with ListingMaster() as master:
        master.upsert(_obs(local="THYAO", at=T1))
        master.upsert(_obs(local="GARAN", at=T1))
        master.upsert(_obs(local="ASELS", at=T1, source="yahoo_chart", raw="ASELS.IS"))
        changed = master.mark_unseen("XIST", "tradingview", ["THYAO"], T2)
        assert [listing.symbol for listing in changed] == ["GARAN@XIST"]
        thyao = master.get("THYAO@XIST")
        asels = master.get("ASELS@XIST")
        garan = master.get("GARAN@XIST")
        assert thyao is not None and thyao.status is ListingStatus.ACTIVE
        assert asels is not None and asels.status is ListingStatus.ACTIVE
        assert garan is not None and garan.status is ListingStatus.UNSEEN
        assert master.events("GARAN@XIST") == [
            ListingEvent(
                kind=ListingEventKind.STATUS_CHANGE,
                old="active",
                new="unseen",
                observed_ns=T2,
                source="tradingview",
            )
        ]


def test_a_reappearing_listing_becomes_active_again_with_an_event() -> None:
    with ListingMaster() as master:
        master.upsert(_obs(local="THYAO", at=T1))
        master.upsert(_obs(local="GARAN", at=T1))
        master.mark_unseen("XIST", "tradingview", ["THYAO"], T2)
        garan = master.upsert(_obs(local="GARAN", at=T3))
        assert garan.status is ListingStatus.ACTIVE
        assert ListingEvent(
            kind=ListingEventKind.STATUS_CHANGE,
            old="unseen",
            new="active",
            observed_ns=T3,
            source="tradingview",
        ) in master.events("GARAN@XIST")


def test_mark_unseen_refuses_an_empty_sweep() -> None:
    with ListingMaster() as master:
        master.upsert(_obs(at=T1))
        with pytest.raises(ValueError):
            master.mark_unseen("XIST", "tradingview", [], T2)


def test_mark_unseen_never_touches_a_delisted_listing_or_one_seen_after_the_sweep() -> None:
    with ListingMaster() as master:
        master.upsert(_obs(local="THYAO", at=T1))
        master.upsert(_obs(local="GARAN", at=T1))
        master.upsert(_obs(local="OLD", at=T1, status=ListingStatus.DELISTED))
        master.upsert(_obs(local="NEW", at=T2))
        changed = master.mark_unseen("XIST", "tradingview", ["THYAO"], T2)
        assert [listing.symbol for listing in changed] == ["GARAN@XIST"]
        old = master.get("OLD@XIST")
        new = master.get("NEW@XIST")
        assert old is not None and old.status is ListingStatus.DELISTED
        assert new is not None and new.status is ListingStatus.ACTIVE


def test_an_observation_may_not_claim_unseen() -> None:
    with ListingMaster() as master, pytest.raises(ValueError):
        master.upsert(_obs(status=ListingStatus.UNSEEN))


def test_listings_filters_by_mic_country_and_status_in_symbol_order() -> None:
    with ListingMaster() as master:
        master.upsert(_obs(mic="XETR", local="ALV", raw="XETRA:ALV"))
        master.upsert(_obs(mic="XFRA", local="ALV", raw="FRA:ALV"))
        master.upsert(_obs(mic="XIST", local="THYAO"))
        german = [listing.symbol for listing in master.listings(country="DE")]
        assert german == ["ALV@XETR", "ALV@XFRA"]
        turkish = [listing.symbol for listing in master.listings(mic="XIST")]
        assert turkish == ["THYAO@XIST"]
        assert master.listings(status=ListingStatus.UNSEEN) == []
        with pytest.raises(ValueError):
            master.listings(country="de")


def test_get_and_find_return_none_for_an_unknown_listing() -> None:
    with ListingMaster() as master:
        assert master.get("NOPE@XIST") is None
        assert master.find("XIST", "NOPE") is None


def test_a_file_database_runs_in_wal_mode_and_records_its_schema_version(tmp_path: Path) -> None:
    with ListingMaster(tmp_path / "listings.sqlite") as master:
        mode = master._conn.execute("PRAGMA journal_mode").fetchone()[0]
        version = master._conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert mode == "wal"
        assert version == 1


def test_a_listing_survives_reopening_the_file(tmp_path: Path) -> None:
    path = tmp_path / "listings.sqlite"
    with ListingMaster(path) as master:
        master.upsert(_obs())
    with ListingMaster(path) as reopened:
        assert reopened.get("THYAO@XIST") is not None


def test_a_database_from_another_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "listings.sqlite"
    with ListingMaster(path) as master:
        master._conn.execute("UPDATE schema_version SET version = 99")
    with pytest.raises(StoreError):
        ListingMaster(path)


def test_the_default_path_is_reference_listings_sqlite_under_the_data_dir(tmp_path: Path) -> None:
    assert REFERENCE_DIR_NAME == "reference"
    assert LISTINGS_DB_NAME == "listings.sqlite"
    assert default_listings_path(Path("d")) == Path("d") / "reference" / "listings.sqlite"
    with ListingMaster.for_data_dir(tmp_path):
        pass
    assert (tmp_path / "reference" / "listings.sqlite").is_file()


def test_the_master_reads_no_clock() -> None:
    source = Path(master_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_calls = {"now", "utcnow", "today", "time", "time_ns", "monotonic"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name != "time" for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module != "time"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_calls


def test_a_read_only_master_reads_without_creating_sidecar_files(tmp_path: Path) -> None:
    path = tmp_path / REFERENCE_DIR_NAME / LISTINGS_DB_NAME
    with ListingMaster(path) as writer:
        writer.upsert(_obs(local="THYAO"))
        writer.upsert(_obs(local="GARAN", raw="XIST:GARAN"))

    def snapshot() -> dict[str, int]:
        return {
            p.relative_to(tmp_path).as_posix(): p.stat().st_mtime_ns for p in tmp_path.rglob("*")
        }

    before = snapshot()
    with ListingMaster(path, readonly=True) as reader:
        assert len(reader.listings()) == 2
    assert snapshot() == before


def test_a_read_only_master_sees_rows_a_live_writer_has_not_checkpointed(tmp_path: Path) -> None:
    path = tmp_path / "listings.sqlite"
    writer = ListingMaster(path)
    try:
        writer.upsert(_obs(local="THYAO"))
        with ListingMaster(path, readonly=True) as reader:
            assert reader.get("THYAO@XIST") is not None
    finally:
        writer.close()


def test_a_read_only_master_refuses_writes_and_never_creates_a_file(tmp_path: Path) -> None:
    path = tmp_path / "listings.sqlite"
    with ListingMaster(path) as writer:
        writer.upsert(_obs())
    with ListingMaster(path, readonly=True) as reader:
        with pytest.raises(StoreError):
            reader.upsert(_obs(local="GARAN", raw="XIST:GARAN"))
        with pytest.raises(StoreError):
            reader.mark_unseen("XIST", "tradingview", ["THYAO"], T2)

    missing_dir = tmp_path / "missing"
    with pytest.raises(StoreError):
        ListingMaster(missing_dir / "listings.sqlite", readonly=True)
    assert not missing_dir.exists()
    with pytest.raises(StoreError):
        ListingMaster(readonly=True)
