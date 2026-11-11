"""The M3 reference merge: what each source contributes, who wins, and what the tail says.

Nothing here reaches a network. The three sources are three parse functions over fixture
payloads and two injected clients, which is the shape the module was written for: SEC's index
and Tiingo's archive are bulk files, so their clients are handed in whole, and OpenFIGI is a
batch mapping call, so its client is handed in as an object with one method. A test that
fetched sec.gov would fail on a plane, fail behind a proxy, and go green or red on whatever
NASDAQ listed this morning.
"""

from __future__ import annotations

import logging
from typing import Any

import polars as pl
import pytest

from crocodile.core.config import Settings
from crocodile.core.errors import ConfigError
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.schema.enums import AssetClass, SecurityType
from crocodile.core.schema.provenance import Provenance
from crocodile.equity.providers.openfigi.models import FigiRecord
from crocodile.equity.providers.sec_edgar.client import (
    SecCompanyTicker,
    SecEdgarClient,
    require_user_agent,
)
from crocodile.equity.providers.tiingo.client import TiingoTicker
from crocodile.equity.reference import universe as reference

_AS_OF = 1_704_067_200_000_000_000
"""2024-01-01T00:00:00Z. One instant for every source, which is what makes the merge merge."""


def _sec(ticker: str, cik: int, title: str | None = "A Company Inc.") -> SecCompanyTicker:
    return SecCompanyTicker(cik=cik, ticker=ticker, title=title)


def _tiingo(
    ticker: str,
    exchange: str = "NASDAQ",
    asset_type: str = "Stock",
    currency: str = "USD",
    start: str = "1990-01-02",
) -> TiingoTicker:
    return TiingoTicker(
        ticker=ticker,
        exchange=exchange,
        asset_type=asset_type,
        price_currency=currency,
        start_date=start,
        end_date="2024-01-01",
    )


def _figi(ticker: str, figi: str = "BBG000B9XRY4", **kw: Any) -> FigiRecord:
    return FigiRecord(
        figi=figi,
        ticker=ticker,
        name=kw.get("name", "APPLE INC"),
        exch_code=kw.get("exch_code", "UW"),
        security_type=kw.get("security_type", "Common Stock"),
        composite_figi=kw.get("composite_figi", "BBG000B9XRY4"),
        share_class_figi=kw.get("share_class_figi", "BBG001S5N8V8"),
    )


def _evidence(
    *,
    sec: list[SecCompanyTicker] | None = None,
    tiingo: list[TiingoTicker] | None = None,
) -> reference.ReferenceEvidence:
    tiingo_rows = tiingo if tiingo is not None else []
    return reference.ReferenceEvidence(
        as_of_ns=_AS_OF,
        by_source={
            reference.SOURCE_SEC: reference.instruments_from_sec(sec or [], as_of_ns=_AS_OF),
            reference.SOURCE_TIINGO: reference.instruments_from_tiingo(
                tiingo_rows, as_of_ns=_AS_OF
            ),
        },
        currency={row.ticker.upper(): row.price_currency for row in tiingo_rows},
    )


# ---------------------------------------------------------------------------
# The per-source builders — one registry's own statement, and nothing more
# ---------------------------------------------------------------------------


def test_sec_contributes_a_cik_and_a_name_and_deliberately_no_exchange() -> None:
    """A filer is not a listing, so SEC has nothing to say about where a ticker trades."""
    (built,) = reference.instruments_from_sec([_sec("AAPL", 320193)], as_of_ns=_AS_OF)
    assert built.cik == "0000320193"
    assert built.name == "A Company Inc."
    assert built.exchange is None
    assert built.source == reference.SOURCE_SEC
    assert built.asset_class is AssetClass.EQUITY


def test_tiingo_contributes_the_venue_and_the_type() -> None:
    (built,) = reference.instruments_from_tiingo([_tiingo("AAPL")], as_of_ns=_AS_OF)
    assert built.exchange == "NASDAQ"
    assert built.security_type is SecurityType.CS
    assert built.listing_date == "1990-01-02"
    assert built.cik is None


def test_a_tiingo_row_with_no_exchange_is_dropped() -> None:
    """The one thing this source is for is the venue; a row without one carries nothing."""
    blank = [_tiingo("AAPL", exchange="  ")]
    assert reference.instruments_from_tiingo(blank, as_of_ns=_AS_OF) == []


