from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import traceback
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Any, Literal

import aiohttp

from crocodile.core.capture.classification import FrameClass, classify_frame
from crocodile.core.capture.emitter import CaptureEmitter
from crocodile.core.capture.schema import (
    ApplicationDecision,
    ControlMetadata,
    MarketFrame,
    RawFrame,
    RegistryCheckpoint,
)
from crocodile.core.capture.writer import CaptureWriter
from crocodile.core.errors import (
    CaptureError,
    ConnectorError,
    FatalConnectorError,
    SinkError,
    TransientConnectorError,
)
from crocodile.core.ingest.deadletter import DeadLetterQueue
from crocodile.core.ingest.transport import AiohttpWsTransport, Transport, iter_transport_frames
from crocodile.core.schema.records import Record
from crocodile.core.sink.base import Sink
from crocodile.core.util.time import now_ns

# Still under crypcodile until Task 15 relocates the remaining packages.
from crocodile.crypto.instruments.registry import Instrument, InstrumentRegistry

__all__ = [
    "Connector",
    "ConnectorError",
    "FatalConnectorError",
    "SinkError",
    "TransientConnectorError",
    "backoff_delays",
    "http_get_helper",
]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessedInput:
    origin_ordinal: int | None
    frame: RawFrame
    local_ts: int
    admitted: bool = True
    generation: int | None = None


def backoff_delays(
    attempt: int,
    base: float = 1.0,
    cap: float = 30.0,
    jitter: float = 0.25,
    rand: float = 0.0,
) -> float:
    raw = min(cap, base * float(2**attempt))
    # Apply the cap AFTER jitter too: jitter can otherwise push a capped delay
    # above `cap` (e.g. raw=30, jitter=0.25, rand→1.0 → 37.5s), defeating the
    # documented ceiling during sustained reconnect failures.
    return min(cap, raw * (1.0 + jitter * rand))


async def http_get_helper(
    url: str,
    params: dict[str, Any] | None = None,
    session: aiohttp.ClientSession | None = None,
    max_retries: int = 3,
    timeout_sec: float = 10.0,
) -> Any:
    timeout = aiohttp.ClientTimeout(total=timeout_sec)

    async def run_req(sess: aiohttp.ClientSession) -> Any:
        for attempt in range(max_retries):
            try:
                async with sess.get(url, params=params, timeout=timeout) as resp:
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", 1.0))
                        await asyncio.sleep(retry_after)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except (aiohttp.ClientError, TimeoutError) as e:
                if attempt == max_retries - 1:
                    raise e
                await asyncio.sleep(0.5 * (2**attempt))

    if session is not None:
        return await run_req(session)
    else:
        async with aiohttp.ClientSession() as sess:
            return await run_req(sess)


