"""Independent source fixtures, event clocks, persistence, and network boundaries."""

from __future__ import annotations

import asyncio
import gzip
import json
import subprocess
import sys
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from crocodile.equity.universal.live.eurex_delayed import (
    EurexDelayedSource,
    EurexDelayedTransport,
    TransportError,
)
from crocodile.equity.universal.live.event_time_bars import parse_file, reduce_minute
from crocodile.equity.universal.live.intraday_models import CONTRACTS, parse_utc_ns
from crocodile.equity.universal.live.intraday_store import IntradayStore, StoreError

FIXTURES = Path(__file__).parents[1] / "fixtures" / "live" / "eurex_delayed"
NAME = "DEUR-posttrade-2026-11-13T10_00.json.gz"
NAME2 = "DEUR-posttrade-2026-11-13T10_01.json.gz"
ISIN = "DE000F0GDWD2"
MINUTE = parse_utc_ns("2026-11-13T10:00:00Z")


def row(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "tradingDateAndTime": "2026-11-13T10:00:00.000000001Z",
        "publicationDateAndTime": "2026-11-13T10:00:01.000000001Z",
        "instrumentIdentificationCode": ISIN,
        "venueOfExecution": "XEUR",
        "transactionIdentificationCode": "one",
        "quantity": "0.1",
        "price": "26874.1",
        "priceNotation": 1,
        "priceCurrency": "EUR",
        "flags": "",
        "tradingSystem": "CLOB",
    }
    result.update(changes)
    return result


def zipped(*rows: dict[str, object]) -> bytes:
    return gzip.compress(("\n".join(json.dumps(r) for r in rows) + "\n").encode(), mtime=0)


def test_real_four_golden_minutes_and_restart(tmp_path: Path) -> None:
    expected = json.loads((FIXTURES / "expected_minutes.json").read_text())["expected_minutes"]
    with IntradayStore(tmp_path) as store:
        for name in (NAME, NAME2):
            raw = (FIXTURES / name).read_bytes()
            result = store.ingest_file(name, raw, captured_ns=MINUTE + 1000)
            assert result.changed
            for _ in range(3):
                assert not store.ingest_file(name, raw).changed
        assert store.observation_count() == 286
        for item in expected:
            bars = store.query_bars(
                item["isin"], start_ns=parse_utc_ns(item["minute_start_utc"]), limit=1
            )
            bar = bars[0]
            for field in ("open", "high", "low", "close"):
                assert getattr(bar, field) == Decimal(item[field])
            assert bar.volume == Decimal(item["quantity_sum_raw"])
            assert bar.vwap == Decimal(item["vwap_raw"])
            assert bar.trade_count == item["rows_eligible"]
            assert bar.quality == "observed"
            assert bar.origin == "derived" and bar.files
    with IntradayStore(tmp_path) as store:
        assert len(store.completed_files()) == 2
        assert store.query_bars(ISIN)[0].volume == Decimal("33")
        assert not store.ingest_file(NAME, (FIXTURES / NAME).read_bytes()).changed


