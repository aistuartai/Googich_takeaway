"""Command-line interface.

``googich scan`` reads Takeout archives and reports what an import would do. With an Immich URL
and key file it also asks Immich which files it already has. It never uploads or changes anything.
"""

import argparse
import json
import os
import sys
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
from googich_takeaway.takeout.archives import ArchiveError, archive_format, group_exports
from googich_takeaway.takeout.dates import DateResolver
from googich_takeaway.takeout.scan import ExportScan, ScannedItem, scan_export

ClientFactory = Callable[[str, str], ImmichClient]


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
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command != "scan":
        parser.print_help(err)
        return 2
    return _scan(args, out, err, client_factory)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="googich", description=__doc__.split("\n")[0])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")
    scan = commands.add_parser(
        "scan",
        help="report what importing Takeout archives would do (never uploads)",
        description="Scan Takeout archives and report what an import would do. Read-only.",
    )
    scan.add_argument("paths", nargs="+", type=Path, help="archives, or folders containing them")
    scan.add_argument(
        "--timezone",
        default=os.environ.get("TZ") or "UTC",
        help="time zone for photos with no other clue (default: $TZ or UTC)",
    )
    scan.add_argument(
        "--immich-url",
        default=os.environ.get("GOOGICH_IMMICH_URL"),
        help="Immich server, e.g. http://immich:2283 (or $GOOGICH_IMMICH_URL)",
    )
    scan.add_argument(
        "--key-file",
        type=Path,
        default=os.environ.get("GOOGICH_IMMICH_KEY_FILE"),
        help="file holding the Immich API key, mode 600 (or $GOOGICH_IMMICH_KEY_FILE)",
    )
    scan.add_argument("--list", action="store_true", help="list every file and its outcome")
    scan.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


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
        parts = sorted({i.archive.name for i in scan.items})
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
                "parts": sorted({i.archive.name for i in scan.items}),
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
