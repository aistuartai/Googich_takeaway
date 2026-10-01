"""Deciding the capture date of a photo or video.

Inputs disagree about time zones:

- The Takeout sidecar ``photoTakenTime`` is a UTC instant with no zone.
- EXIF ``DateTimeOriginal`` is local wall-clock time, usually with no offset.
- Video container ``CreateDate`` is UTC.

Immich treats a time without an offset as UTC, so the result here always carries an explicit
offset. The rules are deterministic, and the result records which inputs were used.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from enum import StrEnum

from googich_takeaway.takeout.filenames import FilenameDate

TimezoneLookup = Callable[[float, float], tzinfo | None]
"""Returns the time zone at a latitude and longitude, or None if unknown."""

# EXIF wall-clock time and the sidecar UTC instant are considered the same moment when they
# differ by no more than this. Real offsets run from UTC-12 to UTC+14.
DEFAULT_TOLERANCE = timedelta(hours=14)

_MIN_OFFSET = timedelta(hours=-12)
_MAX_OFFSET = timedelta(hours=14)
_OFFSET_STEP = timedelta(minutes=15)
_EARLIEST = datetime(1900, 1, 1, tzinfo=UTC)
# Zero values that tools write for "unknown": the QuickTime and Unix epochs.
_UNKNOWN_INSTANTS = frozenset({datetime(1904, 1, 1, tzinfo=UTC), datetime(1970, 1, 1, tzinfo=UTC)})


class DateSource(StrEnum):
    EXIF = "exif"
    SIDECAR = "sidecar"
    VIDEO = "video"
    FILENAME = "filename"


class OffsetSource(StrEnum):
    EXIF_OFFSET = "exif-offset"
    GPS = "gps"
    SIDECAR_DIFFERENCE = "sidecar-difference"
    DEFAULT_TIMEZONE = "default-timezone"


@dataclass(frozen=True)
class DateInputs:
    """Everything known about one media file's capture time."""

    sidecar_utc: datetime | None = None
    """``photoTakenTime`` from the Takeout sidecar, timezone-aware."""
    exif_local: datetime | None = None
    """EXIF ``DateTimeOriginal``, naive local wall-clock time."""
    exif_offset: timedelta | None = None
    """EXIF ``OffsetTimeOriginal``."""
    video_utc: datetime | None = None
    """Video container creation time, timezone-aware."""
    gps: tuple[float, float] | None = None
    """Latitude and longitude, from the file or the sidecar."""
    filename_date: FilenameDate | None = None


@dataclass(frozen=True)
class ResolvedDate:
    local: datetime
    """Naive local wall-clock time."""
    offset: timedelta
    source: DateSource
    offset_source: OffsetSource

    @property
    def aware(self) -> datetime:
        return self.local.replace(tzinfo=_fixed(self.offset))

    @property
    def utc(self) -> datetime:
        return self.aware.astimezone(UTC)

    def xmp_value(self) -> str:
        """ISO 8601 with an explicit offset, e.g. ``2019-07-04T10:15:00+10:00``."""
        return self.aware.isoformat(timespec="seconds")


