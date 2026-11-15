"""Bounded official delayed-file transport and shared incremental collector.

No scanner, Yahoo, cookies, origin spoofing, or arbitrary redirect following.
Signed target URLs exist only during one download and are never persisted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from functools import partial
from itertools import pairwise
from typing import Any, Protocol
from urllib.parse import urlsplit

import aiohttp
from yarl import URL

from .event_time_bars import MAX_COMPRESSED_BYTES, MAX_EXPANDED_BYTES, json_rows
from .intraday_models import (
    MINUTE_NS,
    BarEnvelope,
    CoverageGap,
    MinuteKey,
    RefreshStatus,
    file_minute_ns,
)
from .intraday_store import IntradayStore, StoreError

MANIFEST_URL = "https://mfs.deutsche-boerse.com/api/DEUR-posttrade"
DOWNLOAD_BASE = "https://mfs.deutsche-boerse.com/api/download/"
GCS_PREFIX = "/mv-cef-prod-europe-west3-private-min-by-min-files/DEUR/"
_DAILY = re.compile(r"DEUR-posttrade-daily-\d{4}-\d{2}-\d{2}\.json\.gz")


async def _store_call[T](operation: Callable[[], T]) -> T:
    """Keep the source lock until its worker exits, even under repeated cancel."""
    worker = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Retrieve any worker exception while preserving the caller's cancellation.
        if not worker.cancelled():
            worker.exception()
        raise


def _validate_body(expanded: bytes) -> None:
    for _ in json_rows(expanded):
        pass


class TransportError(RuntimeError):
    """Public messages/codes intentionally contain no request URL or response body."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retry_after_s: float | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after_s = retry_after_s
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    file_name: str
    raw_gzip: bytes
    captured_ns: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw_gzip).hexdigest()

    @property
    def stable_url(self) -> str:
        return DOWNLOAD_BASE + self.file_name

    @property
    def target_host(self) -> str:
        return "storage.googleapis.com"


class DelayedTransport(Protocol):
    async def fetch_manifest(self) -> tuple[str, ...]: ...
    async def download(self, file_name: str) -> DownloadedFile: ...
    async def close(self) -> None: ...


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
        if seconds != seconds or seconds == float("inf"):
            return None
        return max(5.0, seconds)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            seconds = (parsed - datetime.now(UTC)).total_seconds()
            return max(5.0, seconds)
        except (ValueError, TypeError, OverflowError):
            return None


def _http_error(status: int, headers: Any) -> TransportError:
    retryable = status in (408, 429, 500, 502, 503, 504)
    return TransportError(
        f"source HTTP {status}",
        code=f"http_{status}",
        retry_after_s=_retry_after(headers.get("Retry-After")),
        retryable=retryable,
    )


