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
from tests.fake_drive import ACCOUNT, FOLDER, FOLDER_NAME, FakeDrive, service_account_info
from tests.fake_immich import KEY, FakeImmichServer
from tests.test_updates import FakeGitHub
from tests.web.test_app import ORIGIN, PASSWORD

FOLDER_ID = FOLDER + "-abcdefghij"


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.immich = FakeImmichServer()
        self.drive = FakeDrive()
        self.drive.add("a", "takeout-20261001T010203Z-001.zip", b"x" * 1000)
        self.github = FakeGitHub()
        app = create_app(
            WebSettings(tmp_path / "state.db"),
            immich_factory=lambda url, key: ImmichClient(url, key, self.immich.transport()),
            drive_factory=lambda folder, info: GoogleDriveSource(
                FOLDER, info, self.drive.transport()
            ),
            start_worker=False,
            github_transport=self.github.transport(),
        )
        self.client = TestClient(app, follow_redirects=False)
        self.app = app
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
        "/destinations/immich",
        data={
            "url": "http://immich.test:2283",
            "public_url": "https://photos.example",
            "api_key": KEY,
        },
    )
    assert response.status_code == 303
    page = world.client.get("/destinations").text
    assert KEY not in page
    assert "Saved. Leave blank to keep it, unless you change the address." in page
    assert 'href="https://photos.example"' in page  # header link uses the public address
    assert "Connected to Immich 2.7.5. The API key is fine." in world.htmx(
        "/destinations/immich/test"
    )


def test_immich_test_reports_missing_permissions(world: World) -> None:
    world.immich.permissions = ["asset.upload"]
    world.post("/destinations/immich", data={"url": "http://immich.test", "api_key": KEY})
    assert "lacks: asset.read" in world.htmx("/destinations/immich/test")


def test_immich_test_reports_bad_key(world: World) -> None:
    world.post("/destinations/immich", data={"url": "http://immich.test", "api_key": "wrong-key"})
    assert "rejected the API key" in world.htmx("/destinations/immich/test")


def test_invalid_settings_show_errors(world: World) -> None:
    response = world.post("/destinations/immich", data={"url": "nope", "api_key": KEY})
    assert response.status_code == 400
    assert "http:// or https://" in response.text


def test_general_settings(world: World) -> None:
    staging = world.tmp / "staging"
    response = world.post(
        "/destinations/downloads", data={"staging": str(staging), "timezone": "Australia/Melbourne"}
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
    assert f"<code>{FOLDER_ID}</code>" in page
    assert f'href="https://drive.google.com/drive/folders/{FOLDER_ID}"' in page
    assert f"<strong>{FOLDER_NAME}</strong>" in page  # looked up when the source was added
    source_id = re.search(r"/sources/(\d+)/test", page)
    assert source_id
    tested = world.htmx(f"/sources/{source_id[1]}/test")
    assert f"Connected to the folder “{FOLDER_NAME}”. 1 archives in the folder" in tested
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
    world.post("/destinations/immich", data={"url": "http://immich.test", "api_key": KEY})
    world.post("/destinations/downloads", data={"staging": str(world.tmp / "s"), "timezone": "UTC"})
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
        "/schedule",
        data={
            "mode": "weekly",
            "at": "02:30",
            "weekday": "0",
            "every_hours": "24",
            "pause_after": "3",
        },
    )
    assert response.status_code == 303
    page = world.client.get("/schedule").text
    assert 'value="weekly" selected' in page
    bad = world.post("/schedule", data={"mode": "daily", "at": "3pm"})
    assert bad.status_code == 400
    assert "24-hour" in bad.text


def test_notifications_are_named_listed_tested_and_removed(world: World) -> None:
    secret = "json://tok3n@127.0.0.1:9/hook"  # noqa: S105 - test value; nothing listens there
    response = world.post("/notifications/add", data={"name": "Phone", "url": secret})
    assert response.status_code == 303
    page = world.client.get("/notifications").text
    assert "tok3n" not in page
    assert '<input name="url" type="text"' in page
    assert "<strong>Phone</strong>" in page
    assert '<td class="muted">JSON</td>' in page
    target = re.search(r"/notifications/([0-9a-f]{8})/test", page)
    assert target
    tested = world.htmx(f"/notifications/{target[1]}/test")
    assert "Not delivered:" in tested
    assert "tok3n" not in tested
    duplicate = world.post("/notifications/add", data={"name": "phone", "url": secret})
    assert duplicate.status_code == 400
    assert "already a notification called" in duplicate.text
    wrong = world.post("/notifications/add", data={"name": "HA", "url": "http://ha.local/x"})
    assert wrong.status_code == 400
    assert "hassio://" in wrong.text
    assert 'value="HA"' in wrong.text  # the name is kept, the URL is not
    assert world.post("/notifications", data={"outcomes": ["failed"]}).status_code == 303
    assert 'value="failed" checked' in world.client.get("/notifications").text
    world.post(f"/notifications/{target[1]}/remove")
    assert "No notifications yet." in world.client.get("/notifications").text
    assert "Add a notification first." in world.htmx("/notifications/test")


