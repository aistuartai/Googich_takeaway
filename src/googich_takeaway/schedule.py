"""When the next scheduled run is due, and when to pause.

Schedules are simple and deterministic: off, every N hours, daily at a time, weekly on a day at
a time, or following the Takeout schedule, in the configured time zone (so 03:00 stays 03:00 across
daylight saving changes).

Following the Takeout schedule: Takeout's scheduled export runs every month or every two months
for a year, starting on the day it was set up. Each export can take hours or days to arrive, so
from each expected export day the app tries every day (or every few days, as chosen) until the
export has arrived, for up to two weeks (or as chosen). Nothing else runs, unless the user also
chooses a weekly run between exports, in case one comes late or the dates were noted wrongly.
"""

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

DEFAULT_PAUSE_AFTER = 3
DEFAULT_WAIT_DAYS = 14
DEFAULT_RETRY_DAYS = 1
TAKEOUT_FREQUENCIES = {1: "Every month", 2: "Every 2 months"}
"""Takeout's scheduled export choices, in months. Both run for one year."""
DEFAULT_TAKEOUT_MONTHS = 2
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class Mode(StrEnum):
    OFF = "off"
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    TAKEOUT = "takeout"


@dataclass(frozen=True)
class Schedule:
    mode: Mode = Mode.OFF
    at: time = time(3, 0)
    """Local time of day, for daily, weekly and following Takeout."""
    weekday: int = 6
    """0 = Monday … 6 = Sunday, for weekly."""
    every_hours: int = 24
    """For hourly."""
    pause_after: int = DEFAULT_PAUSE_AFTER
    """Consecutive failed scheduled runs before the schedule pauses itself."""
    takeout_started: date | None = None
    """When the Takeout schedule was set up, for following it."""
    takeout_months: int = DEFAULT_TAKEOUT_MONTHS
    """How often Takeout exports: every 1 or 2 months."""
    retry_days: int = DEFAULT_RETRY_DAYS
    """Following Takeout: days between tries while waiting for an export."""
    wait_days: int = DEFAULT_WAIT_DAYS
    """Following Takeout: how long to keep trying after each expected export day."""
    fallback_weekly: bool = False
    """Following Takeout: also run weekly between exports (off unless chosen)."""
    fallback_weekday: int = 6
    """Following Takeout: the day of those weekly runs."""
    latest_download: date | None = None
    """Following Takeout: the local day an archive was last downloaded, so trying can stop once
    the export has arrived. Not a setting: filled in from the download history."""

    def describe(self) -> str:
        if self.mode is Mode.OFF:
            return "Off"
        if self.mode is Mode.HOURLY:
            return "Every hour" if self.every_hours == 1 else f"Every {self.every_hours} hours"
        if self.mode is Mode.DAILY:
            return f"Daily at {self.at:%H:%M}"
        if self.mode is Mode.TAKEOUT:
            again = "every day" if self.retry_days == 1 else f"every {self.retry_days} days"
            between = (
                f", plus every {WEEKDAYS[self.fallback_weekday]} between exports"
                if self.fallback_weekly
                else ""
            )
            return (
                f"Following Takeout: on each expected export day at {self.at:%H:%M}, trying "
                f"again {again} until it arrives{between}"
            )
        return f"Every {WEEKDAYS[self.weekday]} at {self.at:%H:%M}"

    def runs_on(self, day: date) -> bool:
        """For daily, weekly and Takeout schedules: whether a run is due on this local day."""
        if self.mode is Mode.DAILY:
            return True
        if self.mode is Mode.WEEKLY:
            return day.weekday() == self.weekday
        if self.mode is Mode.TAKEOUT:
            if self.waiting_on(day) is not None:
                return True
            return self.fallback_weekly and day.weekday() == self.fallback_weekday
        return False

    def exports(self) -> list[date]:
        return takeout_exports(self.takeout_started, self.takeout_months)

    def waiting_on(self, day: date) -> date | None:
        """Following Takeout: the expected export day being waited for on ``day``, if a try is
        due then. None once that export has arrived, or between waits, or on a skipped day."""
        for expected in self.exports():
            waited = (day - expected).days
            if not 0 <= waited < self.wait_days:
                continue
            if self.latest_download is not None and self.latest_download >= expected:
                return None  # it arrived
            return expected if waited % self.retry_days == 0 else None
        return None

    def check_days(self, expected: date) -> list[date]:
        """The days the app tries for one expected export, if it does not arrive sooner."""
        return [expected + timedelta(days=n) for n in range(0, self.wait_days, self.retry_days)]

    def takeout_status(self, today: date) -> str:
        """Following Takeout, in a sentence: what the schedule is doing now."""
        exports = self.exports()
        between = (
            f"it runs every {WEEKDAYS[self.fallback_weekday]}"
            if self.fallback_weekly
            else "nothing runs"
        )
        if not exports:
            return "No Takeout date is noted, so it can only run weekly, if that is switched on."
        for expected in exports:
            if expected <= today < expected + timedelta(days=self.wait_days):
                last = self.check_days(expected)[-1]
                if self.latest_download is not None and self.latest_download >= expected:
                    return (
                        f"The export expected on {expected:%d %B} has arrived. Until the next "
                        f"one, {between}."
                    )
                return (
                    f"Waiting for the export expected on {expected:%d %B}: trying until it "
                    f"arrives, at the latest on {last:%d %B}."
                )
        upcoming = next_takeout_export(self.takeout_started, today, self.takeout_months)
        if upcoming is None:
            return (
                f"The Takeout schedule ended after its last export on {exports[-1]:%d %B %Y}. "
                "Set up a new one at takeout.google.com and note its date. Meanwhile "
                f"{between}."
            )
        return f"Next export expected on {upcoming:%d %B %Y}. Until then {between}."


