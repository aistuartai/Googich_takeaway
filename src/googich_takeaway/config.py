"""Settings made in the web interface, validated and stored in the state database.

Plain settings are stored as text. Credentials are sealed with the master key before storage and
are only ever unsealed to be used: the web interface can replace or remove them, never show them.
"""

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from googich_takeaway.credentials import SecretBox
from googich_takeaway.locations import (
    LocalLocation,
    Location,
    LocationError,
    SmbSettings,
    probe_smb,
    shared_smb,
)
from googich_takeaway.logs import DEFAULT_RETENTION_DAYS
from googich_takeaway.notify import (
    DEFAULT_OUTCOMES,
    Message,
    Notifier,
    Outcome,
    deliver,
    delivery_note,
    invalid_urls,
    service_name,
)
from googich_takeaway.schedule import (
    DEFAULT_TAKEOUT_MONTHS,
    Mode,
    Schedule,
    parse_time,
)
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
class SmbPublic:
    """SMB settings that may be shown; the password is stored sealed, separately."""

    server: str
    share: str
    folder: str
    username: str
    port: int = 445
    domain: str = ""

    def unc(self) -> str:
        parts = [p for p in self.folder.replace("/", "\\").split("\\") if p]
        return "\\\\" + "\\".join([self.server, self.share, *parts])


@dataclass(frozen=True)
class GeneralSettings:
    staging: Path | None
    """Local download folder, when storage is local."""
    timezone: str
    storage: str = "local"
    """``local`` or ``smb``."""
    smb: SmbPublic | None = None

    @property
    def configured(self) -> bool:
        return self.staging is not None if self.storage == "local" else self.smb is not None

    def describe(self) -> str:
        if self.storage == "smb" and self.smb:
            return self.smb.unc()
        return str(self.staging) if self.staging else "not set"


THEMES = {"auto": "Match this device", "light": "Light", "dark": "Dark"}
COLOUR_SCHEMES = {
    "spectrum": "Spectrum: sky to violet to green as work progresses",
    "ocean": "Ocean: blues and teals",
    "sunset": "Sunset: magenta, orange and gold",
}
BAR_STYLES = {"striped": "Striped bars", "segmented": "Segmented capsules"}
DASHBOARD_ITEMS = {
    "journey": "Where your photos are: Google Drive, download folder and Immich",
    "schedule": "Schedule: what it is set to, and the next run",
    "sources": "Sources",
    "destinations": "Destinations",
    "cleanup": "Cleanup: how much space can be freed",
    "runs": "History: past runs and their results",
}
DEFAULT_DASHBOARD_ITEMS = ("journey", "schedule", "runs")
MOTION = {"auto": "Animate unless this device asks for reduced motion", "off": "No animation"}


DOWNLOAD_FOLDER_KIND = "download-folder"
"""A source that is the download folder itself: archives saved there by hand."""
MIN_RETENTION_DAYS = 7
MAX_RETENTION_DAYS = 3650

_TARGETS = "notify.targets"


@dataclass(frozen=True)
class NotificationTarget:
    id: str
    name: str
    service: str
    """What Apprise calls the service, such as ``HomeAssistant``; the URL itself stays sealed."""


@dataclass(frozen=True)
class Look:
    theme: str = "auto"
    colours: str = "spectrum"
    bars: str = "striped"
    motion: str = "auto"


