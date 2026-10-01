from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from googich_takeaway.takeout.dates import (
    DateInputs,
    DateResolver,
    DateSource,
    OffsetSource,
)
from googich_takeaway.takeout.filenames import parse_filename_date

MELBOURNE = ZoneInfo("Australia/Melbourne")
SYDNEY = ZoneInfo("Australia/Sydney")
NOW = datetime(2026, 10, 1, tzinfo=UTC)
H = timedelta(hours=1)


def sydney_lookup(latitude: float, longitude: float) -> tzinfo | None:
    return SYDNEY


resolver = DateResolver(default_timezone=MELBOURNE)
gps_resolver = DateResolver(default_timezone=MELBOURNE, timezone_lookup=sydney_lookup)


def test_exif_consistent_with_sidecar_keeps_exif_and_derives_offset() -> None:
    result = resolver.resolve(
        DateInputs(
            exif_local=datetime(2019, 7, 4, 10, 15),
            sidecar_utc=datetime(2019, 7, 4, 0, 15, tzinfo=UTC),
        ),
        NOW,
    )
    assert result is not None
    assert result.source is DateSource.EXIF
    assert result.offset_source is OffsetSource.SIDECAR_DIFFERENCE
    assert result.xmp_value() == "2019-07-04T10:15:00+10:00"


def test_exif_offset_tag_wins_over_everything_else() -> None:
    result = gps_resolver.resolve(
        DateInputs(
            exif_local=datetime(2019, 7, 4, 10, 15),
            exif_offset=timedelta(hours=9, minutes=30),
            sidecar_utc=datetime(2019, 7, 4, 0, 45, tzinfo=UTC),
            gps=(-33.86, 151.21),
        ),
        NOW,
    )
    assert result is not None
    assert result.offset_source is OffsetSource.EXIF_OFFSET
    assert result.xmp_value() == "2019-07-04T10:15:00+09:30"


def test_gps_zone_preferred_over_sidecar_difference() -> None:
    result = gps_resolver.resolve(
        DateInputs(
            exif_local=datetime(2019, 1, 4, 10, 15),
            sidecar_utc=datetime(2019, 1, 3, 23, 15, tzinfo=UTC),
            gps=(-33.86, 151.21),
        ),
        NOW,
    )
    assert result is not None
    assert result.offset_source is OffsetSource.GPS
    assert result.xmp_value() == "2019-01-04T10:15:00+11:00"


def test_date_edited_in_google_photos_sidecar_wins() -> None:
    result = resolver.resolve(
        DateInputs(
            exif_local=datetime(2019, 7, 4, 10, 15),
            sidecar_utc=datetime(2015, 1, 1, 1, 0, tzinfo=UTC),
        ),
        NOW,
    )
    assert result is not None
    assert result.source is DateSource.SIDECAR
    assert result.offset_source is OffsetSource.DEFAULT_TIMEZONE
    assert result.xmp_value() == "2015-01-01T12:00:00+11:00"  # Melbourne daylight time


def test_sidecar_only_uses_gps_zone() -> None:
    result = gps_resolver.resolve(
        DateInputs(sidecar_utc=datetime(2016, 3, 3, 2, 0, tzinfo=UTC), gps=(-33.86, 151.21)),
        NOW,
    )
    assert result is not None
    assert result.source is DateSource.SIDECAR
    assert result.offset_source is OffsetSource.GPS
    assert result.xmp_value() == "2016-03-03T13:00:00+11:00"


def test_zero_gps_is_treated_as_no_location() -> None:
    result = gps_resolver.resolve(
        DateInputs(sidecar_utc=datetime(2016, 7, 3, 2, 0, tzinfo=UTC), gps=(0.0, 0.0)),
        NOW,
    )
    assert result is not None
    assert result.offset_source is OffsetSource.DEFAULT_TIMEZONE


