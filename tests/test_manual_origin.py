"""The three carriage-position controls, and what they are allowed to touch.

An AxiDraw has no home switches, so wherever the carriage is standing when a
plot connects becomes that plot's zero. Three session values describe where
that is relative to the paper:

    origin_base           where the page's top-left corner has been declared
    manual_origin_offset  how far the idle "Move" buttons have walked from it
    origin_nudge          a mid-run correction dialed in at a pen-change pause

The carriage sits at the sum of all three, and each control owns exactly one
of them. That separation is the whole design — Set origin folds the move into
the base, the nudge belongs to one run and is walked back at the end of it,
and neither may disturb the other's number. These tests pin the separation
down, and pin down the two rules that keep the stored numbers honest: nothing
is recorded unless the hardware actually moved, and nothing is accepted that
the driver would silently clip.
"""
import math

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
    "svg_id": "mo", "filename": "mo.svg",
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


def _reset():
    state.set_origin_nudge(0.0, 0.0)
    state.set_manual_origin_offset(0.0, 0.0)
    state.set_origin_base(0.0, 0.0)


def triple():
    return state.origin_base(), state.manual_origin_offset(), state.origin_nudge()


def boom(dx, dy):
    raise RuntimeError("Could not connect to the plotter. Check that it is powered on and plugged in.")


@pytest.fixture
def idle(monkeypatch):
    """An idle plotter with the carriage jog stubbed; yields the move log."""
    moves = []
    monkeypatch.setattr(plot_worker, "_jog_carriage",
                        lambda dx, dy: moves.append((dx, dy)))
    state.set_active(None)
    _reset()
    yield moves
    _reset()
    state.set_active(None)


@pytest.fixture
def paused(monkeypatch):
    """An active job at a pen-change pause; yields (job, move log).

    A real SVG on disk, deliberately: nudge_origin refuses a delta that would
    push the artwork off the bed, so it measures the document.
    """
    (main.UPLOAD_DIR / "mo.svg").write_text(SVG)
    moves = []
    monkeypatch.setattr(plot_worker, "_jog_carriage",
                        lambda dx, dy: moves.append((dx, dy)))
    job = state.add_job(dict(JOB))
    state.update_job(job["job_id"], status="plotting")
    state.update_job(job["job_id"], status="awaiting_pen_change")
    state.set_active(job["job_id"])
    _reset()
    yield state.get_job(job["job_id"]), moves
    _reset()
    state.set_active(None)
    state.remove_job(job["job_id"])


# Move -----------------------------------------------------------------------

def test_move_accumulates_and_touches_nothing_else(idle):
    plot_worker.manual_jog(10.0, 5.0)
    plot_worker.manual_jog(2.5, -1.0, confirm_below_origin=True)
    assert idle == [(10.0, 5.0), (2.5, -1.0)]
    assert triple() == ((0.0, 0.0), (12.5, 4.0), (0.0, 0.0))


def test_move_is_idle_only(idle):
    job = state.add_job(dict(JOB, svg_id="mo2"))
    state.update_job(job["job_id"], status="plotting")
    state.set_active(job["job_id"])
    try:
        with pytest.raises(RuntimeError, match="only available while idle"):
            plot_worker.manual_jog(1.0, 0.0)
        assert idle == []
    finally:
        state.set_active(None)
        state.remove_job(job["job_id"])


def test_a_move_the_plotter_refused_is_not_recorded(idle, monkeypatch):
    """The readout, the pre-flight check and Return to Origin all read this
    number. Recording a move that never happened points all three at a place
    the pen is not, and Return to Origin would then drive the carriage that
    far *away* from the real origin."""
    monkeypatch.setattr(plot_worker, "_jog_carriage", boom)
    with pytest.raises(RuntimeError, match="Could not connect"):
        plot_worker.manual_jog(25.0, 0.0)
    assert state.manual_origin_offset() == (0.0, 0.0)


def test_a_walk_back_the_plotter_refused_keeps_the_offset(idle, monkeypatch):
    """The mirror of the above: the carriage is still out there, so the
    number that says so has to survive."""
    state.set_manual_origin_offset(25.0, 0.0)
    monkeypatch.setattr(plot_worker, "_jog_carriage", boom)
    with pytest.raises(RuntimeError, match="Could not connect"):
        plot_worker.manual_jog_home()
    assert state.manual_origin_offset() == (25.0, 0.0)


# Bed limits -------------------------------------------------------------------