def test_run_now_and_resume(world: World) -> None:
    world.post("/destinations/immich", data={"url": "http://immich.test", "api_key": KEY})
    world.post("/destinations/downloads", data={"staging": str(world.tmp / "s"), "timezone": "UTC"})
    folder = world.tmp / "manual"
    folder.mkdir()
    world.post("/sources/local", data={"name": "Manual", "path": str(folder)})
    assert world.post("/runs").status_code == 303
    worker = world.client.app.state.worker  # type: ignore[attr-defined]
    assert worker._manual_requested  # queued for the background worker
    box = SecretBox(load_master_key(world.tmp / "master.key"))
    config = Config(State(world.tmp / "state.db"), box, lambda: datetime.now(UTC))
    config.set_schedule_paused(True)
    assert "Scheduled runs are paused" in world.client.get("/").text
    assert world.post("/schedule/resume").status_code == 303
    assert "Scheduled runs are paused" not in world.client.get("/").text


def test_time_zone_is_a_dropdown(world: World) -> None:
    page = world.client.get("/settings").text
    assert '<select name="timezone"' in page
    assert '<option value="Australia/Melbourne"' in page
    assert '<option value="UTC" selected>' in page


def test_status_poll_reloads_page_when_idle(world: World) -> None:
    response = world.client.get("/status")
    assert response.headers["hx-redirect"] == "/"  # the plain dashboard, without a notice


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
    world.post("/destinations/downloads", data={"staging": str(world.tmp / "s"), "timezone": "UTC"})
    (world.tmp / "s" / "takeout-20261001T010203Z-001.zip").write_bytes(b"zip")
    page = world.client.get("/cleanup").text
    assert "Not imported completely yet." in page
    assert "This app never deletes anything in Google Drive." in page
    assert "/confirm" not in page  # not ready, so no delete link
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
    assert world.post("/destinations/downloads", data=form).status_code == 303
    page = world.client.get("/destinations").text
    assert SMB_LOGIN not in page
    assert 'value="smb" checked' in page
    assert "Can write to \\\\nas.local\\Photos\\takeout" in world.htmx(
        "/destinations/downloads/test"
    )

    bad = world.post("/destinations/downloads", data={**form, "smb_password": "wrong"})
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
    world.post("/updates/daily", data={})  # switch the check off
    assert "Version 9.9.9 is available" not in world.client.get("/").text


