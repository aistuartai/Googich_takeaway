"""Local folder source: archives already on disk, for example downloaded by hand."""

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from googich_takeaway.sources.base import RemoteFile, SourceError

ARCHIVE_SUFFIXES = (".zip", ".tgz", ".tar.gz")
CHUNK = 1024 * 1024


class LocalSource:
    def __init__(self, folder: Path) -> None:
        self.name = f"local:{folder}"
        self.folder = folder

    def list_archives(self) -> list[RemoteFile]:
        if not self.folder.is_dir():
            raise SourceError(f"{self.folder} is not a folder")
        files = []
        for path in sorted(self.folder.iterdir()):
            if path.is_file() and path.name.lower().endswith(ARCHIVE_SUFFIXES):
                info = path.stat()
                files.append(
                    RemoteFile(
                        file_id=path.name,
                        name=path.name,
                        size=info.st_size,
                        modified=datetime.fromtimestamp(info.st_mtime, tz=UTC),
                        sha256=None,
                        md5=None,
                    )
                )
        return files

    def path(self, file: RemoteFile) -> Path:
        return self.folder / file.name

    def read(self, file: RemoteFile, start: int = 0) -> Iterator[bytes]:
        with self.path(file).open("rb") as handle:
            handle.seek(start)
            while chunk := handle.read(CHUNK):
                yield chunk

    def sha256(self, file: RemoteFile) -> str:
        digest = hashlib.sha256()
        for chunk in self.read(file):
            digest.update(chunk)
        return digest.hexdigest()
