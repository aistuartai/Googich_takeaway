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


def test_fake_matches_the_real_library_shapes() -> None:
    """Every attribute the SMB location reads must exist on smbclient's real result types."""
    import smbclient

    assert {"caller_available_size"} <= set(smbclient.SMBStatVolumeResult._fields)
    assert {"st_size", "st_mtime"} <= set(smbclient.SMBStatResult._fields)
    for name in ("name", "is_file", "stat"):
        assert hasattr(smbclient.SMBDirEntry, name)
    for function in (
        "makedirs",
        "open_file",
        "remove",
        "replace",
        "stat",
        "scandir",
        "stat_volume",
    ):
        assert callable(getattr(smbclient, function))


def test_free_space_never_raises() -> None:
    class Broken(FakeSmb):
        def stat_volume(self, path: str, **kwargs: object) -> object:
            return object()  # missing every field

    assert smb(Broken()).free_space() is None


def test_one_shared_connection_per_share(monkeypatch: pytest.MonkeyPatch) -> None:
    from googich_takeaway import locations

    fake = FakeSmb()
    monkeypatch.setitem(sys.modules, "smbclient", fake)
    closed: list[str] = []
    monkeypatch.setattr(SmbLocation, "close", lambda self: closed.append(self.describe()))
    first = locations.shared_smb(SETTINGS)
    assert locations.shared_smb(SETTINGS) is first  # reused, not a new connection
    other = SmbSettings("nas.local", "Photos", "other", "photos", SMB_LOGIN)
    second = locations.shared_smb(other)
    assert second is not first
    assert closed == [first.describe()]  # the old share's connection is closed
    locations.close_shared_smb()
    assert closed == [first.describe(), second.describe()]


def test_probe_always_disconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    from googich_takeaway import locations

    monkeypatch.setitem(sys.modules, "smbclient", FakeSmb())
    closed: list[bool] = []
    monkeypatch.setattr(SmbLocation, "close", lambda self: closed.append(True))
    locations.probe_smb(SETTINGS)
    with pytest.raises(LocationError):
        locations.probe_smb(SmbSettings("nas.local", "Photos", "x", "photos", "wrong"))
    assert closed == [True, True]


def test_connection_limit_is_explained() -> None:
    class Busy(FakeSmb):
        def makedirs(self, path: str, exist_ok: bool = False, **kwargs: object) -> None:
            raise OSError(
                "[Error 0] [NtStatus 0xc00000d0] Unknown NtStatus error returned "
                "'STATUS_REQUEST_NOT_ACCEPTED': '\\\\\\\\optimus\\\\share'"
            )

    with pytest.raises(LocationError, match="Windows desktop editions accept 20") as caught:
        smb(Busy()).prepare()
    assert "STATUS_REQUEST_NOT_ACCEPTED" in str(caught.value)
