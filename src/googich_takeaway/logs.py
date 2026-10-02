"""Application logs: JSON lines on disk, a recent-history buffer for the web viewer, redaction.

- Every record is written as one JSON object per line to ``logs/googich.log`` beside the state
  database, rotated by size with a fixed number of old files kept.
- The most recent records are also kept in memory so the web viewer can show and filter them
  without reading files, and a live tail can poll for anything newer.
- Old log files are deleted once their newest line is older than the retention period (90 days
  unless changed in Settings). Run history is never deleted.
- A redaction filter runs on every record before it is stored anywhere. The app never logs secrets
  on purpose; this is the second line of defence for messages from libraries or mistakes.
"""

import json
import logging
import logging.handlers
import os
import re
import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

LOG_FILE_BYTES = 5 * 1024 * 1024
LOG_FILES_KEPT = 5
DEFAULT_RETENTION_DAYS = 90
BUFFER_RECORDS = 5000

_REDACTIONS = [
    # Authorization headers and bearer tokens.
    (
        re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)(bearer\s+)?[^\s'\",}]+"),
        r"\1\2[redacted]",
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [redacted]"),
    # Immich API key header.
    (re.compile(r"(?i)(x-api-key['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+"), r"\1[redacted]"),
    # PEM private keys (service account keys).
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        "[redacted private key]",
    ),
    (re.compile(r"(?i)(\"private_key\"\s*:\s*\")[^\"]*"), r"\1[redacted]"),
    # Passwords and tokens inside URLs: scheme://user:secret@host and scheme://token@host.
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^\s/@:]+(:[^\s/@]*)?@"), r"\1[redacted]@"),
    # Query parameters that commonly carry secrets.
    (
        re.compile(r"(?i)([?&](?:access_token|token|key|apikey|api_key|password|secret)=)[^&\s]+"),
        r"\1[redacted]",
    ),
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


@dataclass(frozen=True)
class LogEntry:
    seq: int
    time: datetime
    level: str
    logger: str
    message: str

    def as_json(self) -> str:
        return json.dumps(
            {
                "time": self.time.isoformat(timespec="milliseconds"),
                "level": self.level,
                "logger": self.logger,
                "message": self.message,
            },
            ensure_ascii=False,
        )


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "googich_redacted", False):
            return True  # the same filter sits on every handler: redact each record once
        record.googich_redacted = True
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
            record.exc_text = None
        record.msg = redact(message)
        record.args = None
        return True


QUIET_PATHS = ("/status", "/activity", "/logs/tail", "/updates/banner", "/static/", "/healthz")
"""Requests the pages make by themselves every few seconds, and static files."""


class _QuietAccessFilter(logging.Filter):
    """Drops access-log lines for polling and static requests that succeeded: one every few
    seconds per open page, saying nothing. Failed ones (status 400 and up) are kept, and so is
    every page opened and every form sent."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        path, status = str(args[2]), args[4]
        if isinstance(status, int) and status >= 400:
            return True
        return not path.startswith(QUIET_PATHS)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "time": datetime.fromtimestamp(record.created, UTC).isoformat(
                    timespec="milliseconds"
                ),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            },
            ensure_ascii=False,
        )


class _OwnerOnlyRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Every log file, including ones created at rotation, is readable only by the owner."""

    def _open(self):  # type: ignore[no-untyped-def]
        descriptor = os.open(self.baseFilename, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        return os.fdopen(descriptor, self.mode, encoding=self.encoding, errors=self.errors)


class LogBuffer(logging.Handler):
    """Keeps recent records in memory for the web viewer."""

    def __init__(self, capacity: int = BUFFER_RECORDS) -> None:
        super().__init__()
        self._entries: deque[LogEntry] = deque(maxlen=capacity)
        self._seq = 0
        self._guard = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        with self._guard:
            self._seq += 1
            self._entries.append(
                LogEntry(
                    seq=self._seq,
                    time=datetime.fromtimestamp(record.created, UTC),
                    level=record.levelname,
                    logger=record.name,
                    message=record.getMessage(),
                )
            )

    def query(
        self,
        level: str = "INFO",
        text: str = "",
        after: int = 0,
        limit: int = 500,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[LogEntry]:
        """Newest last. ``level`` is the minimum level; ``text`` matches case-insensitively."""
        minimum = logging.getLevelName(level.upper())
        if not isinstance(minimum, int):
            minimum = logging.INFO
        needle = text.casefold()
        found: list[LogEntry] = []
        with self._guard:
            # Newest first, stopping at ``after``: the live tail asks every few seconds and
            # usually needs only the last few lines, not a copy of all of them.
            for e in reversed(self._entries):
                if e.seq <= after or len(found) >= limit:
                    break
                if (
                    (since is None or e.time >= since)
                    and (until is None or e.time <= until)
                    and logging.getLevelName(e.level) >= minimum
                    and (
                        not needle
                        or needle in e.message.casefold()
                        or needle in e.logger.casefold()
                    )
                ):
                    found.append(e)
        found.reverse()
        return found


@dataclass(frozen=True)
class Logs:
    buffer: LogBuffer
    directory: Path

    @property
    def current_file(self) -> Path:
        return self.directory / "googich.log"

    def files(self) -> list[Path]:
        """Log files, newest first."""
        if not self.directory.is_dir():
            return []
        found = [p for p in self.directory.iterdir() if p.name.startswith("googich.log")]
        return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def prune_files(directory: Path, before: datetime) -> list[Path]:
    """Delete rotated log files last written before ``before``; the current file is kept."""
    if not directory.is_dir():
        return []
    removed = []
    for path in directory.iterdir():
        if not path.name.startswith("googich.log.") or not path.is_file():
            continue
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, UTC) < before:
                path.unlink()
                removed.append(path)
        except OSError:
            continue  # rotated or removed meanwhile
    return removed


def setup_logging(state_path: Path, level: int = logging.INFO) -> Logs:
    """Send the app's logs to rotating JSON files and the in-memory buffer, redacted."""
    directory = state_path.parent / "logs"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    redactor = _RedactingFilter()
    file_handler = _OwnerOnlyRotatingFileHandler(
        directory / "googich.log", maxBytes=LOG_FILE_BYTES, backupCount=LOG_FILES_KEPT
    )
    file_handler.setFormatter(_JsonFormatter())
    file_handler.addFilter(redactor)
    buffer = LogBuffer()
    buffer.addFilter(redactor)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    console.addFilter(redactor)
    root = logging.getLogger()
    for handler in (file_handler, buffer, console):
        root.addHandler(handler)
    root.setLevel(level)
    # Libraries that log request URLs at INFO (httpx) are kept to warnings.
    for noisy in ("httpx", "httpcore", "apprise", "google"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").addFilter(_QuietAccessFilter())
    return Logs(buffer=buffer, directory=directory)
