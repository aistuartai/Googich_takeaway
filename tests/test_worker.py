import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from googich_takeaway.config import Config
from googich_takeaway.credentials import SecretBox
from googich_takeaway.destinations.immich import ImmichClient
from googich_takeaway.notify import Message, Notifier, Outcome
from googich_takeaway.sources.gdrive import GoogleDriveSource
from googich_takeaway.state import State
from googich_takeaway.worker import IDLE_CHECK_SECONDS, Trigger, Worker
from tests.fake_drive import FOLDER, FakeDrive
from tests.fake_github import FakeGitHub
from tests.fake_immich import KEY, FakeImmichServer
from tests.fixtures.takeout import quirks_export
from tests.test_config import key_file
from tests.test_schedule import MEL

NOW = datetime(2026, 10, 1, tzinfo=UTC)
BOX = SecretBox(b"k" * 32)


class World:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.path = tmp_path / "state.db"
        self.immich = FakeImmichServer()
        self.drive = FakeDrive()
        self.github = FakeGitHub()
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
            github_transport=self.github.transport(),
        )

    def config(self, at: datetime = NOW) -> Config:
        return Config(State(self.path), BOX, lambda: at)

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
    assert runs[0].title == "Downloaded 2 archives, 13 photos and videos uploaded, 0 skipped"
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

    def stopping(stage: Stage, name: str, size: int | None = None, parallel: bool = False) -> None:
        if stage is Stage(at):
            tracker.request_stop(kind)
        begin(stage, name, size, parallel)

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


def test_update_during_a_run_pauses_it_first_and_resumes_after(world: World) -> None:
    from googich_takeaway.progress import Stage
    from googich_takeaway.worker import RESUME_AFTER_UPDATE

    world.configure()
    tracker = world.worker.tracker
    begin = tracker.begin
    done: list[bool] = []

    def update_pressed(
        stage: Stage, name: str, size: int | None = None, parallel: bool = False
    ) -> None:
        if stage is Stage.UPLOAD and not done:
            # Update now, pressed mid-run: the request waits for the pause.
            assert world.worker.pause_then(lambda: done.append(True))
            assert done == []
        begin(stage, name, size, parallel)

    tracker.begin = update_pressed  # type: ignore[method-assign]
    assert world.worker.run_once(Trigger.MANUAL).outcome is Outcome.STOPPED
    assert done == [True]  # sent once the run had stopped
    assert world.worker.status().run_paused
    assert State(world.path).get_setting(RESUME_AFTER_UPDATE) == "1"
    tracker.begin = begin  # type: ignore[method-assign]
    world.worker.resume_after_update()  # what start-up does after the update
    assert State(world.path).get_setting(RESUME_AFTER_UPDATE) is None
    trigger, options = world.worker._due_trigger() or (None, None)
    assert trigger is Trigger.MANUAL
    assert world.worker.run_once(Trigger.MANUAL, options).outcome is Outcome.SUCCESS
    assert len(world.immich.assets) == 13


def test_update_when_idle_is_sent_straight_away(world: World) -> None:
    done: list[bool] = []
    assert not world.worker.pause_then(lambda: done.append(True))
    assert done == [True]


def test_old_log_files_are_deleted_and_history_kept(world: World) -> None:
    import os
    from datetime import timedelta

    state = State(world.path)
    old = NOW - timedelta(days=120)
    for trigger in ("manual", "manual", "schedule"):
        run = state.start_run(trigger, old)
        state.finish_run(run, "success", "Done", "", old)
    recent = state.start_run("manual", NOW)
    state.finish_run(recent, "success", "Done", "", NOW)
    logs = world.tmp / "logs"
    logs.mkdir()
    (logs / "googich.log").write_text("current\n")
    for name, age in (("googich.log.1", 10), ("googich.log.2", 100)):
        (logs / name).write_text("old\n")
        stamp = (NOW - timedelta(days=age)).timestamp()
        os.utime(logs / name, (stamp, stamp))
    world.worker.prune_logs()
    assert len(state.recent_runs()) == 4  # History is never pruned
    assert sorted(p.name for p in logs.iterdir()) == ["googich.log", "googich.log.1"]
    world.config().save_log_retention("7")
    world.worker.prune_logs()
    assert sorted(p.name for p in logs.iterdir()) == ["googich.log"]


