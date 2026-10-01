"""Reading capture metadata from media files and Takeout sidecars.

Media metadata is read from the first and last bytes of each file, captured while the file is
streamed for hashing, so no file is read twice during a scan. Formats not handled here (for
example HEIC and most RAW files) return empty metadata; the date resolver then relies on the
sidecar.
"""

import io
import json
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from PIL import Image, UnidentifiedImageError

HEAD_BYTES = 256 * 1024
TAIL_BYTES = 256 * 1024
MAX_SIDECAR_BYTES = 1024 * 1024

_QUICKTIME_EPOCH = datetime(1904, 1, 1, tzinfo=UTC)
_PILLOW_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp", "tif", "tiff"})
_QUICKTIME_EXTENSIONS = frozenset({"mp4", "mov", "m4v", "3gp", "mp"})

_EXIF_IFD = 0x8769
_GPS_IFD = 0x8825
_DATETIME_ORIGINAL = 0x9003
_OFFSET_TIME_ORIGINAL = 0x9011


@dataclass(frozen=True)
class MediaMetadata:
    exif_local: datetime | None = None
    exif_offset: timedelta | None = None
    gps: tuple[float, float] | None = None
    video_utc: datetime | None = None


@dataclass(frozen=True)
class SidecarData:
    title: str | None
    taken_utc: datetime | None
    gps: tuple[float, float] | None
    favorited: bool
    description: str


def read_media_metadata(extension: str, head: bytes, tail: bytes = b"") -> MediaMetadata:
    """Best-effort metadata from a file's first and last bytes. Never raises on bad input."""
    extension = extension.lower().removeprefix(".")
    if extension in _PILLOW_EXTENSIONS:
        return _read_image(head)
    if extension in _QUICKTIME_EXTENSIONS:
        return MediaMetadata(video_utc=_read_quicktime_created(head, tail))
    return MediaMetadata()


def parse_sidecar(data: bytes) -> SidecarData | None:
    """Parse a Takeout sidecar. Returns None if it is not valid sidecar JSON."""
    if len(data) > MAX_SIDECAR_BYTES:
        return None
    try:
        document = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    title = document.get("title")
    return SidecarData(
        title=title if isinstance(title, str) else None,
        taken_utc=_takeout_timestamp(document.get("photoTakenTime")),
        gps=_takeout_geo(document.get("geoDataExif")) or _takeout_geo(document.get("geoData")),
        favorited=document.get("favorited") is True,
        description=str(document.get("description") or ""),
    )


def _read_image(head: bytes) -> MediaMetadata:
    try:
        with Image.open(io.BytesIO(head)) as image:
            exif = image.getexif()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return MediaMetadata()
    try:
        exif_ifd = exif.get_ifd(_EXIF_IFD)
        gps_ifd = exif.get_ifd(_GPS_IFD)
    except (OSError, ValueError, KeyError, struct.error):
        return MediaMetadata()
    return MediaMetadata(
        exif_local=_exif_datetime(exif_ifd.get(_DATETIME_ORIGINAL)),
        exif_offset=_exif_offset(exif_ifd.get(_OFFSET_TIME_ORIGINAL)),
        gps=_exif_gps(gps_ifd),
    )


def _exif_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip("\x00 ")[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None  # e.g. "0000:00:00 00:00:00"


def _exif_offset(value: object) -> timedelta | None:
    if not isinstance(value, str):
        return None
    text = value.strip("\x00 ")
    if len(text) != 6 or text[0] not in "+-" or text[3] != ":":
        return None
    try:
        hours, minutes = int(text[1:3]), int(text[4:6])
    except ValueError:
        return None
    offset = timedelta(hours=hours, minutes=minutes)
    return -offset if text[0] == "-" else offset


def _exif_gps(gps: Any) -> tuple[float, float] | None:
    try:
        latitude = _dms(gps[2], gps[1])
        longitude = _dms(gps[4], gps[3])
    except (KeyError, TypeError, ValueError, ZeroDivisionError, IndexError):
        return None
    if latitude == 0 and longitude == 0:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


def _dms(values: Any, reference: Any) -> float:
    degrees, minutes, seconds = (float(v) for v in values)
    result = degrees + minutes / 60 + seconds / 3600
    return -result if str(reference).upper() in ("S", "W") else result


def _read_quicktime_created(head: bytes, tail: bytes) -> datetime | None:
    """Creation time from the ``mvhd`` box, wherever ``moov`` sits in the file."""
    found = _walk_for_mvhd(head)
    if found is None and tail:
        found = _scan_for_mvhd(tail)
    return found


def _walk_for_mvhd(data: bytes, offset: int = 0, end: int | None = None) -> datetime | None:
    end = len(data) if end is None else end
    while offset + 8 <= end:
        size, kind = struct.unpack(">I4s", data[offset : offset + 8])
        header = 8
        if size == 1 and offset + 16 <= end:
            (size,) = struct.unpack(">Q", data[offset + 8 : offset + 16])
            header = 16
        elif size == 0:
            size = end - offset
        if size < header:
            return None
        if kind == b"moov":
            return _walk_for_mvhd(data, offset + header, min(offset + size, end))
        if kind == b"mvhd":
            return _mvhd_time(data[offset + header : offset + size])
        offset += size
    return None


def _scan_for_mvhd(data: bytes) -> datetime | None:
    # moov at the end of the file: its start is not aligned with the tail buffer, so search.
    position = data.find(b"mvhd")
    while position >= 4:
        body = data[position + 4 :]
        found = _mvhd_time(body)
        if found is not None:
            return found
        position = data.find(b"mvhd", position + 1)
    return None


def _mvhd_time(body: bytes) -> datetime | None:
    if len(body) < 8:
        return None
    version = body[0]
    try:
        if version == 0:
            (seconds,) = struct.unpack(">I", body[4:8])
        elif version == 1 and len(body) >= 12:
            (seconds,) = struct.unpack(">Q", body[4:12])
        else:
            return None
        return _QUICKTIME_EPOCH + timedelta(seconds=seconds)
    except (struct.error, OverflowError):
        return None


def _takeout_timestamp(value: object) -> datetime | None:
    if not isinstance(value, dict):
        return None
    try:
        return datetime.fromtimestamp(int(value["timestamp"]), tz=UTC)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


def _takeout_geo(value: object) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        latitude, longitude = float(value["latitude"]), float(value["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if latitude == 0 and longitude == 0:
        return None  # Takeout writes 0,0 when there is no location
    return latitude, longitude
