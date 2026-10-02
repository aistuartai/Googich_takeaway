from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from googich_takeaway.config import Config
from googich_takeaway.credentials import SecretBox
from googich_takeaway.destinations.immich import ImmichClient
from googich_takeaway.notify import Outcome
from googich_takeaway.pipeline import Pipeline
from googich_takeaway.sources.gdrive import GoogleDriveSource
from googich_takeaway.state import State
from tests.fake_drive import FOLDER, FakeDrive
from tests.fake_immich import KEY, FakeImmichServer
from tests.fake_smb import FakeSmb
from tests.fixtures.takeout import quirks_export
from tests.test_config import key_file

NOW = datetime(2026, 10, 1, tzinfo=UTC)
FOLDER_ID = FOLDER  # the fake Drive serves this folder


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.state = State(tmp_path / "state.db")
        self.config = Config(self.state, SecretBox(b"k" * 32), lambda: NOW)
        self.immich = FakeImmichServer()
        self.drive = FakeDrive()
        self.sleeps: list[float] = []

    def configure(self, drive: bool = True) -> None:
        self.config.save_immich("http://immich.test", "", KEY)
        self.config.save_general(str(self.tmp / "staging"), "Australia/Melbourne")
        if drive:
            self.config.add_drive_source("Takeout", FOLDER_ID, key_file())
            built = self.tmp / "built"
            built.mkdir()
            for path in quirks_export().write(built):
                self.drive.add(path.name, path.name, path.read_bytes())

    def pipeline(self) -> Pipeline:
        return Pipeline(
            self.config,
            self.state,
            lambda: NOW,
            self.sleeps.append,
            immich_factory=lambda url, key: ImmichClient(url, key, self.immich.transport()),
            drive_factory=lambda folder, info: GoogleDriveSource(
                folder, info, self.drive.transport()
            ),
        )


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_unconfigured_run_fails_with_a_clear_reason(world: World) -> None:
    message = world.pipeline().run().message()
    assert message.outcome is Outcome.FAILED
    assert "Immich is not set up" in message.title


def test_full_run_then_nothing_new(world: World) -> None:
    world.configure()
    report = world.pipeline().run()
    assert report.problems == []
    assert report.downloaded == 2
    assert report.uploaded == 13
    assert report.needs_review == 1
    message = report.message()
    assert message.outcome is Outcome.SUCCESS
    assert message.title == "Imported 13 new photos and videos"
    assert "Downloaded 2 archives" in message.body
    assert "1 files have no date and need review." in message.body

    again = world.pipeline().run()
    assert again.problems == []
    assert again.message().outcome is Outcome.NO_NEW_DATA
    assert len(world.immich.assets) == 13


def test_latest_export_is_counted_for_the_dashboard(world: World) -> None:
    import json

    world.configure()
    world.pipeline().run()
    counts = json.loads(world.state.get_setting("photos.latest_export") or "{}")
    assert counts["in_immich"] == 13
    assert counts["not_imported"] == 1
    assert counts["items"] >= 14
    assert counts["export_id"]


def test_local_folder_source(world: World) -> None:
    world.configure(drive=False)
    folder = world.tmp / "manual"
    folder.mkdir()
    quirks_export().write(folder)
    world.config.add_local_source("Manual", str(folder))
    report = world.pipeline().run()
    assert report.problems == []
    assert report.uploaded == 13
    assert report.downloaded == 0


def test_failed_part_blocks_its_export(world: World) -> None:
    world.configure()
    world.drive.drop_after = {"takeout-20261001T010203Z-002.zip": [10] * 6}
    report = world.pipeline().run()
    message = report.message()
    assert message.outcome is Outcome.FAILED
    assert any("a part failed to download" in p for p in report.problems)
    assert not world.immich.assets  # nothing imported from an incomplete export

    world.drive.drop_after.clear()
    retry = world.pipeline().run()  # next scheduled run resumes the download, then imports
    assert retry.problems == []
    assert retry.uploaded == 13


def test_immich_down_fails_the_run(world: World) -> None:
    world.configure()

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    pipeline = Pipeline(
        world.config,
        world.state,
        lambda: NOW,
        world.sleeps.append,
        immich_factory=lambda url, key: ImmichClient(url, key, httpx.MockTransport(down)),
        drive_factory=lambda folder, info: GoogleDriveSource(FOLDER, info, world.drive.transport()),
    )
    message = pipeline.run().message()
    assert message.outcome is Outcome.FAILED
    assert "cannot reach Immich" in message.title
    assert "Downloaded 2 archives" in message.body  # what did work is still reported


