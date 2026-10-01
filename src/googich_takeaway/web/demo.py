"""A simulated run for previewing the progress views, available only with GOOGICH_DEMO=1.

It drives the same progress tracker the real worker uses, with made-up files at realistic
speeds, so the dashboard's bars and journey animation can be judged without moving real data.
Nothing is downloaded, uploaded or recorded, and no measured speeds are saved.
"""

import random
import threading
import time
from collections.abc import Callable

from googich_takeaway.progress import Stage, Tracker
from googich_takeaway.worker import Worker

TICK = 0.25


def play(worker: Worker, sleep: Callable[[float], None] = time.sleep, speed: float = 1.0) -> bool:
    """Start a demo run in the background. False if a run is already going."""
    if not worker.claim_for_demo():
        return False
    thread = threading.Thread(
        target=_run, args=(worker, sleep, speed), name="googich-demo", daemon=True
    )
    thread.start()
    return True


def _run(worker: Worker, sleep: Callable[[float], None], speed: float) -> None:
    tracker = worker.tracker
    rng = random.Random(7)  # noqa: S311 - simulated transfer speeds, not security
    try:
        archives = [
            ("takeout-20261001T100632Z-1-001.tgz", 345_519_888),
            ("takeout-20261001T100632Z-001.tgz", 56_192),
        ]
        tracker.plan(Stage.DOWNLOAD, archives)
        for name, size in archives:
            _transfer(tracker, Stage.DOWNLOAD, name, size, 18e6 * speed, rng, sleep)
        tracker.plan(Stage.SCAN, [("20261001T100632Z", 345_576_080)])
        _transfer(tracker, Stage.SCAN, "20261001T100632Z", 345_576_080, 90e6 * speed, rng, sleep)
        photos = [
            (
                f"Takeout/Google Photos/Holiday/PXL_2024{n:04d}_0900{n:02d}.MP.jpg",
                rng.randint(1, 12) * 1_000_000,
            )
            for n in range(1, 37)
        ]
        tracker.plan(Stage.UPLOAD, photos)
        for name, size in photos:
            _transfer(tracker, Stage.UPLOAD, name, size, 14e6 * speed, rng, sleep)
    finally:
        worker.release_from_demo()


def _transfer(
    tracker: Tracker,
    stage: Stage,
    name: str,
    size: int,
    rate: float,
    rng: random.Random,
    sleep: Callable[[float], None],
) -> None:
    tracker.begin(stage, name, size)
    done = 0
    while done < size:
        step = min(size - done, int(rate * TICK * rng.uniform(0.7, 1.3)))
        done += max(step, 1)
        tracker.advance(max(step, 1))
        sleep(TICK)
    tracker.end(stage, name)