class EurexDelayedTransport:
    """Source-specific session; optional injected session is for deterministic tests.

    A production session always uses DummyCookieJar, disables auto decompression,
    sends no Origin/Referer, and explicitly rejects automatic redirects.
    """

    def __init__(
        self,
        *,
        session: Any = None,
        max_compressed_bytes: int = MAX_COMPRESSED_BYTES,
        max_expanded_bytes: int = MAX_EXPANDED_BYTES,
        min_download_interval_s: float = 2.5,
    ) -> None:
        if max_compressed_bytes < 1 or max_expanded_bytes < 1:
            raise ValueError("download bounds must be positive")
        self._session = session
        self._owned_session = session is None
        self.max_compressed_bytes = max_compressed_bytes
        self.max_expanded_bytes = max_expanded_bytes
        if not 0 <= min_download_interval_s < float("inf"):
            raise ValueError("download cadence must be finite and nonnegative")
        self._min_download_interval_s = min_download_interval_s
        self._download_lock = asyncio.Lock()
        self._next_download_at = 0.0

    def _client(self) -> Any:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=45, connect=10, sock_read=20),
                auto_decompress=False,
                trust_env=False,
                headers={"User-Agent": "Crocodile-Eurex-Research/0.1", "Accept": "*/*"},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and self._owned_session:
            await self._session.close()
        self._session = None

    async def fetch_manifest(self) -> tuple[str, ...]:
        try:
            async with self._client().get(MANIFEST_URL, allow_redirects=False) as response:
                if response.status != 200:
                    raise _http_error(response.status, response.headers)
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > 4 * 1024 * 1024:
                        raise TransportError(
                            "oversized manifest", code="oversized", retryable=False
                        )
            manifest = json.loads(body)
            if not isinstance(manifest, dict) or not isinstance(manifest.get("CurrentFiles"), list):
                raise ValueError("invalid_manifest")
            files = set()
            for name in manifest["CurrentFiles"]:
                if not isinstance(name, str):
                    raise ValueError("invalid_manifest")
                if _DAILY.fullmatch(name):
                    continue
                file_minute_ns(name)
                files.add(name)
            return tuple(sorted(files, reverse=True))
        except TransportError:
            raise
        except (ValueError, UnicodeError, RecursionError):
            raise TransportError(
                "invalid manifest", code="invalid_manifest", retryable=False
            ) from None
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise TransportError("source request failed", code="network_error") from None

    @staticmethod
    def _redirect_target(location: str, file_name: str) -> str:
        try:
            target = urlsplit(location)
            valid = (
                target.scheme == "https"
                and target.hostname == "storage.googleapis.com"
                and target.port in (None, 443)
                and target.username is None
                and target.password is None
                and not target.fragment
                and target.path == GCS_PREFIX + file_name
            )
            if not valid:
                raise ValueError("invalid target")
        except ValueError:
            raise TransportError(
                "redirect rejected", code="redirect_rejected", retryable=False
            ) from None
        return location

    async def download(self, file_name: str) -> DownloadedFile:
        file_minute_ns(file_name)
        # Pace before obtaining the two-second signature, never between redirect hops.
        async with self._download_lock:
            wait = self._next_download_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_download_at = time.monotonic() + self._min_download_interval_s
            return await self._download_file(file_name)

    async def _download_file(self, file_name: str) -> DownloadedFile:
        file_minute_ns(file_name)
        for attempt in range(2):
            if attempt:
                # The retry asks the source for a new signature: it is a request like any
                # other and waits its turn (the download lock is still held).
                wait = self._next_download_at - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._next_download_at = time.monotonic() + self._min_download_interval_s
            try:
                return await self._download_once(file_name)
            except TransportError as error:
                if error.code != "expired_signature" or attempt:
                    raise
        raise AssertionError("unreachable download attempt")

    async def _download_once(self, file_name: str) -> DownloadedFile:
        try:
            async with self._client().get(
                DOWNLOAD_BASE + file_name, allow_redirects=False
            ) as response:
                if response.status not in (301, 302, 303, 307, 308):
                    if response.status != 200:
                        raise _http_error(response.status, response.headers)
                    raise TransportError(
                        "expected source redirect", code="redirect_missing", retryable=False
                    )
                target = self._redirect_target(response.headers.get("Location", ""), file_name)
            # GCS signatures cover opaque query encoding. yarl's default
            # canonicalization can rewrite escaped values and break signatures.
            async with self._client().get(
                URL(target, encoded=True), allow_redirects=False
            ) as response:
                if response.status != 200:
                    if response.status == 400:
                        error_body = bytearray()
                        async for chunk in response.content.iter_chunked(1024):
                            error_body.extend(chunk)
                            if len(error_body) > 4096:
                                break
                        if b"<Code>ExpiredToken</Code>" in error_body:
                            # Observed signatures expire after only two seconds;
                            # the first new GCS TLS connection may consume that.
                            # Retry once through the stable origin, with the
                            # now-warm connection, never reuse the stale query.
                            raise TransportError(
                                "source signature expired", code="expired_signature"
                            )
                    if response.status in (301, 302, 303, 307, 308):
                        raise TransportError(
                            "second redirect rejected", code="redirect_rejected", retryable=False
                        )
                    raise _http_error(response.status, response.headers)
                raw, expanded = bytearray(), bytearray()
                decompressor = zlib.decompressobj(31)
                async for chunk in response.content.iter_chunked(65536):
                    if len(raw) + len(chunk) > self.max_compressed_bytes:
                        raise TransportError(
                            "oversized compressed file", code="oversized", retryable=False
                        )
                    raw.extend(chunk)
                    expanded.extend(
                        decompressor.decompress(
                            chunk,
                            self.max_expanded_bytes - len(expanded) + 1,
                        )
                    )
                    if len(expanded) > self.max_expanded_bytes or decompressor.unconsumed_tail:
                        raise TransportError(
                            "oversized expanded file", code="oversized", retryable=False
                        )
                if not decompressor.eof or decompressor.unused_data:
                    raise ValueError("invalid_gzip")
                # Content type is not evidence: validate every JSON line including unknown ISINs.
                await _store_call(partial(_validate_body, bytes(expanded)))
                return DownloadedFile(file_name, bytes(raw), time.time_ns())
        except TransportError:
            raise
        except (zlib.error, ValueError, UnicodeError, RecursionError):
            raise TransportError(
                "invalid gzip/JSON file", code="invalid_content", retryable=False
            ) from None
        except (aiohttp.ClientError, TimeoutError, OSError):
            # Never forward aiohttp exception text: it can contain the signed query.
            raise TransportError("source request failed", code="network_error") from None


