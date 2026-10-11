from types import SimpleNamespace

import aiohttp
import pytest

from crocodile.core.errors import TransientConnectorError
from crocodile.core.ingest import transport as mod


class Socket:
    def __init__(self, messages):
        self.messages = messages

    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        for message in self.messages:
            yield message


async def test_legacy_bytes_transport_does_not_fabricate_receive_metadata():
    frame = await anext(mod.iter_transport_frames(mod.FakeTransport([b"x"])))
    assert frame.payload == b"x"
    assert frame.frame_kind == "unknown"
    assert frame.received_utc_ns is None
    assert frame.received_monotonic_ns is None
    assert frame.connection_id is None
    assert frame.receive_ordinal is None


async def test_iter_frames_preserves_text_binary_and_receive_metadata(monkeypatch):
    transport = mod.AiohttpWsTransport("wss://test.invalid")
    transport._ws = Socket([
        SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data="€"),
        SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=b"\xff\x00"),
    ])
    utc = iter([100, 90])
    mono = iter([1, 2])
    monkeypatch.setattr(mod.time, "time_ns", lambda: next(utc))
    monkeypatch.setattr(mod.time, "monotonic_ns", lambda: next(mono))
    frames = transport.iter_frames()
    first, second = await anext(frames), await anext(frames)
    assert (first.payload, second.payload) == (b"\xe2\x82\xac", b"\xff\x00")
    assert (first.frame_kind, second.frame_kind) == ("text", "binary")
    assert (first.received_utc_ns, second.received_utc_ns) == (100, 90)
    assert (first.received_monotonic_ns, second.received_monotonic_ns) == (1, 2)
    assert (first.receive_ordinal, second.receive_ordinal) == (1, 2)
    assert first.connection_id == second.connection_id
    assert first.connection_id
    await frames.aclose()


async def test_live_close_is_not_fixture_eof():
    transport = mod.AiohttpWsTransport("wss://test.invalid")
    transport._ws = Socket([])
    with pytest.raises(TransientConnectorError):
        await anext(transport.iter_frames())
    assert [b async for b in mod.FakeTransport([])] == []


async def test_two_readers_cannot_consume_same_socket():
    transport = mod.AiohttpWsTransport("wss://test.invalid")
    transport._ws = Socket([SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=b"x")])
    first = transport.iter_frames()
    await anext(first)
    with pytest.raises(RuntimeError, match="reader"):
        await anext(transport.iter_frames())
    await first.aclose()
