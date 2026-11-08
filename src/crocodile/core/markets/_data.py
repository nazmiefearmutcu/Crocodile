"""Private loader for the packaged ``data/`` files of ``crocodile.core.markets``.

Files are read through ``importlib.resources`` so the same code works from an
editable install and from a wheel. Every text read is strict UTF-8. Malformed
content, a missing file or a header that is not the expected one raises
``ReferenceDataError`` naming the file.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Final

from crocodile.core.markets.errors import ReferenceDataError

__all__ = ["DATA_PACKAGE", "read_bytes", "read_csv", "read_text", "resource"]

DATA_PACKAGE: Final = "crocodile.core.markets"


def resource(name: str) -> Traversable:
    """Return the packaged data file ``name`` as a traversable resource."""
    return files(DATA_PACKAGE) / "data" / name


def read_bytes(name: str) -> bytes:
    """Return the raw bytes of a packaged data file."""
    try:
        return resource(name).read_bytes()
    except FileNotFoundError as exc:
        raise ReferenceDataError(name, "packaged file is missing") from exc


def read_text(name: str) -> str:
    """Return a packaged data file decoded as strict UTF-8."""
    try:
        return read_bytes(name).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReferenceDataError(name, f"not valid UTF-8: {exc}") from exc


def read_csv(name: str, header: Sequence[str]) -> list[dict[str, str]]:
    """Parse a packaged CSV, requiring exactly ``header`` and a uniform field count."""
    expected = tuple(header)
    reader = csv.DictReader(io.StringIO(read_text(name), newline=""))
    actual = tuple(reader.fieldnames or ())
    if actual != expected:
        raise ReferenceDataError(name, f"header {list(actual)!r} differs from {list(expected)!r}")
    rows: list[dict[str, str]] = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ReferenceDataError(
                name, f"line {reader.line_num}: expected {len(expected)} fields"
            )
        rows.append({key: value for key, value in row.items() if key is not None})
    return rows
