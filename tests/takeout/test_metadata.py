import io
import json
import struct
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from googich_takeaway.takeout.metadata import (
    MAX_SIDECAR_BYTES,
    parse_sidecar,
    read_media_metadata,
)
from tests.fixtures.takeout import jpeg_bytes, mp4_bytes, sidecar_json


def test_jpeg_exif_date_and_offset() -> None:
    data = jpeg_bytes(1, datetime(2019, 7, 4, 10, 15), timedelta(hours=-3, minutes=-30))
    found = read_media_metadata(".JPG", data)
    assert found.exif_local == datetime(2019, 7, 4, 10, 15)
    assert found.exif_offset == timedelta(hours=-3, minutes=-30)


def test_jpeg_exif_gps() -> None:
    image = Image.new("RGB", (8, 8))
    exif = Image.Exif()
    gps = exif.get_ifd(0x8825)
    gps[1], gps[3] = "S", "E"
    gps[2] = (IFDRational(33), IFDRational(51), IFDRational(2448, 100))
    gps[4] = (IFDRational(151), IFDRational(12), IFDRational(5508, 100))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif.tobytes())
    found = read_media_metadata("jpg", buffer.getvalue())
    assert found.gps is not None
    assert found.gps == pytest.approx((-33.8568, 151.2153), abs=1e-4)


def test_jpeg_without_exif_and_garbage_return_empty() -> None:
    assert read_media_metadata("jpg", jpeg_bytes(1)).exif_local is None
    assert read_media_metadata("jpg", b"not an image").exif_local is None
    assert read_media_metadata("heic", b"anything").exif_local is None


def test_zero_exif_date_is_ignored() -> None:
    image = Image.new("RGB", (8, 8))
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = "0000:00:00 00:00:00"
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif.tobytes())
    assert read_media_metadata("jpg", buffer.getvalue()).exif_local is None


def test_mp4_creation_time_from_head() -> None:
    created = datetime(2020, 6, 1, 0, 0, tzinfo=UTC)
    assert read_media_metadata("mp4", mp4_bytes(1, created)).video_utc == created


def test_mp4_with_moov_at_end_uses_tail() -> None:
    created = datetime(2020, 6, 1, 0, 0, tzinfo=UTC)
    whole = mp4_bytes(1, created)
    ftyp_size = struct.unpack(">I", whole[:4])[0]
    ftyp, rest = whole[:ftyp_size], whole[ftyp_size:]
    mdat = struct.pack(">I", 8 + 300_000) + b"mdat" + bytes(300_000)
    data = ftyp + mdat + rest
    head, tail = data[: 256 * 1024], data[-256 * 1024 :]
    assert read_media_metadata("MOV", head, tail).video_utc == created


def test_mvhd_version_1() -> None:
    created = datetime(2021, 1, 1, tzinfo=UTC)
    seconds = int((created - datetime(1904, 1, 1, tzinfo=UTC)).total_seconds())
    body = struct.pack(">B3xQQIQ", 1, seconds, seconds, 1000, 0) + bytes(80)
    mvhd = struct.pack(">I", 8 + len(body)) + b"mvhd" + body
    moov = struct.pack(">I", 8 + len(mvhd)) + b"moov" + mvhd
    assert read_media_metadata("mp4", moov).video_utc == created


def test_sidecar_parsing() -> None:
    data = sidecar_json(
        "IMG_1.jpg",
        datetime(2019, 7, 4, 0, 15, tzinfo=UTC),
        geo=(-33.8568, 151.2153),
        favorited=True,
        description="Harbour",
    )
    parsed = parse_sidecar(data)
    assert parsed is not None
    assert parsed.title == "IMG_1.jpg"
    assert parsed.taken_utc == datetime(2019, 7, 4, 0, 15, tzinfo=UTC)
    assert parsed.gps == (-33.8568, 151.2153)
    assert parsed.favorited
    assert parsed.description == "Harbour"


def test_sidecar_without_location_or_date() -> None:
    parsed = parse_sidecar(sidecar_json("IMG_1.jpg", None))
    assert parsed is not None
    assert parsed.gps is None
    assert parsed.taken_utc is None


def test_sidecar_prefers_exif_location_over_google_estimate() -> None:
    document = json.loads(sidecar_json("a.jpg", None))
    document["geoData"] = {"latitude": 1.0, "longitude": 2.0}
    document["geoDataExif"] = {"latitude": 3.0, "longitude": 4.0}
    parsed = parse_sidecar(json.dumps(document).encode())
    assert parsed is not None
    assert parsed.gps == (3.0, 4.0)


@pytest.mark.parametrize(
    "data",
    [b"not json", b"[1, 2]", b"\xff\xfe", b"{" + b" " * MAX_SIDECAR_BYTES + b"}"],
)
def test_invalid_sidecars_return_none(data: bytes) -> None:
    assert parse_sidecar(data) is None


def test_deeply_nested_video_boxes_do_not_crash() -> None:
    """A crafted video nests thousands of moov boxes; reading it must not recurse."""
    import struct

    depth = 20_000
    data = b"".join(struct.pack(">I4s", 8 * (depth - n), b"moov") for n in range(depth))
    assert read_media_metadata(".mp4", data[:262_144]).video_utc is None