def test_failed_smb_save_keeps_what_was_typed_except_the_password(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from tests.fake_smb import FakeSmb

    monkeypatch.setitem(sys.modules, "smbclient", FakeSmb())
    form = {
        "storage": "smb", "timezone": "Australia/Melbourne", "smb_server": "nas.example",
        "smb_share": "Photos", "smb_folder": "takeout", "smb_username": "googich",
        "smb_password": "typed-but-wrong", "smb_domain": "HOME", "smb_port": "4455",
    }  # fmt: skip
    response = world.post("/destinations/downloads", data=form)
    assert response.status_code == 400
    page = response.text
    for value in ("nas.example", "Photos", "takeout", "googich", "HOME", "4455"):
        assert f'value="{value}"' in page
    assert 'value="smb" checked' in page
    assert "typed-but-wrong" not in page
    assert "Enter the password again" in page


def test_failed_schedule_save_keeps_what_was_typed(world: World) -> None:
    response = world.post(
        "/schedule",
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
    assert world.post("/updates/apply").headers["location"].endswith("/updates?saved=no-helper")


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
    assert response.headers["location"].endswith("/updates?saved=update-requested")
    assert (folder / "request.json").read_text() == '{"version": "9.9.9"}'


def test_update_in_progress_is_shown_and_refreshes(world: World) -> None:
    _newer_release_seen(world)
    _helper(world, state="updating", version="9.9.9")
    page = world.client.get("/").text
    assert "Updating to 9.9.9: Downloading 9.9.9." in page
    assert 'hx-get="/updates/banner" hx-trigger="every 5s"' in page  # keeps asking
    assert "Update now" not in page


def test_update_banner_says_when_the_update_is_complete(world: World) -> None:
    _newer_release_seen(world)
    folder = _helper(world)
    world.post("/updates/apply")
    waiting = world.client.get("/updates/banner").text
    assert "Update to 9.9.9 requested" in waiting
    assert 'hx-trigger="every 5s"' in waiting
    now = datetime.now(UTC).isoformat()
    (folder / "status.json").write_text(
        '{"helper": "1", "state": "done", "message": "Updated from 0.3.1 to 9.9.9.", '
        f'"version": "9.9.9", "at": "{now}"}}'
    )
    done = world.client.get("/updates/banner").text
    assert "Update complete." in done
    assert "Updated from 0.3.1 to 9.9.9." in done
    assert "every 5s" not in done  # stops asking
    assert "Update complete." in world.client.get("/settings").text  # on every page
    world.client.post(
        "/updates/dismiss",
        data={"csrf_token": world.csrf},
        headers={**ORIGIN, "Referer": "http://testserver/settings"},
    )
    assert "Update complete." not in world.client.get("/settings").text


def test_update_banner_says_why_an_update_failed(world: World) -> None:
    folder = _helper(world)
    now = datetime.now(UTC).isoformat()
    (folder / "status.json").write_text(
        '{"helper": "1", "state": "failed", "message": "Health check timed out. Rolled back '
        f'to 0.3.1.", "version": "9.9.9", "at": "{now}"}}'
    )
    page = world.client.get("/").text
    assert "Update to 9.9.9 failed." in page
    assert "Rolled back to 0.3.1." in page


def test_update_needs_csrf(world: World) -> None:
    _newer_release_seen(world)
    folder = _helper(world)
    assert world.client.post("/updates/apply", headers=ORIGIN).status_code == 403
    assert not (folder / "request.json").exists()


def _ready(world: World) -> None:
    world.post(
        "/destinations/immich",
        data={"url": "http://immich.test", "public_url": "https://photos.example", "api_key": KEY},
    )
    world.post("/destinations/downloads", data={"staging": str(world.tmp / "s")})
    folder = world.tmp / "manual"
    folder.mkdir()
    world.post("/sources/local", data={"name": "Manual", "path": str(folder)})


def test_check_now_from_settings(world: World) -> None:
    response = world.post("/updates/check")
    assert response.headers["location"].endswith("/updates?saved=check-newer")
    assert world.github.calls == 1
    page = world.client.get("/updates?saved=check-newer").text
    assert "Version 9.9.9 is available." in page
    assert "Update to 9.9.9" not in page  # no helper on this host
    assert "<code>compose.yaml</code> to <code>9.9.9</code>" in page
    _helper(world)
    assert "Update to 9.9.9" in world.client.get("/updates").text
    again = world.post("/updates/check")
    assert again.headers["location"].endswith("/updates?saved=check-wait")
    assert world.github.calls == 1


def test_check_now_reports_failure_and_needs_csrf(world: World) -> None:
    assert world.client.post("/updates/check", headers=ORIGIN).status_code == 403
    assert world.github.calls == 0
    world.github.status = 500
    response = world.post("/updates/check")
    assert response.headers["location"].endswith("/updates?saved=check-failed")
    assert (
        "Could not get the latest release" in world.client.get("/updates?saved=check-failed").text
    )


def test_time_zone_saved_in_settings(world: World) -> None:
    response = world.post("/settings/timezone", data={"timezone": "Australia/Melbourne"})
    assert response.status_code == 303
    assert '<option value="Australia/Melbourne" selected>' in world.client.get("/settings").text
    assert world.post("/settings/timezone", data={"timezone": "Mars/Base"}).status_code == 400


def test_menus(world: World) -> None:
    page = world.client.get("/destinations").text
    assert 'id="immich"' in page
    assert 'id="downloads"' in page
    nav = page[page.index('<nav aria-label="Main">') : page.index("</nav>")]
    configuration, help_menu = nav.split('<details class="nav-menu" name="nav-menus">')[1:3]
    for path in ("/sources", "/destinations", "/schedule", "/notifications"):
        assert f'href="{path}"' in configuration
    for path in ("/dashboard/configure", "/cleanup", "/settings"):
        assert f'href="{path}"' in configuration
    for path in ("/help", "/updates", "/logs", "/about"):
        assert f'href="{path}"' in help_menu
    assert "/help/takeout" not in help_menu
    assert "Immich ↗" not in nav
    assert nav.count('name="nav-menus"') == 2  # one menu open at a time
    settings = world.client.get("/settings").text
    for section in ("timezone", "look"):
        assert f'id="{section}"' in settings
    for section in ("immich", "schedule", "notifications", "updates"):
        assert f'id="{section}"' not in settings
    for path, section in (("/schedule", "schedule"), ("/notifications", "services")):
        assert f'id="{section}"' in world.client.get(path).text
    assert 'id="updates"' in world.client.get("/updates").text


def test_dashboard_defaults(world: World) -> None:
    _ready(world)
    page = world.client.get("/").text
    assert 'class="journey"' in page
    assert (
        '<a class="station-icon" href="https://photos.example" target="_blank" '
        'rel="noopener noreferrer"' in page
    )  # the Immich icon opens Immich
    # Only a local folder source: Google Drive is greyed out, and leads to its setup.
    assert page.count('<a href="/cleanup#') == 1  # under the download folder
    assert '<div class="station station-drive station-off">' in page
    assert (
        '<a class="station-icon" href="/sources?how=drive#add" title="Set up Google Drive">' in page
    )
    assert "Not set up: exports arrive as archives you download yourself." in page
    assert '<a class="station-icon" href="/destinations#downloads"' in page
    assert (
        'href="https://photos.example" target="_blank" rel="noopener noreferrer">Open Immich'
        in page
    )
    assert "<h2>History</h2>" in page
    for summary in ("sum-sources", "sum-destinations", "sum-cleanup"):
        assert f'id="{summary}"' not in page
    assert page.index("<h2>History</h2>") < page.index("<summary>Configure dashboard</summary>")


def test_dashboard_items_can_be_chosen(world: World) -> None:
    _ready(world)
    form = {"items": ["sources", "destinations", "cleanup"]}
    assert world.post("/dashboard/items", data=form).status_code == 303
    page = world.client.get("/").text
    assert 'class="journey"' not in page
    assert "<h2>History</h2>" not in page
    for summary in ("sum-sources", "sum-destinations", "sum-cleanup"):
        assert f'id="{summary}"' in page
    assert 'href="/cleanup">Go to Cleanup' in page
    assert "Run now" in page  # always there
    assert 'name="items" value="journey" >' in page
    assert 'name="items" value="cleanup" checked>' in page
    assert world.post("/dashboard/items", data={"items": ["nope"]}).status_code == 400
    saved = world.post("/dashboard/items", data={"items": ["runs"], "back": "/dashboard/configure"})
    assert saved.headers["location"] == "/dashboard/configure?saved=1"
    configure = world.client.get("/dashboard/configure").text
    assert 'name="items" value="runs" checked>' in configure
    assert (
        world.post("/dashboard/items", data={"back": "https://evil.example"}).headers["location"]
        == "/"
    )
    world.post("/dashboard/items", data={})
    page = world.client.get("/").text
    assert 'id="sum-cleanup"' not in page
    assert "Run now" in page


def test_dashboard_counts_what_can_be_cleaned_up(world: World) -> None:
    from googich_takeaway import cleanup

    _ready(world)
    world.post("/dashboard/items", data={"items": ["cleanup"]})
    name = "takeout-20261001T010203Z-001.zip"
    (world.tmp / "s" / name).write_bytes(b"z" * 2_500_000)
    assert "Nothing</p>" in world.client.get("/").text
    with State(world.tmp / "state.db") as state:
        key = cleanup.export_key("20261001T010203Z", [(name, 2_500_000)])
        state.mark_export_complete(key, "20261001T010203Z", "{}", datetime.now(UTC))
    page = world.client.get("/").text
    assert '<p class="summary-big">2.5 MB</p>' in page
    assert "2.5 MB in the download folder (1 export)" in page


def test_run_buttons_and_schedule_box(world: World) -> None:
    _ready(world)
    world.post("/schedule", data={"mode": "daily", "at": "03:00"})
    response = world.post("/runs")
    assert response.headers["location"] == "/?notice=started"
    assert "Run started." in world.client.get("/?notice=started").text
    worker = world.client.app.state.worker  # type: ignore[attr-defined]
    worker._run_started = datetime.now(UTC)  # pretend a run is going
    assert world.post("/runs").headers["location"] == "/?notice=busy"
    page = world.client.get("/").text
    assert 'href="/runs/stop?kind=pause"' in page
    assert 'href="/runs/stop?kind=cancel"' in page
    from googich_takeaway.progress import Stage

    worker.tracker.start_run()
    worker.tracker.begin(Stage.DOWNLOAD, "takeout-002.tgz", 80 * 1000**3)
    worker.tracker.already_have(40 * 1000**3)
    confirm = world.client.get("/runs/stop?kind=pause").text
    assert "Downloading takeout-002.tgz: 40.0 GB of 80.0 GB." in confirm
    assert "continues from 40.0 GB next time" in confirm
    assert 'action="/runs/pause"' in confirm
    worker.tracker.end(Stage.DOWNLOAD, "takeout-002.tgz")
    worker.tracker.begin(Stage.SCAN, "20261001T010203Z", 50 * 1000**3)
    worker.tracker.already_have(20 * 1000**3)
    confirm = world.client.get("/runs/stop?kind=cancel").text
    assert "Reading starts again from the beginning of this export" in confirm
    assert 'action="/runs/cancel"' in confirm
    worker.tracker.finish_run()
    worker._run_started = None
    assert world.client.get("/runs/stop?kind=pause").headers["location"] == "/?notice=idle"
    page = world.client.get("/").text
    assert 'id="sum-schedule"' in page
    assert "Daily at 03:00." in page
    assert "Next run <strong>" in page
    assert world.post("/schedule/pause").headers["location"] == "/?notice=schedule-paused"
    page = world.client.get("/").text
    assert "Paused by you." in page
    assert "Scheduled runs are paused</h2>" not in page  # that card is for failed runs
    assert ">Resume schedule</button>" in page
    world.post("/schedule/resume")
    assert ">Pause schedule</button>" in world.client.get("/").text
    assert world.post("/runs/pause").headers["location"] == "/?notice=idle"


def test_paused_run_shows_how_far_it_got(world: World) -> None:
    import json

    _ready(world)
    progress = [
        {"label": "Downloading", "done": 345_576_080, "total": 345_576_080, "files_done": 2,
         "files_total": 2, "files_failed": 0},
        {"label": "Uploading", "done": 120_000_000, "total": 400_000_000, "files_done": 31,
         "files_total": 120, "files_failed": 1},
    ]  # fmt: skip
    with State(world.tmp / "state.db") as state:
        state.set_setting(
            "run.paused",
            json.dumps(
                {"options": {"reimport": False, "download_again": False}, "progress": progress}
            ),
            datetime.now(UTC),
        )
    page = world.client.get("/").text
    assert '<span class="state-dot paused" aria-hidden="true"></span>Run paused</h2>' in page
    assert 'class="activity paused"' in page  # and in the menu bar, with how far it got
    assert "Paused: uploading <strong>30%</strong>" in page
    assert "How far it got" in page
    assert "31 of 120 files, 1 failed" in page
    assert 'action="/runs/resume"' in page
    world.post("/runs/discard")
    assert "Run paused" not in world.client.get("/").text


def test_cleanup_asks_before_deleting(world: World) -> None:
    from googich_takeaway import cleanup

    _ready(world)
    name = "takeout-20261001T010203Z-001.zip"
    (world.tmp / "s" / name).write_bytes(b"z" * 2_500_000)
    (world.tmp / "s" / "takeout-20261001T010203Z-002.zip.part").write_bytes(b"p" * 1000)
    with State(world.tmp / "state.db") as state:
        key = cleanup.export_key("20261001T010203Z", [(name, 2_500_000)])
        state.mark_export_complete(key, "20261001T010203Z", "{}", datetime.now(UTC))
    page = world.client.get("/cleanup").text
    assert 'href="/cleanup/staged/20261001T010203Z/confirm"' in page
    confirm = world.client.get("/cleanup/staged/20261001T010203Z/confirm").text
    assert "deleted permanently from the download folder" in confirm
    assert f"<code>{name}</code>" in confirm
    assert (world.tmp / "s" / name).exists()  # asking deletes nothing
    partial = world.client.get("/cleanup/partial/takeout-20261001T010203Z-002.zip/confirm").text
    assert "the next run continues it from" in partial
    assert world.post("/cleanup/staged/20261001T010203Z").status_code == 303
    assert not (world.tmp / "s" / name).exists()
    missing = world.client.get("/cleanup/staged/nope/confirm")
    assert missing.headers["location"].startswith("/cleanup?error=")


def test_one_run_now_button_with_options(world: World) -> None:
    _ready(world)
    page = world.client.get("/").text
    form = page[page.index('<form method="post" action="/runs" class="run-form">') :]
    form = form[: form.index("</form>")]
    assert ">Run now</button>" in form
    assert 'name="reimport"' in form
    assert 'name="download_again"' in form
    assert page.count('action="/runs"') == 1  # no second Run button
    world.post("/runs", data={"reimport": "1"})
    worker = world.client.app.state.worker  # type: ignore[attr-defined]
    assert worker._manual_requested.reimport


def test_log_download_is_a_zip_of_every_log_file(world: World) -> None:
    import io
    import zipfile

    logs = world.tmp / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "googich.log").write_text('{"message": "new"}\n')
    (logs / "googich.log.1").write_text('{"message": "old"}\n')
    response = world.client.get("/logs/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"].startswith('attachment; filename="googich-logs-')
    assert int(response.headers["content-length"]) == len(response.content)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read("googich.log") == b'{"message": "new"}\n'
        assert archive.read("googich.log.1") == b'{"message": "old"}\n'


def test_log_retention_is_set_in_settings_and_on_the_logs_page(world: World) -> None:
    assert (
        'name="days" type="number" min="7" max="3650" value="90"'
        in world.client.get("/settings").text
    )
    response = world.post("/settings/logs", data={"days": "30"})
    assert response.headers["location"].endswith("/settings?saved=logs#logs")
    page = world.client.get("/logs").text
    assert 'value="30"' in page
    assert 'name="back" value="logs"' in page
    response = world.post("/settings/logs", data={"days": "2", "back": "logs"})
    assert response.headers["location"].endswith("/logs?saved=retention-invalid")
    assert "between 7 and 3650" in world.client.get("/logs?saved=retention-invalid").text
    response = world.post("/settings/logs", data={"days": "60", "back": "logs"})
    assert response.headers["location"].endswith("/logs?saved=retention")
    assert (
        "Log files older than 60 days are deleted" in world.client.get("/logs?saved=retention").text
    )
    assert "as a whole number" in world.post("/settings/logs", data={"days": "x"}).text


def test_about_and_updates_check_github_as_they_open(world: World) -> None:
    from datetime import timedelta

    from googich_takeaway import __version__

    world.github.body = {**world.github.body, "tag_name": "v0.0.1"}
    with State(world.tmp / "state.db") as state:
        # Saved before this version was installed: older than what is running.
        old = datetime.now(UTC) - timedelta(minutes=2)
        state.set_setting(
            "updates.latest",
            json.dumps(
                {"latest": "0.0.1", "url": "https://github.com/x", "checked_at": old.isoformat()}
            ),
            old,
        )
    page = world.client.get("/about").text
    assert world.github.calls == 1  # older than the running version, so asked again
    assert f"The latest release is <strong>{__version__}</strong>" in page  # never behind
    world.client.get("/updates")
    assert world.github.calls == 1  # at most once a minute
    world.post("/updates/daily", data={})  # checks switched off: pages do not ask
    with State(world.tmp / "state.db") as state:
        state.set_setting("updates.view_attempted", None, datetime.now(UTC))
    world.client.get("/about")
    assert world.github.calls == 1


def test_drive_figure_shows_what_the_last_run_listed(world: World) -> None:
    from googich_takeaway.downloads import record_listing
    from googich_takeaway.sources.base import RemoteFile

    _ready(world)
    world.post(
        "/sources/drive",
        data={"name": "Takeout", "folder_id": FOLDER_ID},
        files={
            "key_file": (
                "key.json",
                json.dumps(service_account_info()).encode(),
                "application/json",
            )
        },
    )
    with State(world.tmp / "state.db") as state:
        files = [
            RemoteFile(f"id{n}", f"takeout-x-00{n}.zip", 2_000_000_000, datetime.now(UTC), "", None)
            for n in range(3)
        ]
        record_listing(state, f"gdrive:{FOLDER_ID}", files, datetime.now(UTC))
        record_listing(state, "gdrive:some-removed-source", files, datetime.now(UTC))
    page = world.client.get("/").text
    assert '<p class="station-figure">3</p>' in page
    assert "archives in Drive, 6.0 GB, seen " in page
    assert "station-off" not in page  # set up: not greyed out


def test_download_folder_figure_grows_while_downloading(world: World) -> None:
    from googich_takeaway.progress import Stage

    _ready(world)
    staging = world.tmp / "s"
    staging.mkdir(exist_ok=True)
    (staging / "takeout-20261001T010203Z-001.zip").write_bytes(b"x" * 1_000_000)
    worker = world.app.state.worker
    assert worker.claim_for_demo()
    try:
        tracker = worker.tracker
        tracker.plan(Stage.DOWNLOAD, [("takeout-20261001T010203Z-002.zip", 4_000_000)])
        tracker.begin(Stage.DOWNLOAD, "takeout-20261001T010203Z-002.zip", 4_000_000)
        tracker.advance(1_500_000)
        page = world.client.get("/status").text
        assert "archive waiting or kept, 2.5 MB, one more arriving" in page
        tracker.advance(1_000_000)
        assert (
            "archive waiting or kept, 3.5 MB, one more arriving" in world.client.get("/status").text
        )
    finally:
        worker.release_from_demo()


def test_menu_bar_shows_a_run_only_while_one_is_going(world: World) -> None:
    from googich_takeaway.progress import Stage

    _ready(world)
    assert '<a id="activity" href="/" class="activity" hidden' in world.client.get("/settings").text
    worker = world.app.state.worker
    assert worker.claim_for_demo()
    try:
        tracker = worker.tracker
        tracker.plan(Stage.DOWNLOAD, [("takeout-x-001.zip", 1000)])
        tracker.begin(Stage.DOWNLOAD, "takeout-x-001.zip", 1000)
        tracker.advance(420)
        page = world.client.get("/settings").text
        assert 'class="activity running"' in page
        assert "Downloading <strong>42%</strong>" in page
        assert 'hx-trigger="every 3s"' in page
        badge = world.client.get("/activity").text
        assert "Downloading <strong>42%</strong>" in badge
        # Running: the run box takes the full width, without the schedule beside it.
        dashboard = world.client.get("/").text
        assert 'class="run-row"' in dashboard
        assert 'id="schedule-summary"' not in dashboard
    finally:
        worker.release_from_demo()
    dashboard = world.client.get("/").text
    assert 'class="run-row with-schedule"' in dashboard
    assert 'id="schedule-summary"' in dashboard
    assert 'hx-trigger="every 30s"' in world.client.get("/activity").text


def test_history_shows_the_latest_run_and_hides_the_rest(world: World) -> None:
    _ready(world)
    with State(world.tmp / "state.db") as state:
        for n in range(3):
            at = datetime(2026, 10, 1 + n, 9, tzinfo=UTC)
            run = state.start_run("manual", at)
            state.finish_run(run, "success", f"Run {n} done", "", at)
    page = world.client.get("/").text
    history = page[page.index("<h2>History</h2>") :]
    assert (
        history.index("Run 2 done") < history.index("2 earlier runs") < history.index("Run 1 done")
    )
    assert '<details class="more-runs">' in history  # closed until opened
    assert re.search(
        r"Last run Sat 03 Oct at \d\d:\d\d: <span class=\"run-success\">Run 2 done", page
    )


def test_retention_shows_how_much_the_logs_take(world: World) -> None:
    logs = world.tmp / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "googich.log").write_bytes(b"x" * 1_500_000)
    (logs / "googich.log.1").write_bytes(b"x" * 1_000_000)
    assert "Now: <strong>2.5 MB</strong> in 2 log files." in world.client.get("/settings").text
    logs_page = world.client.get("/logs").text
    assert "2.5 MB in 2 log files. Files older than 90 days are deleted" in logs_page
    # On the Logs page it sits in the toolbar beside Download, above the log lines.
    toolbar = logs_page[logs_page.index('class="log-toolbar"') : logs_page.index('id="log-body"')]
    assert toolbar.index(">Filter</button>") < toolbar.index(">Pause</a>")
    assert toolbar.index(">Download logs</a>") < toolbar.index('class="keep-form"')
    held = world.client.get("/logs?follow=0").text
    assert ">Resume</a>" in held
    assert ">Pause</a>" not in held
    assert 'name="follow" value="0"' in held  # filtering keeps it paused


