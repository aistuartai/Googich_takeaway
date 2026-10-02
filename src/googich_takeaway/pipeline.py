"""One complete run: fetch new archives, import new photos, and say what happened.

A run never retries without limit. Each download already retries a few times with backoff, and an
import stops after several failures in a row; when a run fails it ends, reports why, and the next
attempt waits for the next scheduled time. The scheduler pauses itself after repeated failed runs.

Exports are imported only when every part of them is present and none failed to download in this
run, because a photo's sidecar can sit in another part. An export that imported cleanly is
remembered, so later runs do not rescan it.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from googich_takeaway.cleanup import export_key
from googich_takeaway.config import Config, ConfigError
from googich_takeaway.destinations.immich import ImmichClient, ImmichError
from googich_takeaway.downloads import Downloader, NotEnoughSpaceError
from googich_takeaway.importer import (
    Decision,
    ImportPlan,
    ImportResult,
    plan_import,
    run_import,
    verify_pending,
)
from googich_takeaway.locations import LocalLocation, LocationError, StoredFile
from googich_takeaway.locations import archives as list_archives
from googich_takeaway.notify import Message, Outcome
from googich_takeaway.progress import ItemState, Stage, Tracker
from googich_takeaway.sources.base import SourceError
from googich_takeaway.sources.gdrive import GoogleDriveSource
from googich_takeaway.state import State
from googich_takeaway.takeout.archives import ArchiveError, group_exports, part_number
from googich_takeaway.takeout.dates import DateResolver
from googich_takeaway.takeout.scan import ExportScan, scan_export

log = logging.getLogger("googich.run")

# Takeout may still be writing parts to Drive. An export whose newest part changed more recently
# than this waits for a later run, so it is never imported without parts that were not yet listed.
SETTLE_TIME = timedelta(hours=1)
COPY_SETTLE_TIME = timedelta(minutes=30)
"""An archive saved by hand (not from Drive) is left alone until it has not changed for this
long, so an export whose parts are still being downloaded or copied is not imported half-done."""
LATEST_EXPORT_SETTING = "photos.latest_export"

ImmichFactory = Callable[[str, str], ImmichClient]
DriveFactory = Callable[[str, dict[str, object]], GoogleDriveSource]


@dataclass(frozen=True)
class RunOptions:
    reimport: bool = False
    """Rescan finished exports and upload files Immich no longer has, even ones deleted there."""
    download_again: bool = False
    """Download archives from Drive even if they were downloaded before."""

    def describe(self) -> list[str]:
        chosen = []
        if self.reimport:
            chosen.append("re-import files missing from Immich")
        if self.download_again:
            chosen.append("download archives again")
        return chosen


@dataclass
class RunReport:
    downloaded: int = 0
    downloaded_bytes: int = 0
    exports_imported: int = 0
    uploaded: int = 0
    already_present: int = 0
    needs_review: int = 0
    date_mismatches: int = 0
    unverified: int = 0
    """Uploads Immich has not processed yet, across all exports."""
    verified_later: int = 0
    """Earlier uploads confirmed in this run."""
    already_imported: list[tuple[str, datetime]] = field(default_factory=list)
    """Exports skipped because they were imported completely before."""
    waiting: list[str] = field(default_factory=list)
    """Exports not imported yet because Takeout may still be adding parts."""
    incomplete: list[tuple[str, str]] = field(default_factory=list)
    """Exports saved by hand that look unfinished, and why: imported once complete."""
    problems: list[str] = field(default_factory=list)

    @property
    def nothing_new(self) -> bool:
        return self.downloaded == 0 and self.uploaded == 0

    def message(self) -> Message:
        if self.problems:
            done = self._done_lines()
            return Message(
                Outcome.FAILED,
                f"Run failed: {self.problems[0]}",
                [*self.problems, *(["", "What did work:", *done] if done else [])],
            )
        if self.nothing_new:
            return Message(
                Outcome.NO_NEW_DATA,
                f"Nothing new: {self._counts()}",
                self._done_lines() or ["No new archives."],
            )
        if self.already_imported and not self.exports_imported and not self.uploaded:
            title = f"{self._counts()}: already imported before"
        else:
            title = self._counts()
        return Message(Outcome.SUCCESS, title, self._done_lines())

    def _counts(self) -> str:
        """What the run did, always with the upload counts, even when they are 0: for the
        dashboard and History, where a run that uploaded nothing should say so."""
        parts = []
        if self.downloaded:
            parts.append(f"Downloaded {self.downloaded} {_plural(self.downloaded, 'archive')}")
        uploaded = (
            f"{self.uploaded:,} {_plural(self.uploaded, 'photo or video', 'photos and videos')}"
        )
        parts.append(
            f"{uploaded} uploaded" if parts else f"{uploaded[0].upper()}{uploaded[1:]} uploaded"
        )
        parts.append(f"{self.already_present:,} skipped")
        return ", ".join(parts)

    def _done_lines(self) -> list[str]:
        lines = []
        if self.downloaded:
            lines.append(
                f"Downloaded {self.downloaded} archives ({self.downloaded_bytes / 1e6:.0f} MB)."
            )
        if self.exports_imported or self.uploaded:
            lines.append(
                f"Uploaded {self.uploaded} new files; "
                f"{self.already_present} were already in Immich."
            )
        if self.needs_review:
            lines.append(f"{self.needs_review} files have no date and need review.")
        if self.date_mismatches:
            lines.append(f"{self.date_mismatches} files show a different date in Immich.")
        if self.verified_later:
            lines.append(f"Confirmed {self.verified_later} earlier uploads in Immich.")
        if self.unverified:
            lines.append(
                f"{self.unverified} uploads are still being processed by Immich; "
                "they are checked on the next runs."
            )
        for export_id in self.waiting:
            lines.append(f"Export {export_id} is still being written by Takeout; next run.")
        for export_id, why in self.incomplete:
            lines.append(f"Export {export_id} is not imported yet: {why}.")
        for export_id, when in self.already_imported:
            lines.append(
                f"Export {export_id} was already imported on {when:%d %b %Y %H:%M} UTC; "
                "skipped (use Re-import to check it again)."
            )
        return lines


def _plural(count: int, one: str, many: str | None = None) -> str:
    return one if count == 1 else many or f"{one}s"


@dataclass(frozen=True)
class Pipeline:
    config: Config
    state: State
    clock: Callable[[], datetime]
    sleep: Callable[[float], None]
    immich_factory: ImmichFactory = ImmichClient
    drive_factory: DriveFactory = GoogleDriveSource
    destination: str = "immich"
    progress: Callable[[int], None] | None = None
    options: RunOptions = RunOptions()
    tracker: Tracker | None = None

    def run(self) -> RunReport:
        report = RunReport()
        try:
            self._run(report)
        except (ConfigError, SourceError, ImmichError, ArchiveError, OSError) as error:
            report.problems.append(str(error))
        except Exception as error:  # a bug: fail the run cleanly, never leave it half-recorded
            log.exception("Unexpected error during the run")
            report.problems.append(
                f"Unexpected error ({type(error).__name__}); details are in the log."
            )
        for problem in report.problems:
            log.warning("Run problem: %s", problem)
        return report

    def _run(self, report: RunReport) -> None:
        immich = self.config.immich()
        key = self.config.immich_key()
        general = self.config.general()
        sources = [s for s in self.config.sources() if s.enabled]
        if not immich.url or not key:
            raise ConfigError("Immich is not set up (Settings).")
        staging = self.config.staging_location()
        if staging is None:
            raise ConfigError("No download folder is set (Settings).")
        if not sources:
            raise ConfigError("No sources are set up (Sources).")

        # 1. Fetch from every Drive source; a failure on one does not stop the others.
        failed_names: set[str] = set()
        newest: dict[str, datetime] = {}
        downloader = Downloader(
            staging,
            self.state,
            self.clock,
            self.sleep,
            progress=self.progress,
            tracker=self.tracker,
        )
        for source in sources:
            if source.kind != "gdrive":
                continue
            try:
                with self.drive_factory(source.location, self.config.drive_key(source.id)) as drive:
                    fetched = downloader.fetch_new(
                        drive, ignore_history=self.options.download_again
                    )
            except NotEnoughSpaceError as error:
                report.problems.append(str(error))
                return
            except (SourceError, ConfigError) as error:
                report.problems.append(f"{source.name}: {error}")
                continue
            for file in fetched.listed:
                export = next(iter(group_exports([Path(file.name)])))
                newest[export] = max(newest.get(export, file.modified), file.modified)
            report.downloaded += len(fetched.downloaded)
            report.downloaded_bytes += sum(f.size for f, _ in fetched.downloaded)
            for file, detail in fetched.failed:
                failed_names.add(file.name)
                report.problems.append(f"{source.name}: {detail}")

        # 2. Import every complete export not imported before.
        try:
            found = list_archives(staging)
        except LocationError as error:
            raise ConfigError(f"Download folder: {error}") from None
        for source in sources:
            if source.kind == "local":
                found += list_archives(LocalLocation(Path(source.location)))
        # The download folder may also be a local source; never list an archive twice.
        archives = sorted({_identity(a): a for a in found}.values())
        resolver = DateResolver(default_timezone=ZoneInfo(general.timezone))
        # A part that failed to download is absent (only its .part file exists), so the export
        # would otherwise look complete without it. Block by export ID, from the failed names.
        blocked = set(group_exports([Path(name) for name in failed_names]))
        exports = group_exports(archives)
        if self.tracker:
            pending = [
                (export_id, sum(p.size for p in parts))
                for export_id, parts in exports.items()
                if self.options.reimport
                or not self.state.is_export_complete(_export_key(export_id, parts))
            ]
            self.tracker.plan(Stage.SCAN, pending)
        with self.immich_factory(immich.url, key) as client:
            for export_id, parts in exports.items():
                if export_id in blocked:
                    report.problems.append(
                        f"Export {export_id} not imported: a part failed to download."
                    )
                    self._skip(export_id, "a part failed to download")
                    continue
                if export_id in newest and self.clock() - newest[export_id] < SETTLE_TIME:
                    report.waiting.append(export_id)
                    self._skip(export_id, "Takeout is still writing it")
                    continue
                unfinished = None if export_id in newest else self._unfinished(parts)
                if unfinished:
                    report.incomplete.append((export_id, unfinished))
                    self._skip(export_id, unfinished)
                    continue
                export_key = _export_key(export_id, parts)
                completed = self.state.export_completed_at(export_key)
                if completed is not None and not self.options.reimport:
                    report.already_imported.append((export_id, completed))
                    log.info(
                        "Export %s was already imported on %s; skipped (Re-import rescans it)",
                        export_id,
                        completed.isoformat(timespec="minutes"),
                    )
                    continue
                try:
                    self._import(export_id, export_key, parts, client, resolver, report)
                except (ImmichError, LocationError):
                    raise  # Immich or the folder is unreachable: every export would fail too
                except Exception as error:  # one damaged export must not stop the others
                    log.exception("Export %s could not be imported", export_id)
                    report.problems.append(
                        f"Export {export_id} not imported: {type(error).__name__}: {error}"[:300]
                    )
                    self._skip(export_id, "could not be read")
            # Immich processes large imports in the background; catch up on earlier uploads.
            checked = verify_pending(self.state, client, self.destination, self.clock)
            report.verified_later += checked.verified
            report.date_mismatches += len(checked.mismatched)
            report.unverified = checked.remaining

    def _unfinished(self, parts: list[StoredFile]) -> str | None:
        """Why an export saved by hand looks unfinished, if it does: a part missing between
        the others, or a part changed in the last half hour (still downloading or copying)."""
        numbers = sorted(n for p in parts if (n := part_number(p.name)) is not None)
        if numbers:
            missing = sorted(set(range(1, numbers[-1] + 1)) - set(numbers))
            if missing:
                listed = ", ".join(f"{n:03d}" for n in missing[:5])
                return (
                    f"part {listed} is missing"
                    if len(missing) == 1
                    else f"parts {listed} are missing"
                )
        now = self.clock()
        # Within half an hour either way: a server whose clock runs ahead (an SMB share) shows
        # times in the future, which must not hold an export back for good.
        if any(p.modified and abs(now - p.modified) < COPY_SETTLE_TIME for p in parts):
            return "a part was saved less than 30 minutes ago; waiting in case more are coming"
        return None

    def _import(
        self,
        export_id: str,
        export_key: str,
        parts: list[StoredFile],
        client: ImmichClient,
        resolver: DateResolver,
        report: RunReport,
    ) -> None:
        log.info("Importing export %s (%d parts)", export_id, len(parts))
        if self.tracker:
            self.tracker.begin(Stage.SCAN, export_id, sum(p.size for p in parts))
        scan = scan_export(parts, resolver, self.clock(), progress=self._scan_progress)
        if self.tracker:
            self.tracker.end(Stage.SCAN, export_id)
        checks = client.check_existing((i.sha1, i.sha1) for i in scan.unique_items())
        plan = plan_import(
            export_id, scan, checks, self.state, self.destination, self.options.reimport
        )
        result = run_import(
            plan,
            client,
            self.state,
            self.destination,
            self.clock,
            self.sleep,
            self.progress,
            tracker=self.tracker,
            verify_attempts=1,  # one quick pass; verify_pending catches up on later runs
        )
        report.exports_imported += 1
        self._remember_export(export_id, scan, plan, result)
        report.uploaded += len(result.uploaded)
        report.already_present += len(plan.with_decision(Decision.IN_IMMICH)) + len(result.adopted)
        report.needs_review += len(plan.with_decision(Decision.NO_DATE))
        report.date_mismatches += len(result.date_mismatch)

        for item, detail in result.failed[:5]:
            report.problems.append(f"{item.name}: {detail}")
        if len(result.failed) > 5:
            report.problems.append(f"…and {len(result.failed) - 5} more files failed.")
        if result.aborted:
            report.problems.append(f"Export {export_id}: {result.aborted}.")
        # Complete once everything is uploaded: checking dates in Immich can take hours after a
        # big import and continues on later runs without rescanning. Cleanup waits for it.
        clean = not (result.failed or result.aborted)
        if clean:
            summary = {
                "uploaded": len(result.uploaded),
                "verified": len(result.verified),
                "no_date": len(plan.with_decision(Decision.NO_DATE)),
                "unsupported": len(plan.with_decision(Decision.UNSUPPORTED)),
                "parts": sorted(p.name for p in parts),
            }
            self.state.mark_export_complete(
                export_key, export_id, json.dumps(summary), self.clock()
            )

    def _remember_export(
        self, export_id: str, scan: ExportScan, plan: ImportPlan, result: ImportResult
    ) -> None:
        """What the newest export held, for the Google Photos figure on the dashboard.

        Google offers no way to count a Google Photos library, so the exports are the best
        measure there is. Every distinct item from every export is also remembered, since an
        export may hold only part of the library (a date range, some albums)."""
        self.state.record_seen_items(
            export_id, (item.sha1 for item in scan.unique_items()), self.clock()
        )
        stored = self.state.get_json(LATEST_EXPORT_SETTING)
        if str(stored.get("export_id", "")) > export_id:
            return  # an older export, imported again
        counts = {
            "export_id": export_id,
            "items": len(scan.unique_items()),
            "in_immich": len(plan.with_decision(Decision.IN_IMMICH))
            + len(result.uploaded)
            + len(result.adopted),
            "in_trash": len(plan.with_decision(Decision.IN_IMMICH_TRASH)),
            "deleted": len(plan.with_decision(Decision.DELETED_IN_IMMICH)),
            "not_imported": len(plan.with_decision(Decision.NO_DATE))
            + len(plan.with_decision(Decision.UNSUPPORTED)),
            "failed": len(result.failed),
            "at": self.clock().isoformat(),
        }
        self.state.set_json(LATEST_EXPORT_SETTING, counts, self.clock())

    def _scan_progress(self, amount: int) -> None:
        if self.progress:
            self.progress(amount)
        if self.tracker:
            self.tracker.advance(amount)

    def _skip(self, export_id: str, why: str) -> None:
        if self.tracker:
            self.tracker.end(Stage.SCAN, export_id, ItemState.SKIPPED, why)


def _identity(archive: StoredFile) -> str:
    location = archive.location
    if isinstance(location, LocalLocation):
        return str((location.folder / archive.name).resolve())
    return f"{location.describe()}\\{archive.name}"


def _export_key(export_id: str, parts: list[StoredFile]) -> str:
    """Identifies an export by its parts' names and sizes, so a newly added part re-imports."""
    return export_key(export_id, [(p.name, p.size) for p in parts])