class Config:
    def __init__(self, state: State, box: SecretBox, clock: Callable[[], datetime]) -> None:
        self.state = state
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
        smb_json = self._state.get_setting("staging.smb")
        smb = SmbPublic(**json.loads(smb_json)) if smb_json else None
        return GeneralSettings(
            staging=Path(staging) if staging else None,
            timezone=self._state.get_setting("timezone") or "UTC",
            storage=self._state.get_setting("staging.storage") or "local",
            smb=smb,
        )

    def staging_location(self) -> Location | None:
        general = self.general()
        if general.storage == "smb":
            if general.smb is None:
                return None
            return shared_smb(self._smb_settings(general.smb, self._smb_password()))
        return LocalLocation(general.staging) if general.staging else None

    def save_general(self, staging: str, timezone: str | None = None) -> None:
        """Use a local download folder. ``timezone`` None keeps the saved time zone."""
        path = Path(staging.strip())
        if not staging.strip() or not path.is_absolute():
            raise ConfigError("The download folder must be a full path, starting with /.")
        if ".." in path.parts:
            raise ConfigError("The download folder must not contain '..'.")
        zone = _zone_name(timezone) if timezone is not None else None
        try:
            LocalLocation(path).prepare()
        except LocationError as error:
            raise ConfigError(str(error)) from None
        now = self._clock()
        self._state.set_setting("staging.path", str(path), now)
        self._state.set_setting("staging.storage", "local", now)
        if zone:
            self._state.set_setting("timezone", zone, now)

    def save_smb(
        self,
        server: str,
        share: str,
        folder: str,
        username: str,
        password: str | None,
        timezone: str | None = None,
        port: str = "445",
        domain: str = "",
        test: Callable[[SmbSettings], None] | None = None,
    ) -> None:
        """Use a folder on an SMB share. ``password`` None or empty keeps the stored one.

        The share is tested (create folder, write and delete a file) before anything is saved.
        """
        public = SmbPublic(
            server=_host(server),
            share=_smb_part(share, "share"),
            folder="/".join(
                _smb_part(p, "folder") for p in folder.replace("\\", "/").split("/") if p
            ),
            username=username.strip(),
            port=_port(port),
            domain=domain.strip(),
        )
        if not public.username or len(public.username) > 256:
            raise ConfigError("Enter the SMB user name.")
        secret = password if password else self._smb_password()
        if not secret:
            raise ConfigError("Enter the SMB password.")
        zone = _zone_name(timezone) if timezone is not None else None
        settings = self._smb_settings(public, secret)
        try:
            (test or probe_smb)(settings)
        except LocationError as error:
            raise ConfigError(str(error)) from None
        now = self._clock()
        self._state.set_setting("staging.smb", json.dumps(public.__dict__), now)
        if password:
            self._state.set_sealed(
                "staging.smb.password",
                self._box.seal("staging.smb.password", password.encode()),
                now,
            )
        self._state.set_setting("staging.storage", "smb", now)
        if zone:
            self._state.set_setting("timezone", zone, now)

    def save_timezone(self, timezone: str) -> None:
        """The time zone for photos with no time zone information of their own."""
        self._state.set_setting("timezone", _zone_name(timezone), self._clock())

    def has_smb_password(self) -> bool:
        return self._state.get_sealed("staging.smb.password") is not None

    def _smb_password(self) -> str | None:
        sealed = self._state.get_sealed("staging.smb.password")
        return self._box.open("staging.smb.password", sealed).decode() if sealed else None

    @staticmethod
    def _smb_settings(public: SmbPublic, password: str | None) -> SmbSettings:
        return SmbSettings(
            server=public.server,
            share=public.share,
            folder=public.folder,
            username=public.username,
            password=password or "",
            port=public.port,
            domain=public.domain,
        )

    # --- log retention ---------------------------------------------------------------------------

    def log_retention_days(self) -> int:
        stored = self._state.get_setting("logs.retention_days")
        try:
            days = int(stored) if stored else DEFAULT_RETENTION_DAYS
        except ValueError:
            days = DEFAULT_RETENTION_DAYS
        return min(max(days, MIN_RETENTION_DAYS), MAX_RETENTION_DAYS)

    def save_log_retention(self, days: str) -> None:
        try:
            value = int(days.strip())
        except ValueError:
            raise ConfigError("Enter the number of days to keep logs, as a whole number.") from None
        if not MIN_RETENTION_DAYS <= value <= MAX_RETENTION_DAYS:
            raise ConfigError(
                f"Keep logs for between {MIN_RETENTION_DAYS} and {MAX_RETENTION_DAYS} days."
            )
        self._state.set_setting("logs.retention_days", str(value), self._clock())

    # --- look and feel ---------------------------------------------------------------------------

    def look(self) -> Look:
        stored = self._state.get_setting("ui.look")
        if not stored:
            return Look()
        data = json.loads(stored)
        return Look(
            theme=data.get("theme", "auto") if data.get("theme") in THEMES else "auto",
            colours=data.get("colours", "spectrum")
            if data.get("colours") in COLOUR_SCHEMES
            else "spectrum",
            bars=data.get("bars", "striped") if data.get("bars") in BAR_STYLES else "striped",
            motion=data.get("motion", "auto") if data.get("motion") in MOTION else "auto",
        )

    def save_look(self, theme: str, colours: str, bars: str, motion: str) -> None:
        for value, allowed, label in (
            (theme, THEMES, "theme"),
            (colours, COLOUR_SCHEMES, "colour scheme"),
            (bars, BAR_STYLES, "progress bar style"),
            (motion, MOTION, "animation setting"),
        ):
            if value not in allowed:
                raise ConfigError(f"Choose a {label} from the list.")
        chosen = {"theme": theme, "colours": colours, "bars": bars, "motion": motion}
        self._state.set_json("ui.look", chosen, self._clock())

    # --- schedule --------------------------------------------------------------------------------

    def schedule(self) -> Schedule:
        data = self._state.get_json("schedule")
        _, latest = self._state.download_dates()
        zone = ZoneInfo(_zone_name(self.general().timezone))
        defaults = Schedule()
        return Schedule(
            mode=Mode(data.get("mode", defaults.mode.value)),
            at=parse_time(data.get("at", f"{defaults.at:%H:%M}")),
            weekday=int(data.get("weekday", defaults.weekday)),
            every_hours=int(data.get("every_hours", defaults.every_hours)),
            pause_after=int(data.get("pause_after", defaults.pause_after)),
            takeout_started=self.takeout_schedule_started(),
            takeout_months=self.takeout_months(),
            retry_days=int(data.get("retry_days", defaults.retry_days)),
            wait_days=int(data.get("wait_days", defaults.wait_days)),
            fallback_weekly=bool(data.get("weekly_between", defaults.fallback_weekly)),
            fallback_weekday=int(data.get("fallback_weekday", defaults.fallback_weekday)),
            latest_download=latest.astimezone(zone).date() if latest else None,
        )

    def save_schedule(
        self, mode: str, at: str, weekday: str, every_hours: str, pause_after: str
    ) -> None:
        """How often to run. The options for following Takeout are kept."""
        try:
            chosen = Mode(mode)
        except ValueError:
            raise ConfigError("Choose how often to run.") from None
        try:
            local_time = parse_time(at)
        except ValueError as error:
            raise ConfigError(str(error)) from None
        try:
            day, hours, pause = int(weekday), int(every_hours), int(pause_after)
        except ValueError:
            raise ConfigError("Use whole numbers for the day, hours and failures.") from None
        if not 0 <= day <= 6:
            raise ConfigError("Choose a day of the week.")
        if not 1 <= hours <= 168:
            raise ConfigError("Run every 1 to 168 hours.")
        if not 1 <= pause <= 20:
            raise ConfigError("Pause after 1 to 20 failed runs in a row.")
        if chosen is Mode.TAKEOUT and self.takeout_schedule_started() is None:
            raise ConfigError(
                "To follow your Takeout schedule, first note the day you set it up, above."
            )
        self._update_schedule(
            mode=chosen.value,
            at=f"{local_time:%H:%M}",
            weekday=day,
            every_hours=hours,
            pause_after=pause,
        )

    def save_follow_options(
        self, retry_days: str, wait_days: str, fallback_weekly: bool, fallback_weekday: str
    ) -> None:
        """How following Takeout waits for each export, and whether it runs between them."""
        try:
            retry, wait, day = int(retry_days), int(wait_days), int(fallback_weekday)
        except ValueError:
            raise ConfigError("Use whole numbers for the days.") from None
        if not 1 <= retry <= 7:
            raise ConfigError("Try again every 1 to 7 days.")
        if not 1 <= wait <= 30:
            raise ConfigError("Keep trying for 1 to 30 days.")
        if not 0 <= day <= 6:
            raise ConfigError("Choose a day of the week.")
        self._update_schedule(
            retry_days=retry,
            wait_days=wait,
            weekly_between=fallback_weekly,
            fallback_weekday=day,
        )

    def _update_schedule(self, **values: object) -> None:
        data = self._state.get_json("schedule")
        data.update(values)
        self._state.set_json("schedule", data, self._clock())

    def schedule_paused(self) -> bool:
        return self._state.get_setting("schedule.paused") is not None

    def schedule_paused_by_user(self) -> bool:
        return (self._state.get_setting("schedule.paused") or "").startswith("user:")

    def set_schedule_paused(self, paused: bool, by_user: bool = False) -> None:
        now = self._clock()
        value = ("user:" if by_user else "") + now.isoformat() if paused else None
        self._state.set_setting("schedule.paused", value, now)
        if not paused:
            self._state.set_setting("schedule.failures", None, now)

    def scheduled_failures(self) -> int:
        return int(self._state.get_setting("schedule.failures") or 0)

    def set_scheduled_failures(self, count: int) -> None:
        self._state.set_setting("schedule.failures", str(count) if count else None, self._clock())

    # --- notifications ---------------------------------------------------------------------------

    def notification_outcomes(self) -> frozenset[Outcome]:
        stored = self._state.get_setting("notify.outcomes")
        if stored is None:
            return DEFAULT_OUTCOMES
        return frozenset(Outcome(o) for o in json.loads(stored) if o in Outcome)

    def has_notification_urls(self) -> bool:
        return bool(self._targets())

    def notification_targets(self) -> list[NotificationTarget]:
        """Saved notification services, by name. Their URLs are never returned."""
        return [
            NotificationTarget(t["id"], t["name"], service_name(t["url"])) for t in self._targets()
        ]

    def add_notification_target(self, name: str, url: str) -> str:
        clean = name.strip()
        if not clean or len(clean) > 60 or not clean.isprintable():
            raise ConfigError("Give the notification a short name (up to 60 characters).")
        address = url.strip()
        if not address:
            raise ConfigError("Enter the Apprise URL.")
        if invalid_urls([address]):
            raise ConfigError(_url_hint(address))
        targets = self._targets()
        if len(targets) >= 20:
            raise ConfigError("Use at most 20 notifications.")
        if any(t["name"].casefold() == clean.casefold() for t in targets):
            raise ConfigError(f"There is already a notification called {clean!r}.")
        target_id = secrets.token_hex(4)
        targets.append({"id": target_id, "name": clean, "url": address})
        self._save_targets(targets)
        return target_id

    def remove_notification_target(self, target_id: str) -> bool:
        targets = self._targets()
        kept = [t for t in targets if t["id"] != target_id]
        self._save_targets(kept)
        return len(kept) < len(targets)

    def test_notification(self, target_id: str | None = None) -> tuple[bool, list[str], str]:
        """Send a test to one saved notification, or to all of them.

        Returns whether it was delivered, the services' reasons if not, and for a single one, a
        note on where it went when that is not obvious."""
        urls = [t["url"] for t in self._targets() if target_id in (None, t["id"])]
        if not urls:
            return False, ["no such notification"], ""
        message = Message(Outcome.SUCCESS, "Test notification", ["Notifications are working."])
        ok, reasons = deliver(urls, message)
        note = delivery_note(urls[0]) if target_id is not None else ""
        return ok, reasons, note

    def save_notifications(self, urls: str | None, outcomes: list[str]) -> None:
        """Choose what to be told about. ``urls`` (one per line) replaces every saved
        notification, each named after its service; None keeps them, an empty string removes
        them."""
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
            self._save_targets(_named(lines))
        self._state.set_setting("notify.outcomes", json.dumps(chosen), now)

    def notifier(self) -> Notifier:
        return Notifier([t["url"] for t in self._targets()], self.notification_outcomes())

    def _targets(self) -> list[dict[str, str]]:
        sealed = self._state.get_sealed(_TARGETS)
        if sealed:
            data = json.loads(self._box.open(_TARGETS, sealed))
            return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []
        old = self._state.get_sealed("notify.urls")  # before 0.3.0: URLs one per line, no names
        if not old:
            return []
        targets = _named(self._box.open("notify.urls", old).decode().splitlines())
        self._save_targets(targets)
        self._state.set_sealed("notify.urls", None, self._clock())
        return targets

    def _save_targets(self, targets: list[dict[str, str]]) -> None:
        sealed = self._box.seal(_TARGETS, json.dumps(targets).encode()) if targets else None
        self._state.set_sealed(_TARGETS, sealed, self._clock())

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

    def add_download_folder_source(self, name: str) -> int:
        """Archives the user downloads from Takeout's emails and saves into the download
        folder. The download folder is read on every run anyway; this source says that is
        how exports arrive, so setup is complete without Google Drive."""
        clean_name = _name(name or "My Takeout downloads")
        if any(s.kind == DOWNLOAD_FOLDER_KIND for s in self.sources()):
            raise ConfigError("The download folder is already a source.")
        try:
            return self._state.add_source(
                DOWNLOAD_FOLDER_KIND, clean_name, DOWNLOAD_FOLDER_KIND, self._clock()
            )
        except StateError as error:
            raise ConfigError(str(error)) from None

    def drive_folder_name(self, source_id: int) -> str | None:
        """The Drive folder's own name, as last seen by a test or a run."""
        return self._state.get_setting(f"source.{source_id}.folder_name")

    def set_drive_folder_name(self, source_id: int, name: str) -> None:
        clean = " ".join(name.split())[:200]
        self._state.set_setting(f"source.{source_id}.folder_name", clean or None, self._clock())

    def delete_source(self, source_id: int) -> None:
        self._state.delete_source(source_id)
        self._state.set_sealed(_drive_key_name(source_id), None, self._clock())
        self._state.set_setting(f"source.{source_id}.folder_name", None, self._clock())

    # --- Takeout schedule ----------------------------------------------------------------------

    def takeout_schedule_started(self) -> date | None:
        """When the user set up their scheduled Takeout export, if they noted it."""
        stored = self._state.get_setting("takeout.schedule_started")
        return date.fromisoformat(stored) if stored else None

    def takeout_months(self) -> int:
        """How often the Takeout schedule exports: every 1 or 2 months."""
        stored = self._state.get_setting("takeout.every_months")
        return int(stored) if stored in ("1", "2") else DEFAULT_TAKEOUT_MONTHS

    def save_takeout_schedule(self, started: str, every_months: str) -> None:
        if every_months not in ("1", "2"):
            raise ConfigError("Choose how often Takeout exports.")
        self.save_takeout_schedule_started(started)
        self._state.set_setting("takeout.every_months", every_months, self._clock())

    def save_takeout_schedule_started(self, value: str) -> None:
        """``value`` is YYYY-MM-DD, or empty to forget it."""
        text = value.strip()
        if not text:
            self._state.set_setting("takeout.schedule_started", None, self._clock())
            return
        try:
            started = date.fromisoformat(text)
        except ValueError:
            raise ConfigError("Enter the date as year-month-day, for example 2026-10-01.") from None
        if started > self._clock().date() or started.year < 2010:
            raise ConfigError("That date is not possible for a Takeout schedule.")
        self._state.set_setting("takeout.schedule_started", started.isoformat(), self._clock())

    # --- dashboard -------------------------------------------------------------------------------

    def dashboard_items(self) -> list[str]:
        """What the dashboard shows, besides the run controls, which are always there."""
        stored = self._state.get_setting("dashboard.items")
        if stored is None:
            return list(DEFAULT_DASHBOARD_ITEMS)
        chosen = set(stored.split(","))
        # Items added in a later release are shown by default, until the user chooses again.
        offered = set((self._state.get_setting("dashboard.offered") or "").split(","))
        if offered == {""}:  # chosen in 0.3.0, which offered these
            offered = {"journey", "sources", "destinations", "cleanup", "runs"}
        new = {item for item in DEFAULT_DASHBOARD_ITEMS if item not in offered}
        return [item for item in DASHBOARD_ITEMS if item in chosen | new]

    def save_dashboard_items(self, items: list[str]) -> None:
        unknown = set(items) - set(DASHBOARD_ITEMS)
        if unknown:
            raise ConfigError("Unknown dashboard item.")
        chosen = ",".join(item for item in DASHBOARD_ITEMS if item in items)
        self._state.set_setting("dashboard.items", chosen, self._clock())
        self._state.set_setting("dashboard.offered", ",".join(DASHBOARD_ITEMS), self._clock())


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