def test_catalogue_immutable_dated_and_nanosecond_order() -> None:
    assert {c.display_symbol for c in CONTRACTS.values()} == {"FDAXZ2026", "FGBLZ2026"}
    with pytest.raises(TypeError):
        CONTRACTS["bad"] = CONTRACTS[ISIN]  # type: ignore[index]
    observations = parse_file(
        NAME,
        zipped(
            row(price="3", quantity="0.2", tradingDateAndTime="2026-11-13T10:00:00.000000003Z"),
            row(price="1", quantity="0.1", transactionIdentificationCode="two"),
            row(
                price="2",
                quantity="0.1",
                transactionIdentificationCode="three",
                tradingDateAndTime="2026-11-13T10:00:00.000000002Z",
            ),
        ),
        captured_ns=99,
    )
    bar = reduce_minute(ISIN, MINUTE, observations)
    assert bar.open == 1 and bar.close == 3
    assert bar.volume == Decimal("0.4") and bar.vwap == Decimal("2.25")
    assert observations[0].event_time_ns == MINUTE + 3
    assert observations[0].captured_ns == 99
    with pytest.raises(ValueError):
        parse_utc_ns("2026-11-13T10:00:00.1234567891Z")


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"price": None}, "missing_price"),
        ({"flags": "CANC"}, "unknown_flags"),
        ({"flags": "TPAC"}, "unknown_flags"),
        ({"priceNotation": 2}, "unknown_price_unit"),
        ({"tradingSystem": "XOFF"}, "unsupported_trading_system"),
    ],
)
def test_excluded_records_still_make_a_minute_incomplete(
    tmp_path: Path,
    change: dict[str, object],
    reason: str,
) -> None:
    with IntradayStore(tmp_path) as store:
        store.ingest_file(NAME, zipped(row(**change)))
        bar = store.query_bars(ISIN)[0]
        assert bar.open is None and bar.volume == 0 and bar.trade_count == 0
        assert bar.quality == "incomplete" and reason in bar.quality_reasons


def test_cross_file_conflict_late_replacement_and_removal(tmp_path: Path) -> None:
    with IntradayStore(tmp_path) as store:
        store.ingest_file(NAME, zipped(row()))
        first = store.query_bars(ISIN)[0]
        result = store.ingest_file(
            NAME2,
            zipped(
                row(
                    price="26875",
                    tradingDateAndTime="2026-11-13T09:59:59.999999999Z",
                )
            ),
        )
        assert len(result.changed_bars) == 2
        assert all("ambiguous_trade_key" in b.quality_reasons for b in store.query_bars(ISIN))
        assert all(b.trade_count == 0 for b in store.query_bars(ISIN))
        # A revision removes the conflicting key and restores both affected minutes.
        store.ingest_file(
            NAME2,
            zipped(
                row(
                    transactionIdentificationCode="different",
                    price="9",
                    tradingDateAndTime="2026-11-13T10:01:00Z",
                )
            ),
        )
        current = store.query_bars(ISIN)
        assert [b.minute_start_ns for b in current] == [MINUTE, MINUTE + 60_000_000_000]
        assert current[0].revision > first.revision and current[0].trade_count == 1
        result = store.ingest_file(NAME, zipped())
        assert result.removed_minutes[0].minute_start_ns == MINUTE
        assert len(store.query_bars(ISIN)) == 1


def test_transaction_rollback_corruption_budget_and_recovery(tmp_path: Path) -> None:
    with IntradayStore(tmp_path) as store:
        store.ingest_file(NAME, zipped(row()))
        before = store.query_bars(ISIN)
        store._connection.execute(
            "CREATE TRIGGER crash BEFORE INSERT ON bars "
            "BEGIN SELECT RAISE(ABORT, 'injected crash'); END"
        )
        with pytest.raises(StoreError):
            store.ingest_file(NAME, zipped(row(price="7")))
        assert store.query_bars(ISIN) == before
        store._connection.execute("DROP TRIGGER crash")
        for bad in (b"not gzip", zipped(row())[:-4], gzip.compress(b"{broken json\n")):
            with pytest.raises((ValueError, StoreError)):
                store.ingest_file(NAME, bad)
            assert store.query_bars(ISIN) == before
    with IntradayStore(tmp_path) as store:
        assert store.query_bars(ISIN) == before
        assert store.ingest_file(NAME, zipped(row(price="7"))).changed
    with IntradayStore(tmp_path / "full", raw_budget_bytes=1) as full:
        with pytest.raises(StoreError, match="storage_full"):
            full.ingest_file(NAME, zipped(row()))
        assert full.completed_files() == ()


