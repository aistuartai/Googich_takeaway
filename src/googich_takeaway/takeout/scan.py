"""Scanning a whole Takeout export: one streaming pass over every archive part.

Each media file is hashed (SHA-1, which Immich uses for duplicate detection) and its metadata is
read from the same pass. Sidecars are collected, paired with media across all parts, and each
item's capture date is resolved. Nothing is extracted to disk.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath

from googich_takeaway.takeout.archives import Readable, iter_entries
from googich_takeaway.takeout.dates import DateInputs, DateResolver, ResolvedDate
from googich_takeaway.takeout.filenames import parse_filename_date
from googich_takeaway.takeout.metadata import (
    HEAD_BYTES,
    MAX_SIDECAR_BYTES,
    TAIL_BYTES,
    MediaMetadata,
    SidecarData,
    parse_sidecar,
    read_media_metadata,
)
from googich_takeaway.takeout.sidecars import (
    VIDEO_EXTENSIONS,
    MatchRule,
    is_media,
    is_sidecar_candidate,
    match_sidecars,
)

CHUNK_BYTES = 1024 * 1024

ProgressCallback = Callable[[int], None]
"""Called with the number of bytes just read, for progress and ETA reporting."""


class MediaKind(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"


@dataclass(frozen=True)
class ScannedItem:
    archive: Path
    path: str
    size: int
    sha1: str
    kind: MediaKind
    sidecar: str | None
    match_rule: MatchRule
    date: ResolvedDate | None
    gps: tuple[float, float] | None
    favorited: bool
    description: str
    duplicate_of: str | None = None
    """Path of an identical file in the same export that will be uploaded instead."""

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name


@dataclass
class ExportScan:
    items: list[ScannedItem] = field(default_factory=list)
    unmatched_sidecars: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    """Entries that are neither media nor sidecars, e.g. ``archive_browser.html``."""
    repeated_paths: list[str] = field(default_factory=list)
    """Paths found in more than one part; only the first copy is used."""

    def unique_items(self) -> list[ScannedItem]:
        return [item for item in self.items if item.duplicate_of is None]


@dataclass(frozen=True)
class _Media:
    archive: Path
    size: int
    sha1: str
    metadata: MediaMetadata


def scan_export(
    archives: list[Path],
    resolver: DateResolver,
    now: datetime,
    progress: ProgressCallback | None = None,
) -> ExportScan:
    """Scan all parts of one export. ``now`` bounds plausible dates, for reproducibility."""
    scan = ExportScan()
    media: dict[str, _Media] = {}
    sidecars: dict[str, SidecarData] = {}
    seen: set[str] = set()

    for archive in archives:
        for entry in iter_entries(archive):
            if entry.path in seen:
                scan.repeated_paths.append(entry.path)
                _drain(entry.stream, progress)
                continue
            seen.add(entry.path)
            if is_sidecar_candidate(entry.path) and entry.size <= MAX_SIDECAR_BYTES:
                data = entry.stream.read(MAX_SIDECAR_BYTES + 1)
                if progress:
                    progress(len(data))
                parsed = parse_sidecar(data)
                if parsed is not None:
                    sidecars[entry.path] = parsed
                else:
                    scan.ignored.append(entry.path)
            elif is_media(entry.path):
                sha1, size, head, tail = _hash(entry.stream, progress)
                extension = PurePosixPath(entry.path).suffix
                media[entry.path] = _Media(
                    archive, size, sha1, read_media_metadata(extension, head, tail)
                )
            else:
                scan.ignored.append(entry.path)
                _drain(entry.stream, progress)

    titles = {path: data.title for path, data in sidecars.items() if data.title}
    matches = match_sidecars([*media, *sidecars], titles)
    used = {m.sidecar for m in matches.values() if m.sidecar}
    scan.unmatched_sidecars = sorted(set(sidecars) - used)

    for path, found in sorted(media.items()):
        match = matches[path]
        sidecar = sidecars.get(match.sidecar) if match.sidecar else None
        gps = found.metadata.gps or (sidecar.gps if sidecar else None)
        inputs = DateInputs(
            sidecar_utc=sidecar.taken_utc if sidecar else None,
            exif_local=found.metadata.exif_local,
            exif_offset=found.metadata.exif_offset,
            video_utc=found.metadata.video_utc,
            gps=gps,
            filename_date=parse_filename_date(PurePosixPath(path).name),
        )
        extension = PurePosixPath(path).suffix.lower().removeprefix(".")
        scan.items.append(
            ScannedItem(
                archive=found.archive,
                path=path,
                size=found.size,
                sha1=found.sha1,
                kind=MediaKind.VIDEO if extension in VIDEO_EXTENSIONS else MediaKind.PHOTO,
                sidecar=match.sidecar,
                match_rule=match.rule,
                date=resolver.resolve(inputs, now),
                gps=gps,
                favorited=sidecar.favorited if sidecar else False,
                description=sidecar.description if sidecar else "",
            )
        )
    scan.items = _mark_duplicates(scan.items)
    return scan


def _hash(stream: Readable, progress: ProgressCallback | None) -> tuple[str, int, bytes, bytes]:
    digest = hashlib.sha1(usedforsecurity=False)
    head = bytearray()
    tail = bytearray()
    size = 0
    read = stream.read
    while chunk := read(CHUNK_BYTES):
        digest.update(chunk)
        size += len(chunk)
        if len(head) < HEAD_BYTES:
            head += chunk[: HEAD_BYTES - len(head)]
        tail += chunk
        if len(tail) > TAIL_BYTES:
            del tail[: len(tail) - TAIL_BYTES]
        if progress:
            progress(len(chunk))
    return digest.hexdigest(), size, bytes(head), bytes(tail)


def _drain(stream: Readable, progress: ProgressCallback | None) -> None:
    read = stream.read
    while chunk := read(CHUNK_BYTES):
        if progress:
            progress(len(chunk))


def _mark_duplicates(items: list[ScannedItem]) -> list[ScannedItem]:
    """Identical files (e.g. a photo in a year folder and an album) are uploaded once.

    The copy kept is the one with the most information: a sidecar, a date, then a
    ``Photos from`` year folder, then the first path.
    """
    by_hash: dict[str, list[ScannedItem]] = {}
    for item in items:
        by_hash.setdefault(item.sha1, []).append(item)
    keep: dict[str, str] = {}
    for sha1, group in by_hash.items():
        best = min(
            group,
            key=lambda i: (
                i.sidecar is None,
                i.date is None,
                "/Photos from " not in i.path,
                i.path,
            ),
        )
        keep[sha1] = best.path
    result = []
    for item in items:
        kept = keep[item.sha1]
        if kept == item.path:
            result.append(item)
        else:
            result.append(replace(item, duplicate_of=kept))
    return result
