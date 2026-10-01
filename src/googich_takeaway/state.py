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

SCHEMA_VERSION = 1

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
