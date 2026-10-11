import msgspec
import pytest

from crocodile.core.capture.replay import RawReplay
from crocodile.core.capture.schema import ApplicationDecision, JournalRecord, MarketFrame, RawFrame
from crocodile.core.errors import CaptureIntegrityError
from crocodile.core.sink.memory import MemorySink
from crocodile.crypto.exchanges.binance.replay import BinanceReplayDecoder
from crocodile.crypto.instruments.registry import InstrumentRegistry


def test_live_book_matches_offline_replay(recorded_json_session, network_forbidden):
    reader, expected = recorded_json_session
    replay = RawReplay(reader, {"binance-json": BinanceReplayDecoder()})
    actual = list(replay.records())
    assert [msgspec.msgpack.encode(r) for r in actual] == [
        msgspec.msgpack.encode(r) for r in expected
    ]
    assert replay.decoders["s1"].states["BTCUSDT"].bids == {10.0: 210.0}
    assert replay.decoders["s1"].states["BTCUSDT"].asks == {}


def test_decoder_requires_snapshot_anchor():
    origin = JournalRecord(
        ordinal=1,
        event=MarketFrame(
            stream_id="s1",
            frame=RawFrame(
                payload=b'{"e":"depthUpdate","s":"BTCUSDT","U":101,"u":101,"b":[],"a":[]}',
                frame_kind="text",
                received_utc_ns=10,
                received_monotonic_ns=20,
                connection_id="c1",
                receive_ordinal=1,
            ),
            observed_utc_ns=10,
            observed_monotonic_ns=20,
        ),
    )
    decision = ApplicationDecision(
        origin_ordinal=1, record_indices=(0,), action="apply", reason="book_sync"
    )
    with pytest.raises(CaptureIntegrityError, match="anchor"):
        list(BinanceReplayDecoder().apply(decision, origin, InstrumentRegistry()))


@pytest.mark.asyncio
async def test_original_pacing_cannot_invent_legacy_receive_clock(recorded_json_session):
    reader, _ = recorded_json_session
    with pytest.raises(CaptureIntegrityError, match="pacing"):
        await RawReplay(reader, {"binance-json": BinanceReplayDecoder()}).run(
            MemorySink(), pacing="original"
        )


def test_unsupported_decoder_version_fails(recorded_json_session):
    reader, _ = recorded_json_session
    decoder = BinanceReplayDecoder()
    decoder.version = 99
    with pytest.raises(CaptureIntegrityError, match="version"):
        list(RawReplay(reader, {"binance-json": decoder}).records())


@pytest.mark.asyncio
async def test_pacing_uses_monotonic_arrivals_despite_utc_rollback(
    tmp_path, capture_context, monkeypatch
):
    from crocodile.core.capture.reader import CaptureReader
    from crocodile.core.capture.writer import CaptureWriter

    writer = await CaptureWriter.create(tmp_path, capture_context)
    for i, (utc, mono) in enumerate(((100, 10), (50, 20_000_010)), 1):
        ordinal = await writer.append(
            MarketFrame(
                stream_id="s1",
                frame=RawFrame(
                    payload=b"{}",
                    frame_kind="text",
                    received_utc_ns=utc,
                    received_monotonic_ns=mono,
                    connection_id="c1",
                    receive_ordinal=i,
                ),
                observed_utc_ns=utc,
                observed_monotonic_ns=mono,
            )
        )
        await writer.append(
            ApplicationDecision(
                origin_ordinal=ordinal, record_indices=(), action="apply", reason="market"
            )
        )
    await writer.close()
    sleeps = []

    async def record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("crocodile.core.capture.replay.asyncio.sleep", record_sleep)
    summary = await RawReplay(
        CaptureReader(tmp_path), {"binance-json": BinanceReplayDecoder()}
    ).run(MemorySink(), pacing="original")
    assert sleeps == [0.02]
    assert summary.last_ordinal == 4


@pytest.mark.asyncio
async def test_future_origin_is_an_integrity_failure(tmp_path, capture_context):
    from crocodile.core.capture.reader import CaptureReader
    from crocodile.core.capture.writer import CaptureWriter

    writer = await CaptureWriter.create(tmp_path, capture_context)
    await writer.append(
        ApplicationDecision(origin_ordinal=2, record_indices=(), action="apply", reason="market")
    )
    await writer.close()
    with pytest.raises(CaptureIntegrityError, match="future"):
        list(RawReplay(CaptureReader(tmp_path), {"binance-json": BinanceReplayDecoder()}).records())
