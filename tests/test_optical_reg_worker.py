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
    crosses = []
    monkeypatch.setattr(plot_worker, "_plot_cross",
                        lambda x, y, size: crosses.append((x, y)))
    monkeypatch.setattr(plot_worker.camera, "write_optical_reg_preview",
                        lambda rgb: None)
    monkeypatch.setattr(config, "OPTICAL_REG_MM_PER_PX", 0.05)
    monkeypatch.setattr(config, "OPTICAL_REG_CAM_ROTATION_DEG", 0.0)
    monkeypatch.setattr(config, "OPTICAL_REG_CAM_OFFSET_X_MM", 0.0)
    monkeypatch.setattr(config, "OPTICAL_REG_CAM_OFFSET_Y_MM", 0.0)
    monkeypatch.setattr(config, "OPTICAL_REG_MARK_X_MM", 10.0)
    monkeypatch.setattr(config, "OPTICAL_REG_MARK_Y_MM", 10.0)
    # Pinned so the geometry these tests reason about doesn't depend on
    # whatever resolution the developer's own config.json happens to hold.
    monkeypatch.setattr(config, "CAMERA_RESOLUTION_WIDTH", 1920)
    monkeypatch.setattr(config, "CAMERA_RESOLUTION_HEIGHT", 1080)
    monkeypatch.setattr(config, "OPTICAL_REG_MARK_SIZE_MM", 3.0)
    monkeypatch.setattr(config, "OPTICAL_REG_PROBE_OFFSET_MM", 6.0)
    monkeypatch.setattr(config, "OPTICAL_REG_PROBE_OFFSET_MAX_MM", 24.0)
    monkeypatch.setattr(config, "OPTICAL_REG_MAX_CORRECTION_MM", 3.0)
    monkeypatch.setattr(plot_worker, "_optical_reg_probe_index", 0)
    monkeypatch.setattr(plot_worker, "_optical_reg_ref_drawn", True)

    job = state.add_job(dict(JOB))
    state.update_job(job["job_id"], status="plotting")
    state.update_job(job["job_id"], status="awaiting_pen_change")
    state.set_active(job["job_id"])
    _reset_origin()
    state.set_optical_reg("idle")
    yield job["job_id"], jog, crosses
    _reset_origin()
    state.set_optical_reg("idle")
    state.set_active(None)
    state.remove_job(job["job_id"])


def _grab(monkeypatch, arr):
    monkeypatch.setattr(plot_worker.camera, "grab_median_gray", lambda n=3: arr)


def _measure(monkeypatch, result):
    from app import optical_reg
    monkeypatch.setattr(optical_reg, "measure",
                        lambda img, exp, **kw: result)


def test_a_reading_lands_in_state_and_leaves_the_carriage_where_it_was(paused, monkeypatch):
    job_id, jog, _ = paused
    _grab(monkeypatch, np.full((60, 80), 255, np.uint8))
    # probe O = (6,6) mm; want measured misalignment δ = (+0.3, -0.2) mm, so
    # sep = O + δ = (6.3, 5.8) mm -> /mm_per_px -> (126, 116) px.
    _measure(monkeypatch, {"sep_px": (126.0, 116.0), "separable": True,
                           "confidence": 0.83})

    plot_worker._run_optical_reg_phase(job_id, 6.0)

    reg = state.optical_reg()
    assert reg["status"] == "measured"
    assert reg["dx_mm"] == pytest.approx(-0.3, abs=1e-6)
    assert reg["dy_mm"] == pytest.approx(0.2, abs=1e-6)
    assert reg["probe_mm"] == 6.0
    assert state.get_job(job_id)["status"] == "awaiting_pen_change"
    assert sum(dx for dx, _ in jog) == pytest.approx(0.0)
    assert sum(dy for _, dy in jog) == pytest.approx(0.0)
    assert jog and jog[0] == (-jog[1][0], -jog[1][1])  # jog out, then exact walk-back


def test_the_measurement_never_moves_the_origin_values(paused, monkeypatch):
    job_id, _, _ = paused
    _grab(monkeypatch, np.full((60, 80), 255, np.uint8))
    _measure(monkeypatch, {"sep_px": (126.0, 116.0), "separable": True,
                           "confidence": 0.7})
    plot_worker._run_optical_reg_phase(job_id, 6.0)
    assert state.origin_base() == (0.0, 0.0)
    assert state.manual_origin_offset() == (0.0, 0.0)
    assert state.origin_nudge() == (0.0, 0.0)


