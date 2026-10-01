from datetime import datetime

import pytest

from googich_takeaway.takeout.filenames import parse_filename_date


@pytest.mark.parametrize(
    ("name", "value", "is_utc", "has_time"),
    [
        ("PXL_20190704_001500123.jpg", datetime(2019, 7, 4, 0, 15), True, True),
        ("PXL_20190704_001500123.MP.jpg", datetime(2019, 7, 4, 0, 15), True, True),
        ("IMG_20190704_101500.jpg", datetime(2019, 7, 4, 10, 15), False, True),
        ("VID_20190704_101500.mp4", datetime(2019, 7, 4, 10, 15), False, True),
        ("MVIMG_20190704_101500.jpg", datetime(2019, 7, 4, 10, 15), False, True),
        ("IMG-20190704-WA0001.jpg", datetime(2019, 7, 4, 12, 0), False, False),
        ("Screenshot_20190704-101500.png", datetime(2019, 7, 4, 10, 15), False, True),
        ("Screenshot_2019-07-04-10-15-00.png", datetime(2019, 7, 4, 10, 15), False, True),
        ("2019-07-04 10.15.00.jpg", datetime(2019, 7, 4, 10, 15), False, True),
        ("20190704_101500.jpg", datetime(2019, 7, 4, 10, 15), False, True),
    ],
)
def test_recognised_names(name: str, value: datetime, is_utc: bool, has_time: bool) -> None:
    result = parse_filename_date(name)
    assert result is not None
    assert (result.value, result.is_utc, result.has_time) == (value, is_utc, has_time)


@pytest.mark.parametrize(
    "name",
    ["holiday.jpg", "IMG_1234.jpg", "IMG_20191304_101500.jpg", "DSC00042.ARW", ""],
)
def test_unrecognised_or_impossible_names(name: str) -> None:
    assert parse_filename_date(name) is None
