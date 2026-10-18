"""Release files that must agree with each other, checked on every push."""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _version() -> str:
    found = re.search(r'^version = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(), re.M)
    assert found
    return found[1]


def _candidate() -> bool:
    return re.fullmatch(r"\d+\.\d+\.\d+rc\d+", _version()) is not None


def _published() -> str:
    """The final release users are pointed at: this one, or for a release candidate, the last."""
    compose = (ROOT / "docker" / "compose.yaml").read_text()
    tags = re.findall(r"image:\s*ghcr\.io/aistuartai/googich_takeaway:(\S+)", compose)
    assert len(tags) == 1
    return str(tags[0])


def test_example_compose_file_pins_the_current_version() -> None:
    if not _candidate():
        assert _published() == _version(), "bump the image tag in docker/compose.yaml"
        return
    # A release candidate leaves users on the last final release, older than the candidate.
    from googich_takeaway.updates import is_newer

    assert re.fullmatch(r"\d+\.\d+\.\d+", _published())
    assert is_newer(_version(), _published())


def test_example_compose_file_runs_as_the_image_user() -> None:
    compose = (ROOT / "docker" / "compose.yaml").read_text()
    assert 'user: "10001:10001"' in compose
    assert "--uid 10001" in (ROOT / "docker" / "Dockerfile").read_text()


def test_changelog_has_a_section_for_released_versions() -> None:
    text = (ROOT / "CHANGELOG.md").read_text()
    assert f"## [{_published()}]" in text
    if _candidate():  # what it brings is still under Unreleased, where its notes are read
        unreleased = text.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
        assert unreleased.strip(), "a release candidate needs notes under Unreleased"


def test_every_versioned_link_names_the_current_release() -> None:
    """Install and update commands point at this release's files."""
    version = _published()
    for path in ("README.md", "src/googich_takeaway/docs/updates.md"):
        text = (ROOT / path).read_text()
        found = set(re.findall(r"Googich_takeaway/(?:releases/download/)?v(\d+\.\d+\.\d+)/", text))
        assert found == {version}, f"bump the version in links in {path}"


def test_helper_version_matches_the_app() -> None:
    from googich_takeaway.updates import HELPER_VERSION

    script = (ROOT / "deploy" / "updater" / "googich-updater.sh").read_text()
    assert f'HELPER_VERSION="{HELPER_VERSION}"' in script
