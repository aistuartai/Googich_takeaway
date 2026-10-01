"""One complete run: fetch new archives, import new photos, and say what happened.

A run never retries without limit. Each download already retries a few times with backoff, and an
import stops after several failures in a row; when a run fails it ends, reports why, and the next
attempt waits for the next scheduled time. The scheduler pauses itself after repeated failed runs.

Exports are imported only when every part of them is present and none failed to download in this
run, because a photo's sidecar can sit in another part. An export that imported cleanly is
remembered, so later runs do not rescan it.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from googich_takeaway.config import Config, ConfigError
from googich_takeaway.destinations.immich import ImmichClient, ImmichError
from googich_takeaway.downloads import Downloader, NotEnoughSpaceError
from googich_takeaway.importer import Decision, plan_import, run_import
from googich_takeaway.notify import Message, Outcome
from googich_takeaway.sources.base import SourceError
from googich_takeaway.sources.gdrive import GoogleDriveSource
from googich_takeaway.sources.local import ARCHIVE_SUFFIXES
from googich_takeaway.state import State
from googich_takeaway.takeout.archives import ArchiveError, group_exports
from googich_takeaway.takeout.dates import DateResolver
from googich_takeaway.takeout.scan import scan_export

log = logging.getLogger("googich.run")

# Takeout may still be writing parts to Drive. An export whose newest part changed more recently
# than this waits for a later run, so it is never imported without parts that were not yet listed.
SETTLE_TIME = timedelta(hours=1)

ImmichFactory = Callable[[str, str], ImmichClient]
DriveFactory = Callable[[str, dict[str, object]], GoogleDriveSource]


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
    waiting: list[str] = field(default_factory=list)
    """Exports not imported yet because Takeout may still be adding parts."""
    problems: list[str] = field(default_factory=list)

    @property
    def nothing_new(self) -> bool:
        return self.downloaded == 0 and self.exports_imported == 0 and self.uploaded == 0

    def message(self) -> Message:
        if self.problems:
            done = self._done_lines()
            return Message(
                Outcome.FAILED,
                f"Run failed: {self.problems[0]}",
                [*self.problems, *(["", "What did work:", *done] if done else [])],
            )
        if self.nothing_new:
            lines = ["No new archives."] if not self.waiting else self._done_lines()
            return Message(Outcome.NO_NEW_DATA, "Checked: nothing new", lines)
        title = (
            f"Imported {self.uploaded} new photos and videos"
            if self.uploaded or not self.downloaded
            else f"Downloaded {self.downloaded} archives"
        )
        return Message(Outcome.SUCCESS, title, self._done_lines())

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
        if self.unverified:
            lines.append(f"{self.unverified} files are still being processed by Immich.")
        for export_id in self.waiting:
            lines.append(f"Export {export_id} is still being written by Takeout; next run.")
        return lines


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

    def run(self) -> RunReport:
        report = RunReport()
        try:
            self._run(report)
        except (ConfigError, SourceError, ImmichError, ArchiveError, OSError) as error:
            report.problems.append(str(error))
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
        if general.staging is None:
            raise ConfigError("No download folder is set (Settings).")
        if not sources:
            raise ConfigError("No sources are set up (Sources).")

        # 1. Fetch from every Drive source; a failure on one does not stop the others.
        failed_names: set[str] = set()
        newest: dict[str, datetime] = {}
        downloader = Downloader(
            general.staging, self.state, self.clock, self.sleep, progress=self.progress
        )
        for source in sources:
            if source.kind != "gdrive":
                continue
            try:
                with self.drive_factory(source.location, self.config.drive_key(source.id)) as drive:
                    fetched = downloader.fetch_new(drive)
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
        archives = _archives_in(general.staging)
        for source in sources:
            if source.kind == "local":
                archives += _archives_in(Path(source.location))
        # The download folder may also be a local source; never list an archive twice.
        archives = sorted({path.resolve(): path for path in archives}.values())
        resolver = DateResolver(default_timezone=ZoneInfo(general.timezone))
        # A part that failed to download is absent (only its .part file exists), so the export
        # would otherwise look complete without it. Block by export ID, from the failed names.
        blocked = set(group_exports([Path(name) for name in failed_names]))
        with self.immich_factory(immich.url, key) as client:
            for export_id, parts in group_exports(archives).items():
                if export_id in blocked:
                    report.problems.append(
                        f"Export {export_id} not imported: a part failed to download."
                    )
                    continue
                if export_id in newest and self.clock() - newest[export_id] < SETTLE_TIME:
                    report.waiting.append(export_id)
                    continue
                export_key = _export_key(export_id, parts)
                if self.state.is_export_complete(export_key):
                    continue
                self._import(export_id, export_key, parts, client, resolver, report)

    def _import(
        self,
        export_id: str,
        export_key: str,
        parts: list[Path],
        client: ImmichClient,
        resolver: DateResolver,
        report: RunReport,
    ) -> None:
        log.info("Importing export %s (%d parts)", export_id, len(parts))
        scan = scan_export(parts, resolver, self.clock(), progress=self.progress)
        checks = client.check_existing((i.sha1, i.sha1) for i in scan.unique_items())
        plan = plan_import(export_id, scan, checks, self.state, self.destination)
        result = run_import(
            plan, client, self.state, self.destination, self.clock, self.sleep, self.progress
        )
        report.exports_imported += 1
        report.uploaded += len(result.uploaded)
        report.already_present += len(plan.with_decision(Decision.IN_IMMICH)) + len(result.adopted)
        report.needs_review += len(plan.with_decision(Decision.NO_DATE))
        report.date_mismatches += len(result.date_mismatch)
        report.unverified += len(result.unverified)
        for item, detail in result.failed[:5]:
            report.problems.append(f"{item.name}: {detail}")
        if len(result.failed) > 5:
            report.problems.append(f"…and {len(result.failed) - 5} more files failed.")
        if result.aborted:
            report.problems.append(f"Export {export_id}: {result.aborted}.")
        clean = not (result.failed or result.aborted or result.date_mismatch or result.unverified)
        if clean:
            summary = {
                "uploaded": len(result.uploaded),
                "verified": len(result.verified),
                "no_date": len(plan.with_decision(Decision.NO_DATE)),
            }
            self.state.mark_export_complete(
                export_key, export_id, json.dumps(summary), self.clock()
            )


def _archives_in(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        p for p in folder.iterdir() if p.is_file() and p.name.lower().endswith(ARCHIVE_SUFFIXES)
    )


def _export_key(export_id: str, parts: list[Path]) -> str:
    """Identifies an export by its parts' names and sizes, so a newly added part re-imports."""
    listing = sorted((p.name, p.stat().st_size) for p in parts)
    digest = hashlib.sha256(json.dumps([export_id, listing]).encode()).hexdigest()
    return f"{export_id}:{digest[:16]}"
