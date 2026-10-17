import json
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from googich_takeaway import __version__, changelog, updates
from googich_takeaway.state import State
from tests.test_updates import CHANGELOG, FakeGitHub

NOW = datetime(2026, 10, 17, tzinfo=UTC)
ROOT = Path(__file__).parents[1]


@pytest.fixture
def state(tmp_path: Path) -> State:
    return State(tmp_path / "state.db")


def _known(state: State, latest: str) -> None:
    state.set_setting(
        "updates.latest",
        json.dumps(
            {"latest": latest, "url": "https://github.com/x", "checked_at": NOW.isoformat()}
        ),
        NOW,
    )


def test_the_app_carries_its_own_change_log_with_the_version_running() -> None:
    releases = changelog.bundled()
    assert releases[0].version == __version__ or updates.parse_version(__version__) is None
    assert [r.version for r in releases if r.running] == [__version__]
    first = releases[-1]
    assert first.version == "0.1.0"
    dates = [r.released for r in releases]
    assert all(d is not None for d in dates)
    assert dates == sorted(dates, reverse=True)  # type: ignore[type-var]


def test_the_change_log_ships_in_the_package_and_the_image() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    included = project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert included["CHANGELOG.md"] == "googich_takeaway/CHANGELOG.md"
    assert "CHANGELOG.md" in (ROOT / "docker" / "Dockerfile").read_text()
    assert "!CHANGELOG.md" in (ROOT / ".dockerignore").read_text().split()


def test_parse_reads_versions_dates_and_kinds() -> None:
    releases = changelog.parse(CHANGELOG)
    assert [r.version for r in releases] == ["9.9.9", "0.0.1"]  # Unreleased left out
    assert releases[0].released == date(2026, 10, 20)
    assert releases[0].kinds == ("Added", "Fixed")
    assert releases[0].anchor == "v9.9.9"
    assert releases[0].newer
    assert '<h3 class="change-kind change-added">Added</h3>' in releases[0].html
    assert "<strong>Time travel</strong>" in releases[0].html


def test_untrusted_change_log_cannot_carry_markup_or_script() -> None:
    text = """## [9.9.9] - 2026-10-20

- <script>alert(1)</script> and <b onclick="x()">bold</b>
- [bad](javascript:alert(1)) [plain](http://example.com) [good](https://example.com) [here](#v1)
- ![tracker](https://example.com/pixel.png)
"""
    html = str(changelog.parse(text)[0].html)
    assert "<script" not in html
    assert "&lt;script&gt;" in html
    assert "<b " not in html
    assert "javascript:" not in html
    assert 'href="http://example.com"' not in html
    assert (
        '<a href="https://example.com" rel="noopener noreferrer" target="_blank">good</a>' in html
    )
    assert '<a href="#v1">here</a>' in html
    assert "<img" not in html


def test_newer_release_change_log_is_fetched_once_and_shown(state: State) -> None:
    github = FakeGitHub()
    updates.check_if_due(state, lambda: NOW, github.transport())
    assert github.changelog_calls == 1
    shown = [r.version for r in changelog.releases(state)]
    assert shown == ["9.9.9", *(r.version for r in changelog.bundled()), "0.0.1"]
    state.set_setting("updates.attempted", None, NOW)
    updates.check_now(state, lambda: NOW, github.transport())
    assert github.changelog_calls == 1  # kept: not asked again for the same release
    updates.set_enabled(state, False, NOW)
    assert changelog.releases(state) == changelog.bundled()


def test_change_log_not_fetched_when_nothing_is_newer(state: State) -> None:
    github = FakeGitHub({**FakeGitHub().body, "tag_name": "v0.0.1"})
    updates.check_if_due(state, lambda: NOW, github.transport())
    assert github.changelog_calls == 0
    assert changelog.releases(state) == changelog.bundled()


def test_wrong_or_missing_change_log_is_not_kept(state: State) -> None:
    github = FakeGitHub()
    github.changelog = None
    assert not changelog.fetch(state, "9.9.9", NOW, github.transport())
    github.changelog = CHANGELOG.replace("9.9.9", "9.9.8")  # does not list the release
    assert not changelog.fetch(state, "9.9.9", NOW, github.transport())
    github.changelog = CHANGELOG + "x" * changelog.MAX_BYTES
    assert not changelog.fetch(state, "9.9.9", NOW, github.transport())
    assert not changelog.fetch(state, "../main", NOW, github.transport())  # never a path
    assert github.changelog_calls == 3
    _known(state, "9.9.9")
    assert changelog.releases(state) == changelog.bundled()


def test_fetch_never_raises(state: State) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network")

    assert not changelog.fetch(state, "9.9.9", NOW, httpx.MockTransport(broken))


def test_fetched_change_log_for_an_older_reading_is_not_used(state: State) -> None:
    github = FakeGitHub()
    assert changelog.fetch(state, "9.9.9", NOW, github.transport())
    _known(state, "9.9.10")  # a newer release since: its notes are not fetched yet
    assert changelog.releases(state) == changelog.bundled()
