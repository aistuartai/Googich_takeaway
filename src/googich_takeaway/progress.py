"""Live progress of a run, with sizes, rates and time estimates.

A run moves through three stages: download, scan (read archives, hash files, pair sidecars) and
upload. Each stage has a queue of items with known sizes, so the queue view can show what is
waiting, what is active and what is done, with an ETA per item and for the whole run.

Rates are smoothed so a single stall does not swing the estimate. Before a stage has started, its
ETA comes from the rate measured on earlier runs (kept in the state database), or a conservative
default on the very first run; such estimates are labelled as estimates.
"""

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum


class Stage(StrEnum):
    DOWNLOAD = "download"
    SCAN = "scan"
    UPLOAD = "upload"


STAGE_LABELS = {
    Stage.DOWNLOAD: "Downloading",
    Stage.SCAN: "Reading archives",
    Stage.UPLOAD: "Uploading",
}

# Conservative first-run guesses, in bytes per second.
DEFAULT_RATES = {Stage.DOWNLOAD: 10e6, Stage.SCAN: 80e6, Stage.UPLOAD: 15e6}
SMOOTHING = 0.2  # weight of the newest sample in the moving average
SAMPLE_SECONDS = 1.0


class ItemState(StrEnum):
    WAITING = "waiting"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Item:
    name: str
    size: int
    state: ItemState = ItemState.WAITING
    done: int = 0
    detail: str = ""


@dataclass(frozen=True)
class StageView:
    stage: Stage
    label: str
    items: list[Item]
    """A window for display: the active file, failures, then the next files waiting.

    Never every item: a full library has tens of thousands, and the dashboard polls."""
    total: int
    done: int
    rate: float | None
    """Bytes per second; None until measured."""
    estimated_rate: float
    """Live rate if known, otherwise the historical or default one."""
    measured: bool
    eta_seconds: float | None
    files_total: int = 0
    files_done: int = 0
    files_failed: int = 0
    files_skipped: int = 0
    recent: list[Item] = field(default_factory=list)
    """The last few files finished."""

    @property
    def fraction(self) -> float:
        return self.done / self.total if self.total else 1.0

    @property
    def files_waiting(self) -> int:
        return self.files_total - self.files_done - self.files_failed - self.files_skipped


@dataclass(frozen=True)
class Snapshot:
    running: bool
    stage: Stage | None
    stages: list[StageView]
    eta_seconds: float | None
    """Whole run, including stages not started yet."""
    eta_is_estimate: bool


WINDOW_WAITING = 25
KEEP_FAILED = 50
KEEP_RECENT = 5
MIN_FILES_FOR_FILE_RATE = 3


@dataclass
class _StageState:
    items: dict[str, Item] = field(default_factory=dict)
    waiting: dict[str, None] = field(default_factory=dict)
    """Names not started yet, in plan order (a dict keeps insertion order)."""
    total: int = 0
    done: int = 0
    files_done: int = 0
    files_failed: int = 0
    files_skipped: int = 0
    failed: list[Item] = field(default_factory=list)
    recent: deque[Item] = field(default_factory=lambda: deque(maxlen=KEEP_RECENT))
    active: str | None = None
    rate: float | None = None
    window_bytes: int = 0
    window_start: float = 0.0
    started_at: float | None = None


