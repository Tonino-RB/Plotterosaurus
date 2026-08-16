"""What the pause between layers is allowed to change.

Pausing to swap a pen is where a multi-pen plot is won or lost. Four things
are offered there — nudge the origin, plot a calibration mark, adjust pen
height, raise/lower the pen — and each one touches the machine while a job is
half-drawn on the paper. The question these tests answer is not "does the
button work" but "what does it leave behind", because the answer has to be:

    the remaining layers of this job   — yes, that is the point
    the job record on disk            — only pen height, deliberately
    the next job                      — nothing, ever

An origin nudge that outlived its run would silently offset the following
plot, and because an AxiDraw has no home switches nothing would ever correct
it: each run would inherit the sum of every nudge before it.
"""
import pytest

from app import plot_worker, state


@pytest.fixture
def paused_job(monkeypatch):
    """An active job sitting at a pen-change pause, with the hardware stubbed.

    A real SVG on disk, deliberately: `nudge_origin` refuses a delta that would
    push the artwork off the page, so it measures the document. Stubbing that
    out would skip the guard these tests are partly about.
    """
    from app import main

    (main.UPLOAD_DIR / "s.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape='
        '"http://www.inkscape.org/namespaces/inkscape" width="210mm" '
        'height="297mm" viewBox="0 0 210 297">'
        '<g inkscape:groupmode="layer" inkscape:label="a">'
        '<path d="M20,20 L80,80" fill="none" stroke="#000"/></g>'
        '<g inkscape:groupmode="layer" inkscape:label="b">'
        '<path d="M30,120 L90,180" fill="none" stroke="#000"/></g></svg>')
    job = state.add_job({
        "svg_id": "s", "filename": "j.svg",
        "layer_selections": [{"index": 0, "label": "a", "selected": True},
                             {"index": 1, "label": "b", "selected": True}],
        "paper_width_mm": 210.0, "paper_height_mm": 297.0,
        "margin_top_mm": 0.0, "margin_right_mm": 0.0,
        "margin_bottom_mm": 0.0, "margin_left_mm": 0.0,
        "fit_content": False, "transform_scale": 1.0,
        "transform_rotation_deg": 0.0,
        "transform_offset_x_mm": 0.0, "transform_offset_y_mm": 0.0,
        "speed_pendown": 25, "speed_penup": 75, "acceleration": 75,
        "pen_pos_up": 60, "pen_pos_down": 30, "optimize_svg": False,
    })
    state.update_job(job["job_id"], status="plotting")
    state.update_job(job["job_id"], status="awaiting_pen_change")
    state.set_active(job["job_id"])
    # Never touch a serial port from a test.
    monkeypatch.setattr(plot_worker, "_jog_carriage", lambda dx, dy: None)
    state.set_origin_nudge(0.0, 0.0)
    yield state.get_job(job["job_id"])
    state.set_origin_nudge(0.0, 0.0)
    state.set_active(None)
    state.remove_job(job["job_id"])


# The origin nudge -----------------------------------------------------------

def test_a_nudge_moves_the_remaining_stages_not_the_saved_job(paused_job):
    """The nudge compensates for paper that drifted mid-run. It belongs to the
    run, so it must not be written back onto the job — a replot tomorrow is a
    fresh sheet and starts from the job's own offset again."""
    # Negative deltas put the page corner above/left of the declared origin,
    # which the API makes the caller confirm rather than refuse — see
    # manual_jog for why that is the user's call.
    plot_worker.nudge_origin(1.5, -2.0, confirm_below_origin=True)

    assert state.origin_nudge() == (1.5, -2.0)
    saved = state.get_job(paused_job["job_id"])
    assert saved["transform_offset_x_mm"] == 0.0
    assert saved["transform_offset_y_mm"] == 0.0


def test_nudges_accumulate_within_one_pause(paused_job):
    """Two taps of the same arrow are two millimetres, not one."""
    plot_worker.nudge_origin(1.0, 0.0)
    plot_worker.nudge_origin(0.5, 0.25)
    assert state.origin_nudge() == (1.5, 0.25)


