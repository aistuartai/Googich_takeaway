from pathlib import Path

from googich_takeaway.takeout.scan import MediaKind, ScannedItem
from googich_takeaway.takeout.sidecars import MatchRule
from googich_takeaway.takeout.stacks import mark_stacks, stack_groups


def _item(
    path: str, kind: MediaKind = MediaKind.PHOTO, duplicate_of: str | None = None
) -> ScannedItem:
    return ScannedItem(
        Path("a.zip"), path, 1, path, kind, None, MatchRule.NONE, None, None, False, "",
        duplicate_of=duplicate_of,
    )  # fmt: skip


def _stacks(*paths: str, **kinds: MediaKind) -> list[list[str]]:
    items = [
        _item(p, kinds.get(p.rsplit("/", 1)[-1].replace(".", "_"), MediaKind.PHOTO)) for p in paths
    ]
    return sorted(
        [m.path for m in members] for members in stack_groups(mark_stacks(items)).values()
    )


def test_edited_copies_raw_pairs_and_bursts_are_stacked_with_the_right_cover() -> None:
    assert _stacks("F/IMG_1.jpg", "F/IMG_1-edited.jpg", "F/IMG_9.jpg") == [
        ["F/IMG_1-edited.jpg", "F/IMG_1.jpg"]
    ]
    assert _stacks("F/DSC_2.NEF", "F/DSC_2.JPG") == [["F/DSC_2.JPG", "F/DSC_2.NEF"]]
    assert _stacks("F/IMG_2.jpg", "F/IMG_2.png") == []  # no RAW: not a pair
    assert _stacks(
        "F/IMG_20231014_183246_BURST002.jpg", "F/IMG_20231014_183246_BURST001_COVER.jpg"
    ) == [["F/IMG_20231014_183246_BURST001_COVER.jpg", "F/IMG_20231014_183246_BURST002.jpg"]]
    pixel = "F/PXL_20230330_184138390.MOTION-0"
    assert _stacks(f"{pixel}2.ORIGINAL.jpg", f"{pixel}1.COVER.jpg")[0][0].endswith("COVER.jpg")
    assert len(_stacks("F/20231207_101605_001.jpg", "F/20231207_101605_002.jpg")) == 1


def test_different_folders_and_album_copies_are_not_stacked() -> None:
    assert _stacks("A/IMG_1.jpg", "B/IMG_1-edited.jpg") == []
    items = mark_stacks(
        [_item("F/IMG_1.jpg"), _item("Album/IMG_1-edited.jpg", duplicate_of="F/IMG_1-edited.jpg")]
    )
    assert not any(i.stack for i in items)


def test_a_live_photo_is_not_an_edited_stack() -> None:
    items = mark_stacks(
        [_item("F/IMG_1.jpg"), _item("F/IMG_1.mov", MediaKind.VIDEO), _item("F/IMG_1-edited.jpg")]
    )
    groups = stack_groups(items)
    [members] = groups.values()
    assert [m.path for m in members] == ["F/IMG_1-edited.jpg", "F/IMG_1.jpg"]
