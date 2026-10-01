"""Settings made in the web interface, validated and stored in the state database.

Plain settings are stored as text. Credentials are sealed with the master key before storage and
are only ever unsealed to be used: the web interface can replace or remove them, never show them.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from googich_takeaway.credentials import SecretBox
from googich_takeaway.notify import DEFAULT_OUTCOMES, Notifier, Outcome, invalid_urls
from googich_takeaway.state import SourceRecord, State, StateError

MAX_KEY_FILE_BYTES = 64 * 1024
_FOLDER_ID = re.compile(r"[A-Za-z0-9_-]{10,200}")


class ConfigError(Exception):
    """A setting was rejected; the message is shown to the user as is."""


@dataclass(frozen=True)
class ImmichSettings:
    url: str | None
    public_url: str | None
    has_key: bool

    @property
    def link(self) -> str | None:
        """Where the web interface links to for the gallery."""
        return self.public_url or self.url


@dataclass(frozen=True)
class GeneralSettings:
    staging: Path | None
    timezone: str


class Config:
    def __init__(self, state: State, box: SecretBox, clock: Callable[[], datetime]) -> None:
        self._state = state
        self._box = box
        self._clock = clock

    # --- Immich ----------------------------------------------------------------------------------

    def immich(self) -> ImmichSettings:
        return ImmichSettings(
            url=self._state.get_setting("immich.url"),
            public_url=self._state.get_setting("immich.public_url"),
            has_key=self._state.get_sealed("immich.api_key") is not None,
        )

    def save_immich(self, url: str, public_url: str, api_key: str | None) -> None:
        """Save Immich settings. ``api_key`` None or empty keeps the stored key."""
        clean_url = _http_url(url, "Immich address")
        clean_public = _http_url(public_url, "Public address") if public_url.strip() else None
        key = (api_key or "").strip()
        if key and (len(key) > 512 or not key.isprintable() or " " in key):
            raise ConfigError("That does not look like an Immich API key.")
        if not key and not self.immich().has_key:
            raise ConfigError("Enter an API key.")
        now = self._clock()
        self._state.set_setting("immich.url", clean_url, now)
        self._state.set_setting("immich.public_url", clean_public, now)
        if key:
            self._state.set_sealed(
                "immich.api_key", self._box.seal("immich.api_key", key.encode()), now
            )

    def immich_key(self) -> str | None:
        sealed = self._state.get_sealed("immich.api_key")
        return self._box.open("immich.api_key", sealed).decode() if sealed else None

    # --- general ---------------------------------------------------------------------------------

    def general(self) -> GeneralSettings:
        staging = self._state.get_setting("staging.path")
        return GeneralSettings(
            staging=Path(staging) if staging else None,
            timezone=self._state.get_setting("timezone") or "UTC",
        )

    def save_general(self, staging: str, timezone: str) -> None:
        path = Path(staging.strip())
        if not staging.strip() or not path.is_absolute():
            raise ConfigError("The download folder must be a full path, starting with /.")
        if ".." in path.parts:
            raise ConfigError("The download folder must not contain '..'.")
        try:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as error:
            raise ConfigError(f"Cannot create the download folder: {error.strerror}.") from None
        if not path.is_dir():
            raise ConfigError("The download folder path is not a folder.")
        probe = path / ".googich-write-test"
        try:
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as error:
            raise ConfigError(f"Cannot write to the download folder: {error.strerror}.") from None
        try:
            ZoneInfo(timezone.strip())
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError(f"Unknown time zone {timezone.strip()!r}.") from None
        now = self._clock()
        self._state.set_setting("staging.path", str(path), now)
        self._state.set_setting("timezone", timezone.strip(), now)

    # --- notifications ---------------------------------------------------------------------------

    def notification_outcomes(self) -> frozenset[Outcome]:
        stored = self._state.get_setting("notify.outcomes")
        if stored is None:
            return DEFAULT_OUTCOMES
        return frozenset(Outcome(o) for o in json.loads(stored) if o in Outcome)

    def has_notification_urls(self) -> bool:
        return self._state.get_sealed("notify.urls") is not None

    def save_notifications(self, urls: str | None, outcomes: list[str]) -> None:
        """``urls`` None keeps the stored URLs; an empty string removes them."""
        chosen = sorted({Outcome(o).value for o in outcomes if o in Outcome})
        now = self._clock()
        if urls is not None:
            lines = [line.strip() for line in urls.splitlines() if line.strip()]
            if len(lines) > 20:
                raise ConfigError("Use at most 20 notification URLs.")
            bad = invalid_urls(lines)
            if bad:
                raise ConfigError(
                    "Apprise does not recognise the URL on line "
                    + ", ".join(str(n) for n in bad)
                    + ". See the Apprise documentation for URL formats."
                )
            sealed = self._box.seal("notify.urls", "\n".join(lines).encode()) if lines else None
            self._state.set_sealed("notify.urls", sealed, now)
        self._state.set_setting("notify.outcomes", json.dumps(chosen), now)

    def notifier(self) -> Notifier:
        sealed = self._state.get_sealed("notify.urls")
        urls = self._box.open("notify.urls", sealed).decode().splitlines() if sealed else []
        return Notifier(urls, self.notification_outcomes())

    # --- sources ---------------------------------------------------------------------------------

    def sources(self) -> list[SourceRecord]:
        return self._state.sources()

    def add_drive_source(self, name: str, folder_id: str, key_file: bytes) -> int:
        clean_name = _name(name)
        folder = folder_id.strip()
        if not _FOLDER_ID.fullmatch(folder):
            raise ConfigError(
                "That folder ID does not look right. It is the last part of the folder's "
                "address in Google Drive, after /folders/."
            )
        info = parse_service_account(key_file)
        try:
            source_id = self._state.add_source("gdrive", clean_name, folder, self._clock())
        except StateError as error:
            raise ConfigError(str(error)) from None
        self.replace_drive_key(source_id, key_file, info)
        return source_id

    def replace_drive_key(
        self, source_id: int, key_file: bytes, info: dict[str, Any] | None = None
    ) -> None:
        info = info or parse_service_account(key_file)
        name = _drive_key_name(source_id)
        sealed = self._box.seal(name, json.dumps(info).encode())
        self._state.set_sealed(name, sealed, self._clock())

    def drive_key(self, source_id: int) -> dict[str, Any]:
        name = _drive_key_name(source_id)
        sealed = self._state.get_sealed(name)
        if sealed is None:
            raise ConfigError("This source has no service account key; upload one.")
        data = json.loads(self._box.open(name, sealed))
        if not isinstance(data, dict):
            raise ConfigError("The stored service account key is damaged; upload it again.")
        return data

    def drive_account(self, source_id: int) -> str | None:
        """Service account email (not secret), to show who the folder must be shared with."""
        try:
            return str(self.drive_key(source_id).get("client_email") or "") or None
        except ConfigError:
            return None

    def add_local_source(self, name: str, folder: str) -> int:
        clean_name = _name(name)
        path = Path(folder.strip())
        if not folder.strip() or not path.is_absolute():
            raise ConfigError("The folder must be a full path, starting with /.")
        if not path.is_dir():
            raise ConfigError("That folder does not exist.")
        try:
            return self._state.add_source("local", clean_name, str(path), self._clock())
        except StateError as error:
            raise ConfigError(str(error)) from None

    def delete_source(self, source_id: int) -> None:
        self._state.delete_source(source_id)
        self._state.set_sealed(_drive_key_name(source_id), None, self._clock())


def parse_service_account(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_KEY_FILE_BYTES:
        raise ConfigError("That file is too large to be a service account key.")
    try:
        info = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ConfigError("That file is not a service account key (not JSON).") from None
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise ConfigError("That file is not a service account key.")
    for field in ("client_email", "private_key", "token_uri"):
        if not isinstance(info.get(field), str) or not info[field]:
            raise ConfigError(f"The service account key is missing {field}.")
    return info


def _drive_key_name(source_id: int) -> str:
    return f"source.{source_id}.service_account"


def _name(value: str) -> str:
    name = value.strip()
    if not name or len(name) > 60 or not name.isprintable():
        raise ConfigError("Give the source a short name (up to 60 characters).")
    return name


def _http_url(value: str, label: str) -> str:
    text = value.strip().rstrip("/")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"{label} must start with http:// or https://.")
    if parts.username or parts.password:
        raise ConfigError(f"{label} must not contain a user name or password.")
    if parts.query or parts.fragment:
        raise ConfigError(f"{label} must not contain ? or #.")
    if text.endswith("/api"):
        text = text.removesuffix("/api")
    return text
