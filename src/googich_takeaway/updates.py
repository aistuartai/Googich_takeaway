"""Checking GitHub for a newer release.

Once a day while the update check is switched on, and whenever the user presses Check now, the
app asks GitHub for the latest release of this project and compares it with its own version. A
newer release shows a banner leading to what it brings (``changelog``) and how to update. Nothing
is ever downloaded or installed automatically: an app holding Google and Immich credentials
should not replace its own code. The request is anonymous and sends nothing but a User-Agent
naming the app version.

Release candidates (``X.Y.Z-rcN``, published on GitHub as pre-releases) are offered only to those
who switch them on: they are test builds of the next release. With them on, the check reads the
releases feed, which lists pre-releases too, instead of the latest release, which never is one.
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
LATEST_PAGE = f"https://github.com/{REPOSITORY}/releases/latest"
"""Redirects to the newest published release (never a draft or pre-release). Not part of
GitHub's API, so not subject to its 60 anonymous requests an hour per network address, which
everything on the network shares."""
RELEASES_FEED = f"https://github.com/{REPOSITORY}/releases.atom"
"""Every published release, pre-releases included, newest first. Also not part of the API."""
_TAG_PAGE = re.compile(rf"^https://github\.com/{re.escape(REPOSITORY)}/releases/tag/(v[^/?#]+)$")
_FEED_TAG = re.compile(
    rf'href="https://github\.com/{re.escape(REPOSITORY)}/releases/tag/(v[^"/?#]+)"'
)
FEED_LIMIT = 1_000_000
API_LIMIT = 512_000
"""The latest release as JSON: a few kilobytes, its notes included."""


class TooLarge(ValueError):
    """An answer larger than any GitHub sends for this: not read further."""


def _read_limited(response: httpx.Response, limit: int) -> bytes:
    """A streamed answer's body, stopping as soon as it passes ``limit`` bytes."""
    body = bytearray()
    for chunk in response.iter_bytes():
        body += chunk
        if len(body) > limit:
            raise TooLarge(f"more than {limit} bytes")
    return bytes(body)


CHECK_EVERY = timedelta(hours=24)
MANUAL_GAP = timedelta(minutes=1)
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-?rc(\d+))?$")
_FINAL = 1 << 30
"""Where a final release sorts against its own release candidates: after all of them."""


@dataclass(frozen=True)
class UpdateInfo:
    latest: str
    url: str
    checked_at: datetime

    @property
    def newer(self) -> bool:
        return is_newer(self.latest, __version__)

    @property
    def candidate(self) -> bool:
        return is_candidate(self.latest)


def parse_version(value: str) -> tuple[int, int, int, int] | None:
    """``1.2.3``, ``v1.2.3`` or a release candidate ``1.2.3-rc1`` (``1.2.3rc1`` as Python writes
    it), as a key that sorts releases in order; development versions return None."""
    match = _VERSION.match(value.strip())
    if match is None:
        return None
    rc = int(match[4]) if match[4] is not None else _FINAL
    return (int(match[1]), int(match[2]), int(match[3]), rc)


def is_candidate(version: str) -> bool:
    key = parse_version(version)
    return key is not None and key[3] != _FINAL


def is_newer(latest: str, current: str) -> bool:
    new = parse_version(latest)
    if new is None:
        return False
    have = parse_version(current)
    if have is None:
        base = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", current)
        if base is None:
            return True
        # A development build of X.Y.Z is older than X.Y.Z and its release candidates.
        have = (int(base[1]), int(base[2]), int(base[3]), -1)
    return new > have


def enabled(state: State) -> bool:
    return state.get_setting("updates.enabled") != "0"


def set_enabled(state: State, on: bool, at: datetime) -> None:
    state.set_setting("updates.enabled", None if on else "0", at)


def candidates(state: State) -> bool:
    """Whether release candidates are offered as updates."""
    return state.get_setting("updates.candidates") == "1"


def set_candidates(state: State, on: bool, at: datetime) -> None:
    """Switch release candidates on or off. The last reading is dropped and the one-a-minute
    limits reset, so the next check answers for the new choice straight away rather than
    offering what the old one found."""
    if on == candidates(state):
        return
    state.set_setting("updates.candidates", "1" if on else None, at)
    for key in ("updates.latest", "updates.view_attempted", "updates.manual"):
        state.set_setting(key, None, at)


