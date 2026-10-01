"""Streaming entries out of Takeout archives without extracting them.

Nothing is ever written to disk, so archive paths cannot escape a target directory and only
regular files are read. Each entry's stream is valid only until the next entry is requested.
"""

import re
import tarfile
import zipfile
import zlib
from collections.abc import Iterator, Sequence
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


class Named(Protocol):
    @property
    def name(self) -> str: ...


class OpenableArchive(Named, Protocol):
    def open(self) -> IO[bytes]: ...


ArchiveSource = Path | OpenableArchive
"""A local path, or an archive in a download location (``locations.StoredFile``)."""


def archive_format(path: Named) -> str:
    name = path.name.lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith((".tgz", ".tar.gz")):
        return "tgz"
    raise ArchiveError(f"not a .zip, .tgz or .tar.gz archive: {path.name}")


def _open(source: ArchiveSource) -> IO[bytes]:
    if isinstance(source, Path):
        return source.open("rb")
    return source.open()


def iter_entries(source: ArchiveSource) -> Iterator[ArchiveEntry]:
    """Yield every regular file in a zip or tgz archive, in archive order."""
    kind = archive_format(source)
    try:
        with _open(source) as handle:
            if kind == "zip":
                yield from _iter_zip(handle, source.name)
            else:
                yield from _iter_tgz(handle, source.name)
    except _READ_ERRORS as error:
        raise ArchiveError(f"{source.name}: {error}") from error


def _iter_zip(handle: IO[bytes], name: str) -> Iterator[ArchiveEntry]:
    # Zip needs random access to its central directory; local files and SMB handles both seek.
    with zipfile.ZipFile(handle) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            with archive.open(info) as stream:
                guarded = _GuardedStream(stream, name, info.filename)
                yield ArchiveEntry(info.filename, info.file_size, guarded)


def _iter_tgz(handle: IO[bytes], name: str) -> Iterator[ArchiveEntry]:
    # Stream mode ("r|gz") reads sequentially, which suits network shares; no seeking.
    with tarfile.open(fileobj=handle, mode="r|gz") as archive:
        for member in archive:
            if not member.isfile():
                continue  # directories, links and devices are never followed
            stream = archive.extractfile(member)
            if stream is None:
                continue
            with stream:
                guarded = _GuardedStream(stream, name, member.name)
                yield ArchiveEntry(member.name, member.size, guarded)


_PART_NAME = re.compile(
    r"^takeout-(?P<export>\d{8}T\d{6}Z)(?:-\d+)*-(?P<part>\d{3})\.(?:zip|tgz|tar\.gz)$",
    re.IGNORECASE,
)


def group_exports[N: Named](paths: Sequence[N]) -> dict[str, list[N]]:
    """Group archive parts by export, parts in order.

    Takeout names parts ``takeout-<timestamp>-001.zip``, ``-002.zip`` and so on. Archives with
    other names are treated as exports of their own, keyed by file name.
    """
    groups: dict[str, list[tuple[int, str, N]]] = {}
    for path in paths:
        match = _PART_NAME.match(path.name)
        key, part = (match["export"], int(match["part"])) if match else (path.name, 0)
        groups.setdefault(key, []).append((part, path.name, path))
    return {
        key: [p for _, _, p in sorted(parts, key=lambda t: (t[0], t[1]))]
        for key, parts in sorted(groups.items())
    }
