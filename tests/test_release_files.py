"""Release files that must agree with each other, checked on every push."""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _version() -> str:
    found = re.search(r'^version = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(), re.M)
    assert found
    return found[1]


def test_example_compose_file_pins_the_current_version() -> None:
    compose = (ROOT / "docker" / "compose.yaml").read_text()
    tags = re.findall(r"image:\s*ghcr\.io/aistuartai/googich_takeaway:(\S+)", compose)
    assert tags == [_version()], "bump the image tag in docker/compose.yaml with each release"


def test_example_compose_file_runs_as_the_image_user() -> None:
    compose = (ROOT / "docker" / "compose.yaml").read_text()
    assert 'user: "10001:10001"' in compose
    assert "--uid 10001" in (ROOT / "docker" / "Dockerfile").read_text()


def test_changelog_has_a_section_for_released_versions() -> None:
    version = _version()
    if version.count(".") == 2 and "dev" not in version:
        assert f"## [{version}]" in (ROOT / "CHANGELOG.md").read_text()