def cached(state: State) -> UpdateInfo | None:
    stored = state.get_setting("updates.latest")
    if not stored:
        return None
    data = json.loads(stored)
    info = UpdateInfo(data["latest"], data["url"], datetime.fromisoformat(data["checked_at"]))
    if info.candidate and info.newer and not candidates(state):
        return None  # found while release candidates were on: not offered now
    return info


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
    """The latest release, saved for the banner; None if GitHub could not say.

    With release candidates on, reads the releases feed first. Then asks the releases page, and
    the API only if that gives no answer."""
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            if candidates(state):
                newest = _newest_in_feed(client)
                if newest is not None:
                    tag = f"v{newest}"
                    return _save(
                        state, now, newest, f"https://github.com/{REPOSITORY}/releases/tag/{tag}"
                    )
            page = client.get(
                LATEST_PAGE, headers={"User-Agent": f"googich-takeaway/{__version__}"}
            )
            found = _TAG_PAGE.match(page.headers.get("location", "")) if page.is_redirect else None
            if found and parse_version(found[1]):
                return _save(state, now, found[1].lstrip("v"), found[0])
            with client.stream(
                "GET",
                LATEST_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"googich-takeaway/{__version__}",
                },
            ) as response:
                body = _read_limited(response, API_LIMIT) if response.status_code == 200 else b""
        if response.status_code == 404:
            _failed(state, now, "no release published yet")
            return None
        if response.status_code in (403, 429):
            _failed(state, now, f"GitHub refused (HTTP {response.status_code}): its limit of 60 "
                    "checks an hour from each network is used up")  # fmt: skip
            return None
        response.raise_for_status()
        data = json.loads(body)
    except httpx.HTTPStatusError as error:
        _failed(state, now, f"GitHub answered HTTP {error.response.status_code}")
        return None
    except httpx.TimeoutException:
        _failed(state, now, "GitHub did not answer in time")
        return None
    except httpx.HTTPError as error:
        _failed(state, now, f"could not connect to GitHub ({type(error).__name__})")
        return None
    except ValueError:
        _failed(state, now, "GitHub sent an answer that could not be read")
        return None
    if (
        data.get("prerelease")
        or data.get("draft")
        or not parse_version(str(data.get("tag_name", "")))
    ):
        return None
    url = str(data.get("html_url") or f"https://github.com/{REPOSITORY}/releases")
    return _save(state, now, str(data["tag_name"]).lstrip("v"), url)


def _newest_in_feed(client: httpx.Client) -> str | None:
    """The newest release or release candidate the releases feed lists; None if it cannot say."""
    try:
        with client.stream(
            "GET", RELEASES_FEED, headers={"User-Agent": f"googich-takeaway/{__version__}"}
        ) as response:
            if response.status_code != 200:
                return None
            text = _read_limited(response, FEED_LIMIT).decode("utf-8", "replace")
    except (httpx.HTTPError, TooLarge):
        return None
    versions = [tag for tag in _FEED_TAG.findall(text) if parse_version(tag)]
    if not versions:
        return None
    newest = max(versions, key=lambda tag: parse_version(tag) or (0, 0, 0, 0))
    return display(newest)


def display(version: str) -> str:
    """A version as tags and images write it: ``1.2.3`` or ``1.2.3-rc1``, never ``v`` first."""
    key = parse_version(version)
    if key is None:
        return version
    numbers = f"{key[0]}.{key[1]}.{key[2]}"
    return numbers if key[3] == _FINAL else f"{numbers}-rc{key[3]}"


def _failed(state: State, now: datetime, why: str) -> None:
    state.set_setting("updates.error", why, now)


def last_error(state: State) -> str | None:
    """Why the last check failed, if it did."""
    return state.get_setting("updates.error")


def _save(state: State, now: datetime, latest: str, url: str) -> UpdateInfo:
    state.set_setting("updates.error", None, now)
    if not url.startswith(f"https://github.com/{REPOSITORY}/"):
        url = f"https://github.com/{REPOSITORY}/releases"  # only ever link to this project
    info = UpdateInfo(latest, url, now)
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


HELPER_VERSION = 6
"""The update helper this release ships (``HELPER_VERSION`` in googich-updater.sh)."""
CANDIDATE_HELPER = 4
"""The first helper version that installs release candidates."""


@dataclass(frozen=True)
class HelperStatus:
    state: str
    """``idle``, ``updating``, ``done`` or ``failed``."""
    message: str
    version: str
    at: datetime | None
    helper: int = 0
    """Which helper version reported this."""

    @property
    def outdated(self) -> bool:
        return self.helper < HELPER_VERSION

    @property
    def installs_candidates(self) -> bool:
        return self.helper >= CANDIDATE_HELPER


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
        helper=int(str(data.get("helper", "0"))) if str(data.get("helper", "0")).isdigit() else 0,
    )


def installer_sha256() -> str | None:
    """SHA-256 of this release's install-updater.sh, from the copy shipped inside the app.

    Shown beside the install command, so the downloaded installer can be checked against a
    source already trusted (the app image), not only against GitHub."""
    import hashlib
    from importlib.resources import files

    shipped = files("googich_takeaway").joinpath("updater_files", "install-updater.sh")
    in_checkout = Path(__file__).parents[2] / "deploy" / "updater" / "install-updater.sh"
    for read in (shipped.read_bytes, in_checkout.read_bytes):  # installed app, then a checkout
        try:
            return hashlib.sha256(read()).hexdigest()
        except OSError:
            continue
    return None


def request_update(data_dir: Path, version: str) -> None:
    """Ask the host helper to install ``version``. Raises ValueError if it cannot be asked.

    A release candidate is marked as one, so the helper knows it was meant: it refuses
    candidates otherwise."""
    if parse_version(version) is None:
        raise ValueError("not a release version")
    folder = data_dir / "updater"
    if not (folder / "status.json").exists():
        raise ValueError("the update helper is not installed")
    request: dict[str, object] = {"version": display(version)}
    if is_candidate(version):
        request["candidates"] = True
    temporary = folder / ".request.json.tmp"
    temporary.write_text(json.dumps(request))
    temporary.chmod(0o644)
    temporary.replace(folder / "request.json")  # one atomic write for the helper to see
