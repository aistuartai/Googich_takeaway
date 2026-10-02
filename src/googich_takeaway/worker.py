"""The background worker: runs the pipeline on schedule or on request, one run at a time.

- On start, runs left open by a crash or restart are closed as interrupted. Downloads resume on
  the next run because partial files are kept.
- A scheduled run that fails counts towards the pause limit. When the limit is reached the
  schedule pauses itself and a "paused" notification is sent; it stays paused until resumed in
  the web interface. A successful run resets the count. Manual runs never pause the schedule.
- Every run's outcome is recorded and notified (if that outcome is switched on).
"""

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

from googich_takeaway import reminders, updates
from googich_takeaway.config import Config
from googich_takeaway.credentials import SecretBox
from googich_takeaway.destinations.immich import ImmichClient
from googich_takeaway.notify import Message, Outcome
from googich_takeaway.pipeline import DriveFactory, ImmichFactory, Pipeline, RunOptions
from googich_takeaway.progress import RunStopped, Snapshot, Stage, Tracker
from googich_takeaway.schedule import next_run
from googich_takeaway.sources.gdrive import GoogleDriveSource
from googich_takeaway.state import State

log = logging.getLogger("googich.worker")

IDLE_CHECK_SECONDS = 30.0
PAUSED_RUN = "run.paused"
"""The options of a run the user paused, so Resume continues it."""
"""How often the worker re-reads the schedule while idle, so setting changes take effect."""