def test_a_tiingo_asset_type_with_no_member_becomes_unknown_rather_than_a_new_member() -> None:
    """Tiingo's third value is ``Mutual Fund``; the enum is not widened from one vendor."""
    (built,) = reference.instruments_from_tiingo(
        [_tiingo("VFIAX", asset_type="Mutual Fund")], as_of_ns=_AS_OF
    )
    assert built.security_type is SecurityType.UNKNOWN


def test_tiingo_leaves_status_unset_rather_than_guessing_a_delisting() -> None:
    """``endDate`` is a coverage bound; turning it into "delisted" needs an invented cutoff."""
    (built,) = reference.instruments_from_tiingo([_tiingo("AAPL")], as_of_ns=_AS_OF)
    assert built.status is None


def test_openfigi_contributes_the_identifiers_and_takes_only_the_first_match() -> None:
    """A ticker maps to a composite plus one FIGI per venue; the universe is keyed on ticker."""
    (built,) = reference.instruments_from_figi(
        {"AAPL": [_figi("AAPL", "BBG000B9XRY4"), _figi("AAPL", "BBG000BPHFS9")]},
        as_of_ns=_AS_OF,
    )
    assert built.figi == "BBG000B9XRY4"
    assert built.composite_figi == "BBG000B9XRY4"
    assert built.share_class_figi == "BBG001S5N8V8"
    assert built.security_type is SecurityType.CS


def test_a_ticker_openfigi_matched_nothing_for_builds_no_row() -> None:
    """An empty row would be counted as an attestation by the merge."""
    assert reference.instruments_from_figi({"NOPE": []}, as_of_ns=_AS_OF) == []


def test_a_pre_merge_row_claims_native_because_a_registry_really_did_say_it() -> None:
    """This is the one place ``native`` is honest here: it was read, not reconstructed."""
    (built,) = reference.instruments_from_sec([_sec("AAPL", 320193)], as_of_ns=_AS_OF)
    assert built.prov is Provenance.NATIVE
    assert built.prov_basis == "native"


def test_no_source_stamps_a_source_ts_because_none_of_the_three_files_is_dated() -> None:
    rows = (
        reference.instruments_from_sec([_sec("AAPL", 320193)], as_of_ns=_AS_OF)
        + reference.instruments_from_tiingo([_tiingo("AAPL")], as_of_ns=_AS_OF)
        + reference.instruments_from_figi({"AAPL": [_figi("AAPL")]}, as_of_ns=_AS_OF)
    )
    assert [row.source_ts for row in rows] == [None, None, None]
    assert {row.local_ts for row in rows} == {_AS_OF}


# ---------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------


def test_the_merge_takes_each_field_from_whichever_source_has_it() -> None:
    """``fill_nulls`` and not ``priority``: the three sources are complementary.

    Taking the winner's row whole would throw away the CIK on every ticker Tiingo also
    lists, which is most of them — and the CIK is the only thing SEC is in the method for.
    """
    (listing,) = _evidence(sec=[_sec("AAPL", 320193)], tiingo=[_tiingo("AAPL")]).merged()
    assert listing.instrument.cik == "0000320193"  # SEC's, and nobody else's
    assert listing.instrument.exchange == "NASDAQ"  # Tiingo's, and nobody else's
    assert listing.instrument.name == "A Company Inc."  # SEC's, since Tiingo has none
    assert listing.currency == "USD"


def test_tiingo_wins_the_exchange_where_both_it_and_openfigi_name_one() -> None:
    """The priority order in one assertion: a venue name beats a venue code."""
    evidence = _evidence(tiingo=[_tiingo("AAPL", exchange="NASDAQ")]).with_figi(
        {"AAPL": [_figi("AAPL", exch_code="UW")]}
    )
    (listing,) = evidence.merged()
    assert listing.instrument.exchange == "NASDAQ"
    assert listing.instrument.figi == "BBG000B9XRY4"