def test_pagination_is_ascending_and_bounded(tmp_path: Path) -> None:
    with IntradayStore(tmp_path) as store:
        store.ingest_file(
            NAME,
            zipped(
                *(
                    row(
                        transactionIdentificationCode=str(i),
                        tradingDateAndTime=f"2026-11-13T10:{i:02d}:00Z",
                    )
                    for i in range(5)
                )
            ),
        )
        assert [b.minute_start_ns for b in store.query_bars(ISIN, limit=2)] == [
            MINUTE,
            MINUTE + 60_000_000_000,
        ]
        assert len(store.query_bars(ISIN, before_ns=MINUTE + 120_000_000_000)) == 2
        assert (
            len(
                store.query_bars(
                    ISIN, start_ns=MINUTE + 120_000_000_000, end_ns=MINUTE + 240_000_000_000
                )
            )
            == 2
        )
        with pytest.raises(ValueError):
            store.query_bars(ISIN, limit=10001)
        with pytest.raises(ValueError):
            store.query_bars("FDAX1!")


class Response:
    def __init__(self, status: int, data: bytes = b"", headers: dict[str, str] | None = None):
        self.status, self.data, self.headers = status, data, headers or {}
        self.content = self

    async def __aenter__(self) -> Response:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def iter_chunked(self, size: int):  # type: ignore[no-untyped-def]
        for offset in range(0, len(self.data), min(size, 17)):
            yield self.data[offset : offset + min(size, 17)]


class Session:
    def __init__(self, *responses: Response):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> Response:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    async def close(self) -> None:
        pass


def target(name: str = NAME) -> str:
    return (
        "https://storage.googleapis.com/"
        + "mv-cef-prod-europe-west3-private-min-by-min-files/DEUR/"
        + name
        + "?secret=do-not-log"
    )


async def test_transport_single_redirect_and_no_redirect_fallback() -> None:
    session = Session(Response(301, headers={"Location": target()}), Response(200, zipped(row())))
    transport = EurexDelayedTransport(session=session)
    data = await transport.download(NAME)
    assert data.raw_gzip == zipped(row())
    assert all(call[1]["allow_redirects"] is False for call in session.calls)
    assert "secret" not in repr(data)
    await transport.close()


@pytest.mark.parametrize(
    "location",
    [
        "http://storage.googleapis.com/mv-cef-prod-europe-west3-private-min-by-min-files/DEUR/"
        + NAME,
        "https://evil.example/" + NAME,
        "https://storage.googleapis.com/other/" + NAME,
        target(NAME2),
        "https://user@storage.googleapis.com/mv-cef-prod-europe-west3-private-min-by-min-files/DEUR/"
        + NAME,
    ],
)
async def test_transport_rejects_redirect_escape_without_leaking_query(location: str) -> None:
    session = Session(Response(301, headers={"Location": location}))
    with pytest.raises(TransportError) as caught:
        await EurexDelayedTransport(session=session).download(NAME)
    assert "secret" not in str(caught.value) and len(session.calls) == 1


@pytest.mark.parametrize(
    "raw", [b"<html>denied</html>", zipped(row())[:-3], gzip.compress(b"{bad}\n")]
)
async def test_transport_rejects_invalid_content(raw: bytes) -> None:
    transport = EurexDelayedTransport(
        session=Session(Response(301, headers={"Location": target()}), Response(200, raw))
    )
    with pytest.raises(TransportError):
        await transport.download(NAME)


async def test_transport_limits_and_second_redirect() -> None:
    for kwargs in ({"max_compressed_bytes": 10}, {"max_expanded_bytes": 10}):
        transport = EurexDelayedTransport(
            session=Session(
                Response(301, headers={"Location": target()}), Response(200, zipped(row()))
            ),
            **kwargs,
        )
        with pytest.raises(TransportError, match="oversized"):
            await transport.download(NAME)
    transport = EurexDelayedTransport(
        session=Session(
            Response(301, headers={"Location": target()}),
            Response(302, headers={"Location": target()}),
        )
    )
    with pytest.raises(TransportError):
        await transport.download(NAME)