class Trigger(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"


@dataclass(frozen=True)
class WorkerStatus:
    running: bool
    run_started: datetime | None
    next_run: datetime | None
    paused: bool
    """The schedule is paused."""
    stopping: str | None = None
    """``pause`` or ``cancel`` once pressed, until the run reaches a safe point."""
    run_paused: bool = False
    """A run was paused and can be resumed."""
    paused_by_user: bool = False
    """The schedule was paused with Pause schedule, not after failed runs."""
    paused_progress: list[dict[str, object]] = field(default_factory=list)
    """How far a paused run got, stage by stage, for the dashboard."""


class Worker:
    def __init__(
        self,
        state_path: Path,
        box: SecretBox,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] | None = None,
        immich_factory: ImmichFactory = ImmichClient,
        drive_factory: DriveFactory = GoogleDriveSource,
    ) -> None:
        self._state_path = state_path
        self._box = box
        self._clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._sleep = sleep or self._interruptible_sleep
        self._immich_factory = immich_factory
        self._drive_factory = drive_factory
        self._lock = threading.Lock()
        self._manual_requested: RunOptions | None = None
        self._run_started: datetime | None = None
        self._thread: threading.Thread | None = None
        self.tracker = Tracker(self._load_rates())

    def _interruptible_sleep(self, seconds: float) -> None:
        """Retry backoff that ends early when the app is stopping."""
        self._stop.wait(seconds)

    def _load_rates(self) -> dict[Stage, float]:
        with State(self._state_path) as state:
            stored = state.get_setting("progress.rates")
        if not stored:
            return {}
        return {Stage(k): float(v) for k, v in json.loads(stored).items() if k in Stage}

    def progress(self) -> Snapshot:
        return self.tracker.snapshot()

    def claim_for_demo(self) -> bool:
        """Mark the worker busy for a simulated run (demo mode only). False if already busy."""
        with self._lock:
            if self._run_started is not None:
                return False
            self._run_started = self._clock()
        self.tracker.start_run()
        return True

    def release_from_demo(self) -> None:
        self.tracker.finish_run()  # measured demo speeds are discarded, never saved
        with self._lock:
            self._run_started = None

    # --- control -------------------------------------------------------------------------------

    def start(self) -> None:
        with State(self._state_path) as state:
            closed = state.abandon_unfinished_runs(self._clock())
        if closed:
            log.warning("Closed %d run(s) interrupted by a restart; they resume next run", closed)
        self._thread = threading.Thread(target=self._loop, name="googich-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def request_run(self, options: RunOptions | None = None) -> bool:
        """Ask for a run now. False if one is already running."""
        with self._lock:
            if self._run_started is not None:
                return False
            self._manual_requested = options or RunOptions()
        self._wake.set()
        return True

    def request_stop(self, kind: str) -> bool:
        """Pause or cancel the run going now, at its next safe point. False if none is going."""
        if kind not in ("pause", "cancel"):
            raise ValueError(kind)
        return self.tracker.request_stop(kind)

    def resume_paused(self) -> bool:
        """Continue a paused run, with the options it had. False if none is paused."""
        with State(self._state_path) as state:
            stored = state.get_setting(PAUSED_RUN)
        if stored is None:
            return False
        options = RunOptions(**json.loads(stored).get("options", {}))
        return self.request_run(options)

    def discard_paused(self) -> None:
        with State(self._state_path) as state:
            state.set_setting(PAUSED_RUN, None, self._clock())

    def status(self) -> WorkerStatus:
        with State(self._state_path) as state:
            config = self._config(state)
            try:
                due = self._next_due(state, config)
            except Exception:
                log.exception("Could not work out the next scheduled run")
                due = None
            paused = config.schedule_paused()
            by_user = config.schedule_paused_by_user()
            paused_run = json.loads(state.get_setting(PAUSED_RUN) or "null")
        with self._lock:
            running = self._run_started is not None
            return WorkerStatus(
                running,
                self._run_started,
                due,
                paused,
                stopping=self.tracker.stopping if running else None,
                run_paused=paused_run is not None and not running,
                paused_by_user=by_user,
                paused_progress=paused_run.get("progress", []) if paused_run else [],
            )

    # --- loop ----------------------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                due = self._due_trigger()
            except Exception:  # never let a bad setting stop the worker for good
                log.exception("Could not work out whether a run is due; trying again shortly")
                due = None
            if due is not None:
                try:
                    self.run_once(*due)
                except Exception:
                    log.exception("Run crashed")
                continue
            self._check_updates()
            self._check_reminders()
            self._wake.wait(IDLE_CHECK_SECONDS)
            self._wake.clear()

    def _due_trigger(self) -> tuple[Trigger, RunOptions] | None:
        with self._lock:
            if self._manual_requested is not None:
                options, self._manual_requested = self._manual_requested, None
                return Trigger.MANUAL, options
        with State(self._state_path) as state:
            config = self._config(state)
            if config.schedule_paused():
                return None
            due = self._next_due(state, config)
        if due is not None and due <= self._clock():
            return Trigger.SCHEDULE, RunOptions()
        return None

    def _check_updates(self) -> None:
        try:
            with State(self._state_path) as state:
                updates.check_if_due(state, self._clock)
        except Exception:  # an update check must never stop the worker
            log.exception("Update check failed")

    def _check_reminders(self) -> None:
        try:
            with State(self._state_path) as state:
                reminders.send_reminders(self._config(state), state, self._clock())
        except Exception:  # a reminder must never stop the worker
            log.exception("Reminder check failed")

    def _next_due(self, state: State, config: Config) -> datetime | None:
        schedule = config.schedule()
        last = next((r.started_at for r in state.recent_runs(50) if r.trigger == "schedule"), None)
        zone = ZoneInfo(config.general().timezone)
        return next_run(schedule, self._clock(), zone, last)

    def run_once(self, trigger: Trigger, options: RunOptions | None = None) -> Message:
        """Run the pipeline now, record it and notify. Used by the loop and by tests."""
        now = self._clock()
        with self._lock:
            self._run_started = now
        self.tracker.start_run()
        try:
            with State(self._state_path) as state:
                config = self._config(state)
                run_id = state.start_run(trigger.value, now)
                log.info("Run %d started (%s)", run_id, trigger.value)
                pipeline = Pipeline(
                    config,
                    state,
                    self._clock,
                    self._sleep,
                    immich_factory=self._immich_factory,
                    drive_factory=self._drive_factory,
                    options=options or RunOptions(),
                    tracker=self.tracker,
                )
                try:
                    message = pipeline.run().message()
                except RunStopped as stop:
                    return self._stopped(state, run_id, stop.kind, options or RunOptions())
                except Exception as error:  # last resort: still record and notify the run
                    log.exception("Run %d crashed", run_id)
                    message = Message(
                        Outcome.FAILED,
                        f"Run failed: unexpected error ({type(error).__name__})",
                        ["Details are in the log."],
                    )
                chosen = (options or RunOptions()).describe()
                if chosen:
                    message.lines.append(f"Options: {', '.join(chosen)}.")
                state.finish_run(
                    run_id, message.outcome.value, message.title, message.body, self._clock()
                )
                log.info("Run %d finished: %s", run_id, message.title)
                state.set_setting(PAUSED_RUN, None, self._clock())  # nothing left to resume
                notifier = config.notifier()
                notifier.send(message)
                self._count_failures(config, trigger, message, notifier.send)
                return message
        finally:
            measured = self.tracker.finish_run()
            if measured:
                with State(self._state_path) as state:
                    stored = json.loads(state.get_setting("progress.rates") or "{}")
                    stored.update({stage.value: rate for stage, rate in measured.items()})
                    state.set_setting("progress.rates", json.dumps(stored), self._clock())
            with self._lock:
                self._run_started = None

    def _stopped(self, state: State, run_id: int, kind: str, options: RunOptions) -> Message:
        """Record a run the user paused or cancelled. No notification; not a failure."""
        if kind == "pause":
            title = "Paused by you"
            body = (
                "Press Resume on the dashboard to continue. Downloads carry on from where they "
                "stopped, and files already in Immich are not sent again."
            )
            progress = [
                {
                    "label": view.label,
                    "done": view.done,
                    "total": view.total,
                    "files_done": view.files_done,
                    "files_total": view.files_total,
                    "files_failed": view.files_failed,
                }
                for view in self.tracker.snapshot().stages
            ]
            paused = {"options": options.__dict__, "progress": progress}
            state.set_setting(PAUSED_RUN, json.dumps(paused), self._clock())
        else:
            title = "Cancelled by you"
            body = "The next scheduled or manual run carries on from where this one stopped."
            state.set_setting(PAUSED_RUN, None, self._clock())
        state.finish_run(
            run_id, "paused" if kind == "pause" else "cancelled", title, body, self._clock()
        )
        log.warning("Run %d %s", run_id, "paused" if kind == "pause" else "cancelled")
        return Message(Outcome.STOPPED, title, [body])

    def _count_failures(
        self,
        config: Config,
        trigger: Trigger,
        message: Message,
        send: Callable[[Message], bool],
    ) -> None:
        if message.outcome is not Outcome.FAILED:
            config.set_scheduled_failures(0)
            return
        if trigger is not Trigger.SCHEDULE:
            return
        failures = config.scheduled_failures() + 1
        config.set_scheduled_failures(failures)
        limit = config.schedule().pause_after
        if failures >= limit and not config.schedule_paused():
            config.set_schedule_paused(True)
            log.warning("Schedule paused after %d failed runs in a row", failures)
            send(
                Message(
                    Outcome.PAUSED,
                    f"Schedule paused after {failures} failed runs",
                    [
                        f"Last error: {message.title.removeprefix('Run failed: ')}",
                        "Fix the problem, then press Resume on the dashboard.",
                    ],
                )
            )

    def _config(self, state: State) -> Config:
        return Config(state, self._box, self._clock)
