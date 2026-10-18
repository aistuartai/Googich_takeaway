"""Level 2, before a release: the update path rehearsed on a throwaway machine, as root, with
real Docker, systemd and the real update helper, against releases published on GitHub.

Run by rehearsal/guest.sh inside the machine rehearsal/proxmox.sh makes, never on a machine
that matters: it installs into /opt/googich and edits /etc/hosts.

    E2E_REHEARSAL=1 E2E_FROM=0.6.0-rc1 E2E_TO=0.6.0-rc2 pytest e2e/test_rehearsal.py

E2E_FROM is the published release installed first, E2E_TO the published release to update to
through Update now. With E2E_FROM the same as E2E_TO, only a fresh install of it is rehearsed.
"""

import json
import os
import re
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from e2e.conftest import Visitor
from e2e.stack import Install, health, released_image, wait_for_version

pytestmark = pytest.mark.skipif(
    os.environ.get("E2E_REHEARSAL") != "1", reason="only inside a rehearsal machine"
)
SITE = Path("/opt/googich")
STATUS = SITE / "data" / "updater" / "status.json"
REQUEST = SITE / "data" / "updater" / "request.json"
HOSTS = Path("/etc/hosts")
BLOCK = (
    "0.0.0.0 ghcr.io pkg-containers.githubusercontent.com  # googich rehearsal\n"
    ":: ghcr.io pkg-containers.githubusercontent.com  # googich rehearsal\n"
)
VERSION = re.compile(r"^\d+\.\d+\.\d+(-rc\d+)?$")


def _version(name: str) -> str:
    value = os.environ.get(name, "")
    assert VERSION.match(value), f"{name} should be a version such as 0.6.0 or 0.6.0-rc1"
    return value


def _example(version: str) -> str:
    """The example compose file as that release shipped it."""
    url = f"https://raw.githubusercontent.com/aistuartai/Googich_takeaway/v{version}/docker/compose.yaml"
    with urllib.request.urlopen(url, timeout=30) as answer:
        return str(answer.read().decode())


def _running(seconds: float = 180) -> str:
    """The version answering, once one does."""
    deadline = time.monotonic() + seconds
    while (answer := health()) is None:
        assert time.monotonic() < deadline, "the app is not answering"
        time.sleep(2)
    return str(answer["version"])


def _helper() -> dict[str, str]:
    try:
        return dict(json.loads(STATUS.read_text()))
    except (OSError, ValueError):
        return {}


def _helper_reports(state: str, at_least_after: str, seconds: float = 300) -> dict[str, str]:
    """Until the helper reports ``state`` with a time after ``at_least_after``."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status = _helper()
        if status.get("state") == state and status.get("at", "") > at_least_after:
            return status
        time.sleep(2)
    raise AssertionError(f"the helper never reported {state}; last: {_helper()}")


def _status_file() -> tuple[int, int, str] | None:
    """What identifies one writing of the status file: its inode, its time to the nanosecond
    and its text. The inode alone is not enough: the helper may write twice for one request
    ("Checking…", then the answer), and the second file can reuse the first one's inode."""
    try:
        info = STATUS.stat()
        return info.st_ino, info.st_mtime_ns, STATUS.read_text()
    except OSError:
        return None


def _ask_helper(request: dict[str, object]) -> dict[str, str]:
    """Write a request as the container could, and wait for the helper's answer: a status file
    written after the request (its own time is only to the second, and a refusal can come back
    within the same second)."""
    before = _status_file()
    REQUEST.write_text(json.dumps(request))
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        now = _status_file()
        status = _helper()
        if now is not None and now != before and status.get("state") in ("done", "failed"):
            return status
        time.sleep(0.5)
    raise AssertionError(f"no answer from the helper; last: {_helper()}")