def _manifest_gaps(files: tuple[str, ...]) -> tuple[CoverageGap, ...]:
    partitions = sorted({file_minute_ns(name) for name in files})
    return tuple(
        CoverageGap(left + MINUTE_NS, right)
        for left, right in pairwise(partitions)
        if right > left + MINUTE_NS
    )


class EurexDelayedSource:
    """One async lock, one cached manifest, bounded newest-first backlog batches.

    Store operations run in a cancellation-shielded worker while this lock is
    held. Cancellation waits for commit/rollback before releasing the lock.
    Two newest completed files are revalidated once per later manifest poll;
    cached backlog batches never repeatedly download all completed files.
    Non-retryable source failures pause collection until a new source instance
    is explicitly created. Transient failures return backoff status, never sleep.
    """

    def __init__(
        self,
        store: IntradayStore,
        *,
        transport: DelayedTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.transport = transport if transport is not None else EurexDelayedTransport()
        self._clock = clock
        self._lock = asyncio.Lock()
        self._manifest: tuple[str, ...] = ()
        self._last_manifest_at: float | None = None
        self._last_success_ns: int | None = None
        self._next_attempt_at = 0.0
        self._failures = 0
        self._fatal_code: str | None = None
        self._closed = False
        self._revalidate: list[str] = []
        self._manifest_succeeded = False
        self._prefer_revalidate = False

    async def close(self) -> None:
        async with self._lock:
            if not self._closed:
                await self.transport.close()
                self._closed = True

    async def _summary(
        self,
        status: Any,
        *,
        ingested: int = 0,
        changed: tuple[BarEnvelope, ...] = (),
        removed: tuple[MinuteKey, ...] = (),
        error: str | None = None,
        retry: float | None = None,
    ) -> RefreshStatus:
        completed = set(await _store_call(self.store.completed_files))
        latest_event_ns = await _store_call(self.store.latest_event_ns)
        return RefreshStatus(
            status,
            ingested,
            len(set(self._manifest) - completed) + len(self._revalidate),
            len(completed),
            time.time_ns(),
            self._last_success_ns,
            latest_event_ns,
            _manifest_gaps(self._manifest),
            changed,
            removed,
            error,
            retry,
        )

    async def refresh(self, max_files: int = 4) -> RefreshStatus:
        if (
            isinstance(max_files, bool)
            or not isinstance(max_files, int)
            or not 0 <= max_files <= 100
        ):
            raise ValueError("max_files must be 0..100")
        async with self._lock:
            if self._closed:
                raise RuntimeError("source is closed")
            if self._fatal_code is not None:
                return await self._summary("error", error=self._fatal_code)
            now = self._clock()
            if now < self._next_attempt_at:
                return await self._summary("backoff", retry=self._next_attempt_at - now)
            changed: dict[MinuteKey, BarEnvelope] = {}
            removed: set[MinuteKey] = set()
            ingested = 0
            fetching_manifest = False
            try:
                if self._last_manifest_at is None or now - self._last_manifest_at >= 60:
                    # Record attempts too: a failed manifest is never hammered under 60s.
                    self._last_manifest_at = now
                    fetching_manifest = True
                    self._manifest = tuple(
                        sorted(set(await self.transport.fetch_manifest()), reverse=True)
                    )
                    for name in self._manifest:
                        file_minute_ns(name)
                    fetching_manifest = False
                    if self._manifest_succeeded:
                        completed_now = set(await _store_call(self.store.completed_files))
                        self._revalidate = [
                            name for name in self._manifest if name in completed_now
                        ][:2]
                    self._manifest_succeeded = True
                    self._last_success_ns = time.time_ns()
                completed = set(await _store_call(self.store.completed_files))
                pending = [name for name in self._manifest if name not in completed]
                revisit = list(self._revalidate)
                scheduled: list[str] = []
                while len(scheduled) < max_files and (pending or revisit):
                    if pending and (not revisit or not self._prefer_revalidate):
                        scheduled.append(pending.pop(0))
                    else:
                        scheduled.append(revisit.pop(0))
                    self._prefer_revalidate = not self._prefer_revalidate
                for name in scheduled:
                    download = await self.transport.download(name)
                    if download.file_name != name:
                        raise TransportError(
                            "file identity mismatch", code="identity_mismatch", retryable=False
                        )
                    result = await _store_call(
                        partial(
                            self.store.ingest_file,
                            name,
                            download.raw_gzip,
                            captured_ns=download.captured_ns,
                        )
                    )
                    if name in self._revalidate:
                        self._revalidate.remove(name)
                    ingested += int(result.changed)
                    for bar in result.changed_bars:
                        key = MinuteKey(bar.isin, bar.minute_start_ns)
                        changed[key] = bar
                        removed.discard(key)
                    for key in result.removed_minutes:
                        changed.pop(key, None)
                        removed.add(key)
                    self._last_success_ns = time.time_ns()
                self._failures = 0
                return await self._summary(
                    "partial" if pending or self._revalidate else "ready",
                    ingested=ingested,
                    changed=tuple(changed[key] for key in sorted(changed)),
                    removed=tuple(sorted(removed)),
                )
            except TransportError as error:
                self._failures += 1
                retry = error.retry_after_s or min(300.0, 5.0 * 2 ** min(self._failures - 1, 6))
                if fetching_manifest:
                    retry = max(retry, 60.0 - (self._clock() - now))
                self._next_attempt_at = self._clock() + retry
                if not error.retryable:
                    self._fatal_code = error.code
                return await self._summary(
                    "error",
                    ingested=ingested,
                    changed=tuple(changed[key] for key in sorted(changed)),
                    removed=tuple(sorted(removed)),
                    error=error.code,
                    retry=retry if error.retryable else None,
                )
            except (StoreError, ValueError) as error:
                code = (
                    "storage_full" if "storage_full" in str(error) else "invalid_or_storage_error"
                )
                self._fatal_code = code
                return await self._summary(
                    "storage_full" if code == "storage_full" else "error",
                    ingested=ingested,
                    changed=tuple(changed[key] for key in sorted(changed)),
                    removed=tuple(sorted(removed)),
                    error=code,
                )
