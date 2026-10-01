import time
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
