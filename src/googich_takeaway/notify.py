"""Run notifications through Apprise.

Apprise URLs often carry tokens or passwords (``ntfys://token@host/topic``, ``mailtos://user:pass@``)
so they are stored sealed and are never logged. Sending is best effort: a notification that
cannot be delivered is logged as a warning and never fails the run.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

import apprise

log = logging.getLogger("googich.notify")


class Outcome(StrEnum):
    SUCCESS = "success"
    NO_NEW_DATA = "no-new-data"
    FAILED = "failed"
    PAUSED = "paused"


DEFAULT_OUTCOMES = frozenset({Outcome.SUCCESS, Outcome.FAILED, Outcome.PAUSED})

_TYPES = {
    Outcome.SUCCESS: apprise.NotifyType.SUCCESS,
    Outcome.NO_NEW_DATA: apprise.NotifyType.INFO,
    Outcome.FAILED: apprise.NotifyType.FAILURE,
    Outcome.PAUSED: apprise.NotifyType.WARNING,
}


@dataclass(frozen=True)
class Message:
    outcome: Outcome
    title: str
    lines: list[str] = field(default_factory=list)

    @property
    def body(self) -> str:
        return "\n".join(self.lines) or self.title


def invalid_urls(urls: Iterable[str]) -> list[int]:
    """1-based line numbers of URLs Apprise does not recognise. Never echoes the URLs."""
    bad = []
    for number, url in enumerate(urls, start=1):
        if not apprise.Apprise().add(url):
            bad.append(number)
    return bad


class Notifier:
    def __init__(self, urls: list[str], outcomes: Iterable[Outcome] = DEFAULT_OUTCOMES) -> None:
        self._urls = [u for u in urls if u.strip()]
        self._outcomes = frozenset(outcomes)

    @property
    def configured(self) -> bool:
        return bool(self._urls)

    def send(self, message: Message, force: bool = False) -> bool:
        """Deliver ``message`` if its outcome is switched on (or ``force``). True if delivered."""
        if not self._urls or (not force and message.outcome not in self._outcomes):
            return False
        sender = apprise.Apprise()
        for url in self._urls:
            sender.add(url)
        try:
            delivered = bool(
                sender.notify(
                    title=f"Googich Takeaway: {message.title}",
                    body=message.body,
                    notify_type=_TYPES[message.outcome],
                )
            )
        except Exception:
            log.warning("Notification could not be sent (service error)")
            return False
        if not delivered:
            log.warning("Notification could not be delivered to at least one service")
        return delivered
