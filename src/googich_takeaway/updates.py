"""Checking GitHub for a newer release.

Once a day while the update check is switched on, and whenever the user presses Check now, the
app asks GitHub for the latest release of this project and compares it with its own version. A
newer release shows a banner with the release notes link and how to update. Nothing is ever
downloaded or installed automatically: an app holding Google and Immich credentials should not
replace its own code. Pre-releases are ignored. The request is anonymous and sends nothing but a
User-Agent naming the app version.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

import httpx

from googich_takeaway import __version__
from googich_takeaway.state import State

REPOSITORY = "aistuartai/Googich_takeaway"
LATEST_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
CHECK_EVERY = timedelta(hours=24)
MANUAL_GAP = timedelta(minutes=1)
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True)
class UpdateInfo:
    latest: str
    url: str
    checked_at: datetime

    @property
    def newer(self) -> bool:
        return is_newer(self.latest, __version__)


def parse_version(value: str) -> tuple[int, int, int] | None:
    """``1.2.3`` or ``v1.2.3``; development and pre-release versions return None."""
    match = _VERSION.match(value.strip())
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def is_newer(latest: str, current: str) -> bool:
    new = parse_version(latest)
    if new is None:
        return False
    base = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", current)
    if base is None:
        return True
    have = (int(base[1]), int(base[2]), int(base[3]))
    if new != have:
        return new > have
    # Same numbers: a development build of X.Y.Z is older than the X.Y.Z release.
    return parse_version(current) is None


def enabled(state: State) -> bool:
    return state.get_setting("updates.enabled") != "0"


def set_enabled(state: State, on: bool, at: datetime) -> None:
    state.set_setting("updates.enabled", None if on else "0", at)


def cached(state: State) -> UpdateInfo | None:
    stored = state.get_setting("updates.latest")
    if not stored:
        return None
    data = json.loads(stored)
    return UpdateInfo(data["latest"], data["url"], datetime.fromisoformat(data["checked_at"]))


def check_if_due(
    state: State,
    clock: Callable[[], datetime],
    transport: httpx.BaseTransport | None = None,
) -> UpdateInfo | None:
    """Ask GitHub if the last check is over a day old. Never raises; returns the latest known."""
    known = cached(state)
    if not enabled(state):
        return known
    now = clock()
    if known and now - known.checked_at < CHECK_EVERY:
        return known
    attempted = state.get_setting("updates.attempted")
    if attempted and now - datetime.fromisoformat(attempted) < timedelta(hours=1):
        return known  # failed recently; do not hammer GitHub
    state.set_setting("updates.attempted", now.isoformat(), now)
    return _fetch(state, now, transport) or known


VIEW_FRESH = timedelta(minutes=10)


def check_on_view(
    state: State,
    clock: Callable[[], datetime],
    transport: httpx.BaseTransport | None = None,
) -> UpdateInfo | None:
    """Ask GitHub when the About or Updates page opens, so the latest release shown is current.

    Only while checks are switched on, and at most once a minute; a reading under 10 minutes
    old is used as it is, unless it is older than the version running (it was saved before an
    update). Never raises; returns the latest known."""
    known = cached(state)
    if not enabled(state):
        return known
    now = clock()
    stale = known is None or is_newer(__version__, known.latest)
    if known and not stale and now - known.checked_at < VIEW_FRESH:
        return known
    attempted = state.get_setting("updates.view_attempted")
    if attempted and now - datetime.fromisoformat(attempted) < MANUAL_GAP:
        return known
    state.set_setting("updates.view_attempted", now.isoformat(), now)
    return _fetch(state, now, transport, timeout=5.0) or known


class CheckResult(StrEnum):
    NEWER = "newer"
    CURRENT = "current"
    FAILED = "failed"
    WAIT = "wait"


def check_now(
    state: State,
    clock: Callable[[], datetime],
    transport: httpx.BaseTransport | None = None,
) -> CheckResult:
    """Ask GitHub straight away, when the user presses Check now. Never raises.

    At most one manual check a minute, so repeated presses cannot use up GitHub's limit of 60
    anonymous requests an hour, which is shared by everything on this network's address.
    """
    now = clock()
    last = state.get_setting("updates.manual")
    if last and now - datetime.fromisoformat(last) < MANUAL_GAP:
        return CheckResult.WAIT
    state.set_setting("updates.manual", now.isoformat(), now)
    info = _fetch(state, now, transport)
    if info is None:
        return CheckResult.FAILED
    return CheckResult.NEWER if info.newer else CheckResult.CURRENT


def _fetch(
    state: State,
    now: datetime,
    transport: httpx.BaseTransport | None,
    timeout: float = 10.0,
) -> UpdateInfo | None:
    """The latest release, saved for the banner; None if GitHub could not say."""
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            response = client.get(
                LATEST_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"googich-takeaway/{__version__}",
                },
            )
        if response.status_code == 404:
            return None  # no release published yet
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if (
        data.get("prerelease")
        or data.get("draft")
        or not parse_version(str(data.get("tag_name", "")))
    ):
        return None
    url = str(data.get("html_url") or f"https://github.com/{REPOSITORY}/releases")
    if not url.startswith(f"https://github.com/{REPOSITORY}/"):
        url = f"https://github.com/{REPOSITORY}/releases"  # only ever link to this project
    info = UpdateInfo(str(data["tag_name"]).lstrip("v"), url, now)
    state.set_setting(
        "updates.latest",
        json.dumps({"latest": info.latest, "url": info.url, "checked_at": now.isoformat()}),
        now,
    )
    return info


# --- one-click updates through the optional host helper ---------------------------------------
#
# The app never touches Docker. It writes the version to install into <data>/updater/request.json;
# googich-updater on the host checks it is a published release, switches the image tag, restarts
# the container and reports back in status.json. Without the helper there is no button, only the
# banner with the manual command.


@dataclass(frozen=True)
class HelperStatus:
    state: str
    """``idle``, ``updating``, ``done`` or ``failed``."""
    message: str
    version: str
    at: datetime | None


def helper_status(data_dir: Path) -> HelperStatus | None:
    """What the host update helper last reported, or None if it is not installed."""
    path = data_dir / "updater" / "status.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "helper" not in data:
        return None
    try:
        at = datetime.fromisoformat(str(data.get("at", "")))
    except ValueError:
        at = None
    return HelperStatus(
        state=str(data.get("state", "idle")),
        message=str(data.get("message", ""))[:300],
        version=str(data.get("version", ""))[:20],
        at=at,
    )


def request_update(data_dir: Path, version: str) -> None:
    """Ask the host helper to install ``version``. Raises ValueError if it cannot be asked."""
    if parse_version(version) is None:
        raise ValueError("not a release version")
    folder = data_dir / "updater"
    if not (folder / "status.json").exists():
        raise ValueError("the update helper is not installed")
    temporary = folder / ".request.json.tmp"
    temporary.write_text(json.dumps({"version": version}))
    temporary.chmod(0o644)
    temporary.replace(folder / "request.json")  # one atomic write for the helper to see
