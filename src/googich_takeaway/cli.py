"""Command-line interface.

``googich scan`` reads Takeout archives and reports what an import would do. With an Immich URL
and key file it also asks Immich which files it already has. It never uploads or changes anything.

``googich import`` does the import: it shows the same plan, asks for confirmation, uploads new
files with their capture dates and checks each one in Immich afterwards.

``googich fetch`` downloads new Takeout archives from a Google Drive folder into a staging folder,
resuming interrupted downloads and skipping archives downloaded before.
"""

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from googich_takeaway import __version__
from googich_takeaway.destinations.immich import (
    CheckAction,
    CheckResult,
    ImmichClient,
    ImmichError,
    read_api_key,
)
from googich_takeaway.downloads import Downloader, NotEnoughSpaceError
from googich_takeaway.importer import Decision, ImportPlan, ImportResult, plan_import, run_import
from googich_takeaway.sources.base import SourceError
from googich_takeaway.sources.gdrive import GoogleDriveSource, load_service_account
from googich_takeaway.state import State, StateError
from googich_takeaway.takeout.archives import ArchiveError, archive_format, group_exports
from googich_takeaway.takeout.dates import DateResolver
from googich_takeaway.takeout.scan import ExportScan, ScannedItem, scan_export

ClientFactory = Callable[[str, str], ImmichClient]
DriveFactory = Callable[[str, dict[str, object]], GoogleDriveSource]


@dataclass(frozen=True)
class ItemStatus:
    item: ScannedItem
    check: CheckResult | None

    @property
    def label(self) -> str:
        if self.check is None:
            return "unchecked"
        if self.check.action is CheckAction.ACCEPT:
            return "new"
        if self.check.reason == "duplicate":
            return "in-immich-trash" if self.check.is_trashed else "in-immich"
        return self.check.reason or "rejected"


def main(
    argv: Sequence[str] | None = None,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    client_factory: ClientFactory = ImmichClient,
    confirm: Callable[[str], bool] | None = None,
    drive_factory: DriveFactory = GoogleDriveSource,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "scan":
        return _scan(args, out, err, client_factory)
    if args.command == "import":
        return _import(args, out, err, client_factory, confirm or _ask)
    if args.command == "fetch":
        return _fetch(args, out, err, drive_factory)
    if args.command == "serve":
        return _serve(args)
    parser.print_help(err)
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="googich", description=__doc__.split("\n")[0])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")
    scan = commands.add_parser(
        "scan",
        help="report what importing Takeout archives would do (never uploads)",
        description="Scan Takeout archives and report what an import would do. Read-only.",
    )
    _add_common(scan)
    scan.add_argument("--list", action="store_true", help="list every file and its outcome")
    scan.add_argument("--json", action="store_true", help="machine-readable output")

    imp = commands.add_parser(
        "import",
        help="upload new files from Takeout archives to Immich",
        description="Upload files Immich does not have, with their capture dates, then check them.",
    )
    _add_common(imp)
    imp.add_argument(
        "--state",
        type=Path,
        default=_default_state(),
        help="state database (default: $GOOGICH_STATE or ~/.local/share/googich/state.db)",
    )
    imp.add_argument(
        "--destination-name",
        default="immich",
        help="name this Immich server in the state database (default: immich)",
    )
    imp.add_argument(
        "--reimport",
        action="store_true",
        help="also upload files this app uploaded before that are no longer in Immich",
    )
    imp.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    fetch = commands.add_parser(
        "fetch",
        help="download new Takeout archives from Google Drive",
        description="Download new Takeout archives from a Google Drive folder. Read-only in Drive.",
    )
    fetch.add_argument(
        "--drive-folder",
        required=True,
        help="ID of the Drive folder Takeout writes to (the last part of its URL)",
    )
    fetch.add_argument(
        "--service-account",
        type=Path,
        required=True,
        help="service account key file (JSON), mode 600",
    )
    fetch.add_argument(
        "--staging", type=Path, required=True, help="folder to download archives into"
    )
    fetch.add_argument("--state", type=Path, default=_default_state(), help="state database")
    fetch.add_argument(
        "--ignore-history",
        action="store_true",
        help="download every archive again, even ones downloaded before",
    )
    fetch.add_argument(
        "--list-only", action="store_true", help="only list what would be downloaded"
    )

    serve = commands.add_parser("serve", help="run the web interface")
    serve.add_argument(
        "--host",
        default=os.environ.get("GOOGICH_HOST", "127.0.0.1"),
        help="address to listen on (default 127.0.0.1; use 0.0.0.0 in a container)",
    )
    serve.add_argument(
        "--port", type=int, default=int(os.environ.get("GOOGICH_PORT", "8080")), help="port"
    )
    serve.add_argument("--state", type=Path, default=_default_state(), help="state database")
    return parser


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("paths", nargs="+", type=Path, help="archives, or folders containing them")
    parser.add_argument(
        "--timezone",
        default=os.environ.get("TZ") or "UTC",
        help="time zone for photos with no other clue (default: $TZ or UTC)",
    )
    parser.add_argument(
        "--immich-url",
        default=os.environ.get("GOOGICH_IMMICH_URL"),
        help="Immich server, e.g. http://immich:2283 (or $GOOGICH_IMMICH_URL)",
    )
    parser.add_argument(
        "--key-file",
        type=Path,
        default=os.environ.get("GOOGICH_IMMICH_KEY_FILE"),
        help="file holding the Immich API key, mode 600 (or $GOOGICH_IMMICH_KEY_FILE)",
    )


