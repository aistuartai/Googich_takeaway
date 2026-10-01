from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from googich_takeaway.destinations.immich import ImmichClient
from googich_takeaway.importer import Decision, ImportPlan, ImportResult, plan_import, run_import
from googich_takeaway.state import State, UploadStatus
from googich_takeaway.takeout.dates import DateResolver
from googich_takeaway.takeout.scan import ExportScan, scan_export
from tests.fake_immich import KEY, FakeImmichServer
from tests.fixtures.takeout import ROOT, quirks_export

NOW = datetime(2026, 10, 1, tzinfo=UTC)
RESOLVER = DateResolver(default_timezone=ZoneInfo("Australia/Melbourne"))
EXPORT = "20261001T010203Z"
F = f"{ROOT}/Photos from 2019"


class Run:
    def __init__(self, tmp_path: Path, server: FakeImmichServer | None = None) -> None:
        self.archives = quirks_export().write(tmp_path)
        self.server = server or FakeImmichServer()
        self.state = State(tmp_path / "state.db")
        self.sleeps: list[float] = []

    def scan(self) -> ExportScan:
        return scan_export(self.archives, RESOLVER, NOW)

    def plan(self, reimport: bool = False) -> ImportPlan:
        scan = self.scan()
        with self.client() as client:
            checks = client.check_existing((i.sha1, i.sha1) for i in scan.unique_items())
        return plan_import(EXPORT, scan, checks, self.state, "immich", reimport)

    def run(self, reimport: bool = False) -> tuple[ImportPlan, ImportResult]:
        plan = self.plan(reimport)
        with self.client() as client:
            result = run_import(plan, client, self.state, "immich", lambda: NOW, self.sleeps.append)
        return plan, result

    def client(self) -> ImmichClient:
        return ImmichClient("http://immich.test", KEY, transport=self.server.transport())


def test_first_import_uploads_every_dated_file_and_verifies(tmp_path: Path) -> None:
    r = Run(tmp_path)
    plan, result = r.run()
    assert len(plan.with_decision(Decision.UPLOAD)) == 13
    assert [p.item.name for p in plan.with_decision(Decision.NO_DATE)] == ["IMG_0007.jpg"]
    assert len(result.uploaded) == 13
    assert len(result.verified) == 13
    assert not result.failed
    assert len(r.server.assets) == 13
    records = r.state.uploads("immich")
    assert {rec.status for rec in records} == {UploadStatus.VERIFIED}


def test_files_arrive_unmodified_with_date_sidecar_and_favourite(tmp_path: Path) -> None:
    r = Run(tmp_path)
    r.run()
    item = next(i for i in r.scan().items if i.path == f"{F}/IMG_0003.jpg")
    asset = r.server.by_sha1(item.sha1)
    assert asset is not None
    assert asset.filename == "IMG_0003.jpg"
    assert asset.favorite
    assert asset.xmp is not None
    assert b'exif:DateTimeOriginal="2019-07-04T10:15:00+10:00"' in asset.xmp
    assert b"exif:GPSLatitude=" in asset.xmp


def test_second_run_uploads_nothing(tmp_path: Path) -> None:
    r = Run(tmp_path)
    r.run()
    plan, result = r.run()
    assert not plan.with_decision(Decision.UPLOAD)
    assert len(plan.with_decision(Decision.IN_IMMICH)) == 13
    assert not result.uploaded


def test_photo_deleted_in_immich_is_not_uploaded_again(tmp_path: Path) -> None:
    r = Run(tmp_path)
    r.run()
    gone = next(i for i in r.scan().items if i.name == "IMG_0001.jpg")
    r.server.delete(gone.sha1)
    plan, result = r.run()
    assert [p.item.name for p in plan.with_decision(Decision.DELETED_IN_IMMICH)] == ["IMG_0001.jpg"]
    assert not result.uploaded

    plan, result = r.run(reimport=True)
    assert [i.name for i in result.uploaded] == ["IMG_0001.jpg"]


