"""Fetching archives from a source into the staging folder.

Downloads are resumable and verified:

- Data goes to ``<name>.part``. A small ``<name>.part.json`` records which version of the file it
  belongs to; if the file changed at the source, the partial download is discarded.
- An interrupted download continues from where it stopped, with retries and backoff.
- The finished file is checked against the checksum the source reports, flushed to disk and only
  then renamed into place, so a file without ``.part`` is always complete.
- A download that would not fit in the free space is refused before it starts.
- Every completed download is recorded, so the same file is never downloaded twice unless the
  history is ignored or the file is forgotten.
"""

import hashlib
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from googich_takeaway.progress import ItemState, Stage, Tracker
from googich_takeaway.sources.base import (
    RemoteFile,
    Source,
    SourceError,
    TransientSourceError,
)
from googich_takeaway.state import DownloadRecord, State

ATTEMPTS = 6
BACKOFF_START = 2.0
BACKOFF_MAX = 60.0
DEFAULT_FREE_MARGIN = 1024**3  # keep 1 GiB free after a download
HASH_CHUNK = 1024 * 1024


class NotEnoughSpaceError(SourceError):
    """The staging folder cannot hold the file."""


@dataclass
class FetchResult:
    downloaded: list[tuple[RemoteFile, Path]] = field(default_factory=list)
    skipped: list[RemoteFile] = field(default_factory=list)
    """Downloaded before, according to the history."""
    failed: list[tuple[RemoteFile, str]] = field(default_factory=list)
    removed_from_source: int = 0
    listed: list[RemoteFile] = field(default_factory=list)
    """Every archive the source showed, downloaded or not."""


@dataclass(frozen=True)
class Downloader:
    staging: Path
    state: State
    clock: Callable[[], datetime]
    sleep: Callable[[float], None]
    free_margin: int = DEFAULT_FREE_MARGIN
    progress: Callable[[int], None] | None = None
    tracker: Tracker | None = None

    def fetch_new(self, source: Source, ignore_history: bool = False) -> FetchResult:
        """Download every archive at the source not downloaded before."""
        result = FetchResult()
        files = source.list_archives()
        result.listed = files
        result.removed_from_source = self.state.mark_removed_from_source(
            source.name, (f.file_id for f in files), self.clock()
        )
        wanted = [
            f
            for f in files
            if ignore_history
            or not self.state.was_downloaded(source.name, f.file_id, f.fingerprint)
        ]
        result.skipped = [f for f in files if f not in wanted]
        if self.tracker:
            self.tracker.plan(Stage.DOWNLOAD, [(f.name, f.size) for f in wanted])
        for file in wanted:
            if self.tracker:
                self.tracker.begin(Stage.DOWNLOAD, file.name, file.size)
            try:
                path = self.download(source, file)
            except NotEnoughSpaceError as error:
                if self.tracker:
                    self.tracker.end(Stage.DOWNLOAD, file.name, ItemState.FAILED, str(error))
                raise  # nothing later will fit either
            except SourceError as error:
                result.failed.append((file, str(error)))
                if self.tracker:
                    self.tracker.end(Stage.DOWNLOAD, file.name, ItemState.FAILED, str(error))
                continue
            result.downloaded.append((file, path))
            if self.tracker:
                self.tracker.end(Stage.DOWNLOAD, file.name)
        return result

    def download(self, source: Source, file: RemoteFile) -> Path:
        self.staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.staging / _safe_name(file.name)
        part = target.with_name(target.name + ".part")
        marker = target.with_name(target.name + ".part.json")

        if part.exists() and _marker(marker) != file.fingerprint:
            part.unlink()  # partial download of an older version
        marker.write_text(json.dumps({"file_id": file.file_id, "fingerprint": file.fingerprint}))
        if not part.exists():
            # Owner-only from the first byte: these are someone's photos.
            os.close(os.open(part, os.O_CREAT | os.O_WRONLY, 0o600))
        have = part.stat().st_size
        if have > file.size:
            part.unlink()
            have = 0
        self._check_space(file.size - have, file.name)

        delay = BACKOFF_START
        for attempt in range(1, ATTEMPTS + 1):
            try:
                with part.open("ab") as handle:
                    for chunk in source.read(file, start=have):
                        handle.write(chunk)
                        have += len(chunk)
                        if self.progress:
                            self.progress(len(chunk))
                        if self.tracker:
                            self.tracker.advance(len(chunk))
                        if have > file.size:
                            raise SourceError(f"{file.name}: larger than the source reported")
                    handle.flush()
                    os.fsync(handle.fileno())
                break
            except TransientSourceError:
                have = part.stat().st_size
                if attempt == ATTEMPTS:
                    raise
                self.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX)

        if have != file.size:
            raise SourceError(f"{file.name}: got {have} bytes, expected {file.size}; will resume")
        _verify(part, file)
        part.replace(target)
        _fsync_dir(target.parent)
        marker.unlink(missing_ok=True)
        self.state.record_download(
            DownloadRecord(
                source=source.name,
                file_id=file.file_id,
                fingerprint=file.fingerprint,
                name=file.name,
                size=file.size,
                link=file.link,
                local_path=str(target),
                downloaded_at=self.clock(),
                forgotten_at=None,
                removed_at=None,
            )
        )
        return target

    def _check_space(self, needed: int, name: str) -> None:
        free = shutil.disk_usage(self.staging).free
        if needed + self.free_margin > free:
            raise NotEnoughSpaceError(
                f"{name}: needs {_gb(needed)} plus {_gb(self.free_margin)} margin, "
                f"but only {_gb(free)} is free in {self.staging}"
            )


def _verify(path: Path, file: RemoteFile) -> None:
    if file.sha256:
        algorithm, expected = "sha256", file.sha256
    elif file.md5:
        algorithm, expected = "md5", file.md5
    else:
        return  # nothing to check against; size already matched
    digest = hashlib.new(algorithm, usedforsecurity=False)
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)
    if digest.hexdigest() != expected.lower():
        path.unlink()
        raise SourceError(f"{file.name}: {algorithm} does not match the source; discarded")


def _marker(path: Path) -> str | None:
    try:
        return str(json.loads(path.read_text())["fingerprint"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _safe_name(name: str) -> str:
    """Archive names come from the source; never let one point outside the staging folder."""
    cleaned = Path(name.replace("\\", "/")).name
    if cleaned in ("", ".", "..") or cleaned.startswith("."):
        raise SourceError(f"refusing unsafe file name {name!r}")
    return cleaned


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _gb(value: int) -> str:
    return f"{value / 1000**3:.1f} GB"