def test_sec_wins_the_name_over_openfigis_security_description() -> None:
    """The registrant's legal name beats a security description, which is what the field holds.

    This test used to assert the opposite of its own name. It was called
    ``test_sec_still_wins_the_name_over_openfigis_security_description``, its docstring said
    SEC was "first for the one field only it reports well", and its body asserted
    ``name == "APPLE INC"`` — OpenFIGI's — under a comment conceding that OpenFIGI outranked
    SEC. Green, and certifying the inverse of the claim three docstrings in the source make.

    The code moved rather than the name, because the prose was right and the priority tuple
    was wrong: ``Instrument.name`` is documented as the registrant-attested company name, and
    with OpenFIGI ranked above SEC the same ticker carried SEC's legal name out of ``census``
    and OpenFIGI's uppercase description out of ``universe``, decided by whether anyone had
    enriched the row. See :data:`~crocodile.equity.reference.universe.REFERENCE_PRIORITY`.
    """
    evidence = _evidence(
        sec=[_sec("AAPL", 320193, title="Apple Inc.")], tiingo=[_tiingo("AAPL")]
    ).with_figi({"AAPL": [_figi("AAPL", name="APPLE INC")]})
    (listing,) = evidence.merged()
    assert listing.instrument.name == "Apple Inc."
    assert listing.instrument.cik == "0000320193"


def test_openfigis_description_still_names_a_ticker_the_sec_does_not_register() -> None:
    """Demoting OpenFIGI kept its name as the fallback rather than discarding it.

    A foreign issuer or an ETF share class has no SEC registrant row, and Tiingo publishes no
    name at all, so the security description is the only name there is. Asserting it here is
    what stops the priority change from being read as "OpenFIGI's name is not worth having".
    """
    evidence = _evidence(tiingo=[_tiingo("SPY")]).with_figi(
        {"SPY": [_figi("SPY", name="SPDR S&P 500 ETF TRUST")]}
    )
    (listing,) = evidence.merged()
    assert listing.instrument.name == "SPDR S&P 500 ETF TRUST"
    assert listing.instrument.cik is None


def test_the_priority_order_only_decides_the_three_fields_two_sources_both_publish() -> None:
    """The order is an argument about ``name``, ``exchange`` and ``security_type`` only.

    Everything else has exactly one publisher, so ``fill_nulls`` hands it over whatever the
    ranking is — which is why moving SEC above OpenFIGI could change the resolution of
    ``name`` and of nothing else. Pinned because that claim is the whole justification for
    the change, and it would otherwise be a sentence in a docstring nobody re-checks.
    """
    contested = {
        field
        for field in ("name", "exchange", "security_type", "cik", "figi", "listing_date")
        if sum(
            1
            for rows in (
                reference.instruments_from_sec([_sec("AAPL", 320193, title="A")], as_of_ns=_AS_OF),
                reference.instruments_from_tiingo([_tiingo("AAPL")], as_of_ns=_AS_OF),
                reference.instruments_from_figi({"AAPL": [_figi("AAPL")]}, as_of_ns=_AS_OF),
            )
            if getattr(rows[0], field) is not None
        )
        > 1
    }
    assert contested == {"name", "exchange", "security_type"}


def test_the_merged_row_carries_the_merges_own_tail_and_not_the_winners() -> None:
    """``fill_nulls`` only fills nulls, and every ``prov_*`` field has a non-null default.

    Left alone the merged row would say ``prov_basis='native'`` — the claim that a registrar
    published it as it stands. It did not; this engine assembled it.
    """
    (listing,) = _evidence(sec=[_sec("AAPL", 320193)], tiingo=[_tiingo("AAPL")]).merged()
    assert listing.instrument.prov is Provenance.DERIVED
    assert listing.instrument.prov_basis == "reference_merge"
    assert listing.instrument.prov_confidence == pytest.approx(2 / 3)
    assert listing.instrument.prov_inputs == ["instrument"]


def test_confidence_rises_with_the_third_attestation() -> None:
    """The whole point of the fixed denominator: two sources is not a full house."""
    two = _evidence(sec=[_sec("AAPL", 320193)], tiingo=[_tiingo("AAPL")])
    three = two.with_figi({"AAPL": [_figi("AAPL")]})
    assert two.merged()[0].instrument.prov_confidence == pytest.approx(2 / 3)
    assert three.merged()[0].instrument.prov_confidence == pytest.approx(1.0)
    assert three.merged()[0].sources == (
        reference.SOURCE_TIINGO,
        reference.SOURCE_SEC,
        reference.SOURCE_OPENFIGI,
    )


