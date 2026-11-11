"""Durable equity dead-letter queues: crash survival, factory wiring, and the equity drain."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import pytest

from crocodile.capabilities import ops
from crocodile.core.capability import CapabilityContext
from crocodile.core.config import Settings
from crocodile.core.ingest.deadletter import DeadLetterQueue, dlq_db_path, report_drained_dlqs
from crocodile.core.ingest.transport import FakeTransport
from crocodile.core.schema.enums import AssetClass, Channel, SecurityType
from crocodile.core.sink.base import Sink
from crocodile.core.store.catalog import Catalog
from crocodile.equity.client.collect import collect
from crocodile.equity.providers import factory
from crocodile.equity.providers.base import Provider
from crocodile.equity.providers.factory import make_provider
from crocodile.equity.providers.finra.connector import FinraProvider
from crocodile.equity.reference.identity import InstrumentIdentity
from crocodile.equity.reference.registry import InstrumentRegistry


class DummySink(Sink):
    def __init__(self) -> None:
        self.records: list[Any] = []

    async def put(self, record: Any) -> None:
        self.records.append(record)

    async def flush(self) -> None:
        pass


class DummyProvider(Provider):
    name = "dummy"

    async def list_instruments(self) -> list[InstrumentIdentity]:
        return [
            InstrumentIdentity(
                symbol="AAPL",
                source=self.name,
                symbol_raw="AAPL",
                security_type=SecurityType.CS,
            )
        ]

    async def _subscribe(self, transport: Any) -> None:
        await transport.send(b'{"action": "subscribe"}')

    def normalize(self, msg: object, local_ts: int) -> Iterable[Any]:
        yield msg


def _dummy(sink: Sink | None = None) -> DummyProvider:
    return DummyProvider(
        symbols=["AAPL"],
        channels=["trade"],
        out=sink or DummySink(),
        registry=InstrumentRegistry(),
    )


def test_use_durable_dlq_moves_pending_letters_into_the_durable_queue(tmp_path: Path) -> None:
    provider = _dummy()
    provider._dlq.put(1, b"raw", "JSONDecodeError", "tb")

    path = provider.use_durable_dlq(tmp_path)

    assert path == dlq_db_path(tmp_path, "dummy")
    assert provider._dlq.db_path == str(path)
    assert len(provider._dlq) == 1


def test_dead_letters_survive_a_crash_and_are_reported_by_the_next_run(tmp_path: Path) -> None:
    crashed = _dummy()
    crashed.use_durable_dlq(tmp_path)
    crashed._dlq.put(1, b"x", "E1", "tb")
    crashed._dlq.put(2, b"y", "E2", "tb")
    del crashed  # the run dies here, undrained

    next_run = _dummy()
    next_run.use_durable_dlq(tmp_path)
    assert len(next_run._dlq) == 2

    report = report_drained_dlqs([next_run], data_dir=tmp_path)
    assert report["count"] == 2
    written = json.loads((tmp_path / "dlq_report.json").read_text(encoding="utf-8"))
    assert written["count"] == 2
    assert all(item["connector"] == "dummy" for item in written["items"])

    afterwards = _dummy()
    afterwards.use_durable_dlq(tmp_path)
    assert len(afterwards._dlq) == 0


def test_make_provider_with_a_data_dir_gives_a_durable_queue(
    tmp_path: Path, network_forbidden: None
) -> None:
    provider = make_provider(
        provider="finra",
        symbols=["GME"],
        channels=[Channel.SHORT_VOLUME.value],
        out=DummySink(),
        registry=InstrumentRegistry(),
        data_dir=tmp_path,
    )

    assert isinstance(provider, FinraProvider)
    assert provider._dlq.db_path == str(dlq_db_path(tmp_path, "finra"))


def test_make_provider_without_a_data_dir_keeps_the_in_memory_queue(
    network_forbidden: None,
) -> None:
    provider = make_provider(
        provider="finra",
        symbols=["GME"],
        channels=[Channel.SHORT_VOLUME.value],
        out=DummySink(),
        registry=InstrumentRegistry(),
    )

    assert provider._dlq.db_path is None


async def test_equity_collect_drains_and_reports_dead_letters(tmp_path: Path) -> None:
    provider = _dummy()
    provider.use_durable_dlq(tmp_path)
    provider.transport = FakeTransport([b"not json", b'{"data": 1}'])

    await collect([provider], DummySink(), max_reconnects=0, data_dir=tmp_path)

    report = json.loads((tmp_path / "dlq_report.json").read_text(encoding="utf-8"))
    assert report["count"] == 1
    assert report["items"][0]["error_type"] == "JSONDecodeError"
    drained = DeadLetterQueue(db_path=str(dlq_db_path(tmp_path, "dummy")))
    assert len(drained) == 0


async def test_equity_collect_with_an_explicit_report_path(tmp_path: Path) -> None:
    provider = _dummy()
    provider.use_durable_dlq(tmp_path)
    provider.transport = FakeTransport([b"not json"])
    explicit = tmp_path / "x.json"

    await collect(
        [provider],
        DummySink(),
        max_reconnects=0,
        dlq_report_path=explicit,
        data_dir=tmp_path,
    )

    assert explicit.exists()
    assert not (tmp_path / "dlq_report.json").exists()


async def test_equity_backfill_drains_its_provider_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class PullFake:
        name = "dummy"

        def __init__(self) -> None:
            self._dlq = DeadLetterQueue()
            self._dlq.put(1, b"x", "E", "tb")

        async def backfill(
            self, channel: str, symbol: str, start_ns: int, end_ns: int
        ) -> AsyncIterator[Any]:
            if False:  # pragma: no cover - makes this an async generator that yields nothing
                yield None

        async def close(self) -> None:
            return None

    provider = PullFake()
    monkeypatch.setattr(factory, "make_provider", lambda **kwargs: provider)
    params = ops.BackfillParams(
        source="dummy",
        channel="trade",
        symbols=("AAPL",),
        start_ns=0,
        end_ns=1,
    )

    await ops._drain_provider_backfill("dummy", params, DummySink(), Settings(data_dir=tmp_path))

    report = json.loads((tmp_path / "dlq_report.json").read_text(encoding="utf-8"))
    assert report["count"] == 1


async def test_equity_collect_and_backfill_pass_the_lake_root_to_the_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[dict[str, Any]] = []

    class Fake:
        name = "dummy"
        transport: object = FakeTransport([])
        ws_url = "wss://unused.example"

        def __init__(self) -> None:
            self._dlq = DeadLetterQueue()

        async def backfill(
            self, channel: str, symbol: str, start_ns: int, end_ns: int
        ) -> AsyncIterator[Any]:
            if False:  # pragma: no cover
                yield None

        async def close(self) -> None:
            return None

    def recorder(**kwargs: Any) -> Fake:
        recorded.append(kwargs)
        return Fake()

    monkeypatch.setattr(factory, "make_provider", recorder)
    catalog = Catalog(tmp_path)
    try:
        ctx = CapabilityContext(
            catalog=catalog,
            settings=Settings(data_dir=tmp_path),
            asset_class=AssetClass.EQUITY,
        )
        ops.collect_equities(
            ctx,
            ops.CollectParams(sources=("dummy",), symbols=("AAPL",), channels=("trade",)),
        )
        await ops._drain_provider_backfill(
            "dummy",
            ops.BackfillParams(
                source="dummy", channel="trade", symbols=("AAPL",), start_ns=0, end_ns=1
            ),
            DummySink(),
            Settings(data_dir=tmp_path),
        )
    finally:
        catalog.close()

    assert len(recorded) == 2
    assert all(kwargs["data_dir"] == tmp_path for kwargs in recorded)
