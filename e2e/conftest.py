"""Fixtures for the end-to-end tests: the app under real Docker, and a real browser.

They run only when asked, as CI's deploy job and the release rehearsal do:

    E2E_IMAGE=googich:test uv run --group e2e pytest e2e

E2E_IMAGE is the image to test (the candidate); a rehearsal (test_rehearsal.py) sets
E2E_REHEARSAL instead, as it tests published releases. Screenshots, page errors and container
logs go to e2e-artifacts/.
"""

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, ConsoleMessage, Page, sync_playwright

from e2e.stack import ROOT, URL, Install

ARTIFACTS = Path(os.environ.get("E2E_ARTIFACTS", ROOT / "e2e-artifacts"))
PASSWORD = "e2e-password-" + "x" * 8


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get("E2E_IMAGE") or os.environ.get("E2E_REHEARSAL") == "1":
        return
    skip = pytest.mark.skip(reason="set E2E_IMAGE to the image to test")
    for item in items:
        item.add_marker(skip)


@pytest.fixture(scope="session")
def candidate() -> str:
    return os.environ["E2E_IMAGE"]


@pytest.fixture
def artifacts(request: pytest.FixtureRequest) -> Path:
    folder = ARTIFACTS / re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@pytest.fixture
def install(tmp_path: Path, artifacts: Path) -> Iterator[Install]:
    site = Install(tmp_path / "googich")
    yield site
    (artifacts / "container.log").write_text(site.logs())
    site.remove()


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        launched = playwright.chromium.launch()
        yield launched
        launched.close()


class Visitor:
    """One person at the browser: goes to pages, keeps screenshots, notes anything the browser
    complained about (a script error, or something the content security policy refused)."""

    def __init__(self, page: Page, artifacts: Path) -> None:
        self.page = page
        self.artifacts = artifacts
        self.complaints: list[str] = []
        page.on("console", self._console)
        page.on("pageerror", lambda error: self.complaints.append(f"page error: {error}"))

    def _console(self, message: ConsoleMessage) -> None:
        if message.type == "error":
            self.complaints.append(f"{self.page.url}: {message.text}")

    def open(self, path: str) -> Page:
        response = self.page.goto(URL + path)
        assert response is not None
        assert response.ok, f"{path} answered {response.status}"
        return self.page

    def snap(self, name: str) -> None:
        self.page.screenshot(path=self.artifacts / f"{name}.png", full_page=True)

    def set_up(self, token: str) -> None:
        self.open("/setup")
        self.page.fill("input[name=token]", token)
        self.page.fill("input[name=password]", PASSWORD)
        self.page.fill("input[name=confirm]", PASSWORD)
        self.page.click("button[type=submit]")
        self.page.wait_for_load_state()
        assert not self.page.url.endswith(("/setup", "/login")), "setup did not finish"

    def choose_timezone(self, zone: str) -> None:
        """Change one setting, to check later that it survived."""
        form = self.open("/settings").locator("form[action='/settings/timezone']")
        form.locator("select[name=timezone]").select_option(zone)
        form.locator("button[type=submit]").click()
        self.page.wait_for_load_state()

    def timezone(self) -> str:
        select = self.open("/settings").locator("form[action='/settings/timezone'] select")
        return select.input_value()

    def log_in(self) -> None:
        self.open("/login")
        self.page.fill("input[name=password]", PASSWORD)
        self.page.click("button[type=submit]")
        self.page.wait_for_load_state()
        assert "/login" not in self.page.url, "the password was not accepted"


@pytest.fixture
def visitor(browser: Browser, artifacts: Path) -> Iterator[Visitor]:
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    person = Visitor(context.new_page(), artifacts)
    yield person
    (artifacts / "browser.log").write_text("\n".join(person.complaints))
    context.close()
