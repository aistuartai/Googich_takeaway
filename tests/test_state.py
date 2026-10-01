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
        assert len(state.uploads("immich")) == 1
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