def _default_state() -> Path:
    if os.environ.get("GOOGICH_STATE"):
        return Path(os.environ["GOOGICH_STATE"])
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "googich" / "state.db"


def _ask(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _scan(args: argparse.Namespace, out: TextIO, err: TextIO, client_factory: ClientFactory) -> int:
    try:
        zone = ZoneInfo(args.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        err.write(f"error: unknown time zone {args.timezone!r}\n")
        return 2
    if bool(args.immich_url) != bool(args.key_file):
        err.write("error: --immich-url and --key-file must be given together\n")
        return 2

    archives = _find_archives(args.paths)
    if not archives:
        err.write("error: no .zip, .tgz or .tar.gz archives found\n")
        return 2

    resolver = DateResolver(default_timezone=zone)
    now = datetime.now(UTC)
    scans: dict[str, ExportScan] = {}
    try:
        for export, parts in group_exports(archives).items():
            scans[export] = scan_export(parts, resolver, now)
    except ArchiveError as error:
        err.write(f"error: {error}\n")
        return 1

    version = None
    checks: dict[str, CheckResult] = {}
    if args.immich_url:
        try:
            with client_factory(args.immich_url, read_api_key(args.key_file)) as client:
                version = client.server_version()
                unique = {i.sha1 for s in scans.values() for i in s.unique_items()}
                checks = client.check_existing((sha1, sha1) for sha1 in sorted(unique))
        except (ImmichError, OSError) as error:
            err.write(f"error: {error}\n")
            return 1

    statuses = {
        export: [ItemStatus(i, checks.get(i.sha1)) for i in scan.unique_items()]
        for export, scan in scans.items()
    }
    if args.json:
        json.dump(_as_json(scans, statuses, version), out, indent=2)
        out.write("\n")
    else:
        _print_report(scans, statuses, version, args.list, out)
    return 0


def _import(
    args: argparse.Namespace,
    out: TextIO,
    err: TextIO,
    client_factory: ClientFactory,
    confirm: Callable[[str], bool],
) -> int:
    if not args.immich_url or not args.key_file:
        err.write("error: import needs --immich-url and --key-file\n")
        return 2
    try:
        zone = ZoneInfo(args.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        err.write(f"error: unknown time zone {args.timezone!r}\n")
        return 2
    archives = _find_archives(args.paths)
    if not archives:
        err.write("error: no .zip, .tgz or .tar.gz archives found\n")
        return 2

    resolver = DateResolver(default_timezone=zone)
    try:
        key = read_api_key(args.key_file)
        scans = {
            export: scan_export(parts, resolver, datetime.now(UTC))
            for export, parts in group_exports(archives).items()
        }
        with State(args.state) as state, client_factory(args.immich_url, key) as client:
            version = client.server_version()
            plans = []
            for export, scan in scans.items():
                checks = client.check_existing((i.sha1, i.sha1) for i in scan.unique_items())
                plans.append(
                    plan_import(export, scan, checks, state, args.destination_name, args.reimport)
                )
            _print_plans(plans, version, out)
            files = sum(len(p.with_decision(Decision.UPLOAD)) for p in plans)
            size = sum(p.upload_bytes for p in plans)
            if files == 0:
                out.write("Nothing to upload.\n")
                return 0
            question = f"Upload {files} files ({_size(size)}) to Immich at {args.immich_url}?"
            if not args.yes and not confirm(question):
                out.write("Cancelled: nothing was uploaded.\n")
                return 0
            results = [
                run_import(
                    plan,
                    client,
                    state,
                    args.destination_name,
                    lambda: datetime.now(UTC),
                    time.sleep,
                )
                for plan in plans
            ]
    except (ArchiveError, ImmichError, StateError, OSError) as error:
        err.write(f"error: {error}\n")
        return 1
    return _print_results(results, out)


def _fetch(args: argparse.Namespace, out: TextIO, err: TextIO, drive_factory: DriveFactory) -> int:
    try:
        info = load_service_account(args.service_account)
        with State(args.state) as state, drive_factory(args.drive_folder, info) as source:
            files = source.list_archives()
            if not files:
                out.write(
                    "No Takeout archives found. If the folder is not empty, share it with "
                    f"{source.account} as Viewer.\n"
                )
                return 0
            if args.list_only:
                for file in files:
                    seen = state.was_downloaded(source.name, file.file_id, file.fingerprint)
                    label = "downloaded before" if seen and not args.ignore_history else "new"
                    out.write(f"  {label:<18} {_size(file.size):>9}  {file.name}\n")
                return 0
            downloader = Downloader(args.staging, state, lambda: datetime.now(UTC), time.sleep)
            result = downloader.fetch_new(source, ignore_history=args.ignore_history)
    except NotEnoughSpaceError as error:
        err.write(f"error: {error}\n")
        return 1
    except (SourceError, StateError, OSError) as error:
        err.write(f"error: {error}\n")
        return 1
    for file, path in result.downloaded:
        out.write(f"  downloaded  {_size(file.size):>9}  {path}\n")
    out.write(
        f"Downloaded {len(result.downloaded)}, skipped {len(result.skipped)} downloaded before"
    )
    out.write(f", {len(result.failed)} failed\n" if result.failed else "\n")
    for file, detail in result.failed:
        out.write(f"  failed: {file.name}: {detail}; run again to resume\n")
    return 1 if result.failed else 0


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from googich_takeaway.web.app import WebSettings, create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app = create_app(WebSettings(args.state))
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False, server_header=False)
    return 0


def _print_plans(plans: list[ImportPlan], version: str, out: TextIO) -> None:
    out.write(f"Immich {version}\n")
    labels = {
        Decision.UPLOAD: "to upload",
        Decision.IN_IMMICH: "already in Immich",
        Decision.IN_IMMICH_TRASH: "in Immich trash (left alone)",
        Decision.DELETED_IN_IMMICH: "deleted in Immich since uploaded (skipped)",
        Decision.NO_DATE: "no date found (skipped, needs review)",
        Decision.UNSUPPORTED: "rejected by Immich",
    }
    for plan in plans:
        out.write(f"Export {plan.export_id}\n")
        for decision, label in labels.items():
            count = len(plan.with_decision(decision))
            if count:
                extra = f" ({_size(plan.upload_bytes)})" if decision is Decision.UPLOAD else ""
                out.write(f"  {count:>6}  {label}{extra}\n")
        for planned in plan.with_decision(Decision.NO_DATE):
            out.write(f"          review: {planned.item.path}\n")


def _print_results(results: list[ImportResult], out: TextIO) -> int:
    uploaded = sum(len(r.uploaded) for r in results)
    adopted = sum(len(r.adopted) for r in results)
    verified = sum(len(r.verified) for r in results)
    out.write(f"Uploaded {uploaded}, verified {verified}")
    out.write(f", {adopted} were already there\n" if adopted else "\n")
    problems = 0
    for result in results:
        for item, detail in result.date_mismatch:
            out.write(f"  date mismatch: {item.path}: {detail}\n")
            problems += 1
        for item in result.unverified:
            out.write(f"  not yet checked (Immich still processing): {item.path}\n")
        for item, detail in result.failed:
            out.write(f"  failed: {item.path}: {detail}\n")
            problems += 1
        if result.aborted:
            out.write(f"  {result.aborted}; run again to continue\n")
            problems += 1
    return 1 if problems else 0


def _find_archives(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        candidates = sorted(path.iterdir()) if path.is_dir() else [path]
        for candidate in candidates:
            try:
                archive_format(candidate)
            except ArchiveError:
                continue
            if candidate.is_file():
                found.append(candidate)
    return found


def _print_report(
    scans: dict[str, ExportScan],
    statuses: dict[str, list[ItemStatus]],
    version: str | None,
    listing: bool,
    out: TextIO,
) -> None:
    for export, scan in scans.items():
        parts = [archive.name for archive in scan.archives]
        total = sum(i.size for i in scan.items)
        copies = len(scan.items) - len(scan.unique_items())
        dates = Counter(i.date.source.value if i.date else "none" for i in scan.unique_items())
        paired = sum(1 for i in scan.unique_items() if i.sidecar)
        out.write(f"Export {export}: {len(parts)} part(s), {_size(total)}\n")
        out.write(
            f"  Media files   {len(scan.items)}"
            + (f" ({copies} identical copies in other folders)" if copies else "")
            + "\n"
        )
        out.write(f"  Unique        {len(scan.unique_items())}\n")
        out.write(
            "  Dates         "
            + ", ".join(
                f"{k} {dates.get(k, 0)}" for k in ("exif", "sidecar", "video", "filename", "none")
            )
            + "\n"
        )
        out.write(f"  Sidecars      {paired} paired, {len(scan.unmatched_sidecars)} unmatched\n")
        if scan.motion_companions:
            out.write(
                f"  Motion videos {len(scan.motion_companions)} Pixel .MP copies skipped "
                "(already embedded in the photo)\n"
            )
        items = statuses[export]
        if version is not None:
            labels = Counter(s.label for s in items)
            new_bytes = sum(s.item.size for s in items if s.label == "new")
            out.write(
                f"  Immich {version}  {labels.get('new', 0)} new ({_size(new_bytes)}), "
                f"{labels.get('in-immich', 0)} already there, "
                f"{labels.get('in-immich-trash', 0)} in Immich trash"
            )
            other = len(items) - sum(
                labels.get(k, 0) for k in ("new", "in-immich", "in-immich-trash")
            )
            out.write(f", {other} rejected\n" if other else "\n")
        review = [s.item.path for s in items if s.item.date is None]
        if review:
            out.write("  No date found (needs review):\n")
            for path in review:
                out.write(f"    {path}\n")
        if scan.unmatched_sidecars:
            out.write("  Sidecars with no media file:\n")
            for path in scan.unmatched_sidecars:
                out.write(f"    {path}\n")
        if listing:
            out.write("  Files:\n")
            for status in items:
                date = status.item.date.xmp_value() if status.item.date else "no date"
                out.write(f"    {status.label:<15} {date:<25} {status.item.path}\n")
        out.write("\n")
    out.write("Dry run: nothing was uploaded or changed.\n")


def _as_json(
    scans: dict[str, ExportScan],
    statuses: dict[str, list[ItemStatus]],
    version: str | None,
) -> dict[str, object]:
    return {
        "immich_version": version,
        "exports": {
            export: {
                "parts": [archive.name for archive in scan.archives],
                "unmatched_sidecars": scan.unmatched_sidecars,
                "files": [
                    {
                        "path": s.item.path,
                        "archive": s.item.archive.name,
                        "size": s.item.size,
                        "sha1": s.item.sha1,
                        "kind": s.item.kind.value,
                        "status": s.label,
                        "date": s.item.date.xmp_value() if s.item.date else None,
                        "date_source": s.item.date.source.value if s.item.date else None,
                        "offset_source": (s.item.date.offset_source.value if s.item.date else None),
                        "sidecar": s.item.sidecar,
                        "match_rule": s.item.match_rule.value,
                        "copies": [i.path for i in scan.items if i.duplicate_of == s.item.path],
                    }
                    for s in statuses[export]
                ],
            }
            for export, scan in scans.items()
        },
    }


def _size(value: int) -> str:
    size = float(value)
    for unit in ("B", "kB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"


if __name__ == "__main__":
    raise SystemExit(main())
