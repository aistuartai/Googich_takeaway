import json
from pathlib import Path

import pytest

from googich_takeaway.sources.base import SourceError, TransientSourceError
from googich_takeaway.sources.gdrive import GoogleDriveSource, load_service_account
from tests.fake_drive import ACCOUNT, FOLDER, FakeDrive, service_account_info


def source(drive: FakeDrive) -> GoogleDriveSource:
    return GoogleDriveSource(FOLDER, service_account_info(), transport=drive.transport())


def test_lists_archives_across_pages_and_ignores_other_files() -> None:
    drive = FakeDrive(page_size=2)
    drive.add("a", "takeout-20261001T010203Z-001.zip", b"one")
    drive.add("b", "takeout-20261001T010203Z-002.tgz", b"two")
    drive.add("c", "notes.txt", b"x")
    drive.add("d", "takeout-20261001T010203Z-003.zip", b"three")
    with source(drive) as s:
        files = s.list_archives()
    assert [f.name for f in files] == [
        "takeout-20261001T010203Z-001.zip",
        "takeout-20261001T010203Z-002.tgz",
        "takeout-20261001T010203Z-003.zip",
    ]
    assert files[0].size == 3
    assert files[0].sha256 is not None
    assert files[0].link == "https://drive.google.com/file/d/a/view"
    assert drive.tokens_issued == 1  # one token reused across pages


def test_download_whole_and_from_offset() -> None:
    drive = FakeDrive()
    drive.add("a", "takeout-x-001.zip", b"0123456789")
    with source(drive) as s:
        file = s.list_archives()[0]
        assert b"".join(s.read(file)) == b"0123456789"
        assert b"".join(s.read(file, start=4)) == b"456789"
    assert drive.ranges == [None, "bytes=4-"]


def test_dropped_connection_is_transient() -> None:
    drive = FakeDrive(drop_after={"a": [3]})
    drive.add("a", "takeout-x-001.zip", b"0123456789")
    with source(drive) as s:
        file = s.list_archives()[0]
        received = bytearray()

        def read_all() -> None:
            for chunk in s.read(file):
                received.extend(chunk)

        with pytest.raises(TransientSourceError, match="interrupted"):
            read_all()
    assert bytes(received) == b"012"  # bytes received before the drop are not lost


def test_unshared_folder_explains_itself() -> None:
    drive = FakeDrive(shared=False)
    drive.add("a", "takeout-x-001.zip", b"x")
    with source(drive) as s, pytest.raises(SourceError, match=f"not shared with {ACCOUNT}"):
        s.list_archives()


def test_odd_folder_ids_are_refused_before_any_request() -> None:
    drive = FakeDrive()
    s = GoogleDriveSource("abc/../x", service_account_info(), transport=drive.transport())
    with pytest.raises(SourceError, match="unexpected characters"):
        s.list_archives()


def test_account_email_is_available_for_setup_instructions() -> None:
    with source(FakeDrive()) as s:
        assert s.account == ACCOUNT


def test_key_file_must_be_private_and_valid(tmp_path: Path) -> None:
    path = tmp_path / "sa.json"
    path.write_text(json.dumps(service_account_info()))
    path.chmod(0o644)
    with pytest.raises(SourceError, match="chmod 600"):
        load_service_account(path)
    path.chmod(0o600)
    assert load_service_account(path)["client_email"] == ACCOUNT
    path.write_text(json.dumps({"type": "authorized_user"}))
    with pytest.raises(SourceError, match="not a service account"):
        load_service_account(path)


def test_errors_never_include_the_private_key(tmp_path: Path) -> None:
    info = service_account_info()
    info["private_key"] = "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n"
    with pytest.raises(SourceError) as caught:
        GoogleDriveSource(FOLDER, info, transport=FakeDrive().transport())
    assert "not-a-key" not in str(caught.value)


def test_disabled_drive_api_is_explained() -> None:
    drive = FakeDrive(api_enabled=False)
    with source(drive) as s, pytest.raises(SourceError, match="Drive API is not enabled"):
        s.list_archives()


def test_disabled_drive_api_is_explained_on_download_too() -> None:
    drive = FakeDrive()
    drive.add("a", "takeout-x-001.zip", b"x")
    with source(drive) as s:
        file = s.list_archives()[0]
        drive.api_enabled = False
        with pytest.raises(SourceError, match="Drive API is not enabled"):
            b"".join(s.read(file))
