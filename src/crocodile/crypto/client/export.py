"""Multi-format export helper for CrypcodileClient (Task 3.3).

Supported formats:
    parquet — Apache Parquet (zstd-5, row_group_size=250k)
    csv     — Comma-separated values with header row
    arrow   — Arrow IPC (Feather v2) stream format
    json    — JSON array of objects
    jsonl   — Newline-delimited JSON (one object per line)

Usage::

    client.export(
        channel="trade",
        symbols=["deribit:BTC-PERPETUAL"],
        frm=start_ns,
        to=end_ns,
        fmt="csv",
        dest=pathlib.Path("/data/out/trades.csv"),
    )

Design notes:
    - The function scans the catalog for the requested channel x symbols x time
      range, then writes the resulting Polars DataFrame to the specified format.
    - The parent directory of ``dest`` is created automatically.
    - An empty result set (no matching rows) still creates the destination file
      (zero-byte for formats that have no schema-only representation; a valid
      empty structure for formats that do).
    - Arrow export uses PyArrow IPC (``ipc.new_file``) rather than Polars'
      native Arrow writer so that the file is readable by standard Arrow
      tooling (``pyarrow.ipc.open_file``).
    - Book-level columns (``bids``/``asks``) are stored as Polars
      ``list[struct]`` in Parquet.  JSON/JSONL serialisation relies on Polars'
      built-in ``write_ndjson`` / ``write_json``, which handle nested structs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import polars as pl

from crocodile.core.optional import require
from crocodile.core.store.catalog import Catalog

# Supported format strings (exposed as a Literal type for type-checker friendliness).
ExportFmt = Literal["parquet", "csv", "arrow", "json", "jsonl"]

_VALID_FMTS: frozenset[str] = frozenset({"parquet", "csv", "arrow", "json", "jsonl"})


def _python_type_to_polars(py_type) -> pl.DataType:
    import typing
    origin = typing.get_origin(py_type)
    if origin is typing.Union:
        args = typing.get_args(py_type)
        args = [a for a in args if a is not type(None)]
        if len(args) == 1:
            py_type = args[0]
        else:
            py_type = args[0]

    origin = typing.get_origin(py_type)
    if origin is list:
        return pl.List(pl.Null)

    if py_type is int:
        return pl.Int64
    if py_type is float:
        return pl.Float64
    if py_type is bool:
        return pl.Boolean
    if py_type is str:
        return pl.String

    import enum
    if isinstance(py_type, type) and issubclass(py_type, enum.Enum):
        return pl.String

    return pl.Null


def _get_empty_df_for_channel(catalog: Catalog, channel: str) -> pl.DataFrame:
    try:
        df = catalog.query(f'SELECT * FROM "{channel}" LIMIT 0')
        if len(df.columns) > 0:
            return df
    except Exception:
        pass

    try:
        import msgspec

        from crocodile.core.schema import records
        cls = None
        for name in dir(records):
            item = getattr(records, name)
            if (
                isinstance(item, type)
                and hasattr(item, "__struct_config__")
                and getattr(item.__struct_config__, "tag", None) == channel
            ):
                cls = item
                break

        if cls is not None:
            fields = msgspec.structs.fields(cls)
            schema = {}
            for f in fields:
                schema[f.name] = _python_type_to_polars(f.type)
            schema["channel"] = pl.String
            schema["date"] = pl.String
            schema["bucket"] = pl.Int64
            return pl.DataFrame(schema=schema)
    except Exception:
        pass

    return pl.DataFrame()


def export(
    catalog: Catalog,
    channel: str,
    symbols: list[str],
    frm: int,
    to: int,
    fmt: str,
    dest: Path | str,
    limit: int | None = None,
) -> None:
    """Export rows for ``(channel, symbols, [frm, to])`` to a file in ``fmt`` format.

    Args:
        catalog:  A :class:`~crocodile.core.store.catalog.Catalog` instance pointing
                  at the data lake root.
        channel:  Channel name, e.g. ``"trade"``, ``"book_snapshot"``.
        symbols:  List of canonical symbol strings.  An empty list writes an
                  empty file.
        frm:      Inclusive lower bound on ``local_ts`` (nanoseconds UTC).
        to:       Inclusive upper bound on ``local_ts`` (nanoseconds UTC).
        fmt:      One of ``parquet``, ``csv``, ``arrow``, ``json``, ``jsonl``.
        dest:     Destination file path.  Parent directories are created if
                  they do not exist.
        limit:    Optional maximum number of rows to export.

    Raises:
        ValueError: If ``fmt`` is not one of the supported format strings.
    """
    if fmt not in _VALID_FMTS:
        raise ValueError(
            f"Unsupported fmt={fmt!r}. Must be one of: {sorted(_VALID_FMTS)}"
        )

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Scan all requested symbols in a single database pass
    df = catalog.scan(channel, symbols, frm, to, limit=limit)
    if len(df) > 0:
        result = df
    else:
        result = _get_empty_df_for_channel(catalog, channel)

    _write(result, fmt, dest)


# ---------------------------------------------------------------------------
# Per-format write helpers
# ---------------------------------------------------------------------------


def _write(df: pl.DataFrame, fmt: str, dest: Path) -> None:
    if fmt == "parquet":
        _write_parquet(df, dest)
    elif fmt == "csv":
        _write_csv(df, dest)
    elif fmt == "arrow":
        _write_arrow(df, dest)
    elif fmt == "json":
        _write_json(df, dest)
    elif fmt == "jsonl":
        _write_jsonl(df, dest)
    else:
        raise AssertionError(fmt)  # unreachable: all _VALID_FMTS must be handled above


def _write_parquet(df: pl.DataFrame, dest: Path) -> None:
    """Write a Polars DataFrame as Parquet (zstd-5, row_group_size=250k).

    Polars produces a valid ~135-byte Parquet file even for empty DataFrames
    (proper header + footer), so we always delegate to write_parquet rather than
    short-circuiting to zero bytes (which would produce an unreadable file).
    """
    df.write_parquet(
        dest,
        compression="zstd",
        compression_level=5,
        row_group_size=250_000,
    )


def _write_csv(df: pl.DataFrame, dest: Path) -> None:
    """Write a Polars DataFrame as CSV with header, nested columns as JSON text.

    Polars refuses to guess a CSV encoding for a list or a struct — it raises
    ``ComputeError: CSV format does not support nested data``. That used to
    affect only the book channels, whose ``bids``/``asks`` are lists. Every
    canonical record now carries ``prov_inputs``, a ``list[str]``, so leaving it
    alone would have turned CSV into a format this tool cannot export at all.

    JSON text is the encoding, rather than dropping the column: a dropped column
    is an export that quietly answers a different question than it was asked.
    A null stays empty so it reads as a missing cell and not as the string
    ``"null"``.
    """
    if len(df) == 0:
        dest.write_bytes(b"")
        return
    nested = [name for name, dtype in df.schema.items() if dtype.is_nested()]
    if nested:
        df = df.with_columns(
            pl.Series(
                name,
                [None if v is None else json.dumps(v) for v in df[name].to_list()],
                dtype=pl.Utf8,
            )
            for name in nested
        )
    df.write_csv(dest)


def _write_arrow(df: pl.DataFrame, dest: Path) -> None:
    """Write a Polars DataFrame as Arrow IPC (Feather v2) using PyArrow IPC.

    PyArrow's new_file produces a valid ~146-byte IPC file even when zero batches
    are written (proper magic bytes + schema block + footer), so we always use the
    IPC writer rather than short-circuiting to zero bytes (which would produce an
    unreadable file for downstream pa_ipc.open_file callers).
    """
    # Deferred on purpose — see crocodile.core.optional. pyarrow at module scope
    # made this one output format able to break `import crocodile.crypto.client`
    # on any platform that cannot install it.
    pa_ipc = require(
        "pyarrow.ipc",
        feature="fmt='arrow' (Arrow IPC / Feather v2) export",
        hint="Install pyarrow, or export parquet/csv/json/jsonl instead.",
    )
    # Convert to PyArrow Table then write IPC stream.
    table = df.to_arrow()
    with pa_ipc.new_file(str(dest), table.schema) as writer:  # type: ignore[no-untyped-call]
        if len(table) > 0:
            writer.write_table(table)


def _write_json(df: pl.DataFrame, dest: Path) -> None:
    """Write a Polars DataFrame as a JSON array of objects."""
    if len(df) == 0:
        dest.write_text("[]")
        return
    # Polars write_json produces a JSON array.
    df.write_json(dest)


def _write_jsonl(df: pl.DataFrame, dest: Path) -> None:
    """Write a Polars DataFrame as JSONL (one JSON object per line)."""
    if len(df) == 0:
        dest.write_bytes(b"")
        return
    # Polars write_ndjson produces newline-delimited JSON.
    df.write_ndjson(dest)