def test_a_ticker_only_one_source_names_is_kept_and_scored_as_one() -> None:
    """Absent attestation is reported, not filtered: the universe is what the sources name."""
    listings = {
        listing.symbol: listing
        for listing in _evidence(sec=[_sec("PRIVATECO", 1)], tiingo=[_tiingo("AAPL")]).merged()
    }
    assert set(listings) == {"AAPL", "PRIVATECO"}
    assert listings["PRIVATECO"].instrument.prov_confidence == pytest.approx(1 / 3)
    assert listings["PRIVATECO"].sources == (reference.SOURCE_SEC,)


def test_enrichment_does_not_double_count_the_rows_it_already_merged() -> None:
    """Why the evidence is kept un-merged rather than re-fed to the resolver.

    Merging the merged output with a fourth list would count the already-merged row as one
    more attestation and report a two-source identity as a three-source one.
    """
    evidence = _evidence(sec=[_sec("AAPL", 320193)], tiingo=[_tiingo("AAPL")])
    twice = evidence.with_figi({"AAPL": [_figi("AAPL")]}).with_figi({"AAPL": [_figi("AAPL")]})
    assert twice.merged()[0].instrument.prov_confidence == pytest.approx(1.0)


def test_the_merged_universe_comes_back_in_ticker_order() -> None:
    listings = _evidence(
        tiingo=[_tiingo("MSFT"), _tiingo("AAPL"), _tiingo("NVDA")]
    ).merged()
    assert [listing.symbol for listing in listings] == ["AAPL", "MSFT", "NVDA"]


def test_restricting_the_evidence_narrows_every_source_at_once() -> None:
    evidence = _evidence(
        sec=[_sec("AAPL", 320193), _sec("MSFT", 789019)],
        tiingo=[_tiingo("AAPL"), _tiingo("MSFT")],
    )
    narrowed = evidence.restricted(["AAPL"])
    assert narrowed.symbols() == {"AAPL"}
    assert [row.symbol for row in narrowed.by_source[reference.SOURCE_SEC]] == ["AAPL"]


# ---------------------------------------------------------------------------
# Filters, kinds and ranking
# ---------------------------------------------------------------------------


def test_filters_are_case_insensitive_and_none_means_any() -> None:
    listings = _evidence(
        tiingo=[
            _tiingo("AAPL", exchange="NASDAQ", asset_type="Stock", currency="USD"),
            _tiingo("SPY", exchange="NYSE ARCA", asset_type="ETF", currency="USD"),
            _tiingo("SHOP", exchange="NASDAQ", asset_type="Stock", currency="CAD"),
        ]
    ).merged()
    assert len(reference.filter_listings(listings)) == 3
    assert [
        listing.symbol for listing in reference.filter_listings(listings, exchange="nasdaq")
    ] == ["AAPL", "SHOP"]
    assert [
        listing.symbol
        for listing in reference.filter_listings(listings, kinds={SecurityType.ETF})
    ] == ["SPY"]
    assert [
        listing.symbol for listing in reference.filter_listings(listings, currency="cad")
    ] == ["SHOP"]


def test_the_exchange_filter_is_exact_rather_than_a_substring() -> None:
    """``search`` on ``markets`` is a lookup aid; this decides which market a request is about."""
    listings = _evidence(tiingo=[_tiingo("AAPL", exchange="NASDAQ")]).merged()
    assert reference.filter_listings(listings, exchange="NAS") == []


@pytest.mark.parametrize("spelling", ["CS", "cs", " Etf ", "unknown"])
def test_parse_kinds_accepts_any_casing_of_a_real_member(spelling: str) -> None:
    assert reference.parse_kinds([spelling])


def test_parse_kinds_rejects_a_typo_rather_than_matching_nothing() -> None:
    """Silently matching nothing would make a typo look like a market with no ETFs."""
    with pytest.raises(ValueError, match="unknown instrument kind"):
        reference.parse_kinds(["perpetual"])


def test_parse_kinds_of_nothing_is_no_filter_rather_than_an_empty_one() -> None:
    assert reference.parse_kinds(()) is None


