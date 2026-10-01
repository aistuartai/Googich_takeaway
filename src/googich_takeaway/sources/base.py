"""What every source provides."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class SourceError(Exception):
    """The source cannot be listed or read."""


class TransientSourceError(SourceError):
    """A failure worth retrying: network trouble, rate limiting, server errors."""


@dataclass(frozen=True)
class RemoteFile:
    file_id: str
    """Stable identifier at the source (Drive file ID, or path for local files)."""
    name: str
    size: int
    modified: datetime
    sha256: str | None
    md5: str | None
    link: str | None = None
    """Where a person can open the file, for guided cleanup."""

    @property
    def fingerprint(self) -> str:
        """Changes whenever the file's content changes."""
        return self.sha256 or self.md5 or f"{self.size}:{self.modified.isoformat()}"


class Source(Protocol):
    name: str

    def list_archives(self) -> list[RemoteFile]: ...

    def read(self, file: RemoteFile, start: int = 0) -> Iterator[bytes]:
        """Yield the file's bytes from offset ``start``."""
        ...
