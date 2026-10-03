"""Pausing while archives are read: the next run carries on, instead of reading them again."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pytest

from googich_takeaway.read_ahead import StateScanCache
from googich_takeaway.state import State
from googich_takeaway.takeout import scan as scan_module
from googich_takeaway.takeout.dates import DateResolver
from googich_takeaway.takeout.scan import scan_export
from googich_takeaway.takeout.scan_cache import CachedEntry, decode, encode
from tests.fixtures.takeout import quirks_export

NOW = datetime(2026, 10, 1, tzinfo=UTC)
RESOLVER = DateResolver(default_timezone=ZoneInfo("Australia/Melbourne"))


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "a"
    folder.mkdir()
    return folder


class Stop(BaseException):
    """Stands in for Pause: a BaseException, like RunStopped."""


def _summary(scan: scan_module.ExportScan) -> list[tuple[object, ...]]:
    return [
        (i.path, i.size, i.sha1, i.sidecar, i.match_rule, i.date, i.gps, i.favorited)
        for i in scan.items
    ]


@pytest.mark.parametrize("kind", ["zip", "tgz"])
def test_a_paused_read_carries_on_where_it_stopped(
    tmp_path: Path, kind: Literal["zip", "tgz"], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan_module, "SAVE_EVERY", 2)  # save often, so a pause keeps a lot
    archives = quirks_export().write(_folder(tmp_path), kind)
    everything = [0]
    full = scan_export(
        archives, RESOLVER, NOW, progress=lambda n: everything.__setitem__(0, everything[0] + n)
    )
    total = everything[0]

    state = State(tmp_path / "state.db")
    cache = StateScanCache(state, lambda: NOW)
    read = [0]

    def stop_halfway(amount: int) -> None:
        read[0] += amount
        if read[0] > total // 3:
            raise Stop

    with pytest.raises(Stop):
        scan_export(archives, RESOLVER, NOW, progress=stop_halfway, cache=cache)

    again, skipped = [0], [0]
    resumed = scan_export(
        archives,
        RESOLVER,
        NOW,
        progress=lambda n: again.__setitem__(0, again[0] + n),
        cache=cache,
        resumed=lambda n: skipped.__setitem__(0, skipped[0] + n),
    )
    assert _summary(resumed) == _summary(full)  # the same result as one uninterrupted read
    assert skipped[0] > 0  # what the first run read was taken from the cache
    assert again[0] < everything[0] * 3 // 4  # and only the rest was read

    # A third run reads nothing at all: every part was read to the end.
    third = [0]
    scan_export(
        archives, RESOLVER, NOW, progress=lambda n: third.__setitem__(0, third[0] + n), cache=cache
    )
    assert third[0] == 0


def test_a_changed_part_is_read_afresh(tmp_path: Path) -> None:
    archives = quirks_export().write(_folder(tmp_path))
    state = State(tmp_path / "state.db")
    cache = StateScanCache(state, lambda: NOW)
    scan_export(archives, RESOLVER, NOW, cache=cache)
    archives[0].write_bytes(archives[0].read_bytes() + b"\0" * 10)  # replaced: another size
    read = [0]
    scan_export(archives, RESOLVER, NOW, progress=lambda n: read.__setitem__(0, read[0] + n),
                cache=cache)  # fmt: skip
    assert read[0] > 0


def test_entries_survive_encoding() -> None:
    from datetime import timedelta

    from googich_takeaway.takeout.metadata import MediaMetadata, SidecarData

    media = CachedEntry(
        "a.jpg", "media", 10, "ab" * 20,
        media=MediaMetadata(datetime(2019, 7, 4, 10, 15), timedelta(hours=10), (1.5, 2.5), None),
    )  # fmt: skip
    side = CachedEntry(
        "a.jpg.json", "sidecar",
        sidecar=SidecarData("a.jpg", datetime(2019, 7, 4, tzinfo=UTC), None, True, "note"),
    )  # fmt: skip
    for entry in (media, side, CachedEntry("x.html", "other")):
        assert decode(entry.path, encode(entry)) == entry
    assert decode("bad", "not json") is None
