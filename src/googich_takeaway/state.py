"""Local state: what has been uploaded, so nothing is processed twice.

SQLite, one file. The schema is versioned with ``PRAGMA user_version`` and upgraded in place;
every upgrade step runs in a transaction. Times are stored as UTC ISO 8601 strings and are always
passed in by the caller, so behaviour is reproducible in tests.
"""

import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

SCHEMA_VERSION = 2

_MIGRATIONS: dict[int, str] = {
    1: """
        CREATE TABLE uploads (
            destination  TEXT NOT NULL,
            sha1         TEXT NOT NULL,
            asset_id     TEXT NOT NULL,
            status       TEXT NOT NULL,
            export_id    TEXT NOT NULL,
            archive      TEXT NOT NULL,
            path         TEXT NOT NULL,
            capture_date TEXT,
            uploaded_at  TEXT NOT NULL,
            verified_at  TEXT,
            detail       TEXT,
            PRIMARY KEY (destination, sha1)
        ) STRICT;
    """,
    2: """
        CREATE TABLE downloads (
            source        TEXT NOT NULL,
            file_id       TEXT NOT NULL,
            fingerprint   TEXT NOT NULL,
            name          TEXT NOT NULL,
            size          INTEGER NOT NULL,
            link          TEXT,
            local_path    TEXT NOT NULL,
            downloaded_at TEXT NOT NULL,
            forgotten_at  TEXT,
            removed_at    TEXT,
            PRIMARY KEY (source, file_id, fingerprint)
        ) STRICT;
    """,
}


class UploadStatus(StrEnum):
    UPLOADED = "uploaded"
    """Sent to the destination; date not yet checked."""
    ADOPTED = "adopted"
    """Already present when the app tried to upload it, e.g. after an interrupted run."""
    VERIFIED = "verified"
    """Read back from the destination with the expected date."""
    DATE_MISMATCH = "date-mismatch"
    """Read back with a different date from the one sent."""


@dataclass(frozen=True)
class UploadRecord:
    destination: str
    sha1: str
    asset_id: str
    status: UploadStatus
    export_id: str
    archive: str
    path: str
    capture_date: str | None
    uploaded_at: datetime
    verified_at: datetime | None
    detail: str | None


@dataclass(frozen=True)
class DownloadRecord:
    source: str
    file_id: str
    fingerprint: str
    name: str
    size: int
    link: str | None
    local_path: str
    downloaded_at: datetime
    forgotten_at: datetime | None
    removed_at: datetime | None
    """When the file was found to be gone from the source (deleted by the user)."""


class StateError(Exception):
    """The state database cannot be used."""


