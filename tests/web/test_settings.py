import json
import re
from collections.abc import Mapping
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
        data: Mapping[str, str | list[str]] | None = None,
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
    assert "Bring your Google Photos home" in page
    assert "Immich ↗" not in page


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
    assert "Bring your Google Photos home" not in page
    assert 'class="journey"' in page
    assert "Download folder" in page
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


def test_status_fragment_shows_live_progress(world: World) -> None:
    from googich_takeaway.progress import Stage

    worker = world.client.app.state.worker  # type: ignore[attr-defined]
    worker.tracker.start_run()
    worker.tracker.plan(Stage.DOWNLOAD, [("takeout-x-001.zip", 2_000_000_000)])
    worker.tracker.begin(Stage.DOWNLOAD, "takeout-x-001.zip")
    worker._run_started = datetime.now(UTC)
    fragment = world.client.get("/status").text
    assert "Downloading" in fragment
    assert "takeout-x-001.zip" in fragment
    assert "2.0 GB" in fragment
    assert "estimated from earlier runs" in fragment
    assert "pct-0 tone-0" in fragment
    assert "leg leg-1 flowing" in fragment  # the Drive-to-folder leg animates while downloading
    assert 'hx-trigger="every 2s"' in fragment


def test_log_viewer_tail_and_download(world: World) -> None:
    import logging

    logging.getLogger("googich.test.viewer").warning("viewer test line 1")
    page = world.client.get("/logs?level=WARNING").text
    assert "viewer test line 1" in page
    assert 'hx-trigger="every 3s"' in page
    last = re.findall(r"after=(\d+)", page)[-1]
    logging.getLogger("googich.test.viewer").warning("viewer test line 2")
    tail = world.client.get(f"/logs/tail?after={last}&level=WARNING").text
    assert "viewer test line 2" in tail
    assert "viewer test line 1" not in tail
    assert world.client.get("/logs/download").status_code == 404  # no file in tests
    paused = world.client.get("/logs?follow=0").text
    assert 'hx-trigger="every 3s"' not in paused


def test_pages_disable_htmx_eval(world: World) -> None:
    page = world.client.get("/").text
    assert '"allowEval": false' in page


def test_cleanup_page(world: World) -> None:
    world.post("/settings/general", data={"staging": str(world.tmp / "s"), "timezone": "UTC"})
    (world.tmp / "s" / "takeout-20261001T010203Z-001.zip").write_bytes(b"zip")
    page = world.client.get("/cleanup").text
    assert "Not imported completely yet." in page
    assert "This app never deletes anything in Google Drive." in page
    refused = world.post("/cleanup/staged/20261001T010203Z")
    assert refused.status_code == 303
    assert "not+been+imported" in refused.headers["location"].replace("%20", "+")
    assert (world.tmp / "s" / "takeout-20261001T010203Z-001.zip").exists()


