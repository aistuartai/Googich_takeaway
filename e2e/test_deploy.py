"""Level 1, on every pull request: the image as users run it, through the example compose file.

A fresh install, set up from the token in the logs and walked through in a browser; and an
upgrade from the release users run today to this image, on the same data folder.
"""

import json
import os
import subprocess

import pytest
from playwright.sync_api import Browser

from e2e.conftest import Visitor
from e2e.stack import (
    Install,
    candidate_version,
    example_of,
    health,
    published_version,
    released_image,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("E2E_REHEARSAL") == "1", reason="CI's tests of the image just built"
)

PAGES = {
    "dashboard": "/",
    "updates": "/updates",
    "changelog": "/updates/changelog",
    "guide-updates": "/help/updates",
    "about": "/about",
    "settings": "/settings",
    "sources": "/sources",
    "destinations": "/destinations",
    "logs": "/logs",
}


def test_example_compose_file_runs_the_image_locked_down(install: Install, candidate: str) -> None:
    install.prepare(candidate)
    install.up()
    assert install.container_user() == "10001"
    container = install.compose_cmd("ps", "-q", "googich").stdout.strip()
    inspected = subprocess.run(  # noqa: S603
        ["docker", "inspect", container],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    details = json.loads(inspected.stdout)[0]["HostConfig"]
    assert details["ReadonlyRootfs"] is True
    assert details["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in details["SecurityOpt"]


def test_fresh_install_set_up_and_every_page_works(
    install: Install, candidate: str, visitor: Visitor, browser: Browser
) -> None:
    install.prepare(candidate)
    install.up()
    visitor.open("/")
    assert "/setup" in visitor.page.url or "/login" in visitor.page.url
    visitor.set_up(install.setup_token())
    for name, path in PAGES.items():
        visitor.open(path)
        visitor.snap(name)
    page = visitor.open("/updates")
    assert page.locator("text=Offer release candidates").count() == 1
    assert page.locator(".candidate-warning").is_visible()
    assert visitor.complaints == []

    phone = browser.new_context(
        viewport={"width": 390, "height": 844}, storage_state=visitor.page.context.storage_state()
    )
    small = Visitor(phone.new_page(), visitor.artifacts)
    for name, path in PAGES.items():
        small.open(path)
        small.snap(f"phone-{name}")
        assert small.sticking_out() == [], f"{name} scrolls sideways on a phone"
    phone.close()
    assert small.complaints == []


def test_upgrade_from_the_published_release_keeps_everything(
    install: Install, candidate: str, visitor: Visitor
) -> None:
    published = published_version()
    theirs = example_of(published)  # the compose file users of that release have
    install.prepare(released_image(published), theirs)
    install.up()
    assert (health() or {}).get("version") == published
    visitor.set_up(install.setup_token())
    visitor.choose_timezone("Pacific/Auckland")
    visitor.snap("before-upgrade")

    install.use(candidate, theirs)  # only the version line changed, by hand or by the helper
    install.up()
    assert (health() or {}).get("version") == candidate_version()  # the new one answers
    visitor.page.context.clear_cookies()
    visitor.log_in()  # the password set on the old version
    assert visitor.timezone() == "Pacific/Auckland"
    for name, path in PAGES.items():
        visitor.open(path)
        visitor.snap(f"after-upgrade-{name}")
    assert "First-run setup" not in install.logs()  # the new container: no second setup
    assert visitor.complaints == []