def test_a_reading_past_the_max_correction_is_refused_not_clamped(paused, monkeypatch):
    """Pinning an over-range reading to the limit would turn "this measurement
    is wrong" into a plausible few millimetres the user is one click from
    applying — so it is published as a failure instead."""
    job_id, _, _ = paused
    _grab(monkeypatch, np.full((60, 80), 255, np.uint8))
    # sep implies δx = +10 mm against a 3 mm limit.
    _measure(monkeypatch, {"sep_px": ((6.0 + 10.0) / 0.05, 6.0 / 0.05),
                           "separable": True, "confidence": 0.6})
    plot_worker._run_optical_reg_phase(job_id, 6.0)
    reg = state.optical_reg()
    assert reg["status"] == "failed"
    assert "limit" in reg["reason"]
    assert reg["dx_mm"] == 0.0 and reg["dy_mm"] == 0.0


def test_unreadable_frame_fails_cleanly(paused, monkeypatch):
    job_id, jog, _ = paused
    monkeypatch.setattr(plot_worker.camera, "grab_median_gray", lambda n=3: None)
    plot_worker._run_optical_reg_phase(job_id, 2.0)
    reg = state.optical_reg()
    assert reg["status"] == "failed"
    assert reg["reason"]
    assert state.get_job(job_id)["status"] == "awaiting_pen_change"
    assert sum(dx for dx, _ in jog) == pytest.approx(0.0)


def test_overlapping_crosses_widen_the_probe_then_give_up(paused, monkeypatch):
    job_id, _, crosses = paused
    _grab(monkeypatch, np.full((60, 80), 255, np.uint8))
    _measure(monkeypatch, {"sep_px": None, "separable": False,
                           "confidence": 0.0})
    plot_worker._run_optical_reg_phase(job_id, 6.0)

    # 6 -> 12 -> 24 mm, then 48 is past the 24 mm ceiling.
    assert state.optical_reg()["probe_mm"] == 24.0
    assert len(crosses) == 3
    reg = state.optical_reg()
    assert reg["status"] == "failed"
    assert "overlap" in reg["reason"]


def test_every_probe_cross_of_a_run_lands_on_its_own_spot(paused, monkeypatch):
    """Each probe stays on the paper for the rest of the run. Two landing on the
    same spot means the second pen change draws over the first probe and
    measures a blend of two pens — and a widen retry measures against the blob
    it just made."""
    job_id, _, crosses = paused
    _grab(monkeypatch, np.full((60, 80), 255, np.uint8))
    _measure(monkeypatch, {"sep_px": (126.0, 116.0), "separable": True,
                           "confidence": 0.8})

    for _ in range(3):
        plot_worker._run_optical_reg_phase(job_id, 6.0)

    assert len(crosses) == 3
    assert len(set(crosses)) == 3
    # Far enough apart that no pair of them can pass for the measured pair.
    gaps = [abs(b[0] - a[0]) for a, b in zip(crosses, crosses[1:])]
    assert min(gaps) > 1.6 * config.OPTICAL_REG_MAX_CORRECTION_MM


def test_a_pattern_too_wide_for_the_frame_is_refused(paused, monkeypatch):
    """The lanes walk the probe further out with every measurement, so sooner or
    later the pair stops fitting one frame. Say so rather than grabbing a frame
    with half the pattern outside it."""
    job_id, _, _ = paused
    monkeypatch.setattr(config, "CAMERA_RESOLUTION_WIDTH", 160)
    monkeypatch.setattr(config, "CAMERA_RESOLUTION_HEIGHT", 120)  # 6 mm of view
    _grab(monkeypatch, np.full((60, 80), 255, np.uint8))
    _measure(monkeypatch, {"sep_px": (126.0, 116.0), "separable": True,
                           "confidence": 0.8})

    plot_worker._run_optical_reg_phase(job_id, 6.0)
    reg = state.optical_reg()
    assert reg["status"] == "failed"
    assert "camera" in reg["reason"]


