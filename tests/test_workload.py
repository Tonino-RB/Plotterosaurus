"""The shared budget for heavy background work.

Uploading three complex designs at once used to put three long CPU-bound jobs
on a four-core Pi simultaneously — the ink cache measuring, the optimize queue
simplifying, the plan queue simulating — each with a subprocess of its own.
Memory was never the limit (peak 290MB with 1.8GB free); sustained load was.
That is what makes the board hot and what browns out a marginal supply, and it
is also what starves the thread writing to the plotter's serial port.
"""
import os
import threading
import time
from pathlib import Path

import pytest

from app import export, main, plot_worker, state, svg_utils, workload


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


# The two paths that used to escape the budget ------------------------------

FIXTURE = Path(__file__).parent / "fixtures" / "multi-layer.svg"


def test_a_cold_ink_measurement_is_not_parsed_on_the_calling_thread(
        job_from_svg, monkeypatch):
    """The plot worker's bounds guards ask ink_cache first and used to fall
    through to a full vpype read of the document *inline* when it missed — on
    a FastAPI worker thread for a mid-pause nudge, on the plot worker's own
    thread before a run. Normal priority, outside the slot, and duplicating a
    measurement the UI had usually queued already."""
    job = job_from_svg(FIXTURE, optimize_svg=False)
    svg = main.UPLOAD_DIR / f"{job['svg_id']}.svg"

    def no_inline_parse(*args, **kwargs):
        raise AssertionError("parsed the document on the calling thread")

    monkeypatch.setattr(svg_utils, "ink_rect_doc_mm", no_inline_parse)

    seen = {}

    def watched(path):
        seen["busy"] = workload.busy()
        seen["nice"] = os.getpriority(os.PRIO_PROCESS, 0)
        seen["thread"] = threading.current_thread().name
        return {0: {"rect": (10.0, 10.0, 90.0, 60.0), "length_mm": 120.0}}

    monkeypatch.setattr(svg_utils, "measure_layers", watched)

    bounds = plot_worker._job_ink_bounds(job, svg)

    assert bounds is not None, "the guard got no answer to guard with"
    assert seen["thread"] == "ink-measure"
    assert seen["busy"] is True, "measured outside the single heavy slot"
    assert seen["nice"] >= os.getpriority(os.PRIO_PROCESS, 0) + workload.NICE


def test_a_leftover_jog_no_longer_blocks_the_run(job_from_svg, monkeypatch):
    """_run_job used to measure the drawing's ink before touching anything, to
    fail the job if a leftover jog put the artwork off the page. That measure
    was a full vpype parse on a cold cache, so every plot paid for it while the
    card still read `ready`, and the one control for aiming the pen at real
    paper could make a job unplottable. Both are gone: the run goes ahead and
    the plot is clipped at the bed edge (plot_worker._apply_plot_bounds)."""
    job = job_from_svg(FIXTURE, optimize_svg=False)
    job_id = job["job_id"]
    measured = []
    monkeypatch.setattr(svg_utils, "measure_layers",
                        lambda path: measured.append(path) or
                        {0: {"rect": (10.0, 10.0, 90.0, 60.0), "length_mm": 120.0}})
    # A leftover jog this wide puts the ink off the page whatever the drawing is.
    state.set_manual_origin_offset(300.0, 0.0)
    try:
        plot_worker._run_job(job_id)
    finally:
        state.set_manual_origin_offset(0.0, 0.0)
        state.set_origin_past_bed(0.0, 0.0)

    after = state.get_job(job_id)
    assert after["status"] != "failed" or "off the page" not in (after["error"] or ""), \
        "the removed pre-flight is still blocking the run"
    # The run reached its own planning phase instead of stopping at a bounds
    # check, which is the whole difference being pinned here.
    assert after["plan_status"] == "ready", after["plan_status"]


def test_an_export_conversion_waits_its_turn_and_runs_niced(client, job_from_svg,
                                                            monkeypatch):
    """A sync route runs on FastAPI's threadpool, which neither takes the
    heavy slot nor sits below the plot worker — so a PNG of a real drawing was
    several seconds of full-priority CPU beside a moving pen."""
    job = job_from_svg(FIXTURE, optimize_svg=False)
    seen = {}
    real = export.export

    def watched(*args, **kwargs):
        seen["busy"] = workload.busy()
        seen["nice"] = os.getpriority(os.PRIO_PROCESS, 0)
        return real(*args, **kwargs)

    monkeypatch.setattr(export, "export", watched)
    res = client.get(f"/jobs/{job['job_id']}/export", params={"fmt": "svg"})

    assert res.status_code == 200, res.text
    assert seen["busy"] is True, "converted outside the single heavy slot"
    assert seen["nice"] >= os.getpriority(os.PRIO_PROCESS, 0) + workload.NICE


def test_run_background_reraises_and_leaves_no_niced_thread_behind():
    """The niceness has to die with the thread — a pooled thread that niced
    itself would serve every later request from the bottom of the run queue."""
    before = os.getpriority(os.PRIO_PROCESS, 0)
    with pytest.raises(ValueError):
        workload.run_background(lambda: (_ for _ in ()).throw(ValueError("boom")))
    assert os.getpriority(os.PRIO_PROCESS, 0) == before
    assert not workload.busy()