def test_move_past_the_far_edge_is_refused_from_where_the_carriage_is(idle):
    bed_w, _ = plot_worker.machine_bounds_mm()
    state.set_origin_base(bed_w - 5.0, 0.0)
    with pytest.raises(RuntimeError, match="machine bed edge"):
        plot_worker.manual_jog(6.0, 0.0)
    assert state.manual_origin_offset() == (0.0, 0.0)
    assert idle == []


def test_a_move_longer_than_the_bed_is_refused_however_it_is_confirmed(idle):
    """Confirming a move below the declared origin is the user's call, so no
    distance below it was ever bounded. But the driver clips a move longer
    than the bed while still recording the unclipped target, so the app would
    believe a displacement the carriage never made."""
    bed_w, _ = plot_worker.machine_bounds_mm()
    with pytest.raises(RuntimeError, match="machine bed edge"):
        plot_worker.manual_jog(-(bed_w * 3), 0.0, confirm_below_origin=True)
    assert state.manual_origin_offset() == (0.0, 0.0)
    assert idle == []


def test_a_small_move_below_the_bed_zero_is_still_allowed(idle):
    """_jog_carriage re-seeds the driver before every move, so absolute
    position never reaches it — the origin is only ever an assumption about
    where the carriage happened to be at startup, and stepping behind it is
    the user's call, exactly as before."""
    plot_worker.manual_jog(-5.0, -5.0, confirm_below_origin=True)
    assert state.manual_origin_offset() == (-5.0, -5.0)
    assert idle == [(-5.0, -5.0)]


def test_below_the_declared_origin_is_still_only_a_confirmation(idle):
    """An origin declared away from the machine corner leaves real bed to the
    left of it, and reaching that is a prompt, not a refusal."""
    state.set_origin_base(100.0, 100.0)
    with pytest.raises(RuntimeError, match="above or left"):
        plot_worker.manual_jog(-30.0, 0.0)
    plot_worker.manual_jog(-30.0, 0.0, confirm_below_origin=True)
    assert state.manual_origin_offset() == (-30.0, 0.0)
    assert idle == [(-30.0, 0.0)]


# Set origin ------------------------------------------------------------------

def test_set_origin_folds_the_move_in_and_moves_nothing(idle):
    plot_worker.manual_jog(30.0, 20.0)
    idle.clear()
    plot_worker.set_origin()
    assert idle == [], "set_origin must not touch hardware"
    assert triple() == ((30.0, 20.0), (0.0, 0.0), (0.0, 0.0))


def test_set_origin_is_idempotent(idle):
    plot_worker.manual_jog(30.0, 20.0)
    plot_worker.set_origin()
    plot_worker.set_origin()
    assert state.origin_base() == (30.0, 20.0)


def test_set_origin_clears_what_the_preflight_check_measures(paused, monkeypatch):
    """The point of the button: after it, a plot no longer lands offset by
    the jog that aimed it. The pre-flight measures the jog against the *bed*
    edge now, so pin the bed to the page size to keep the numbers here local."""
    monkeypatch.setattr(plot_worker, "machine_bounds_mm", lambda: (210.0, 297.0))
    job, _ = paused
    svg = main.UPLOAD_DIR / "mo.svg"
    state.set_manual_origin_offset(150.0, 0.0)
    assert plot_worker._delta_correction_mm(job, svg, 150.0, 0.0) is not None
    state.set_active(None)                     # back to idle
    plot_worker.set_origin()
    mx, my = state.manual_origin_offset()
    assert (mx, my) == (0.0, 0.0)
    assert plot_worker._delta_correction_mm(job, svg, mx, my) is None
    assert state.origin_base() == (150.0, 0.0)


# Return to origin ------------------------------------------------------------

def test_home_returns_to_the_declared_origin_not_the_machine_corner(idle):
    state.set_origin_base(100.0, 100.0)
    plot_worker.manual_jog(7.0, 3.0)
    idle.clear()
    plot_worker.manual_jog_home()
    assert idle == [(-7.0, -3.0)]
    assert triple() == ((100.0, 100.0), (0.0, 0.0), (0.0, 0.0))


def test_home_after_set_origin_moves_nothing(idle):
    plot_worker.manual_jog(30.0, 20.0)
    plot_worker.set_origin()
    idle.clear()
    plot_worker.manual_jog_home()
    assert idle == []
    assert triple() == ((30.0, 20.0), (0.0, 0.0), (0.0, 0.0))


