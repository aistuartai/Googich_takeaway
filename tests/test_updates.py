import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from googich_takeaway import updates
from googich_takeaway.state import State
from tests.fake_github import FakeGitHub

NOW = datetime(2026, 10, 2, tzinfo=UTC)


@pytest.mark.parametrize(
    ("latest", "current", "newer"),
    [
        ("0.2.0", "0.1.0", True),
        ("v0.1.1", "0.1.0", True),
        ("0.1.0", "0.1.0", False),
        ("0.1.0", "0.1.0.dev0", True),  # the release is newer than its development build
        ("0.0.9", "0.1.0.dev0", False),
        ("1.0.0-rc1", "0.9.0", True),
        ("1.0.0-rc2", "1.0.0-rc1", True),
        ("1.0.0-rc10", "1.0.0-rc9", True),  # numbers, not text
        ("1.0.0", "1.0.0-rc2", True),  # the final release is newer than its candidates
        ("1.0.0-rc1", "1.0.0", False),
        ("1.0.0-rc1", "1.0.0.dev0", True),  # a development build is older than candidates
        ("1.0.0rc1", "0.9.0", True),  # as Python writes it
        ("1.0.0-beta1", "0.9.0", False),  # only release candidates
    ],
)
def test_is_newer(latest: str, current: str, newer: bool) -> None:
    assert updates.is_newer(latest, current) is newer


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


def test_versions_are_shown_as_tags_and_images_write_them() -> None:
    assert updates.display("v1.2.3") == "1.2.3"
    assert updates.display("1.2.3rc4") == "1.2.3-rc4"
    assert updates.is_candidate("1.2.3-rc4")
    assert not updates.is_candidate("1.2.3")


def _candidates_on(state: State) -> None:
    updates.set_candidates(state, True, NOW)


def test_release_candidates_are_offered_only_when_switched_on(state: State) -> None:
    github = FakeGitHub()
    github.feed = ["v10.0.0-rc1", "v9.9.9", "v0.0.1"]
    assert updates.check_now(state, lambda: NOW, github.transport()) is updates.CheckResult.NEWER
    assert updates.cached(state) is not None
    assert updates.cached(state).latest == "9.9.9"  # type: ignore[union-attr]
    assert github.feed_calls == 0  # off: the feed is never read
    _candidates_on(state)
    assert updates.cached(state) is None  # the last reading answered the old choice
    later = NOW + timedelta(minutes=2)
    assert updates.check_now(state, lambda: later, github.transport()) is updates.CheckResult.NEWER
    info = updates.cached(state)
    assert info is not None
    assert (info.latest, info.candidate) == ("10.0.0-rc1", True)
    assert info.url == "https://github.com/aistuartai/Googich_takeaway/releases/tag/v10.0.0-rc1"
    assert github.calls == 1  # the feed answered: no other lookup
    updates.set_candidates(state, False, later)
    assert updates.cached(state) is None


def test_with_candidates_on_a_final_release_still_wins(state: State) -> None:
    _candidates_on(state)
    github = FakeGitHub()
    github.feed = ["v10.0.0", "v10.0.0-rc2", "v10.0.0-rc1"]
    updates.check_now(state, lambda: NOW, github.transport())
    assert updates.cached(state).latest == "10.0.0"  # type: ignore[union-attr]


def test_feed_trouble_falls_back_to_the_latest_release(state: State) -> None:
    _candidates_on(state)
    for feed in (None, [], ["v1.0.0-beta1", "nonsense"]):
        github = FakeGitHub()
        github.feed = feed
        state.set_setting("updates.manual", None, NOW)
        assert (
            updates.check_now(state, lambda: NOW, github.transport()) is updates.CheckResult.NEWER
        )
        assert updates.cached(state).latest == "9.9.9"  # type: ignore[union-attr]
        assert github.calls == 1


def test_feed_links_from_elsewhere_are_ignored(state: State) -> None:
    _candidates_on(state)
    github = FakeGitHub()
    github.feed = ["v9.9.9"]
    original = github.transport()

    def handle(request: httpx.Request) -> httpx.Response:
        response = original.handle_request(request)
        if request.url.path.endswith("/releases.atom"):
            text = response.read().decode() + (
                '<link href="https://github.com/someone/else/releases/tag/v99.0.0-rc1"/>'
            )
            return httpx.Response(200, text=text)
        return response

    updates.check_now(state, lambda: NOW, httpx.MockTransport(handle))
    assert updates.cached(state).latest == "9.9.9"  # type: ignore[union-attr]


def test_a_candidate_request_says_it_is_one(tmp_path: Path) -> None:
    folder = tmp_path / "updater"
    folder.mkdir()
    (folder / "status.json").write_text('{"helper": "4", "state": "idle"}')
    updates.request_update(tmp_path, "1.0.0rc2")
    assert json.loads((folder / "request.json").read_text()) == {
        "version": "1.0.0-rc2",
        "candidates": True,
    }
    status = updates.helper_status(tmp_path)
    assert status is not None
    assert status.installs_candidates