def next_run(
    schedule: Schedule, now: datetime, zone: ZoneInfo, last_start: datetime | None
) -> datetime | None:
    """UTC time the next scheduled run is due, or None when nothing is scheduled."""
    if schedule.mode is Mode.OFF:
        return None
    if schedule.mode is Mode.HOURLY:
        if last_start is None:
            return now
        return max(now, last_start + timedelta(hours=schedule.every_hours))
    local_now = now.astimezone(zone)
    # A week covers daily and weekly; following Takeout may wait up to two months between
    # exports, or until a Takeout schedule set up within the next year starts.
    horizon = 8 if schedule.mode is not Mode.TAKEOUT else 800
    for days in range(0, horizon):
        day = (local_now + timedelta(days=days)).date()
        if not schedule.runs_on(day):
            continue
        candidate = datetime.combine(day, schedule.at, tzinfo=zone).astimezone(UTC)
        if candidate > now and (last_start is None or candidate > last_start):
            return candidate
    return None  # following Takeout with no exports left and no weekly runs


def parse_time(value: str) -> time:
    try:
        hours, minutes = value.strip().split(":")
        return time(int(hours), int(minutes))
    except ValueError:
        raise ValueError("Use a 24-hour time such as 03:00.") from None


def takeout_exports(started: date | None, months: int = DEFAULT_TAKEOUT_MONTHS) -> list[date]:
    """The days a Takeout schedule set up on ``started`` is expected to make its exports: a year
    of them, every ``months`` months."""
    if started is None:
        return []
    return [_add_months(started, months * n) for n in range(12 // months)]


def next_takeout_export(
    started: date | None, today: date, months: int = DEFAULT_TAKEOUT_MONTHS
) -> date | None:
    return next((day for day in takeout_exports(started, months) if day >= today), None)


def upcoming_runs(
    schedule: Schedule, now: datetime, zone: ZoneInfo, last_start: datetime | None, count: int = 5
) -> list[datetime]:
    """The next few scheduled runs, assuming no export arrives in the meantime."""
    found: list[datetime] = []
    after = last_start
    moment = now
    while len(found) < count:
        due = next_run(schedule, moment, zone, after)
        if due is None:
            break
        found.append(due)
        after = due
        moment = max(moment, due)
    return found


def _add_months(day: date, months: int) -> date:
    year, month = divmod(day.month - 1 + months, 12)
    year, month = day.year + year, month + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))