# Independence ----------------------------------------------------------------

def test_a_nudge_leaves_the_move_and_the_declared_origin_alone(paused):
    _, moves = paused
    state.set_origin_base(50.0, 40.0)
    state.set_manual_origin_offset(3.0, 2.0)
    moves.clear()
    plot_worker.nudge_origin(1.0, 0.5)
    assert triple() == ((50.0, 40.0), (3.0, 2.0), (1.0, 0.5))
    assert moves == [(1.0, 0.5)]


def test_a_run_that_nudged_gives_the_move_back_unchanged(paused):
    _, moves = paused
    state.set_manual_origin_offset(4.0, 4.0)
    plot_worker.nudge_origin(2.0, 1.0)
    plot_worker._undo_origin_nudge()
    assert triple() == ((0.0, 0.0), (4.0, 4.0), (0.0, 0.0))
    assert moves[-1] == (-2.0, -1.0)


def test_a_nudge_the_plotter_refused_is_not_recorded(paused, monkeypatch):
    """Same rule as the move: a stored nudge is walked back off the carriage
    when the run ends, so one that never happened would send the pen the
    wrong way at the end of the job."""
    monkeypatch.setattr(plot_worker, "_jog_carriage", boom)
    with pytest.raises(RuntimeError, match="Could not connect"):
        plot_worker.nudge_origin(2.0, 0.0)
    assert state.origin_nudge() == (0.0, 0.0)


def test_the_bed_guard_sees_the_move_and_the_nudge_together(paused, monkeypatch):
    """Two individually-fine deltas must not add up to an off-the-bed plot.
    Pin the bed to the page size so the ink's own extent is what matters."""
    monkeypatch.setattr(plot_worker, "machine_bounds_mm", lambda: (210.0, 297.0))
    _, _ = paused
    # Ink spans x 10..100 on the 210 bed: 110mm of slack to the right.
    state.set_manual_origin_offset(115.0, 0.0)
    with pytest.raises(RuntimeError, match="off the bed"):
        plot_worker.nudge_origin(10.0, 0.0)
    assert state.origin_nudge() == (0.0, 0.0)


def test_the_below_origin_prompt_sees_an_outstanding_move(paused):
    """"Above or left of the origin" is a statement about the paper, so it is
    measured from the page corner — the declared origin plus whatever jog is
    still standing on top of it — not from where this run happened to start.
    Testing the nudge on its own let a leftward jog swallow the prompt."""
    state.set_manual_origin_offset(-20.0, 0.0)
    with pytest.raises(RuntimeError, match="above or left"):
        plot_worker.nudge_origin(1.0, 0.0)
    assert state.origin_nudge() == (0.0, 0.0)
    plot_worker.nudge_origin(1.0, 0.0, confirm_below_origin=True)
    assert state.origin_nudge() == (1.0, 0.0)


def test_a_nudge_never_writes_to_the_job_record(paused):
    job, _ = paused
    before = state.get_job(job["job_id"])
    plot_worker.nudge_origin(1.0, 1.0)
    after = state.get_job(job["job_id"])
    for k in ("transform_offset_x_mm", "transform_offset_y_mm",
              "transform_scale", "transform_rotation_deg"):
        assert before[k] == after[k]


def test_set_origin_and_home_are_refused_mid_run(paused):
    with pytest.raises(RuntimeError, match="only available while idle"):
        plot_worker.set_origin()
    with pytest.raises(RuntimeError, match="only available while idle"):
        plot_worker.manual_jog_home()


# Jog to paper origin --------------------------------------------------------
#
# The one-press button between Move and Return to Origin. It walks the carriage
# to the active machine's configured paper origin — an absolute *position*, not
# a distance, so what it must guarantee is where the carriage ends up. It never
# declares an origin: the whole point of the offset is slack for skew / nudge
# corrections, so it stays a plain move.


@pytest.fixture
def paper_origin(monkeypatch):
    """The active machine's paper origin set to 6, 6."""
    machine = config.active_machine()
    monkeypatch.setitem(machine, "paper_origin_x_mm", 6.0)
    monkeypatch.setitem(machine, "paper_origin_y_mm", 6.0)


def test_paper_origin_jog_lands_on_the_configured_spot_whatever_it_started_from(idle, paper_origin):
    plot_worker.manual_jog(40.0, 10.0)
    idle.clear()
    plot_worker.jog_to_paper_origin()
    assert idle == [(-34.0, -4.0)], "the move is the difference, not the origin itself"
    assert triple() == ((0.0, 0.0), (6.0, 6.0), (0.0, 0.0))


