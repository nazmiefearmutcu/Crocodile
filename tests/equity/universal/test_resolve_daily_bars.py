"""Read-time federation of daily bars: one bar per session date, from the best-ranked source.

Every test writes its bars into a ``tmp_path`` lake through ``ParquetSink`` and reads them back
through ``Catalog``. The order is passed explicitly, so these tests do not depend on the packaged
table. Offline (``network_forbidden``).
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from crocodile.core.schema.enums import AssetClass
from crocodile.core.schema.records import OHLCV, Record
from crocodile.core.store.catalog import Catalog
from crocodile.core.store.parquet_sink import ParquetSink
from crocodile.equity.universal.order import (
    NS_PER_DAY,
    OrderRow,
    SourceOrder,
    resolve_daily_bars,
)

pytestmark = pytest.mark.usefixtures("network_forbidden")

_SYMBOL = "THYAO@XIST"
_CAPTURE = 1_720_000_000_000_000_000  # a fixed capture instant, far from every session date
_ORDER = SourceOrder([OrderRow("XIST", "ohlcv", ("yahoo_chart", "tradingview"))])


def _midnight_ns(day: date) -> int:
    return (day - date(1970, 1, 1)).days * NS_PER_DAY


def _bar(
    source: str,
    symbol: str,
    day: date,
    close: float,
    *,
    local_ts: int = _CAPTURE,
    interval: str = "1d",
    currency: str = "TRY",
    price_scale: int = 0,
) -> OHLCV:
    return OHLCV(
        source=source,
        symbol=symbol,
        symbol_raw=symbol,
        local_ts=local_ts,
        asset_class=AssetClass.EQUITY,
        source_ts=_midnight_ns(day),
        interval=interval,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1.0,
        currency=currency,
        price_scale=price_scale,
    )


def _lake(root: Path, records: list[Record]) -> Catalog:
    async def _write() -> None:
        sink = ParquetSink(root, max_buffer_rows=10_000, flush_interval_seconds=9999)
        for record in records:
            await sink.put(record)
        await sink.flush()
        await sink.close()

    asyncio.run(_write())
    return Catalog(root)


def _closes(frame: pl.DataFrame) -> list[float]:
    return frame["close"].to_list()


def _sources(frame: pl.DataFrame) -> list[str]:
    return frame["source"].to_list()


D4, D5, D6, D7 = date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6), date(2024, 3, 7)


def test_each_session_date_is_answered_by_the_highest_ranked_source_that_has_it(
    tmp_path: Path,
) -> None:
    catalog = _lake(
        tmp_path,
        [
            _bar("yahoo_chart", _SYMBOL, D5, 10.0),
            _bar("yahoo_chart", _SYMBOL, D6, 11.0),
            _bar("tradingview", _SYMBOL, D5, 20.0),
            _bar("tradingview", _SYMBOL, D6, 21.0),
        ],
    )
    frame = resolve_daily_bars(_SYMBOL, D5, D6, catalog=catalog, order=_ORDER)
    assert _closes(frame) == [10.0, 11.0]
    assert _sources(frame) == ["yahoo_chart", "yahoo_chart"]


def test_a_date_the_higher_source_lacks_falls_back_to_the_next_source(tmp_path: Path) -> None:
    catalog = _lake(
        tmp_path,
        [
            _bar("yahoo_chart", _SYMBOL, D5, 10.0),
            _bar("yahoo_chart", _SYMBOL, D6, 11.0),
            _bar("tradingview", _SYMBOL, D7, 30.0),
        ],
    )
    frame = resolve_daily_bars(_SYMBOL, D5, D7, catalog=catalog, order=_ORDER)
    assert _closes(frame) == [10.0, 11.0, 30.0]
    assert _sources(frame) == ["yahoo_chart", "yahoo_chart", "tradingview"]
    assert frame["source_ts"].n_unique() == 3


def test_a_source_outside_the_declared_order_ranks_last_and_alphabetically(
    tmp_path: Path,
) -> None:
    catalog = _lake(
        tmp_path,
        [
            _bar("yahoo_chart", _SYMBOL, D5, 10.0),
            # Neither ranked source has 03-06; two unranked sources do, and "alpha" sorts first.
            _bar("zeta_src", _SYMBOL, D6, 61.0),
            _bar("alpha_src", _SYMBOL, D6, 60.0),
            _bar("zeta_src", _SYMBOL, D7, 71.0),
        ],
    )
    frame = resolve_daily_bars(_SYMBOL, D5, D7, catalog=catalog, order=_ORDER)
    assert _closes(frame) == [10.0, 60.0, 71.0]
    assert _sources(frame) == ["yahoo_chart", "alpha_src", "zeta_src"]


def test_explicit_sources_answer_alone_and_in_their_own_order(tmp_path: Path) -> None:
    catalog = _lake(
        tmp_path,
        [
            _bar("yahoo_chart", _SYMBOL, D5, 10.0),
            _bar("tradingview", _SYMBOL, D5, 20.0),
        ],
    )
    frame = resolve_daily_bars(
        _SYMBOL, D5, D5, catalog=catalog, order=_ORDER, sources=("tradingview",)
    )
    assert _closes(frame) == [20.0]
    assert _sources(frame) == ["tradingview"]


def test_repeated_captures_from_one_source_keep_the_latest_capture(tmp_path: Path) -> None:
    catalog = _lake(
        tmp_path,
        [
            _bar("yahoo_chart", _SYMBOL, D5, 10.0, local_ts=_CAPTURE + 1),
            _bar("yahoo_chart", _SYMBOL, D5, 12.0, local_ts=_CAPTURE + 2),
        ],
    )
    frame = resolve_daily_bars(_SYMBOL, D5, D5, catalog=catalog, order=_ORDER)
    assert _closes(frame) == [12.0]
    assert frame["local_ts"].to_list() == [_CAPTURE + 2]


def test_intraday_bars_are_never_mistaken_for_daily_bars(tmp_path: Path) -> None:
    catalog = _lake(
        tmp_path,
        [
            _bar("yahoo_chart", _SYMBOL, D5, 10.0),
            _bar("yahoo_chart", _SYMBOL, D5, 99.0, interval="1m"),
        ],
    )
    frame = resolve_daily_bars(_SYMBOL, D5, D5, catalog=catalog, order=_ORDER)
    assert _closes(frame) == [10.0]


def test_only_the_requested_session_dates_are_returned(tmp_path: Path) -> None:
    catalog = _lake(
        tmp_path,
        [
            _bar("yahoo_chart", _SYMBOL, D4, 9.0),
            _bar("yahoo_chart", _SYMBOL, D5, 10.0),
            _bar("yahoo_chart", _SYMBOL, D6, 11.0),
            _bar("yahoo_chart", _SYMBOL, D7, 12.0),
        ],
    )
    frame = resolve_daily_bars(_SYMBOL, D5, D6, catalog=catalog, order=_ORDER)
    assert _closes(frame) == [10.0, 11.0]


def test_rows_keep_their_own_currency_and_price_scale(tmp_path: Path) -> None:
    catalog = _lake(
        tmp_path,
        [
            _bar("yahoo_chart", _SYMBOL, D5, 1234.5, currency="TRY", price_scale=0),
            _bar("yahoo_chart", _SYMBOL, D6, 98765.0, currency="TRY", price_scale=2),
        ],
    )
    frame = resolve_daily_bars(_SYMBOL, D5, D6, catalog=catalog, order=_ORDER)
    assert _closes(frame) == [1234.5, 98765.0]
    assert frame["price_scale"].to_list() == [0, 2]
    assert frame["currency"].to_list() == ["TRY", "TRY"]


def test_a_lake_without_an_ohlcv_channel_answers_an_empty_frame(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "no_such_lake")
    frame = resolve_daily_bars(_SYMBOL, D5, D6, catalog=catalog, order=_ORDER)
    assert frame.height == 0


def test_a_bare_symbol_without_a_mic_is_refused(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "no_such_lake")
    with pytest.raises(ValueError, match="MIC"):
        resolve_daily_bars("AAPL", D5, D6, catalog=catalog, order=_ORDER)


def test_an_inverted_date_range_is_refused(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "no_such_lake")
    with pytest.raises(ValueError):
        resolve_daily_bars(_SYMBOL, D6, D5, catalog=catalog, order=_ORDER)


def test_the_answer_does_not_depend_on_write_order(tmp_path: Path) -> None:
    bars = [
        _bar("yahoo_chart", _SYMBOL, D5, 10.0),
        _bar("tradingview", _SYMBOL, D5, 20.0),
        _bar("zeta_src", _SYMBOL, D6, 61.0),
        _bar("alpha_src", _SYMBOL, D6, 60.0),
        _bar("yahoo_chart", _SYMBOL, D7, 12.0, local_ts=_CAPTURE + 5),
        _bar("yahoo_chart", _SYMBOL, D7, 13.0, local_ts=_CAPTURE + 9),
    ]
    forward = _lake(tmp_path / "forward", list(bars))
    backward = _lake(tmp_path / "backward", list(reversed(bars)))
    first = resolve_daily_bars(_SYMBOL, D5, D7, catalog=forward, order=_ORDER)
    second = resolve_daily_bars(_SYMBOL, D5, D7, catalog=backward, order=_ORDER)
    assert first.equals(second)
    assert _closes(first) == [10.0, 60.0, 13.0]
