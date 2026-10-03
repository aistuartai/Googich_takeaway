"""Reading archive parts while the next ones download.

Reading an export (hashing every file, collecting the sidecars) used to wait until every part
had downloaded. Now each part is read as soon as it arrives, in a thread of its own, into the
scan cache. When the downloads are done, the import finds those parts already read and goes
straight on to checking Immich and uploading.

Only reading is brought forward. Nothing is uploaded before the whole export has been read:
a photo's sidecar, which holds its date, can be in any part.

Pause and Cancel stop the reader at its next safe point, like the rest of the run: what it read
is kept in the scan cache, so the next run carries on from there. A part it cannot read is left
for the import to read, which reports the problem as it always has.
"""

import logging
import queue
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from googich_takeaway.locations import StoredFile
from googich_takeaway.progress import RunStopped, Stage, Tracker
from googich_takeaway.state import State
from googich_takeaway.takeout.archives import group_exports
from googich_takeaway.takeout.dates import DateResolver
from googich_takeaway.takeout.scan import scan_export
from googich_takeaway.takeout.scan_cache import CachedEntry, decode_all, encode

log = logging.getLogger(__name__)


class StateScanCache:
    """The scan cache (``takeout.scan_cache``), kept in the state database."""

    def __init__(self, state: State, clock: Callable[[], datetime]) -> None:
        self._state = state
        self._clock = clock

    def load(self, archive: str) -> tuple[bool, list[CachedEntry]]:
        complete, rows = self._state.scan_part(archive)
        if complete:
            self._state.touch_scan_part(archive, self._clock())
        return complete, decode_all(rows)

    def save(self, archive: str, entries: list[CachedEntry], complete: bool = False) -> None:
        rows = [(entry.path, encode(entry)) for entry in entries]
        self._state.save_scan_part(archive, rows, complete, self._clock())


class _Abandoned(BaseException):
    """The run ended before the reader got through its parts."""


class ReadAhead:
    """One reader thread, fed with each archive as it downloads.

    Use as a context manager around the downloads; ``finish`` waits for the parts still being
    read, and leaving the block early (Pause, Cancel, an error) stops the reader."""

    def __init__(
        self,
        state_path: Path,
        clock: Callable[[], datetime],
        resolver: DateResolver,
        tracker: Tracker | None,
    ) -> None:
        self._state_path = state_path
        self._clock = clock
        self._resolver = resolver
        self._tracker = tracker
        self._queue: queue.SimpleQueue[StoredFile | None] = queue.SimpleQueue()
        self._abandon = threading.Event()
        self._thread = threading.Thread(target=self._run, name="googich-read-ahead", daemon=True)
        self.parts_read = 0
        self._ended = False
        """The end of the queue was reached."""

    def __enter__(self) -> "ReadAhead":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._abandon.set()
        self._stop()

    def add(self, archive: StoredFile) -> None:
        self._queue.put(archive)

    def finish(self) -> None:
        """Wait until every part added so far has been read."""
        self._stop()

    def _stop(self) -> None:
        if self._thread.is_alive():
            self._queue.put(None)
            self._thread.join()

    def _run(self) -> None:
        try:
            self._read_all()
        except Exception:  # the import reads whatever was not read here
            log.exception("Reading while downloading stopped")
            self._abandon.set()
            while not self._ended and self._queue.get() is not None:
                pass  # take what is still added, so finish() returns

    def _read_all(self) -> None:
        with State(self._state_path) as state:
            cache = StateScanCache(state, self._clock)
            while (archive := self._queue.get()) is not None:
                if self._abandon.is_set():
                    continue
                try:
                    self._read(archive, cache)
                except (_Abandoned, RunStopped):
                    self._abandon.set()  # what was read so far is in the cache
                except Exception as error:  # the import reads it again, and reports any problem
                    log.warning("Could not read %s ahead: %s", archive.name, error)
            self._ended = True

    def _read(self, archive: StoredFile, cache: StateScanCache) -> None:
        export_id = next(iter(group_exports([Path(archive.name)])))
        tracker = self._tracker
        if tracker:
            tracker.begin_alongside(Stage.SCAN, export_id, archive.size)

        def progress(amount: int) -> None:
            if self._abandon.is_set():
                raise _Abandoned
            if tracker:
                tracker.advance_alongside(Stage.SCAN, export_id, amount)

        def resumed(amount: int) -> None:
            if tracker:
                tracker.advance_alongside(Stage.SCAN, export_id, amount)

        log.info("Reading %s while the downloads go on", archive.name)
        scan_export([archive], self._resolver, self._clock(), progress, cache, resumed)
        self.parts_read += 1
