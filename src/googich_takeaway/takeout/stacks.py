"""Files that belong together, to be stacked in Immich so they show as one.

Three kinds, each found from file names in the same folder:

- **Edited copies** with their original: ``IMG_1.jpg`` and ``IMG_1-edited.jpg`` (the suffix in
  the export's language). The edited copy is shown.
- **RAW and JPEG** shot together: ``DSC_1.NEF`` and ``DSC_1.JPG`` (or HEIC). The JPEG is shown.
- **Bursts**, by the names phones give them (Pixel, Samsung, Huawei, Sony, Nothing). The one
  the phone marked as the cover is shown, or else the first.

A file joins at most one stack, in that order. Identical copies elsewhere in the export (album
folders) are left out: only the copy that is uploaded is stacked.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from googich_takeaway.takeout.sidecars import EDITED_SUFFIXES

if TYPE_CHECKING:
    from googich_takeaway.takeout.scan import ScannedItem

EDITED = "edited"
RAW = "raw"
BURST = "burst"

RAW_EXTENSIONS = frozenset({"dng", "cr2", "cr3", "nef", "arw", "orf", "rw2", "raf", "srw", "pef"})
SHOWN_EXTENSIONS = frozenset({"jpg", "jpeg", "heic", "heif"})

# (pattern, group naming the burst). A ``cover`` group marks the phone's chosen cover.
_BURSTS = (
    # Pixel: PXL_20230330_184138390.MOTION-01.COVER.jpg
    re.compile(r"^(?P<key>PXL_\d{8}_\d{9})\.MOTION-\d{2}\.(?:(?P<cover>COVER)|ORIGINAL)\.", re.I),
    # Older Pixel and Nothing: 00001IMG_00001_BURST1723801037429_COVER.jpg
    re.compile(r"^\d{5}IMG_\d{5}_BURST(?P<key>\d{13,17})(?P<cover>_COVER)?\.", re.I),
    # Huawei: IMG_20231014_183246_BURST001_COVER.jpg
    re.compile(r"^(?P<key>IMG_\d{8}_\d{6})_BURST\d{3}(?P<cover>_COVER)?\.", re.I),
    # Sony: DSC_0035_BURST20230709220904977_COVER.JPG
    re.compile(r"^DSC_\d+_BURST(?P<key>\d{17})(?P<cover>_COVER)?\.", re.I),
    # Samsung: 20231207_101605_001.jpg
    re.compile(r"^(?P<key>\d{8}_\d{6})_\d{3}\.", re.I),
)


def mark_stacks(items: list["ScannedItem"]) -> list["ScannedItem"]:
    """Give the files that belong together a shared ``stack`` key, and mark each stack's
    cover."""
    unique = [i for i in items if i.duplicate_of is None]
    taken: set[str] = set()
    found: dict[str, tuple[str, list[ScannedItem], str]] = {}
    for kind, grouping, cover in (
        (EDITED, _edited_key, _edited_cover),
        (RAW, _raw_key, _raw_cover),
        (BURST, _burst_key, _burst_cover),
    ):
        groups: dict[str, list[ScannedItem]] = {}
        for item in unique:
            if item.path in taken:
                continue
            key = grouping(item.path)
            if key is not None:
                # A photo and a video of the same name (a Live Photo) are not one stack.
                groups.setdefault(f"{kind}:{item.kind.value}:{key}", []).append(item)
        for key, members in groups.items():
            if len(members) < 2:
                continue
            if kind == RAW and not _is_raw_pair(members):
                continue
            if kind == EDITED and not any(_is_edited(m.path) for m in members):
                continue
            chosen = cover(members)
            found[key] = (kind, members, chosen.path)
            taken.update(m.path for m in members)
    marks: dict[str, tuple[str, str, bool]] = {}
    for key, (kind, members, cover_path) in found.items():
        for member in members:
            marks[member.path] = (key, kind, member.path == cover_path)
    result = []
    for item in items:
        if item.path in marks:
            key, kind, is_cover = marks[item.path]
            item = replace(item, stack=key, stack_kind=kind, stack_cover=is_cover)
        result.append(item)
    return result


def _split(path: str) -> tuple[str, str, str]:
    """Folder, name without extension, extension (lower case, no dot)."""
    pure = PurePosixPath(path)
    return str(pure.parent), pure.stem, pure.suffix.lower().removeprefix(".")


def _edited_key(path: str) -> str | None:
    folder, stem, _ = _split(path)
    lowered = stem.lower()
    for suffix in EDITED_SUFFIXES:
        if lowered.endswith(suffix):
            return f"{folder}/{lowered[: -len(suffix)]}"
    return f"{folder}/{lowered}"


def _edited_cover(members: list["ScannedItem"]) -> "ScannedItem":
    edited = [m for m in members if _is_edited(m.path)]
    return min(edited or members, key=lambda m: m.path)


def _is_edited(path: str) -> bool:
    stem = PurePosixPath(path).stem.lower()
    return any(stem.endswith(suffix) for suffix in EDITED_SUFFIXES)


def _raw_key(path: str) -> str | None:
    folder, stem, extension = _split(path)
    if extension in RAW_EXTENSIONS or extension in SHOWN_EXTENSIONS:
        return f"{folder}/{stem.lower()}"
    return None


def _is_raw_pair(members: list["ScannedItem"]) -> bool:
    extensions = {_split(m.path)[2] for m in members}
    return bool(extensions & RAW_EXTENSIONS) and bool(extensions & SHOWN_EXTENSIONS)


def _raw_cover(members: list["ScannedItem"]) -> "ScannedItem":
    shown = [m for m in members if _split(m.path)[2] in SHOWN_EXTENSIONS]
    return min(shown or members, key=lambda m: m.path)


def _burst_key(path: str) -> str | None:
    folder, _, _ = _split(path)
    name = PurePosixPath(path).name
    for pattern in _BURSTS:
        found = pattern.match(name)
        if found:
            return f"{folder}/{found['key'].lower()}"
    return None


def _burst_cover(members: list["ScannedItem"]) -> "ScannedItem":
    def marked(item: "ScannedItem") -> bool:
        name = PurePosixPath(item.path).name
        for pattern in _BURSTS:
            found = pattern.match(name)
            if found:
                return bool(found.groupdict().get("cover"))
        return False

    covers = [m for m in members if marked(m)]
    return min(covers or members, key=lambda m: m.path)


def stack_groups(items: Iterable["ScannedItem"]) -> dict[str, list["ScannedItem"]]:
    """Stacked files by stack key, the cover first."""
    groups: dict[str, list[ScannedItem]] = {}
    for item in items:
        if item.stack:
            groups.setdefault(item.stack, []).append(item)
    sort: Callable[[ScannedItem], tuple[bool, str]] = lambda i: (not i.stack_cover, i.path)  # noqa: E731
    return {key: sorted(members, key=sort) for key, members in groups.items()}
