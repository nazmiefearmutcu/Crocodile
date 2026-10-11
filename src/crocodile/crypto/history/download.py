"""Capped streaming transfer with conditional resume and atomic verified publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .schema import (
    ArchiveManifest,
    ArchiveObject,
    DownloadError,
    ExpiredArchiveURL,
    public_url_identity,
)

HOSTS = {
    "okx": {"static.okx.com", "qp-pri-hk.oss-cn-hongkong.aliyuncs.com"},
    "bybit": {"quote-saver.bycsi.com"},
    "hyperliquid": {"zenodo.org"},
}
_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, data: dict[str, object]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    if temporary.is_symlink():
        raise DownloadError("archive metadata temporary path is a symlink")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, sort_keys=True, ensure_ascii=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _prepare_storage(directory: Path, filename: str) -> tuple[Path, Path, Path]:
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    part = target.with_name(target.name + ".part")
    metadata = target.with_name(target.name + ".resume.json")
    for path in (target, part, metadata):
        if path.is_symlink() or path.resolve().parent != directory:
            raise DownloadError("archive output path escapes the selected directory")
    return target, part, metadata


def _partial_metadata(part: Path, metadata: Path, identity: str) -> tuple[dict[str, Any], int]:
    if not metadata.exists() or not part.exists():
        return {}, 0
    try:
        saved = json.loads(metadata.read_text(encoding="utf-8"))
        size = part.stat().st_size
        if (
            not isinstance(saved, dict)
            or saved.get("identity") != identity
            or not saved.get("etag")
            or size != saved.get("partial_bytes")
            or sha256_file(part) != saved.get("partial_sha256")
        ):
            return {}, 0
        return saved, size
    except (ValueError, OSError):
        return {}, 0


def _url_allowed(obj: ArchiveObject) -> None:
    parts = urlsplit(obj.url)
    if (
        parts.scheme not in {"http", "https"}
        or parts.hostname not in HOSTS.get(obj.venue, set())
        or parts.username
        or parts.password
        or parts.port not in {None, 80, 443}
    ):
        raise DownloadError("archive host is not an official published object host")
    if parts.scheme == "http" and parts.hostname != "qp-pri-hk.oss-cn-hongkong.aliyuncs.com":
        raise DownloadError("HTTPS required for this official archive host")


async def download_archive(
    obj: ArchiveObject,
    directory: Path,
    *,
    max_bytes: int,
    expected_sha256: str | None = None,
    session: Any | None = None,
    imported_at_ns: int | None = None,
    resume: bool = True,
) -> ArchiveManifest:
    """Only the selected object is transferred. Cancellation leaves resumable partial data.

    The local SHA256 proves file identity/integrity after transfer; without an independently
    supplied expected SHA256 it does not prove that the source's content was complete.
    Redirects are refused so an allowlisted URL cannot send a transfer to another host.
    """
    _url_allowed(obj)
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if expected_sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("expected SHA256 must contain 64 hexadecimal characters")
    if session is None:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_read=60)
        ) as owned:
            return await download_archive(
                obj,
                directory,
                max_bytes=max_bytes,
                expected_sha256=expected_sha256,
                session=owned,
                imported_at_ns=imported_at_ns,
                resume=resume,
            )
    target, part, metadata = await asyncio.to_thread(_prepare_storage, directory, obj.filename)
    saved, start = (
        await asyncio.to_thread(_partial_metadata, part, metadata, obj.identity)
        if resume
        else ({}, 0)
    )
    if start > max_bytes:
        raise DownloadError("existing partial archive exceeds byte cap")
    for attempt in range(2):
        headers = {"Accept-Encoding": "identity"}
        if start:
            headers.update({"Range": f"bytes={start}-", "If-Range": str(saved["etag"])})
        async with session.get(obj.url, headers=headers, allow_redirects=False) as response:
            if response.status in {401, 403}:
                raise ExpiredArchiveURL(
                    "archive URL expired or unavailable; refresh official catalogue"
                )
            if response.status not in {200, 206}:
                if response.status == 416 and start and attempt == 0:
                    start = 0
                    continue
                raise DownloadError(f"archive HTTP {response.status}")
            etag = response.headers.get("ETag")
            length = response.headers.get("Content-Length")
            total = int(length) if length is not None else None
            if response.status == 206:
                matched = _RANGE.fullmatch(response.headers.get("Content-Range", ""))
                if matched is None or int(matched[1]) != start or int(matched[2]) < start:
                    raise DownloadError("invalid Content-Range for archive resume")
                total = int(matched[3])
                if int(matched[2]) >= total or (
                    length is not None and int(length) != int(matched[2]) - start + 1
                ):
                    raise DownloadError("inconsistent Content-Range length")
                if start and (etag != saved.get("etag") or total != saved.get("total")):
                    start = 0
                    continue  # Changed object: discard old bytes on the next full response.
            else:
                start = 0  # Server ignored Range/If-Range: safe full restart.
            if total is not None and (total < 0 or total > max_bytes):
                raise DownloadError("archive Content-Length exceeds byte cap")
            _atomic_json(metadata, {"identity": obj.identity, "etag": etag, "total": total})
            digest = hashlib.sha256()
            if start:
                with part.open("rb") as existing:
                    for block in iter(lambda: existing.read(1024 * 1024), b""):
                        digest.update(block)
            transferred = start
            try:
                with part.open("ab" if start else "wb") as output:
                    async for block in response.content.iter_chunked(1024 * 1024):
                        if transferred + len(block) > max_bytes:
                            raise DownloadError("archive stream exceeds byte cap")
                        output.write(block)
                        digest.update(block)
                        transferred += len(block)
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                _atomic_json(
                    metadata,
                    {
                        "identity": obj.identity,
                        "etag": etag,
                        "total": total,
                        "partial_bytes": transferred,
                        "partial_sha256": digest.hexdigest(),
                    },
                )
            if total is not None and transferred != total:
                raise DownloadError("truncated archive transfer; resume the partial file")
            checksum = digest.hexdigest()
            if expected_sha256 is not None and checksum != expected_sha256.lower():
                part.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
                raise DownloadError("archive SHA256 mismatch")
            manifest = ArchiveManifest(
                obj.venue,
                obj.product,
                obj.symbol,
                obj.module,
                obj.source_date,
                obj.filename,
                public_url_identity(obj.url),
                checksum,
                transferred,
                obj.format,
                obj.depth,
                imported_at_ns if imported_at_ns is not None else time.time_ns(),
                license=obj.license,
                decoder_version="okx-archive-ndjson-v1"
                if obj.venue == "okx"
                else "unsupported-schema-audit",
                timestamp_precision_ns=1_000_000 if obj.venue == "okx" else None,
            )
            os.replace(part, target)
            _atomic_json(target.with_name(target.name + ".manifest.json"), manifest.to_dict())
            metadata.unlink(missing_ok=True)
            return manifest
    raise DownloadError("archive changed while resuming; refresh catalogue and retry")
