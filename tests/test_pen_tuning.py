"""Corner-speed and pen-timing knobs wired through the plot path.

Two ink defects motivate these:
  - a dark blob wherever a path turns sharply (the pen decelerates into the
    corner and sits there while ink wicks) -> `cornering`
  - a starting dot when the pen is planted a beat before it starts moving ->
    `pen_delay_down` / `pen_rate_lower`

pyaxidraw exposes them as options/params that Plotterosaurus previously left at
library defaults. These tests pin how a job's values reach the driver, that an
untouched job still plots exactly as before, and that a mid-plot change lands.
"""
import json

import pytest

from app import config, main, plot_worker, state


class _Bag:
    pass


class _FakeAd:
    """Records what _run_stage / _apply_pending_live_settings assign."""

    def __init__(self):
        self.options = _Bag()
        self.params = _Bag()
        self.pen = _Bag()
        self.plot_status = _Bag()
        self.plot_status.stopped = 0
        self.calls = []
        self.pen.servo_init = lambda ad: self.calls.append("servo_init")

    # _run_stage surface
    def plot_setup(self, path):
        self.setup_path = path

    def plot_run(self, output=False):
        return "out.svg"

    def disconnect(self):
        pass

    # _apply_pending_live_settings surface
    def enable_motors(self):
        self.calls.append("enable_motors")


# --- _accel_rate_pu -------------------------------------------------------

def test_accel_rate_pu_is_independent_of_pen_down_value():
    # Effective pen-up acceleration is accel_rate_pu * accel / 100; the helper
    # is built so that only acceleration_penup moves it.
    def effective(accel, penup):
        return plot_worker._accel_rate_pu(accel, penup) * accel / 100.0

    assert effective(75, 40) == pytest.approx(effective(20, 40))
    assert effective(75, 40) == pytest.approx(60.0 * 40 / 100.0)


def test_accel_rate_pu_matches_stock_when_penup_equals_accel():
    # The backward-compat guarantee: a job that only ever set `acceleration`
    # gets acceleration_penup == acceleration, and accel_rate_pu stays at
    # pyaxidraw's own default of 60.0.
    for accel in (10, 25, 75, 100):
        assert plot_worker._accel_rate_pu(accel, accel) == pytest.approx(60.0)


# --- _run_stage mapping -------------------------------------------------------

@pytest.fixture
def run_stage_ad(monkeypatch):
    """Run _run_stage against a recording fake, hardware and threads stubbed."""
    made = []

    def _factory():
        ad = _FakeAd()
        made.append(ad)
        return ad

    monkeypatch.setattr(plot_worker, "_LiveAdjustAxiDraw", _factory)
    monkeypatch.setattr(plot_worker, "_apply_plot_bounds", lambda ad: None)
    monkeypatch.setattr(plot_worker, "_start_position_poll", lambda: None)
    monkeypatch.setattr(plot_worker, "_stop_position_poll", lambda: None)
    monkeypatch.setattr(plot_worker.config, "active_machine",
                        lambda: {"skew_deg": 0.0, "skew_true_axis": "x"})
    return made


def _job(**over):
    base = {
        "job_id": "j", "speed_pendown": 25, "speed_penup": 75,
        "acceleration": 75, "acceleration_penup": 40, "cornering": 35,
        "pen_pos_up": 60, "pen_pos_down": 30, "pen_rate_lower": 70,
        "pen_rate_raise": 65, "pen_delay_down": -120, "pen_delay_up": 30,
        "paper_width_mm": 210.0, "paper_height_mm": 297.0,
    }
    base.update(over)
    return base


def test_run_stage_pushes_every_knob_to_the_driver(run_stage_ad):
    plot_worker._run_stage(plot_worker.Path("x.svg"), "plot", _job())
    ad = run_stage_ad[0]
    assert ad.params.cornering == 35
    assert ad.options.pen_rate_lower == 70
    assert ad.options.pen_rate_raise == 65
    assert ad.options.pen_delay_down == -120
    assert ad.options.pen_delay_up == 30
    assert ad.params.accel_rate_pu == pytest.approx(60.0 * 40 / 75)


