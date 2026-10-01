import json
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from googich_takeaway.takeout.sidecars import MatchRule, match_sidecars
from tests.fixtures.takeout import (
    ROOT,
    SidecarStyle,
    TakeoutBuilder,
    duplicate_name,
    quirks_export,
    sidecar_name,
)

F = f"{ROOT}/Photos from 2019"


def titles_of(builder: TakeoutBuilder) -> dict[str, str]:
    return {
        e.path: json.loads(e.data)["title"]
        for e in builder.entries
        if e.path.endswith(".json") and not e.path.endswith("/metadata.json")
    }


def test_quirks_export_every_item_gets_its_expected_sidecar() -> None:
    builder = quirks_export()
    results = match_sidecars([e.path for e in builder.entries], titles_of(builder))
    assert set(results) == {item.path for item in builder.expected}
    for item in builder.expected:
        assert results[item.path].sidecar == item.sidecar_path, item.path


def test_quirks_export_without_titles_still_matches_by_name() -> None:
    builder = quirks_export()
    results = match_sidecars([e.path for e in builder.entries])
    for item in builder.expected:
        assert results[item.path].sidecar == item.sidecar_path, item.path


@pytest.mark.parametrize(
    ("media", "sidecar"),
    [
        ("IMG_1.jpg", "IMG_1.jpg.json"),
        ("IMG_1.jpg", "IMG_1.jpg.supplemental-metadata.json"),
        ("IMG_1.jpg", "IMG_1.jpg.supplemental-metad.json"),
        ("IMG_1.jpg", "IMG_1.jpg.supp.json"),
        ("IMG_1(1).jpg", "IMG_1.jpg(1).json"),
        ("IMG_1(2).jpg", "IMG_1.jpg.supplemental-metadata(2).json"),
        ("photo(1).jpg", "photo(1).jpg.json"),  # "(1)" that is part of the real name
    ],
)
def test_name_rules(media: str, sidecar: str) -> None:
    results = match_sidecars([f"{F}/{media}", f"{F}/{sidecar}"])
    assert results[f"{F}/{media}"] == results[f"{F}/{media}"].__class__(
        f"{F}/{media}", f"{F}/{sidecar}", MatchRule.NAME
    )


def test_duplicates_do_not_steal_each_others_sidecars() -> None:
    paths = [
        f"{F}/IMG_1.jpg",
        f"{F}/IMG_1(1).jpg",
        f"{F}/IMG_1(2).jpg",
        f"{F}/IMG_1.jpg.json",
        f"{F}/IMG_1.jpg(1).json",
        f"{F}/IMG_1.jpg(2).json",
    ]
    results = match_sidecars(paths)
    assert results[f"{F}/IMG_1.jpg"].sidecar == f"{F}/IMG_1.jpg.json"
    assert results[f"{F}/IMG_1(1).jpg"].sidecar == f"{F}/IMG_1.jpg(1).json"
    assert results[f"{F}/IMG_1(2).jpg"].sidecar == f"{F}/IMG_1.jpg(2).json"


def test_short_unrelated_prefix_is_not_a_match() -> None:
    results = match_sidecars([f"{F}/IMG_12.jpg", f"{F}/IMG_1.jpg.json"])
    assert results[f"{F}/IMG_12.jpg"].rule is MatchRule.NONE


def test_sidecars_only_match_within_their_folder() -> None:
    results = match_sidecars([f"{F}/IMG_1.jpg", f"{ROOT}/Other album/IMG_1.jpg.json"])
    assert results[f"{F}/IMG_1.jpg"].sidecar is None


def test_album_metadata_is_never_a_sidecar() -> None:
    results = match_sidecars([f"{F}/metadata.jpg", f"{F}/metadata.json"])
    assert results[f"{F}/metadata.jpg"].sidecar is None