@dataclass(frozen=True)
class DateResolver:
    default_timezone: tzinfo
    timezone_lookup: TimezoneLookup | None = None
    tolerance: timedelta = DEFAULT_TOLERANCE

    def resolve(self, inputs: DateInputs, now: datetime) -> ResolvedDate | None:
        """Return the capture date, or None if no usable date exists.

        ``now`` bounds plausible dates; pass it in so results are reproducible.
        """
        sidecar = _plausible_instant(inputs.sidecar_utc, now)
        video = _plausible_instant(inputs.video_utc, now)
        exif = inputs.exif_local
        if exif is not None and not _plausible_local(exif, now):
            exif = None

        if exif is not None:
            if sidecar is None:
                return self._from_local(exif, inputs, DateSource.EXIF, derived=None)
            difference = exif - sidecar.astimezone(UTC).replace(tzinfo=None)
            if abs(difference) <= self.tolerance:
                return self._from_local(exif, inputs, DateSource.EXIF, derived=difference)
            # Too far apart to be a time zone: the date was edited in Google Photos.
            return self._from_instant(sidecar, inputs, DateSource.SIDECAR)

        if sidecar is not None:
            return self._from_instant(sidecar, inputs, DateSource.SIDECAR)
        if video is not None:
            return self._from_instant(video, inputs, DateSource.VIDEO)

        named = inputs.filename_date
        if named is not None:
            if named.is_utc:
                instant = named.value.replace(tzinfo=UTC)
                if _plausible_instant(instant, now):
                    return self._from_instant(instant, inputs, DateSource.FILENAME)
            elif _plausible_local(named.value, now):
                return self._from_local(named.value, inputs, DateSource.FILENAME, derived=None)
        return None

    def _from_local(
        self,
        local: datetime,
        inputs: DateInputs,
        source: DateSource,
        derived: timedelta | None,
    ) -> ResolvedDate:
        """Wall-clock time is known; find its offset."""
        if inputs.exif_offset is not None and _valid_offset(inputs.exif_offset):
            return ResolvedDate(local, inputs.exif_offset, source, OffsetSource.EXIF_OFFSET)
        zone = self._gps_zone(inputs)
        if zone is not None:
            return ResolvedDate(local, _local_offset(local, zone), source, OffsetSource.GPS)
        if derived is not None:
            rounded = _round_offset(derived)
            if _valid_offset(rounded):
                return ResolvedDate(local, rounded, source, OffsetSource.SIDECAR_DIFFERENCE)
        offset = _local_offset(local, self.default_timezone)
        return ResolvedDate(local, offset, source, OffsetSource.DEFAULT_TIMEZONE)

    def _from_instant(
        self, instant: datetime, inputs: DateInputs, source: DateSource
    ) -> ResolvedDate:
        """The moment is known; find the local wall-clock time."""
        if inputs.exif_offset is not None and _valid_offset(inputs.exif_offset):
            offset = inputs.exif_offset
            offset_source = OffsetSource.EXIF_OFFSET
        else:
            zone = self._gps_zone(inputs)
            if zone is not None:
                offset_source = OffsetSource.GPS
            else:
                zone = self.default_timezone
                offset_source = OffsetSource.DEFAULT_TIMEZONE
            offset = _instant_offset(instant, zone)
        local = (instant.astimezone(UTC) + offset).replace(tzinfo=None)
        return ResolvedDate(local, offset, source, offset_source)

    def _gps_zone(self, inputs: DateInputs) -> tzinfo | None:
        if inputs.gps is None or self.timezone_lookup is None:
            return None
        latitude, longitude = inputs.gps
        if latitude == 0 and longitude == 0:
            return None  # Takeout writes 0,0 when there is no location
        return self.timezone_lookup(latitude, longitude)


def _fixed(offset: timedelta) -> tzinfo:
    return timezone(offset)


def _local_offset(local: datetime, zone: tzinfo) -> timedelta:
    offset = local.replace(tzinfo=zone).utcoffset()
    if offset is None:
        raise ValueError(f"time zone {zone!r} has no UTC offset")
    return offset


def _instant_offset(instant: datetime, zone: tzinfo) -> timedelta:
    offset = instant.astimezone(zone).utcoffset()
    if offset is None:
        raise ValueError(f"time zone {zone!r} has no UTC offset")
    return offset


def _round_offset(difference: timedelta) -> timedelta:
    steps = round(difference / _OFFSET_STEP)
    return steps * _OFFSET_STEP


def _valid_offset(offset: timedelta) -> bool:
    return _MIN_OFFSET <= offset <= _MAX_OFFSET


def _plausible_instant(value: datetime | None, now: datetime) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("instants must be timezone-aware")
    if value in _UNKNOWN_INSTANTS:
        return None
    if not _EARLIEST <= value <= now + timedelta(days=1):
        return None  # e.g. a camera clock set in the future
    return value


def _plausible_local(value: datetime, now: datetime) -> bool:
    if value.tzinfo is not None:
        raise ValueError("local times must be naive")
    latest = (now + timedelta(days=1)).astimezone(UTC).replace(tzinfo=None) + _MAX_OFFSET
    return _EARLIEST.replace(tzinfo=None) <= value <= latest
