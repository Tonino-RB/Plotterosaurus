"""_run_optical_reg_phase: the plot-worker side of camera registration.

Every hardware/vision call is stubbed — the point of these is the wiring: the
carriage comes back exactly where it started, the result lands in
state.optical_reg, the job returns to awaiting_pen_change, and none of the
three carriage-position values move (only the user's Apply, via nudge_origin,
does that).
"""
import numpy as np
import pytest

from app import config, main, plot_worker, state

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape='
       '"http://www.inkscape.org/namespaces/inkscape" width="210mm" '
       'height="297mm" viewBox="0 0 210 297">'
       '<g inkscape:groupmode="layer" inkscape:label="a">'
       '<path d="M20,20 L80,80" fill="none" stroke="#000"/></g>'
       '<g inkscape:groupmode="layer" inkscape:label="b">'
       '<path d="M30,120 L90,180" fill="none" stroke="#000"/></g></svg>')

JOB = {
    "svg_id": "or", "filename": "or.svg",
    "layer_selections": [{"index": 0, "label": "a", "selected": True},
                         {"index": 1, "label": "b", "selected": True}],
    "paper_width_mm": 210.0, "paper_height_mm": 297.0,
    "margin_top_mm": 0.0, "margin_right_mm": 0.0,
    "margin_bottom_mm": 0.0, "margin_left_mm": 0.0,
    "fit_content": False, "transform_scale": 1.0, "transform_rotation_deg": 0.0,
    "transform_offset_x_mm": 0.0, "transform_offset_y_mm": 0.0,
    "speed_pendown": 25, "speed_penup": 75, "acceleration": 75,
    "pen_pos_up": 60, "pen_pos_down": 30, "optimize_svg": False,
}


def _reset_origin():
    state.set_origin_nudge(0.0, 0.0)
    state.set_manual_origin_offset(0.0, 0.0)
    state.set_origin_base(0.0, 0.0)


@pytest.fixture
def paused(monkeypatch):
    """A job parked at a pen-change pause, with the carriage jog + probe-cross
    plot + camera stubbed. Yields (job_id, jog_log)."""
    (main.UPLOAD_DIR / "or.svg").write_text(SVG)
    jog = []
    monkeypatch.setattr(plot_worker, "_jog_carriage",
                        lambda dx, dy: jog.append((dx, dy)))
    monkeypatch.setattr(plot_worker, "_plot_cross",
                        lambda x, y, size: None)
    monkeypatch.setattr(plot_worker.camera, "write_optical_reg_preview",
                        lambda rgb: None)
    monkeypatch.setattr(config, "OPTICAL_REG_MM_PER_PX", 0.05)
    monkeypatch.setattr(config, "OPTICAL_REG_CAM_ROTATION_DEG", 0.0)
    monkeypatch.setattr(config, "OPTICAL_REG_CAM_OFFSET_X_MM", 0.0)
    monkeypatch.setattr(config, "OPTICAL_REG_CAM_OFFSET_Y_MM", 0.0)
    monkeypatch.setattr(config, "OPTICAL_REG_MARK_X_MM", 10.0)
    monkeypatch.setattr(config, "OPTICAL_REG_MARK_Y_MM", 10.0)
    monkeypatch.setattr(config, "OPTICAL_REG_PROBE_OFFSET_MM", 2.0)
    monkeypatch.setattr(config, "OPTICAL_REG_PROBE_OFFSET_MAX_MM", 8.0)
    monkeypatch.setattr(config, "OPTICAL_REG_MAX_CORRECTION_MM", 3.0)

    job = state.add_job(dict(JOB))
    state.update_job(job["job_id"], status="plotting")
    state.update_job(job["job_id"], status="awaiting_pen_change")
    state.set_active(job["job_id"])
    _reset_origin()
    state.set_optical_reg("idle")
    yield job["job_id"], jog
    _reset_origin()
    state.set_optical_reg("idle")
    state.set_active(None)
    state.remove_job(job["job_id"])


def _grab(monkeypatch, arr):
    monkeypatch.setattr(plot_worker.camera, "grab_median_gray", lambda n=3: arr)


def _measure(monkeypatch, result):
    from app import optical_reg
    monkeypatch.setattr(optical_reg, "measure", lambda img, exp: result)