def test_smb_download_folder_saved_through_the_form(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from tests.fake_smb import SMB_LOGIN, FakeSmb

    fake = FakeSmb()
    monkeypatch.setitem(sys.modules, "smbclient", fake)
    form = {
        "storage": "smb",
        "timezone": "UTC",
        "smb_server": "nas.local",
        "smb_share": "Photos",
        "smb_folder": "takeout",
        "smb_username": "photos",
        "smb_password": SMB_LOGIN,
        "smb_port": "445",
    }
    assert world.post("/settings/general", data=form).status_code == 303
    page = world.client.get("/settings").text
    assert SMB_LOGIN not in page
    assert 'value="smb" checked' in page
    assert "Can write to \\\\nas.local\\Photos\\takeout" in world.htmx("/settings/storage/test")

    bad = world.post("/settings/general", data={**form, "smb_password": "wrong"})
    assert bad.status_code == 400
    assert "LOGON_FAILURE" in bad.text


def test_update_banner_and_setting(world: World) -> None:
    import json

    from googich_takeaway.state import State

    with State(world.tmp / "state.db") as state:
        state.set_setting(
            "updates.latest",
            json.dumps(
                {
                    "latest": "9.9.9",
                    "url": "https://github.com/aistuartai/Googich_takeaway/releases",
                    "checked_at": datetime.now(UTC).isoformat(),
                }
            ),
            datetime.now(UTC),
        )
    assert "Version 9.9.9 is available" in world.client.get("/").text
    world.post("/settings/updates", data={})  # switch the check off
    assert "Version 9.9.9 is available" not in world.client.get("/").text


def test_failed_smb_save_keeps_what_was_typed_except_the_password(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from tests.fake_smb import FakeSmb

    monkeypatch.setitem(sys.modules, "smbclient", FakeSmb())
    form = {
        "storage": "smb", "timezone": "Australia/Melbourne", "smb_server": "optimus.local",
        "smb_share": "GoogichDump", "smb_folder": "takeout", "smb_username": "googich",
        "smb_password": "typed-but-wrong", "smb_domain": "HOME", "smb_port": "4455",
    }  # fmt: skip
    response = world.post("/settings/general", data=form)
    assert response.status_code == 400
    page = response.text
    for value in ("optimus.local", "GoogichDump", "takeout", "googich", "HOME", "4455"):
        assert f'value="{value}"' in page
    assert 'value="smb" checked' in page
    assert '<option value="Australia/Melbourne" selected>' in page
    assert "typed-but-wrong" not in page
    assert "Enter the password again" in page


def test_failed_schedule_save_keeps_what_was_typed(world: World) -> None:
    response = world.post(
        "/settings/schedule",
        data={
            "mode": "weekly",
            "at": "25:99",
            "weekday": "2",
            "every_hours": "24",
            "pause_after": "3",
        },
    )
    assert response.status_code == 400
    assert 'value="25:99"' in response.text
    assert 'value="weekly" selected' in response.text
    assert '<option value="2" selected>' in response.text


def _newer_release_seen(world: World) -> None:
    import json

    from googich_takeaway.state import State

    with State(world.tmp / "state.db") as state:
        state.set_setting(
            "updates.latest",
            json.dumps(
                {
                    "latest": "9.9.9",
                    "url": "https://github.com/aistuartai/Googich_takeaway/releases",
                    "checked_at": datetime.now(UTC).isoformat(),
                }
            ),
            datetime.now(UTC),
        )


def _helper(world: World, state: str = "idle", version: str = "") -> Path:
    folder = world.tmp / "updater"
    folder.mkdir(exist_ok=True)
    (folder / "status.json").write_text(
        f'{{"helper": "1", "state": "{state}", "message": "Downloading {version}.", '
        f'"version": "{version}", "at": "2026-10-02T00:00:00+00:00"}}'
    )
    return folder


def test_no_helper_means_no_update_button(world: World) -> None:
    _newer_release_seen(world)
    page = world.client.get("/").text
    assert "Version 9.9.9 is available" in page
    assert "Update now" not in page
    assert world.post("/updates/apply").headers["location"].endswith("no-helper#updates")


def test_update_button_requests_the_version_the_app_found(world: World) -> None:
    _newer_release_seen(world)
    folder = _helper(world)
    assert "Update now" in world.client.get("/").text
    response = world.client.post(
        "/updates/apply",
        data={
            "csrf_token": world.csrf,
            "version": "6.6.6",
        },  # a browser-supplied version is ignored
        headers=ORIGIN,
    )
    assert response.headers["location"].endswith("update-requested#updates")
    assert (folder / "request.json").read_text() == '{"version": "9.9.9"}'


def test_update_in_progress_is_shown_and_refreshes(world: World) -> None:
    _newer_release_seen(world)
    _helper(world, state="updating", version="9.9.9")
    page = world.client.get("/").text
    assert "Updating to 9.9.9: Downloading 9.9.9." in page
    assert '<meta http-equiv="refresh" content="8">' in page
    assert "Update now" not in page


def test_update_needs_csrf(world: World) -> None:
    _newer_release_seen(world)
    folder = _helper(world)
    assert world.client.post("/updates/apply", headers=ORIGIN).status_code == 403
    assert not (folder / "request.json").exists()