def test_pressing_the_paper_origin_jog_twice_leaves_the_carriage_where_it_was(idle, paper_origin):
    """Absolute, so the second press is a no-op — the failure this rules out
    is a jog that walks another 6, 6 every time it is pressed."""
    plot_worker.jog_to_paper_origin()
    plot_worker.jog_to_paper_origin()
    assert state.manual_origin_offset() == (6.0, 6.0)


def test_the_paper_origin_jog_never_declares_an_origin(idle, paper_origin):
    """It is a plain move: the arrival point stays a manual offset, never
    folded into the declared origin. So a second press still lands on the same
    spot rather than walking a fresh 6, 6 from a moved corner."""
    plot_worker.jog_to_paper_origin()
    assert triple() == ((0.0, 0.0), (6.0, 6.0), (0.0, 0.0))
    plot_worker.jog_to_paper_origin()
    assert triple() == ((0.0, 0.0), (6.0, 6.0), (0.0, 0.0))


def test_a_paper_origin_jog_the_plotter_refused_moves_nothing(monkeypatch, paper_origin):
    monkeypatch.setattr(plot_worker, "_jog_carriage", boom)
    state.set_active(None)
    _reset()
    try:
        with pytest.raises(RuntimeError, match="Could not connect"):
            plot_worker.jog_to_paper_origin()
        assert triple() == ((0.0, 0.0), (0.0, 0.0), (0.0, 0.0))
    finally:
        _reset()


def test_the_paper_origin_jog_is_idle_only(idle, paper_origin):
    job = state.add_job(dict(JOB, svg_id="mo3"))
    state.update_job(job["job_id"], status="plotting")
    state.set_active(job["job_id"])
    try:
        with pytest.raises(RuntimeError, match="only available while idle"):
            plot_worker.jog_to_paper_origin()
        assert idle == []
    finally:
        state.set_active(None)
        state.remove_job(job["job_id"])


def test_a_paper_origin_past_the_far_edge_is_refused(idle, monkeypatch):
    """It borrows manual_jog's bed guard rather than carrying its own — a
    profile switch can leave a paper origin pointing off a smaller bed."""
    bed_w, _ = plot_worker.machine_bounds_mm()
    machine = config.active_machine()
    monkeypatch.setitem(machine, "paper_origin_x_mm", bed_w + 10.0)
    monkeypatch.setitem(machine, "paper_origin_y_mm", 6.0)
    with pytest.raises(RuntimeError, match="machine bed edge"):
        plot_worker.jog_to_paper_origin()
    assert state.manual_origin_offset() == (0.0, 0.0)
    assert idle == []


def test_the_paper_origin_jog_is_never_asked_to_confirm_going_behind_the_origin(idle, paper_origin):
    """config keeps the paper origin non-negative, so the one refusal
    manual_jog raises that a body-less button could not answer is out of reach
    — even from a carriage currently sitting behind the origin."""
    plot_worker.manual_jog(-5.0, -5.0, confirm_below_origin=True)
    idle.clear()
    plot_worker.jog_to_paper_origin()
    assert state.manual_origin_offset() == (6.0, 6.0)


# Skew correction --------------------------------------------------------------
#
# On a machine with a nonzero axis-skew angle, plotted artwork already gets
# corrected (axis_skew.apply_axis_skew) — these confirm _jog_carriage gives
# manual_jog/nudge_origin/manual_jog_home the same correction, while the
# session state that drives the UI readout stays in true/design-space mm,
# untouched. Unlike the `idle`/`paused` fixtures above, this can't monkeypatch
# _jog_carriage away — it needs to see what _jog_carriage itself sends the
# driver — so it stubs the AxiDraw driver class one level deeper instead.

SKEW_DEG = 5.0
SKEW_TAN = math.tan(math.radians(SKEW_DEG))


class _Pos:
    xpos = 0.0
    ypos = 0.0


