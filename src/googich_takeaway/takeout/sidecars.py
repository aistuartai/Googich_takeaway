"""Pairing media files with their Takeout JSON sidecars.

Takeout names a sidecar after its media file, but not consistently:

- ``IMG_1.jpg.json`` (older exports) or ``IMG_1.jpg.supplemental-metadata.json`` (newer).
- Sidecar names are truncated, so the ``.supplemental-metadata`` part is often cut short.
- Long media names are truncated too, differently from their sidecars.
- Duplicates: ``IMG_1(1).jpg`` pairs with ``IMG_1.jpg(1).json``.
- Edited copies (``IMG_1-edited.jpg``) and Live Photo videos share the original's sidecar.
- A sidecar can sit in a different archive part from its media file.

Matching works on archive paths across all parts of one export, so split exports pair correctly.
Only files in the same folder are paired. Results are deterministic and record which rule matched.
"""

import bisect
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

PHOTO_EXTENSIONS = frozenset(
    {
        "jpg", "jpeg", "png", "gif", "heic", "heif", "webp", "bmp", "tif", "tiff", "avif",
        "dng", "cr2", "cr3", "nef", "arw", "orf", "rw2", "raf", "srw", "pef",
    }
)  # fmt: skip
VIDEO_EXTENSIONS = frozenset(
    {"mp4", "mov", "m4v", "3gp", "avi", "mkv", "mts", "m2ts", "wmv", "mpg", "mpeg", "webm", "mp"}
)
MEDIA_EXTENSIONS = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS

# Suffixes Google Photos adds to edited copies, by interface language.
EDITED_SUFFIXES = (
    "-edited",  # English
    "-bearbeitet",  # German
    "-modifié",  # French
    "-editado",  # Spanish, Portuguese
    "-modificato",  # Italian
    "-bewerkt",  # Dutch
    "-redigeret",  # Danish
    "-redigert",  # Norwegian
    "-redigerad",  # Swedish
)

# JSON files Takeout writes that are not media sidecars.
NON_SIDECAR_NAMES = frozenset(
    {
        "metadata.json",
        "print-subscriptions.json",
        "shared_album_comments.json",
        "user-generated-memory-titles.json",
    }
)

SUPPLEMENTAL = ".supplemental-metadata"
# A truncated sidecar stem shorter than the media name is only accepted if at least this long,
# so short unrelated names never match by prefix.
MIN_TRUNCATED_STEM = 40
# Media names at least this long may have been truncated by Takeout.
MIN_TRUNCATED_MEDIA = 46

_DUPLICATE = re.compile(r"^(?P<stem>.*)\((?P<n>\d+)\)(?P<ext>\.[^.()]+)?$")
_SIDECAR_DUPLICATE = re.compile(r"^(?P<stem>.*)\((?P<n>\d+)\)$")


class MatchRule(StrEnum):
    NAME = "name"
    """Sidecar name derived from the media name, allowing for truncation."""
    TRUNCATED_MEDIA = "truncated-media"
    """Media name was truncated; matched on the shared prefix (and title, if known)."""
    EDITED = "edited"
    """Edited copy using the original's sidecar."""
    LIVE_PHOTO = "live-photo"
    """Motion video using its still image's sidecar."""
    TITLE = "title"
    """Matched by the ``title`` recorded inside the sidecar."""
    NONE = "none"


@dataclass(frozen=True)
class SidecarMatch:
    media: str
    sidecar: str | None
    rule: MatchRule


@dataclass(frozen=True)
class _Sidecar:
    path: str
    stem: str
    """Name without ``.json`` and without a trailing ``(n)``."""
    duplicate: int


def is_media(path: str) -> bool:
    return PurePosixPath(path).suffix.lower().removeprefix(".") in MEDIA_EXTENSIONS


