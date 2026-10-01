from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from googich_takeaway.schedule import Mode, Schedule, next_run, parse_time

MEL = ZoneInfo("Australia/Melbourne")


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def test_off_has_no_next_run() -> None:
    assert next_run(Schedule(), utc(2026, 10, 1), MEL, None) is None


def test_daily_runs_at_local_time() -> None:
    schedule = Schedule(Mode.DAILY, at=time(3, 0))
    now = utc(2026, 10, 1, 10, 0)  # 20:00 in Melbourne (+10)
    assert next_run(schedule, now, MEL, None) == utc(2026, 10, 1, 17, 0)  # 03:00 on the 2nd


def test_daily_keeps_local_time_across_daylight_saving() -> None:
    schedule = Schedule(Mode.DAILY, at=time(3, 0))
    # Melbourne moves to +11 on 2026-10-04; 03:00 local is then 16:00 UTC the day before.
    assert next_run(schedule, utc(2026, 10, 4, 12, 0), MEL, None) == utc(2026, 10, 4, 16, 0)


def test_weekly_picks_the_right_day() -> None:
    schedule = Schedule(Mode.WEEKLY, at=time(2, 30), weekday=6)  # Sunday
    now = utc(2026, 10, 8, 0, 0)  # Thursday in Melbourne
    due = next_run(schedule, now, MEL, None)
    assert due is not None
    assert due.astimezone(MEL).strftime("%A %d %H:%M") == "Sunday 11 02:30"


def test_time_skipped_by_daylight_saving_runs_just_after() -> None:
    # 2026-10-04 02:00 jumps to 03:00 in Melbourne, so 02:30 does not exist that day.
    schedule = Schedule(Mode.WEEKLY, at=time(2, 30), weekday=6)
    due = next_run(schedule, utc(2026, 10, 1, 0, 0), MEL, None)
    assert due is not None
    assert due.astimezone(MEL).strftime("%A %d %H:%M") == "Sunday 04 03:30"


def test_hourly_counts_from_the_last_run() -> None:
    schedule = Schedule(Mode.HOURLY, every_hours=6)
    now = utc(2026, 10, 1, 12, 0)
    assert next_run(schedule, now, MEL, None) == now  # never ran: due now
    assert next_run(schedule, now, MEL, utc(2026, 10, 1, 9, 0)) == utc(2026, 10, 1, 15, 0)
    assert next_run(schedule, now, MEL, utc(2026, 10, 1, 1, 0)) == now  # overdue


def test_a_run_already_made_today_is_not_repeated() -> None:
    schedule = Schedule(Mode.DAILY, at=time(3, 0))
    now = utc(2026, 9, 30, 17, 0, 30)  # 03:00:30 local on the 1st, just after the slot
    last = utc(2026, 9, 30, 17, 0, 0)
    assert next_run(schedule, now, MEL, last) == utc(2026, 10, 1, 17, 0)  # still +10 on the 2nd


def test_describe() -> None:
    assert Schedule(Mode.WEEKLY, at=time(2, 30), weekday=0).describe() == "Every Monday at 02:30"
    assert Schedule(Mode.HOURLY, every_hours=1).describe() == "Every hour"


def test_parse_time() -> None:
    assert parse_time(" 03:15 ") == time(3, 15)
    for bad in ("3pm", "25:00", "03"):
        with pytest.raises(ValueError, match="24-hour"):
            parse_time(bad)
