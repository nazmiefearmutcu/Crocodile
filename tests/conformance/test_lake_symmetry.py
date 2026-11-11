"""What the lake can be written with, it can be read back with.

The sink could write all 30 canonical channels. ``from_row`` had 13 hand-written
arms and raised ``ValueError: Unknown channel tag`` on the other 17 — three of
them (``limit_order_fill``, ``balance_correction``, ``por_update``) live channels
``base_onchain`` emits, whose payloads the sink had just stopped discarding. So a
``base_onchain`` lake recorded data ``CrypcodileClient.replay`` could not read.
Nothing compared the two sets, because each side only ever tested itself.

Both sides now derive their channel list from the ``Record`` union, which is why
the round trip below is the assertion that carries the weight: it writes real
Parquet through the real sink and reads it back through the real reader, so it
fails on a missing coercion, a wrong dtype, a column the schema forgot or a field
that comes back as something else — not only on a channel nobody wired up.
"""

from __future__ import annotations

import enum
import pathlib
import types
import typing
from typing import Any

import msgspec.structs
import polars as pl
import pytest

from crocodile.core.schema.enums import PriceAdjustment
from crocodile.core.schema.provenance import provenance_fields
from crocodile.core.schema.records import OHLCV, Instrument, Record, _Header
from crocodile.core.store.parquet_sink import _CANONICAL_CHANNEL_EXTRA, ParquetSink
from crocodile.core.store.rows import _RECORD_BY_CHANNEL, from_row

_TS = 1_700_000_000_000_000_000  # 2023-11-14

_HEADER_FIELDS = frozenset(f.name for f in msgspec.structs.fields(_Header))

# Built from the union, not from either side's table. A gate whose subject list
# comes from the thing under test goes quiet the moment that thing loses an entry
# — which is the exact failure being guarded here.
_STRUCT_BY_TAG: dict[str, Any] = {
    struct.__struct_config__.tag: struct for struct in typing.get_args(Record)
}


def _header_kwargs() -> dict[str, Any]:
    """A header with a *non-default* provenance tail.

    Defaults would round-trip even if the reader dropped the four ``prov_*``
    columns entirely, which is the shape of bug that turns a modelled value back
    into a venue-reported one.
    """
    tail = provenance_fields("yahoo_1m_vap", {"n_volume_bars": 195})
    return {
        "source": "binance",
        "symbol": "binance:BTC-USDT",
        "symbol_raw": "BTCUSDT",
        "local_ts": _TS,
        "asset_class": "crypto",
        "source_ts": _TS,
        "prov": tail.prov,
        "prov_basis": tail.prov_basis,
        "prov_confidence": tail.prov_confidence,
        "prov_inputs": tail.prov_inputs,
    }


def _sample(annotation: Any, field_name: str) -> Any:
    """A value of the declared type, distinct enough to catch a column swap.

    String fields carry their own name, so a body field written into the wrong
    column comes back naming where it landed rather than comparing equal by
    accident.
    """
    if typing.get_origin(annotation) in (types.UnionType, typing.Union):
        members = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(members) == 1:
            annotation = members[0]

    if isinstance(annotation, type):
        if issubclass(annotation, enum.Enum):
            return next(iter(annotation))
        if annotation is bool:
            return True
        if annotation is int:
            return 7
        if annotation is float:
            return 1.5
        if annotation is str:
            return field_name

    if typing.get_origin(annotation) is list:
        (inner,) = typing.get_args(annotation)
        if typing.get_origin(inner) is tuple:
            # The 0.0 level is the canonical removal signal and has been lost to
            # falsy-checks before, so it is in every book fixture here.
            return [(100.0, 5.0), (99.0, 0.0)]
        return [_sample(inner, field_name)]

    raise AssertionError(f"this fixture has no sample value for {annotation!r} ({field_name})")


def _a_record_of(struct: Any) -> Any:
    """Build ``struct`` with every field populated, header included."""
    body = {
        f.name: _sample(f.type, f.name)
        for f in msgspec.structs.fields(struct)
        if f.name not in _HEADER_FIELDS
    }
    return struct(**_header_kwargs(), **body)


def test_every_channel_the_sink_can_write_from_row_can_read() -> None:
    """The two sets, stated. Widening one side without the other fails here."""
    assert set(_CANONICAL_CHANNEL_EXTRA) == set(_RECORD_BY_CHANNEL) == set(_STRUCT_BY_TAG)


@pytest.mark.parametrize("channel", sorted(_STRUCT_BY_TAG))
async def test_every_canonical_channel_survives_a_lake_round_trip(
    channel: str, tmp_path: pathlib.Path
) -> None:
    """Write it as the product writes it, read it as the product reads it.

    One lake per channel: a single ``read_parquet`` over 30 channels would union
    their schemas and hand every row columns it never had, which is a friendlier
    input than the reader will ever get in production.
    """
    original = _a_record_of(_STRUCT_BY_TAG[channel])

    sink = ParquetSink(data_dir=tmp_path, max_buffer_rows=1000, flush_interval_seconds=9999)
    await sink.put(original)
    await sink.flush()

    # ``hive_partitioning`` because ``source`` is a path component, never a
    # column: the sink keeps it out of every file schema so a renamed lake still
    # reads as one shape.
    df = pl.read_parquet(str(tmp_path / "**" / "*.parquet"), hive_partitioning=True)
    (row,) = df.to_dicts()

    assert from_row(row) == original


async def test_stage_b_columns_are_written_typed_and_read_back(tmp_path: pathlib.Path) -> None:
    """The Stage-B columns land as typed Parquet columns and read back to the same record.

    ``SPLIT_DIVIDEND`` is deliberately not the first member, so a reader that falls back to
    the first member instead of the stored value fails here.
    """
    bar = OHLCV(
        **_header_kwargs(),
        interval="1m",
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=10.0,
        currency="GBP",
        price_scale=2,
        adjustment=PriceAdjustment.SPLIT_DIVIDEND,
    )
    sink = ParquetSink(data_dir=tmp_path, max_buffer_rows=1000, flush_interval_seconds=9999)
    await sink.put(bar)
    await sink.flush()

    df = pl.read_parquet(str(tmp_path / "**" / "*.parquet"), hive_partitioning=True)
    assert df.schema["currency"] == pl.String
    assert df.schema["price_scale"] == pl.Int64
    assert df.schema["adjustment"] == pl.String
    (row,) = df.to_dicts()
    assert from_row(row) == bar


async def test_stage_b_instrument_fields_are_written_typed_and_read_back(
    tmp_path: pathlib.Path,
) -> None:
    """All six listing-identity fields, in a sub-lake of their own so the schemas stay apart."""
    listing = Instrument(
        **_header_kwargs(),
        mic="XNAS",
        segment_mic="XNGS",
        isin="US0378331005",
        country="US",
        currency="USD",
        price_scale=0,
    )
    lake = tmp_path / "i"
    sink = ParquetSink(data_dir=lake, max_buffer_rows=1000, flush_interval_seconds=9999)
    await sink.put(listing)
    await sink.flush()

    df = pl.read_parquet(str(lake / "**" / "*.parquet"), hive_partitioning=True)
    for name in ("mic", "segment_mic", "isin", "country", "currency"):
        assert df.schema[name] == pl.String, name
    assert df.schema["price_scale"] == pl.Int64
    (row,) = df.to_dicts()
    assert from_row(row) == listing
