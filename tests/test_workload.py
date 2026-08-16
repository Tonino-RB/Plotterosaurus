"""The shared budget for heavy background work.

Uploading three complex designs at once used to put three long CPU-bound jobs
on a four-core Pi simultaneously — the ink cache measuring, the optimize queue
simplifying, the plan queue simulating — each with a subprocess of its own.
Memory was never the limit (peak 290MB with 1.8GB free); sustained load was.
That is what makes the board hot and what browns out a marginal supply, and it
is also what starves the thread writing to the plotter's serial port.
"""
import threading
import time

from app import workload


def test_only_one_heavy_job_runs_at_a_time():
    """Three uploads arriving together must be worked through, not raced."""
    concurrent = 0
    peak = 0
    lock = threading.Lock()

    def job():
        nonlocal concurrent, peak
        with workload.heavy("test"):
            with lock:
                concurrent += 1
                peak = max(peak, concurrent)
            time.sleep(0.05)
            with lock:
                concurrent -= 1

    threads = [threading.Thread(target=job) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert peak == 1, f"{peak} heavy jobs ran at once"


def test_the_slot_is_released_when_a_job_raises():
    """A failed measurement must not wedge the queue behind it forever."""
    try:
        with workload.heavy("boom"):
            raise RuntimeError("measurement failed")
    except RuntimeError:
        pass
    assert not workload.busy()
    # And the slot is genuinely reusable.
    with workload.heavy("after"):
        assert workload.busy()
    assert not workload.busy()


def test_deprioritize_only_touches_the_calling_thread():
    """Background workers drop below normal priority so the plot worker and the
    event loop always win. On Linux niceness is per-task, so this must not
    follow the whole process — a niced plot thread means late serial writes,
    and late serial writes are visible on paper."""
    import os

    main_before = os.getpriority(os.PRIO_PROCESS, 0)
    seen = {}

    def worker():
        workload.deprioritize()
        seen["worker"] = os.getpriority(os.PRIO_PROCESS, 0)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert seen["worker"] >= main_before + workload.NICE
    assert os.getpriority(os.PRIO_PROCESS, 0) == main_before
