"""Local state: what has been uploaded, so nothing is processed twice.

SQLite, one file. The schema is versioned with ``PRAGMA user_version`` and upgraded in place;
every upgrade step runs in a transaction. Times are stored as UTC ISO 8601 strings and are always
passed in by the caller, so behaviour is reproducible in tests.
"""

import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

SCHEMA_VERSION = 5

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
    3: """
        CREATE TABLE web_user (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            password_hash TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            changed_at    TEXT NOT NULL
        ) STRICT;
        CREATE TABLE web_sessions (
            token_hash  TEXT PRIMARY KEY,
            csrf_token  TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            last_seen   TEXT NOT NULL,
            expires_at  TEXT NOT NULL
        ) STRICT;
    """,
    4: """
        CREATE TABLE settings (
            name       TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        ) STRICT;
        CREATE TABLE secrets (
            name       TEXT PRIMARY KEY,
            sealed     TEXT NOT NULL,
            updated_at TEXT NOT NULL
        ) STRICT;
        CREATE TABLE sources (
            id         INTEGER PRIMARY KEY,
            kind       TEXT NOT NULL CHECK (kind IN ('gdrive', 'local')),
            name       TEXT NOT NULL UNIQUE,
            location   TEXT NOT NULL,
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        ) STRICT;
    """,
    5: """
        CREATE TABLE runs (
            id          INTEGER PRIMARY KEY,
            trigger     TEXT NOT NULL,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            status      TEXT,
            title       TEXT,
            details     TEXT
        ) STRICT;
        CREATE TABLE completed_exports (
            export_key   TEXT PRIMARY KEY,
            export_id    TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            summary      TEXT NOT NULL
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


@dataclass(frozen=True)
class SourceRecord:
    id: int
    kind: str
    """``gdrive`` or ``local``."""
    name: str
    location: str
    """Drive folder ID, or local folder path."""
    enabled: bool


@dataclass(frozen=True)
class RunRecord:
    id: int
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    status: str | None
    title: str | None
    details: str | None


class StateError(Exception):
    """The state database cannot be used."""


class State:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            # Owner-only: the database lists every photo path and, later, holds credentials.
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        # Explicit transactions only. A State belongs to one task (a web request, a job) at a time,
        # which may hop between threads, so the same-thread check is off.
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
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

    def upload_count(self, destination: str) -> int:
        row = self._db.execute(
            "SELECT count(*) FROM uploads WHERE destination = ?", (destination,)
        ).fetchone()
        return int(row[0])

    def download_totals(self) -> tuple[int, int]:
        """Distinct archives ever downloaded, and their total size."""
        row = self._db.execute(
            "SELECT count(*), coalesce(sum(size), 0) FROM ("
            "SELECT source, file_id, max(size) AS size FROM downloads GROUP BY source, file_id)"
        ).fetchone()
        return int(row[0]), int(row[1])

    def unverified_uploads(self, destination: str, limit: int) -> list[UploadRecord]:
        """Uploads Immich has not confirmed yet, oldest first."""
        rows = self._db.execute(
            "SELECT * FROM uploads WHERE destination = ? AND status = ? "
            "ORDER BY uploaded_at, sha1 LIMIT ?",
            (destination, UploadStatus.UPLOADED.value, limit),
        )
        return [_record(row) for row in rows]

    def verification_counts(self, destination: str, export_id: str | None = None) -> dict[str, int]:
        """Upload counts by status, for one export or all of them."""
        query = "SELECT status, count(*) FROM uploads WHERE destination = ?"
        args: tuple[str, ...] = (destination,)
        if export_id is not None:
            query += " AND export_id = ?"
            args = (destination, export_id)
        rows = self._db.execute(query + " GROUP BY status", args)
        return {str(row[0]): int(row[1]) for row in rows}

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

    def download_times(self) -> list[datetime]:
        """When each archive was downloaded, oldest first."""
        rows = self._db.execute("SELECT DISTINCT downloaded_at FROM downloads ORDER BY 1")
        return [datetime.fromisoformat(row[0]) for row in rows]

    def download_dates(self) -> tuple[datetime | None, datetime | None]:
        """When the first and the latest archive were downloaded, from any source."""
        row = self._db.execute(
            "SELECT min(downloaded_at), max(downloaded_at) FROM downloads"
        ).fetchone()
        return (
            datetime.fromisoformat(row[0]) if row[0] else None,
            datetime.fromisoformat(row[1]) if row[1] else None,
        )

    def downloads(self, source: str) -> list[DownloadRecord]:
        rows = self._db.execute(
            "SELECT * FROM downloads WHERE source = ? ORDER BY downloaded_at, name", (source,)
        )
        return [_download(row) for row in rows]

    # --- web login ------------------------------------------------------------------------------

    def password_hash(self) -> str | None:
        row = self._db.execute("SELECT password_hash FROM web_user WHERE id = 1").fetchone()
        return str(row[0]) if row else None

    def set_password_hash(self, value: str, at: datetime) -> None:
        self._db.execute(
            """
            INSERT INTO web_user (id, password_hash, created_at, changed_at) VALUES (1, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET password_hash = excluded.password_hash,
                                           changed_at = excluded.changed_at
            """,
            (value, _to_text(at), _to_text(at)),
        )

    def create_session(
        self, token_hash: str, csrf_token: str, at: datetime, expires: datetime
    ) -> None:
        self._db.execute(
            "INSERT INTO web_sessions (token_hash, csrf_token, created_at, last_seen, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token_hash, csrf_token, _to_text(at), _to_text(at), _to_text(expires)),
        )

    def get_session(self, token_hash: str, at: datetime) -> str | None:
        """CSRF token of a live session, or None. Expired sessions are removed."""
        self._db.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (_to_text(at),))
        row = self._db.execute(
            "SELECT csrf_token FROM web_sessions WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if row is None:
            return None
        self._db.execute(
            "UPDATE web_sessions SET last_seen = ? WHERE token_hash = ?",
            (_to_text(at), token_hash),
        )
        return str(row[0])

    def delete_session(self, token_hash: str) -> None:
        self._db.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))

    def delete_all_sessions(self) -> None:
        self._db.execute("DELETE FROM web_sessions")

    def clear_password(self) -> None:
        """Forget the web password and log everyone out; the next start offers setup again."""
        self._db.execute("DELETE FROM web_user")
        self.delete_all_sessions()

    # --- settings, secrets and sources ----------------------------------------------------------

    def get_setting(self, name: str) -> str | None:
        row = self._db.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
        return str(row[0]) if row else None

    def set_setting(self, name: str, value: str | None, at: datetime) -> None:
        if value is None:
            self._db.execute("DELETE FROM settings WHERE name = ?", (name,))
            return
        self._db.execute(
            "INSERT INTO settings (name, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (name) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (name, value, _to_text(at)),
        )

    def get_sealed(self, name: str) -> str | None:
        row = self._db.execute("SELECT sealed FROM secrets WHERE name = ?", (name,)).fetchone()
        return str(row[0]) if row else None

    def set_sealed(self, name: str, sealed: str | None, at: datetime) -> None:
        if sealed is None:
            self._db.execute("DELETE FROM secrets WHERE name = ?", (name,))
            return
        self._db.execute(
            "INSERT INTO secrets (name, sealed, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (name) DO UPDATE SET sealed = excluded.sealed, "
            "updated_at = excluded.updated_at",
            (name, sealed, _to_text(at)),
        )

    def add_source(self, kind: str, name: str, location: str, at: datetime) -> int:
        try:
            cursor = self._db.execute(
                "INSERT INTO sources (kind, name, location, created_at) VALUES (?, ?, ?, ?)",
                (kind, name, location, _to_text(at)),
            )
        except sqlite3.IntegrityError as error:
            raise StateError(f"a source named {name!r} already exists") from error
        return int(cursor.lastrowid or 0)

    def sources(self) -> list[SourceRecord]:
        rows = self._db.execute("SELECT * FROM sources ORDER BY name").fetchall()
        return [_source(row) for row in rows]

    def get_source(self, source_id: int) -> SourceRecord | None:
        row = self._db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return _source(row) if row else None

    def set_source_enabled(self, source_id: int, enabled: bool) -> None:
        self._db.execute("UPDATE sources SET enabled = ? WHERE id = ?", (int(enabled), source_id))

    def delete_source(self, source_id: int) -> None:
        self._db.execute("DELETE FROM sources WHERE id = ?", (source_id,))

    # --- runs and completed exports ------------------------------------------------------------

    def start_run(self, trigger: str, at: datetime) -> int:
        cursor = self._db.execute(
            "INSERT INTO runs (trigger, started_at) VALUES (?, ?)", (trigger, _to_text(at))
        )
        return int(cursor.lastrowid or 0)

    def finish_run(self, run_id: int, status: str, title: str, details: str, at: datetime) -> None:
        self._db.execute(
            "UPDATE runs SET finished_at = ?, status = ?, title = ?, details = ? WHERE id = ?",
            (_to_text(at), status, title, details, run_id),
        )

    def recent_runs(self, limit: int = 20) -> list[RunRecord]:
        rows = self._db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [
            RunRecord(
                id=row["id"],
                trigger=row["trigger"],
                started_at=_from_text(row["started_at"]),
                finished_at=_from_text(row["finished_at"]) if row["finished_at"] else None,
                status=row["status"],
                title=row["title"],
                details=row["details"],
            )
            for row in rows
        ]

    def abandon_unfinished_runs(self, at: datetime) -> int:
        """Runs left open by a crash or restart are closed as interrupted."""
        cursor = self._db.execute(
            "UPDATE runs SET finished_at = ?, status = 'interrupted', "
            "title = 'Interrupted (the app stopped during the run)' WHERE finished_at IS NULL",
            (_to_text(at),),
        )
        return cursor.rowcount

    def is_export_complete(self, export_key: str) -> bool:
        return self.export_completed_at(export_key) is not None

    def export_completed_at(self, export_key: str) -> datetime | None:
        row = self._db.execute(
            "SELECT completed_at FROM completed_exports WHERE export_key = ?", (export_key,)
        ).fetchone()
        return _from_text(row[0]) if row else None

    def export_summary(self, export_key: str) -> dict[str, object] | None:
        row = self._db.execute(
            "SELECT summary FROM completed_exports WHERE export_key = ?", (export_key,)
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row[0])
        return data if isinstance(data, dict) else {}

    def uploaded_hashes_for_export(self, destination: str, export_id: str) -> list[str]:
        rows = self._db.execute(
            "SELECT sha1 FROM uploads WHERE destination = ? AND export_id = ? ORDER BY sha1",
            (destination, export_id),
        )
        return [row[0] for row in rows]

    def mark_export_complete(
        self, export_key: str, export_id: str, summary: str, at: datetime
    ) -> None:
        self._db.execute(
            "INSERT INTO completed_exports (export_key, export_id, completed_at, summary) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (export_key) DO UPDATE SET "
            "completed_at = excluded.completed_at, summary = excluded.summary",
            (export_key, export_id, _to_text(at), summary),
        )


def _source(row: sqlite3.Row) -> SourceRecord:
    return SourceRecord(
        id=row["id"],
        kind=row["kind"],
        name=row["name"],
        location=row["location"],
        enabled=bool(row["enabled"]),
    )


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
