"""The four sources that joined the provider registry, and the refusals they carry.

The end-to-end proof that each of them fills its channel lives in
``tests/equity/test_shipped_writer_end_to_end.py``. What is tested here is the other half of
a connector's contract: the things it declines to do, and the messages it declines with. A
provider that answers an unservable request with an empty iterator is the defect this whole
change is about, one layer down — so each refusal below is a raise, and each raise names the
verb or the source that does work.
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any

import pytest

from crocodile.core.config import Settings
from crocodile.core.errors import ConfigError
from crocodile.core.schema.records import MacroSeries
from crocodile.core.sink.base import Sink
from crocodile.equity.providers.factory import make_provider
from crocodile.equity.providers.treasury.client import TreasuryYieldClient
from crocodile.equity.providers.treasury.connector import TreasuryProvider
from crocodile.equity.providers.yahoo.connector import YahooProvider
from crocodile.equity.reference.registry import InstrumentRegistry

_CSV = (
    pathlib.Path(__file__).parent / "fixtures" / "treasury_par_yield_2024.csv"
).read_text()

_2024_START = 1_704_067_200_000_000_000
_2024_END = 1_735_603_200_000_000_000


class _NullSink(Sink):
    """A sink that keeps what it is given. These tests are about refusals, not about rows."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    async def put(self, record: Any) -> None:
        self.records.append(record)

    async def flush(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _FixtureTreasury(TreasuryYieldClient):
    def __init__(self) -> None:
        super().__init__()
        self.years: list[int] = []

    async def fetch_par_yield_csv(self, year: int) -> str:
        self.years.append(year)
        return _CSV if year == 2024 else "Date\n"


def _treasury(symbols: list[str], client: TreasuryYieldClient | None = None) -> TreasuryProvider:
    provider = make_provider(
        provider="treasury",
        symbols=symbols,
        channels=["macro_series"],
        out=_NullSink(),
        registry=InstrumentRegistry(),
        client=client or _FixtureTreasury(),
    )
    assert isinstance(provider, TreasuryProvider)
    return provider


async def _drain(provider: Any, channel: str, symbol: str) -> list[Any]:
    return [
        record
        async for record in provider.backfill(channel, symbol, _2024_START, _2024_END)
    ]


def test_the_whole_curve_is_the_ordinary_request() -> None:
    """The unit Treasury publishes is a curve, and the tenor set is read from the file.

    A caller cannot enumerate next year's tenors in advance — that is the point of deriving
    them from the header — so requiring them to name each one would mean silently missing
    whichever one Treasury adds next.
    """
    provider = _treasury(["*"])
    records = asyncio.run(_drain(provider, "macro_series", "*"))
    assert records
    assert all(isinstance(record, MacroSeries) for record in records)
    assert len({record.symbol for record in records}) > 1


@pytest.mark.parametrize("spelling", ["treasury:UST10Y", "UST10Y", "10 Yr"])
def test_a_single_tenor_resolves_under_all_three_spellings_a_user_could_have_read(
    spelling: str,
) -> None:
    """Canonical, bare and the file's own header — each is something this codebase printed."""
    provider = _treasury([spelling])
    records = asyncio.run(_drain(provider, "macro_series", spelling))
    assert records
    assert {record.symbol for record in records} == {"treasury:UST10Y"}


def test_one_document_is_fetched_once_however_many_tenors_are_asked_for() -> None:
    """``backfill`` is per-symbol and the endpoint is per-year; the cache reconciles them."""
    client = _FixtureTreasury()
    provider = _treasury(["treasury:UST1M", "treasury:UST10Y"], client=client)
    for symbol in ("treasury:UST1M", "treasury:UST10Y"):
        assert asyncio.run(_drain(provider, "macro_series", symbol))
    assert client.years == [2024]


def test_the_treasury_provider_refuses_a_channel_it_does_not_publish() -> None:
    """A refusal rather than an empty iterator, which is the distinction being defended."""
    provider = _treasury(["*"])
    with pytest.raises(ValueError, match="macro_series"):
        asyncio.run(_drain(provider, "ohlcv", "*"))


def test_a_provider_refuses_a_construction_whose_every_channel_is_unservable() -> None:
    """Before the session opens, so the CLI reports it instead of polling forever."""
    with pytest.raises(ValueError, match="no supported channels"):
        make_provider(
            provider="treasury",
            symbols=["*"],
            channels=["trade"],
            out=_NullSink(),
            registry=InstrumentRegistry(),
            client=_FixtureTreasury(),
        )


def test_yahoo_refuses_to_backfill_a_chain_it_keeps_no_history_of() -> None:
    """The one channel here that is a snapshot rather than a series.

    Answering a 2023 range with today's chain would put a snapshot taken now into a
    partition labelled then, which is the confusion ``local_ts`` and ``source_ts`` exist to
    prevent. The message names ``collect``, which is the verb that does work.
    """
    provider = make_provider(
        provider="yahoo",
        symbols=["AAPL"],
        channels=["options_chain"],
        out=_NullSink(),
        registry=InstrumentRegistry(),
        client=object(),
    )
    assert isinstance(provider, YahooProvider)
    with pytest.raises(ValueError, match="collect"):
        asyncio.run(_drain(provider, "options_chain", "AAPL"))


def test_yahoo_refuses_to_poll_a_channel_that_is_finished_history() -> None:
    """A poll loop over yesterday's closed bar spends a rate limit on nothing."""
    provider = make_provider(
        provider="yahoo",
        symbols=["AAPL"],
        channels=["ohlcv"],
        out=_NullSink(),
        registry=InstrumentRegistry(),
        client=object(),
    )
    with pytest.raises(NotImplementedError, match="backfill"):
        asyncio.run(provider.run())


def test_tiingo_says_why_it_will_not_serve_an_option_chain() -> None:
    """The one genuinely paid gap in this batch, and it blocks nothing.

    ``TiingoClient.fetch_option_chain`` raises ``NotImplementedError`` because the data is a
    paid add-on. ``options_chain`` is served keylessly by ``yahoo``, so the entry is a
    recorded decision rather than a missing capability — which is exactly what
    :attr:`~crocodile.equity.providers.base.Provider.unservable_channels` is for.
    """
    from crocodile.equity.providers.tiingo.connector import TiingoProvider

    reason = TiingoProvider.unservable_channels["options_chain"]
    assert "paid" in reason and "yahoo" in reason


def test_tiingo_refuses_to_start_without_the_key_it_cannot_work_without() -> None:
    """No keyless tier exists, so an absent token is no answer rather than a slower one."""
    with pytest.raises(ConfigError, match="CROCODILE_TIINGO_API_KEY"):
        make_provider(
            provider="tiingo",
            symbols=["AAPL"],
            channels=["ohlcv"],
            out=_NullSink(),
            registry=InstrumentRegistry(),
            settings=Settings(tiingo_api_key=None),
        )


def test_the_factory_hands_settings_only_to_the_connectors_that_asked_for_them() -> None:
    """Opt-in, so a connector that needs nothing does not carry a parameter it ignores.

    ``stooq`` reads ``os.environ`` in its own constructor and would raise ``TypeError`` on an
    unexpected ``settings=``; that it does not is the whole behaviour under test.
    """
    from crocodile.equity.providers.factory import _REGISTRY

    # yahoo_chart asks for Settings because the sanctions opt-in (spec 5) is read from them.
    assert {name for name, cls in _REGISTRY.items() if cls.wants_settings} == {
        "sec_edgar",
        "tiingo",
        "yahoo_chart",
    }
    provider = make_provider(
        provider="stooq",
        symbols=["AAPL"],
        channels=["ohlcv"],
        out=_NullSink(),
        registry=InstrumentRegistry(),
        settings=Settings(),
    )
    assert provider.name == "stooq"
