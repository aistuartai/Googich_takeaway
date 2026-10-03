"""Dashboard figures: the journey from Google Photos to Immich, and the summary boxes.

Kept apart from the web routes so they can be tested on their own. Readings that are slow over
SMB (folder listings, free space) are remembered for a few seconds, shared by requests that run
in parallel threads: single dict reads and writes are atomic, and the worst a race can do is
read a folder once more than needed. A lock covers the one check-then-change.
"""

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import PurePath

from googich_takeaway import cleanup, downloads
from googich_takeaway.config import Config, ConfigError
from googich_takeaway.destinations.immich import ImmichError
from googich_takeaway.locations import Location, LocationError, StoredFile
from googich_takeaway.locations import archives as list_archives
from googich_takeaway.pipeline import LATEST_EXPORT_SETTING, DriveFactory, ImmichFactory
from googich_takeaway.progress import Stage
from googich_takeaway.sources.base import SourceError
from googich_takeaway.state import State
from googich_takeaway.worker import Worker

log = logging.getLogger(__name__)

LIBRARY_READ_EVERY = 60.0
"""Seconds between readings of how much Immich holds."""


def drive_labels(config: Config) -> dict[str, str]:
    """Drive source names, by the source name downloads are recorded under."""
    return {f"gdrive:{s.location}": s.name for s in config.sources() if s.kind == "gdrive"}


def _library_key(url: str, key: str) -> str:
    """A new address or key is another library, read afresh."""
    return url + hashlib.sha256(key.encode()).hexdigest()[:16]


def latest_export(state: State) -> dict[str, object] | None:
    stored = state.get_setting(LATEST_EXPORT_SETTING)
    if not stored:
        return None
    data = json.loads(stored)
    data["at"] = datetime.fromisoformat(data["at"])
    return data if isinstance(data, dict) else None


