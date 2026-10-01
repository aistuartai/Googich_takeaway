import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from googich_takeaway.config import Config
from googich_takeaway.credentials import SecretBox, load_master_key
from googich_takeaway.destinations.immich import ImmichClient
from googich_takeaway.sources.gdrive import GoogleDriveSource
from googich_takeaway.state import State
from googich_takeaway.web.app import WebSettings, create_app
from tests.fake_drive import ACCOUNT, FOLDER, FakeDrive, service_account_info
from tests.fake_immich import KEY, FakeImmichServer
from tests.web.test_app import ORIGIN, PASSWORD

FOLDER_ID = FOLDER + "-abcdefghij"


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.immich = FakeImmichServer()
        self.drive = FakeDrive()
        self.drive.add("a", "takeout-20261001T010203Z-001.zip", b"x" * 1000)
        app = create_app(
            WebSettings(tmp_path / "state.db"),
            immich_factory=lambda url, key: ImmichClient(url, key, self.immich.transport()),
            drive_factory=lambda folder, info: GoogleDriveSource(
                FOLDER, info, self.drive.transport()
            ),
            start_worker=False,
        )
        self.client = TestClient(app, follow_redirects=False)
        token = app.state.setup_token
        self.client.post(
            "/setup",
            data={"token": token, "password": PASSWORD, "confirm": PASSWORD},
            headers=ORIGIN,
        )
        page = self.client.get("/").text
        found = re.search(r'name="csrf_token" value="([^"]+)"', page)
        assert found
        self.csrf = found[1]
        self.tmp = tmp_path

    def post(
        self,
        url: str,
        data: dict[str, str | list[str]] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> httpx.Response:
        form = {**(data or {}), "csrf_token": self.csrf}
        response: httpx.Response = self.client.post(url, data=form, files=files, headers=ORIGIN)
        return response

    def htmx(self, url: str) -> str:
        response = self.client.post(url, headers={**ORIGIN, "X-CSRF-Token": self.csrf})
        assert response.status_code == 200
        return str(response.text)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_dashboard_starts_with_a_checklist(world: World) -> None:
    page = world.client.get("/").text
    assert "Getting started" in page
    assert "Open Immich" not in page


def test_immich_settings_saved_tested_and_key_never_shown(world: World) -> None:
    response = world.post(
        "/settings/immich",
        data={
            "url": "http://immich.test:2283",
            "public_url": "https://photos.example",
            "api_key": KEY,
        },
    )
    assert response.status_code == 303
    page = world.client.get("/settings").text
    assert KEY not in page
    assert "Saved. Leave blank to keep it." in page
    assert 'href="https://photos.example"' in page  # header link uses the public address
    assert "Connected to Immich 2.7.5. The API key is fine." in world.htmx("/settings/immich/test")


def test_immich_test_reports_missing_permissions(world: World) -> None:
    world.immich.permissions = ["asset.upload"]
    world.post("/settings/immich", data={"url": "http://immich.test", "api_key": KEY})
    assert "lacks: asset.read" in world.htmx("/settings/immich/test")


def test_immich_test_reports_bad_key(world: World) -> None:
    world.post("/settings/immich", data={"url": "http://immich.test", "api_key": "wrong-key"})
    assert "rejected the API key" in world.htmx("/settings/immich/test")


def test_invalid_settings_show_errors(world: World) -> None:
    response = world.post("/settings/immich", data={"url": "nope", "api_key": KEY})
    assert response.status_code == 400
    assert "http:// or https://" in response.text


def test_general_settings(world: World) -> None:
    staging = world.tmp / "staging"
    response = world.post(
        "/settings/general", data={"staging": str(staging), "timezone": "Australia/Melbourne"}
    )
    assert response.status_code == 303
    assert staging.is_dir()


def test_add_drive_source_test_it_and_remove_it(world: World) -> None:
    key = json.dumps(service_account_info()).encode()
    response = world.post(
        "/sources/drive",
        data={"name": "Takeout", "folder_id": FOLDER_ID},
        files={"key_file": ("key.json", key, "application/json")},
    )
    assert response.status_code == 303
    page = world.client.get("/sources").text
    assert ACCOUNT in page  # who to share the folder with
    assert "PRIVATE KEY" not in page
    assert FOLDER_ID not in page  # only a short prefix is shown
    source_id = re.search(r"/sources/(\d+)/test", page)
    assert source_id
    assert "1 archives in the folder" in world.htmx(f"/sources/{source_id[1]}/test")
    world.post(f"/sources/{source_id[1]}/delete")
    assert "No sources yet." in world.client.get("/sources").text


def test_bad_key_file_is_rejected(world: World) -> None:
    response = world.post(
        "/sources/drive",
        data={"name": "Takeout", "folder_id": FOLDER_ID},
        files={"key_file": ("key.json", b"{}", "application/json")},
    )
    assert response.status_code == 400
    assert "not a service account key" in response.text


def test_upload_without_csrf_is_refused(world: World) -> None:
    response = world.client.post(
        "/sources/drive",
        data={"name": "Takeout", "folder_id": FOLDER_ID},
        files={"key_file": ("key.json", b"{}", "application/json")},
        headers=ORIGIN,
    )
    assert response.status_code == 403


def test_local_source(world: World) -> None:
    folder = world.tmp / "manual"
    folder.mkdir()
    (folder / "takeout-x-001.zip").write_bytes(b"zip")
    world.post("/sources/local", data={"name": "Manual", "path": str(folder)})
    page = world.client.get("/sources").text
    source_id = re.search(r"/sources/(\d+)/test", page)
    assert source_id
    assert "1 archives" in world.htmx(f"/sources/{source_id[1]}/test")


def test_dashboard_when_ready(world: World) -> None:
    world.post("/settings/immich", data={"url": "http://immich.test", "api_key": KEY})
    world.post("/settings/general", data={"staging": str(world.tmp / "s"), "timezone": "UTC"})
    folder = world.tmp / "manual"
    folder.mkdir()
    world.post("/sources/local", data={"name": "Manual", "path": str(folder)})
    page = world.client.get("/").text
    assert "Getting started" not in page
    assert "1 source." in page
    assert "Run now" in page
    assert "No runs yet." in page


def test_schedule_saved_and_shown(world: World) -> None:
    response = world.post(
        "/settings/schedule",
        data={
            "mode": "weekly",
            "at": "02:30",
            "weekday": "0",
            "every_hours": "24",
            "pause_after": "3",
        },
    )
    assert response.status_code == 303
    page = world.client.get("/settings").text
    assert 'value="weekly" selected' in page
    bad = world.post("/settings/schedule", data={"mode": "daily", "at": "3pm"})
    assert bad.status_code == 400
    assert "24-hour" in bad.text


def test_notifications_saved_sealed_and_tested(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from googich_takeaway.notify import Message, Notifier

    sent: list[Message] = []

    def capture(self: Notifier, message: Message, force: bool = False) -> bool:
        sent.append(message)
        return True

    monkeypatch.setattr(Notifier, "send", capture)
    secret = "ntfys://tok3n@ntfy.example/photos"  # noqa: S105 - test value
    response = world.post(
        "/settings/notifications", data={"urls": secret, "outcomes": ["failed", "paused"]}
    )
    assert response.status_code == 303
    page = world.client.get("/settings").text
    assert "tok3n" not in page
    assert "Saved. Leave blank to keep them." in page
    assert "Test notification sent." in world.htmx("/settings/notifications/test")
    assert sent[0].title == "Test notification"


def test_run_now_and_resume(world: World) -> None:
    world.post("/settings/immich", data={"url": "http://immich.test", "api_key": KEY})
    world.post("/settings/general", data={"staging": str(world.tmp / "s"), "timezone": "UTC"})
    folder = world.tmp / "manual"
    folder.mkdir()
    world.post("/sources/local", data={"name": "Manual", "path": str(folder)})
    assert world.post("/runs").status_code == 303
    worker = world.client.app.state.worker  # type: ignore[attr-defined]
    assert worker._manual_requested  # queued for the background worker
    box = SecretBox(load_master_key(world.tmp / "master.key"))
    config = Config(State(world.tmp / "state.db"), box, lambda: datetime.now(UTC))
    config.set_schedule_paused(True)
    assert "Schedule paused" in world.client.get("/").text
    assert world.post("/schedule/resume").status_code == 303
    assert "Schedule paused" not in world.client.get("/").text


def test_time_zone_is_a_dropdown(world: World) -> None:
    page = world.client.get("/settings").text
    assert '<select name="timezone"' in page
    assert '<option value="Australia/Melbourne"' in page
    assert '<option value="UTC" selected>' in page


def test_status_poll_reloads_page_when_idle(world: World) -> None:
    response = world.client.get("/status")
    assert response.headers["hx-refresh"] == "true"


def test_run_with_options_reaches_the_worker(world: World) -> None:
    from googich_takeaway.pipeline import RunOptions

    world.post("/runs", data={"reimport": "1"})
    worker = world.client.app.state.worker  # type: ignore[attr-defined]
    assert worker._manual_requested == RunOptions(reimport=True, download_again=False)