async def test_transport_preserves_signed_encoding_and_refreshes_expired_signature_once() -> None:
    location = target() + "&opaque=a%2Fb%2Bc%3D"
    session = Session(
        Response(301, headers={"Location": location}),
        Response(400, b"<Error><Code>ExpiredToken</Code></Error>"),
        Response(301, headers={"Location": location}),
        Response(200, zipped(row())),
    )
    transport = EurexDelayedTransport(session=session, min_download_interval_s=0.01)
    result = await transport.download(NAME)
    assert result.raw_gzip == zipped(row())
    assert len(session.calls) == 4
    assert str(session.calls[1][0]) == location
    assert str(session.calls[3][0]) == location


class FakeTransport:
    def __init__(self) -> None:
        self.manifests = 0
        self.downloads: list[str] = []
        self.failure: TransportError | None = None

    async def fetch_manifest(self) -> tuple[str, ...]:
        self.manifests += 1
        return (NAME, NAME2, "DEUR-posttrade-2026-11-13T10_03.json.gz")

    async def download(self, name: str):  # type: ignore[no-untyped-def]
        from crocodile.equity.universal.live.eurex_delayed import DownloadedFile

        self.downloads.append(name)
        if self.failure:
            raise self.failure
        return DownloadedFile(name, zipped(row()), 1)

    async def close(self) -> None:
        pass


async def test_source_manifest_throttle_cached_batches_restart_and_backoff(tmp_path: Path) -> None:
    clock = [100.0]
    transport = FakeTransport()
    with IntradayStore(tmp_path) as store:
        source = EurexDelayedSource(store, transport=transport, clock=lambda: clock[0])
        result = await source.refresh(max_files=1)
        assert result.ingested_files == 1 and result.pending_files == 2
        assert transport.downloads[0].endswith("10_03.json.gz")
        assert result.manifest_gaps and result.declared_delay_s == 900
        await source.refresh(max_files=1)
        assert transport.manifests == 1
        transport.failure = TransportError("unavailable", code="http_503", retry_after_s=20)
        result = await source.refresh(max_files=1)
        assert result.status == "error"
        calls = len(transport.downloads)
        assert (await source.refresh()).status == "backoff"
        assert len(transport.downloads) == calls
        clock[0] += 61
        transport.failure = None
        await source.refresh()
        assert transport.manifests == 2
        await source.close()
    with IntradayStore(tmp_path) as store:
        fresh = FakeTransport()
        source = EurexDelayedSource(store, transport=fresh)
        assert (await source.refresh()).pending_files == 0
        assert fresh.downloads == []
        await source.close()


async def test_failed_manifest_stays_in_backoff_until_next_allowed_attempt(tmp_path: Path) -> None:
    class FailingManifest(FakeTransport):
        async def fetch_manifest(self) -> tuple[str, ...]:
            self.manifests += 1
            raise TransportError("busy", code="http_503")

    clock = [0.0]
    transport = FailingManifest()
    with IntradayStore(tmp_path) as store:
        source = EurexDelayedSource(store, transport=transport, clock=lambda: clock[0])
        assert (await source.refresh()).status == "error"
        clock[0] = 6
        result = await source.refresh()
        assert result.status == "backoff" and transport.manifests == 1
        clock[0] = 60
        assert (await source.refresh()).status == "error"
        assert transport.manifests == 2


async def test_retry_after_is_respected_and_production_session_has_no_cookies() -> None:
    transport = EurexDelayedTransport(
        session=Session(Response(429, headers={"Retry-After": "3600"}))
    )
    with pytest.raises(TransportError) as caught:
        await transport.fetch_manifest()
    assert caught.value.retry_after_s == 3600
    production = EurexDelayedTransport()
    client = production._client()
    assert type(client.cookie_jar).__name__ == "DummyCookieJar"
    assert "Origin" not in client.headers and "Referer" not in client.headers
    assert "Cookie" not in client.headers
    await production.close()