def test_the_probe_offset_is_floored_past_the_configured_value(paused, monkeypatch):
    """A probe offset under the mark size draws the probe on top of the
    reference; one under the identification tolerance makes two probes look like
    the pair. The shipped default used to sit below both."""
    job_id, _, crosses = paused
    monkeypatch.setattr(config, "OPTICAL_REG_PROBE_OFFSET_MM", 0.5)
    _grab(monkeypatch, np.full((60, 80), 255, np.uint8))
    _measure(monkeypatch, {"sep_px": None, "separable": False,
                           "confidence": 0.0})

    plot_worker._run_optical_reg_phase(job_id, 0.5)

    mx, my = config.OPTICAL_REG_MARK_X_MM, config.OPTICAL_REG_MARK_Y_MM
    assert crosses[0][0] - mx >= 1.25 * config.OPTICAL_REG_MARK_SIZE_MM
    assert crosses[0][1] - my >= 1.25 * config.OPTICAL_REG_MARK_SIZE_MM


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


def test_measure_route_is_rejected_without_a_reference_mark(paused, monkeypatch):
    """The reference cross is drawn once, at the end of stage 0. With none on
    the paper — the job never opted in, the run resumed past that stage, or the
    cross failed to plot — measuring would draw probe crosses on the artwork and
    report the gap between two of *those* as the misalignment. The UI hides the
    button; the API has to refuse it too."""
    job_id, _, _ = paused
    monkeypatch.setattr(config, "CAMERA_ENABLED", True)

    state.update_job(job_id, optical_reg=False)
    with pytest.raises(RuntimeError, match="reference mark"):
        plot_worker.trigger_optical_reg(None)

    state.update_job(job_id, optical_reg=True)
    monkeypatch.setattr(plot_worker, "_optical_reg_ref_drawn", False)
    with pytest.raises(RuntimeError, match="reference mark"):
        plot_worker.trigger_optical_reg(None)


def test_a_latched_measure_request_does_not_survive_into_the_next_run(monkeypatch):
    """Measure sets _optical_reg_event alongside _continue_event. If a cancel
    lands before the pause loop consumes it, both stay set — and the next job's
    plain Continue would silently run a measurement nobody asked for."""
    monkeypatch.setattr(plot_worker, "_run_loop", lambda: None)
    plot_worker._optical_reg_event.set()
    plot_worker._calibrate_event.set()
    try:
        plot_worker.start_plot()
        assert not plot_worker._optical_reg_event.is_set()
        assert not plot_worker._calibrate_event.is_set()
        assert plot_worker._optical_reg_probe_mm is None
        assert plot_worker._optical_reg_probe_index == 0
    finally:
        plot_worker._clear_side_action_state()


def test_changing_the_camera_resolution_invalidates_the_calibration(monkeypatch):
    """mm_per_px is millimetres per *pixel* — meaningless at another resolution,
    but still reported as 'Calibrated' and still used.

    config.update persists the whole of config.json, so this checks what
    patch_settings *decides* rather than letting it write: nothing under tests/
    may touch the running server's real settings file (see tests/README.md).
    """
    monkeypatch.setattr(config, "CAMERA_RESOLUTION_WIDTH", 1920)
    monkeypatch.setattr(config, "CAMERA_RESOLUTION_HEIGHT", 1080)
    monkeypatch.setattr(config, "OPTICAL_REG_MM_PER_PX", 0.05)
    monkeypatch.setattr(main.camera, "apply_camera_settings", lambda: None)
    monkeypatch.setattr(main, "_settings_payload", lambda: {})
    written = []
    monkeypatch.setattr(config, "update", lambda **kw: written.append(kw))

    main.patch_settings(main.SettingsUpdate(camera_resolution_width=1920))
    assert "optical_reg_mm_per_px" not in written[-1]  # unchanged -> kept

    main.patch_settings(main.SettingsUpdate(camera_resolution_height=720))
    assert written[-1]["optical_reg_mm_per_px"] == 0.0

    # An explicit scale in the same request is the caller's own value, not a
    # leftover to invalidate.
    main.patch_settings(main.SettingsUpdate(camera_resolution_width=1280,
                                            optical_reg_mm_per_px=0.02))
    assert written[-1]["optical_reg_mm_per_px"] == 0.02


