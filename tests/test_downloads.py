import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from googich_takeaway.downloads import Downloader, NotEnoughSpaceError
from googich_takeaway.sources.gdrive import GoogleDriveSource
from googich_takeaway.state import State
from tests.fake_drive import FOLDER, FakeDrive, service_account_info

NOW = datetime(2026, 10, 1, tzinfo=UTC)
DATA = bytes(range(256)) * 4000  # ~1 MB


class Setup:
    def __init__(self, tmp_path: Path, drive: FakeDrive | None = None) -> None:
        self.drive = drive or FakeDrive()
        self.staging = tmp_path / "staging"
        self.state = State(tmp_path / "state.db")
        self.sleeps: list[float] = []
        self.source = GoogleDriveSource(FOLDER, service_account_info(), self.drive.transport())

    def downloader(self, free_margin: int = 0) -> Downloader:
        return Downloader(
            self.staging, self.state, lambda: NOW, self.sleeps.append, free_margin=free_margin
        )


def test_downloads_new_archives_and_records_them(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.drive.add("a", "takeout-20261001T010203Z-001.zip", DATA)
    result = s.downloader().fetch_new(s.source)
    assert [p.name for _, p in result.downloaded] == ["takeout-20261001T010203Z-001.zip"]
    assert (s.staging / "takeout-20261001T010203Z-001.zip").read_bytes() == DATA
    assert list(s.staging.iterdir()) == [s.staging / "takeout-20261001T010203Z-001.zip"]
    assert len(s.state.downloads(s.source.name)) == 1


def test_second_fetch_skips_already_downloaded(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.drive.add("a", "takeout-x-001.zip", DATA)
    s.downloader().fetch_new(s.source)
    (s.staging / "takeout-x-001.zip").unlink()  # cleaned up after import
    result = s.downloader().fetch_new(s.source)
    assert not result.downloaded
    assert [f.name for f in result.skipped] == ["takeout-x-001.zip"]


def test_ignore_history_downloads_missing_copies_again(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.drive.add("a", "takeout-x-001.zip", DATA)
    s.downloader().fetch_new(s.source)
    # The copy is still in the download folder: not fetched again (a resumed run relies on it).
    assert s.downloader().fetch_new(s.source, ignore_history=True).downloaded == []
    for copy in (tmp_path / "staging").glob("*.zip"):
        copy.unlink()  # cleaned up
    assert len(s.downloader().fetch_new(s.source, ignore_history=True).downloaded) == 1


def test_changed_file_at_source_is_downloaded_again(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    file = s.drive.add("a", "takeout-x-001.zip", DATA)
    s.downloader().fetch_new(s.source)
    file.data = DATA + b"more"
    assert len(s.downloader().fetch_new(s.source).downloaded) == 1


def test_interrupted_download_resumes_where_it_stopped(tmp_path: Path) -> None:
    drive = FakeDrive(drop_after={"a": [300_000, 400_000]})
    s = Setup(tmp_path, drive)
    s.drive.add("a", "takeout-x-001.zip", DATA)
    result = s.downloader().fetch_new(s.source)
    assert len(result.downloaded) == 1
    assert (s.staging / "takeout-x-001.zip").read_bytes() == DATA
    assert drive.ranges == [None, "bytes=300000-", "bytes=700000-"]
    assert s.sleeps == [2.0, 4.0]


def test_partial_from_a_previous_run_is_resumed(tmp_path: Path) -> None:
    drive = FakeDrive(drop_after={"a": [100_000] * 6})  # every attempt this run is cut off
    s = Setup(tmp_path, drive)
    s.drive.add("a", "takeout-x-001.zip", DATA)
    first = s.downloader().fetch_new(s.source)
    assert first.failed  # gave up after six attempts
    part = s.staging / "takeout-x-001.zip.part"
    assert 0 < part.stat().st_size < len(DATA)
    drive.drop_after.clear()
    second = s.downloader().fetch_new(s.source)
    assert len(second.downloaded) == 1
    assert (s.staging / "takeout-x-001.zip").read_bytes() == DATA


def test_partial_of_an_older_version_is_discarded(tmp_path: Path) -> None:
    drive = FakeDrive(drop_after={"a": [100] * 6})
    s = Setup(tmp_path, drive)
    file = s.drive.add("a", "takeout-x-001.zip", DATA)
    s.downloader().fetch_new(s.source)
    file.data = b"new version " * 1000
    drive.drop_after.clear()
    s.downloader().fetch_new(s.source)
    assert (s.staging / "takeout-x-001.zip").read_bytes() == file.data


def test_checksum_mismatch_is_discarded_and_reported(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.drive.add("a", "takeout-x-001.zip", DATA)
    files = s.source.list_archives()
    wrong = type(files[0])(**{**files[0].__dict__, "sha256": hashlib.sha256(b"x").hexdigest()})
    s.source.list_archives = lambda: [wrong]  # type: ignore[method-assign]
    result = s.downloader().fetch_new(s.source)
    assert "sha256 does not match" in result.failed[0][1]
    assert not (s.staging / "takeout-x-001.zip").exists()
    assert not (s.staging / "takeout-x-001.zip.part").exists()
    assert s.state.downloads(s.source.name) == []


def test_refuses_download_that_will_not_fit(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.drive.add("a", "takeout-x-001.zip", DATA)
    with pytest.raises(NotEnoughSpaceError, match="only"):
        s.downloader(free_margin=10**18).fetch_new(s.source)
    assert not any(s.staging.glob("*.zip"))


def test_unsafe_names_from_the_source_stay_in_staging(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.drive.add("a", "../../escape.zip", DATA)
    result = s.downloader().fetch_new(s.source)
    assert [p.name for _, p in result.downloaded] == ["escape.zip"]
    assert (s.staging / "escape.zip").exists()
    assert not (tmp_path / "escape.zip").exists()


def test_files_gone_from_source_are_marked_removed(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.drive.add("a", "takeout-x-001.zip", DATA)
    s.drive.add("b", "takeout-x-002.zip", DATA[:10])
    s.downloader().fetch_new(s.source)
    s.drive.files = [f for f in s.drive.files if f.id != "a"]  # user deleted it in Drive
    result = s.downloader().fetch_new(s.source)
    assert result.removed_from_source == 1
    removed = {d.file_id: d.removed_at for d in s.state.downloads(s.source.name)}
    assert removed == {"a": NOW, "b": None}


def test_staged_files_are_owner_only(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.drive.add("a", "takeout-x-001.zip", DATA)
    s.downloader().fetch_new(s.source)
    assert (s.staging / "takeout-x-001.zip").stat().st_mode & 0o077 == 0
    assert s.staging.stat().st_mode & 0o077 == 0


def test_resumed_download_shows_the_bytes_already_there(tmp_path: Path) -> None:
    import json

    from googich_takeaway.progress import Stage, Tracker

    s = Setup(tmp_path)
    s.drive.add("a", "takeout-x-001.zip", DATA)
    remote = s.source.list_archives()[0]
    s.staging.mkdir()
    (s.staging / "takeout-x-001.zip.part").write_bytes(DATA[:300_000])  # left by a paused run
    marker = {"file_id": "a", "fingerprint": remote.fingerprint}
    (s.staging / "takeout-x-001.zip.part.json").write_text(json.dumps(marker))
    tracker = Tracker()
    tracker.start_run()
    downloader = Downloader(
        s.staging, s.state, lambda: NOW, s.sleeps.append, free_margin=0, tracker=tracker
    )
    downloader.fetch_new(s.source)
    assert s.drive.ranges == ["bytes=300000-"]  # only the rest was fetched
    view = next(v for v in tracker.snapshot().stages if v.stage is Stage.DOWNLOAD)
    assert view.done == view.total == len(DATA)  # and the bar counts the part already there