def test_exif_only_uses_default_zone_with_daylight_saving() -> None:
    winter = resolver.resolve(DateInputs(exif_local=datetime(2019, 7, 4, 10, 15)), NOW)
    summer = resolver.resolve(DateInputs(exif_local=datetime(2019, 1, 4, 10, 15)), NOW)
    assert winter is not None
    assert summer is not None
    assert winter.offset == 10 * H
    assert summer.offset == 11 * H


def test_video_container_time_is_utc() -> None:
    result = resolver.resolve(DateInputs(video_utc=datetime(2020, 6, 1, 0, 0, tzinfo=UTC)), NOW)
    assert result is not None
    assert result.source is DateSource.VIDEO
    assert result.xmp_value() == "2020-06-01T10:00:00+10:00"


def test_quicktime_unknown_date_falls_through_to_filename() -> None:
    result = resolver.resolve(
        DateInputs(
            video_utc=datetime(1904, 1, 1, tzinfo=UTC),
            filename_date=parse_filename_date("VID_20200601_101500.mp4"),
        ),
        NOW,
    )
    assert result is not None
    assert result.source is DateSource.FILENAME
    assert result.xmp_value() == "2020-06-01T10:15:00+10:00"


def test_pixel_filename_is_utc() -> None:
    result = resolver.resolve(
        DateInputs(filename_date=parse_filename_date("PXL_20200601_001500123.jpg")), NOW
    )
    assert result is not None
    assert result.xmp_value() == "2020-06-01T10:15:00+10:00"


def test_future_dates_are_ignored() -> None:
    result = resolver.resolve(
        DateInputs(
            exif_local=datetime(2099, 1, 1),
            sidecar_utc=datetime(2020, 6, 1, 0, 0, tzinfo=UTC),
        ),
        NOW,
    )
    assert result is not None
    assert result.source is DateSource.SIDECAR


def test_no_usable_date_returns_none() -> None:
    assert resolver.resolve(DateInputs(), NOW) is None
    assert (
        resolver.resolve(DateInputs(filename_date=parse_filename_date("holiday.jpg")), NOW) is None
    )


def test_naive_instant_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        resolver.resolve(DateInputs(sidecar_utc=datetime(2020, 1, 1)), NOW)


instants = st.datetimes(
    min_value=datetime(1990, 1, 1), max_value=datetime(2026, 1, 1), timezones=st.just(UTC)
)
offsets = st.integers(min_value=-48, max_value=56).map(lambda q: q * timedelta(minutes=15))


@given(instant=instants, offset=offsets)
def test_consistent_exif_always_keeps_wall_time_and_instant(
    instant: datetime, offset: timedelta
) -> None:
    instant = instant.replace(microsecond=0)
    exif = (instant + offset).replace(tzinfo=None)
    result = resolver.resolve(DateInputs(exif_local=exif, sidecar_utc=instant), NOW)
    assert result is not None
    assert result.source is DateSource.EXIF
    assert result.local == exif
    assert result.utc == instant


@given(instant=instants, gap_hours=st.integers(min_value=15, max_value=24 * 365 * 5))
def test_large_gap_always_means_sidecar_wins(instant: datetime, gap_hours: int) -> None:
    exif = (instant + timedelta(hours=gap_hours)).replace(tzinfo=None)
    result = resolver.resolve(DateInputs(exif_local=exif, sidecar_utc=instant), NOW)
    assert result is not None
    assert result.source is DateSource.SIDECAR
    assert result.utc == instant


@pytest.mark.parametrize(
    "unknown", [datetime(1904, 1, 1, tzinfo=UTC), datetime(1970, 1, 1, tzinfo=UTC)]
)
def test_epoch_zero_values_mean_unknown(unknown: datetime) -> None:
    assert resolver.resolve(DateInputs(video_utc=unknown), NOW) is None
    assert resolver.resolve(DateInputs(sidecar_utc=unknown), NOW) is None


def test_old_scanned_photo_dates_are_kept() -> None:
    result = resolver.resolve(DateInputs(exif_local=datetime(1955, 12, 25, 9, 0)), NOW)
    assert result is not None
    assert result.local == datetime(1955, 12, 25, 9, 0)
