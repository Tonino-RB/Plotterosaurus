"""What the time and distance estimate is allowed to measure.

The driver clips pen-down moves to its travel bounds, and clipped moves are
simply absent from the totals it reports. Handing it the machine's real bed
therefore made the estimate describe the clip instead of the drawing: a
1313x928mm document on a 297x420mm bed came back as 0 metres, 0 seconds, 0 pen
lifts, for 152 metres of ink and nearly three hours of plotting.

Zero for a drawing full of line reads as a broken app, and it buried the fact
worth surfacing. The estimate now measures the artwork; the card's
machine-bounds warning is what says it will not all fit.
"""
import json

import pytest

from app import config, plot_worker

BED_MM = (297.0, 420.0)


@pytest.fixture
def captured_options(monkeypatch):
    """Run _run_preview far enough to see the options it hands the simulation,
    without paying for a real pyaxidraw run."""
    seen = {}

    class FakeProc:
        returncode = 0

        def communicate(self):
            return json.dumps({"estimated_total_seconds": 1.0,
                               "distance_pendown_m": 1.0,
                               "distance_total_m": 1.0, "pen_lifts": 0}), ""

        def poll(self):
            return 0

    def fake_popen(args, **kwargs):
        # [python, runner.py, preview.svg, options-json]
        seen["options"] = json.loads(args[3])
        return FakeProc()

    monkeypatch.setattr(plot_worker.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(plot_worker, "machine_bounds_mm", lambda: BED_MM)
    return seen


def _job(paper_w, paper_h):
    return {"job_id": "j", "svg_id": "s", "paper_width_mm": paper_w,
            "paper_height_mm": paper_h, "speed_pendown": 25, "speed_penup": 75,
            "acceleration": 75}


def test_a_page_that_fits_is_simulated_against_the_real_bed(captured_options):
    """The normal case must not move. Widening the envelope is only for
    drawings that were going to be clipped anyway."""
    plot_worker._run_preview(plot_worker.Path("x.svg"), _job(210.0, 297.0))
    travel_mm = [v * 25.4 for v in captured_options["options"]["travel_in"]]
    assert travel_mm == pytest.approx(list(BED_MM))


def test_an_oversized_page_is_still_measured_in_full(captured_options):
    """PrintSVG_hatched, the drawing that reported zero. The envelope has to
    hold the page or the totals describe the corner that fits."""
    plot_worker._run_preview(plot_worker.Path("x.svg"), _job(1312.6, 928.2))
    travel_mm = [v * 25.4 for v in captured_options["options"]["travel_in"]]
    assert travel_mm == pytest.approx([1312.6, 928.2])


def test_the_simulation_still_mirrors_the_plots_own_settings(captured_options):
    """Widening the travel bounds must not disturb anything else: an estimate
    simulated at different speeds, or a different orientation, measures a
    different plot."""
    plot_worker._run_preview(plot_worker.Path("x.svg"), _job(210.0, 297.0))
    options = captured_options["options"]
    assert options["speed_pendown"] == 25
    assert options["speed_penup"] == 75
    assert options["acceleration"] == 75
    assert options["model"] == config.PLOTTER_MODEL
    assert len(options["travel_params"]) == 2