@pytest.fixture
def skewed(monkeypatch):
    """An idle plotter on a machine skewed SKEW_DEG about true_axis="x", with
    the AxiDraw driver stubbed to record every move() call in motor-space mm.
    Yields the move log."""
    moves = []

    class FakeAd:
        def __init__(self):
            self.options = type("o", (), {})()
            self.params = type("p", (), {})()
            self.pen = type("pen", (), {})()
            self.pen.turtle = _Pos()
            self.pen.phys = _Pos()
            self.bounds = [[0.0, 0.0], [1000.0, 1000.0]]

        def interactive(self): pass
        def connect(self): return True
        def move(self, dx, dy): moves.append((dx, dy))
        def disconnect(self): pass

    monkeypatch.setattr(plot_worker.axidraw, "AxiDraw", FakeAd)
    monkeypatch.setattr(config, "MACHINES", [{
        "id": "skew-test", "name": "Skewed", "width_mm": 1000.0, "height_mm": 1000.0,
        "auto_rotate": "off", "skew_deg": SKEW_DEG, "skew_true_axis": "x", "skew_mode": "clip",
    }])
    monkeypatch.setattr(config, "ACTIVE_MACHINE_ID", "skew-test")
    state.set_active(None)
    _reset()
    yield moves
    _reset()
    state.set_active(None)


def test_move_sends_the_driver_a_skew_corrected_command(skewed):
    """The state a caller reads back (and that the UI/preview show) stays the
    true mm the caller asked for; only the driver command is sheared."""
    plot_worker.manual_jog(10.0, 4.0)
    assert skewed == [(10.0 - 4.0 * SKEW_TAN, 4.0)]
    assert state.manual_origin_offset() == (10.0, 4.0)


def test_a_pure_true_axis_move_needs_no_correction(skewed):
    """A move entirely along true_axis="x" has nothing to correct — the
    physical defect only shows up when the other axis is involved."""
    plot_worker.manual_jog(10.0, 0.0)
    assert skewed == [(10.0, 0.0)]


def test_home_walks_back_the_exact_corrected_move(skewed):
    """manual_jog then manual_jog_home must send the driver two commands that
    are exact opposites, in motor space, so the carriage returns to the same
    physical spot it started from — regardless of skew."""
    plot_worker.manual_jog(7.0, 3.0)
    plot_worker.manual_jog_home()
    assert skewed[1] == (-skewed[0][0], -skewed[0][1])
    assert state.manual_origin_offset() == (0.0, 0.0)


def test_a_nudge_sends_a_corrected_command_and_records_the_true_delta(skewed):
    (main.UPLOAD_DIR / "sk.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" '
        'viewBox="0 0 210 297"><path d="M20,20 L80,80" fill="none" stroke="#000"/></svg>')
    job = state.add_job({
        "svg_id": "sk", "filename": "sk.svg",
        "layer_selections": [{"index": 0, "label": "a", "selected": True}],
        "paper_width_mm": 210.0, "paper_height_mm": 297.0,
        "margin_top_mm": 0.0, "margin_right_mm": 0.0,
        "margin_bottom_mm": 0.0, "margin_left_mm": 0.0,
        "fit_content": False, "transform_scale": 1.0, "transform_rotation_deg": 0.0,
        "transform_offset_x_mm": 0.0, "transform_offset_y_mm": 0.0,
        "speed_pendown": 25, "speed_penup": 75, "acceleration": 75,
        "pen_pos_up": 60, "pen_pos_down": 30, "optimize_svg": False,
    })
    try:
        state.update_job(job["job_id"], status="plotting")
        state.update_job(job["job_id"], status="awaiting_pen_change")
        state.set_active(job["job_id"])
        plot_worker.nudge_origin(2.0, 6.0)
        assert skewed == [(2.0 - 6.0 * SKEW_TAN, 6.0)]
        assert state.origin_nudge() == (2.0, 6.0)
    finally:
        state.set_active(None)
        state.remove_job(job["job_id"])


def test_the_far_edge_guard_catches_true_position_drift_a_raw_sum_would_miss(skewed):
    """A nominal position that looks safely inside the bed can still command
    a motor-space position past it, once the axis defect is folded in — this
    is what stops the carriage drifting out of its bed "bit by bit". base_x +
    dx = 950, comfortably under the 1000mm bed on its own — a raw-sum guard
    would allow this. But true_axis="x" means a large move up (-y) also
    drags the motor-space x command up by -dy*tan(skew), past the real edge."""
    state.set_origin_base(900.0, 0.0)
    with pytest.raises(RuntimeError, match="machine bed edge"):
        plot_worker.manual_jog(50.0, -999.0, confirm_below_origin=True)
    assert state.manual_origin_offset() == (0.0, 0.0)
    assert skewed == []
