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
from tests.fixtures.takeout import quirks_export
from tests.test_config import key_file

NOW = datetime(2026, 10, 1, tzinfo=UTC)
FOLDER_ID = FOLDER + "-abcdefghij"


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
                FOLDER, info, self.drive.transport()
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