def test_demo_run_button_is_inside_the_options(world: World) -> None:
    _ready(world)
    page = world.client.get("/").text
    assert 'formaction="/demo/run"' not in page  # not in demo mode


def test_logs_are_shown_newest_first_and_new_lines_go_on_top(world: World) -> None:
    import logging

    for n in (1, 2):
        logging.getLogger("googich.test.order").warning("order line %d", n)
    page = world.client.get("/logs?level=WARNING&q=order+line").text
    assert page.index("order line 2") < page.index("order line 1")
    last = re.search(r'id="log-tail" class="tail-marker" hx-get="/logs/tail\?after=(\d+)', page)
    assert last
    assert 'hx-target="#log-body" hx-swap="afterbegin"' in page
    for n in (3, 4):
        logging.getLogger("googich.test.order").warning("order line %d", n)
    tail = world.client.get(f"/logs/tail?after={last[1]}&level=WARNING&q=order+line").text
    assert tail.index("order line 4") < tail.index("order line 3")
    assert "order line 2" not in tail
    assert 'id="log-tail" class="tail-marker" hx-swap-oob="true"' in tail  # remembers line 4


def test_run_notices_go_once_the_run_has_ended(world: World) -> None:
    _ready(world)
    for key in ("cancelling", "pausing", "started", "resumed"):
        page = world.client.get(f"/?notice={key}").text
        assert 'class="result ok notice"' not in page  # no run going: nothing to say
    assert "No run is going." in world.client.get("/?notice=idle").text
    worker = world.app.state.worker
    assert worker.claim_for_demo()
    try:
        assert "Cancelling at the next safe point." in world.client.get("/?notice=cancelling").text
    finally:
        worker.release_from_demo()


