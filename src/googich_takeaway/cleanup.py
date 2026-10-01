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
from datetime import UTC, datetime
from pathlib import Path

from googich_takeaway.destinations.immich import CheckAction, ImmichClient
from googich_takeaway.sources.local import ARCHIVE_SUFFIXES
from googich_takeaway.state import State
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

    @property
    def ready(self) -> bool:
        return self.completed_at is not None

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


def _describe(copy: ExportCopy, state: State) -> ExportCopy:
    key = export_key(copy.export_id, [(p.name, p.size) for p in copy.parts])
    copy.completed_at = state.export_completed_at(key)
    if copy.completed_at is None:
        copy.reason = "Not imported completely yet."
        return copy
    summary = state.export_summary(key) or {}
    copy.not_imported = _count(summary.get("no_date")) + _count(summary.get("unsupported"))
    return copy


def _count(value: object) -> int:
    return value if isinstance(value, int) else 0


def staged_exports(staging: Path | None, state: State) -> list[ExportCopy]:
    if staging is None or not staging.is_dir():
        return []
    archives = [
        p for p in staging.iterdir() if p.is_file() and p.name.lower().endswith(ARCHIVE_SUFFIXES)
    ]
    copies = []
    for export_id, paths in group_exports(archives).items():
        parts = [Part(p.name, p.stat().st_size) for p in paths]
        copies.append(_describe(ExportCopy(export_id, parts), state))
    return copies


def partial_downloads(staging: Path | None) -> list[Partial]:
    if staging is None or not staging.is_dir():
        return []
    found = []
    for path in sorted(staging.glob("*.part")):
        info = path.stat()
        found.append(
            Partial(
                path.name.removesuffix(".part"),
                info.st_size,
                datetime.fromtimestamp(info.st_mtime, UTC),
            )
        )
    return found


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
    staging: Path,
    state: State,
    export_id: str,
    confirmed_not_imported: bool,
    log: Callable[[str], None] = lambda _: None,
) -> int:
    """Delete one export's archives from the download folder. Returns bytes freed.

    Re-checks completeness against the files on disk at the moment of deletion.
    """
    copy = next((c for c in staged_exports(staging, state) if c.export_id == export_id), None)
    if copy is None:
        raise CleanupError("That export is no longer in the download folder.")
    if not copy.ready:
        raise CleanupError("That export has not been imported completely; nothing deleted.")
    if copy.needs_confirmation and not confirmed_not_imported:
        raise CleanupError(
            f"{copy.not_imported} files from this export were not imported (no date, or rejected "
            "by Immich). Tick the box to confirm you want to delete the archives anyway."
        )
    root = staging.resolve()
    freed = 0
    for part in copy.parts:
        path = (staging / part.name).resolve()
        if path.parent != root:
            raise CleanupError("Refusing to delete a file outside the download folder.")
        freed += path.stat().st_size
        path.unlink()
        log(f"Deleted {part.name} from the download folder")
    return freed


def delete_partial(staging: Path, name: str) -> int:
    path = (staging / (name + ".part")).resolve()
    if path.parent != staging.resolve() or not path.is_file():
        raise CleanupError("No such partial download.")
    size = path.stat().st_size
    path.unlink()
    (staging / (name + ".part.json")).unlink(missing_ok=True)
    return size
