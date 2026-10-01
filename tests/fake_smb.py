"""An in-memory stand-in for the parts of ``smbclient`` the SMB location uses.

Paths are UNC strings (``\\\\server\\share\\folder\\file``). Every call checks the credentials it is
given, as a real server would.
"""

import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Built at runtime so nothing in the source looks like a real credential.
SMB_LOGIN = "test" + "-smb-" + "pass"


class SMBOSError(OSError):
    pass


@dataclass
class _Stat:
    st_size: int
    st_mtime: float


@dataclass
class _Entry:
    name: str
    _stat: _Stat

    def is_file(self) -> bool:
        return True

    def stat(self) -> _Stat:
        return self._stat


@dataclass
class _Volume:
    caller_available_units: int
    sectors_per_unit: int = 1
    bytes_per_sector: int = 1


class _Writer(io.BytesIO):
    def __init__(self, fake: "FakeSmb", path: str, initial: bytes) -> None:
        super().__init__()
        self.write(initial)
        self._fake, self._path = fake, path

    def flush(self) -> None:
        super().flush()
        self._fake.files[self._path] = bytearray(self.getvalue())

    def close(self) -> None:
        if not self.closed:
            self.flush()
        super().close()


@dataclass
class FakeSmb:
    username: str = "photos"
    password: str = SMB_LOGIN
    free: int = 10**12
    files: dict[str, bytearray] = field(default_factory=dict)
    folders: set[str] = field(default_factory=set)
    fail_writes_after: int | None = None
    """Raise on writes once this many bytes have been written in total (lost share)."""
    written: int = 0

    def _auth(self, kwargs: dict[str, Any]) -> None:
        user = str(kwargs.get("username", "")).split("\\")[-1]
        if user != self.username or kwargs.get("password") != self.password:
            raise SMBOSError("STATUS_LOGON_FAILURE: The attempted logon is invalid.")

    def makedirs(self, path: str, exist_ok: bool = False, **kwargs: Any) -> None:
        self._auth(kwargs)
        self.folders.add(path.lower())

    def open_file(self, path: str, mode: str = "rb", **kwargs: Any) -> io.BytesIO:
        self._auth(kwargs)
        if mode == "rb":
            if path not in self.files:
                raise SMBOSError(2, "No such file or directory")
            return io.BytesIO(bytes(self.files[path]))
        initial = bytes(self.files.get(path, b"")) if mode == "ab" else b""
        writer = _Writer(self, path, initial)
        if self.fail_writes_after is not None:
            fake = self
            original = writer.write

            def guarded(data: Any) -> int:
                fake.written += len(data)
                if fake.written > (fake.fail_writes_after or 0):
                    writer.flush()
                    raise SMBOSError("STATUS_CONNECTION_RESET: the connection was reset")
                return original(data)

            writer.write = guarded  # type: ignore[method-assign]
        self.files[path] = bytearray(initial)
        return writer

    def remove(self, path: str, **kwargs: Any) -> None:
        self._auth(kwargs)
        if path not in self.files:
            raise SMBOSError(2, "No such file or directory")
        del self.files[path]

    def replace(self, source: str, target: str, **kwargs: Any) -> None:
        self._auth(kwargs)
        self.files[target] = self.files.pop(source)

    def stat(self, path: str, **kwargs: Any) -> _Stat:
        self._auth(kwargs)
        if path not in self.files:
            raise SMBOSError(2, "No such file or directory")
        return _Stat(len(self.files[path]), datetime.now(UTC).timestamp())

    def scandir(self, path: str, **kwargs: Any) -> list[_Entry]:
        self._auth(kwargs)
        prefix = path + "\\"
        return [
            _Entry(name[len(prefix) :], _Stat(len(data), datetime.now(UTC).timestamp()))
            for name, data in sorted(self.files.items())
            if name.startswith(prefix) and "\\" not in name[len(prefix) :]
        ]

    def stat_volume(self, path: str, **kwargs: Any) -> _Volume:
        self._auth(kwargs)
        return _Volume(self.free)
