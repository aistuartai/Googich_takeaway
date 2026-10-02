import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from googich_takeaway.config import Config, ConfigError
from googich_takeaway.credentials import SecretBox
from googich_takeaway.state import State
from tests.fake_drive import service_account_info

NOW = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(State(tmp_path / "state.db"), SecretBox(b"k" * 32), lambda: NOW)


def key_file() -> bytes:
    return json.dumps(service_account_info()).encode()


def test_immich_settings_and_key(config: Config) -> None:
    config.save_immich("http://immich:2283/api/", "", "abc123")
    settings = config.immich()
    assert settings.url == "http://immich:2283"
    assert settings.link == "http://immich:2283"
    assert settings.has_key
    assert config.immich_key() == "abc123"
    config.save_immich("http://immich:2283", "https://photos.example", None)  # keep key
    assert config.immich_key() == "abc123"
    assert config.immich().link == "https://photos.example"


@pytest.mark.parametrize(
    ("url", "key", "message"),
    [
        ("immich:2283", "k", "http:// or https://"),
        ("http://user:pw@immich", "k", "user name or password"),
        ("http://immich", "", "Enter an API key"),
        ("http://immich", "has space", "does not look like"),
    ],
)
def test_immich_validation(config: Config, url: str, key: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        config.save_immich(url, "", key)


def test_general_settings(config: Config, tmp_path: Path) -> None:
    config.save_general(str(tmp_path / "staging"), "Australia/Melbourne")
    general = config.general()
    assert general.staging == tmp_path / "staging"
    assert general.staging.is_dir()
    assert general.timezone == "Australia/Melbourne"


@pytest.mark.parametrize(
    ("staging", "timezone", "message"),
    [
        ("relative/path", "UTC", "full path"),
        ("/srv/../etc", "UTC", "'..'"),
        ("/proc/googich", "UTC", "Cannot create"),
        (None, "Mars/Olympus", "Unknown time zone"),
    ],
)
def test_general_validation(
    config: Config, tmp_path: Path, staging: str | None, timezone: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        config.save_general(staging or str(tmp_path), timezone)


def test_drive_source_key_is_sealed(config: Config, tmp_path: Path) -> None:
    source_id = config.add_drive_source("Takeout", "1AbCdEfGhIjKlMnOpQrStUvWxYz012345", key_file())
    assert config.drive_key(source_id)["client_email"] == service_account_info()["client_email"]
    assert config.drive_account(source_id) == service_account_info()["client_email"]
    dump = "\n".join(__import__("sqlite3").connect(tmp_path / "state.db").iterdump())
    assert "PRIVATE KEY" not in dump


@pytest.mark.parametrize(
    ("folder", "data", "message"),
    [
        ("short", None, "folder ID does not look right"),
        ("1AbCdEfGhIjKlMnOpQrStUvWxYz012345", b"not json", "not JSON"),
        ("1AbCdEfGhIjKlMnOpQrStUvWxYz012345", b'{"type": "authorized_user"}', "not a service"),
        ("1AbCdEfGhIjKlMnOpQrStUvWxYz012345", b"x" * 70000, "too large"),
    ],
)
def test_drive_source_validation(
    config: Config, folder: str, data: bytes | None, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        config.add_drive_source("Takeout", folder, data or key_file())


def test_duplicate_names_and_delete_removes_key(config: Config) -> None:
    folder = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
    source_id = config.add_drive_source("Takeout", folder, key_file())
    with pytest.raises(ConfigError, match="already exists"):
        config.add_drive_source("Takeout", folder, key_file())
    config.delete_source(source_id)
    assert config.sources() == []
    with pytest.raises(ConfigError, match="no service account key"):
        config.drive_key(source_id)


def test_local_source(config: Config, tmp_path: Path) -> None:
    config.add_local_source("Manual", str(tmp_path))
    assert [s.kind for s in config.sources()] == ["local"]
    with pytest.raises(ConfigError, match="does not exist"):
        config.add_local_source("Other", str(tmp_path / "missing"))


def test_new_dashboard_items_show_for_choices_made_before_them(tmp_path: Path) -> None:
    from googich_takeaway.state import State

    state = State(tmp_path / "state.db")
    config = Config(state, SecretBox(b"k" * 32), lambda: NOW)
    assert config.dashboard_items() == ["journey", "schedule", "runs"]
    state.set_setting("dashboard.items", "journey,cleanup", NOW)  # as saved by 0.3.0
    assert config.dashboard_items() == ["journey", "schedule", "cleanup"]
    config.save_dashboard_items(["cleanup"])  # a choice made now is kept exactly
    assert config.dashboard_items() == ["cleanup"]


def test_saved_secrets_only_go_where_they_were_entered_for(tmp_path: Path) -> None:
    config = Config(State(tmp_path / "state.db"), SecretBox(b"k" * 32), lambda: NOW)
    config.save_immich("http://immich.lan:2283", "", "key-123")
    config.save_immich("http://immich.lan:2283/", "https://photos.example", "")  # same server
    assert config.immich_key() == "key-123"
    with pytest.raises(ConfigError, match="enter the API key again"):
        config.save_immich("http://attacker.example:2283", "", "")
    assert str(config.immich().url).startswith("http://immich.lan")  # nothing changed
    config.save_immich("http://new.lan:2283", "", "key-456")  # with the key: fine

    def ok(_: object) -> None:
        return None

    config.save_smb("nas.lan", "Photos", "takeout", "photos", "pw", test=ok)
    config.save_smb("nas.lan", "Photos", "other", "photos", "", test=ok)  # same server and user
    with pytest.raises(ConfigError, match="enter the SMB password again"):
        config.save_smb("evil.example", "Photos", "takeout", "photos", "", test=ok)
    with pytest.raises(ConfigError, match="enter the SMB password again"):
        config.save_smb("nas.lan", "Photos", "takeout", "admin", "", test=ok)
