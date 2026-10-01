import pytest

from googich_takeaway.progress import (
    DEFAULT_RATES,
    ItemState,
    Stage,
    Tracker,
    format_duration,
    format_size,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_estimates_before_a_stage_starts_use_default_rates() -> None:
    tracker = Tracker(clock=Clock())
    tracker.start_run()
    tracker.plan(Stage.DOWNLOAD, [("a.zip", 100_000_000)])
    snap = tracker.snapshot()
    assert snap.eta_is_estimate
    assert snap.eta_seconds == pytest.approx(100_000_000 / DEFAULT_RATES[Stage.DOWNLOAD])


def test_measured_rate_drives_the_eta() -> None:
    clock = Clock()
    tracker = Tracker(clock=clock)
    tracker.start_run()
    tracker.plan(Stage.DOWNLOAD, [("a.zip", 100), ("b.zip", 100)])
    tracker.begin(Stage.DOWNLOAD, "a.zip")
    clock.now = 2.0
    tracker.advance(50)  # 25 B/s
    view = tracker.snapshot().stages[0]
    assert view.rate == pytest.approx(25)
    assert view.measured
    assert view.eta_seconds == pytest.approx(150 / 25)
    assert not tracker.snapshot().eta_is_estimate


def test_rate_is_smoothed() -> None:
    clock = Clock()
    tracker = Tracker(clock=clock)
    tracker.start_run()
    tracker.begin(Stage.UPLOAD, "x", 10_000)
    clock.now = 1.0
    tracker.advance(100)  # 100 B/s
    clock.now = 2.0
    tracker.advance(1100)  # a burst of 1100 B/s
    rate = tracker.snapshot().stages[0].rate
    assert rate is not None
    assert 100 < rate < 1100


def test_done_and_skipped_items() -> None:
    tracker = Tracker(clock=Clock())
    tracker.start_run()
    tracker.plan(Stage.SCAN, [("e1", 100), ("e2", 100)])
    tracker.begin(Stage.SCAN, "e1")
    tracker.end(Stage.SCAN, "e1")
    tracker.end(Stage.SCAN, "e2", ItemState.SKIPPED, "still being written")
    view = tracker.snapshot().stages[0]
    assert view.total == 100  # skipped items do not count
    assert view.done == 100
    assert view.eta_seconds is None
    assert [i.state for i in view.items] == [ItemState.DONE, ItemState.SKIPPED]


def test_finish_returns_measured_rates_for_next_time() -> None:
    clock = Clock()
    tracker = Tracker(clock=clock)
    tracker.start_run()
    tracker.begin(Stage.DOWNLOAD, "a", 1000)
    clock.now = 1.0
    tracker.advance(500)
    measured = tracker.finish_run()
    assert measured == {Stage.DOWNLOAD: pytest.approx(500)}
    tracker.start_run()
    tracker.plan(Stage.DOWNLOAD, [("b", 1000)])
    assert tracker.snapshot().eta_seconds == pytest.approx(2.0)  # uses the remembered rate


def test_formatting() -> None:
    assert format_duration(None) == "—"
    assert format_duration(42) == "42 s"
    assert format_duration(125) == "2 min 05 s"
    assert format_duration(3 * 3600 + 120) == "3 h 02 min"
    assert format_size(999) == "999 B"
    assert format_size(345_500_000) == "345.5 MB"