def test_measuring_never_touches_the_mediamtx_control_api(paused, monkeypatch):
    """A registration measurement must not restart the camera pipeline —
    grabbing frames is a passive RTSP pull, nothing more."""
    job_id, _, _ = paused
    _grab(monkeypatch, np.full((60, 80), 255, np.uint8))
    _measure(monkeypatch, {"sep_px": (126.0, 116.0), "separable": True,
                           "confidence": 0.8})

    def boom(*a, **k):
        raise AssertionError("optical-reg measurement hit the MediaMTX Control API")

    monkeypatch.setattr(plot_worker.camera, "_api_patch", boom)
    plot_worker._run_optical_reg_phase(job_id, 6.0)
    assert state.optical_reg()["status"] == "measured"


# _plot_cross ------------------------------------------------------------------
#
# The cross has to come out a cross. connect() parks the driver's position
# trackers at (0, 0), which is also its travel-bounds *minimum*, and moveto()
# clips a target outside the bounds while still recording the unclipped one — so
# an unseeded _plot_cross silently drops every arm reaching left of or above the
# carriage and draws an "L", with the driver (and this function's own return to
# origin) believing otherwise. Calibration ran on exactly that.

class _Pos:
    xpos = 0.0
    ypos = 0.0


@pytest.fixture
def cross_driver(monkeypatch):
    """The AxiDraw driver stubbed to record every absolute target _plot_cross
    commands, in driver space. Yields (targets, bounds)."""
    targets = []
    bounds = [[0.0, 0.0], [400.0, 300.0]]

    class FakeAd:
        def __init__(self):
            self.options = type("o", (), {})()
            self.params = type("p", (), {})()
            self.pen = type("pen", (), {})()
            self.pen.turtle = _Pos()
            self.pen.phys = _Pos()
            self.bounds = bounds

        def interactive(self): pass
        def connect(self): return True
        def penup(self): pass
        def moveto(self, x, y): targets.append((x, y))
        def lineto(self, x, y): targets.append((x, y))
        def disconnect(self): pass

    monkeypatch.setattr(plot_worker.axidraw, "AxiDraw", FakeAd)
    # Pinned, and deliberately skewed: the marks have to land in the same frame
    # the artwork plots in, so every target carries the machine's axis-skew
    # correction like _jog_carriage's do.
    monkeypatch.setattr(config, "MACHINES", [{
        "id": "cross-test", "name": "Cross", "width_mm": 400.0, "height_mm": 300.0,
        "auto_rotate": "off", "skew_deg": 5.0, "skew_true_axis": "x",
        "skew_mode": "clip",
    }])
    monkeypatch.setattr(config, "ACTIVE_MACHINE_ID", "cross-test")
    yield targets, bounds


def test_a_cross_is_drawn_whole_and_inside_the_travel_bounds(cross_driver):
    from app import optical_reg

    targets, bounds = cross_driver
    plot_worker._plot_cross(0.0, 0.0, 3.0)

    (b0x, b0y), (b1x, b1y) = bounds
    assert targets, "nothing was commanded"
    for x, y in targets:
        assert b0x <= x <= b1x and b0y <= y <= b1y, (
            f"({x}, {y}) is outside the driver's bounds — the driver would clip "
            f"it and record the unclipped target anyway")

    # All four arms, in the shape cross_arms defines, skew-corrected, and back
    # to the origin the carriage started on.
    from app import axis_skew

    seed = targets[-1]   # the return-to-origin move
    drawn = {(round(x - seed[0], 6), round(y - seed[1], 6)) for x, y in targets}
    assert (0.0, 0.0) in drawn
    for arm in optical_reg.cross_arms(0.0, 0.0, 3.0):
        for px, py in arm:
            want = axis_skew.skew_delta(px, py, 5.0, "x")
            assert (round(want[0], 6), round(want[1], 6)) in drawn


def test_a_cross_too_big_for_the_bed_is_refused(cross_driver):
    _, bounds = cross_driver
    bounds[1] = [2.0, 2.0]
    with pytest.raises(RuntimeError, match="does not fit the bed"):
        plot_worker._plot_cross(0.0, 0.0, 20.0)