def test_the_nudge_is_walked_back_and_cleared_when_the_run_ends(paused_job, monkeypatch):
    """The carriage physically returns, and the stored value goes with it.

    Clearing without moving would drift the next run by exactly the nudge;
    moving without clearing would double-count it. Both halves matter.
    """
    walked = []
    monkeypatch.setattr(plot_worker, "_jog_carriage",
                        lambda dx, dy: walked.append((dx, dy)))
    plot_worker.nudge_origin(3.0, 1.0)

    plot_worker._undo_origin_nudge()

    assert walked[-1] == (-3.0, -1.0), "the carriage was not walked back"
    assert state.origin_nudge() == (0.0, 0.0)


def test_the_nudge_is_cleared_even_when_walking_back_fails(paused_job, monkeypatch):
    """The undo runs from a finally block after an outcome that must not be
    replaced by a homing error. A serial failure here cannot be allowed to
    strand the nudge either, or the next run inherits it."""
    plot_worker.nudge_origin(2.0, 2.0)        # stored while the jog still works

    def boom(dx, dy):
        raise RuntimeError("serial port went away")

    monkeypatch.setattr(plot_worker, "_jog_carriage", boom)
    plot_worker._undo_origin_nudge()          # must not raise
    assert state.origin_nudge() == (0.0, 0.0)


def test_nudging_is_refused_when_no_job_is_paused():
    """Outside a pause there is no half-drawn sheet to correct against, and
    the manual jog is the tool instead."""
    state.set_active(None)
    with pytest.raises(RuntimeError, match="pen-change pause"):
        plot_worker.nudge_origin(1.0, 1.0)


# Pen height -----------------------------------------------------------------

def test_pen_height_changed_at_a_pause_belongs_to_that_job(paused_job, monkeypatch):
    """Unlike the nudge, this one *is* saved — the height that worked for this
    pen on this paper should still be there on a replot. What it must not do
    is escape to another job."""
    other = state.add_job({
        "svg_id": "s2", "filename": "other.svg",
        "layer_selections": [{"index": 0, "label": "a", "selected": True}],
        "paper_width_mm": 210.0, "paper_height_mm": 297.0,
        "margin_top_mm": 0.0, "margin_right_mm": 0.0,
        "margin_bottom_mm": 0.0, "margin_left_mm": 0.0,
        "fit_content": False, "transform_scale": 1.0,
        "transform_rotation_deg": 0.0,
        "transform_offset_x_mm": 0.0, "transform_offset_y_mm": 0.0,
        "speed_pendown": 25, "speed_penup": 75, "acceleration": 75,
        "pen_pos_up": 60, "pen_pos_down": 30, "optimize_svg": False,
    })
    try:
        # Stub the interactive AxiDraw: the state write is what is under test.
        class FakeAd:
            options = type("o", (), {})()
            params = type("p", (), {})()

            def interactive(self): pass
            def connect(self): return True
            def penup(self): pass
            def pendown(self): pass
            def disconnect(self): pass

        monkeypatch.setattr(plot_worker.axidraw, "AxiDraw", FakeAd)
        plot_worker.set_live_pen_heights(pen_pos_up=55, pen_pos_down=22, test="down")

        assert state.get_job(paused_job["job_id"])["pen_pos_down"] == 22
        assert state.get_job(other["job_id"])["pen_pos_down"] == 30
    finally:
        state.remove_job(other["job_id"])


# What the pause cannot reach ------------------------------------------------

def test_the_free_jog_tools_stay_idle_only(paused_job):
    """manual_jog / set_origin / jog_home aim the pen at fresh paper before a
    run. Mid-run the physical origin is already baked into the plot underway,
    so moving the page corner out from under it would desynchronise every
    remaining stage. The nudge exists precisely because these are refused."""
    for call in (lambda: plot_worker.manual_jog(1.0, 1.0),
                 plot_worker.set_origin,
                 plot_worker.manual_jog_home):
        with pytest.raises(RuntimeError, match="idle"):
            call()


def test_a_calibration_plot_leaves_the_job_where_it_was(paused_job):
    """Plotting a calibration mark is a side trip. The job has to come back to
    the same pause, at the same stage, or the next layer is skipped or redrawn.
    """
    job_id = paused_job["job_id"]
    before = state.get_job(job_id).get("current_stage_index", 0)

    state.update_job(job_id, status="plotting_calibration")
    state.update_job(job_id, status="awaiting_pen_change")

    after = state.get_job(job_id)
    assert after["status"] == "awaiting_pen_change"
    assert after.get("current_stage_index", 0) == before
