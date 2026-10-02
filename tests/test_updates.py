from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from googich_takeaway import updates
from googich_takeaway.state import State

NOW = datetime(2026, 10, 2, tzinfo=UTC)


@pytest.mark.parametrize(
    ("latest", "current", "newer"),
    [
        ("0.2.0", "0.1.0", True),
        ("v0.1.1", "0.1.0", True),
        ("0.1.0", "0.1.0", False),
        ("0.1.0", "0.1.0.dev0", True),  # the release is newer than its development build
        ("0.0.9", "0.1.0.dev0", False),
        ("1.0.0-rc1", "0.9.0", False),  # not a plain release
    ],
)
def test_is_newer(latest: str, current: str, newer: bool) -> None:
    assert updates.is_newer(latest, current) is newer


class FakeGitHub:
    def __init__(self, body: dict[str, object] | None = None, status: int = 200) -> None:
        self.body = body or {
            "tag_name": "v9.9.9",
            "html_url": "https://github.com/aistuartai/Googich_takeaway/releases/tag/v9.9.9",
            "prerelease": False,
            "draft": False,
        }
        self.status = status
        self.calls = 0
        self.page = True
        """Answer the releases page with a redirect, as GitHub does; False: only the API."""

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            assert "authorization" not in request.headers
            assert request.headers["user-agent"].startswith("googich-takeaway/")
            if request.url.host == "github.com" and not self.page:
                return httpx.Response(404)
            if request.url.host == "github.com":  # the releases page, as GitHub answers it
                tag = str(self.body.get("tag_name", ""))
                if self.status != 200 or self.body.get("prerelease") or self.body.get("draft"):
                    return httpx.Response(404 if self.status == 200 else self.status)
                location = f"https://github.com/aistuartai/Googich_takeaway/releases/tag/{tag}"
                return httpx.Response(302, headers={"location": location})
            return httpx.Response(self.status, json=self.body)

        return httpx.MockTransport(handle)


@pytest.fixture
def state(tmp_path: Path) -> State:
    return State(tmp_path / "state.db")


def test_new_release_is_cached_and_checked_once_a_day(state: State) -> None:
    github = FakeGitHub()
    info = updates.check_if_due(state, lambda: NOW, github.transport())
    assert info is not None
    assert info.latest == "9.9.9"
    assert info.newer
    updates.check_if_due(state, lambda: NOW + timedelta(hours=5), github.transport())
    assert github.calls == 1
    updates.check_if_due(state, lambda: NOW + timedelta(hours=25), github.transport())
    assert github.calls == 2


def test_prerelease_and_drafts_are_ignored(state: State) -> None:
    github = FakeGitHub({"tag_name": "v9.9.9", "prerelease": True})
    assert updates.check_if_due(state, lambda: NOW, github.transport()) is None


def test_no_release_yet_and_errors_are_quiet_and_backed_off(state: State) -> None:
    missing = FakeGitHub(status=404)
    assert updates.check_if_due(state, lambda: NOW, missing.transport()) is None
    broken = FakeGitHub(status=500)
    later = NOW + timedelta(minutes=30)
    assert updates.check_if_due(state, lambda: later, broken.transport()) is None
    assert broken.calls == 0  # tried less than an hour ago: wait


def test_disabled_makes_no_request(state: State) -> None:
    updates.set_enabled(state, False, NOW)
    github = FakeGitHub()
    assert updates.check_if_due(state, lambda: NOW, github.transport()) is None
    assert github.calls == 0


def test_links_only_ever_point_at_this_project(state: State) -> None:
    github = FakeGitHub(
        {"tag_name": "v9.9.9", "html_url": "https://evil.example/download", "prerelease": False}
    )
    github.page = False  # the API's answer is the one with a link in it
    info = updates.check_if_due(state, lambda: NOW, github.transport())
    assert info is not None
    assert info.url == "https://github.com/aistuartai/Googich_takeaway/releases"


def test_helper_status_and_request(tmp_path: Path) -> None:
    assert updates.helper_status(tmp_path) is None
    with pytest.raises(ValueError, match="not installed"):
        updates.request_update(tmp_path, "0.1.1")
    folder = tmp_path / "updater"
    folder.mkdir()
    (folder / "status.json").write_text(
        '{"helper": "1", "state": "done", "message": "Updated from 0.1.0 to 0.1.1.", '
        '"version": "0.1.1", "at": "2026-10-02T00:14:00+00:00"}'
    )
    status = updates.helper_status(tmp_path)
    assert status is not None
    assert (status.state, status.version) == ("done", "0.1.1")
    updates.request_update(tmp_path, "0.1.2")
    assert (folder / "request.json").read_text() == '{"version": "0.1.2"}'
    with pytest.raises(ValueError, match="release version"):
        updates.request_update(tmp_path, "0.1.2; rm -rf /")


def test_damaged_status_file_means_no_helper(tmp_path: Path) -> None:
    (tmp_path / "updater").mkdir()
    (tmp_path / "updater" / "status.json").write_text("not json")
    assert updates.helper_status(tmp_path) is None


def test_check_now_asks_straight_away_but_at_most_once_a_minute(state: State) -> None:
    github = FakeGitHub()
    updates.check_if_due(state, lambda: NOW, github.transport())
    later = NOW + timedelta(minutes=5)
    assert updates.check_now(state, lambda: later, github.transport()) is updates.CheckResult.NEWER
    assert github.calls == 2  # the daily check's result did not stop it
    soon = later + timedelta(seconds=30)
    assert updates.check_now(state, lambda: soon, github.transport()) is updates.CheckResult.WAIT
    assert github.calls == 2


def test_check_now_says_when_up_to_date_or_unreachable(state: State) -> None:
    from googich_takeaway import __version__

    current = FakeGitHub({"tag_name": f"v{__version__}", "prerelease": False, "draft": False})
    result = updates.check_now(state, lambda: NOW, current.transport())
    assert result is (
        updates.CheckResult.CURRENT
        if updates.parse_version(__version__)
        else updates.CheckResult.NEWER
    )
    broken = FakeGitHub(status=500)
    later = NOW + timedelta(minutes=2)
    assert updates.check_now(state, lambda: later, broken.transport()) is updates.CheckResult.FAILED
    assert updates.cached(state) is not None  # the last good answer is kept


def test_the_check_uses_the_releases_page_not_the_rate_limited_api(state: State) -> None:
    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        location = "https://github.com/aistuartai/Googich_takeaway/releases/tag/v9.9.9"
        return httpx.Response(302, headers={"location": location})

    info = updates.check_if_due(state, lambda: NOW, httpx.MockTransport(handle))
    assert info is not None
    assert info.latest == "9.9.9"
    assert info.url.endswith("/releases/tag/v9.9.9")
    assert asked == ["https://github.com/aistuartai/Googich_takeaway/releases/latest"]


def test_a_redirect_elsewhere_falls_back_to_the_api(state: State) -> None:
    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.host)
        if request.url.host == "github.com":
            return httpx.Response(302, headers={"location": "https://evil.example/v9.9.9"})
        return httpx.Response(200, json={"tag_name": "v1.2.3", "draft": False, "prerelease": False})

    info = updates.check_if_due(state, lambda: NOW, httpx.MockTransport(handle))
    assert info is not None
    assert info.latest == "1.2.3"
    assert asked == ["github.com", "api.github.com"]