def is_sidecar_candidate(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.lower().endswith(".json") and name not in NON_SIDECAR_NAMES


def match_sidecars(
    paths: Iterable[str],
    titles: Mapping[str, str] | None = None,
) -> dict[str, SidecarMatch]:
    """Pair every media path in ``paths`` with its sidecar.

    ``paths`` are archive paths from all parts of one export. ``titles`` optionally maps sidecar
    paths to the ``title`` field read from them; it disambiguates truncated names and enables
    matching by title. Returns one result per media path.
    """
    titles = titles or {}
    all_paths = sorted(set(paths))
    sidecars_by_folder: dict[str, list[_Sidecar]] = defaultdict(list)
    media_by_folder: dict[str, list[str]] = defaultdict(list)
    for path in all_paths:
        folder = _folder(path)
        if is_sidecar_candidate(path):
            sidecars_by_folder[folder].append(_parse_sidecar(path))
        elif is_media(path):
            media_by_folder[folder].append(path)

    results: dict[str, SidecarMatch] = {}
    for folder, media_paths in media_by_folder.items():
        results.update(_match_folder(media_paths, sidecars_by_folder.get(folder, []), titles))
    return dict(sorted(results.items()))


def _match_folder(
    media_paths: list[str],
    sidecars: list[_Sidecar],
    titles: Mapping[str, str],
) -> dict[str, SidecarMatch]:
    results: dict[str, SidecarMatch] = {}
    used: set[str] = set()
    # Indexes, so a folder of tens of thousands of photos is matched in linear time.
    by_stem: dict[tuple[int, str], list[_Sidecar]] = defaultdict(list)
    by_title: dict[str, list[_Sidecar]] = defaultdict(list)
    for sidecar in sidecars:
        by_stem[(sidecar.duplicate, sidecar.stem)].append(sidecar)
        if sidecar.path in titles:
            by_title[titles[sidecar.path]].append(sidecar)

    # Sidecars without a (n), sorted by stem, so a prefix finds its candidates by bisection.
    plain_stems = sorted((s.stem, s.path, s) for s in sidecars if s.duplicate == 0)

    # Pass 1: names derived from the media name.
    for media in media_paths:
        found = _by_name(_name(media), by_stem)
        if found:
            results[media] = SidecarMatch(media, found.path, MatchRule.NAME)
            used.add(found.path)

    # Pass 2: truncated media names, using only sidecars nobody claimed.
    for media in media_paths:
        if media in results or len(_name(media)) < MIN_TRUNCATED_MEDIA:
            continue
        found = _by_truncated_media(_name(media), plain_stems, used, titles)
        if found:
            results[media] = SidecarMatch(media, found.path, MatchRule.TRUNCATED_MEDIA)
            used.add(found.path)

    # Pass 3: title recorded inside the sidecar.
    for media in media_paths:
        if media in results:
            continue
        name = _name(media)
        for sidecar in by_title.get(name, ()):
            if sidecar.path not in used:
                results[media] = SidecarMatch(media, sidecar.path, MatchRule.TITLE)
                used.add(sidecar.path)
                break

    # Pass 4: files that share another file's sidecar.
    by_name = {_name(m): m for m in media_paths}
    photos_by_stem: dict[str, list[str]] = defaultdict(list)
    for other in media_paths:
        candidate = PurePosixPath(other)
        if candidate.suffix.lower().removeprefix(".") in PHOTO_EXTENSIONS:
            photos_by_stem[candidate.stem].append(other)
    for media in media_paths:
        if media in results:
            continue
        shared = _edited_original(media, by_name, results)
        if shared:
            results[media] = SidecarMatch(media, shared, MatchRule.EDITED)
            continue
        shared = _live_photo_image(media, photos_by_stem, results)
        if shared:
            results[media] = SidecarMatch(media, shared, MatchRule.LIVE_PHOTO)
            continue
        results[media] = SidecarMatch(media, None, MatchRule.NONE)
    return results


def _by_name(name: str, by_stem: Mapping[tuple[int, str], list[_Sidecar]]) -> _Sidecar | None:
    """Sidecar whose stem is the media name, plain or with ``.supplemental-metadata``,
    possibly truncated (the longest such stem wins). Tries the ``(n)`` duplicate reading first.

    An acceptable stem is a prefix of ``<name>.supplemental-metadata`` that still covers the
    whole media name, or is at least ``MIN_TRUNCATED_STEM`` long (cut by Takeout's length limit,
    not an unrelated shorter name). So candidates are looked up prefix by prefix, longest first."""
    readings: list[tuple[str, int]] = []
    duplicate = _DUPLICATE.match(name)
    if duplicate:
        readings.append((duplicate["stem"] + (duplicate["ext"] or ""), int(duplicate["n"])))
    readings.append((name, 0))

    for base, n in readings:
        full = base + SUPPLEMENTAL
        shortest = min(len(base), MIN_TRUNCATED_STEM)
        for length in range(len(full), max(shortest, 1) - 1, -1):
            found = by_stem.get((n, full[:length]))
            if found:
                return found[0]
    return None


def _by_truncated_media(
    name: str,
    plain_stems: list[tuple[str, str, _Sidecar]],
    used: set[str],
    titles: Mapping[str, str],
) -> _Sidecar | None:
    if len(name) < MIN_TRUNCATED_MEDIA:
        return None
    stem, dot, extension = name.rpartition(".")
    if not dot:
        stem, extension = name, ""
    start = bisect.bisect_left(plain_stems, (stem,))
    candidates = []
    for found_stem, path, sidecar in plain_stems[start:]:
        if not found_stem.startswith(stem):
            break
        if path not in used:
            candidates.append(sidecar)
    titled = [s for s in candidates if s.path in titles]
    if titled:
        # Titles hold the full original name: require it to start and end like the media file.
        candidates = [
            s
            for s in titled
            if titles[s.path].startswith(stem)
            and titles[s.path].lower().endswith("." + extension.lower())
        ]
    return candidates[0] if len(candidates) == 1 else None


def _edited_original(
    media: str, by_name: Mapping[str, str], results: Mapping[str, SidecarMatch]
) -> str | None:
    stem, dot, extension = _name(media).rpartition(".")
    for suffix in EDITED_SUFFIXES:
        if stem.endswith(suffix):
            original = by_name.get(stem.removesuffix(suffix) + dot + extension)
            if original and original in results:
                return results[original].sidecar
    return None


def _live_photo_image(
    media: str, photos_by_stem: Mapping[str, list[str]], results: Mapping[str, SidecarMatch]
) -> str | None:
    path = PurePosixPath(media)
    if path.suffix.lower().removeprefix(".") not in VIDEO_EXTENSIONS:
        return None
    for other in photos_by_stem.get(path.stem, ()):
        if other in results and results[other].sidecar:
            return results[other].sidecar
    return None


def _parse_sidecar(path: str) -> _Sidecar:
    stem = PurePosixPath(path).name[: -len(".json")]
    duplicate = _SIDECAR_DUPLICATE.match(stem)
    if duplicate:
        return _Sidecar(path, duplicate["stem"], int(duplicate["n"]))
    return _Sidecar(path, stem, 0)


def _folder(path: str) -> str:
    return str(PurePosixPath(path).parent)


def _name(path: str) -> str:
    return PurePosixPath(path).name
