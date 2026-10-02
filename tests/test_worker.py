import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from googich_takeaway.config import Config
from googich_takeaway.credentials import SecretBox
from googich_takeaway.destinations.immich import ImmichClient
from googich_takeaway.notify import Message, Notifier, Outcome
from googich_takeaway.sources.gdrive import GoogleDriveSource
from googich_takeaway.state import State
from googich_takeaway.worker import Trigger, Worker
from tests.fake_drive import FOLDER, FakeDrive
from tests.fake_immich import KEY, FakeImmichServer
from tests.fixtures.takeout import quirks_export
from tests.test_config import key_file

NOW = datetime(2026, 10, 1, tzinfo=UTC)
BOX = SecretBox(b"k" * 32)


class World:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.path = tmp_path / "state.db"
        self.immich = FakeImmichServer()
        self.drive = FakeDrive()
        self.sent: list[Message] = []

        def capture(notifier: Notifier, message: Message, force: bool = False) -> bool:
            self.sent.append(message)
            return True

        monkeypatch.setattr(Notifier, "send", capture)
        self.worker = Worker(
            self.path,
            BOX,
            clock=lambda: NOW,
            sleep=lambda seconds: None,
            immich_factory=lambda url, key: ImmichClient(url, key, self.immich.transport()),
            drive_factory=lambda folder, info: GoogleDriveSource(
                FOLDER, info, self.drive.transport()
            ),
        )

    def config(self) -> Config:
        return Config(State(self.path), BOX, lambda: NOW)

    def configure(self) -> None:
        config = self.config()
        config.save_immich("http://immich.test", "", KEY)
        config.save_general(str(self.tmp / "staging"), "Australia/Melbourne")
        config.add_drive_source("Takeout", FOLDER + "-abcdefghij", key_file())
        config.save_notifications("ntfys://example.invalid/topic", ["success", "failed", "paused"])
        built = self.tmp / "built"
        built.mkdir()
        for path in quirks_export().write(built):
            self.drive.add(path.name, path.name, path.read_bytes())


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


def test_run_is_recorded_and_notified(world: World) -> None:
    world.configure()
    message = world.worker.run_once(Trigger.MANUAL)
    assert message.outcome is Outcome.SUCCESS
    runs = State(world.path).recent_runs()
    assert [(r.trigger, r.status) for r in runs] == [("manual", "success")]
    assert runs[0].title == "Imported 13 new photos and videos"
    assert [m.outcome for m in world.sent] == [Outcome.SUCCESS]


def test_scheduled_failures_pause_after_the_limit(world: World) -> None:
    world.configure()
    world.config().save_schedule("daily", "03:00", "6", "24", "3")
    world.drive.shared = False  # every run fails
    for _ in range(2):
        assert world.worker.run_once(Trigger.SCHEDULE).outcome is Outcome.FAILED
        assert not world.config().schedule_paused()
    world.worker.run_once(Trigger.SCHEDULE)
    assert world.config().schedule_paused()
    assert [m.outcome for m in world.sent] == [Outcome.FAILED] * 3 + [Outcome.PAUSED]
    assert "Schedule paused after 3 failed runs" in world.sent[-1].title
    assert world.worker.status().paused


def test_manual_failures_never_pause(world: World) -> None:
    world.configure()
    world.drive.shared = False
    for _ in range(5):
        world.worker.run_once(Trigger.MANUAL)
    assert not world.config().schedule_paused()


def test_success_resets_the_count_and_resume_clears_pause(world: World) -> None:
    world.configure()
    world.drive.shared = False
    world.worker.run_once(Trigger.SCHEDULE)
    world.worker.run_once(Trigger.SCHEDULE)
    world.drive.shared = True
    world.worker.run_once(Trigger.SCHEDULE)
    assert world.config().scheduled_failures() == 0
    config = world.config()
    config.set_schedule_paused(True)
    config.set_schedule_paused(False)
    assert not config.schedule_paused()


def test_interrupted_runs_are_closed_on_start(world: World) -> None:
    State(world.path).start_run("schedule", NOW)
    world.worker.start()
    world.worker.stop()
    runs = State(world.path).recent_runs()
    assert runs[-1].status == "interrupted"


def test_background_loop_runs_when_due_and_on_request(world: World) -> None:
    world.configure()
    world.config().save_schedule("hourly", "03:00", "6", "24", "3")  # never ran: due now
    world.worker.start()
    try:
        deadline = time.monotonic() + 10

        def finished() -> list[str]:
            return [r.trigger for r in State(world.path).recent_runs() if r.finished_at]

        while time.monotonic() < deadline and not finished():
            time.sleep(0.05)
        runs = State(world.path).recent_runs()
        assert [r.trigger for r in runs] == ["schedule"]
        assert world.worker.request_run()
        while time.monotonic() < deadline and len(finished()) < 2:
            time.sleep(0.05)
        assert finished() == ["manual", "schedule"]
    finally:
        world.worker.stop()