class State:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            # Owner-only: the database lists every photo path and, later, holds credentials.
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        self._db = sqlite3.connect(path, isolation_level=None)  # explicit transactions only
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._migrate()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        return int(self._db.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        current = self.schema_version
        if current > SCHEMA_VERSION:
            raise StateError(
                f"state database is from a newer version (schema {current}, "
                f"this version understands {SCHEMA_VERSION}); upgrade the app"
            )
        for version in range(current + 1, SCHEMA_VERSION + 1):
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for statement in _statements(_MIGRATIONS[version]):
                    self._db.execute(statement)
                self._db.execute(f"PRAGMA user_version = {version}")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def record_upload(self, record: UploadRecord) -> None:
        self._db.execute(
            """
            INSERT INTO uploads (destination, sha1, asset_id, status, export_id, archive, path,
                                 capture_date, uploaded_at, verified_at, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (destination, sha1) DO UPDATE SET
                asset_id = excluded.asset_id,
                status = excluded.status,
                export_id = excluded.export_id,
                archive = excluded.archive,
                path = excluded.path,
                capture_date = excluded.capture_date,
                uploaded_at = excluded.uploaded_at,
                verified_at = excluded.verified_at,
                detail = excluded.detail
            """,
            (
                record.destination,
                record.sha1,
                record.asset_id,
                record.status.value,
                record.export_id,
                record.archive,
                record.path,
                record.capture_date,
                _to_text(record.uploaded_at),
                _to_text(record.verified_at) if record.verified_at else None,
                record.detail,
            ),
        )

    def mark_verified(
        self,
        destination: str,
        sha1: str,
        status: UploadStatus,
        at: datetime,
        detail: str | None = None,
    ) -> None:
        cursor = self._db.execute(
            "UPDATE uploads SET status = ?, verified_at = ?, detail = ? "
            "WHERE destination = ? AND sha1 = ?",
            (status.value, _to_text(at), detail, destination, sha1),
        )
        if cursor.rowcount != 1:
            raise StateError(f"no upload recorded for {sha1} at {destination}")

    def get_upload(self, destination: str, sha1: str) -> UploadRecord | None:
        row = self._db.execute(
            "SELECT * FROM uploads WHERE destination = ? AND sha1 = ?", (destination, sha1)
        ).fetchone()
        return _record(row) if row else None

    def uploaded_hashes(self, destination: str, sha1s: Iterable[str]) -> set[str]:
        """Which of ``sha1s`` this app has ever uploaded to ``destination``."""
        wanted = list(sha1s)
        found: set[str] = set()
        for start in range(0, len(wanted), 500):
            batch = wanted[start : start + 500]
            marks = ",".join("?" * len(batch))
            rows = self._db.execute(
                f"SELECT sha1 FROM uploads WHERE destination = ? AND sha1 IN ({marks})",  # noqa: S608
                (destination, *batch),
            )
            found.update(row[0] for row in rows)
        return found

    def uploads(self, destination: str) -> list[UploadRecord]:
        rows = self._db.execute(
            "SELECT * FROM uploads WHERE destination = ? ORDER BY uploaded_at, sha1",
            (destination,),
        )
        return [_record(row) for row in rows]

    # --- download history ---------------------------------------------------------------------

    def record_download(self, record: DownloadRecord) -> None:
        self._db.execute(
            """
            INSERT INTO downloads (source, file_id, fingerprint, name, size, link, local_path,
                                   downloaded_at, forgotten_at, removed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            ON CONFLICT (source, file_id, fingerprint) DO UPDATE SET
                name = excluded.name,
                size = excluded.size,
                link = excluded.link,
                local_path = excluded.local_path,
                downloaded_at = excluded.downloaded_at,
                forgotten_at = NULL,
                removed_at = NULL
            """,
            (
                record.source,
                record.file_id,
                record.fingerprint,
                record.name,
                record.size,
                record.link,
                record.local_path,
                _to_text(record.downloaded_at),
            ),
        )

    def was_downloaded(self, source: str, file_id: str, fingerprint: str) -> bool:
        """True if this exact file content was downloaded before and not forgotten."""
        row = self._db.execute(
            "SELECT 1 FROM downloads WHERE source = ? AND file_id = ? AND fingerprint = ? "
            "AND forgotten_at IS NULL",
            (source, file_id, fingerprint),
        ).fetchone()
        return row is not None

    def forget_download(self, source: str, file_id: str, at: datetime) -> int:
        """Make the next fetch download ``file_id`` again. Returns rows changed."""
        cursor = self._db.execute(
            "UPDATE downloads SET forgotten_at = ? "
            "WHERE source = ? AND file_id = ? AND forgotten_at IS NULL",
            (_to_text(at), source, file_id),
        )
        return cursor.rowcount

    def mark_removed_from_source(
        self, source: str, present_ids: Iterable[str], at: datetime
    ) -> int:
        """Record that downloaded files not in ``present_ids`` are gone from the source."""
        present = set(present_ids)
        rows = self._db.execute(
            "SELECT file_id FROM downloads WHERE source = ? AND removed_at IS NULL", (source,)
        ).fetchall()
        gone = {row[0] for row in rows} - present
        for file_id in sorted(gone):
            self._db.execute(
                "UPDATE downloads SET removed_at = ? WHERE source = ? AND file_id = ? "
                "AND removed_at IS NULL",
                (_to_text(at), source, file_id),
            )
        return len(gone)

    def downloads(self, source: str) -> list[DownloadRecord]:
        rows = self._db.execute(
            "SELECT * FROM downloads WHERE source = ? ORDER BY downloaded_at, name", (source,)
        )
        return [_download(row) for row in rows]


def _download(row: sqlite3.Row) -> DownloadRecord:
    return DownloadRecord(
        source=row["source"],
        file_id=row["file_id"],
        fingerprint=row["fingerprint"],
        name=row["name"],
        size=row["size"],
        link=row["link"],
        local_path=row["local_path"],
        downloaded_at=_from_text(row["downloaded_at"]),
        forgotten_at=_from_text(row["forgotten_at"]) if row["forgotten_at"] else None,
        removed_at=_from_text(row["removed_at"]) if row["removed_at"] else None,
    )


def _statements(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]


def _to_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("state times must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _from_text(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _record(row: sqlite3.Row) -> UploadRecord:
    return UploadRecord(
        destination=row["destination"],
        sha1=row["sha1"],
        asset_id=row["asset_id"],
        status=UploadStatus(row["status"]),
        export_id=row["export_id"],
        archive=row["archive"],
        path=row["path"],
        capture_date=row["capture_date"],
        uploaded_at=_from_text(row["uploaded_at"]),
        verified_at=_from_text(row["verified_at"]) if row["verified_at"] else None,
        detail=row["detail"],
    )
