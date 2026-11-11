"""Unit tests for DeadLetterQueue drain and report helpers."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from crocodile.core.ingest.deadletter import (
    DEFAULT_DLQ_REPORT_NAME,
    DLQ_DIR_NAME,
    DeadLetter,
    DeadLetterQueue,
    build_dlq_report,
    dead_letter_to_dict,
    dlq_db_path,
    drain_connector_dlqs,
    report_drained_dlqs,
    write_dlq_report,
)


@pytest.mark.asyncio
async def test_drain_returns_items_and_clears() -> None:
    dlq = DeadLetterQueue(max_size=10)
    await dlq.put(1, b"a", "ParseError", "tb1")
    await dlq.put(2, b"b", "ValueError", "tb2")
    assert len(dlq) == 2

    items = dlq.drain()
    assert len(items) == 2
    assert items[0].raw == b"a"
    assert items[1].error_type == "ValueError"
    assert len(dlq) == 0
    assert dlq.drain() == []


@pytest.mark.asyncio
async def test_drain_bounded_eviction() -> None:
    dlq = DeadLetterQueue(max_size=2)
    await dlq.put(1, b"a", "e", "t")
    await dlq.put(2, b"b", "e", "t")
    await dlq.put(3, b"c", "e", "t")
    items = dlq.drain()
    assert [i.raw for i in items] == [b"b", b"c"]


def test_dead_letter_to_dict_utf8() -> None:
    item = DeadLetter(local_ts=99, raw=b'{"x":1}', error_type="JSONDecodeError", traceback="t")
    d = dead_letter_to_dict(item, connector="deribit")
    assert d["local_ts"] == 99
    assert d["raw"] == '{"x":1}'
    assert d["error_type"] == "JSONDecodeError"
    assert d["connector"] == "deribit"


def test_dead_letter_to_dict_replaces_invalid_utf8() -> None:
    item = DeadLetter(local_ts=1, raw=b"\xff\xfe", error_type="E", traceback="")
    d = dead_letter_to_dict(item)
    assert "\ufffd" in d["raw"] or d["raw"]  # replacement chars accepted


def test_build_dlq_report_counts_by_error_type() -> None:
    a = DeadLetter(1, b"x", "JSONDecodeError", "t")
    b = DeadLetter(2, b"y", "ValueError", "t")
    c = DeadLetter(3, b"z", "JSONDecodeError", "t")
    report = build_dlq_report([("deribit", a), ("binance", b), ("deribit", c)])
    assert report["count"] == 3
    assert report["by_error_type"] == {"JSONDecodeError": 2, "ValueError": 1}
    assert report["items"][0]["connector"] == "deribit"
    assert report["items"][1]["connector"] == "binance"


def test_write_dlq_report(tmp_path: Path) -> None:
    dest = tmp_path / "nested" / "report.json"
    report = {"count": 1, "by_error_type": {"E": 1}, "items": []}
    written = write_dlq_report(dest, report)
    assert written == dest
    loaded = json.loads(dest.read_text(encoding="utf-8"))
    assert loaded["count"] == 1


@pytest.mark.asyncio
async def test_report_drained_dlqs_writes_under_data_dir(tmp_path: Path) -> None:
    dlq = DeadLetterQueue()
    await dlq.put(10, b"bad", "JSONDecodeError", "trace")
    conn = SimpleNamespace(name="deribit", _dlq=dlq)

    report = report_drained_dlqs([conn], data_dir=tmp_path)
    assert report["count"] == 1
    path = tmp_path / DEFAULT_DLQ_REPORT_NAME
    assert path.is_file()
    assert report["path"] == str(path)
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["items"][0]["raw"] == "bad"
    assert body["items"][0]["connector"] == "deribit"
    # drained
    assert len(dlq) == 0


@pytest.mark.asyncio
async def test_report_drained_dlqs_explicit_path(tmp_path: Path) -> None:
    dlq = DeadLetterQueue()
    await dlq.put(1, b"x", "E", "t")
    conn = SimpleNamespace(name="x", _dlq=dlq)
    dest = tmp_path / "custom_dlq.json"
    report = report_drained_dlqs([conn], report_path=dest, data_dir=tmp_path / "ignored")
    assert Path(report["path"]) == dest
    assert dest.is_file()
    assert not (tmp_path / "ignored" / DEFAULT_DLQ_REPORT_NAME).exists()


@pytest.mark.asyncio
async def test_report_drained_dlqs_empty_writes_nothing(tmp_path: Path) -> None:
    conn = SimpleNamespace(name="x", _dlq=DeadLetterQueue())
    report = report_drained_dlqs([conn], data_dir=tmp_path)
    assert report["count"] == 0
    assert not (tmp_path / DEFAULT_DLQ_REPORT_NAME).exists()
    assert "path" not in report


@pytest.mark.asyncio
async def test_drain_connector_dlqs_skips_missing() -> None:
    dlq = DeadLetterQueue()
    await dlq.put(1, b"a", "E", "t")
    entries = drain_connector_dlqs(
        [
            SimpleNamespace(name="ok", _dlq=dlq),
            SimpleNamespace(name="nope"),  # no _dlq
        ]
    )
    assert len(entries) == 1
    assert entries[0][0] == "ok"


def test_dlq_db_path_is_under_the_lake_dlq_directory(tmp_path: Path) -> None:
    assert DLQ_DIR_NAME == "dlq"
    assert dlq_db_path(tmp_path, "yahoo_chart") == tmp_path / "dlq" / "yahoo_chart.sqlite"
    assert dlq_db_path(str(tmp_path), "yahoo_chart") == tmp_path / "dlq" / "yahoo_chart.sqlite"


@pytest.mark.parametrize("source", ["", "../x", "a/b", "a\\b", "a\b", "Yahoo"])
def test_dlq_db_path_refuses_a_name_that_could_leave_the_directory(
    tmp_path: Path, source: str
) -> None:
    with pytest.raises(ValueError):
        dlq_db_path(tmp_path, source)


def test_a_durable_queue_creates_its_directory(tmp_path: Path) -> None:
    db = tmp_path / "dlq" / "x.sqlite"
    first = DeadLetterQueue(db_path=str(db))
    first.put(7, b"raw", "E", "tb")

    assert db.exists()
    second = DeadLetterQueue(db_path=str(db))
    assert len(second) == 1
    assert second.drain()[0].local_ts == 7
