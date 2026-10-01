"""Builds small synthetic Google Takeout exports for tests.

Everything is generated at test time: no real photos or personal metadata are stored in the
repository. Output is deterministic for a given builder, so checksums are stable across runs.

The layout and naming rules here model what real Takeout exports are known to contain. Rules
marked ASSUMPTION have not yet been checked against a real export and may need adjusting.
"""

import gzip
import io
import json
import struct
import tarfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal

from PIL import Image

ROOT = "Takeout/Google Photos"
# Fixed timestamp for archive entries, so archives are byte-for-byte reproducible.
_ENTRY_TIME = (2020, 1, 1, 0, 0, 0)
# ASSUMPTION: sidecar file names are cut to this many characters before ".json".
SIDECAR_STEM_LIMIT = 46
_QUICKTIME_EPOCH = datetime(1904, 1, 1, tzinfo=UTC)


class SidecarStyle(StrEnum):
    LEGACY = "legacy"
    """``IMG_1234.jpg.json`` — exports before 2024."""
    SUPPLEMENTAL = "supplemental"
    """``IMG_1234.jpg.supplemental-metadata.json`` — exports from 2024."""


def sidecar_name(media_name: str, style: SidecarStyle, duplicate: int = 0) -> str:
    """Name Takeout gives the sidecar of ``media_name``.

    ``media_name`` is the original name without any ``(n)`` duplicate suffix; ``duplicate`` is
    that ``n``. Takeout puts the suffix after the full name, before ``.json``:
    ``IMG_1234(1).jpg`` pairs with ``IMG_1234.jpg(1).json``.
    """
    stem = media_name if style is SidecarStyle.LEGACY else f"{media_name}.supplemental-metadata"
    stem = stem[:SIDECAR_STEM_LIMIT]
    suffix = f"({duplicate})" if duplicate else ""
    return f"{stem}{suffix}.json"


def duplicate_name(media_name: str, duplicate: int) -> str:
    """``IMG_1234.jpg`` with duplicate 1 becomes ``IMG_1234(1).jpg``."""
    stem, dot, extension = media_name.rpartition(".")
    if not dot:
        return f"{media_name}({duplicate})"
    return f"{stem}({duplicate}).{extension}"


def jpeg_bytes(
    seed: int,
    exif_local: datetime | None = None,
    exif_offset: timedelta | None = None,
) -> bytes:
    """A tiny JPEG whose pixels depend on ``seed``, with optional EXIF capture date."""
    image = Image.new("RGB", (16, 16), ((seed * 53) % 256, (seed * 97) % 256, (seed * 31) % 256))
    image.putpixel((0, 0), (seed % 256, (seed >> 8) % 256, (seed >> 16) % 256))
    buffer = io.BytesIO()
    if exif_local is None:
        image.save(buffer, "JPEG", quality=90)
        return buffer.getvalue()
    exif = Image.Exif()
    exif_ifd = exif.get_ifd(0x8769)
    exif_ifd[0x9003] = exif_local.strftime("%Y:%m:%d %H:%M:%S")  # DateTimeOriginal
    if exif_offset is not None:
        exif_ifd[0x9011] = _format_offset(exif_offset)  # OffsetTimeOriginal
    image.save(buffer, "JPEG", quality=90, exif=exif.tobytes())
    return buffer.getvalue()