@pytest.fixture
def site(artifacts: Path) -> Iterator[Install]:
    install = Install(SITE)
    yield install
    (artifacts / "container.log").write_text(install.logs())
    (artifacts / "helper-status.json").write_text(json.dumps(_helper(), indent=2))
    journal = subprocess.run(
        ["journalctl", "-u", "googich-updater.service", "--no-pager"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    (artifacts / "helper.log").write_text(journal.stdout)
    if BLOCK in HOSTS.read_text():
        HOSTS.write_text(HOSTS.read_text().replace(BLOCK, ""))


def _install_helper_as_the_page_says(visitor: Visitor) -> None:
    """Copy the command from Help → Updates and run it, as a user pastes it into a shell."""
    command = visitor.open("/updates").locator("pre.command code").first.inner_text()
    assert "install-updater.sh" in command
    done = subprocess.run(  # noqa: S603
        ["bash", "-euo", "pipefail", "-c", command],  # noqa: S607
        cwd="/root",
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    deadline = time.monotonic() + 60
    while "One-click updates are on" not in visitor.open("/updates").content():
        assert time.monotonic() < deadline, "the page never saw the helper"
        time.sleep(2)


def _offer(visitor: Visitor, to: str) -> None:
    """Turn release candidates on for a candidate, or press Check now for a final release."""
    page = visitor.open("/updates")
    if "-rc" in to:
        page.locator("form[action='/updates/candidates'] input[name=on]").check()
        page.locator("form[action='/updates/candidates'] button[type=submit]").click()
        page.wait_for_load_state()
        assert f"Release candidate {to} is available." in page.content()
    else:
        page.locator("form[action='/updates/check'] button[type=submit]").click()
        page.wait_for_load_state()
        assert f"Version {to} is available." in page.content()


def _press_update_now(visitor: Visitor, to: str) -> str:
    """The page's own button (the banner has an Update now of its own)."""
    before = _helper().get("at", "")
    page = visitor.open("/updates")
    page.get_by_role("button", name=f"Update to {to}").click()
    page.wait_for_load_state()
    assert to in page.content()
    return before


def test_update_from_one_published_release_to_the_next(site: Install, visitor: Visitor) -> None:
    start, to = _version("E2E_FROM"), _version("E2E_TO")
    if start == to:
        pytest.skip("a fresh install only: nothing to update to")
    site.prepare(released_image(start), _example(start))
    site.up()
    assert (health() or {}).get("version") == start
    visitor.set_up(site.setup_token())
    visitor.choose_timezone("Pacific/Auckland")
    _install_helper_as_the_page_says(visitor)
    visitor.snap("1-helper-installed")

    _offer(visitor, to)
    page = visitor.open("/updates")
    assert page.locator(f"#whats-new h4:text-is('{to}')").count() == 1, "What's new is missing"
    visitor.snap("2-update-offered")

    # First with the image registry unreachable: the helper must put everything back.
    HOSTS.write_text(HOSTS.read_text() + BLOCK)
    before = _press_update_now(visitor, to)
    failed = _helper_reports("failed", before)
    assert f"Rolled back to {start}" in failed["message"], failed
    HOSTS.write_text(HOSTS.read_text().replace(BLOCK, ""))
    assert (health() or {}).get("version") == start
    visitor.snap("3-rolled-back")
    page = visitor.open("/updates")
    if page.locator("form[action='/updates/dismiss'] button").count():
        page.locator("form[action='/updates/dismiss'] button").first.click()

    before = _press_update_now(visitor, to)
    wait_for_version(to)
    done = _helper_reports("done", before)
    assert done["message"] == f"Updated from {start} to {to}.", done
    assert f"googich_takeaway:{to}" in (SITE / "compose.yaml").read_text()
    visitor.open("/")
    visitor.snap("4-updated")
    assert "Update complete." in visitor.page.content()

    visitor.page.context.clear_cookies()
    visitor.log_in()
    assert visitor.timezone() == "Pacific/Auckland"
    visitor.open("/updates/changelog")
    visitor.snap("5-changelog-after")
    assert visitor.complaints == []


def test_the_helper_refuses_what_it_should(site: Install, visitor: Visitor) -> None:
    to = _version("E2E_TO")
    if not site.installed:  # on its own: install the release and helper first
        site.prepare(released_image(to), _example(to))
        site.up()
        visitor.set_up(site.setup_token())
        _install_helper_as_the_page_says(visitor)
    running = _running()
    base = to.split("-rc")[0]
    refused: list[tuple[dict[str, object], str]] = [
        ({"version": f"{base}-rc99"}, "not switched on"),  # a candidate, not asked for as one
        ({"version": f"{base}-rc98", "candidates": True}, "is not a published release"),
        ({"version": "99.0.0"}, "newer than the newest published release"),
        ({"version": "0.6.0; reboot"}, "not a plain version number"),
    ]
    for request, reason in refused:
        status = _ask_helper(request)
        assert status["state"] == "failed", (request, status)
        assert reason in status["message"], (request, status)
    assert (health() or {}).get("version") == running  # nothing changed


def test_every_page_works_on_the_release(site: Install, visitor: Visitor) -> None:
    to = _version("E2E_TO")
    if not site.installed:
        site.prepare(released_image(to), _example(to))
        site.up()
        visitor.set_up(site.setup_token())
    else:  # whichever version the update test left: an earlier failure is reported there
        _running()
        visitor.log_in()
    for path in ("/", "/updates", "/updates/changelog", "/help/updates", "/about", "/settings"):
        visitor.open(path)
        visitor.snap("page" + path.replace("/", "-"))
    assert visitor.complaints == []