class Tracker:
    """Shared between the worker thread (updates) and web requests (snapshots).

    Totals are kept as running counters, so a snapshot costs the same for 50 files or 50,000.
    """

    def __init__(
        self,
        rates: dict[Stage, float] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._history = {**DEFAULT_RATES, **(rates or {})}
        self._stages: dict[Stage, _StageState] = {s: _StageState() for s in Stage}
        self._current: Stage | None = None
        self._running = False

    # --- updates from the run --------------------------------------------------------------------

    def start_run(self) -> None:
        with self._lock:
            self._stages = {s: _StageState() for s in Stage}
            self._current, self._running = None, True

    def finish_run(self) -> dict[Stage, float]:
        """End the run; returns the rates measured, to remember for the next run's estimates."""
        with self._lock:
            self._running = False
            self._current = None
            measured = {s: st.rate for s, st in self._stages.items() if st.rate is not None}
            self._history.update(measured)
            return measured

    def plan(self, stage: Stage, items: list[tuple[str, int]]) -> None:
        """Add items expected in ``stage`` (names unique within the stage)."""
        with self._lock:
            state = self._stages[stage]
            for name, size in items:
                if name in state.items:
                    continue
                state.items[name] = Item(name, size)
                state.waiting[name] = None
                state.total += size

    def begin(self, stage: Stage, name: str, size: int | None = None) -> None:
        with self._lock:
            state = self._stages[stage]
            item = state.items.get(name)
            if item is None:
                item = Item(name, size or 0)
                state.total += item.size
            elif size is not None and size != item.size:
                state.total += size - item.size
                item = replace(item, size=size)
            state.items[name] = replace(item, state=ItemState.ACTIVE)
            state.waiting.pop(name, None)
            state.active = name
            now = self._clock()
            if state.started_at is None:
                state.started_at = now
                state.window_start = now
            self._current = stage

    def advance(self, amount: int) -> None:
        with self._lock:
            if self._current is None:
                return
            state = self._stages[self._current]
            if state.active is None:
                return
            item = state.items[state.active]
            counted = min(amount, max(0, item.size - item.done))
            state.items[state.active] = replace(item, done=item.done + amount)
            state.done += counted
            state.window_bytes += amount
            now = self._clock()
            elapsed = now - state.window_start
            if elapsed >= SAMPLE_SECONDS:
                sample = state.window_bytes / elapsed
                state.rate = (
                    sample
                    if state.rate is None
                    else (1 - SMOOTHING) * state.rate + SMOOTHING * sample
                )
                state.window_bytes, state.window_start = 0, now

    def end(
        self, stage: Stage, name: str, state: ItemState = ItemState.DONE, detail: str = ""
    ) -> None:
        with self._lock:
            stage_state = self._stages[stage]
            item = stage_state.items.get(name) or Item(name, 0)
            if item.state in (ItemState.DONE, ItemState.FAILED, ItemState.SKIPPED):
                return  # already finished
            counted = min(item.done, item.size)
            if name not in stage_state.items:
                stage_state.total += item.size
            if state is ItemState.DONE:
                stage_state.done += item.size - counted
                finished = replace(item, state=state, done=item.size, detail=detail)
                stage_state.files_done += 1
                stage_state.recent.append(finished)
            elif state is ItemState.SKIPPED:
                stage_state.total -= item.size
                stage_state.done -= counted
                finished = replace(item, state=state, detail=detail)
                stage_state.files_skipped += 1
                if len(stage_state.failed) < KEEP_FAILED:
                    stage_state.failed.append(finished)  # shown with its reason, like failures
            else:
                finished = replace(item, state=state, detail=detail)
                stage_state.files_failed += 1
                if len(stage_state.failed) < KEEP_FAILED:
                    stage_state.failed.append(finished)
            stage_state.items[name] = finished
            stage_state.waiting.pop(name, None)
            if stage_state.active == name:
                stage_state.active = None

    # --- reading ---------------------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        with self._lock:
            now = self._clock()
            views = [self._view(stage, now) for stage in Stage]
            current, running = self._current, self._running
        remaining = [v.eta_seconds for v in views if v.eta_seconds]
        shown = [v for v in views if v.files_total]
        return Snapshot(
            running=running,
            stage=current,
            stages=shown,
            eta_seconds=sum(remaining) if remaining else None,
            eta_is_estimate=any(not v.measured for v in shown if v.done < v.total),
        )

    def _view(self, stage: Stage, now: float) -> StageView:
        state = self._stages[stage]
        measured = state.rate is not None
        rate = state.rate if measured else self._history[stage]
        left = state.total - state.done
        eta = left / rate if left > 0 and rate else None
        files_left = len(state.items) - state.files_done - state.files_failed - state.files_skipped
        if (
            files_left > 0
            and state.started_at is not None
            and state.files_done >= MIN_FILES_FOR_FILE_RATE
            and now > state.started_at
        ):
            # Thousands of small files are limited by per-file overhead, not bytes: use the
            # slower of the two estimates.
            files_per_second = state.files_done / (now - state.started_at)
            by_files = files_left / files_per_second
            eta = max(eta or 0.0, by_files)
        window: list[Item] = []
        if state.active is not None:
            window.append(state.items[state.active])
        window.extend(state.failed)
        for name in state.waiting:
            if len(window) >= WINDOW_WAITING + len(state.failed) + 1:
                break
            window.append(state.items[name])
        return StageView(
            stage=stage,
            label=STAGE_LABELS[stage],
            items=window,
            total=state.total,
            done=state.done,
            rate=state.rate,
            estimated_rate=rate or 0.0,
            measured=measured,
            eta_seconds=eta,
            files_total=len(state.items),
            files_done=state.files_done,
            files_failed=state.files_failed,
            files_skipped=state.files_skipped,
            recent=list(state.recent),
        )


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def format_size(value: float) -> str:
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} TB"
