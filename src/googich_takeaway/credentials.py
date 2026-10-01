"""Encryption of stored credentials.

Credentials (the Immich API key, Google service account keys) are encrypted with AES-256-GCM
before they reach the database. The master key lives outside the database:

- ``GOOGICH_MASTER_KEY_FILE`` names a file, for example a Docker secret under ``/run/secrets``.
- Otherwise ``master.key`` next to the state database is used, and created on first start.

The second option keeps the key on the same disk as the database, so it protects against the
database being copied on its own (a backup, a support bundle) but not against someone who can read
the whole data folder. The README says so. Each secret is bound to its name as associated data,
so an encrypted value cannot be moved to another name.
"""

import base64
import os
import secrets
import stat
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
PREFIX = "v1:"
ENV = "GOOGICH_MASTER_KEY_FILE"


class SecretError(Exception):
    """A secret cannot be read, or the master key is unusable."""


def master_key_path(state_path: Path) -> Path:
    configured = os.environ.get(ENV)
    return Path(configured) if configured else state_path.parent / "master.key"


def load_master_key(path: Path, create: bool = True) -> bytes:
    """Read the master key, creating it (owner-only) if it does not exist and ``create``."""
    if not path.exists():
        if not create or os.environ.get(ENV):
            raise SecretError(f"master key file {path} does not exist")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = secrets.token_bytes(KEY_BYTES)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(base64.b64encode(key).decode() + "\n")
        return key
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077 and not _is_docker_secret(path):
        raise SecretError(f"{path} is readable by other users; run: chmod 600 {path}")
    try:
        key = base64.b64decode(path.read_text().strip(), validate=True)
    except (ValueError, OSError) as error:
        raise SecretError(f"{path} does not contain a valid master key") from error
    if len(key) != KEY_BYTES:
        raise SecretError(f"{path} must hold a {KEY_BYTES}-byte key, base64-encoded")
    return key


class SecretBox:
    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise SecretError("master key has the wrong length")
        self._aead = AESGCM(key)

    def seal(self, name: str, value: bytes) -> str:
        nonce = secrets.token_bytes(NONCE_BYTES)
        sealed = self._aead.encrypt(nonce, value, name.encode())
        return PREFIX + base64.b64encode(nonce + sealed).decode()

    def open(self, name: str, token: str) -> bytes:
        if not token.startswith(PREFIX):
            raise SecretError(f"stored secret {name!r} is in an unknown format")
        try:
            raw = base64.b64decode(token[len(PREFIX) :], validate=True)
            return self._aead.decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], name.encode())
        except (InvalidTag, ValueError) as error:
            raise SecretError(
                f"stored secret {name!r} cannot be decrypted; was the master key replaced?"
            ) from error


def _is_docker_secret(path: Path) -> bool:
    # Docker secrets are mounted read-only and often 0444; the container is the boundary there.
    return str(path).startswith("/run/secrets/")
