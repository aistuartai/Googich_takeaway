"""Checking GitHub for a newer release.

Once a day, while the update check is switched on, the app asks GitHub for the latest release of
this project and compares it with its own version. A newer release shows a banner with the
release notes link and how to update. Nothing is ever downloaded or installed automatically: an
app holding Google and Immich credentials should not replace its own code. Pre-releases are
ignored. The request is anonymous and sends nothing but a User-Agent naming the app version.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from googich_takeaway import __version__
from googich_takeaway.state import State

REPOSITORY = "aistuartai/Googich_takeaway"
LATEST_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
CHECK_EVERY = timedelta(hours=24)
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
    try:
        with httpx.Client(timeout=10.0, transport=transport) as client:
            response = client.get(
                LATEST_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"googich-takeaway/{__version__}",
                },
            )
        if response.status_code == 404:
            return known  # no release published yet
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return known
    if (
        data.get("prerelease")
        or data.get("draft")
        or not parse_version(str(data.get("tag_name", "")))
    ):
        return known
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
