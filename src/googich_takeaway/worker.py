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
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

from googich_takeaway.config import Config
from googich_takeaway.credentials import SecretBox
from googich_takeaway.destinations.immich import ImmichClient
from googich_takeaway.notify import Message, Outcome
from googich_takeaway.pipeline import DriveFactory, ImmichFactory, Pipeline, RunOptions
from googich_takeaway.progress import Snapshot, Stage, Tracker
from googich_takeaway.schedule import next_run
from googich_takeaway.sources.gdrive import GoogleDriveSource
from googich_takeaway.state import State

log = logging.getLogger("googich.worker")

IDLE_CHECK_SECONDS = 30.0
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

    def status(self) -> WorkerStatus:
        with State(self._state_path) as state:
            config = self._config(state)
            due = self._next_due(state, config)
            paused = config.schedule_paused()
        with self._lock:
            return WorkerStatus(self._run_started is not None, self._run_started, due, paused)

    # --- loop ----------------------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            due = self._due_trigger()
            if due is not None:
                try:
                    self.run_once(*due)
                except Exception:
                    log.exception("Run crashed")
                continue
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
                message = pipeline.run().message()
                chosen = (options or RunOptions()).describe()
                if chosen:
                    message.lines.append(f"Options: {', '.join(chosen)}.")
                state.finish_run(
                    run_id, message.outcome.value, message.title, message.body, self._clock()
                )
                log.info("Run %d finished: %s", run_id, message.title)
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
