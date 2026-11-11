"""Unit tests for the Security Master module."""

from datetime import UTC, datetime

import pytest

from crocodile.core.errors import CrocodileError, StoreError
from crocodile.core.markets.errors import ListingConflictError
from crocodile.core.schema.enums import SecurityType
from crocodile.equity.reference.master import SecurityMaster
from crocodile.equity.reference.models import Security


def test_register_and_get_by_symbol() -> None:
    with SecurityMaster() as master:
        sec = Security(
            symbol="AAPL",
            ticker="AAPL",
            exchange="NASDAQ",
            name="Apple Inc.",
            security_type=SecurityType.CS,
            cik="0000320193",
            figi="BBG000B9Y5M5",
            cusip="037833100",
            isin="US0378331005",
            is_active=True,
        )
        master.register_security(sec)

        retrieved = master.get_by_symbol("AAPL")
        assert retrieved is not None
        assert retrieved.symbol == "AAPL"
        assert retrieved.ticker == "AAPL"
        assert retrieved.exchange == "NASDAQ"
        assert retrieved.name == "Apple Inc."
        assert retrieved.security_type == SecurityType.CS
        assert retrieved.cik == "0000320193"
        assert retrieved.figi == "BBG000B9Y5M5"
        assert retrieved.cusip == "037833100"
        assert retrieved.isin == "US0378331005"
        assert retrieved.is_active is True
        assert isinstance(retrieved.created_at, datetime)
        assert isinstance(retrieved.updated_at, datetime)


def test_conflict_resolution() -> None:
    with SecurityMaster() as master:
        sec = Security(
            symbol="MSFT",
            ticker="MSFT",
            exchange="NASDAQ",
            name="Microsoft Corp",
            security_type=SecurityType.CS,
        )
        master.register_security(sec)

        sec_updated = Security(
            symbol="MSFT",
            ticker="MSFT",
            exchange="NASDAQ",
            name="Microsoft Corporation",
            security_type=SecurityType.CS,
            is_active=False,
        )
        master.register_security(sec_updated)

        retrieved = master.get_by_symbol("MSFT")
        assert retrieved is not None
        assert retrieved.name == "Microsoft Corporation"
        assert retrieved.is_active is False


def test_get_by_various_identifiers() -> None:
    with SecurityMaster() as master:
        sec = Security(
            symbol="GOOGL",
            ticker="GOOGL",
            exchange="NASDAQ",
            cik="0001652044",
            figi="BBG009S3NB18",
            cusip="38259P508",
        )
        master.register_security(sec)

        # FIGI
        retrieved_figi = master.get_by_figi("BBG009S3NB18")
        assert retrieved_figi is not None
        assert retrieved_figi.symbol == "GOOGL"

        assert master.get_by_figi("UNKNOWN") is None

        # CUSIP
        retrieved_cusip = master.get_by_cusip("38259P508")
        assert retrieved_cusip is not None
        assert retrieved_cusip.symbol == "GOOGL"

        assert master.get_by_cusip("UNKNOWN") is None

        # CIK
        retrieved_cik = master.get_by_cik("0001652044")
        assert len(retrieved_cik) == 1
        assert retrieved_cik[0].symbol == "GOOGL"

        assert len(master.get_by_cik("UNKNOWN")) == 0


def test_ticker_mappings_and_resolution() -> None:
    with SecurityMaster() as master:
        sec = Security(
            symbol="META",
            ticker="META",
            exchange="NASDAQ",
        )
        master.register_security(sec)

        # Default mapping check
        assert master.resolve_ticker("META") == "META"

        # Add historical mapping (e.g. FB)
        master.add_ticker_mapping(ticker="FB", symbol="META", exchange="NASDAQ")
        assert master.resolve_ticker("FB") == "META"

        # Check ticker lookup
        securities = master.get_by_ticker("FB")
        assert len(securities) == 1
        assert securities[0].symbol == "META"

        # Check filter by exchange
        assert len(master.get_by_ticker("FB", exchange="NASDAQ")) == 1
        assert len(master.get_by_ticker("FB", exchange="NYSE")) == 0

        # Check get all mappings
        mappings = master.get_ticker_mappings("META")
        assert len(mappings) == 2
        tickers = {m.ticker for m in mappings}
        assert "META" in tickers
        assert "FB" in tickers

        # Remove mapping
        master.remove_ticker_mapping("FB", "META")
        assert master.resolve_ticker("FB") is None


