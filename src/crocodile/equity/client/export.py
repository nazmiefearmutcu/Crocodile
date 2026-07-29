"""Multi-format export helper for StockodileClient.

Supported formats:
    parquet — Apache Parquet (zstd-5, row_group_size=250k)
    csv     — Comma-separated values with header row
    arrow   — Arrow IPC (Feather v2) stream format
    json    — JSON array of objects
    jsonl   — Newline-delimited JSON (one object per line)
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import polars as pl

from crocodile.core.optional import require
from crocodile.core.store.catalog import Catalog

# Supported format strings
ExportFmt = Literal["parquet", "csv", "arrow", "json", "jsonl"]

_VALID_FMTS: frozenset[str] = frozenset({"parquet", "csv", "arrow", "json", "jsonl"})


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

    If ``limit`` is set, only the first ``limit`` rows (after concat/sort) are written.
    """
    if fmt not in _VALID_FMTS:
        raise ValueError(f"Unsupported fmt={fmt!r}. Must be one of: {sorted(_VALID_FMTS)}")
    if limit is not None and limit < 0:
        raise ValueError(f"limit must be >= 0, got {limit!r}")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Collect rows across all requested symbols.
    frames: list[pl.DataFrame] = []
    for symbol in symbols:
        df = catalog.scan(channel, symbol, frm, to)
        if len(df) > 0:
            frames.append(df)

    if frames:
        if len(frames) > 1:
            result = pl.concat(frames, how="diagonal").sort("local_ts")
        else:
            result = frames[0]
    else:
        result = pl.DataFrame()

    if limit is not None:
        result = result.head(limit)

    _write(result, fmt, dest)


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
        raise AssertionError(fmt)


def _write_parquet(df: pl.DataFrame, dest: Path) -> None:
    df.write_parquet(
        dest,
        compression="zstd",
        compression_level=5,
        row_group_size=250_000,
    )


def _write_csv(df: pl.DataFrame, dest: Path) -> None:
    if len(df) == 0:
        dest.write_bytes(b"")
        return
    # Convert list/array/struct columns to string so CSV writer doesn't crash
    cols_to_cast = []
    for col_name, dtype in zip(df.columns, df.dtypes, strict=True):
        if isinstance(dtype, (pl.List, pl.Array)):
            cols_to_cast.append(pl.col(col_name).cast(pl.List(pl.String)).list.join(","))
        elif isinstance(dtype, pl.Struct):
            cols_to_cast.append(pl.col(col_name).cast(pl.String))
    if cols_to_cast:
        df = df.with_columns(cols_to_cast)
    df.write_csv(dest)


def _write_arrow(df: pl.DataFrame, dest: Path) -> None:
    # Deferred on purpose — see crocodile.core.optional. pyarrow at module scope
    # made `import crocodile.equity.client` (and therefore `collect`) fail
    # wherever pyarrow cannot be installed, for the sake of one output format.
    pa_ipc = require(
        "pyarrow.ipc",
        feature="fmt='arrow' (Arrow IPC / Feather v2) export",
        hint="Install pyarrow, or export parquet/csv/json/jsonl instead.",
    )
    table = df.to_arrow()
    with pa_ipc.new_file(str(dest), table.schema) as writer:  # type: ignore[no-untyped-call]
        if len(table) > 0:
            writer.write_table(table)


def _write_json(df: pl.DataFrame, dest: Path) -> None:
    if len(df) == 0:
        dest.write_text("[]")
        return
    df.write_json(dest)


def _write_jsonl(df: pl.DataFrame, dest: Path) -> None:
    if len(df) == 0:
        dest.write_bytes(b"")
        return
    df.write_ndjson(dest)