async def test_concurrent_refresh_is_single_collector_and_cancellation_is_clean(
    tmp_path: Path,
) -> None:
    class PausedTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.entered, self.release = asyncio.Event(), asyncio.Event()

        async def download(self, name: str):  # type: ignore[no-untyped-def]
            self.entered.set()
            await self.release.wait()
            return await super().download(name)

    transport = PausedTransport()
    with IntradayStore(tmp_path) as store:
        source = EurexDelayedSource(store, transport=transport)
        task = asyncio.create_task(source.refresh(max_files=1))
        await transport.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.completed_files() == ()
        transport.release.set()
        results = await asyncio.gather(source.refresh(max_files=1), source.refresh(max_files=1))
        assert [result.ingested_files for result in results] == [1, 1]
        assert transport.manifests == 1 and len(set(transport.downloads)) == 2
        await source.close()


def test_process_crash_before_commit_leaves_original_visible(tmp_path: Path) -> None:
    with IntradayStore(tmp_path) as store:
        store.ingest_file(NAME, zipped(row()))
        before = store.query_bars(ISIN)
    script = (
        "import os; from pathlib import Path; "
        "from crocodile.equity.universal.live.intraday_store import IntradayStore; "
        "store=IntradayStore(Path(os.environ['EUREX_CRASH_ROOT'])); "
        "store._recompute=lambda minute:os._exit(77); "
        f"store.ingest_file({NAME!r},{zipped(row(price='99'))!r})"
    )
    import os

    environment = dict(os.environ, EUREX_CRASH_ROOT=str(tmp_path), PYTHONPATH="src")
    process = subprocess.run([sys.executable, "-c", script], env=environment, check=False)
    assert process.returncode == 77
    with IntradayStore(tmp_path) as store:
        assert store.query_bars(ISIN) == before
        assert store.ingest_file(NAME, zipped(row(price="99"))).changed


def test_json_constants_crc_and_raw_archive_corruption(tmp_path: Path) -> None:
    raw = bytearray(zipped(row()))
    raw[-8] ^= 1
    with pytest.raises(ValueError):
        parse_file(NAME, bytes(raw), captured_ns=1)
    with pytest.raises(ValueError):
        parse_file(NAME, gzip.compress(b'{"unknown":NaN}\n'), captured_ns=1)
    with IntradayStore(tmp_path) as store:
        store.ingest_file(NAME, zipped(row()))
        archive = next((tmp_path / "raw").glob("*.gz"))
        archive.write_bytes(b"corrupted")
        with pytest.raises(StoreError, match="corrupt_raw_file"):
            store.ingest_file(NAME, zipped(row()))


async def test_ingest_worker_keeps_loop_responsive_and_cancellation_waits_for_commit(
    tmp_path: Path,
) -> None:
    entered, release = threading.Event(), threading.Event()
    timed_out = []
    with IntradayStore(tmp_path) as store:
        ingest = store.ingest_file

        def slow_ingest(*args, **kwargs):  # type: ignore[no-untyped-def]
            entered.set()
            if not release.wait(timeout=1):
                timed_out.append(True)
            return ingest(*args, **kwargs)

        store.ingest_file = slow_ingest  # type: ignore[method-assign]
        source = EurexDelayedSource(store, transport=FakeTransport())
        task = asyncio.create_task(source.refresh(max_files=1))
        await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=2)
        await asyncio.sleep(0.02)
        assert not timed_out, "synchronous import blocked the event loop"
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done(), "cancelled refresh abandoned its ingest worker"
        close_task = asyncio.create_task(source.close())
        await asyncio.sleep(0.01)
        assert not close_task.done(), "close released the lock before import finished"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await close_task
        assert len(store.completed_files()) == 1
        assert store.observation_count() == 1