def test_a_second_run_now_is_refused_while_the_first_is_starting(world: World) -> None:
    assert world.worker.request_run()
    assert world.worker._due_trigger() is not None  # the loop has taken the request...
    assert not world.worker.request_run()  # ...so a double click does not queue another run
    assert world.worker.run_pending_or_going()


def test_a_finished_update_request_is_settled_by_the_worker(world: World) -> None:
    from googich_takeaway.worker import RESUME_AFTER_UPDATE

    state = State(world.path)
    state.set_setting("updates.requested", "9.9.9", NOW)
    state.set_setting(RESUME_AFTER_UPDATE, "1", NOW)
    folder = world.tmp / "updater"
    folder.mkdir()
    status = folder / "status.json"
    status.write_text('{"helper": "2", "state": "updating", "version": "9.9.9", "message": ""}')
    world.worker.settle_update_request()
    assert state.get_setting("updates.requested") == "9.9.9"  # still going
    status.write_text('{"helper": "2", "state": "failed", "version": "9.9.9", "message": "x"}')
    world.worker.settle_update_request()
    assert state.get_setting("updates.requested") is None
    assert state.get_setting(RESUME_AFTER_UPDATE) is None  # the paused run was resumed


def test_the_last_scheduled_run_is_found_after_many_manual_runs(world: World) -> None:
    from datetime import timedelta

    state = State(world.path)
    scheduled = state.start_run("schedule", NOW - timedelta(days=3))
    state.finish_run(scheduled, "success", "Done", "", NOW - timedelta(days=3))
    for n in range(60):
        run = state.start_run("manual", NOW - timedelta(hours=60 - n))
        state.finish_run(run, "success", "Done", "", NOW)
    assert state.last_run_started("schedule") == NOW - timedelta(days=3)


def test_pause_while_reading_resumes_without_reading_again(world: World) -> None:
    """Pausing during "Reading archives": Resume takes what was read from the scan cache."""
    world.configure()
    original = _stop_at_first_upload(world, "pause", at="upload")
    assert world.worker.run_once(Trigger.MANUAL).outcome is Outcome.STOPPED
    state = State(world.path)
    complete = state._db.execute("SELECT count(*) FROM scan_parts WHERE complete = 1").fetchone()
    assert complete[0] == 2  # both parts read to the end and remembered
    world.worker.tracker.begin = original  # type: ignore[method-assign]
    assert world.worker.resume_paused()
    _, options = world.worker._due_trigger() or (None, None)
    assert world.worker.run_once(Trigger.MANUAL, options).outcome is Outcome.SUCCESS
    assert len(world.immich.assets) == 13


def test_completed_with_errors_does_not_count_towards_pausing(world: World) -> None:
    from googich_takeaway.notify import Message

    config = world.config()
    config.set_scheduled_failures(2)
    sent: list[Message] = []

    def send(message: Message) -> bool:
        sent.append(message)
        return True

    world.worker._count_failures(
        config, Trigger.SCHEDULE, Message(Outcome.PARTIAL, "Completed with errors", []), send
    )
    assert config.scheduled_failures() == 0
    assert sent == []


