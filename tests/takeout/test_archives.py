import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from googich_takeaway.takeout.archives import (
    ArchiveError,
    archive_format,
    group_exports,
    iter_entries,
)
from tests.fixtures.takeout import quirks_export


@pytest.mark.parametrize("archive_type", ["zip", "tgz"])
def test_iter_entries_yields_every_file_with_content(tmp_path: Path, archive_type: str) -> None:
    builder = quirks_export()
    paths = builder.write(tmp_path, archive_type)  # type: ignore[arg-type]
    found = {}
    for path in paths:
        for entry in iter_entries(path):
            data = entry.stream.read()
            assert len(data) == entry.size
            found[entry.path] = data
    assert found == {e.path: e.data for e in builder.entries}


def test_tar_links_and_directories_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "x.tgz"
    with tarfile.open(path, "w:gz") as archive:
        directory = tarfile.TarInfo("Takeout")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        link = tarfile.TarInfo("Takeout/link.jpg")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
        real = tarfile.TarInfo("Takeout/real.jpg")
        real.size = 3
        archive.addfile(real, io.BytesIO(b"abc"))
    assert [e.path for e in iter_entries(path)] == ["Takeout/real.jpg"]


def test_zip_directories_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "x.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Takeout/", b"")
        archive.writestr("Takeout/a.jpg", b"abc")
    assert [e.path for e in iter_entries(path)] == ["Takeout/a.jpg"]


@pytest.mark.parametrize("name", ["broken.zip", "broken.tgz"])
def test_corrupt_archive_raises_archive_error(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_bytes(b"this is not an archive")
    with pytest.raises(ArchiveError, match=name):
        list(iter_entries(path))


def test_truncated_tgz_raises_archive_error(tmp_path: Path) -> None:
    good = quirks_export().write(tmp_path, "tgz")[0]
    cut = tmp_path / "cut.tgz"
    cut.write_bytes(good.read_bytes()[: good.stat().st_size // 2])

    def read_all() -> None:
        for entry in iter_entries(cut):
            entry.stream.read()

    with pytest.raises(ArchiveError, match=r"cut\.tgz"):
        read_all()


def test_corrupt_zip_entry_raises_archive_error_on_read(tmp_path: Path) -> None:
    path = tmp_path / "crc.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("Takeout/a.jpg", b"A" * 1000)
    data = bytearray(path.read_bytes())
    data[data.index(b"A" * 1000) + 10] = ord("B")  # flip a byte: CRC no longer matches
    path.write_bytes(bytes(data))

    def read_all() -> None:
        for entry in iter_entries(path):
            entry.stream.read()

    with pytest.raises(ArchiveError, match=r"a\.jpg"):
        read_all()


def test_unknown_extension_is_rejected() -> None:
    with pytest.raises(ArchiveError, match="not a"):
        archive_format(Path("photos.rar"))
    assert archive_format(Path("x.TAR.GZ")) == "tgz"


def test_group_exports_orders_parts_and_keeps_strangers_apart() -> None:
    paths = [
        Path("takeout-20261001T010203Z-002.zip"),
        Path("takeout-20261001T010203Z-001.zip"),
        Path("takeout-20250101T000000Z-3-001.tgz"),
        Path("my-own-backup.zip"),
    ]
    assert group_exports(paths) == {
        "20250101T000000Z": [Path("takeout-20250101T000000Z-3-001.tgz")],
        "20261001T010203Z": [
            Path("takeout-20261001T010203Z-001.zip"),
            Path("takeout-20261001T010203Z-002.zip"),
        ],
        "my-own-backup.zip": [Path("my-own-backup.zip")],
    }


@pytest.mark.parametrize("kind", ["zip", "tgz"])
def test_only_the_wanted_entries_are_read(tmp_path: Path, kind: str) -> None:
    names = [f"Takeout/Google Photos/p{n}.jpg" for n in range(5)]
    path = tmp_path / f"takeout-x-001.{kind}"
    if kind == "zip":
        with zipfile.ZipFile(path, "w") as archive:
            for name in names:
                archive.writestr(name, name.encode())
    else:
        with tarfile.open(path, "w:gz") as archive:
            for name in names:
                data = name.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    found = [(e.path, e.stream.read()) for e in iter_entries(path, only={names[1], names[3]})]
    assert found == [(names[1], names[1].encode()), (names[3], names[3].encode())]
    assert list(iter_entries(path, only=set())) == []


def test_a_huge_tar_header_is_refused_without_reading_it(tmp_path: Path) -> None:
    """A tiny .tgz claiming a 2 GB long-name header must fail fast, not fill memory."""
    import gzip

    header = tarfile.TarInfo("././@LongLink")
    header.type = tarfile.GNUTYPE_LONGNAME
    header.size = 2 * 1024**3
    block = header.tobuf(format=tarfile.GNU_FORMAT)[:512]
    path = tmp_path / "takeout-x-001.tgz"
    path.write_bytes(gzip.compress(block + b"\0" * 4096))
    with pytest.raises(ArchiveError, match="header"):
        list(iter_entries(path))


def test_files_passed_over_are_reported(tmp_path: Path) -> None:
    names = [f"Takeout/p{n}.jpg" for n in range(4)]
    path = tmp_path / "takeout-x-001.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, b"x" * 100)
    passed: list[int] = []
    found = [e.path for e in iter_entries(path, only={names[2]}, skipped=passed.append)]
    assert found == [names[2]]
    assert passed == [100, 100]  # the two before it; it stops after the last one wanted