def test_unshared_drive_folder_is_reported_but_local_sources_still_import(world: World) -> None:
    world.configure()
    world.drive.shared = False
    folder = world.tmp / "manual"
    folder.mkdir()
    quirks_export().write(folder)
    world.config.add_local_source("Manual", str(folder))
    report = world.pipeline().run()
    assert any("not shared" in p for p in report.problems)
    assert report.uploaded == 13


def test_export_still_being_written_waits(world: World) -> None:
    world.configure()
    for file in world.drive.files:
        file.modified = "2026-09-30T23:30:00.000Z"  # 30 minutes before NOW
    report = world.pipeline().run()
    assert report.problems == []
    assert report.waiting == ["20261001T010203Z"]
    assert not world.immich.assets
    assert "still being written" in report.message().body

    later = Pipeline(
        world.config,
        world.state,
        lambda: datetime(2026, 10, 1, 2, 0, tzinfo=UTC),
        world.sleeps.append,
        immich_factory=lambda url, key: ImmichClient(url, key, world.immich.transport()),
        drive_factory=lambda folder, info: GoogleDriveSource(FOLDER, info, world.drive.transport()),
    )
    assert later.run().uploaded == 13


def test_download_folder_that_is_also_a_local_source_is_not_listed_twice(world: World) -> None:
    world.configure()
    world.pipeline().run()  # downloads into staging and imports
    world.config.add_local_source("Same folder", str(world.tmp / "staging"))
    again = world.pipeline().run()
    assert again.problems == []
    assert again.exports_imported == 0  # same parts, same export: recognised as done


def test_export_already_in_immich_is_no_new_data(world: World) -> None:
    world.configure(drive=False)
    folder = world.tmp / "manual"
    folder.mkdir()
    quirks_export().write(folder)
    world.config.add_local_source("Manual", str(folder))
    world.pipeline().run()
    (world.tmp / "second").mkdir()
    other = World(world.tmp / "second")
    other.immich = world.immich  # same Immich, fresh app state: everything is already there
    other.configure(drive=False)
    other.config.add_local_source("Manual", str(folder))
    message = other.pipeline().run().message()
    assert message.outcome is Outcome.NO_NEW_DATA
    assert message.title == "Nothing new: 13 files already in Immich"


def test_reimport_option_brings_back_files_deleted_in_immich(world: World) -> None:
    from googich_takeaway.pipeline import RunOptions

    world.configure()
    world.pipeline().run()
    deleted = sorted(world.immich.assets.values(), key=lambda a: a.filename)[:3]
    for asset in deleted:
        world.immich.delete(asset.sha1)

    normal = world.pipeline().run()
    assert normal.uploaded == 0  # finished export, and deletions are respected

    pipeline = world.pipeline()
    again = Pipeline(**{**pipeline.__dict__, "options": RunOptions(reimport=True)})
    report = again.run()
    assert report.problems == []
    assert report.uploaded == 3
    assert len(world.immich.assets) == 13


def test_download_again_option_refetches_archives(world: World) -> None:
    from googich_takeaway.pipeline import RunOptions

    world.configure()
    world.pipeline().run()
    for path in (world.tmp / "staging").glob("*.zip"):
        path.unlink()  # cleaned up after import
    assert world.pipeline().run().downloaded == 0
    pipeline = world.pipeline()
    again = Pipeline(**{**pipeline.__dict__, "options": RunOptions(download_again=True)})
    assert again.run().downloaded == 2


def test_tracker_sees_every_stage(world: World) -> None:
    from googich_takeaway.progress import ItemState, Stage, Tracker

    world.configure()
    tracker = Tracker()
    tracker.start_run()
    pipeline = world.pipeline()
    Pipeline(**{**pipeline.__dict__, "tracker": tracker}).run()
    stages = {v.stage: v for v in tracker.snapshot().stages}
    assert (stages[Stage.DOWNLOAD].files_total, stages[Stage.DOWNLOAD].files_done) == (2, 2)
    assert stages[Stage.SCAN].files_done == 1
    assert (stages[Stage.UPLOAD].files_total, stages[Stage.UPLOAD].files_done) == (13, 13)
    assert stages[Stage.UPLOAD].done == stages[Stage.UPLOAD].total
    assert stages[Stage.UPLOAD].items == []  # nothing left to show: all finished
    assert {i.state for i in stages[Stage.UPLOAD].recent} == {ItemState.DONE}