def test_ranking_puts_the_most_traded_first_and_the_unmeasured_last() -> None:
    """Absent evidence is not evidence of absence; an unranked ticker is kept, at the back."""
    listings = _evidence(tiingo=[_tiingo("AAPL"), _tiingo("MSFT"), _tiingo("ZZZZ")]).merged()
    ranked = reference.rank_listings(listings, {"MSFT": 900.0, "AAPL": 100.0})
    assert [listing.symbol for listing in ranked] == ["MSFT", "AAPL", "ZZZZ"]


def test_ties_break_on_ticker_so_the_order_is_reproducible() -> None:
    listings = _evidence(tiingo=[_tiingo("BBB"), _tiingo("AAA")]).merged()
    ranked = reference.rank_listings(listings, {"AAA": 5.0, "BBB": 5.0})
    assert [listing.symbol for listing in ranked] == ["AAA", "BBB"]


# ---------------------------------------------------------------------------
# The lake-side half of the ranking
# ---------------------------------------------------------------------------


def test_volume_evidence_is_read_through_the_callers_query_and_not_a_catalog() -> None:
    """Taking the bound method is what keeps the read inside the surface's own policy."""
    seen: list[str] = []

    def _query(sql: str) -> pl.DataFrame:
        seen.append(sql)
        return pl.DataFrame({"symbol": ["MSFT", "AAPL"], "mean_volume": [900.0, 100.0]})

    assert reference.volume_by_symbol(_query, channels=["ohlcv", "trade"]) == {
        "MSFT": 900.0,
        "AAPL": 100.0,
    }
    assert seen == [reference.VOLUME_RANK_SQL]


def test_a_lake_that_has_never_held_a_bar_is_a_state_rather_than_an_error() -> None:
    """A ``SELECT`` against a channel with no view raises out of DuckDB; that is not an answer."""

    def _query(sql: str) -> pl.DataFrame:  # pragma: no cover - must not be reached
        raise AssertionError("the channel check should have run first")

    assert reference.volume_by_symbol(_query, channels=["trade"]) == {}


def test_an_empty_bar_table_reaches_the_same_empty_answer() -> None:
    assert (
        reference.volume_by_symbol(
            lambda _: pl.DataFrame({"symbol": [], "mean_volume": []}), channels=["ohlcv"]
        )
        == {}
    )


def test_the_ranking_sql_orders_inside_the_statement_so_a_row_cap_keeps_the_top() -> None:
    """``CapabilityContext.query`` wraps a capped read as ``SELECT * FROM (…) LIMIT n``.

    Over an unordered aggregate that is an arbitrary n symbols wearing the word "top".
    """
    assert "ORDER BY mean_volume DESC" in reference.VOLUME_RANK_SQL
    assert "avg(volume)" in reference.VOLUME_RANK_SQL
    assert "volume > 0" in reference.VOLUME_RANK_SQL


# ---------------------------------------------------------------------------
# Configuration, and the fetchers with their clients handed in
# ---------------------------------------------------------------------------


def test_a_missing_sec_contact_string_is_refused_rather_than_defaulted() -> None:
    """``SecEdgarClient``'s own default is a dead mailbox that satisfies SEC's string check."""
    with pytest.raises(ConfigError, match="CROCODILE_SEC_USER_AGENT"):
        reference.require_sec_user_agent(Settings())


def test_a_configured_sec_contact_string_is_returned_unchanged() -> None:
    agent = "Crocodile/1.0 (ops@example.com)"
    assert reference.require_sec_user_agent(Settings(sec_user_agent=agent)) == agent


@pytest.mark.parametrize("blank", ["   ", "\t", "\n", " \t\n "])
def test_a_whitespace_only_contact_string_is_refused_by_both_entry_points(blank: str) -> None:
    """One contract, one gate — and the weaker of the two copies was the wired one.

    ``require_sec_user_agent`` tested ``if not settings.sec_user_agent`` with no ``.strip()``
    while ``SecEdgarClient.from_settings`` stripped, and nothing routed through
    ``from_settings``. So this value passed configuration validation and the failure moved
    *inside* ``begin()``, which is the one place ``capabilities/ops.py`` says it must not be:
    six requests, 2 + 4 + 8 + 16 + 30 seconds of backoff, sixty seconds to a 403, with
    ``User-Agent: '   '`` actually on the wire. Both entry points are asserted because a
    single-sourced check that only one of them calls is the defect, not the fix.
    """
    settings = Settings(sec_user_agent=blank)
    with pytest.raises(ConfigError, match="CROCODILE_SEC_USER_AGENT"):
        reference.require_sec_user_agent(settings)
    with pytest.raises(ConfigError, match="CROCODILE_SEC_USER_AGENT"):
        SecEdgarClient.from_settings(settings)


