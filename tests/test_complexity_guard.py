"""Refusing a drawing the machine cannot measure, instead of dying trying.

A 130,990-subpath generative drawing crashed the board outright: pyaxidraw
plans every stroke in pure Python, and the estimate for that document wanted
somewhere upward of 9GB on a 3.7GB Pi. The kernel spent itself compressing
pages into zram — which is RAM, not extra memory — and the machine stopped
answering long before the OOM killer got anywhere. An unclean reboot, no log,
and on restart the plan queue picked the same job up again.

The guard is deliberately *not* a threshold on the document. That was tried
and measured wrong: a real 17,110-subpath hatched drawing previews in 113MB,
while a 10,786-subpath fragment of the generative one takes 810MB. Point
counts order them backwards too. So the bound is measured at runtime — the
preview runs in a subprocess, its RSS is watched, and it is killed if it grows
past the limit. That works precisely because the plot path runs the same
preview before touching hardware.
"""
import json
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from app import plot_worker, svg_utils


# Counting -----------------------------------------------------------------

def _svg(body: str) -> str:
    return ('<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
            'width="100mm" height="100mm" viewBox="0 0 100 100">'
            '<g inkscape:groupmode="layer" inkscape:label="L">' + body + '</g></svg>')


def test_counts_subpaths_not_elements(tmp_path):
    """The documents that break this are a few <path> elements carrying six
    figures of moveto commands, so element counts say nothing useful."""
    p = tmp_path / "a.svg"
    p.write_text(_svg('<path d="M0,0L1,1L2,2 M5,5L6,6 M9,9L8,8"/>'))
    assert svg_utils.parse_layers(p)["subpath_count"] == 3


def test_counts_relative_movetos_too(tmp_path):
    p = tmp_path / "b.svg"
    p.write_text(_svg('<path d="M0,0L1,1 m2,2l1,1 m3,3l1,1"/>'))
    assert svg_utils.parse_layers(p)["subpath_count"] == 3


def test_counts_primitives_as_one_stroke_each(tmp_path):
    p = tmp_path / "c.svg"
    p.write_text(_svg('<polyline points="0,0 1,1 2,2"/><line x1="0" y1="0" x2="9" y2="9"/>'
                      '<polygon points="0,0 5,0 5,5"/>'))
    assert svg_utils.parse_layers(p)["subpath_count"] == 3


def test_heavy_document_is_counted(heavy_svg):
    """The shared dense fixture: 3,600 polylines across four layers."""
    assert svg_utils.parse_layers(heavy_svg)["subpath_count"] == 3600


# The measured bound -------------------------------------------------------

def test_watchdog_kills_a_runaway_preview_and_raises(tmp_path, monkeypatch):
    """The whole point: a preview that outgrows the board is killed on its own
    rather than taking the machine with it, and says so in a way no caller can
    mistake for an ordinary empty estimate.

    Uses a stand-in for the real runner so the test costs a second rather than
    the ninety it takes to grow a real preview past a gigabyte.
    """
    hog = tmp_path / "hog.py"
    hog.write_text(textwrap.dedent("""
        import time
        blocks = []
        while True:                      # grow until something stops us
            blocks.append(bytearray(8 * 1024 * 1024))
            time.sleep(0.02)
    """))
    monkeypatch.setattr(plot_worker, "PREVIEW_RSS_LIMIT_MB", 60)
    monkeypatch.setattr(plot_worker, "_PREVIEW_RUNNER", hog)

    job = {"job_id": "t", "paper_width_mm": 210.0, "paper_height_mm": 297.0,
           "speed_pendown": 15, "speed_penup": 75, "acceleration": 20}
    with pytest.raises(plot_worker.DrawingTooComplex) as caught:
        plot_worker._run_preview(tmp_path / "whatever.svg", job)
    assert caught.value.peak_mb >= 60


def test_too_complex_is_not_an_ordinary_preview_failure():
    """A caller that treats it as 'no estimate' would carry on to the plotter,
    where _run_stage runs pyaxidraw in-process and an OOM takes the server
    down mid-stroke. It must be an exception, not a falsy return."""
    assert issubclass(plot_worker.DrawingTooComplex, Exception)
    exc = plot_worker.DrawingTooComplex(1234.0)
    assert "1234" in str(exc) and exc.peak_mb == 1234.0


def test_rss_reader_survives_a_process_that_has_gone():
    """The watcher races the subprocess exiting; a dead pid is normal."""
    assert plot_worker._proc_rss_mb(999_999) == 0.0
    assert plot_worker._proc_rss_mb(  # our own pid has a real, positive RSS
        __import__("os").getpid()) > 0


# Not retrying what already proved fatal -----------------------------------

def test_bootstrap_skips_a_plan_that_already_failed(monkeypatch):
    """The reboot loop: the service restarts on failure and used to re-enqueue
    the same job, so one drawing produced a crash that survived reboots."""
    from app import plan_queue

    jobs = [
        {"job_id": "fatal", "status": "queued", "plan_status": "too_complex"},
        {"job_id": "broke", "status": "queued", "plan_status": "failed"},
        {"job_id": "fine", "status": "queued", "plan_status": "pending"},
        {"job_id": "done", "status": "queued", "plan_status": "ready",
         "estimated_total_seconds": 42.0},
    ]
    monkeypatch.setattr(plan_queue.state, "snapshot", lambda: {"queue": jobs})
    enqueued = []
    monkeypatch.setattr(plan_queue, "enqueue", lambda job: enqueued.append(job["job_id"]))

    plan_queue.bootstrap_from_state()
    assert enqueued == ["fine"]