async def test_source_revalidates_recent_completed_files_once_per_manifest_poll(
    tmp_path: Path,
) -> None:
    class RevisingTransport(FakeTransport):
        price = "1"

        async def fetch_manifest(self) -> tuple[str, ...]:
            self.manifests += 1
            return (NAME,)

        async def download(self, name: str):  # type: ignore[no-untyped-def]
            from crocodile.equity.universal.live.eurex_delayed import DownloadedFile

            self.downloads.append(name)
            return DownloadedFile(name, zipped(row(price=self.price)), 1)

    clock = [0.0]
    transport = RevisingTransport()
    with IntradayStore(tmp_path) as store:
        source = EurexDelayedSource(store, transport=transport, clock=lambda: clock[0])
        await source.refresh()
        assert store.query_bars(ISIN)[0].close == 1
        transport.price = "7"
        assert (await source.refresh()).ingested_files == 0
        assert len(transport.downloads) == 1
        clock[0] = 60
        result = await source.refresh()
        assert result.ingested_files == 1
        assert result.changed_bars[0].close == 7 and result.changed_bars[0].revision == 2
        await source.refresh()
        assert len(transport.downloads) == 2


@pytest.mark.parametrize(
    "invalid",
    [
        {"error": "unexpected upstream response"},
        {"instrumentIdentificationCode": None},
        row(instrumentIdentificationCode=123),
        row(tradingDateAndTime=None),
    ],
)
async def test_invalid_structure_cannot_replace_active_file_or_be_marked_completed(
    tmp_path: Path,
    invalid: dict[str, object],
) -> None:
    malformed = zipped(invalid)
    with IntradayStore(tmp_path / "existing") as store:
        store.ingest_file(NAME, zipped(row()))
        before = store.query_bars(ISIN)
        with pytest.raises(ValueError):
            store.ingest_file(NAME, malformed)
        assert store.query_bars(ISIN) == before
        assert not store.ingest_file(NAME, zipped(row())).changed
    session = Session(
        Response(200, json.dumps({"CurrentFiles": [NAME]}).encode()),
        Response(301, headers={"Location": target()}),
        Response(200, malformed),
    )
    with IntradayStore(tmp_path / "fresh") as store:
        source = EurexDelayedSource(store, transport=EurexDelayedTransport(session=session))
        result = await source.refresh()
        assert result.status == "error" and result.error_code is not None
        assert result.ingested_files == result.completed_files == 0
        assert store.completed_files() == () and store.query_bars(ISIN) == ()
        await source.close()


def test_legitimate_unknown_instruments_and_empty_files_remain_accepted(tmp_path: Path) -> None:
    unknown = row(instrumentIdentificationCode="DE000UNKNOWN", price=None, flags="TPAC")
    with IntradayStore(tmp_path) as store:
        assert store.ingest_file(NAME, zipped(unknown)).indexed_rows == 0
        assert store.ingest_file(NAME2, gzip.compress(b"", mtime=0)).indexed_rows == 0
        assert len(store.completed_files()) == 2
        assert store.query_bars(ISIN) == ()


def test_exact_empty_pretrade_marker_is_not_a_trade_or_an_arbitrary_schema_bypass(
    tmp_path: Path,
) -> None:
    # Observed inside the official 2026-11-13T17_05 post-trade file.
    marker = {
        "messageId": "pretrade",
        "quantity": 0.0,
        "notionalAmount": 0.0,
        "price": 0.0,
        "tradingDateAndTime": None,
        "transactionIdentificationCode": "",
    }
    with IntradayStore(tmp_path) as store:
        result = store.ingest_file(NAME, zipped(row(), marker))
        assert result.indexed_rows == 1
        before = store.query_bars(ISIN)
        assert len(before) == 1 and before[0].trade_count == 1
        assert not store.ingest_file(NAME, zipped(row(), marker)).changed
        for changed in (
            {**marker, "quantity": 1},
            {**marker, "price": False},
            {**marker, "instrumentIdentificationCode": ISIN},
            {**marker, "messageId": "posttrade"},
            {**marker, "transactionIdentificationCode": "potential-trade"},
        ):
            with pytest.raises(ValueError):
                store.ingest_file(NAME, zipped(changed))
            assert store.query_bars(ISIN) == before
        actual_name = "DEUR-posttrade-2026-11-13T17_05.json.gz"
        actual_raw = (FIXTURES / actual_name).read_bytes()
        assert store.ingest_file(actual_name, actual_raw).indexed_rows == 36
        assert not store.ingest_file(actual_name, actual_raw).changed