def _zone_name(value: str) -> str:
    try:
        ZoneInfo(value.strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"Unknown time zone {value.strip()!r}.") from None
    return value.strip()


_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}")
_SMB_PART = re.compile(r"[^\\/:*?\"<>|\x00-\x1f]{1,255}")


def _host(value: str) -> str:
    host = value.strip().removeprefix("\\\\").removeprefix("//").rstrip("/\\")
    if not _HOST.fullmatch(host):
        raise ConfigError("Enter the SMB server as a name or IP address, for example nas.local.")
    return host


def _smb_part(value: str, label: str) -> str:
    text = value.strip()
    if not _SMB_PART.fullmatch(text) or text in (".", ".."):
        raise ConfigError(f"The SMB {label} name contains characters SMB does not allow.")
    return text


def _port(value: str) -> int:
    try:
        port = int(value or "445")
    except ValueError:
        raise ConfigError("The SMB port must be a number.") from None
    if not 1 <= port <= 65535:
        raise ConfigError("The SMB port must be between 1 and 65535.")
    return port


def _drive_key_name(source_id: int) -> str:
    return f"source.{source_id}.service_account"


def _named(urls: list[str]) -> list[dict[str, str]]:
    """Name unnamed URLs after their service: ``ntfy``, ``ntfy 2``, ``HomeAssistant``."""
    targets: list[dict[str, str]] = []
    for url in (u.strip() for u in urls):
        if not url:
            continue
        base = service_name(url)
        taken = {t["name"] for t in targets}
        name = base
        number = 2
        while name in taken:
            name = f"{base} {number}"
            number += 1
        targets.append({"id": secrets.token_hex(4), "name": name, "url": url})
    return targets


def _url_hint(url: str) -> str:
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if scheme in ("http", "https"):
        return (
            "Apprise does not recognise that URL: it chooses the service by the start of the "
            "address, so it does not begin with http:// or https://. Home Assistant addresses "
            "start with hassio:// (or hassios:// for HTTPS), and a plain web request (a "
            "webhook) with json://."
        )
    return "Apprise does not recognise that URL. See the Apprise documentation for URL formats."


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
