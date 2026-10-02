import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from googich_takeaway.takeout.dates import DateResolver, DateSource
from googich_takeaway.takeout.scan import MediaKind, scan_export
from tests.fixtures.takeout import ROOT, quirks_export

NOW = datetime(2026, 10, 1, tzinfo=UTC)
RESOLVER = DateResolver(default_timezone=ZoneInfo("Australia/Melbourne"))
F = f"{ROOT}/Photos from 2019"


@pytest.fixture(params=["zip", "tgz"])
def archives(request: pytest.FixtureRequest, tmp_path: Path) -> list[Path]:
    return quirks_export().write(tmp_path, request.param)


def test_every_media_file_is_scanned_with_correct_hash(archives: list[Path]) -> None:
    builder = quirks_export()
    scan = scan_export(archives, RESOLVER, NOW)
    expected = {e.path: e.data for e in builder.entries if not e.path.endswith(".json")}
    assert {i.path: i.sha1 for i in scan.items} == {
        path: hashlib.sha1(data, usedforsecurity=False).hexdigest()
        for path, data in expected.items()
    }
    assert {i.path: i.size for i in scan.items} == {p: len(d) for p, d in expected.items()}


def test_sidecars_pair_across_parts(archives: list[Path]) -> None:
    builder = quirks_export()
    items = {i.path: i for i in scan_export(archives, RESOLVER, NOW).items}
    for item in builder.expected:
        assert items[item.path].sidecar == item.sidecar_path, item.path


def test_dates(archives: list[Path]) -> None:
    items = {i.path: i for i in scan_export(archives, RESOLVER, NOW).items}

    exif = items[f"{F}/IMG_20190704_101500.jpg"].date
    assert exif is not None
    assert exif.source is DateSource.EXIF
    assert exif.xmp_value() == "2019-07-04T10:15:00+10:00"

    sidecar_only = items[f"{F}/IMG_0001.jpg"].date
    assert sidecar_only is not None
    assert sidecar_only.source is DateSource.SIDECAR
    assert sidecar_only.utc == datetime(2019, 7, 4, 0, 15, tzinfo=UTC)

    video = items[f"{F}/VID_20190704_101500.mp4"].date
    assert video is not None
    assert video.source is DateSource.VIDEO

    undated_video = items[f"{F}/VID_0005.mp4"].date
    assert undated_video is not None
    assert undated_video.source is DateSource.SIDECAR
    assert undated_video.utc == datetime(2019, 8, 1, tzinfo=UTC)

    assert items[f"{F}/IMG_0007.jpg"].date is None  # nothing to go on: flagged for review


def test_sidecar_details_carried_over(archives: list[Path]) -> None:
    items = {i.path: i for i in scan_export(archives, RESOLVER, NOW).items}
    favourite = items[f"{F}/IMG_0003.jpg"]
    assert favourite.favorited
    assert favourite.gps == (-33.8568, 151.2153)
    assert items[f"{F}/IMG_0004.MP4"].kind is MediaKind.VIDEO


def test_album_copy_is_marked_duplicate_of_year_folder(archives: list[Path]) -> None:
    scan = scan_export(archives, RESOLVER, NOW)
    items = {i.path: i for i in scan.items}
    assert items[f"{ROOT}/Holiday 2019/IMG_0003.jpg"].duplicate_of == f"{F}/IMG_0003.jpg"
    assert items[f"{F}/IMG_0003.jpg"].duplicate_of is None
    assert len(scan.unique_items()) == len(scan.items) - 1


def test_album_metadata_is_ignored_and_no_sidecar_left_over(archives: list[Path]) -> None:
    scan = scan_export(archives, RESOLVER, NOW)
    assert f"{ROOT}/Holiday 2019/metadata.json" in scan.ignored
    assert scan.unmatched_sidecars == []


def test_progress_reports_every_byte(archives: list[Path]) -> None:
    total = sum(len(e.data) for e in quirks_export().entries)
    seen: list[int] = []
    scan_export(archives, RESOLVER, NOW, progress=seen.append)
    assert sum(seen) == total


