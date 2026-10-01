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
    total: int
    done: int
    rate: float | None
    """Bytes per second; None until measured."""
    estimated_rate: float
    """Live rate if known, otherwise the historical or default one."""
    measured: bool
    eta_seconds: float | None

    @property
    def fraction(self) -> float:
        return self.done / self.total if self.total else 1.0


@dataclass(frozen=True)
class Snapshot:
    running: bool
    stage: Stage | None
    stages: list[StageView]
    eta_seconds: float | None
    """Whole run, including stages not started yet."""
    eta_is_estimate: bool


@dataclass
class _StageState:
    items: dict[str, Item] = field(default_factory=dict)
    rate: float | None = None
    window_bytes: int = 0
    window_start: float = 0.0
    started: bool = False


class Tracker:
    """Shared between the worker thread (updates) and web requests (snapshots)."""

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
        self._active: str | None = None
        self._running = False

    # --- updates from the run --------------------------------------------------------------------

    def start_run(self) -> None:
        with self._lock:
            self._stages = {s: _StageState() for s in Stage}
            self._current, self._active, self._running = None, None, True

    def finish_run(self) -> dict[Stage, float]:
        """End the run; returns the rates measured, to remember for the next run's estimates."""
        with self._lock:
            self._running = False
            self._current = self._active = None
            measured = {s: st.rate for s, st in self._stages.items() if st.rate is not None}
            self._history.update(measured)
            return measured

    def plan(self, stage: Stage, items: list[tuple[str, int]]) -> None:
        """Add items expected in ``stage`` (names unique within the stage)."""
        with self._lock:
            state = self._stages[stage]
            for name, size in items:
                state.items.setdefault(name, Item(name, size))

    def begin(self, stage: Stage, name: str, size: int | None = None) -> None:
        with self._lock:
            state = self._stages[stage]
            item = state.items.get(name) or Item(name, size or 0)
            if size is not None:
                item = replace(item, size=size)
            state.items[name] = replace(item, state=ItemState.ACTIVE)
            if not state.started:
                state.started = True
                state.window_start = self._clock()
            self._current, self._active = stage, name

    def advance(self, amount: int) -> None:
        with self._lock:
            if self._current is None or self._active is None:
                return
            state = self._stages[self._current]
            item = state.items[self._active]
            state.items[self._active] = replace(item, done=item.done + amount)
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
            done = item.size if state is ItemState.DONE else item.done
            stage_state.items[name] = replace(item, state=state, done=done, detail=detail)
            if self._active == name:
                self._active = None

    # --- reading ---------------------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        with self._lock:
            views = [self._view(stage) for stage in Stage]
            current, running = self._current, self._running
        remaining = [v.eta_seconds for v in views if v.eta_seconds]
        return Snapshot(
            running=running,
            stage=current,
            stages=[v for v in views if v.items],
            eta_seconds=sum(remaining) if remaining else None,
            eta_is_estimate=any(not v.measured for v in views if v.items and v.done < v.total),
        )

    def _view(self, stage: Stage) -> StageView:
        state = self._stages[stage]
        items = list(state.items.values())
        counted = [i for i in items if i.state is not ItemState.SKIPPED]
        total = sum(i.size for i in counted)
        done = sum(min(i.done, i.size) for i in counted)
        measured = state.rate is not None
        rate = state.rate if measured else self._history[stage]
        left = total - done
        eta = left / rate if left > 0 and rate else None
        return StageView(
            stage=stage,
            label=STAGE_LABELS[stage],
            items=items,
            total=total,
            done=done,
            rate=state.rate,
            estimated_rate=rate or 0.0,
            measured=measured,
            eta_seconds=eta,
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
