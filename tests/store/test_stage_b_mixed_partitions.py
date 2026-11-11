"""Stage B fields in a lake that holds both 0.3.x parts and post-widening parts.

The ruling is written in full in the module docstring of
:mod:`crocodile.core.store.parquet_sink` under "Nullable widening of the canonical family". In
short: a trailing nullable record field is a nullable column in the canonical family's file
schema, and a part written before the widening simply lacks that column. The catalog reads
every group with ``union_by_name`` and the compactor concatenates ``diagonal_relaxed``, so one
query over an old part and a new part returns NULL for the old rows. ``from_row`` reads the same
absence as the field's ``None`` default. The two legacy family tables stay frozen. SQL naming a
Stage-B column over a lake with no post-widening part has no such column in any file and fails
with a binder error, which these tests pin rather than hide.
"""

from __future__ import annotations

import pathlib
from typing import Any

import duckdb
import polars as pl
import pytest

from crocodile.core.schema.enums import (
    AssetClass,
    CorpActionType,
    PriceAdjustment,
    SecurityType,
    Side,
)
from crocodile.core.schema.records import (
    OHLCV,
    CorporateAction,
    Instrument,
    Quote,
    Record,
    Trade,
)
from crocodile.core.store.catalog import Catalog
from crocodile.core.store.compactor import ParquetCompactor
from crocodile.core.store.parquet_sink import (
    _CRYPTO_CHANNEL_EXTRA,
    _CRYPTO_COMMON_FIELDS,
    _EQUITY_CHANNEL_EXTRA,
    _EQUITY_COMMON_FIELDS,
    ParquetSink,
    _channel_schema,
)
from crocodile.core.store.rows import FAMILY_CANONICAL, from_row, to_row

_TS = 1_793_577_600_000_000_000  # 2026-11-02T00:00:00Z; old row at _TS, new row at _TS + 1

_STAGE_B_COLUMNS: dict[str, dict[str, Any]] = {
    "trade": {"currency": pl.Utf8, "price_scale": pl.Int64},
    "quote": {"currency": pl.Utf8, "price_scale": pl.Int64},
    "ohlcv": {"currency": pl.Utf8, "price_scale": pl.Int64, "adjustment": pl.Utf8},
    "corp_action": {"currency": pl.Utf8, "price_scale": pl.Int64},
    "instrument": {
        "mic": pl.Utf8,
        "segment_mic": pl.Utf8,
        "isin": pl.Utf8,
        "country": pl.Utf8,
        "currency": pl.Utf8,
        "price_scale": pl.Int64,
    },
}

_NEW_VALUES: dict[str, dict[str, Any]] = {
    "trade": {"currency": "GBP", "price_scale": 2},
    "quote": {"currency": "GBP", "price_scale": 2},
    "ohlcv": {"currency": "TRY", "price_scale": 0, "adjustment": PriceAdjustment.SPLIT},
    "corp_action": {"currency": "TRY", "price_scale": 0},
    "instrument": {
        "mic": "XNAS",
        "segment_mic": "XNGS",
        "isin": "US0378331005",
        "country": "US",
        "currency": "USD",
        "price_scale": 0,
    },
}

_SYMBOL: dict[str, str] = {
    "trade": "VOD@XLON",
    "quote": "VOD@XLON",
    "ohlcv": "THYAO@XIST",
    "corp_action": "THYAO@XIST",
    "instrument": "AAPL",
}

_CHANNELS = sorted(_STAGE_B_COLUMNS)


def _record(channel: str, local_ts: int, **stage_b: Any) -> Record:
    """One equity record for ``channel``, with ``stage_b`` as its Stage-B fields."""
    symbol = _SYMBOL[channel]
    header: dict[str, Any] = {
        "source": "stageb",
        "symbol": symbol,
        "symbol_raw": symbol.split("@")[0],
        "source_ts": local_ts,
        "local_ts": local_ts,
        "asset_class": AssetClass.EQUITY,
    }
    if channel == "trade":
        return Trade(**header, id="t1", price=100.0, amount=1.0, side=Side.UNKNOWN, **stage_b)
    if channel == "quote":
        return Quote(**header, bid_px=99.5, bid_sz=1.0, ask_px=100.5, ask_sz=2.0, **stage_b)
    if channel == "ohlcv":
        return OHLCV(
            **header,
            interval="1d",
            open=10.0,
            high=12.0,
            low=9.0,
            close=11.0,
            volume=1000.0,
            **stage_b,
        )
    if channel == "corp_action":
        return CorporateAction(
            **header,
            ex_date="2026-11-02",
            type=CorpActionType.DIVIDEND_CASH,
            value=3.5,
            **stage_b,
        )
    return Instrument(**header, name="Apple Inc.", security_type=SecurityType.CS, **stage_b)


def _write_pre_stage_b_part(lake: pathlib.Path, record: Record) -> pathlib.Path:
    """Write one canonical part the way 0.3.x wrote it: no Stage-B columns at all."""
    row = to_row(record)
    channel = row["channel"]
    schema = {
        name: dtype
        for name, dtype in _channel_schema(channel, FAMILY_CANONICAL).items()
        if name not in _STAGE_B_COLUMNS[channel]
    }
    part_dir = (
        lake
        / f"source={row['source']}"
        / f"channel={channel}"
        / f"date={row['date']}"
        / f"bucket={row['bucket']}"
    )
    part_dir.mkdir(parents=True, exist_ok=True)
    path = part_dir / "part-pre-stage-b.parquet"  # must match the catalog glob part-*.parquet
    pl.DataFrame([{n: row.get(n) for n in schema}], schema=schema).write_parquet(path)
    return path


