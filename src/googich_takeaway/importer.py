"""Importing a scanned export into Immich: plan, upload, verify.

Planning decides each file's fate from the scan, Immich's duplicate check and the local state:

- Already in Immich: recorded as known, so a later deletion in Immich is respected.
- In Immich's trash: left alone.
- Uploaded by this app before but no longer in Immich: skipped, because the user deleted it.
  ``reimport`` overrides this.
- No capture date found: skipped and listed for review, never given today's date.
- Otherwise: uploaded with an XMP sidecar carrying the date, then read back and checked.

Every upload is recorded as soon as Immich confirms it, so an interrupted run resumes cleanly:
files already sent are found by Immich's duplicate check and adopted, never sent twice.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from googich_takeaway.destinations.immich import (
    AssetDates,
    CheckAction,
    CheckResult,
    ImmichClient,
    ImmichError,
)
from googich_takeaway.destinations.xmp import build_xmp
from googich_takeaway.progress import ItemState, Stage, Tracker
from googich_takeaway.state import State, UploadRecord, UploadStatus
from googich_takeaway.takeout.archives import ArchiveSource, iter_entries
from googich_takeaway.takeout.scan import ExportScan, ScannedItem

MAX_CONSECUTIVE_FAILURES = 5
VERIFY_ATTEMPTS = 10
VERIFY_DELAY_SECONDS = 2.0


class Decision(StrEnum):
    UPLOAD = "upload"
    IN_IMMICH = "in-immich"
    IN_IMMICH_TRASH = "in-immich-trash"
    DELETED_IN_IMMICH = "deleted-in-immich"
    """This app uploaded it before; it is gone from Immich, so the user removed it."""
    NO_DATE = "no-date"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class PlannedItem:
    item: ScannedItem
    decision: Decision
    existing_asset: str | None = None


@dataclass
class ImportPlan:
    export_id: str
    items: list[PlannedItem] = field(default_factory=list)

    def with_decision(self, decision: Decision) -> list[PlannedItem]:
        return [p for p in self.items if p.decision is decision]

    @property
    def upload_bytes(self) -> int:
        return sum(p.item.size for p in self.with_decision(Decision.UPLOAD))


@dataclass
class ImportResult:
    uploaded: list[ScannedItem] = field(default_factory=list)
    adopted: list[ScannedItem] = field(default_factory=list)
    """Immich already had it when uploading (e.g. sent by an interrupted run)."""
    verified: list[ScannedItem] = field(default_factory=list)
    date_mismatch: list[tuple[ScannedItem, str]] = field(default_factory=list)
    unverified: list[ScannedItem] = field(default_factory=list)
    """Immich had not finished reading the file's metadata in time; checked on the next run."""
    failed: list[tuple[ScannedItem, str]] = field(default_factory=list)
    aborted: str | None = None


def plan_import(
    export_id: str,
    scan: ExportScan,
    checks: dict[str, CheckResult],
    state: State,
    destination: str,
    reimport: bool = False,
) -> ImportPlan:
    unique = scan.unique_items()
    ours = state.uploaded_hashes(destination, (i.sha1 for i in unique))
    plan = ImportPlan(export_id)
    for item in unique:
        check = checks[item.sha1]
        if check.action is CheckAction.REJECT:
            if check.reason == "duplicate":
                decision = Decision.IN_IMMICH_TRASH if check.is_trashed else Decision.IN_IMMICH
            else:
                decision = Decision.UNSUPPORTED
            plan.items.append(PlannedItem(item, decision, check.existing_asset))
        elif item.sha1 in ours and not reimport:
            plan.items.append(PlannedItem(item, Decision.DELETED_IN_IMMICH))
        elif item.date is None:
            plan.items.append(PlannedItem(item, Decision.NO_DATE))
        else:
            plan.items.append(PlannedItem(item, Decision.UPLOAD))
    return plan


def run_import(
    plan: ImportPlan,
    client: ImmichClient,
    state: State,
    destination: str,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    progress: Callable[[int], None] | None = None,
    tracker: Tracker | None = None,
    verify_attempts: int = VERIFY_ATTEMPTS,
) -> ImportResult:
    """Upload, then check what Immich has processed so far.

    Immich reads metadata in the background, and after a large import that can take hours, so
    files it has not processed yet stay ``uploaded`` and are checked by ``verify_pending`` on
    later runs. ``verify_attempts`` 1 makes a single quick pass.
    """
    result = ImportResult()
    _record_known(plan, state, destination, clock)
    _upload(plan, client, state, destination, clock, result, progress, tracker)
    _verify(result.uploaded, client, state, destination, clock, sleep, result, verify_attempts)
    return result


@dataclass
class PendingCheck:
    verified: int = 0
    mismatched: list[tuple[str, str]] = field(default_factory=list)
    """(path, detail) for uploads whose date in Immich differs from the one sent."""
    still_pending: int = 0
    """Checked now but not processed by Immich yet."""
    remaining: int = 0
    """Uploads still waiting to be checked after this pass, including ones not reached."""


