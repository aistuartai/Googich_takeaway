"""Run notifications through Apprise.

Apprise URLs often carry tokens or passwords (``ntfys://token@host/topic``, ``mailtos://user:pass@``)
so they are stored sealed and are never logged. Sending is best effort: a notification that
cannot be delivered is logged as a warning and never fails the run.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import parse_qs, urlsplit

import apprise

log = logging.getLogger("googich.notify")


class Outcome(StrEnum):
    SUCCESS = "success"
    NO_NEW_DATA = "no-new-data"
    FAILED = "failed"
    PAUSED = "paused"
    REMINDER = "reminder"
    """Not a run: a reminder about the Takeout schedule."""


DEFAULT_OUTCOMES = frozenset({Outcome.SUCCESS, Outcome.FAILED, Outcome.PAUSED, Outcome.REMINDER})

_TYPES = {
    Outcome.SUCCESS: apprise.NotifyType.SUCCESS,
    Outcome.NO_NEW_DATA: apprise.NotifyType.INFO,
    Outcome.FAILED: apprise.NotifyType.FAILURE,
    Outcome.PAUSED: apprise.NotifyType.WARNING,
    Outcome.REMINDER: apprise.NotifyType.WARNING,
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
        delivered, _ = deliver(self._urls, message)
        if not delivered:
            log.warning("Notification could not be delivered to at least one service")
        return delivered


def deliver(urls: list[str], message: Message) -> tuple[bool, list[str]]:
    """Send to every URL. Returns whether all succeeded, and the reasons Apprise gave if not.

    Apprise reports why a service refused (a wrong token, an unreachable server) only in its
    log, so those messages are collected while sending. They never contain the URL's secrets.
    """
    sender = apprise.Apprise()
    for url in urls:
        sender.add(url)
    reasons = _Reasons()
    source = logging.getLogger("apprise")
    source.addHandler(reasons)
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
        return False, ["the service failed unexpectedly; see Logs"]
    finally:
        source.removeHandler(reasons)
    return delivered, reasons.found


def service_name(url: str) -> str:
    """The kind of service a URL points at, such as ``HomeAssistant`` or ``ntfy``."""
    instance = apprise.Apprise.instantiate(url)
    return str(instance.service_name) if instance else "Unknown"


def delivery_note(url: str) -> str:
    """Where a delivered test went, when that is not obvious: Home Assistant shows messages in
    its own notifications unless the URL names a notify service, such as a phone."""
    if service_name(url) != "HomeAssistant":
        return ""
    parts = urlsplit(url.strip())
    after_token = [p for p in parts.path.split("/") if p][1:]
    query = parse_qs(parts.query)
    named = after_token or query.get("to") or query.get("targets")
    if named:
        return f" Home Assistant sent it through: {', '.join(named)}."
    return (
        " This URL names no notify service, so it is in Home Assistant's notifications (the "
        "bell). To send it to a phone, add /mobile_app_yourphone after the token."
    )


class _Reasons(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.found: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.found) < 5:
            self.found.append(record.getMessage()[:300])