def test_observed_instrumentless_pretrade_shapes_stay_out_of_native_trade_totals(
    tmp_path: Path,
) -> None:
    partial = {
        "messageId": "pretrade",
        "tradingDateAndTime": "2026-11-13T14:32:31.934314218Z",
        "transactionIdentificationCode": "1000000000000149005800179458035193431421810603979815",
    }
    markers = [
        {"messageId": "pretrade", "sourceName": "EUR"},
        partial,
        {**partial, "quantity": 3000, "notionalAmount": 19200000},
        {**partial, "quantity": 6, "notionalAmount": 33900, "price": 0},
    ]
    with IntradayStore(tmp_path) as store:
        assert store.ingest_file(NAME, zipped(row(), *markers)).indexed_rows == 1
        before = store.query_bars(ISIN)
        assert before[0].trade_count == 1 and before[0].volume == Decimal("0.1")
        for invalid in (
            {"messageId": "pretrade", "sourceName": "OTHER"},
            {**partial, "instrumentIdentificationCode": ISIN},
            {**partial, "tradingDateAndTime": "invalid"},
            {**partial, "transactionIdentificationCode": None},
            {**partial, "quantity": "6", "notionalAmount": 33900},
            {**partial, "quantity": False, "notionalAmount": 33900},
            {**partial, "quantity": -1, "notionalAmount": 33900},
            {**partial, "quantity": 6},
            {"messageId": "pretrade", "error": "unrecognized response"},
            row(messageId="pretrade"),
        ):
            with pytest.raises(ValueError):
                store.ingest_file(NAME, zipped(invalid))
            assert store.query_bars(ISIN) == before
        actual_name = "DEUR-posttrade-2026-11-13T16_44.json.gz"
        actual_raw = (FIXTURES / actual_name).read_bytes()
        assert store.ingest_file(actual_name, actual_raw).indexed_rows == 50
        assert not store.ingest_file(actual_name, actual_raw).changed