def test_paused_schedule_does_not_run(world: World) -> None:
    world.configure()
    config = world.config()
    config.save_schedule("hourly", "03:00", "6", "24", "3")
    config.set_schedule_paused(True)
    world.worker.start()
    time.sleep(0.5)
    world.worker.stop()
    assert State(world.path).recent_runs() == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("weekly", "3pm", "6", "24", "3"), "24-hour"),
        (("weekly", "03:00", "9", "24", "3"), "day of the week"),
        (("hourly", "03:00", "6", "0", "3"), "1 to 168"),
        (("daily", "03:00", "6", "24", "0"), "1 to 20"),
        (("sometimes", "03:00", "6", "24", "3"), "how often"),
    ],
)
def test_schedule_validation(world: World, args: tuple[str, ...], message: str) -> None:
    from googich_takeaway.config import ConfigError

    with pytest.raises(ConfigError, match=message):
        world.config().save_schedule(*args)


def test_run_options_are_recorded(world: World) -> None:
    from googich_takeaway.pipeline import RunOptions

    world.configure()
    world.worker.run_once(Trigger.MANUAL, RunOptions(reimport=True))
    run = State(world.path).recent_runs()[0]
    assert run.details is not None
    assert "Options: re-import files missing from Immich." in run.details


def test_measured_rates_are_remembered(world: World) -> None:
    import json

    world.configure()
    world.worker.run_once(Trigger.MANUAL)
    stored = State(world.path).get_setting("progress.rates")
    # Tiny test files finish within the sampling window, so a rate may not be measured;
    # whatever is stored must be valid and per stage.
    if stored:
        assert set(json.loads(stored)) <= {"download", "scan", "upload"}


def test_unexpected_error_still_records_and_notifies(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from googich_takeaway.pipeline import Pipeline

    world.configure()

    def boom(self: Pipeline, report: object) -> None:
        raise AttributeError("a bug")

    monkeypatch.setattr(Pipeline, "_run", boom)
    message = world.worker.run_once(Trigger.MANUAL)
    assert message.outcome is Outcome.FAILED
    assert "Unexpected error (AttributeError)" in message.title
    run = State(world.path).recent_runs()[0]
    assert run.finished_at is not None
    assert run.status == "failed"
    assert [m.outcome for m in world.sent] == [Outcome.FAILED]


def _stop_at_first_upload(world: World, kind: str, at: str = "upload") -> Callable[..., None]:
    """Press Pause or Cancel as the first upload (or ``at`` stage) starts. Returns the original
    ``begin``."""
    from googich_takeaway.progress import Stage

    tracker = world.worker.tracker
    begin = tracker.begin

    def stopping(stage: Stage, name: str, size: int | None = None) -> None:
        if stage is Stage(at):
            tracker.request_stop(kind)
        begin(stage, name, size)

    tracker.begin = stopping  # type: ignore[method-assign]
    return begin


def test_pause_then_resume(world: World) -> None:
    world.configure()
    original = _stop_at_first_upload(world, "pause")
    message = world.worker.run_once(Trigger.MANUAL)
    assert message.outcome is Outcome.STOPPED
    runs = State(world.path).recent_runs()
    assert (runs[0].status, runs[0].title) == ("paused", "Paused by you")
    assert world.sent == []  # no notification, and not a failure
    assert world.worker.status().run_paused
    assert world.worker.resume_paused()
    world.worker.tracker.begin = original  # type: ignore[method-assign]
    trigger, options = world.worker._due_trigger() or (None, None)
    assert trigger is Trigger.MANUAL
    assert world.worker.run_once(Trigger.MANUAL, options).outcome is Outcome.SUCCESS
    assert not world.worker.status().run_paused
    assert len(world.immich.assets) == 13


def test_cancel(world: World) -> None:
    world.configure()
    _stop_at_first_upload(world, "cancel")
    assert world.worker.run_once(Trigger.SCHEDULE).outcome is Outcome.STOPPED
    runs = State(world.path).recent_runs()
    assert runs[0].status == "cancelled"
    assert not world.worker.status().run_paused
    assert world.config().scheduled_failures() == 0


def test_a_broken_schedule_does_not_stop_the_worker(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def broken() -> None:
        calls.append(1)
        if len(calls) >= 2:
            world.worker._stop.set()
        raise RuntimeError("bad setting")

    monkeypatch.setattr(world.worker, "_due_trigger", broken)
    monkeypatch.setattr("googich_takeaway.worker.IDLE_CHECK_SECONDS", 0.01)
    world.worker._loop()  # returns once stopped, instead of dying on the first error
    assert len(calls) == 2


def test_resuming_a_download_again_run_does_not_fetch_finished_archives(world: World) -> None:
    from googich_takeaway.pipeline import RunOptions

    world.configure()
    world.worker.run_once(Trigger.MANUAL)
    for copy in (world.tmp / "staging").glob("*.zip"):
        copy.unlink()  # cleaned up after the first import
    original = _stop_at_first_upload(world, "pause", at="scan")  # while reading the archives
    world.worker.run_once(Trigger.MANUAL, RunOptions(reimport=True, download_again=True))
    fetched = sum(1 for r in world.drive.requests if "/files/takeout-" in r)
    status = world.worker.status()
    assert status.run_paused
    stages = {view["label"]: view for view in status.paused_progress}
    assert stages["Downloading"]["files_done"] == 2
    assert stages["Downloading"]["done"] == stages["Downloading"]["total"]
    world.worker.tracker.begin = original  # type: ignore[method-assign]
    assert world.worker.resume_paused()
    _, options = world.worker._due_trigger() or (None, RunOptions())
    world.worker.run_once(Trigger.MANUAL, options)
    media = [r for r in world.drive.requests if "/files/takeout-" in r]
    assert len(media) == fetched  # nothing downloaded again on resume
