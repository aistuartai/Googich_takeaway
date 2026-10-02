"""What can be cleaned up safely, and doing it.

Two places hold copies of Takeout archives once they are imported:

- **The download folder.** The app deletes archives there itself, but only for exports that were
  imported completely, and only after checking the files on disk still match what was imported.
- **Google Drive.** The app never deletes anything in Drive. It lists archives whose export was
  imported completely, with links, so the user removes them in Drive.

Files with no capture date, and files Immich rejected, are not uploaded. Deleting their archive
would leave them nowhere but Google Photos, so cleanup warns and asks for confirmation then.
Download history is kept, so a deleted archive is not downloaded again.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from googich_takeaway.destinations.immich import CheckAction, ImmichClient
from googich_takeaway.locations import (
    LocalLocation,
    Location,
    LocationError,
    StoredFile,
    archives,
)
from googich_takeaway.state import State, UploadStatus
from googich_takeaway.takeout.archives import group_exports


@dataclass(frozen=True)
class Part:
    name: str
    size: int
    link: str | None = None
    removed_at: datetime | None = None


@dataclass
class ExportCopy:
    export_id: str
    parts: list[Part]
    completed_at: datetime | None = None
    not_imported: int = 0
    """Files never uploaded (no date, or rejected by Immich) that only this archive holds."""
    reason: str = ""
    sources: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(p.size for p in self.parts)

    awaiting_check: int = 0
    """Uploads Immich has not processed yet, so their dates are not confirmed."""
    mismatched: int = 0
    """Uploads whose date in Immich differs from the one sent."""

    @property
    def ready(self) -> bool:
        return self.completed_at is not None and not self.awaiting_check and not self.mismatched

    @property
    def needs_confirmation(self) -> bool:
        return self.not_imported > 0


@dataclass(frozen=True)
class Partial:
    name: str
    size: int
    modified: datetime


def export_key(export_id: str, parts: list[tuple[str, int]]) -> str:
    """Same identity the pipeline uses: the export's parts' names and sizes."""
    listing = sorted(parts)
    digest = hashlib.sha256(json.dumps([export_id, listing]).encode()).hexdigest()
    return f"{export_id}:{digest[:16]}"


def _describe(copy: ExportCopy, state: State, destination: str = "immich") -> ExportCopy:
    key = export_key(copy.export_id, [(p.name, p.size) for p in copy.parts])
    copy.completed_at = state.export_completed_at(key)
    if copy.completed_at is None:
        copy.reason = "Not imported completely yet."
        return copy
    counts = state.verification_counts(destination, copy.export_id)
    copy.awaiting_check = counts.get(UploadStatus.UPLOADED.value, 0)
    copy.mismatched = counts.get(UploadStatus.DATE_MISMATCH.value, 0)
    if copy.mismatched:
        copy.reason = (
            f"{copy.mismatched} files show a different date in Immich than the one sent. "
            "Check them in the logs before removing anything."
        )
    elif copy.awaiting_check:
        copy.reason = (
            f"Imported. Immich is still processing {copy.awaiting_check} files; their dates are "
            "checked on the next runs, and the archive is safe to remove after that."
        )
    summary = state.export_summary(key) or {}
    copy.not_imported = _count(summary.get("no_date")) + _count(summary.get("unsupported"))
    return copy


def _count(value: object) -> int:
    return value if isinstance(value, int) else 0


def _staging(staging: Location | Path | None) -> Location | None:
    if isinstance(staging, Path):
        return LocalLocation(staging)
    return staging


def staged_exports(
    staging: Location | Path | None, state: State, found: list[StoredFile] | None = None
) -> list[ExportCopy]:
    """``found`` is a listing of the folder's archives already made, to save reading it again."""
    location = _staging(staging)
    if location is None:
        return []
    copies = []
    for export_id, files in group_exports(
        found if found is not None else archives(location)
    ).items():
        parts = [Part(f.name, f.size) for f in files]
        copies.append(_describe(ExportCopy(export_id, parts), state))
    return copies


def partial_downloads(staging: Location | Path | None) -> list[Partial]:
    location = _staging(staging)
    if location is None:
        return []
    return [
        Partial(f.name.removesuffix(".part"), f.size, f.modified)
        for f in location.list()
        if f.name.endswith(".part")
    ]


def drive_exports(state: State, source_names: dict[str, str]) -> list[ExportCopy]:
    """Exports downloaded from Drive, newest download record per file.

    ``source_names`` maps source keys (``gdrive:<folder>``) to the names the user gave them.
    """
    by_export: dict[str, ExportCopy] = {}
    for source, label in source_names.items():
        latest: dict[str, Part] = {}
        for record in state.downloads(source):
            latest[record.name] = Part(record.name, record.size, record.link, record.removed_at)
        for export_id, paths in group_exports([Path(n) for n in latest]).items():
            copy = by_export.setdefault(export_id, ExportCopy(export_id, []))
            copy.parts.extend(latest[p.name] for p in paths)
            if label not in copy.sources:
                copy.sources.append(label)
    copies = [_describe(c, state) for c in by_export.values()]
    return sorted(copies, key=lambda c: c.export_id, reverse=True)


@dataclass(frozen=True)
class Recheck:
    checked: int
    missing: int
    trashed: int

    @property
    def ok(self) -> bool:
        return self.missing == 0 and self.trashed == 0


def recheck(state: State, client: ImmichClient, destination: str, export_id: str) -> Recheck:
    """Ask Immich now whether every file uploaded from this export is still there."""
    hashes = state.uploaded_hashes_for_export(destination, export_id)
    results = client.check_existing((h, h) for h in hashes)
    missing = sum(1 for r in results.values() if r.action is CheckAction.ACCEPT)
    trashed = sum(1 for r in results.values() if r.is_trashed)
    return Recheck(checked=len(hashes), missing=missing, trashed=trashed)


class CleanupError(Exception):
    """A cleanup request was refused; the message is shown to the user."""


def delete_staged_export(
    staging: Location | Path,
    state: State,
    export_id: str,
    confirmed_not_imported: bool,
    log: Callable[[str], None] = lambda _: None,
) -> int:
    """Delete one export's archives from the download folder. Returns bytes freed.

    Re-checks completeness against the files on disk at the moment of deletion.
    """
    location = _staging(staging)
    if location is None:
        raise CleanupError("No download folder is set.")
    copy = next((c for c in staged_exports(location, state) if c.export_id == export_id), None)
    if copy is None:
        raise CleanupError("That export is no longer in the download folder.")
    if not copy.ready:
        if copy.completed_at is None:
            raise CleanupError("That export has not been imported completely; nothing deleted.")
        raise CleanupError(f"Not yet: {copy.reason} Nothing deleted.")
    if copy.needs_confirmation and not confirmed_not_imported:
        raise CleanupError(
            f"{copy.not_imported} files from this export were not imported (no date, or rejected "
            "by Immich). Tick the box to confirm you want to delete the archives anyway."
        )
    freed = 0
    for part in copy.parts:
        try:
            location.delete(part.name)  # names are checked: never outside the folder
        except LocationError as error:
            raise CleanupError(str(error)) from None
        freed += part.size
        log(f"Deleted {part.name} from the download folder")
    return freed


def delete_partial(staging: Location | Path, name: str) -> int:
    location = _staging(staging)
    if location is None:
        raise CleanupError("No download folder is set.")
    try:
        size = location.size(name + ".part")
        if size is None:
            raise CleanupError("No such partial download.")
        location.delete(name + ".part")
        location.delete(name + ".part.json")
    except LocationError:
        raise CleanupError("No such partial download.") from None
    return size