class Connector(ABC):
    name: str
    ws_url: str
    rest_url: str

    def __init__(
        self,
        symbols: list[str],
        channels: list[str],
        out: Sink,
        registry: InstrumentRegistry,
    ) -> None:
        self.symbols = symbols
        self.channels = channels
        self.out = out
        self.registry = registry
        self.transport: Transport | None = None
        self._dlq: DeadLetterQueue = DeadLetterQueue()
        self._session: aiohttp.ClientSession | None = None
        self.capture: CaptureWriter | None = None
        self.capture_stream_id: str | None = None
        self._origin_ordinal: int | None = None
        self._received_generation: int | None = None
        self._processor_budget = 32 << 20

    @property
    def http_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def make_transport(self) -> Transport:
        return AiohttpWsTransport(self.ws_url)

    async def http_get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        max_retries: int = 3,
        timeout_sec: float = 10.0,
    ) -> Any:
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        for attempt in range(max_retries):
            try:
                async with self.http_session.get(url, params=params, timeout=timeout) as resp:
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", 1.0))
                        await asyncio.sleep(retry_after)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except (aiohttp.ClientError, TimeoutError) as e:
                if attempt == max_retries - 1:
                    raise e
                await asyncio.sleep(0.5 * (2**attempt))

    @abstractmethod
    def normalize(self, msg: object, local_ts: int) -> Iterable[Record]: ...

    @abstractmethod
    async def list_instruments(self) -> list[Instrument]: ...

    def subscribe_channels(self) -> list[str] | list[dict[str, str]]:
        """Return the WS channel descriptors this connector will subscribe to.

        Override in concrete connectors.  Not abstract so that future connectors
        are not forced to implement it before they are ready.

        String-arg connectors (Deribit, Binance, Bybit) return ``list[str]``.
        Dict-arg connectors (OKX) return ``list[dict[str, str]]`` because the
        OKX subscribe frame uses ``{"channel": "...", "instId": "..."}`` objects
        rather than plain topic strings.  The ABC is widened to accommodate both
        forms so the override in OKXConnector does not need a ``# type: ignore``
        suppression.
        """
        raise NotImplementedError

    @abstractmethod
    async def _subscribe(self, transport: Transport) -> None:
        """Send exchange-specific subscribe frames over *transport*.

        Each exchange uses a completely different wire format for subscription
        (Deribit: JSON-RPC 2.0 ``public/subscribe``; Binance: ``{"method":
        "SUBSCRIBE", "params": [...]}``; Bybit/OKX/Coinbase differ again —
        appendix §4 table, §3.2).  This method is therefore abstract: every
        concrete connector is responsible for composing and sending its own
        subscribe frame(s).  A connector that needs no subscription (e.g. a
        pure pull source) should implement an explicit no-op.
        """

    async def _handle_message(self, msg: object, local_ts: int) -> None:
        """Process one decoded WS message and emit records to ``self.out``.

        Default: ``normalize(msg)`` → ``out.put`` for each record.

        **Integration point for book resync:** connectors that need async
        side-effects during the message loop (e.g. Binance depth sequence-gap
        recovery via :class:`~crocodile.core.ingest.gap_bridge.BookResyncBridge`)
        should override this method.  Keep the default path for non-book
        channels so existing normalizers stay pure/sync.

        Called from :meth:`run` inside the normalize-error try block; exceptions
        here are DLQ'd and the loop continues.
        """
        await self._emit_records(list(self.normalize(msg, local_ts)))

    async def _emit_records(
        self,
        records: list[Record],
        *,
        action: Literal["apply", "drop", "snapshot", "gap"] = "apply",
        reason: str = "market",
        origin_ordinal: int | None = None,
    ) -> None:
        origin = self._origin_ordinal if origin_ordinal is None else origin_ordinal
        try:
            if self.capture is not None:
                if origin is None:
                    raise CaptureError("Canonical emission has no recorded origin")
                decision = ApplicationDecision(
                    origin_ordinal=origin,
                    record_indices=tuple(range(len(records))),
                    action=action,
                    reason=reason,
                )
                await CaptureEmitter(self.capture, self.out).emit(decision, records)
            else:
                for record in records:
                    await self.out.put(record)
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"{self.name}: sink write failed") from exc

    def _decode_frame(self, frame: RawFrame) -> object:
        return json.loads(frame.payload)

    def _classify_frame(self, frame: RawFrame, msg: object | None) -> FrameClass:
        source = self.name
        if (
            source == "deribit"
            and self.capture is None
            and not getattr(self, "_mixed_controls", False)
        ):
            source = "public-json"
        return classify_frame(source, frame, msg)

    async def _handle_control_message(self, msg: object, frame: RawFrame) -> bool:
        return False

    def _market_ready(self, frame: RawFrame, msg: object | None) -> bool:
        return True

    def _market_generation(self, msg: object | None) -> int | None:
        return None

    async def _on_connection_close(self) -> None:
        return None

    async def _prepare_connection(self) -> None:
        return None

    async def _drain_market(self) -> None:
        return None

    async def _watch_market_failure(self) -> None:
        await asyncio.Event().wait()

    def _stream_id(self) -> str:
        if self.capture is None:
            raise CaptureError("Missing capture context")
        if self.capture_stream_id is not None:
            if self.capture_stream_id in {s.stream_id for s in self.capture.context.streams}:
                return self.capture_stream_id
            raise CaptureError("Unknown capture stream")
        source = getattr(self, "_venue", self.name)
        matches = [s.stream_id for s in self.capture.context.streams if s.source == source]
        if len(matches) != 1:
            raise CaptureError("Ambiguous capture stream")
        return matches[0]

    async def _run_connection(self, transport: Transport) -> None:
        queue: asyncio.Queue[ProcessedInput | None] = asyncio.Queue()
        pending_bytes = 0

        async def read() -> None:
            nonlocal pending_bytes
            async for frame in iter_transport_frames(transport):
                local_ts = frame.received_utc_ns if frame.received_utc_ns is not None else now_ns()
                msg: object | None = None
                if frame.frame_kind != "binary":
                    try:
                        msg = json.loads(frame.payload)
                    except (ValueError, UnicodeDecodeError):
                        pass
                kind = self._classify_frame(frame, msg)
                if kind != "market":
                    if self.capture is not None:
                        request_id = (
                            str(msg["id"])
                            if (isinstance(msg, dict) and isinstance(msg.get("id"), int))
                            else None
                        )
                        await self.capture.append(
                            ControlMetadata(
                                stream_id=self._stream_id(),
                                kind=kind,
                                connection_id=frame.connection_id,
                                request_id=request_id,
                                status="received",
                                observed_utc_ns=local_ts,
                            )
                        )
                    handled = await self._handle_control_message(msg, frame)
                    if not handled and isinstance(msg, dict) and msg.get("error") is not None:
                        error = msg["error"]
                        code = error.get("code") if isinstance(error, dict) else None
                        log.warning(
                            "%s: exchange rejected request (code=%s)",
                            self.name,
                            code if isinstance(code, int) else "unknown",
                        )
                    continue
                origin = None
                if self.capture is not None:
                    origin = await self.capture.append(
                        MarketFrame(
                            stream_id=self._stream_id(),
                            frame=frame,
                            observed_utc_ns=local_ts,
                            observed_monotonic_ns=time.monotonic_ns(),
                        )
                    )
                size = len(frame.payload) + 256
                if pending_bytes + size > self._processor_budget:
                    error_type = CaptureError if self.capture is not None else FatalConnectorError
                    raise error_type("Market processor byte queue overflow")
                pending_bytes += size
                queue.put_nowait(
                    ProcessedInput(
                        origin,
                        frame,
                        local_ts,
                        self._market_ready(frame, msg),
                        self._market_generation(msg),
                    )
                )
            queue.put_nowait(None)

        async def process() -> None:
            nonlocal pending_bytes
            while (item := await queue.get()) is not None:
                self._origin_ordinal = item.origin_ordinal
                self._received_generation = item.generation
                try:
                    if not item.admitted:
                        await self._emit_records([], action="drop", reason="not_ready")
                        continue
                    msg = self._decode_frame(item.frame)
                    await self._handle_message(msg, item.local_ts)
                except (FatalConnectorError, CaptureError):
                    raise
                except Exception as exc:
                    await self._dlq.put(
                        item.local_ts,
                        item.frame.payload,
                        type(exc).__name__,
                        traceback.format_exc(),
                    )
                    log.debug("DLQ: market processing failed (%s)", type(exc).__name__)
                finally:
                    pending_bytes -= len(item.frame.payload) + 256
                    self._origin_ordinal = None
                    self._received_generation = None

        try:
            async with asyncio.TaskGroup() as group:
                reader = group.create_task(read())
                processor = group.create_task(process())
                watcher = group.create_task(self.capture.wait_failure()) if self.capture else None
                background = group.create_task(self._watch_market_failure())
                await self._subscribe(transport)
                await reader
                await processor
                await self._drain_market()
                background.cancel()
                if watcher is not None:
                    watcher.cancel()
        except ExceptionGroup as exc:

            def leaves(group: BaseExceptionGroup) -> list[BaseException]:
                return [
                    leaf
                    for e in group.exceptions
                    for leaf in (leaves(e) if isinstance(e, BaseExceptionGroup) else [e])
                ]

            errors = leaves(exc)
            for error in errors:
                if isinstance(error, (CaptureError, FatalConnectorError)):
                    raise error from None
            raise errors[0] from None

    async def backfill(
        self,
        channel: str,
        symbol: str,
        start_ns: int,
        end_ns: int,
    ) -> AsyncIterator[Record]:
        raise NotImplementedError
        yield  # pragma: no cover  (makes this an async generator)

    async def run(self, max_reconnects: int = -1) -> None:
        """Supervised run loop.

        Connects the transport, subscribes, then drains frames into the sink.
        On exception: exponential backoff then reconnect (up to max_reconnects).
        max_reconnects=-1 means unlimited; max_reconnects=0 means no reconnect.
        Unparseable frames go to the DLQ; the loop continues.
        ``transport.close()`` is always called — on clean exit and on exception
        — via a ``try/finally`` block so that socket handles are never leaked.

        A :class:`FatalConnectorError` is the one failure that ends the loop
        outright, however many reconnects remain.
        """
        attempt = 0
        while True:
            transport = self.transport
            if transport is None:
                raise RuntimeError("No transport configured; set conn.transport before run()")
            try:
                await self._prepare_connection()
                if self.capture is not None:
                    await self.capture.append(
                        RegistryCheckpoint(instruments=self.registry.instruments())
                    )
                await transport.connect()
                if self.capture is not None:
                    await self.capture.append(
                        ControlMetadata(
                            stream_id=self._stream_id(),
                            kind="connected",
                            connection_id=getattr(transport, "_connection_id", None),
                            request_id=None,
                            status="connected",
                            observed_utc_ns=now_ns(),
                        )
                    )
                await self._run_connection(transport)
                break

            except (FatalConnectorError, CaptureError):
                # Authentication or configuration is wrong. Reconnecting cannot fix
                # it and would hammer the venue; surface it to the caller.
                raise
            except Exception as exc:
                log.warning(
                    "Connector %s error (attempt %d, type=%s)",
                    self.name,
                    attempt,
                    type(exc).__name__,
                )
                if max_reconnects == 0 or (max_reconnects > 0 and attempt >= max_reconnects):
                    raise
                delay = backoff_delays(attempt, jitter=0.25, rand=random.random())
                log.info("Reconnecting in %.2fs...", delay)
                await asyncio.sleep(delay)
                attempt += 1
            finally:
                await self._on_connection_close()
                await transport.close()
                if self._session is not None and not self._session.closed:
                    await self._session.close()
