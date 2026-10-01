import base64
from pathlib import Path

import pytest

from googich_takeaway.credentials import SecretBox, SecretError, load_master_key


def test_master_key_is_created_owner_only_and_reused(tmp_path: Path) -> None:
    path = tmp_path / "data" / "master.key"
    first = load_master_key(path)
    assert path.stat().st_mode & 0o077 == 0
    assert load_master_key(path) == first


def test_readable_master_key_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "master.key"
    load_master_key(path)
    path.chmod(0o644)
    with pytest.raises(SecretError, match="chmod 600"):
        load_master_key(path)


def test_configured_key_file_must_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGICH_MASTER_KEY_FILE", str(tmp_path / "missing"))
    with pytest.raises(SecretError, match="does not exist"):
        load_master_key(tmp_path / "missing")


def test_bad_key_contents(tmp_path: Path) -> None:
    path = tmp_path / "master.key"
    path.write_text(base64.b64encode(b"short").decode())
    path.chmod(0o600)
    with pytest.raises(SecretError, match="32-byte"):
        load_master_key(path)


def test_seal_and_open_round_trip_with_name_binding() -> None:
    box = SecretBox(b"k" * 32)
    sealed = box.seal("immich.api_key", b"secret value")
    assert b"secret value" not in sealed.encode()
    assert box.open("immich.api_key", sealed) == b"secret value"
    with pytest.raises(SecretError, match="cannot be decrypted"):
        box.open("source.1.service_account", sealed)  # moved to another name


def test_wrong_master_key_cannot_open() -> None:
    sealed = SecretBox(b"a" * 32).seal("x", b"value")
    with pytest.raises(SecretError, match="master key replaced"):
        SecretBox(b"b" * 32).open("x", sealed)


def test_each_seal_uses_a_fresh_nonce() -> None:
    box = SecretBox(b"k" * 32)
    assert box.seal("x", b"same") != box.seal("x", b"same")


def test_tampering_is_detected(tmp_path: Path) -> None:
    box = SecretBox(b"k" * 32)
    sealed = box.seal("x", b"value")
    raw = bytearray(base64.b64decode(sealed[3:]))
    raw[-1] ^= 1
    with pytest.raises(SecretError):
        box.open("x", "v1:" + base64.b64encode(bytes(raw)).decode())