def test_redownloaded_export_already_imported_is_explained(world: World) -> None:
    from googich_takeaway.pipeline import RunOptions

    world.configure()
    world.pipeline().run()
    pipeline = world.pipeline()
    again = Pipeline(**{**pipeline.__dict__, "options": RunOptions(download_again=True)})
    report = again.run()
    message = report.message()
    assert report.downloaded == 2
    assert report.exports_imported == 0
    assert message.title == "Downloaded 2 archives, already imported before"
    assert "was already imported on 01 Oct 2026 00:00 UTC; skipped" in message.body


def smb_world(world: World, monkeypatch: pytest.MonkeyPatch) -> FakeSmb:
    import sys

    from tests.fake_smb import SMB_LOGIN

    fake = FakeSmb()
    monkeypatch.setitem(sys.modules, "smbclient", fake)
    world.configure()
    world.config.save_smb(
        "nas.local", "Photos", "takeout", "photos", SMB_LOGIN, "Australia/Melbourne"
    )
    return fake


def test_full_run_with_an_smb_download_folder(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = smb_world(world, monkeypatch)
    report = world.pipeline().run()
    assert report.problems == []
    assert report.downloaded == 2
    assert report.uploaded == 13
    stored = sorted(p.rsplit("\\", 1)[-1] for p in fake.files)
    assert stored == ["takeout-20261001T010203Z-001.zip", "takeout-20261001T010203Z-002.zip"]
    assert list((world.tmp / "staging").iterdir()) == []  # nothing written locally
    assert world.pipeline().run().message().outcome is Outcome.NO_NEW_DATA


def test_lost_smb_share_mid_download_resumes_next_run(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = smb_world(world, monkeypatch)
    fake.fail_writes_after = 5000
    first = world.pipeline().run()
    assert first.message().outcome is Outcome.FAILED
    assert not world.immich.assets
    assert any(p.endswith(".part") for p in fake.files)
    fake.fail_writes_after = None
    second = world.pipeline().run()
    assert second.problems == []
    assert second.uploaded == 13


def test_smb_cleanup(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from googich_takeaway import cleanup

    fake = smb_world(world, monkeypatch)
    world.pipeline().run()
    location = world.config.staging_location()
    copy = cleanup.staged_exports(location, world.state)[0]
    assert copy.ready
    assert location is not None
    cleanup.delete_staged_export(location, world.state, copy.export_id, confirmed_not_imported=True)
    assert not any(p.endswith(".zip") for p in fake.files)


def test_smb_password_is_sealed_and_kept(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3

    from tests.fake_smb import SMB_LOGIN

    smb_world(world, monkeypatch)
    dump = "\n".join(sqlite3.connect(world.tmp / "state.db").iterdump())
    assert SMB_LOGIN not in dump
    world.config.save_smb("nas.local", "Photos", "takeout", "photos", None, "UTC")  # keep it
    assert world.config.general().storage == "smb"
    assert world.config.general().describe() == "\\\\nas.local\\Photos\\takeout"


def test_smb_settings_are_tested_before_saving(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from googich_takeaway.config import ConfigError

    monkeypatch.setitem(sys.modules, "smbclient", FakeSmb())
    with pytest.raises(ConfigError, match="LOGON_FAILURE"):
        world.config.save_smb("nas.local", "Photos", "x", "photos", "wrong", "UTC")
    assert world.config.general().storage == "local"  # nothing saved


def test_slow_immich_does_not_force_a_rescan(world: World) -> None:
    from googich_takeaway import cleanup

    world.configure()
    world.immich.metadata_delay_reads = 10**6  # Immich has not processed anything yet
    first = world.pipeline().run()
    assert first.problems == []
    assert first.uploaded == 13
    assert first.unverified == 13
    assert "still being processed by Immich" in first.message().body
    copy = cleanup.staged_exports(world.tmp / "staging", world.state)[0]
    assert copy.completed_at is not None  # imported: will not be rescanned
    assert not copy.ready  # but not safe to delete yet
    assert "still processing 13 files" in copy.reason

    world.immich.metadata_delay_reads = 0  # Immich has caught up
    second = world.pipeline().run()
    assert second.exports_imported == 0  # no rescan of the archives
    assert second.verified_later == 13
    assert second.unverified == 0
    assert cleanup.staged_exports(world.tmp / "staging", world.state)[0].ready


def test_wrong_date_in_immich_blocks_cleanup(world: World) -> None:
    from googich_takeaway import cleanup

    world.configure()
    world.immich.shift_hours = 1
    report = world.pipeline().run()
    assert report.date_mismatches == 13
    copy = cleanup.staged_exports(world.tmp / "staging", world.state)[0]
    assert not copy.ready
    assert "different date" in copy.reason