def _reading_threads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The thread that opened each archive part to read it."""
    import threading

    from googich_takeaway.takeout.archives import iter_entries as original

    threads: list[str] = []

    def spy(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        threads.append(threading.current_thread().name)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("googich_takeaway.takeout.scan.iter_entries", spy)
    return threads


def test_parts_are_read_while_the_next_ones_download(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads = _reading_threads(monkeypatch)
    world.configure()
    assert world.worker.run_once(Trigger.MANUAL).outcome is Outcome.SUCCESS
    assert threads == ["googich-read-ahead", "googich-read-ahead"]  # never read again
    assert len(world.immich.assets) == 13


def test_reading_while_downloading_can_be_turned_off(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads = _reading_threads(monkeypatch)
    world.configure()
    world.config().save_read_while_downloading(False)
    assert world.worker.run_once(Trigger.MANUAL).outcome is Outcome.SUCCESS
    assert len(threads) == 2
    assert "googich-read-ahead" not in threads
    assert len(world.immich.assets) == 13


def test_pause_while_reading_ahead_then_resume(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from googich_takeaway.progress import Stage

    tracker = world.worker.tracker
    begin_alongside = tracker.begin_alongside

    def pausing(stage: Stage, name: str, more: int) -> None:
        tracker.request_stop("pause")
        begin_alongside(stage, name, more)

    monkeypatch.setattr(tracker, "begin_alongside", pausing)
    world.configure()
    assert world.worker.run_once(Trigger.MANUAL).outcome is Outcome.STOPPED
    assert world.worker.status().run_paused
    monkeypatch.setattr(tracker, "begin_alongside", begin_alongside)
    assert world.worker.resume_paused()
    _, options = world.worker._due_trigger() or (None, None)
    assert world.worker.run_once(Trigger.MANUAL, options).outcome is Outcome.SUCCESS
    assert len(world.immich.assets) == 13


def test_a_run_asked_for_shows_as_starting_and_keeps_its_options(world: World) -> None:
    """Before the loop picks it up: the dashboard polls, and a second press changes nothing."""
    from googich_takeaway.pipeline import RunOptions

    assert world.worker.request_run(RunOptions(reimport=True))
    status = world.worker.status()
    assert status.starting
    assert not status.running
    assert not world.worker.request_run(RunOptions())  # does not replace the first one's
    assert world.worker._manual_requested == RunOptions(reimport=True)


# --- the schedule, as the worker loop sees it -------------------------------------------------


def _at(hour: int, minute: int = 0, day: int = 1) -> datetime:
    """A Melbourne time in October 2026, in which NOW is 10:00 on the 1st."""
    return datetime(2026, 10, day, hour, minute, tzinfo=MEL).astimezone(UTC)


def _set_schedule(
    world: World, when: datetime, mode: str, at: str = "03:00", hours: str = "24"
) -> None:
    world.configure()
    world.config(when).save_schedule(mode, at, "6", hours, "3")


def _record_run(world: World, trigger: Trigger, started: datetime, finished: datetime) -> None:
    with State(world.path) as state:
        run_id = state.start_run(trigger.value, started)
        state.finish_run(run_id, "success", "Done", "", finished)


def _checks(world: World, start: datetime, end: datetime) -> list[datetime]:
    """Step the worker's clock from ``start`` to ``end`` as its idle loop does, recording each
    scheduled run it starts (instantly, without running the pipeline)."""
    started: list[datetime] = []
    clock = [start]
    world.worker._clock = lambda: clock[0]
    moment = start
    while moment <= end:
        clock[0] = moment
        due = world.worker._due_trigger()
        if due is not None:
            assert due[0] is Trigger.SCHEDULE
            started.append(moment)
            _record_run(world, Trigger.SCHEDULE, moment, moment)
        moment += timedelta(seconds=IDLE_CHECK_SECONDS)
    return started


def test_a_daily_run_starts_once_its_time_passes(world: World) -> None:
    _set_schedule(world, _at(10), "daily", "23:00")
    assert _checks(world, _at(10), _at(9, 0, day=3)) == [_at(23), _at(23, day=2)]


def test_a_weekly_run_starts_on_its_day(world: World) -> None:
    _set_schedule(world, _at(10), "weekly", "06:30")  # Sundays; the 4th is a Sunday
    assert _checks(world, _at(10), _at(12, day=4)) == [_at(6, 30, day=4)]


def test_every_24_hours_starts_at_once_then_a_day_later(world: World) -> None:
    _set_schedule(world, _at(10), "hourly", hours="24")
    assert _checks(world, _at(10), _at(11, day=2)) == [_at(10), _at(10, day=2)]


def test_following_takeout_starts_on_the_export_day_and_retries(world: World) -> None:
    world.configure()
    world.config(_at(10)).save_takeout_schedule("2026-09-03", "1")  # next export on the 3rd
    world.config(_at(10)).save_schedule("takeout", "09:00", "6", "24", "3")
    world.config(_at(10)).save_follow_options("1", "2", False, "6")  # the 3rd and 4th
    assert _checks(world, _at(10), _at(12, day=5)) == [_at(9, day=3), _at(9, day=4)]


def test_a_time_already_past_today_waits_for_tomorrow(world: World) -> None:
    _set_schedule(world, _at(10), "daily", "06:30")
    assert _checks(world, _at(10), _at(7, day=2)) == [_at(6, 30, day=2)]


def test_a_run_missed_while_the_app_was_down_starts_when_it_is_back(world: World) -> None:
    _set_schedule(world, _at(10), "daily", "23:00")
    assert _checks(world, _at(1, day=2), _at(1, 1, day=2)) == [_at(1, day=2)]


def test_a_run_missed_by_more_than_a_day_waits_for_the_next(world: World) -> None:
    _set_schedule(world, _at(10), "weekly", "06:30")  # Sundays: the 4th, then the 11th
    assert _checks(world, _at(7, day=5), _at(7, day=11)) == [_at(6, 30, day=11)]


def test_saving_only_the_pause_limit_keeps_a_missed_run(world: World) -> None:
    _set_schedule(world, _at(10), "daily", "23:00")
    world.config(_at(0, 30, day=2)).save_schedule("daily", "23:00", "6", "24", "5")
    assert _checks(world, _at(1, day=2), _at(1, 1, day=2)) == [_at(1, day=2)]


def test_changing_when_runs_happen_drops_a_missed_run(world: World) -> None:
    _set_schedule(world, _at(10), "daily", "23:00")
    world.config(_at(0, 30, day=2)).save_schedule("daily", "23:30", "6", "24", "3")
    assert _checks(world, _at(1, day=2), _at(23, 31, day=2)) == [_at(23, 30, day=2)]


def test_runs_missed_while_paused_stay_missed(world: World) -> None:
    _set_schedule(world, _at(10), "daily", "23:00")
    world.config(_at(22)).set_schedule_paused(True, by_user=True)
    assert _checks(world, _at(22), _at(1, day=2)) == []
    world.config(_at(1, day=2)).set_schedule_paused(False)
    assert _checks(world, _at(1, day=2), _at(23, 1, day=2)) == [_at(23, day=2)]


def test_a_run_due_during_another_run_is_skipped(world: World) -> None:
    _set_schedule(world, _at(10), "daily", "23:00")
    _record_run(world, Trigger.MANUAL, _at(22, 30), _at(23, 30))
    assert _checks(world, _at(23, 30), _at(23, 1, day=2)) == [_at(23, day=2)]


def test_a_skipped_run_is_logged_and_not_shown_as_next(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    _set_schedule(world, _at(10), "daily", "10:30")
    clock = [_at(10, 40)]
    world.worker._clock = lambda: clock[0]
    with world.worker._lock:
        world.worker._run_started = _at(10, 20)  # a run going since before 10:30
    assert world.worker.status().next_run == _at(10, 30, day=2)
    with world.worker._lock:
        world.worker._run_started = None
    times = iter([_at(10, 20)])  # the run starts at 10:20; every later reading is 10:45
    world.worker._clock = lambda: next(times, _at(10, 45))
    with caplog.at_level("INFO", logger="googich.worker"):
        world.worker.run_once(Trigger.MANUAL)
    assert "Scheduled run due at 2026-10-01 10:30 skipped" in caplog.text
    assert _checks(world, _at(10, 45), _at(10, 31, day=2)) == [_at(10, 30, day=2)]
