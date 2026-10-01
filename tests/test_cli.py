import hashlib
import io
import json
from pathlib import Path

import httpx
import pytest

from googich_takeaway.cli import main
from googich_takeaway.destinations.immich import ImmichClient
from tests.destinations.test_immich import KEY, FakeImmich
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
