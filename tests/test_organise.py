"""Albums, stacks, tags, archived photos, Google's bin, partner photos and the date range."""

import os
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from googich_takeaway.config import ImportSettings
from googich_takeaway.pipeline import RunOptions
from tests.fixtures.takeout import TakeoutBuilder
from tests.test_pipeline import NOW, World

ALL = [
    "asset.upload", "asset.read", "asset.delete", "album.read", "album.create",
    "albumAsset.create", "stack.create", "tag.create", "tag.asset",
]  # fmt: skip


def _export() -> TakeoutBuilder:
    b = TakeoutBuilder()
    original = b.add_photo("IMG_1.jpg")
    b.add_edited(original)
    b.add_photo("DSC_2.NEF")
    b.add_photo("DSC_2.JPG")
    b.add_photo("IMG_20231014_183246_BURST001_COVER.jpg")
    b.add_photo("IMG_20231014_183246_BURST002.jpg")
    b.add_album("Trip", [original])
    b.add_photo("archived.jpg", extra={"archived": True})
    b.add_photo("binned.jpg", extra={"trashed": True})
    b.add_photo("partner.jpg", extra={"googlePhotosOrigin": {"fromPartnerSharing": {}}})
    b.add_photo("people.jpg", extra={"people": [{"name": "Ann"}, {"name": "Bo"}]})
    b.add_photo("old.jpg", taken_utc=datetime(2001, 1, 1, tzinfo=UTC))
    return b


@pytest.fixture
def world(tmp_path: Path) -> World:
    world = World(tmp_path)
    world.configure(drive=False)
    folder = tmp_path / "manual"
    folder.mkdir()
    old = (NOW - timedelta(hours=2)).timestamp()
    for path in _export().write(folder):
        os.utime(path, (old, old))
    world.config.add_local_source("Manual", str(folder))
    world.immich.permissions = list(ALL)
    return world


def _named(world: World) -> dict[str, str]:
    """Asset id by file name."""
    return {a.filename: a.id for a in world.immich.assets.values()}


def test_defaults_make_albums_and_stacks_and_respect_the_archive(world: World) -> None:
    report = world.pipeline().run()
    assert report.problems == []
    names = _named(world)
    assert "binned.jpg" not in names  # Google's bin is left out
    assert report.in_google_bin == 1
    assert {"partner.jpg", "old.jpg"} <= set(names)  # partner photos and any date: imported
    [(album, members)] = world.immich.albums.values()
    assert album == "Trip"
    assert members == {names["IMG_1.jpg"]}
    stacks = {tuple(stack) for stack in world.immich.stacks}
    assert (names["IMG_1-edited.jpg"], names["IMG_1.jpg"]) in stacks  # the edit is shown
    assert (names["DSC_2.JPG"], names["DSC_2.NEF"]) in stacks
    burst = "IMG_20231014_183246_BURST"
    assert (names[f"{burst}001_COVER.jpg"], names[f"{burst}002.jpg"]) in stacks
    assert len(stacks) == 3
    assert next(a for a in world.immich.assets.values() if a.filename == "archived.jpg").archived
    assert not world.immich.tags  # tags are off unless chosen
    lines = report.message().lines
    assert "Added 1 photos and videos to albums (1 new)." in lines
    assert "Stacked 3 sets of photos that belong together." in lines


def test_chosen_settings_tag_skip_partner_and_filter_dates(world: World) -> None:
    world.config.save_import_settings(
        replace(
            ImportSettings(),
            partner="skip",
            archived="timeline",
            tag_people=True,
            tag_import=True,
            taken_from=date(2010, 1, 1),
        )
    )
    report = replace(world.pipeline(), options=RunOptions(include_trashed=True)).run()
    names = _named(world)
    assert "partner.jpg" not in names
    assert report.partner_skipped == 1
    assert "old.jpg" not in names
    assert report.out_of_range == 1
    assert not any(a.archived for a in world.immich.assets.values())
    binned = next(a for a in world.immich.assets.values() if a.filename == "binned.jpg")
    assert binned.trashed  # uploaded, then put in Immich's trash
    tags = {value: ids for value, ids in world.immich.tags.values()}
    assert tags["People/Ann"] == {names["people.jpg"]} == tags["People/Bo"]
    assert names["IMG_1.jpg"] in tags["Google Takeout/2026-10-01"]
    # Photos outside the range are only in the archive: Cleanup asks before removing it.
    from googich_takeaway import cleanup

    [copy] = cleanup.staged_exports(world.tmp / "manual", world.state)
    assert copy.not_imported == 1


def test_a_missing_permission_skips_that_part_without_failing(world: World) -> None:
    world.immich.permissions = ["asset.upload", "asset.read"]
    report = world.pipeline().run()
    assert report.problems == []
    message = report.message()
    assert message.outcome.value == "success"
    assert any("No albums: the API key needs album.create" in line for line in message.lines)
    assert any("No stacks: the API key needs stack.create" in line for line in message.lines)
    assert not world.immich.albums


