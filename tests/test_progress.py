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
    assert (view.files_done, view.files_skipped, view.files_waiting) == (1, 1, 0)
    assert [i.state for i in view.items] == [ItemState.SKIPPED]  # shown with its reason
    assert [i.state for i in view.recent] == [ItemState.DONE]


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


def test_snapshot_of_a_full_library_is_small_and_fast() -> None:
    import time as real_time

    tracker = Tracker(clock=Clock())
    tracker.start_run()
    tracker.plan(Stage.UPLOAD, [(f"photo-{n:05d}.jpg", 3_000_000) for n in range(30_000)])
    for n in range(12_000):
        tracker.begin(Stage.UPLOAD, f"photo-{n:05d}.jpg")
        tracker.advance(3_000_000)
        tracker.end(Stage.UPLOAD, f"photo-{n:05d}.jpg")
    tracker.begin(Stage.UPLOAD, "photo-12000.jpg")
    tracker.end(Stage.UPLOAD, "photo-12001.jpg", ItemState.FAILED, "Immich answered 500")
    started = real_time.perf_counter()
    for _ in range(50):
        view = tracker.snapshot().stages[0]
    per_snapshot = (real_time.perf_counter() - started) / 50
    assert per_snapshot < 0.01  # well under the dashboard's two-second refresh
    assert (view.files_total, view.files_done, view.files_failed) == (30_000, 12_000, 1)
    assert view.files_waiting == 17_999
    assert len(view.items) <= 27  # active, the failure, and the next 25 waiting
    assert view.items[0].name == "photo-12000.jpg"
    assert view.items[1].detail == "Immich answered 500"


def test_eta_counts_files_when_overhead_dominates() -> None:
    clock = Clock()
    tracker = Tracker(clock=clock)
    tracker.start_run()
    tracker.plan(Stage.UPLOAD, [(f"p{n}", 1000) for n in range(100)])
    for n in range(10):  # ten tiny files take ten seconds: one file a second
        tracker.begin(Stage.UPLOAD, f"p{n}")
        clock.now += 1.0
        tracker.advance(1000)
        tracker.end(Stage.UPLOAD, f"p{n}")
    view = tracker.snapshot().stages[0]
    assert view.eta_seconds == pytest.approx(
        90, rel=0.01
    )  # 90 files left, not 90 kB at a fast rate