def test_choose_how_exports_arrive(world: World) -> None:
    page = world.client.get("/").text
    assert 'href="/sources?how=drive#add">Automatically, from Google Drive</a>' in page
    assert 'href="/sources?how=manual#add">I download them myself</a>' in page
    manual = world.client.get("/sources?how=manual").text
    assert 'name="how" value="manual" checked' in manual
    assert "Choose the download folder</a> first" in manual  # none set yet
    assert world.post("/sources/download-folder").status_code == 400
    world.post("/destinations/downloads", data={"staging": str(world.tmp / "s")})
    page = world.client.get("/sources?how=manual").text
    assert ">Use the download folder</button>" in page
    assert (
        world.post("/sources/download-folder", data={"name": "My Takeout downloads"}).status_code
        == 303
    )
    page = world.client.get("/sources").text
    assert "Your downloads" in page
    assert f"Archives you save into the download folder: <code>{world.tmp / 's'}</code>" in page
    assert "The download folder is already a source." in page
    assert "already a source" in world.post("/sources/download-folder").text
    source_id = re.search(r"/sources/(\d+)/test", page)
    assert source_id
    assert "No Takeout archives saved there yet" in world.htmx(f"/sources/{source_id[1]}/test")
    world.post(
        "/destinations/immich",
        data={"url": "http://immich.test", "public_url": "", "api_key": KEY},
    )
    dashboard = world.client.get("/").text
    assert "Bring your Google Photos home" not in dashboard  # setup complete without Drive
    assert '<div class="station station-drive station-off">' in dashboard