def test_a_reading_lands_in_state_and_leaves_the_carriage_where_it_was(paused, monkeypatch):
    job_id, jog = paused
    _grab(monkeypatch, np.full((60, 80), 255, np.uint8))
    # probe O = (2,2) mm; want measured misalignment δ = (+0.3, -0.2) mm, so
    # sep = O + δ = (2.3, 1.8) mm -> /mm_per_px -> (46, 36) px.
    _measure(monkeypatch, {"sep_px": (46.0, 36.0), "separable": True,
                           "confidence": 0.83})

    plot_worker._run_optical_reg_phase(job_id, 2.0)

    reg = state.optical_reg()
    assert reg["status"] == "measured"
    assert reg["dx_mm"] == pytest.approx(-0.3, abs=1e-6)
    assert reg["dy_mm"] == pytest.approx(0.2, abs=1e-6)
    assert reg["probe_mm"] == 2.0
    assert state.get_job(job_id)["status"] == "awaiting_pen_change"
    assert sum(dx for dx, _ in jog) == pytest.approx(0.0)
    assert sum(dy for _, dy in jog) == pytest.approx(0.0)
    assert jog and jog[0] == (-jog[1][0], -jog[1][1])  # jog out, then exact walk-back


def test_the_measurement_never_moves_the_origin_values(paused, monkeypatch):
    job_id, _ = paused
    _grab(monkeypatch, np.full((60, 80), 255, np.uint8))
    _measure(monkeypatch, {"sep_px": (60.0, 40.0), "separable": True,
                           "confidence": 0.7})
    plot_worker._run_optical_reg_phase(job_id, 2.0)
    assert state.origin_base() == (0.0, 0.0)
    assert state.manual_origin_offset() == (0.0, 0.0)
    assert state.origin_nudge() == (0.0, 0.0)


def test_the_proposed_nudge_is_clamped_to_the_max_correction(paused, monkeypatch):
    job_id, _ = paused
    _grab(monkeypatch, np.full((60, 80), 255, np.uint8))
    # sep implies δx = +10 mm; clamp is 3 mm, so the proposal is -3, not -10.
    _measure(monkeypatch, {"sep_px": ((2.0 + 10.0) / 0.05, 2.0 / 0.05),
                           "separable": True, "confidence": 0.6})
    plot_worker._run_optical_reg_phase(job_id, 2.0)
    reg = state.optical_reg()
    assert reg["dx_mm"] == pytest.approx(-3.0)
    assert reg["dy_mm"] == pytest.approx(0.0)


def test_unreadable_frame_fails_cleanly(paused, monkeypatch):
    job_id, jog = paused
    monkeypatch.setattr(plot_worker.camera, "grab_median_gray", lambda n=3: None)
    plot_worker._run_optical_reg_phase(job_id, 2.0)
    reg = state.optical_reg()
    assert reg["status"] == "failed"
    assert reg["reason"]
    assert state.get_job(job_id)["status"] == "awaiting_pen_change"
    assert sum(dx for dx, _ in jog) == pytest.approx(0.0)


def test_overlapping_crosses_widen_the_probe_then_give_up(paused, monkeypatch):
    job_id, _ = paused
    _grab(monkeypatch, np.full((60, 80), 255, np.uint8))
    probes_tried = []
    from app import optical_reg

    def fake_measure(img, exp):
        # exp is the pixel offset for the current probe; record its mm size.
        probes_tried.append(round(exp[0] * 0.05, 3))
        return {"sep_px": None, "separable": False, "confidence": 0.0}

    monkeypatch.setattr(optical_reg, "measure", fake_measure)
    plot_worker._run_optical_reg_phase(job_id, 2.0)

    assert probes_tried == [2.0, 4.0, 8.0]
    reg = state.optical_reg()
    assert reg["status"] == "failed"
    assert "overlap" in reg["reason"]


def test_measure_route_is_rejected_outside_a_pause(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_ENABLED", True)
    monkeypatch.setattr(config, "OPTICAL_REG_MM_PER_PX", 0.05)
    state.set_active(None)
    with pytest.raises(RuntimeError, match="pen-change pause"):
        plot_worker.trigger_optical_reg(None)


def test_measure_route_is_rejected_when_uncalibrated(paused, monkeypatch):
    monkeypatch.setattr(config, "CAMERA_ENABLED", True)
    monkeypatch.setattr(config, "OPTICAL_REG_MM_PER_PX", 0.0)
    with pytest.raises(RuntimeError, match="not calibrated"):
        plot_worker.trigger_optical_reg(None)


def test_measuring_never_touches_the_mediamtx_control_api(paused, monkeypatch):
    """A registration measurement must not restart the camera pipeline —
    grabbing frames is a passive RTSP pull, nothing more."""
    job_id, _ = paused
    _grab(monkeypatch, np.full((60, 80), 255, np.uint8))
    _measure(monkeypatch, {"sep_px": (44.0, 40.0), "separable": True,
                           "confidence": 0.8})

    def boom(*a, **k):
        raise AssertionError("optical-reg measurement hit the MediaMTX Control API")

    monkeypatch.setattr(plot_worker.camera, "_api_patch", boom)
    plot_worker._run_optical_reg_phase(job_id, 2.0)
    assert state.optical_reg()["status"] == "measured"
