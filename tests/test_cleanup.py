from datetime import UTC, datetime
from pathlib import Path

import pytest

from googich_takeaway import cleanup
from googich_takeaway.destinations.immich import ImmichClient
from googich_takeaway.pipeline import Pipeline, RunOptions
from googich_takeaway.state import State
from tests.fake_immich import KEY
from tests.test_pipeline import World

NOW = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = World(tmp_path)
    w.configure()
    return w


def staging(world: World) -> Path:
    return world.tmp / "staging"


def test_before_import_nothing_is_deletable(world: World) -> None:
    built = world.tmp / "built"
    for path in built.iterdir():
        (staging(world)).mkdir(exist_ok=True)
        (staging(world) / path.name).write_bytes(path.read_bytes())
    copies = cleanup.staged_exports(staging(world), world.state)
    assert [c.ready for c in copies] == [False]
    with pytest.raises(cleanup.CleanupError, match="not been imported completely"):
        cleanup.delete_staged_export(staging(world), world.state, copies[0].export_id, True)
    assert len(list(staging(world).glob("*.zip"))) == 2


def test_imported_export_with_undated_files_needs_confirmation(world: World) -> None:
    world.pipeline().run()
    copy = cleanup.staged_exports(staging(world), world.state)[0]
    assert copy.ready
    assert copy.not_imported == 1  # IMG_0007.jpg has no date
    with pytest.raises(cleanup.CleanupError, match="Tick the box"):
        cleanup.delete_staged_export(staging(world), world.state, copy.export_id, False)
    freed = cleanup.delete_staged_export(staging(world), world.state, copy.export_id, True)
    assert freed == copy.size
    assert list(staging(world).glob("*.zip")) == []
    assert world.pipeline().run().downloaded == 0  # history kept: not downloaded again


def test_changed_archive_is_no_longer_ready(world: World) -> None:
    world.pipeline().run()
    part = next(staging(world).glob("*-002.zip"))
    part.write_bytes(part.read_bytes() + b"extra")  # sizes no longer match what was imported
    copy = cleanup.staged_exports(staging(world), world.state)[0]
    assert not copy.ready
    with pytest.raises(cleanup.CleanupError):
        cleanup.delete_staged_export(staging(world), world.state, copy.export_id, True)


def test_partial_downloads_listed_and_deleted(world: World) -> None:
    staging(world).mkdir(exist_ok=True)
    (staging(world) / "takeout-x-001.zip.part").write_bytes(b"x" * 10)
    (staging(world) / "takeout-x-001.zip.part.json").write_text("{}")
    assert [p.name for p in cleanup.partial_downloads(staging(world))] == ["takeout-x-001.zip"]
    assert cleanup.delete_partial(staging(world), "takeout-x-001.zip") == 10
    assert list(staging(world).iterdir()) == []
    with pytest.raises(cleanup.CleanupError):
        cleanup.delete_partial(staging(world), "../../etc/passwd")


def test_drive_exports_ready_with_links_and_recheck(world: World) -> None:
    world.pipeline().run()
    labels = {f"gdrive:{s.location}": s.name for s in world.config.sources()}
    copies = cleanup.drive_exports(world.state, labels)
    assert len(copies) == 1
    copy = copies[0]
    assert copy.ready
    assert copy.sources == ["Takeout"]
    assert all(p.link and p.link.startswith("https://drive.google.com/") for p in copy.parts)

    with ImmichClient("http://immich.test", KEY, world.immich.transport()) as client:
        assert cleanup.recheck(world.state, client, "immich", copy.export_id).ok
        gone = next(iter(world.immich.assets.values()))
        world.immich.delete(gone.sha1)
        found = cleanup.recheck(world.state, client, "immich", copy.export_id)
    assert found.missing == 1
    assert not found.ok


def test_drive_files_deleted_by_the_user_are_shown_as_removed(world: World) -> None:
    world.pipeline().run()
    world.drive.files = []  # user removed both archives from Drive
    pipeline = world.pipeline()
    Pipeline(**{**pipeline.__dict__, "options": RunOptions()}).run()
    labels = {f"gdrive:{s.location}": s.name for s in world.config.sources()}
    copy = cleanup.drive_exports(world.state, labels)[0]
    assert all(p.removed_at is not None for p in copy.parts)
    assert isinstance(world.state, State)
