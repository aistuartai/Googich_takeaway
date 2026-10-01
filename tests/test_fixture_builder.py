"""The fixture builder itself must be right, or every test built on it is wrong."""

import io
import struct
import tarfile
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from tests.fixtures.takeout import (
    ROOT,
    SidecarStyle,
    duplicate_name,
    jpeg_bytes,
    mp4_bytes,
    quirks_export,
    sidecar_name,
)


@pytest.mark.parametrize(
    ("media", "style", "duplicate", "expected"),
    [
        ("IMG_1234.jpg", SidecarStyle.LEGACY, 0, "IMG_1234.jpg.json"),
        ("IMG_1234.jpg", SidecarStyle.SUPPLEMENTAL, 0, "IMG_1234.jpg.supplemental-metadata.json"),
        ("IMG_1234.jpg", SidecarStyle.LEGACY, 1, "IMG_1234.jpg(1).json"),
        (
            "IMG_1234.jpg",
            SidecarStyle.SUPPLEMENTAL,
            1,
            "IMG_1234.jpg.supplemental-metadata(1).json",
        ),
        (
            "PXL_20190704_001500123.jpg",
            SidecarStyle.SUPPLEMENTAL,
            0,
            "PXL_20190704_001500123.jpg.supplemental-metada.json",
        ),
    ],
)
def test_sidecar_names(media: str, style: SidecarStyle, duplicate: int, expected: str) -> None:
    limit = 46 if expected.endswith("metada.json") else None
    assert sidecar_name(media, style, duplicate, limit) == expected


def test_duplicate_name() -> None:
    assert duplicate_name("IMG_1234.jpg", 1) == "IMG_1234(1).jpg"
    assert duplicate_name("README", 2) == "README(2)"


def test_jpeg_carries_exif_date_and_offset() -> None:
    data = jpeg_bytes(1, datetime(2019, 7, 4, 10, 15), timedelta(hours=10))
    exif = Image.open(io.BytesIO(data)).getexif().get_ifd(0x8769)
    assert exif[0x9003] == "2019:07:04 10:15:00"
    assert exif[0x9011] == "+10:00"


def test_jpeg_content_differs_by_seed_and_is_reproducible() -> None:
    assert jpeg_bytes(1) != jpeg_bytes(2)
    assert jpeg_bytes(1) == jpeg_bytes(1)


def test_mp4_creation_time_round_trips() -> None:
    created = datetime(2019, 7, 4, 0, 15, tzinfo=UTC)
    data = mp4_bytes(1, created)
    start = data.index(b"mvhd") + 4 + 4  # skip type and version/flags
    (seconds,) = struct.unpack(">I", data[start : start + 4])
    assert datetime(1904, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds) == created


def test_quirks_export_writes_numbered_zip_parts(tmp_path: Path) -> None:
    paths = quirks_export().write(tmp_path)
    assert [p.name for p in paths] == [
        "takeout-20261001T010203Z-001.zip",
        "takeout-20261001T010203Z-002.zip",
    ]
    with zipfile.ZipFile(paths[0]) as part1, zipfile.ZipFile(paths[1]) as part2:
        names1, names2 = set(part1.namelist()), set(part2.namelist())
    folder = f"{ROOT}/Photos from 2019"
    assert f"{folder}/IMG_0006.jpg" in names1
    assert f"{folder}/IMG_0006.jpg.supplemental-metadata.json" in names2
    assert f"{folder}/IMG_0002(1).jpg" in names1
    assert f"{folder}/IMG_0003-edited.jpg" in names1
    assert f"{ROOT}/Holiday 2019/metadata.json" in names1


def test_zip_and_tgz_hold_identical_entries(tmp_path: Path) -> None:
    builder = quirks_export()
    (tmp_path / "z").mkdir()
    (tmp_path / "t").mkdir()
    zips = builder.write(tmp_path / "z")
    tgzs = builder.write(tmp_path / "t", "tgz")
    for zip_path, tgz_path in zip(zips, tgzs, strict=True):
        with zipfile.ZipFile(zip_path) as z, tarfile.open(tgz_path) as t:
            from_zip = {n: z.read(n) for n in z.namelist()}
            from_tgz = {}
            for member in t.getmembers():
                handle = t.extractfile(member)
                assert handle is not None
                from_tgz[member.name] = handle.read()
        assert from_zip == from_tgz


def test_archives_are_byte_for_byte_reproducible(tmp_path: Path) -> None:
    first, second = tmp_path / "1", tmp_path / "2"
    first.mkdir()
    second.mkdir()
    for archive_format in ("zip", "tgz"):
        a = quirks_export().write(first, archive_format)
        b = quirks_export().write(second, archive_format)
        assert [p.read_bytes() for p in a] == [p.read_bytes() for p in b]


def test_album_copies_are_identical_bytes(tmp_path: Path) -> None:
    builder = quirks_export()
    by_path = {e.path: e.data for e in builder.entries}
    assert (
        by_path[f"{ROOT}/Photos from 2019/IMG_0003.jpg"]
        == by_path[f"{ROOT}/Holiday 2019/IMG_0003.jpg"]
    )


def test_expected_items_cover_every_media_file() -> None:
    builder = quirks_export()
    media = {e.path for e in builder.entries if not e.path.endswith(".json")}
    assert {item.path for item in builder.expected} == media