@pytest.mark.parametrize("value", [None, "", "   ", "\t"])
def test_the_two_entry_points_refuse_in_the_same_words(value: str | None) -> None:
    """Not "both refuse" — refuse *identically*, which is what one gate means.

    Two functions that agree today are two functions that can disagree tomorrow, and that is
    the whole finding: they already had, on whitespace. Comparing the rendered message is
    the assertion that survives a rewrite of either side, because two independent copies
    cannot stay word-for-word equal by accident.

    ``crocodile.equity.reference.universe`` imports ``SecEdgarClient``, so the check can only
    live on the provider side; the reference module keeps the name its two adapters call and
    the *timing* argument that puts it before anything is built.
    """
    settings = Settings(sec_user_agent=value)
    with pytest.raises(ConfigError) as from_reference:
        reference.require_sec_user_agent(settings)
    with pytest.raises(ConfigError) as from_provider:
        require_user_agent(settings)
    with pytest.raises(ConfigError) as from_client:
        SecEdgarClient.from_settings(settings)
    assert str(from_reference.value) == str(from_provider.value) == str(from_client.value)


def test_a_padded_contact_string_reaches_the_wire_trimmed() -> None:
    """A header of ``' Acme ops@acme.example '`` is not the string SEC was told about."""
    agent = "Acme/1.0 (ops@acme.example)"
    settings = Settings(sec_user_agent=f"  {agent}  ")
    assert reference.require_sec_user_agent(settings) == agent
    assert SecEdgarClient.from_settings(settings).user_agent == agent


class _FakeSec:
    """SEC's half of the bulk fetch: one method, one payload, no session."""

    def __init__(self, rows: list[SecCompanyTicker]) -> None:
        self.rows = rows
        self.closed = False

    async def fetch_company_tickers(self) -> list[SecCompanyTicker]:
        return self.rows

    async def close(self) -> None:  # pragma: no cover - the caller owns what it opened
        self.closed = True


class _FakeTiingo:
    def __init__(self, rows: list[TiingoTicker]) -> None:
        self.rows = rows
        self.closed = False

    async def download_supported_tickers(self) -> list[TiingoTicker]:
        return self.rows

    async def close(self) -> None:  # pragma: no cover
        self.closed = True


async def test_the_bulk_fetch_merges_two_injected_clients_into_one_universe() -> None:
    sec = _FakeSec([_sec("AAPL", 320193)])
    tiingo = _FakeTiingo([_tiingo("AAPL"), _tiingo("SPY", asset_type="ETF")])
    evidence = await reference.fetch_bulk_evidence(as_of_ns=_AS_OF, sec=sec, tiingo=tiingo)  # type: ignore[arg-type]

    assert evidence.as_of_ns == _AS_OF
    assert evidence.symbols() == {"AAPL", "SPY"}
    assert evidence.currency == {"AAPL": "USD", "SPY": "USD"}
    assert not sec.closed and not tiingo.closed, "the caller owns what it opened"


async def test_the_bulk_fetch_asks_no_one_for_a_figi() -> None:
    """OpenFIGI is per-symbol; running it over the universe would be a multi-hour job."""
    evidence = await reference.fetch_bulk_evidence(
        as_of_ns=_AS_OF,
        sec=_FakeSec([_sec("AAPL", 320193)]),  # type: ignore[arg-type]
        tiingo=_FakeTiingo([_tiingo("AAPL")]),  # type: ignore[arg-type]
    )
    assert reference.SOURCE_OPENFIGI not in evidence.by_source
    assert evidence.merged()[0].instrument.figi is None


class _FakeFigi:
    """OpenFIGI's half: ``map_jobs`` in, one result list per job out, in job order."""

    def __init__(self, by_ticker: dict[str, list[FigiRecord]]) -> None:
        self.by_ticker = by_ticker
        self.jobs: list[str] = []

    async def map_jobs(self, jobs: list[Any]) -> list[list[FigiRecord]]:
        self.jobs = [job.id_value for job in jobs]
        return [self.by_ticker.get(job.id_value, []) for job in jobs]

    async def close(self) -> None:  # pragma: no cover
        return None


