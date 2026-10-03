import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from googich_takeaway.state import (
    SCHEMA_VERSION,
    State,
    StateError,
    UploadRecord,
    UploadStatus,
)

AT = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


def all_uploads(state: State, destination: str) -> list[UploadRecord]:
    """Every upload record, for checking in tests."""
    rows = state._db.execute(
        "SELECT sha1 FROM uploads WHERE destination = ? ORDER BY sha1", (destination,)
    ).fetchall()
    return [r for r in (state.get_upload(destination, row[0]) for row in rows) if r is not None]


def record(sha1: str = "a" * 40, status: UploadStatus = UploadStatus.UPLOADED) -> UploadRecord:
    return UploadRecord(
        destination="immich",
        sha1=sha1,
        asset_id="asset-1",
        status=status,
        export_id="20261001T010203Z",
        archive="takeout-20261001T010203Z-001.zip",
        path="Takeout/Google Photos/Photos from 2019/IMG_1.jpg",
        capture_date="2019-07-04T10:15:00+10:00",
        uploaded_at=AT,
        verified_at=None,
        detail=None,
    )


def test_new_database_is_at_current_schema(tmp_path: Path) -> None:
    with State(tmp_path / "state.db") as state:
        assert state.schema_version == SCHEMA_VERSION


def test_reopening_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.db"
    with State(path) as state:
        state.record_upload(record())
    with State(path) as state:
        assert state.get_upload("immich", "a" * 40) == record()


def test_record_upload_replaces_existing(tmp_path: Path) -> None:
    with State(tmp_path / "state.db") as state:
        state.record_upload(record())
        state.record_upload(record(status=UploadStatus.ADOPTED))
        assert len(all_uploads(state, "immich")) == 1
        found = state.get_upload("immich", "a" * 40)
        assert found is not None
        assert found.status is UploadStatus.ADOPTED


def test_mark_verified(tmp_path: Path) -> None:
    with State(tmp_path / "state.db") as state:
        state.record_upload(record())
        state.mark_verified("immich", "a" * 40, UploadStatus.VERIFIED, AT)
        found = state.get_upload("immich", "a" * 40)
        assert found is not None
        assert found.status is UploadStatus.VERIFIED
        assert found.verified_at == AT
        with pytest.raises(StateError, match="no upload"):
            state.mark_verified("immich", "b" * 40, UploadStatus.VERIFIED, AT)


def test_uploaded_hashes_is_per_destination_and_batches(tmp_path: Path) -> None:
    with State(tmp_path / "state.db") as state:
        hashes = [f"{i:040x}" for i in range(1200)]
        for sha1 in hashes[:700]:
            state.record_upload(record(sha1))
        assert state.uploaded_hashes("immich", hashes) == set(hashes[:700])
        assert state.uploaded_hashes("other", hashes) == set()


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    State(path).close()
    with sqlite3.connect(path) as raw:
        raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(StateError, match="newer version"):
        State(path)


def test_naive_times_are_rejected(tmp_path: Path) -> None:
    bad = record()
    bad = UploadRecord(**{**bad.__dict__, "uploaded_at": datetime(2026, 1, 1)})
    with State(tmp_path / "state.db") as state, pytest.raises(ValueError, match="aware"):
        state.record_upload(bad)


def test_database_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    State(path).close()
    assert path.stat().st_mode & 0o077 == 0


def test_version_1_database_is_upgraded_keeping_uploads(tmp_path: Path) -> None:
    from googich_takeaway import state as state_module

    path = tmp_path / "state.db"
    with sqlite3.connect(path) as raw:
        for statement in state_module._statements(state_module._MIGRATIONS[1]):
            raw.execute(statement)
        raw.execute(
            "INSERT INTO uploads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
            (
                "immich", "a" * 40, "asset-1", "uploaded", "20261001T010203Z",
                "takeout-20261001T010203Z-001.zip",
                "Takeout/Google Photos/Photos from 2019/IMG_1.jpg",
                "2019-07-04T10:15:00+10:00", "2026-10-01T10:00:00+00:00",
            ),
        )  # fmt: skip
        raw.execute("PRAGMA user_version = 1")
    with State(path) as state:
        assert state.schema_version == SCHEMA_VERSION
        assert state.get_upload("immich", "a" * 40) == record()
        assert state.downloads("anything") == []
        assert state.password_hash() is None


def test_version_6_database_keeps_its_sources_and_counts_seen_items(tmp_path: Path) -> None:
    from googich_takeaway import state as state_module

    path = tmp_path / "state.db"
    with sqlite3.connect(path) as raw:
        for version in range(1, 7):
            for statement in state_module._statements(state_module._MIGRATIONS[version]):
                raw.execute(statement)
        raw.execute(
            "INSERT INTO sources (kind, name, location, created_at) VALUES "
            "('gdrive', 'Takeout', 'folder-1', '2026-10-01T00:00:00+00:00')"
        )
        raw.execute("PRAGMA user_version = 6")
    with State(path) as state:
        assert state.schema_version == SCHEMA_VERSION
        assert [(s.kind, s.name, s.location) for s in state.sources()] == [
            ("gdrive", "Takeout", "folder-1")
        ]
        state.add_source("download-folder", "Downloads", "download-folder", AT)
        assert len(state.sources()) == 2


def test_sessions_are_checked_without_writing_every_time(tmp_path: Path) -> None:
    from datetime import timedelta

    with State(tmp_path / "state.db") as state:
        state.create_session("h", "csrf", AT, AT + timedelta(days=7))
        assert state.get_session("h", AT + timedelta(seconds=30)) == "csrf"
        seen = state._db.execute("SELECT last_seen FROM web_sessions").fetchone()[0]
        assert seen == AT.isoformat()  # under a minute: not written
        assert state.get_session("h", AT + timedelta(minutes=5)) == "csrf"
        seen = state._db.execute("SELECT last_seen FROM web_sessions").fetchone()[0]
        assert seen == (AT + timedelta(minutes=5)).isoformat()
        assert state.get_session("h", AT + timedelta(days=8)) is None  # expired
        assert state._db.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


def test_a_failed_batch_leaves_no_transaction_open(tmp_path: Path) -> None:
    def fail_in_the_middle(state: State) -> None:
        with state.transaction():
            state.start_run("manual", AT)
            raise sqlite3.OperationalError("disk I/O error")

    with State(tmp_path / "s.db") as state:
        with pytest.raises(sqlite3.OperationalError):
            fail_in_the_middle(state)
        assert not state._db.in_transaction
        assert state.recent_runs() == []  # rolled back
        with state.transaction():  # a later batch still commits
            state.start_run("manual", AT)
        assert len(state.recent_runs()) == 1


def test_media_read_counts_distinct_photos_in_parts_read_to_the_end(tmp_path: Path) -> None:
    import json as js

    def media(sha1: str) -> str:
        return js.dumps({"kind": "media", "size": 1, "sha1": sha1})

    with State(tmp_path / "state.db") as state:
        at = datetime(2026, 10, 1, tzinfo=UTC)
        state.save_scan_part(
            "a.zip:1", [("x.jpg", media("1")), ("album/x.jpg", media("1"))], True, at
        )
        state.save_scan_part(
            "b.zip:1", [("y.jpg", media("2")), ("y.json", '{"kind":"sidecar"}')], True, at
        )
        state.save_scan_part("c.zip:1", [("z.jpg", media("3"))], False, at)  # still being read
        assert state.media_read(["a.zip:1", "b.zip:1", "c.zip:1", "d.zip:1"]) == (2, 2)
        assert state.media_read([]) == (0, 0)
