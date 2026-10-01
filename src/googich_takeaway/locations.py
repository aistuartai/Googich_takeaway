"""Where downloaded archives are kept: a local folder or an SMB share.

Everything that touches the download folder goes through a ``Location``, so downloading, reading
archives, uploading and cleanup work the same on a local disk and on a network share. The SMB
implementation talks SMB itself (``smbprotocol``), so it needs no mounts and no special
privileges, which matters in unprivileged containers.

Archives are handed around as ``StoredFile`` objects: a name, a size and a way to open them.
"""

import contextlib
import os
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Protocol

ARCHIVE_SUFFIXES = (".zip", ".tgz", ".tar.gz")


class LocationError(Exception):
    """The location cannot be used; the message is shown to the user."""


@dataclass(frozen=True)
class FileInfo:
    name: str
    size: int
    modified: datetime


class Location(Protocol):
    def describe(self) -> str: ...

    def list(self) -> list[FileInfo]: ...

    def size(self, name: str) -> int | None: ...

    def open_read(self, name: str) -> IO[bytes]: ...

    def open_append(self, name: str, create_owner_only: bool = True) -> IO[bytes]: ...

    def write_small(self, name: str, data: bytes) -> None: ...

    def read_small(self, name: str) -> bytes | None: ...

    def replace(self, source: str, target: str) -> None: ...

    def delete(self, name: str) -> None: ...

    def free_space(self) -> int | None: ...

    def prepare(self) -> None:
        """Create the folder if needed and check it can be written. Raises LocationError."""
        ...


@dataclass(frozen=True)
class StoredFile:
    """An archive in a location: what the scanner and uploader read from."""

    location: Location
    name: str
    size: int

    def open(self) -> IO[bytes]:
        return self.location.open_read(self.name)

    def __lt__(self, other: "StoredFile") -> bool:
        return self.name < other.name


def archives(location: Location) -> list[StoredFile]:
    return sorted(
        StoredFile(location, f.name, f.size)
        for f in location.list()
        if f.name.lower().endswith(ARCHIVE_SUFFIXES)
    )


def _check_name(name: str) -> str:
    if not name or "/" in name or "\\" in name or name in (".", "..") or name.startswith("."):
        raise LocationError(f"refusing unsafe file name {name!r}")
    return name


class LocalLocation:
    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def describe(self) -> str:
        return str(self.folder)

    def _path(self, name: str) -> Path:
        return self.folder / _check_name(name)

    def prepare(self) -> None:
        try:
            self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as error:
            raise LocationError(f"Cannot create the download folder: {error.strerror}.") from None
        if not self.folder.is_dir():
            raise LocationError("The download folder path is not a folder.")
        probe = self.folder / f"googich-write-test-{uuid.uuid4().hex[:8]}"
        try:
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as error:
            raise LocationError(f"Cannot write to the download folder: {error.strerror}.") from None

    def list(self) -> list[FileInfo]:
        if not self.folder.is_dir():
            return []
        found = []
        for path in self.folder.iterdir():
            if path.is_file():
                info = path.stat()
                found.append(
                    FileInfo(path.name, info.st_size, datetime.fromtimestamp(info.st_mtime, UTC))
                )
        return sorted(found, key=lambda f: f.name)

    def size(self, name: str) -> int | None:
        path = self._path(name)
        return path.stat().st_size if path.is_file() else None

    def open_read(self, name: str) -> IO[bytes]:
        return self._path(name).open("rb")

    def open_append(self, name: str, create_owner_only: bool = True) -> IO[bytes]:
        path = self._path(name)
        if not path.exists() and create_owner_only:
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        return path.open("ab")

    def write_small(self, name: str, data: bytes) -> None:
        path = self._path(name)
        descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)

    def read_small(self, name: str) -> bytes | None:
        path = self._path(name)
        try:
            return path.read_bytes()
        except OSError:
            return None

    def replace(self, source: str, target: str) -> None:
        self._path(source).replace(self._path(target))
        descriptor = os.open(self.folder, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)

    def free_space(self) -> int | None:
        return shutil.disk_usage(self.folder).free


@dataclass(frozen=True)
class SmbSettings:
    server: str
    share: str
    folder: str
    username: str
    password: str
    port: int = 445
    domain: str = ""

    def unc(self) -> str:
        parts = [p for p in self.folder.replace("/", "\\").split("\\") if p]
        return "\\\\" + "\\".join([self.server, self.share, *parts])