def mp4_bytes(seed: int, created_utc: datetime | None = None) -> bytes:
    """A minimal MP4 container: ``ftyp`` plus ``moov``/``mvhd`` carrying the creation time.

    Not playable, but enough for metadata readers and checksums. ``created_utc`` None writes
    zero, which QuickTime readers report as 1904-01-01 ("unknown").
    """
    seconds = 0 if created_utc is None else int((created_utc - _QUICKTIME_EPOCH).total_seconds())
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41")
    mvhd_body = (
        struct.pack(">B3x", 0)  # version 0, flags
        + struct.pack(">IIII", seconds, seconds, 1000, 0)  # created, modified, timescale, duration
        + struct.pack(">IH10x", 0x00010000, 0x0100)  # rate, volume, reserved
        + struct.pack(">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)  # matrix
        + bytes(24)  # pre-defined
        + struct.pack(">I", 2)  # next track ID
    )
    moov = _box(b"moov", _box(b"mvhd", mvhd_body))
    padding = _box(b"free", seed.to_bytes(8, "big"))  # makes content unique per seed
    return ftyp + moov + padding


def sidecar_json(
    title: str,
    taken_utc: datetime | None,
    geo: tuple[float, float] | None = None,
    favorited: bool = False,
    description: str = "",
) -> bytes:
    """Sidecar JSON in the shape Takeout writes for Google Photos items."""
    latitude, longitude = geo if geo else (0.0, 0.0)
    location = {
        "latitude": latitude,
        "longitude": longitude,
        "altitude": 0.0,
        "latitudeSpan": 0.0,
        "longitudeSpan": 0.0,
    }
    data: dict[str, object] = {
        "title": title,
        "description": description,
        "imageViews": "0",
        "geoData": location,
        "geoDataExif": location,
        "url": "https://photos.google.com/photo/example",
    }
    if taken_utc is not None:
        data["photoTakenTime"] = _takeout_time(taken_utc)
        data["creationTime"] = _takeout_time(taken_utc + timedelta(minutes=5))
    if favorited:
        data["favorited"] = True
    return json.dumps(data, indent=2).encode()


def album_metadata_json(title: str) -> bytes:
    """``metadata.json`` Takeout writes in each album folder. Not a media sidecar."""
    return json.dumps({"title": title, "description": "", "access": "protected"}).encode()


@dataclass(frozen=True)
class Entry:
    path: str
    """Path inside the archive."""
    data: bytes
    part: int


@dataclass(frozen=True)
class ExpectedItem:
    """What a correct importer should find for one media file."""

    path: str
    title: str
    """Original file name, as recorded in the sidecar."""
    sidecar_path: str | None
    taken_utc: datetime | None


@dataclass
class TakeoutBuilder:
    """Collects files for one export and writes them as numbered archive parts."""

    export_id: str = "20261001T010203Z"
    entries: list[Entry] = field(default_factory=list)
    expected: list[ExpectedItem] = field(default_factory=list)
    _seed: int = 0

    def next_seed(self) -> int:
        self._seed += 1
        return self._seed

    def add_file(self, path: str, data: bytes, part: int = 1) -> str:
        full = f"{ROOT}/{path}"
        self.entries.append(Entry(full, data, part))
        return full

    def add_photo(
        self,
        name: str,
        folder: str = "Photos from 2019",
        taken_utc: datetime | None = datetime(2019, 7, 4, 0, 15, tzinfo=UTC),
        exif_local: datetime | None = None,
        exif_offset: timedelta | None = None,
        style: SidecarStyle = SidecarStyle.SUPPLEMENTAL,
        sidecar: bool = True,
        duplicate: int = 0,
        archive_name: str | None = None,
        media_part: int = 1,
        sidecar_part: int | None = None,
        geo: tuple[float, float] | None = None,
        favorited: bool = False,
        data: bytes | None = None,
    ) -> ExpectedItem:
        """Add a photo and, by default, its sidecar.

        ``name`` is the original file name (the sidecar ``title``). ``archive_name`` overrides
        the name used inside the archive, for truncation cases. ``data`` reuses existing bytes,
        for the same photo appearing in several folders.
        """
        stored = archive_name or (duplicate_name(name, duplicate) if duplicate else name)
        content = (
            data if data is not None else jpeg_bytes(self.next_seed(), exif_local, exif_offset)
        )
        media_path = self.add_file(f"{folder}/{stored}", content, media_part)
        sidecar_path = None
        if sidecar:
            sidecar_path = self.add_file(
                f"{folder}/{sidecar_name(name, style, duplicate)}",
                sidecar_json(name, taken_utc, geo, favorited),
                sidecar_part or media_part,
            )
        item = ExpectedItem(media_path, name, sidecar_path, taken_utc if sidecar else None)
        self.expected.append(item)
        return item

    def add_video(
        self,
        name: str,
        folder: str = "Photos from 2019",
        created_utc: datetime | None = datetime(2019, 7, 4, 0, 15, tzinfo=UTC),
        taken_utc: datetime | None = None,
        style: SidecarStyle = SidecarStyle.SUPPLEMENTAL,
        part: int = 1,
    ) -> ExpectedItem:
        """Add a video. A sidecar is written only when ``taken_utc`` is given."""
        media_path = self.add_file(
            f"{folder}/{name}", mp4_bytes(self.next_seed(), created_utc), part
        )
        sidecar_path = None
        if taken_utc is not None:
            sidecar_path = self.add_file(
                f"{folder}/{sidecar_name(name, style)}",
                sidecar_json(name, taken_utc),
                part,
            )
        item = ExpectedItem(media_path, name, sidecar_path, taken_utc)
        self.expected.append(item)
        return item

    def add_live_photo(
        self,
        stem: str,
        folder: str = "Photos from 2019",
        taken_utc: datetime = datetime(2019, 7, 4, 0, 15, tzinfo=UTC),
        image_extension: str = "HEIC",
        video_extension: str = "MP4",
    ) -> tuple[ExpectedItem, ExpectedItem]:
        """Image plus motion video with the same stem; only the image has a sidecar.

        The image bytes are a JPEG regardless of extension; tests need names, not codecs.
        """
        image = self.add_photo(f"{stem}.{image_extension}", folder, taken_utc)
        video = self.add_video(f"{stem}.{video_extension}", folder, created_utc=taken_utc)
        return image, video

    def add_edited(self, original: ExpectedItem, suffix: str = "-edited") -> ExpectedItem:
        """Edited copy of ``original``: same folder, no sidecar of its own."""
        folder, _, name = original.path.removeprefix(f"{ROOT}/").rpartition("/")
        stem, _, extension = name.rpartition(".")
        edited_name = f"{stem}{suffix}.{extension}"
        media_path = self.add_file(f"{folder}/{edited_name}", jpeg_bytes(self.next_seed()))
        item = ExpectedItem(media_path, edited_name, original.sidecar_path, original.taken_utc)
        self.expected.append(item)
        return item

    def add_album(self, title: str, items: list[ExpectedItem]) -> list[ExpectedItem]:
        """Album folder holding byte-identical copies of ``items`` plus their sidecars."""
        self.add_file(f"{title}/metadata.json", album_metadata_json(title))
        copies = []
        by_path = {entry.path: entry for entry in self.entries}
        for item in items:
            media = by_path[item.path]
            name = item.path.rpartition("/")[2]
            media_path = self.add_file(f"{title}/{name}", media.data, media.part)
            sidecar_path = None
            if item.sidecar_path:
                sidecar = by_path[item.sidecar_path]
                sidecar_path = self.add_file(
                    f"{title}/{item.sidecar_path.rpartition('/')[2]}", sidecar.data, sidecar.part
                )
            copy = ExpectedItem(media_path, item.title, sidecar_path, item.taken_utc)
            self.expected.append(copy)
            copies.append(copy)
        return copies

    def write(self, directory: Path, archive_format: Literal["zip", "tgz"] = "zip") -> list[Path]:
        """Write one archive per part into ``directory`` and return their paths in order."""
        parts = sorted({entry.part for entry in self.entries})
        paths = []
        for part in parts:
            # ASSUMPTION: part naming takeout-<export id>-<NNN>.<ext>.
            path = directory / f"takeout-{self.export_id}-{part:03d}.{archive_format}"
            entries = [entry for entry in self.entries if entry.part == part]
            if archive_format == "zip":
                _write_zip(path, entries)
            else:
                _write_tgz(path, entries)
            paths.append(path)
        return paths


def _write_zip(path: Path, entries: list[Entry]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry in entries:
            info = zipfile.ZipInfo(entry.path, date_time=_ENTRY_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, entry.data)


def _write_tgz(path: Path, entries: list[Entry]) -> None:
    mtime = datetime(*_ENTRY_TIME, tzinfo=UTC).timestamp()
    with (
        path.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=int(mtime)) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for entry in entries:
            info = tarfile.TarInfo(entry.path)
            info.size = len(entry.data)
            info.mtime = int(mtime)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(entry.data))


def _box(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", 8 + len(body)) + kind + body


def _takeout_time(value: datetime) -> dict[str, str]:
    utc = value.astimezone(UTC)
    return {
        "timestamp": str(int(utc.timestamp())),
        "formatted": f"{utc.day} {utc:%b %Y, %H:%M:%S} UTC",
    }


def _format_offset(offset: timedelta) -> str:
    sign = "-" if offset < timedelta(0) else "+"
    minutes = abs(int(offset.total_seconds())) // 60
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def quirks_export() -> TakeoutBuilder:
    """One export containing every naming quirk the importer must handle."""
    b = TakeoutBuilder()
    b.add_photo("IMG_20190704_101500.jpg", exif_local=datetime(2019, 7, 4, 10, 15))
    b.add_photo("IMG_0001.jpg", style=SidecarStyle.LEGACY)
    b.add_photo("IMG_0002.jpg", duplicate=1)
    b.add_photo("IMG_0002.jpg", duplicate=1, style=SidecarStyle.LEGACY, folder="Photos from 2020")
    long_name = "Family holiday at the beach house summer 2019 sunset.jpg"
    b.add_photo(long_name, archive_name=long_name[:43] + ".jpg")  # ASSUMPTION: 47-char media names
    original = b.add_photo("IMG_0003.jpg", geo=(-33.8568, 151.2153), favorited=True)
    b.add_edited(original)
    b.add_live_photo("IMG_0004")
    b.add_video("VID_20190704_101500.mp4")
    b.add_video("VID_0005.mp4", created_utc=None, taken_utc=datetime(2019, 8, 1, tzinfo=UTC))
    b.add_photo("IMG_0006.jpg", media_part=1, sidecar_part=2)  # sidecar in another part
    b.add_photo("IMG_0007.jpg", sidecar=False)  # no sidecar at all
    b.add_photo("IMG_0008.jpg", media_part=2)
    b.add_album("Holiday 2019", [original])
    return b