class Summaries:
    def __init__(self, worker: Worker, immich_factory: ImmichFactory) -> None:
        self._worker = worker
        self._immich_factory = immich_factory
        self._library: dict[str, tuple[float, int | None]] = {}
        self._library_reading: set[str] = set()
        self.listing_cache: dict[str, tuple[float, list[StoredFile] | None]] = {}
        """Download folder listings; cleared after a delete."""
        self._slow_cache: dict[str, tuple[float, object]] = {}
        self._downloading_now: list[str] = []
        self._lock = threading.Lock()

    def folder_listing(self, location: Location) -> list[StoredFile] | None:
        """The download folder's archives, read at most every 10 seconds (it may be on SMB).

        None if the folder cannot be read right now."""
        key = location.describe()
        hit = self.listing_cache.get(key)
        if hit and time.monotonic() - hit[0] < 10:
            return hit[1]
        try:
            found: list[StoredFile] | None = list_archives(location)
        except LocationError:
            found = None
        self.listing_cache[key] = (time.monotonic(), found)
        return found

    def immich_library(self, url: str | None, key: str | None) -> int | None:
        """How many photos and videos the Immich user has, all told: read at most once a
        minute, in the background so a slow Immich never holds the dashboard up. None until
        known, or if the API key may not read it (it needs ``asset.statistics``)."""
        if not url or not key:
            return None
        cache_key = _library_key(url, key)
        reader = None
        with self._lock:
            hit = self._library.get(cache_key)
            stale = hit is None or time.monotonic() - hit[0] >= LIBRARY_READ_EVERY
            if stale and cache_key not in self._library_reading:
                self._library_reading.add(cache_key)
                reader = threading.Thread(
                    target=self._read_library, args=(cache_key, url, key), daemon=True
                )
                reader.start()
        if reader is not None and hit is None:
            reader.join(2)  # usually quick: show it on this page already
        hit = self._library.get(cache_key)
        return hit[1] if hit else None

    def refresh(
        self, config: Config, state: State, drive_factory: DriveFactory, at: datetime
    ) -> list[str]:
        """Read every figure on the journey again now: each Drive folder, the download folder
        and Immich. Returns what could not be read."""
        problems: list[str] = []
        for source in config.sources():
            if source.kind != "gdrive":
                continue
            try:
                with drive_factory(source.location, config.drive_key(source.id)) as drive:
                    files = drive.list_archives()
                downloads.record_listing(state, f"gdrive:{source.location}", files, at)
            except (SourceError, ConfigError) as error:
                log.warning("Could not list %s: %s", source.name, error)
                problems.append(source.name)
        location = config.staging_location()
        if location is not None:
            self.listing_cache.pop(location.describe(), None)
            self._slow_cache.pop(f"free:{location.describe()}", None)
            if self.folder_listing(location) is None:
                log.warning("Could not read the download folder %s", location.describe())
                problems.append("the download folder")
        immich = config.immich()
        key = config.immich_key()
        if (
            immich.url
            and key
            and not self._read_library(_library_key(immich.url, key), immich.url, key)
        ):
            log.warning("Could not reach Immich to count its photos and videos")
            problems.append("Immich")
        return problems

    def _read_library(self, cache_key: str, url: str, key: str) -> bool:
        """Count the library now. False if Immich could not be asked."""
        previous = self._library.get(cache_key)
        answered = True
        try:
            with self._immich_factory(url, key) as client:
                found: int | None = client.library_size()
        except ImmichError as error:
            log.debug("Could not count the photos in Immich: %s", error)
            # Not allowed: show none. Unreachable for now: keep the last count.
            answered = "permission" in str(error)
            found = None if answered else (previous[1] if previous else None)
        except Exception:
            log.exception("Could not count the photos in Immich")
            answered, found = False, None
        self._library[cache_key] = (time.monotonic(), found)
        self._library_reading.discard(cache_key)
        return answered

    def journey(self, config: Config, state: State) -> dict[str, object]:
        drive_count, drive_bytes = state.download_totals()
        location = config.staging_location()
        folder_count: int | None = 0
        folder_bytes = 0
        arriving = False
        found: list[StoredFile] | None = []
        if location is not None:
            active = self._worker.tracker.active() if self._worker.status_running() else None
            name = active[1].name if active and active[0] is Stage.DOWNLOAD else None
            with self._lock:
                if self._downloading_now != [name or ""]:
                    # A download finished or started: read the folder again, counted once.
                    self.listing_cache.pop(location.describe(), None)
                    self._downloading_now[:] = [name or ""]
            found = self.folder_listing(location)
            if found is None:
                folder_count = None
            else:
                folder_count, folder_bytes = len(found), sum(a.size for a in found)
                if active and name and all(a.name != name for a in found):
                    folder_bytes += min(active[1].done, active[1].size)  # arriving now
                    arriving = True
        drive_names = {f"gdrive:{s.location}" for s in config.sources() if s.kind == "gdrive"}
        # Drive archives with a copy in the download folder now (not cleaned up yet).
        drive_in_folder: int | None = None
        if folder_count is not None:
            here = {a.name for a in found or []}  # an archive arriving now is not here yet
            drive_in_folder = len(
                {
                    (r.source, r.file_id)
                    for source in drive_names
                    for r in state.downloads(source)
                    if PurePath(r.name.replace("\\", "/")).name in here
                }
            )
        listed = [v for k, v in downloads.listings(state).items() if k in drive_names]
        photos = latest_export(state)
        if photos and photos.get("uploading") and self._worker.status_running():
            # Saved when uploading began: add what has gone up since, from the live progress.
            snapshot = self._worker.progress()
            done = sum(v.files_done for v in snapshot.stages if v.stage is Stage.UPLOAD)
            sent = max(0, done - int(str(photos.get("sent_before", 0))))
            photos = dict(photos)
            photos["in_immich"] = int(str(photos.get("in_immich", 0))) + sent
            photos["to_upload"] = max(0, int(str(photos.get("to_upload", 0))) - sent)
        return {
            "uses_drive": bool(drive_names),
            "has_sources": bool(config.sources()),
            "drive_count": drive_count,
            "drive_in_folder": drive_in_folder,
            "drive_bytes": drive_bytes,
            "drive_listed": sum(int(str(v["count"])) for v in listed) if listed else None,
            "drive_listed_bytes": sum(int(str(v["bytes"])) for v in listed),
            "drive_listed_at": max(datetime.fromisoformat(str(v["at"])) for v in listed)
            if listed
            else None,
            "folder_count": folder_count,
            "folder_bytes": folder_bytes,
            "folder_arriving": arriving,
            "immich_count": state.upload_count("immich"),
            "immich_library": self.immich_library(config.immich().url, config.immich_key()),
            "photos": photos,
            "photos_seen": state.seen_item_count(),
        }

    def cached_call(self, key: str, seconds: float, read: Callable[[], object]) -> object:
        """Remember a slow reading (an SMB listing, free space) for a few seconds."""
        hit = self._slow_cache.get(key)
        if hit and time.monotonic() - hit[0] < seconds:
            return hit[1]
        value = read()
        self._slow_cache[key] = (time.monotonic(), value)
        return value

    def source_summaries(self, config: Config, state: State) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        for source in config.sources():
            latest: dict[str, tuple[int, datetime]] = {}
            for record in state.downloads(f"{source.kind}:{source.location}"):
                latest[record.file_id] = (record.size, record.downloaded_at)
            found.append(
                {
                    "source": source,
                    "folder_name": config.drive_folder_name(source.id),
                    "archives": len(latest),
                    "bytes": sum(size for size, _ in latest.values()),
                    "last": max((at for _, at in latest.values()), default=None),
                }
            )
        return found

    def destination_summary(self, config: Config, state: State) -> dict[str, object]:
        counts = state.verification_counts("immich")
        location = config.staging_location()
        free: int | None = None
        if location is not None:

            def read_free() -> int | None:
                try:
                    return location.free_space()
                except LocationError:
                    return None

            value = self.cached_call(f"free:{location.describe()}", 60, read_free)
            free = value if isinstance(value, int) else None
        return {
            "immich": config.immich(),
            "uploaded": sum(counts.values()),
            "awaiting": counts.get("uploaded", 0),
            "general": config.general(),
            "free": free,
        }

    def cleanup_summary(self, config: Config, state: State) -> dict[str, object]:
        location = config.staging_location()
        staged: list[cleanup.ExportCopy] | None = []
        if location is not None:
            found = self.folder_listing(location)
            staged = None if found is None else cleanup.staged_exports(location, state, found)
        folder_ready = [c for c in staged or [] if c.ready]
        drive_ready = [
            c
            for c in cleanup.drive_exports(state, drive_labels(config))
            if c.ready and any(p.removed_at is None for p in c.parts)
        ]
        folder_bytes = sum(c.size for c in folder_ready)
        drive_bytes = sum(p.size for c in drive_ready for p in c.parts if p.removed_at is None)
        return {
            "total": folder_bytes + drive_bytes,
            "folder_exports": len(folder_ready),
            "folder_bytes": folder_bytes,
            "folder_unreadable": staged is None,
            "drive_exports": len(drive_ready),
            "drive_bytes": drive_bytes,
        }
