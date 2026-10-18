import tomllib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from googich_takeaway import __version__, changelog, updates
from googich_takeaway.state import State
from tests.fake_github import CHANGELOG, FakeGitHub

NOW = datetime(2026, 10, 17, tzinfo=UTC)
ROOT = Path(__file__).parents[1]


@pytest.fixture
def state(tmp_path: Path) -> State:
    return State(tmp_path / "state.db")


def _found(state: State, tag: str = "v9.9.9") -> FakeGitHub:
    """The update check, as it runs, finding ``tag`` the latest release."""
    github = FakeGitHub({**FakeGitHub().body, "tag_name": tag})
    state.set_setting("updates.manual", None, NOW)
    updates.check_now(state, lambda: NOW, github.transport())
    return github


def test_the_app_carries_its_own_change_log_with_the_version_running() -> None:
    releases = changelog.bundled()
    assert [r.version for r in releases if r.running] == [__version__]
    assert releases[-1].version == "0.1.0"
    dates = [r.released for r in releases if not r.candidate]  # a candidate is not dated
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
    assert "<strong>Time travel</strong>" in releases[0].notes()


def test_kind_headings_sit_at_the_level_asked_for() -> None:
    release = changelog.parse(CHANGELOG + "\n#### Detail\n")[1]
    assert '<h3 class="change-kind change-added">Added</h3>' in release.notes(3)
    assert '<h5 class="change-kind change-added">Added</h5>' in release.notes(5)
    assert "<h6>Detail</h6>" in release.notes(5)
    assert "<h6>Detail</h6>" in release.notes(6)  # never below h6


def test_untrusted_change_log_cannot_carry_markup_or_script() -> None:
    text = """## [9.9.9] - 2026-10-20

- <script>alert(1)</script> and <b onclick="x()">bold</b>
- [bad](javascript:alert(1)) [plain](http://example.com) [good](https://example.com) [here](#v1)
- ![tracker](https://example.com/pixel.png) and ![two](https://example.com/two.png)
"""
    html = str(changelog.parse(text)[0].notes())
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


def test_newer_release_notes_are_fetched_once_and_shown(state: State) -> None:
    github = _found(state)
    assert changelog.fetch_newer(state, NOW, github.transport())
    assert changelog.fetch_newer(state, NOW, github.transport())
    assert github.changelog_calls == 1  # kept: not asked again for the same release
    shown = [r.version for r in changelog.releases(state)]
    assert shown == ["9.9.9", *(r.version for r in changelog.bundled())]


def test_fetched_text_is_used_only_for_newer_releases(state: State) -> None:
    github = _found(state)
    github.changelog = CHANGELOG.replace(
        "## [0.0.1]", f"## [{__version__}] - 2026-10-17\n\n- Rewritten history.\n\n## [0.0.1]"
    )
    assert changelog.fetch_newer(state, NOW, github.transport())
    running = next(r for r in changelog.releases(state) if r.running)
    assert running == next(r for r in changelog.bundled() if r.running)  # this version's words
    assert all(r.version != "0.0.1" for r in changelog.releases(state))


def test_notes_show_with_the_daily_check_off(state: State) -> None:
    github = _found(state)
    changelog.fetch_newer(state, NOW, github.transport())
    updates.set_enabled(state, False, NOW)
    assert changelog.releases(state)[0].version == "9.9.9"


def test_nothing_fetched_when_nothing_is_newer(state: State) -> None:
    github = _found(state, "v0.0.1")
    assert not changelog.fetch_newer(state, NOW, github.transport())
    assert github.changelog_calls == 0
    assert changelog.releases(state) == changelog.bundled()


def test_a_failed_fetch_waits_before_asking_again(state: State) -> None:
    github = _found(state)
    github.changelog = None
    assert not changelog.fetch_newer(state, NOW, github.transport())
    github.changelog = CHANGELOG
    assert not changelog.fetch_newer(state, NOW + timedelta(minutes=5), github.transport())
    assert github.changelog_calls == 1
    assert changelog.fetch_newer(state, NOW + timedelta(minutes=5), github.transport(), None)
    assert github.changelog_calls == 2  # Check now asks straight away


def test_wrong_or_missing_change_log_is_not_kept(state: State) -> None:
    github = _found(state)
    github.changelog = None
    assert not changelog.fetch(state, "9.9.9", NOW, github.transport())
    github.changelog = CHANGELOG.replace("9.9.9", "9.9.8")  # does not list the release
    assert not changelog.fetch(state, "9.9.9", NOW, github.transport())
    github.changelog = CHANGELOG + "x" * changelog.MAX_BYTES
    assert not changelog.fetch(state, "9.9.9", NOW, github.transport())
    assert not changelog.fetch(state, "../main", NOW, github.transport())  # never a path
    assert github.changelog_calls == 3
    assert changelog.releases(state) == changelog.bundled()


def test_an_endless_answer_is_cut_off_at_the_limit(state: State) -> None:
    sent = 0

    class Endless(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal sent
            while True:
                sent += 65536
                yield b"x" * 65536

    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Endless()))
    assert not changelog.fetch(state, "9.9.9", NOW, transport)
    assert sent <= changelog.MAX_BYTES + 65536


def test_fetch_never_raises(state: State) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network")

    assert not changelog.fetch(state, "9.9.9", NOW, httpx.MockTransport(broken))


def test_notes_fetched_for_an_older_reading_are_not_used(state: State) -> None:
    github = _found(state)
    assert changelog.fetch_newer(state, NOW, github.transport())
    _found(state, "v9.9.10")  # a newer release since: its notes are not fetched yet
    assert changelog.releases(state) == changelog.bundled()
    assert not changelog.has_notes_for(state, "9.9.10")


CANDIDATE = CHANGELOG.replace(
    "## [Unreleased]\n", "## [Unreleased]\n\n### Fixed\n\n- **Rehearsed** before release.\n"
)


def test_a_release_candidate_brings_what_its_change_log_has_not_released_yet() -> None:
    plain = changelog.parse(CANDIDATE)
    assert [r.version for r in plain] == ["9.9.9", "0.0.1"]
    releases = changelog.parse(CANDIDATE, unreleased_as="10.0.0-rc1")
    assert [r.version for r in releases] == ["10.0.0-rc1", "9.9.9", "0.0.1"]
    assert releases[0].candidate
    assert not releases[1].candidate
    assert releases[0].released is None
    assert releases[0].kinds == ("Fixed",)
    assert "<strong>Rehearsed</strong>" in releases[0].notes()


def test_a_candidates_notes_are_fetched_from_its_tag(state: State) -> None:
    updates.set_candidates(state, True, NOW)
    github = _found(state)
    github.feed = ["v10.0.0-rc1", "v9.9.9"]
    github.changelog = CANDIDATE
    state.set_setting("updates.manual", None, NOW)
    updates.check_now(state, lambda: NOW, github.transport())
    assert changelog.fetch_newer(state, NOW, github.transport())
    shown = changelog.releases(state)
    assert [r.version for r in shown[:2]] == ["10.0.0-rc1", "9.9.9"]
    assert changelog.remote_url("10.0.0-rc1").endswith("/v10.0.0-rc1/CHANGELOG.md")
