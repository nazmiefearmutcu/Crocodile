"""Capture context and immutable sealed segment inventory."""

import hashlib
import os
from pathlib import Path

import msgspec

from crocodile.core.capture.schema import CaptureContext, CaptureStats
from crocodile.core.errors import CaptureIntegrityError


class SegmentInfo(msgspec.Struct, frozen=True, kw_only=True):
    name: str
    size_bytes: int
    first_ordinal: int
    last_ordinal: int
    sha256: str


class CaptureManifest(msgspec.Struct, frozen=True, kw_only=True):
    context: CaptureContext
    segments: tuple[SegmentInfo, ...] = ()
    active_name: str | None = None
    fsynced_ordinal: int = 0
    version: int = 1
    stats: CaptureStats | None = None


def segment_path(root: Path, name: str) -> Path:
    if Path(name).name != name or "/" in name or "\\" in name or ":" in name:
        raise CaptureIntegrityError("Invalid segment name")
    return root / name


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while block := file.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(root: Path) -> CaptureManifest:
    try:
        manifest = msgspec.json.decode((root / "manifest.json").read_bytes(), type=CaptureManifest)
    except (OSError, msgspec.DecodeError) as exc:
        raise CaptureIntegrityError("Cannot read capture manifest") from exc
    if manifest.version != 1:
        raise CaptureIntegrityError("Unsupported manifest version")
    names = [s.name for s in manifest.segments]
    if manifest.active_name:
        names.append(manifest.active_name)
    if len(names) != len(set(names)) or manifest.fsynced_ordinal < 0:
        raise CaptureIntegrityError("Invalid manifest segment inventory")
    for name in names:
        segment_path(root, name)
    return manifest


def save_manifest(root: Path, manifest: CaptureManifest) -> None:
    temporary = root / "manifest.json.tmp"
    with temporary.open("wb") as file:
        file.write(msgspec.json.encode(manifest))
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, root / "manifest.json")
