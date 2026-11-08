"""Packaged-data loader: header, field-count, missing-file and encoding rejections.

``read_csv`` is the one seam through which the reference tables enter the package.
Each rejection below raises ``ReferenceDataError`` naming the file. The tests inject
the bytes through ``_data.read_bytes`` so the packaged files stay untouched.
"""

from __future__ import annotations

import pytest

from crocodile.core.markets import _data
from crocodile.core.markets.errors import ReferenceDataError

pytestmark = pytest.mark.usefixtures("network_forbidden")

HEADER = ("a", "b", "c")


def _inject(monkeypatch: pytest.MonkeyPatch, content: bytes) -> None:
    monkeypatch.setattr(_data, "read_bytes", lambda name: content)


def test_read_csv_returns_one_dict_per_row_under_the_exact_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inject(monkeypatch, b"a,b,c\n1,2,3\n4,5,6\n")
    assert _data.read_csv("x.csv", HEADER) == [
        {"a": "1", "b": "2", "c": "3"},
        {"a": "4", "b": "5", "c": "6"},
    ]


@pytest.mark.parametrize(
    "content",
    [b"a,b\n1,2\n", b"a,b,d\n1,2,3\n", b"c,b,a\n1,2,3\n", b""],
    ids=["too-few-columns", "renamed-column", "reordered-columns", "empty-file"],
)
def test_read_csv_refuses_a_header_that_differs(
    monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    _inject(monkeypatch, content)
    with pytest.raises(ReferenceDataError) as caught:
        _data.read_csv("x.csv", HEADER)
    assert "x.csv" in str(caught.value)


@pytest.mark.parametrize(
    "content",
    [b"a,b,c\n1,2,3,4\n", b"a,b,c\n1,2\n"],
    ids=["too-many-fields", "too-few-fields"],
)
def test_read_csv_refuses_a_row_with_the_wrong_field_count(
    monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    _inject(monkeypatch, content)
    with pytest.raises(ReferenceDataError) as caught:
        _data.read_csv("x.csv", HEADER)
    assert "expected 3 fields" in str(caught.value)


def test_read_csv_refuses_a_missing_resource() -> None:
    with pytest.raises(ReferenceDataError) as caught:
        _data.read_csv("no-such-reference-file.csv", HEADER)
    assert "no-such-reference-file.csv" in str(caught.value)
    assert "missing" in str(caught.value)


def test_read_text_refuses_bytes_that_are_not_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    _inject(monkeypatch, b"a,b,c\n\xff,2,3\n")
    with pytest.raises(ReferenceDataError) as caught:
        _data.read_text("x.csv")
    assert "not valid UTF-8" in str(caught.value)
    with pytest.raises(ReferenceDataError):
        _data.read_csv("x.csv", HEADER)
