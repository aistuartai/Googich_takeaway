"""Reminders about Google Takeout, which the app cannot see or control.

Google offers no API for Takeout: the app cannot start an export, or tell whether a scheduled
export is still set up. Two things show when the schedule needs attention:

- **No new export for a while.** A scheduled export arrives every month or every two months. If
  nothing new has been downloaded for 10 days longer than that (40 or 70 days), the schedule has
  probably ended, or an export failed.
- **The schedule ends soon.** Takeout's schedule runs for one year. If the user notes when they
  set it up, the app reminds them three weeks before it ends.

Each reminder is shown on the dashboard, and sent as a notification once.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from googich_takeaway.config import Config
from googich_takeaway.notify import Message, Outcome
from googich_takeaway.state import State

log = logging.getLogger("googich.reminders")

STALE_GRACE = timedelta(days=10)
SCHEDULE_LENGTH = timedelta(days=365)
ENDING_WARNING = timedelta(days=21)
_SENT = "reminder.sent"


@dataclass(frozen=True)
class Reminder:
    key: str
    """Identifies this reminder, so its notification is sent once."""
    title: str
    lines: list[str]


def takeout_reminders(config: Config, state: State, now: datetime) -> list[Reminder]:
    found = []
    _, latest = state.download_dates()
    months = config.takeout_months()
    stale_after = timedelta(days=30 * months) + STALE_GRACE
    if latest is not None and config.sources() and now - latest >= stale_after:
        days = (now - latest).days
        every = "every month" if months == 1 else "every two months"
        found.append(
            Reminder(
                f"stale:{latest.date().isoformat()}",
                "No new Takeout export for a while",
                [
                    f"The last archive was downloaded {days} days ago, on "
                    f"{latest.date():%d %B %Y}. Scheduled exports arrive {every}.",
                    "The schedule may have ended (it runs for one year) or an export failed. "
                    "Check takeout.google.com, and set up a new scheduled export if needed.",
                ],
            )
        )
    started = config.takeout_schedule_started()
    if started is not None:
        ends = started + SCHEDULE_LENGTH
        left = ends - now.date()
        if timedelta(0) <= left <= ENDING_WARNING:
            found.append(
                Reminder(
                    f"ending:{ends.isoformat()}",
                    "Your Takeout schedule ends soon",
                    [
                        f"It was set up on {started:%d %B %Y}, and scheduled exports run for one "
                        f"year: the last one is due around {ends:%d %B %Y}.",
                        "Set up a new scheduled export at takeout.google.com, then note the new "
                        "date on the Schedule page.",
                    ],
                )
            )
    return found


def send_reminders(config: Config, state: State, now: datetime) -> int:
    """Notify each current reminder once. Returns how many were sent."""
    sent = set(filter(None, (state.get_setting(_SENT) or "").split(",")))
    current = takeout_reminders(config, state, now)
    new = [r for r in current if r.key not in sent]
    if not new:
        return 0
    notifier = config.notifier()
    for reminder in new:
        log.warning("Reminder: %s", reminder.title)
        notifier.send(Message(Outcome.REMINDER, reminder.title, reminder.lines))
    # Keep only keys that still apply, so the list stays short.
    keep = {r.key for r in current}
    state.set_setting(_SENT, ",".join(sorted(keep)), now)
    return len(new)


def schedule_end(started: date | None) -> date | None:
    return started + SCHEDULE_LENGTH if started else None