async def test_figi_lookup_returns_a_mapping_so_a_miss_is_an_absent_key() -> None:
    """An empty row would be counted as an attestation; an absent key cannot be."""
    client = _FakeFigi({"AAPL": [_figi("AAPL")]})
    matches = await reference.fetch_figi(["AAPL", "NOPE"], client=client)  # type: ignore[arg-type]
    assert client.jobs == ["AAPL", "NOPE"]
    assert set(matches) == {"AAPL"}


async def test_figi_lookup_of_nothing_asks_nothing() -> None:
    assert await reference.fetch_figi([]) == {}


def test_a_fourth_source_is_not_silently_dropped_from_the_merge() -> None:
    """Iterating the priority list alone would make an unranked source's rows vanish.

    Silent absence is the failure the whole merge exists to end, so the rows are walked and
    the fourth attestation reaches ``reference_merge`` — whose denominator is three — and
    raises. Loud is the right answer: a fourth registry means the formula's argument no
    longer holds, and somebody has to say what the denominator became.
    """
    evidence = _evidence(sec=[_sec("AAPL", 320193)], tiingo=[_tiingo("AAPL")]).with_figi(
        {"AAPL": [_figi("AAPL")]}
    )
    fourth = dict(evidence.by_source)
    fourth["some_new_registry"] = reference.instruments_from_sec(
        [_sec("AAPL", 320193)], as_of_ns=_AS_OF
    )
    with pytest.raises(Exception, match="n_sources"):
        reference.ReferenceEvidence(
            as_of_ns=_AS_OF, by_source=fourth, currency=evidence.currency
        ).merged()


_NON_US = ("000001", "SHE", "CNY")


def test_tiingo_rows_on_non_us_exchange_labels_are_skipped_and_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-US label cannot enter the US-scoped universe, and the skip is logged per label."""
    caplog.set_level(logging.INFO, logger="crocodile.equity.reference.universe")
    rows = [
        _tiingo("AAPL"),
        _tiingo("000001", exchange="SHE", currency="CNY"),
        _tiingo("000002", exchange="SHE", currency="CNY"),
        _tiingo("600519", exchange="SHG", currency="CNY"),
    ]
    built = reference.instruments_from_tiingo(rows, as_of_ns=_AS_OF)
    assert [instrument.symbol for instrument in built] == ["AAPL"]
    assert "skipped 3 row(s)" in caplog.text
    assert "SHE=2" in caplog.text
    assert "SHG=1" in caplog.text


def test_no_skip_line_is_logged_when_every_label_is_us(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="crocodile.equity.reference.universe")
    built = reference.instruments_from_tiingo(
        [_tiingo("AAPL"), _tiingo("SPY", exchange="NYSE ARCA", asset_type="ETF")],
        as_of_ns=_AS_OF,
    )
    assert len(built) == 2
    assert "skipped" not in caplog.text


def test_every_tiingo_us_label_maps_to_a_us_market_or_to_no_mic() -> None:
    registry = ExchangeRegistry()
    for key, value in reference.TIINGO_US_EXCHANGES.items():
        assert key == key.strip().upper()
        if value is not None:
            assert registry.get(value).country == "US"


def test_the_code_derived_us_labels_are_in_the_table() -> None:
    assert {"NASDAQ", "NYSE", "NYSE ARCA", "NYSE MKT", "BATS"} <= set(
        reference.TIINGO_US_EXCHANGES
    )


async def test_a_skipped_tiingo_row_contributes_no_currency() -> None:
    evidence = await reference.fetch_bulk_evidence(
        as_of_ns=_AS_OF,
        sec=_FakeSec([_sec("AAPL", 320193)]),  # type: ignore[arg-type]
        tiingo=_FakeTiingo(  # type: ignore[arg-type]
            [_tiingo("AAPL"), _tiingo(_NON_US[0], exchange=_NON_US[1], currency=_NON_US[2])]
        ),
    )
    assert evidence.currency == {"AAPL": "USD"}
    assert "000001" not in evidence.currency
