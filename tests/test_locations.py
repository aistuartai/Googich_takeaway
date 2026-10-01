import sys
from pathlib import Path

import pytest

from googich_takeaway.locations import (
    LocalLocation,
    LocationError,
    SmbLocation,
    SmbSettings,
    archives,
)
from tests.fake_smb import SMB_LOGIN, FakeSmb

SETTINGS = SmbSettings("nas.local", "Photos", "takeout", "photos", SMB_LOGIN)


def smb(fake: FakeSmb, settings: SmbSettings = SETTINGS) -> SmbLocation:
    return SmbLocation(settings, client=fake)


@pytest.fixture(params=["local", "smb"])
def location(request: pytest.FixtureRequest, tmp_path: Path) -> LocalLocation | SmbLocation:
    if request.param == "local":
        return LocalLocation(tmp_path / "staging")
    return smb(FakeSmb())


def test_basic_operations(location: LocalLocation | SmbLocation) -> None:
    location.prepare()
    with location.open_append("takeout-x-001.zip.part") as handle:
        handle.write(b"abc")
    with location.open_append("takeout-x-001.zip.part") as handle:
        handle.write(b"def")
    assert location.size("takeout-x-001.zip.part") == 6
    location.replace("takeout-x-001.zip.part", "takeout-x-001.zip")
    assert location.size("takeout-x-001.zip.part") is None
    with location.open_read("takeout-x-001.zip") as handle:
        assert handle.read() == b"abcdef"
    location.write_small("marker.json", b"{}")
    assert location.read_small("marker.json") == b"{}"
    assert location.read_small("missing.json") is None
    assert [a.name for a in archives(location)] == ["takeout-x-001.zip"]
    location.delete("takeout-x-001.zip")
    location.delete("takeout-x-001.zip")  # deleting twice is fine
    assert archives(location) == []
    assert location.free_space()


@pytest.mark.parametrize("name", ["../escape.zip", "a/b.zip", "a\\b.zip", ".hidden", ""])
def test_unsafe_names_are_refused(location: LocalLocation | SmbLocation, name: str) -> None:
    with pytest.raises(LocationError, match="unsafe"):
        location.size(name)


def test_smb_paths_and_domain_user() -> None:
    fake = FakeSmb()
    location = smb(fake, SmbSettings("nas", "Photos", "a/b", "photos", SMB_LOGIN, domain="HOME"))
    assert location.describe() == "\\\\nas\\Photos\\a\\b"
    location.prepare()
    assert "\\\\nas\\photos\\a\\b" in fake.folders


def test_smb_wrong_password_is_a_clear_error_without_the_password() -> None:
    location = smb(FakeSmb(), SmbSettings("nas", "Photos", "x", "photos", "wrong-pass"))
    with pytest.raises(LocationError, match="LOGON_FAILURE") as caught:
        location.prepare()
    assert "wrong-pass" not in str(caught.value)


def test_smb_client_is_imported_only_when_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSmb()
    monkeypatch.setitem(sys.modules, "smbclient", fake)
    location = SmbLocation(SETTINGS)
    location.prepare()
    assert fake.folders
