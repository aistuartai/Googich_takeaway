"""When the next scheduled run is due, and when to pause.

Schedules are simple and deterministic: off, every N hours, daily at a time, or weekly on a day at
a time, in the configured time zone (so 03:00 stays 03:00 across daylight saving changes).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

DEFAULT_PAUSE_AFTER = 3
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class Mode(StrEnum):
    OFF = "off"
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"


@dataclass(frozen=True)
class Schedule:
    mode: Mode = Mode.OFF
    at: time = time(3, 0)
    """Local time of day, for daily and weekly."""
    weekday: int = 6
    """0 = Monday … 6 = Sunday, for weekly."""
    every_hours: int = 24
    """For hourly."""
    pause_after: int = DEFAULT_PAUSE_AFTER
    """Consecutive failed scheduled runs before the schedule pauses itself."""

    def describe(self) -> str:
        if self.mode is Mode.OFF:
            return "Off"
        if self.mode is Mode.HOURLY:
            return "Every hour" if self.every_hours == 1 else f"Every {self.every_hours} hours"
        if self.mode is Mode.DAILY:
            return f"Daily at {self.at:%H:%M}"
        return f"Every {WEEKDAYS[self.weekday]} at {self.at:%H:%M}"


def next_run(
    schedule: Schedule, now: datetime, zone: ZoneInfo, last_start: datetime | None
) -> datetime | None:
    """UTC time the next scheduled run is due, or None when the schedule is off."""
    if schedule.mode is Mode.OFF:
        return None
    if schedule.mode is Mode.HOURLY:
        if last_start is None:
            return now
        return max(now, last_start + timedelta(hours=schedule.every_hours))
    local_now = now.astimezone(zone)
    for days in range(0, 8):
        day = (local_now + timedelta(days=days)).date()
        if schedule.mode is Mode.WEEKLY and day.weekday() != schedule.weekday:
            continue
        candidate = datetime.combine(day, schedule.at, tzinfo=zone).astimezone(UTC)
        if candidate > now and (last_start is None or candidate > last_start):
            return candidate
    return None  # unreachable for valid schedules


def parse_time(value: str) -> time:
    try:
        hours, minutes = value.strip().split(":")
        return time(int(hours), int(minutes))
    except ValueError:
        raise ValueError("Use a 24-hour time such as 03:00.") from None