def test_edited_copy_shares_original_sidecar_in_other_languages() -> None:
    paths = [f"{F}/IMG_1.jpg", f"{F}/IMG_1-bearbeitet.jpg", f"{F}/IMG_1.jpg.json"]
    results = match_sidecars(paths)
    assert results[f"{F}/IMG_1-bearbeitet.jpg"].sidecar == f"{F}/IMG_1.jpg.json"
    assert results[f"{F}/IMG_1-bearbeitet.jpg"].rule is MatchRule.EDITED


def test_live_photo_video_uses_image_sidecar() -> None:
    paths = [f"{F}/IMG_4.HEIC", f"{F}/IMG_4.MP4", f"{F}/IMG_4.HEIC.json"]
    results = match_sidecars(paths)
    assert results[f"{F}/IMG_4.MP4"].sidecar == f"{F}/IMG_4.HEIC.json"
    assert results[f"{F}/IMG_4.MP4"].rule is MatchRule.LIVE_PHOTO


def test_video_with_own_sidecar_prefers_it() -> None:
    paths = [f"{F}/IMG_4.HEIC", f"{F}/IMG_4.MP4", f"{F}/IMG_4.HEIC.json", f"{F}/IMG_4.MP4.json"]
    results = match_sidecars(paths)
    assert results[f"{F}/IMG_4.MP4"].sidecar == f"{F}/IMG_4.MP4.json"


def test_truncated_media_name_is_disambiguated_by_title() -> None:
    stem = "Family holiday at the beach house summer 20"  # 43 characters, as Takeout cuts it
    media = f"{F}/{stem}.jpg"
    jpg_sidecar = f"{F}/{stem}19 s.json"
    png_sidecar = f"{F}/{stem}18 s.json"
    paths = [media, jpg_sidecar, png_sidecar]
    titles = {jpg_sidecar: f"{stem}19 sunset.jpg", png_sidecar: f"{stem}18 sunset.png"}

    with_titles = match_sidecars(paths, titles)[media]
    assert with_titles.sidecar == jpg_sidecar
    assert with_titles.rule is MatchRule.TRUNCATED_MEDIA

    # Without titles both sidecars fit; refusing to guess is the correct answer.
    assert match_sidecars(paths)[media].rule is MatchRule.NONE


def test_match_by_title_when_names_are_unrelated() -> None:
    paths = [f"{F}/renamed.jpg", f"{F}/whatever.json"]
    results = match_sidecars(paths, {f"{F}/whatever.json": "renamed.jpg"})
    assert results[f"{F}/renamed.jpg"].rule is MatchRule.TITLE


def test_result_is_independent_of_input_order() -> None:
    paths = [e.path for e in quirks_export().entries]
    assert match_sidecars(paths) == match_sidecars(list(reversed(paths)))


names = st.from_regex(r"[A-Za-z0-9_ -]{1,60}", fullmatch=True).filter(
    lambda s: s.strip() == s and "(" not in s
)


@given(
    stem=names,
    extension=st.sampled_from(["jpg", "JPG", "heic", "mp4", "png"]),
    style=st.sampled_from(list(SidecarStyle)),
    duplicate=st.integers(min_value=0, max_value=3),
)
def test_any_takeout_named_sidecar_is_found(
    stem: str, extension: str, style: SidecarStyle, duplicate: int
) -> None:
    original = f"{stem}.{extension}"
    media = duplicate_name(original, duplicate) if duplicate else original
    sidecar = sidecar_name(original, style, duplicate)
    results = match_sidecars([f"{F}/{media}", f"{F}/{sidecar}"])
    assert results[f"{F}/{media}"].sidecar == f"{F}/{sidecar}"


def test_split_export_pairs_across_parts(tmp_path: object) -> None:
    builder = TakeoutBuilder()
    item = builder.add_photo(
        "IMG_9.jpg", taken_utc=datetime(2020, 1, 1, tzinfo=UTC), media_part=1, sidecar_part=3
    )
    results = match_sidecars([e.path for e in builder.entries])
    assert results[item.path].sidecar == item.sidecar_path