def test_changes_made_in_immich_are_respected(world: World) -> None:
    world.pipeline().run()
    [(album_id, (_, members))] = world.immich.albums.items()
    members.clear()  # taken out of the album in Immich
    reimport = replace(world.pipeline(), options=RunOptions(reimport=True))
    reimport.run()
    assert world.immich.albums[album_id][1] == set()  # not put back
    assert len(world.immich.stacks) == 3  # not stacked again
    del world.immich.albums[album_id]  # the album deleted in Immich
    world.state._db.execute("DELETE FROM organised WHERE kind = 'album'")  # a new photo for it
    reimport.run()
    assert not world.immich.albums  # not made again


def test_photos_imported_before_get_albums_with_reimport(world: World) -> None:
    world.immich.permissions = ["asset.upload", "asset.read"]
    world.pipeline().run()
    assert not world.immich.albums
    assert not world.immich.stacks
    world.immich.permissions = list(ALL)
    replace(world.pipeline(), options=RunOptions(reimport=True)).run()
    assert len(world.immich.albums) == 1
    assert len(world.immich.stacks) == 3


def test_a_photo_from_googles_bin_is_trashed_even_if_its_run_stopped_first(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The intent is kept: the next run moves it to Immich's trash, and only once."""
    from googich_takeaway import pipeline
    from googich_takeaway.progress import RunStopped

    def stopped(*args: object, **kwargs: object) -> None:
        raise RunStopped("pause")  # paused after uploading, before organising

    monkeypatch.setattr(pipeline, "organise", stopped)
    run = replace(world.pipeline(), options=RunOptions(include_trashed=True))
    with pytest.raises(RunStopped):
        run.run()
    binned = next(a for a in world.immich.assets.values() if a.filename == "binned.jpg")
    assert not binned.trashed
    monkeypatch.undo()

    replace(world.pipeline(), options=RunOptions(include_trashed=True)).run()
    assert binned.trashed  # moved by the next run
    binned.trashed = False  # restored in Immich by the user
    replace(world.pipeline(), options=RunOptions(include_trashed=True, reimport=True)).run()
    assert not binned.trashed  # not moved again


def test_photos_put_in_immichs_trash_on_purpose_do_not_block_cleanup(world: World) -> None:
    """Check Immich now: a photo moved from Google's bin to Immich's trash is where it should
    be; one the user deleted in Immich since still keeps the archive."""
    from googich_takeaway import cleanup
    from googich_takeaway.destinations.immich import ImmichClient
    from tests.fake_immich import KEY

    replace(world.pipeline(), options=RunOptions(include_trashed=True)).run()
    binned = next(a for a in world.immich.assets.values() if a.filename == "binned.jpg")
    assert binned.trashed
    [copy] = cleanup.staged_exports(world.tmp / "manual", world.state)
    with ImmichClient("http://immich.test", KEY, world.immich.transport()) as client:
        found = cleanup.recheck(world.state, client, "immich", copy.export_id)
        assert found.ok, found
        assert found.binned == 1
        other = next(a for a in world.immich.assets.values() if a.filename == "IMG_1.jpg")
        other.trashed = True  # deleted in Immich by the user since
        found = cleanup.recheck(world.state, client, "immich", copy.export_id)
    assert not found.ok
    assert found.trashed == 1


def test_ignoring_failed_files_keeps_the_photos_outside_the_date_range_counted(
    world: World,
) -> None:
    from googich_takeaway import cleanup
    from googich_takeaway.pipeline import ignore_failures, pending_failures

    world.config.save_import_settings(replace(ImportSettings(), taken_from=date(2010, 1, 1)))
    world.immich.refuse_names = frozenset({"IMG_1.jpg"})
    world.pipeline().run()
    [record] = pending_failures(world.state)
    assert ignore_failures(world.state, str(record["export_id"]), NOW) == 1
    [copy] = cleanup.staged_exports(world.tmp / "manual", world.state)
    assert copy.not_imported == 2  # the ignored file, and old.jpg outside the date range


@pytest.mark.parametrize("version", [(2, 7, 5), (3, 3, 3)])
def test_photos_never_go_into_an_album_someone_else_shared(
    world: World, version: tuple[int, int, int]
) -> None:
    """Immich 3 lists albums shared with you unless asked for owned ones: one with the same name
    as a Google Photos album must not receive this library's photos."""
    world.immich.version = version
    world.immich.shared_with_me["theirs"] = ("Trip", set())
    world.pipeline().run()
    assert world.immich.shared_with_me["theirs"][1] == set()  # nothing added to theirs
    [(_, (name, members))] = world.immich.albums.items()
    assert name == "Trip"  # our own album was made instead
    assert members