async def test_transport_body_validation_is_responsive_and_cancellation_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crocodile.equity.universal.live import eurex_delayed

    entered, release = threading.Event(), threading.Event()
    timed_out = []
    original = eurex_delayed._validate_body

    def slow_validation(expanded: bytes) -> None:
        entered.set()
        if not release.wait(timeout=1):
            timed_out.append(True)
        original(expanded)

    monkeypatch.setattr(eurex_delayed, "_validate_body", slow_validation)
    transport = EurexDelayedTransport(
        session=Session(
            Response(301, headers={"Location": target()}),
            Response(200, zipped(row())),
        )
    )
    task = asyncio.create_task(transport.download(NAME))
    await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=2)
    await asyncio.sleep(0.02)
    assert not timed_out, "body validation blocked the event loop"
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done(), "transport abandoned its body-validation worker"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_download_pacing_serializes_before_requesting_signatures(monkeypatch):
    import time

    from crocodile.equity.universal.live.eurex_delayed import DownloadedFile

    transport = EurexDelayedTransport(min_download_interval_s=0.04)
    starts = []
    active = 0
    peak = 0

    async def download_file(file_name):
        nonlocal active, peak
        starts.append(time.monotonic())
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        active -= 1
        return DownloadedFile(file_name, gzip.compress(b""), 1)

    monkeypatch.setattr(transport, "_download_file", download_file)
    await asyncio.gather(transport.download(NAME), transport.download(NAME2))
    # The monotonic clock ticks every ~16 ms on Windows: allow one tick of it.
    tick = time.get_clock_info("monotonic").resolution
    assert len(starts) == 2 and starts[1] - starts[0] >= 0.04 - tick - 0.001
    assert peak == 1
    for value in (-1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            EurexDelayedTransport(min_download_interval_s=value)


def test_one_trade_stated_twice_counts_once(tmp_path: Path) -> None:
    # The same trade, byte for byte, in a second file and then twice inside a third one: the
    # publisher restated it, it did not trade again.
    with IntradayStore(tmp_path) as store:
        store.ingest_file(NAME, zipped(row()))
        base = store.query_bars(ISIN)[0]
        assert base.volume == Decimal("0.1") and base.trade_count == 1
        store.ingest_file(NAME2, zipped(row()))
        store.ingest_file("DEUR-posttrade-2026-11-13T10_02.json.gz", zipped(row(), row()))
        bar = store.query_bars(ISIN)[0]
        assert bar.volume == Decimal("0.1") and bar.trade_count == 1
        assert bar.quality == "observed" and bar.quality_reasons == ()
        # A restatement is not an excluded record.
        assert bar.excluded_count == 0
        assert bar.vwap == base.vwap


def test_a_restated_trade_beside_others_leaves_their_totals_alone() -> None:
    other = row(transactionIdentificationCode="two", quantity="2")
    rows = parse_file(NAME, zipped(row(), row(), other), captured_ns=1)
    bar = reduce_minute(ISIN, MINUTE, rows)
    assert bar.trade_count == 2 and bar.volume == Decimal("2.1")
    # Copies that differ stay a conflict: neither counts.
    differing = parse_file(NAME, zipped(row(), row(price="26875")), captured_ns=1)
    conflict = reduce_minute(ISIN, MINUTE, differing)
    assert conflict.trade_count == 0 and "ambiguous_trade_key" in conflict.quality_reasons


def test_a_backward_page_is_the_newest_minutes_below_the_cursor(tmp_path: Path) -> None:
    with IntradayStore(tmp_path) as store:
        store.ingest_file(
            NAME,
            zipped(
                *(
                    row(
                        transactionIdentificationCode=str(i),
                        tradingDateAndTime=f"2026-11-13T10:{i:02d}:00Z",
                    )
                    for i in range(5)
                )
            ),
        )
        page = store.query_bars(ISIN, before_ns=MINUTE + 4 * 60_000_000_000, limit=2)
        assert [b.minute_start_ns for b in page] == [
            MINUTE + 2 * 60_000_000_000,
            MINUTE + 3 * 60_000_000_000,
        ]
        older = store.query_bars(ISIN, before_ns=page[0].minute_start_ns, limit=2)
        assert [b.minute_start_ns for b in older] == [MINUTE, MINUTE + 60_000_000_000]


async def test_the_expired_signature_retry_waits_its_turn() -> None:
    import time

    location = target()
    session = Session(
        Response(301, headers={"Location": location}),
        Response(400, b"<Error><Code>ExpiredToken</Code></Error>"),
        Response(301, headers={"Location": location}),
        Response(200, zipped(row())),
    )
    stamps: list[float] = []
    original = session.get

    def timed(url: str, **kwargs: object) -> Response:
        stamps.append(time.monotonic())
        return original(url, **kwargs)

    session.get = timed  # type: ignore[method-assign]
    transport = EurexDelayedTransport(session=session, min_download_interval_s=0.05)
    await transport.download(NAME)
    assert len(stamps) == 4
    # The second signature request waited the cadence after the first one.
    tick = time.get_clock_info("monotonic").resolution
    assert stamps[2] - stamps[0] >= 0.05 - tick - 0.001