def test_zip_and_tgz_scan_identically(tmp_path: Path) -> None:
    (tmp_path / "z").mkdir()
    (tmp_path / "t").mkdir()
    zips = quirks_export().write(tmp_path / "z", "zip")
    tgzs = quirks_export().write(tmp_path / "t", "tgz")

    def strip(items: list) -> list:  # type: ignore[type-arg]
        return [(i.path, i.sha1, i.sidecar, i.date, i.duplicate_of) for i in items]

    assert strip(scan_export(zips, RESOLVER, NOW).items) == strip(
        scan_export(tgzs, RESOLVER, NOW).items
    )


def test_scan_is_deterministic(archives: list[Path]) -> None:
    assert scan_export(archives, RESOLVER, NOW) == scan_export(archives, RESOLVER, NOW)


def test_offset_in_exif_survives_scan(tmp_path: Path) -> None:
    from tests.fixtures.takeout import TakeoutBuilder

    builder = TakeoutBuilder()
    builder.add_photo(
        "IMG_9.jpg",
        exif_local=datetime(2019, 7, 4, 10, 15),
        exif_offset=timedelta(hours=9, minutes=30),
        taken_utc=datetime(2019, 7, 4, 0, 45, tzinfo=UTC),
    )
    item = scan_export(builder.write(tmp_path), RESOLVER, NOW).items[0]
    assert item.date is not None
    assert item.date.xmp_value() == "2019-07-04T10:15:00+09:30"


def test_pixel_motion_video_copy_is_skipped_when_embedded(tmp_path: Path) -> None:
    from tests.fixtures.takeout import TakeoutBuilder

    builder = TakeoutBuilder()
    still = builder.add_motion_photo("PXL_20240101_010203456")
    scan = scan_export(builder.write(tmp_path), RESOLVER, NOW)
    assert [i.path for i in scan.items] == [still.path]
    assert scan.motion_companions == [f"{F}/PXL_20240101_010203456.MP"]
    assert scan.items[0].sidecar == still.sidecar_path


def test_lone_mp_video_is_kept_as_a_video(tmp_path: Path) -> None:
    from tests.fixtures.takeout import TakeoutBuilder, mp4_bytes

    builder = TakeoutBuilder()
    builder.add_file("Photos from 2019/PXL_20240101_010203456.MP", mp4_bytes(1))
    scan = scan_export(builder.write(tmp_path), RESOLVER, NOW)
    assert [i.kind for i in scan.items] == [MediaKind.VIDEO]
    assert scan.motion_companions == []


def test_numbered_motion_videos_are_paired_with_their_still(tmp_path: Path) -> None:
    """Takeout numbers a second copy before the last extension of each file:
    PXL_x(1).MP is the motion video of PXL_x.MP(1).jpg, already inside that photo."""
    import zipfile

    folder = "Takeout/Google Photos/Photos from 2022"
    path = tmp_path / "takeout-20261001T010203Z-001.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in (
            ("PXL_20220202_094253007.MP.jpg", b"still one"),
            ("PXL_20220202_094253007.MP", b"video one"),
            ("PXL_20220202_094253007.MP(1).jpg", b"still two"),
            ("PXL_20220202_094253007(1).MP", b"video two"),
            ("PXL_20220303_101010000.MP", b"a video with no still"),
        ):
            archive.writestr(f"{folder}/{name}", data)
    scan = scan_export([path], RESOLVER, NOW)
    assert sorted(p.rsplit("/", 1)[1] for p in scan.motion_companions) == [
        "PXL_20220202_094253007(1).MP",
        "PXL_20220202_094253007.MP",
    ]
    kept = sorted(i.name for i in scan.items)
    assert "PXL_20220303_101010000.MP" in kept  # no still: kept, so it is not lost


def test_a_lone_motion_video_goes_to_immich_as_mp4() -> None:
    from googich_takeaway.importer import _upload_name

    assert _upload_name("PXL_20220303_101010000.MP") == "PXL_20220303_101010000.mp4"
    assert _upload_name("IMG_1.jpg") == "IMG_1.jpg"
