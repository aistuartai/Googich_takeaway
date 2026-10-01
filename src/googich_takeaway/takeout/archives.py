"""Streaming entries out of Takeout archives without extracting them.

Nothing is ever written to disk, so archive paths cannot escape a target directory and only
regular files are read. Each entry's stream is valid only until the next entry is requested.
"""

import re
import tarfile
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

_READ_ERRORS = (zipfile.BadZipFile, tarfile.TarError, zlib.error, EOFError, OSError)


class ArchiveError(Exception):
    """The archive cannot be read."""


class Readable(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class _GuardedStream:
    """Turns low-level read errors (bad CRC, truncated data) into ArchiveError."""

    def __init__(self, stream: IO[bytes], archive: str, entry: str) -> None:
        self._stream = stream
        self._where = f"{archive}: {entry}"

    def read(self, size: int = -1, /) -> bytes:
        try:
            return self._stream.read(size)
        except _READ_ERRORS as error:
            raise ArchiveError(f"{self._where}: {error}") from error


@dataclass(frozen=True)
class ArchiveEntry:
    path: str
    """Path inside the archive, with ``/`` separators."""
    size: int
    """Uncompressed size in bytes."""
    stream: Readable


def archive_format(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith((".tgz", ".tar.gz")):
        return "tgz"
    raise ArchiveError(f"not a .zip, .tgz or .tar.gz archive: {path.name}")


def iter_entries(path: Path) -> Iterator[ArchiveEntry]:
    """Yield every regular file in a zip or tgz archive, in archive order."""
    kind = archive_format(path)
    try:
        if kind == "zip":
            yield from _iter_zip(path)
        else:
            yield from _iter_tgz(path)
    except _READ_ERRORS as error:
        raise ArchiveError(f"{path.name}: {error}") from error


def _iter_zip(path: Path) -> Iterator[ArchiveEntry]:
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            with archive.open(info) as stream:
                guarded = _GuardedStream(stream, path.name, info.filename)
                yield ArchiveEntry(info.filename, info.file_size, guarded)


def _iter_tgz(path: Path) -> Iterator[ArchiveEntry]:
    # Stream mode ("r|gz") reads sequentially, which suits network shares; no seeking.
    with tarfile.open(path, mode="r|gz") as archive:
        for member in archive:
            if not member.isfile():
                continue  # directories, links and devices are never followed
            stream = archive.extractfile(member)
            if stream is None:
                continue
            with stream:
                guarded = _GuardedStream(stream, path.name, member.name)
                yield ArchiveEntry(member.name, member.size, guarded)


_PART_NAME = re.compile(
    r"^takeout-(?P<export>\d{8}T\d{6}Z)(?:-\d+)*-(?P<part>\d{3})\.(?:zip|tgz|tar\.gz)$",
    re.IGNORECASE,
)


def group_exports(paths: list[Path]) -> dict[str, list[Path]]:
    """Group archive parts by export, parts in order.

    Takeout names parts ``takeout-<timestamp>-001.zip``, ``-002.zip`` and so on. Archives with
    other names are treated as exports of their own, keyed by file name.
    """
    groups: dict[str, list[tuple[int, Path]]] = {}
    for path in paths:
        match = _PART_NAME.match(path.name)
        key, part = (match["export"], int(match["part"])) if match else (path.name, 0)
        groups.setdefault(key, []).append((part, path))
    return {key: [p for _, p in sorted(parts)] for key, parts in sorted(groups.items())}