def test_files_already_in_immich_are_adopted_and_then_protected(tmp_path: Path) -> None:
    server = FakeImmichServer()
    data = next(e.data for e in quirks_export().entries if e.path == f"{F}/IMG_0001.jpg")
    existing = server.preload(data)  # e.g. imported earlier with another tool
    r = Run(tmp_path, server)
    plan, _ = r.run()
    assert [p.item.name for p in plan.with_decision(Decision.IN_IMMICH)] == ["IMG_0001.jpg"]
    record = r.state.get_upload("immich", existing.sha1)
    assert record is not None
    assert record.status is UploadStatus.ADOPTED

    server.delete(existing.sha1)
    plan, _ = r.run()
    assert [p.item.name for p in plan.with_decision(Decision.DELETED_IN_IMMICH)] == ["IMG_0001.jpg"]


def test_trashed_files_are_left_alone(tmp_path: Path) -> None:
    server = FakeImmichServer()
    data = next(e.data for e in quirks_export().entries if e.path == f"{F}/IMG_0001.jpg")
    server.preload(data, trashed=True)
    plan, result = Run(tmp_path, server).run()
    assert [p.item.name for p in plan.with_decision(Decision.IN_IMMICH_TRASH)] == ["IMG_0001.jpg"]
    assert "IMG_0001.jpg" not in [i.name for i in result.uploaded]


def test_lost_response_is_recovered_on_the_next_run(tmp_path: Path) -> None:
    server = FakeImmichServer(fail_uploads=1)  # Immich stored the file but the reply was lost
    r = Run(tmp_path, server)
    _, first = r.run()
    assert len(first.failed) == 1
    assert len(server.assets) == 13
    plan, second = r.run()
    assert not plan.with_decision(Decision.UPLOAD)  # found by the duplicate check
    lost = first.failed[0][0]
    record = r.state.get_upload("immich", lost.sha1)
    assert record is not None
    assert record.status is UploadStatus.ADOPTED
    assert not second.uploaded


def test_repeated_failures_stop_the_run(tmp_path: Path) -> None:
    server = FakeImmichServer(fail_uploads=100, store_failed_uploads=False)
    _, result = Run(tmp_path, server).run()
    assert result.aborted is not None
    assert len(result.failed) == 5
    assert not server.assets


def test_archive_changed_after_scan_is_never_uploaded(tmp_path: Path) -> None:
    r = Run(tmp_path)
    plan = r.plan()
    builder = quirks_export()
    target = f"{F}/IMG_0001.jpg"
    builder.entries = [
        e if e.path != target else type(e)(e.path, e.data[:-1] + b"\x00", e.part)
        for e in builder.entries
    ]
    builder.write(tmp_path)  # overwrite the archives with one file altered
    with r.client() as client:
        result = run_import(plan, client, r.state, "immich", lambda: NOW, r.sleeps.append)
    assert [i.name for i, _ in result.failed] == ["IMG_0001.jpg"]
    assert "changed since it was scanned" in result.failed[0][1]
    assert all(a.filename != "IMG_0001.jpg" for a in r.server.assets.values())


def test_verification_waits_for_immich_metadata(tmp_path: Path) -> None:
    r = Run(tmp_path, FakeImmichServer(metadata_delay_reads=2))
    _, result = r.run()
    assert len(result.verified) == 13
    assert r.sleeps == [2.0, 2.0]


def test_date_mismatch_is_reported_and_recorded(tmp_path: Path) -> None:
    r = Run(tmp_path, FakeImmichServer(shift_hours=1))
    _, result = r.run()
    assert len(result.date_mismatch) == 13
    item, detail = result.date_mismatch[0]
    assert "expected" in detail
    record = r.state.get_upload("immich", item.sha1)
    assert record is not None
    assert record.status is UploadStatus.DATE_MISMATCH


def test_metadata_never_appearing_leaves_items_unverified(tmp_path: Path) -> None:
    r = Run(tmp_path, FakeImmichServer(metadata_delay_reads=100))
    _, result = r.run()
    assert len(result.unverified) == 13
    assert len(r.sleeps) == 9


def test_dry_plan_changes_nothing(tmp_path: Path) -> None:
    r = Run(tmp_path)
    r.plan()
    assert not r.server.assets
    assert r.state.uploads("immich") == []