def _parts(lake: pathlib.Path) -> list[pathlib.Path]:
    return sorted(lake.rglob("part-*.parquet"))


def _expected_new(channel: str) -> dict[str, Any]:
    """The new values as the SQL reader returns them: enum members as their strings."""
    return {
        name: value.value if isinstance(value, PriceAdjustment) else value
        for name, value in _NEW_VALUES[channel].items()
    }


@pytest.mark.parametrize("channel", _CHANNELS)
def test_the_canonical_family_declares_every_stage_b_column_with_its_dtype(channel: str) -> None:
    schema = _channel_schema(channel, FAMILY_CANONICAL)
    for name, dtype in _STAGE_B_COLUMNS[channel].items():
        assert schema[name] == dtype, (channel, name)


def test_the_frozen_legacy_family_tables_gain_no_stage_b_column() -> None:
    stage_b_names = {name for cols in _STAGE_B_COLUMNS.values() for name in cols}
    legacy_tables: list[Any] = [
        *_CRYPTO_CHANNEL_EXTRA.values(),
        *_EQUITY_CHANNEL_EXTRA.values(),
        _CRYPTO_COMMON_FIELDS,
        _EQUITY_COMMON_FIELDS,
    ]
    for table in legacy_tables:
        assert not stage_b_names & set(table)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", _CHANNELS)
async def test_an_old_part_and_a_new_part_read_back_through_one_catalog_query(
    channel: str, tmp_path: pathlib.Path
) -> None:
    old_path = _write_pre_stage_b_part(tmp_path, _record(channel, _TS))

    sink = ParquetSink(data_dir=tmp_path, max_buffer_rows=1000, flush_interval_seconds=9999)
    await sink.put(_record(channel, _TS + 1, **_NEW_VALUES[channel]))
    await sink.flush()

    new_paths = [p for p in _parts(tmp_path) if p != old_path]
    assert len(new_paths) == 1
    assert new_paths[0].parent == old_path.parent  # same bucket directory: a mixed partition
    assert not set(_STAGE_B_COLUMNS[channel]) & set(pl.read_parquet_schema(old_path))

    columns = ", ".join(_STAGE_B_COLUMNS[channel])
    sql = f'SELECT local_ts, {columns} FROM "{channel}" ORDER BY local_ts'
    with Catalog(tmp_path) as catalog:
        rows = catalog.query(sql).to_dicts()
    assert rows == [
        {"local_ts": _TS, **dict.fromkeys(_STAGE_B_COLUMNS[channel])},
        {"local_ts": _TS + 1, **_expected_new(channel)},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", _CHANNELS)
async def test_the_record_api_reads_old_rows_as_unknown_and_new_rows_as_written(
    channel: str, tmp_path: pathlib.Path
) -> None:
    old = _record(channel, _TS)
    _write_pre_stage_b_part(tmp_path, old)
    new = _record(channel, _TS + 1, **_NEW_VALUES[channel])
    sink = ParquetSink(data_dir=tmp_path, max_buffer_rows=1000, flush_interval_seconds=9999)
    await sink.put(new)
    await sink.flush()

    with Catalog(tmp_path) as catalog:
        frame = catalog.scan(channel, _SYMBOL[channel], _TS - 1, _TS + 2)
    assert [from_row(row) for row in frame.to_dicts()] == [old, new]


@pytest.mark.asyncio
async def test_compacting_a_mixed_partition_keeps_the_values_and_the_nulls(
    tmp_path: pathlib.Path,
) -> None:
    channel = "ohlcv"
    old = _record(channel, _TS)
    _write_pre_stage_b_part(tmp_path, old)
    new = _record(channel, _TS + 1, **_NEW_VALUES[channel])
    sink = ParquetSink(data_dir=tmp_path, max_buffer_rows=1000, flush_interval_seconds=9999)
    await sink.put(new)
    await sink.flush()

    await ParquetCompactor(tmp_path, min_age_seconds=0.0).compact()

    (compacted,) = _parts(tmp_path)
    assert compacted.name.startswith("part-compacted-")
    with Catalog(tmp_path) as catalog:
        frame = catalog.scan(channel, _SYMBOL[channel], _TS - 1, _TS + 2)
    assert [from_row(row) for row in frame.to_dicts()] == [old, new]


@pytest.mark.asyncio
async def test_a_lake_with_no_stage_b_part_reads_unknown_through_the_record_api(
    tmp_path: pathlib.Path,
) -> None:
    channel = "ohlcv"
    old = _record(channel, _TS)
    _write_pre_stage_b_part(tmp_path, old)

    with Catalog(tmp_path) as catalog:
        frame = catalog.scan(channel, _SYMBOL[channel], _TS - 1, _TS + 2)
    (record,) = [from_row(row) for row in frame.to_dicts()]
    assert record == old
    assert isinstance(record, OHLCV)
    assert record.currency is None
    assert record.price_scale is None
    assert record.adjustment is None


@pytest.mark.asyncio
async def test_sql_naming_a_stage_b_column_on_a_lake_without_one_fails_loudly(
    tmp_path: pathlib.Path,
) -> None:
    _write_pre_stage_b_part(tmp_path, _record("ohlcv", _TS))
    with Catalog(tmp_path) as catalog, pytest.raises(duckdb.Error, match="currency"):
        catalog.query("SELECT currency FROM ohlcv")