def test_status_management_and_listing() -> None:
    with SecurityMaster() as master:
        sec1 = Security(symbol="AAPL", ticker="AAPL", exchange="NASDAQ", is_active=True)
        sec2 = Security(symbol="TSLA", ticker="TSLA", exchange="NASDAQ", is_active=True)
        master.register_security(sec1)
        master.register_security(sec2)

        # List active
        active = master.list_securities(active_only=True)
        assert len(active) == 2

        # Deactivate TSLA
        master.update_security_status("TSLA", is_active=False)

        retrieved_tsla = master.get_by_symbol("TSLA")
        assert retrieved_tsla is not None
        assert retrieved_tsla.is_active is False

        # List active only
        active_now = master.list_securities(active_only=True)
        assert len(active_now) == 1
        assert active_now[0].symbol == "AAPL"

        # List all
        all_securities = master.list_securities(active_only=False)
        assert len(all_securities) == 2

        # Error on updating non-existent security status
        with pytest.raises(ValueError, match=r"Security with symbol 'UNKNOWN' not found\."):
            master.update_security_status("UNKNOWN", is_active=True)


def test_datetime_preservation() -> None:
    with SecurityMaster() as master:
        dt = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
        sec = Security(
            symbol="NVDA",
            ticker="NVDA",
            exchange="NASDAQ",
            created_at=dt,
            updated_at=dt,
        )
        master.register_security(sec)

        retrieved = master.get_by_symbol("NVDA")
        assert retrieved is not None
        assert retrieved.created_at == dt
        assert retrieved.updated_at == dt


def test_case_insensitive_lookups() -> None:
    """Test case-insensitive lookups on symbols and tickers."""
    with SecurityMaster() as master:
        sec = Security(
            symbol="AAPL",
            ticker="AAPL",
            exchange="NASDAQ",
        )
        master.register_security(sec)

        # Test get_by_symbol case-insensitive
        retrieved = master.get_by_symbol("aapl")
        assert retrieved is not None
        assert retrieved.symbol == "AAPL"

        # Test resolve_ticker case-insensitive
        assert master.resolve_ticker("aapl") == "AAPL"

        # Test get_by_ticker case-insensitive
        secs = master.get_by_ticker("aapl")
        assert len(secs) == 1
        assert secs[0].symbol == "AAPL"


def test_register_security_refuses_a_same_symbol_different_exchange_overwrite() -> None:
    """SecurityMaster is US-scoped and keyed on the ticker: a second exchange is a conflict."""
    with SecurityMaster() as master:
        master.register_security(Security(symbol="AAPL", ticker="AAPL", exchange="NASDAQ"))

        with pytest.raises(ListingConflictError):
            master.register_security(Security(symbol="AAPL", ticker="AAPL", exchange="NYSE"))

        stored = master.get_by_symbol("AAPL")
        assert stored is not None and stored.exchange == "NASDAQ"
        mappings = master.get_ticker_mappings("AAPL")
        assert len(mappings) == 1
        assert mappings[0].exchange == "NASDAQ"


def test_register_security_compares_exchanges_case_insensitively() -> None:
    """The same exchange in another letter case is the same listing and still updates."""
    with SecurityMaster() as master:
        master.register_security(
            Security(symbol="AAPL", ticker="AAPL", exchange="NASDAQ", name="Apple")
        )
        master.register_security(
            Security(symbol="AAPL", ticker="AAPL", exchange="nasdaq", name="Apple Inc.")
        )
        stored = master.get_by_symbol("AAPL")
        assert stored is not None and stored.name == "Apple Inc."


def test_the_security_master_refusal_is_the_listing_conflict_error_of_core_markets() -> None:
    with SecurityMaster() as master:
        master.register_security(Security(symbol="AAPL", ticker="AAPL", exchange="NASDAQ"))
        with pytest.raises(ListingConflictError) as info:
            master.register_security(Security(symbol="AAPL", ticker="AAPL", exchange="NYSE"))
    assert type(info.value) is ListingConflictError
    assert isinstance(info.value, CrocodileError)
    assert isinstance(info.value, StoreError)


def test_instrument_registry_fallback() -> None:
    """Test InstrumentRegistry fallback search using SecurityMaster."""
    from crocodile.equity.reference.registry import InstrumentRegistry

    with SecurityMaster() as master:
        sec = Security(
            symbol="AAPL",
            ticker="AAPL",
            exchange="NASDAQ",
            cik="0000320193",
            figi="BBG000B9Y5M5",
            cusip="037833100",
        )
        master.register_security(sec)

        # Create registry with master
        registry = InstrumentRegistry(security_master=master)

        # Look up symbol not manually registered -> should fallback to master
        inst = registry.by_symbol("AAPL")
        assert inst is not None
        assert inst.symbol == "AAPL"
        assert inst.symbol_raw == "AAPL"
        assert inst.exchange == "NASDAQ"
        assert inst.cik == "0000320193"
        assert inst.figi == "BBG000B9Y5M5"
        assert inst.cusip == "037833100"

        # Look up raw/ticker not manually registered -> should fallback to master
        inst_raw = registry.by_raw("alpaca", "AAPL")
        assert inst_raw is not None
        assert inst_raw.symbol == "AAPL"
        assert inst_raw.source == "alpaca", "who serves the data, not where AAPL lists"