def test_run_stage_falls_back_for_a_pre_feature_job(run_stage_ad, monkeypatch):
    # A job record created before these fields existed carries none of them.
    monkeypatch.setattr(config, "CORNERING_DEFAULT", 10)
    old = {"job_id": "j", "speed_pendown": 25, "speed_penup": 75,
           "acceleration": 30, "paper_width_mm": 210.0, "paper_height_mm": 297.0}
    plot_worker._run_stage(plot_worker.Path("x.svg"), "plot", old)
    ad = run_stage_ad[0]
    assert ad.params.cornering == 10                       # stock
    assert ad.options.pen_delay_down == 0                  # stock
    # acceleration_penup falls back to the job's own acceleration -> stock 60.0
    assert ad.params.accel_rate_pu == pytest.approx(60.0)


# --- live mid-plot adjust ---------------------------------------------------

def test_apply_pending_live_settings_maps_each_slot():
    ad = _FakeAd()
    ad.options.accel = 75
    plot_worker._pending_live_settings = {
        "cornering": 50, "pen_delay_down": -200, "pen_delay_up": 15,
        "pen_rate_lower": 80, "pen_rate_raise": 40, "acceleration_penup": 30,
    }
    try:
        plot_worker._apply_pending_live_settings(ad)
    finally:
        plot_worker._pending_live_settings = None
    assert ad.params.cornering == 50
    assert ad.options.pen_delay_down == -200
    assert ad.options.pen_delay_up == 15
    assert ad.options.pen_rate_lower == 80
    assert ad.options.pen_rate_raise == 40
    # a pen_rate change has to be pushed to the servo
    assert "servo_init" in ad.calls
    assert ad.params.accel_rate_pu == pytest.approx(60.0 * 30 / 75)


def test_set_live_plot_settings_accepts_and_persists_new_knobs(monkeypatch):
    job = state.add_job({
        "svg_id": "s", "filename": "j.svg",
        "layer_selections": [{"index": 0, "label": "a", "selected": True}],
        "paper_width_mm": 210.0, "paper_height_mm": 297.0,
        "speed_pendown": 25, "speed_penup": 75, "acceleration": 75,
    })
    state.update_job(job["job_id"], status="plotting")
    state.set_active(job["job_id"])
    monkeypatch.setattr(plot_worker, "_current_ad", object())
    monkeypatch.setattr(plot_worker, "_schedule_live_estimate_recompute",
                        lambda job_id: None)
    plot_worker._pending_live_settings = None
    try:
        plot_worker.set_live_plot_settings(cornering=45, pen_delay_down=-150,
                                           acceleration_penup=20)
        assert plot_worker._pending_live_settings == {
            "cornering": 45, "pen_delay_down": -150, "acceleration_penup": 20}
        rec = state.get_job(job["job_id"])
        assert rec["cornering"] == 45
        assert rec["pen_delay_down"] == -150
        assert rec["acceleration_penup"] == 20
    finally:
        plot_worker._pending_live_settings = None
        state.set_active(None)
        state.remove_job(job["job_id"])


# --- estimate parity ------------------------------------------------------

def test_preview_options_mirror_the_new_knobs(monkeypatch):
    seen = {}

    class FakeProc:
        returncode = 0

        def communicate(self):
            return json.dumps({"estimated_total_seconds": 1.0,
                               "distance_pendown_m": 1.0,
                               "distance_total_m": 1.0, "pen_lifts": 0}), ""

        def poll(self):
            return 0

    def fake_popen(args, **kw):
        seen["o"] = json.loads(args[3])   # [python, runner.py, preview.svg, options-json]
        return FakeProc()

    monkeypatch.setattr(plot_worker.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(plot_worker, "machine_bounds_mm", lambda: (297.0, 420.0))
    plot_worker._run_preview(plot_worker.Path("x.svg"),
                             _job(acceleration=75, acceleration_penup=40))
    o = seen["o"]
    assert o["cornering"] == 35
    assert o["pen_rate_lower"] == 70
    assert o["pen_delay_down"] == -120
    assert o["accel_rate_pu"] == pytest.approx(60.0 * 40 / 75)


# --- clamp / null-drop --------------------------------------------------------

def test_out_of_range_new_fields_are_clamped_not_rejected():
    d = {"cornering": 999, "pen_rate_lower": 0, "pen_delay_down": -9000,
         "pen_delay_up": 9000, "acceleration_penup": 250}
    main._clamp_job_fields(d)
    assert d == {"cornering": 100, "pen_rate_lower": 1, "pen_delay_down": -500,
                 "pen_delay_up": 500, "acceleration_penup": 100}


def test_an_emptied_number_box_drops_rather_than_nulls_the_field():
    d = {"cornering": None, "pen_delay_down": None, "acceleration_penup": None}
    main._clamp_job_fields(d)
    assert d == {}