class SmbLocation:
    """A folder on an SMB share. Credentials stay in this object, never in a global cache."""

    def __init__(self, settings: SmbSettings, client: Any = None) -> None:
        import smbclient  # imported lazily: only SMB users need it loaded

        self._smb = client or smbclient
        self._settings = settings
        self._cache: dict[str, Any] = {}
        user = settings.username
        if settings.domain and "\\" not in user and "@" not in user:
            user = f"{settings.domain}\\{user}"
        self._auth = {
            "username": user,
            "password": settings.password,
            "port": settings.port,
            "connection_cache": self._cache,
        }

    def describe(self) -> str:
        return self._settings.unc()

    def _path(self, name: str) -> str:
        return self._settings.unc() + "\\" + _check_name(name)

    @contextmanager
    def _errors(self, action: str) -> Iterator[None]:
        try:
            yield
        except LocationError:
            raise
        except Exception as error:  # smbprotocol raises many exception types
            raise LocationError(f"SMB share: cannot {action} ({_smb_reason(error)}).") from None

    def prepare(self) -> None:
        with self._errors("open or create the folder"):
            self._smb.makedirs(self._settings.unc(), exist_ok=True, **self._auth)
        probe = f"googich-write-test-{uuid.uuid4().hex[:8]}"
        with self._errors("write a test file"):
            with self._smb.open_file(self._path(probe), mode="wb", **self._auth) as handle:
                handle.write(b"")
            self._smb.remove(self._path(probe), **self._auth)

    def list(self) -> list[FileInfo]:
        with self._errors("list the folder"):
            entries = list(self._smb.scandir(self._settings.unc(), **self._auth))
        found = []
        for entry in entries:
            if entry.is_file():
                info = entry.stat()
                found.append(
                    FileInfo(entry.name, info.st_size, datetime.fromtimestamp(info.st_mtime, UTC))
                )
        return sorted(found, key=lambda f: f.name)

    def size(self, name: str) -> int | None:
        try:
            return int(self._smb.stat(self._path(name), **self._auth).st_size)
        except FileNotFoundError:
            return None
        except Exception as error:
            if _is_not_found(error):
                return None
            raise LocationError(f"SMB share: cannot read {name} ({_smb_reason(error)}).") from None

    def open_read(self, name: str) -> IO[bytes]:
        with self._errors(f"open {name}"):
            handle: IO[bytes] = self._smb.open_file(self._path(name), mode="rb", **self._auth)
            return handle

    def open_append(self, name: str, create_owner_only: bool = True) -> IO[bytes]:
        with self._errors(f"write {name}"):
            handle: IO[bytes] = self._smb.open_file(self._path(name), mode="ab", **self._auth)
            return handle

    def write_small(self, name: str, data: bytes) -> None:
        with (
            self._errors(f"write {name}"),
            self._smb.open_file(self._path(name), mode="wb", **self._auth) as handle,
        ):
            handle.write(data)

    def read_small(self, name: str) -> bytes | None:
        try:
            with self._smb.open_file(self._path(name), mode="rb", **self._auth) as handle:
                data: bytes = handle.read()
                return data
        except Exception:  # missing or unreadable: treated the same
            return None

    def replace(self, source: str, target: str) -> None:
        with self._errors(f"rename {source}"):
            self._smb.replace(self._path(source), self._path(target), **self._auth)

    def delete(self, name: str) -> None:
        try:
            self._smb.remove(self._path(name), **self._auth)
        except FileNotFoundError:
            return
        except Exception as error:
            if _is_not_found(error):
                return
            reason = _smb_reason(error)
            raise LocationError(f"SMB share: cannot delete {name} ({reason}).") from None

    def free_space(self) -> int | None:
        """Space available to this user, or None if the server does not say."""
        try:
            volume = self._smb.stat_volume(self._settings.unc(), **self._auth)
            return int(volume.caller_available_size)
        except Exception:  # not every server reports it; never fail a download over it
            return None

    def close(self) -> None:
        for connection in list(self._cache.values()):
            with contextlib.suppress(Exception):  # best effort on shutdown
                connection.disconnect()
        self._cache.clear()


def _is_not_found(error: Exception) -> bool:
    return (
        type(error).__name__ in ("SMBOSError", "FileNotFoundError")
        and getattr(error, "errno", None) in (2, None)
        and "No such file" in str(error)
    )


def _smb_reason(error: Exception) -> str:
    """A short reason without echoing credentials (smbprotocol messages never include them)."""
    text = str(error).splitlines()[0] if str(error) else type(error).__name__
    return text[:200]