def verify_pending(
    state: State,
    client: ImmichClient,
    destination: str,
    clock: Callable[[], datetime],
    limit: int = 2000,
    budget: timedelta = timedelta(minutes=5),
) -> PendingCheck:
    """Check earlier uploads that Immich had not processed yet, oldest first.

    Uses the capture date recorded at upload, so archives are not read again. Stops after
    ``limit`` files or ``budget`` time, whichever comes first; the rest wait for the next run.
    """
    check = PendingCheck()
    deadline = clock() + budget
    for record in state.unverified_uploads(destination, limit):
        if clock() >= deadline:
            break
        if not record.capture_date:
            continue
        expected = datetime.fromisoformat(record.capture_date)
        try:
            dates = client.asset_dates(record.asset_id)
        except ImmichError:
            check.still_pending += 1
            continue
        if dates.date_time_original is None:
            check.still_pending += 1
            continue
        if _dates_match(dates, expected):
            state.mark_verified(destination, record.sha1, UploadStatus.VERIFIED, clock())
            check.verified += 1
        else:
            detail = _mismatch_detail(record.capture_date, dates)
            state.mark_verified(
                destination, record.sha1, UploadStatus.DATE_MISMATCH, clock(), detail
            )
            check.mismatched.append((record.path, detail))
    check.remaining = state.verification_counts(destination).get(UploadStatus.UPLOADED.value, 0)
    return check


def _dates_match(dates: AssetDates, expected: datetime) -> bool:
    return dates.date_time_original == expected.astimezone(
        UTC
    ) and dates.local_date_time == expected.replace(tzinfo=None)


def _mismatch_detail(expected: str, dates: AssetDates) -> str:
    found = dates.date_time_original.isoformat() if dates.date_time_original else "no date"
    return (
        f"expected {expected}, Immich has {found} "
        f"(shown as {dates.local_date_time}, zone {dates.time_zone})"
    )


def _record_known(
    plan: ImportPlan, state: State, destination: str, clock: Callable[[], datetime]
) -> None:
    """Files Immich already has become known, so deleting them later is respected."""
    for planned in plan.with_decision(Decision.IN_IMMICH):
        if planned.existing_asset and state.get_upload(destination, planned.item.sha1) is None:
            state.record_upload(
                _record(
                    destination,
                    plan,
                    planned.item,
                    planned.existing_asset,
                    UploadStatus.ADOPTED,
                    clock(),
                )
            )


def _upload(
    plan: ImportPlan,
    client: ImmichClient,
    state: State,
    destination: str,
    clock: Callable[[], datetime],
    result: ImportResult,
    progress: Callable[[int], None] | None,
    tracker: Tracker | None = None,
) -> None:
    wanted = {p.item.path: p.item for p in plan.with_decision(Decision.UPLOAD)}
    if tracker:
        tracker.plan(Stage.UPLOAD, [(p, i.size) for p, i in sorted(wanted.items())])

    def advance(amount: int) -> None:
        if progress:
            progress(amount)
        if tracker:
            tracker.advance(amount)

    archives: dict[ArchiveSource, set[str]] = {}
    for item in wanted.values():
        archives.setdefault(item.archive, set()).add(item.path)

    consecutive_failures = 0
    for archive in sorted(archives, key=lambda a: a.name):
        paths = archives[archive]
        for entry in iter_entries(archive):
            if entry.path not in paths:
                continue
            item = wanted[entry.path]
            if item.date is None:
                raise RuntimeError(f"planned upload without a date: {item.path}")
            sidecar = build_xmp(item.date, item.gps, item.description)
            if tracker:
                tracker.begin(Stage.UPLOAD, item.path, item.size)
            try:
                sent = client.upload(
                    entry.stream,
                    item.name,
                    item.size,
                    item.sha1,
                    item.date.utc,
                    sidecar=sidecar,
                    favorite=item.favorited,
                    progress=advance,
                )
            except ImmichError as error:
                result.failed.append((item, str(error)))
                if tracker:
                    tracker.end(Stage.UPLOAD, item.path, ItemState.FAILED, str(error))
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    result.aborted = f"stopped after {consecutive_failures} failures in a row"
                    return
                continue
            consecutive_failures = 0
            if tracker:
                tracker.end(Stage.UPLOAD, item.path)
            status = UploadStatus.ADOPTED if sent.duplicate else UploadStatus.UPLOADED
            state.record_upload(_record(destination, plan, item, sent.asset_id, status, clock()))
            (result.adopted if sent.duplicate else result.uploaded).append(item)


def _verify(
    items: list[ScannedItem],
    client: ImmichClient,
    state: State,
    destination: str,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    result: ImportResult,
    attempts: int = VERIFY_ATTEMPTS,
) -> None:
    """Read each upload back; Immich extracts metadata asynchronously, so retry briefly."""
    pending = list(items)
    for attempt in range(attempts):
        still_pending = []
        for item in pending:
            record = state.get_upload(destination, item.sha1)
            if record is None or item.date is None:
                raise RuntimeError(f"verifying an item that was never recorded: {item.path}")
            try:
                dates = client.asset_dates(record.asset_id)
            except ImmichError:
                still_pending.append(item)
                continue
            if dates.date_time_original is None:
                still_pending.append(item)
                continue
            if _dates_match(dates, item.date.aware):
                state.mark_verified(destination, item.sha1, UploadStatus.VERIFIED, clock())
                result.verified.append(item)
            else:
                detail = _mismatch_detail(item.date.xmp_value(), dates)
                state.mark_verified(
                    destination, item.sha1, UploadStatus.DATE_MISMATCH, clock(), detail
                )
                result.date_mismatch.append((item, detail))
        pending = still_pending
        if not pending:
            return
        if attempt < attempts - 1:
            sleep(VERIFY_DELAY_SECONDS)
    result.unverified.extend(pending)


def _record(
    destination: str,
    plan: ImportPlan,
    item: ScannedItem,
    asset_id: str,
    status: UploadStatus,
    at: datetime,
) -> UploadRecord:
    return UploadRecord(
        destination=destination,
        sha1=item.sha1,
        asset_id=asset_id,
        status=status,
        export_id=plan.export_id,
        archive=item.archive.name,
        path=item.path,
        capture_date=item.date.xmp_value() if item.date else None,
        uploaded_at=at,
        verified_at=None,
        detail=None,
    )
