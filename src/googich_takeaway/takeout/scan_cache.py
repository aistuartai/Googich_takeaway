"""Remembering what reading an export found, so a paused or failed run does not read it again.

Reading a large export (hashing every file, collecting the sidecars) can take hours. As each
archive part is read, what was found in it is saved in small batches: each media file's size,
SHA-1 and metadata, each sidecar's contents, and the names of other entries. A later run of the
same export takes those from the cache instead of the archive: in a zip it does not even open
them; a tgz must still be unpacked in order, but nothing is hashed again.

An archive part is known by its name and size, so a changed or replaced part is read afresh.
What was found is kept after the export is imported, so a Re-import or a run into a different
Immich library does not read it again; it goes once unused for a while.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from googich_takeaway.takeout.metadata import MediaMetadata, SidecarData

MEDIA = "media"
SIDECAR = "sidecar"
OTHER = "other"


@dataclass(frozen=True)
class CachedEntry:
    path: str
    kind: str
    """``media``, ``sidecar`` or ``other``."""
    size: int = 0
    sha1: str = ""
    media: MediaMetadata | None = None
    sidecar: SidecarData | None = None


class ScanCache(Protocol):
    def load(self, archive: str) -> tuple[bool, list[CachedEntry]]:
        """Whether ``archive`` was read to the end, and the entries saved from it so far."""
        ...

    def save(self, archive: str, entries: list[CachedEntry], complete: bool = False) -> None:
        """Add ``entries`` read from ``archive``; ``complete`` once it has been read to the end."""
        ...


READ_VERSION = 1
"""Raised when reading an archive finds something new (a fix in reading dates, say), so saved
scans from before are read again rather than reused."""


def archive_key(name: str, size: int) -> str:
    key = f"{name}:{size}"
    return key if READ_VERSION == 1 else f"{key}:v{READ_VERSION}"


def encode(entry: CachedEntry) -> str:
    data: dict[str, Any] = {"kind": entry.kind}
    if entry.kind == MEDIA:
        meta = entry.media or MediaMetadata()
        data.update(
            size=entry.size,
            sha1=entry.sha1,
            exif_local=_time(meta.exif_local),
            exif_offset=meta.exif_offset.total_seconds() if meta.exif_offset else None,
            gps=list(meta.gps) if meta.gps else None,
            video_utc=_time(meta.video_utc),
        )
    elif entry.kind == SIDECAR and entry.sidecar is not None:
        side = entry.sidecar
        data.update(
            title=side.title,
            taken_utc=_time(side.taken_utc),
            gps=list(side.gps) if side.gps else None,
            favorited=side.favorited,
            description=side.description,
        )
    return json.dumps(data, separators=(",", ":"))


def decode(path: str, text: str) -> CachedEntry | None:
    """The saved entry, or None if it cannot be read (then the file is read again)."""
    try:
        data = json.loads(text)
        kind = data["kind"]
        if kind == MEDIA:
            offset = data.get("exif_offset")
            media = MediaMetadata(
                exif_local=_parse(data.get("exif_local")),
                exif_offset=timedelta(seconds=offset) if offset is not None else None,
                gps=_gps(data.get("gps")),
                video_utc=_parse(data.get("video_utc")),
            )
            return CachedEntry(path, MEDIA, int(data["size"]), str(data["sha1"]), media=media)
        if kind == SIDECAR:
            sidecar = SidecarData(
                title=data.get("title"),
                taken_utc=_parse(data.get("taken_utc")),
                gps=_gps(data.get("gps")),
                favorited=bool(data.get("favorited")),
                description=str(data.get("description") or ""),
            )
            return CachedEntry(path, SIDECAR, sidecar=sidecar)
        if kind == OTHER:
            return CachedEntry(path, OTHER)
    except (ValueError, KeyError, TypeError):
        return None
    return None


def decode_all(rows: Iterable[tuple[str, str]]) -> list[CachedEntry]:
    return [entry for path, text in rows if (entry := decode(path, text)) is not None]


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse(value: object) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def _gps(value: object) -> tuple[float, float] | None:
    if isinstance(value, list) and len(value) == 2:
        return float(value[0]), float(value[1])
    return None
