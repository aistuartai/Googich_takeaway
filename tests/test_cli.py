import hashlib
import io
import json
from pathlib import Path

import httpx
import pytest

from googich_takeaway.cli import main
from googich_takeaway.destinations.immich import ImmichClient
from tests.destinations.test_immich import KEY, FakeImmich
from tests.fake_drive import ACCOUNT, FOLDER, FakeDrive, service_account_info
from tests.fake_immich import FakeImmichServer
from tests.fixtures.takeout import ROOT, quirks_export


@pytest.fixture
def export_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "downloads"
    directory.mkdir()
    quirks_export().write(directory)
    (directory / "notes.txt").write_text("not an archive")
    return directory


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "immich.key"
    path.write_text(KEY)
    path.chmod(0o600)
    return path


def run(*argv: str, fake: FakeImmich | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()

    def factory(url: str, key: str) -> ImmichClient:
        return ImmichClient(url, key, transport=httpx.MockTransport(fake or FakeImmich()))

    code = main(list(argv), out, err, client_factory=factory)
    return code, out.getvalue(), err.getvalue()


def sha1_of(path: str) -> str:
    data = next(e.data for e in quirks_export().entries if e.path == path)
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


def test_scan_without_immich(export_dir: Path) -> None:
    code, out, err = run("scan", str(export_dir), "--timezone", "Australia/Melbourne")
    assert code == 0, err
    assert "Export 20261001T010203Z: 2 part(s)" in out
    assert "Media files   15 (1 identical copies in other folders)" in out
    assert "Unique        14" in out
    assert "IMG_0007.jpg" in out  # no date: listed for review
    assert "Dry run: nothing was uploaded or changed." in out
    assert "Immich" not in out.replace("Immich trash", "")


def test_scan_with_immich_counts_new_and_existing(export_dir: Path, key_file: Path) -> None:
    present = sha1_of(f"{ROOT}/Photos from 2019/IMG_0001.jpg")
    trashed = sha1_of(f"{ROOT}/Photos from 2019/IMG_0003.jpg")
    fake = FakeImmich(known={present: False, trashed: True})
    code, out, err = run(
        "scan", str(export_dir), "--immich-url", "http://immich.test",
        "--key-file", str(key_file), "--list", fake=fake,
    )  # fmt: skip
    assert code == 0, err
    assert "Immich 2.7.5  12 new" in out
    assert "1 already there, 1 in Immich trash" in out
    assert "in-immich-trash" in out
    # Only the read-only duplicate check and version were called.
    assert {r.url.path for r in fake.requests} == {
        "/api/server/version",
        "/api/assets/bulk-upload-check",
    }


def test_json_output(export_dir: Path, key_file: Path) -> None:
    code, out, _ = run(
        "scan", str(export_dir), "--json", "--immich-url", "http://immich.test",
        "--key-file", str(key_file), "--timezone", "Australia/Melbourne",
    )  # fmt: skip
    assert code == 0
    data = json.loads(out)
    files = {f["path"]: f for f in data["exports"]["20261001T010203Z"]["files"]}
    assert data["immich_version"] == "2.7.5"
    first = files[f"{ROOT}/Photos from 2019/IMG_20190704_101500.jpg"]
    assert first["date"] == "2019-07-04T10:15:00+10:00"
    assert first["status"] == "new"
    album_copy = f"{ROOT}/Holiday 2019/IMG_0003.jpg"
    assert files[f"{ROOT}/Photos from 2019/IMG_0003.jpg"]["copies"] == [album_copy]


def test_key_never_appears_in_output(export_dir: Path, key_file: Path) -> None:
    _, out, err = run(
        "scan", str(export_dir), "--immich-url", "http://immich.test",
        "--key-file", str(key_file), "--json",
    )  # fmt: skip
    assert KEY not in out
    assert KEY not in err


def test_immich_errors_exit_1(export_dir: Path, key_file: Path) -> None:
    code, _, err = run(
        "scan", str(export_dir), "--immich-url", "http://immich.test",
        "--key-file", str(key_file), fake=FakeImmich(status=401),
    )  # fmt: skip
    assert code == 1
    assert "rejected the API key" in err


def test_url_without_key_file_is_a_usage_error(export_dir: Path) -> None:
    code, _, err = run("scan", str(export_dir), "--immich-url", "http://immich.test")
    assert code == 2
    assert "together" in err


def test_bad_timezone_and_no_archives(tmp_path: Path) -> None:
    assert run("scan", str(tmp_path), "--timezone", "Mars/Olympus")[0] == 2
    code, _, err = run("scan", str(tmp_path))
    assert code == 2
    assert "no .zip" in err


def test_corrupt_archive_exits_1(tmp_path: Path) -> None:
    (tmp_path / "takeout-20261001T010203Z-001.zip").write_bytes(b"garbage")
    code, _, err = run("scan", str(tmp_path))
    assert code == 1
    assert "takeout-20261001T010203Z-001.zip" in err


# --- googich import ---------------------------------------------------------------------------


def run_import_cli(
    server: FakeImmichServer, *argv: str, answer: bool = False
) -> tuple[int, str, str, list[str]]:
    out, err = io.StringIO(), io.StringIO()
    questions: list[str] = []

    def factory(url: str, key: str) -> ImmichClient:
        return ImmichClient(url, key, transport=server.transport())

    def confirm(question: str) -> bool:
        questions.append(question)
        return answer

    code = main(["import", *argv], out, err, client_factory=factory, confirm=confirm)
    return code, out.getvalue(), err.getvalue(), questions


def import_args(export_dir: Path, key_file: Path, tmp_path: Path) -> list[str]:
    return [
        str(export_dir), "--immich-url", "http://immich.test", "--key-file", str(key_file),
        "--state", str(tmp_path / "state" / "state.db"), "--timezone", "Australia/Melbourne",
    ]  # fmt: skip


def test_import_asks_first_and_declining_uploads_nothing(
    export_dir: Path, key_file: Path, tmp_path: Path
) -> None:
    server = FakeImmichServer()
    code, out, _, questions = run_import_cli(server, *import_args(export_dir, key_file, tmp_path))
    assert code == 0
    assert questions == ["Upload 13 files (7.2 kB) to Immich at http://immich.test?"]
    assert "Cancelled: nothing was uploaded." in out
    assert "review: " in out
    assert not server.assets


def test_import_with_confirmation_uploads_and_verifies(
    export_dir: Path, key_file: Path, tmp_path: Path
) -> None:
    server = FakeImmichServer()
    code, out, err, _ = run_import_cli(
        server, *import_args(export_dir, key_file, tmp_path), answer=True
    )
    assert code == 0, err
    assert "Uploaded 13, verified 13" in out
    assert len(server.assets) == 13

    code, out, _, questions = run_import_cli(server, *import_args(export_dir, key_file, tmp_path))
    assert code == 0
    assert "Nothing to upload." in out
    assert questions == []


def test_import_yes_skips_question(export_dir: Path, key_file: Path, tmp_path: Path) -> None:
    server = FakeImmichServer()
    code, _, _, questions = run_import_cli(
        server, *import_args(export_dir, key_file, tmp_path), "--yes"
    )
    assert code == 0
    assert questions == []
    assert len(server.assets) == 13


def test_import_reports_failures_with_exit_1(
    export_dir: Path, key_file: Path, tmp_path: Path
) -> None:
    server = FakeImmichServer(fail_uploads=100, store_failed_uploads=False)
    code, out, _, _ = run_import_cli(server, *import_args(export_dir, key_file, tmp_path), "--yes")
    assert code == 1
    assert "failed:" in out
    assert "run again to continue" in out


def test_import_requires_immich(export_dir: Path) -> None:
    code, _, err, _ = run_import_cli(FakeImmichServer(), str(export_dir))
    assert code == 2
    assert "needs --immich-url" in err


# --- googich fetch ----------------------------------------------------------------------------


def run_fetch_cli(drive: FakeDrive, tmp_path: Path, *extra: str) -> tuple[int, str, str]:
    from googich_takeaway.sources.gdrive import GoogleDriveSource

    key = tmp_path / "sa.json"
    if not key.exists():
        key.write_text(json.dumps(service_account_info()))
        key.chmod(0o600)
    out, err = io.StringIO(), io.StringIO()

    def factory(folder: str, info: dict[str, object]) -> GoogleDriveSource:
        return GoogleDriveSource(folder, info, transport=drive.transport())

    argv = [
        "fetch", "--drive-folder", FOLDER, "--service-account", str(key),
        "--staging", str(tmp_path / "staging"), "--state", str(tmp_path / "state.db"), *extra,
    ]  # fmt: skip
    code = main(argv, out, err, drive_factory=factory)
    return code, out.getvalue(), err.getvalue()


def test_fetch_downloads_then_skips(tmp_path: Path) -> None:
    drive = FakeDrive()
    drive.add("a", "takeout-20261001T010203Z-001.zip", b"x" * 5000)
    code, out, err = run_fetch_cli(drive, tmp_path)
    assert code == 0, err
    assert "Downloaded 1, skipped 0" in out
    assert (tmp_path / "staging" / "takeout-20261001T010203Z-001.zip").exists()
    code, out, _ = run_fetch_cli(drive, tmp_path)
    assert "Downloaded 0, skipped 1" in out


def test_fetch_list_only_downloads_nothing(tmp_path: Path) -> None:
    drive = FakeDrive()
    drive.add("a", "takeout-20261001T010203Z-001.zip", b"x" * 5000)
    code, out, _ = run_fetch_cli(drive, tmp_path, "--list-only")
    assert code == 0
    assert "new" in out
    assert not (tmp_path / "staging").exists()


def test_fetch_empty_folder_names_the_account_to_share_with(tmp_path: Path) -> None:
    code, out, _ = run_fetch_cli(FakeDrive(shared=False), tmp_path)
    assert code == 0
    assert ACCOUNT in out


def test_fetch_refuses_readable_key_file(tmp_path: Path) -> None:
    key = tmp_path / "sa.json"
    key.write_text(json.dumps(service_account_info()))
    key.chmod(0o644)
    code, _, err = run_fetch_cli(FakeDrive(), tmp_path)
    assert code == 1
    assert "chmod 600" in err
    assert "PRIVATE KEY" not in err