def test_an_old_update_helper_is_offered_a_one_command_upgrade(world: World) -> None:
    from googich_takeaway import __version__

    _helper(world)  # reports helper version 1
    page = world.client.get("/updates").text
    assert "The update helper can be upgraded." in page
    assert (
        f"github.com/aistuartai/Googich_takeaway/releases/download/v{__version__}/"
        "install-updater.sh" in page
    )
    assert "sudo bash install-updater.sh /opt/googich" in page
    folder = world.tmp / "updater"
    (folder / "status.json").write_text(
        '{"helper": "2", "state": "idle", "message": "Ready for updates.", "version": "", "at": ""}'
    )
    assert "can be upgraded" not in world.client.get("/updates").text


def test_without_the_helper_the_install_command_is_shown(world: World) -> None:
    page = world.client.get("/updates").text
    assert "One-click updates are off" in page
    assert "sudo bash install-updater.sh /opt/googich" in page


def test_a_run_notice_goes_once_progress_shows(world: World) -> None:
    _ready(world)
    worker = world.app.state.worker
    assert worker.claim_for_demo()
    try:
        page = world.client.get("/?notice=resumed").text
        assert "Resuming the paused run." in page  # straight after pressing Resume
        assert "Resuming the paused run." not in world.client.get("/status").text  # next poll
    finally:
        worker.release_from_demo()


def test_uploads_at_a_time_is_set_in_settings(world: World) -> None:
    page = world.client.get("/settings").text
    assert 'name="parallel" type="number" min="1" max="8" value="3"' in page
    assert world.post("/settings/uploads", data={"parallel": "6"}).status_code == 303
    assert 'value="6"' in world.client.get("/settings").text
    assert "between 1 and 8" in world.post("/settings/uploads", data={"parallel": "20"}).text


def test_install_command_checks_the_installer_against_the_app(world: World) -> None:
    import hashlib

    from tests.test_updater_script import INSTALL

    expected = hashlib.sha256(INSTALL.read_bytes()).hexdigest()
    page = world.client.get("/updates").text
    assert f'echo "{expected}  install-updater.sh" | sha256sum -c' in page
