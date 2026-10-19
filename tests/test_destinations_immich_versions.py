"""Which Immich versions this app supports, and what Test connection says about others."""

import pytest

from googich_takeaway.destinations.immich import IMMICH_MINIMUM, IMMICH_TESTED, support_note


def test_the_supported_range_starts_at_2_7_5() -> None:
    assert IMMICH_MINIMUM == (2, 7, 5)  # kept until a needed change makes it impossible
    assert IMMICH_TESTED >= IMMICH_MINIMUM


@pytest.mark.parametrize("version", ["2.7.5", "3.0.0", "3.3.3", "3.3.9"])
def test_supported_versions_need_no_note(version: str) -> None:
    assert support_note(version) == (True, "")


def test_an_older_immich_is_not_supported() -> None:
    supported, note = support_note("2.7.4")
    assert not supported
    assert "older than 2.7.5, the oldest this app supports" in note


def test_a_newer_immich_is_supported_with_a_note() -> None:
    supported, note = support_note("3.4.0")
    assert supported
    assert "newer than this release was checked against (3.3.3)" in note
