import hashlib
import json
import logging
import math
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

from axidrawinternal import dripfeed
from plotink import ebb_motion, ebb_serial
from pyaxidraw import axidraw

from . import axis_skew, camera, config, ink_cache, notify, optimize_queue, state, svg_complexity, svg_utils, workload

log = logging.getLogger(__name__)

# dripfeed.feed_sm is where AxiDraw dispatches each shattered motion segment
# and advances ad.pen.phys.xpos/ypos to that segment's endpoint. Emitting the
# draw-stream position right here, once per real segment, gives the live
# preview the exact polyline AxiDraw computed — sampling on a timer instead
# (as this used to) skips segment endpoints once the plot moves fast enough
# for segments to complete between polls, which flattens curves into
# straight-edged "triangles" on the live stream.
#
# Suppressed while _jog_carriage is moving: that helper seeds pen.phys to a
# fake bed-corner position before every move (see its own docstring), which
# is not a real pen position and must never reach emit_position — the
# on-card pen cursor and the draw-stream overlay would otherwise jump to it.
_suppress_position_emit = threading.local()
# The (skew_deg, true_axis, paper_width_mm, paper_height_mm, absorb_scale) the
# currently running stage's geometry was corrected with — set by _run_stage.
# Read here so the live draw-stream keeps showing the pristine, uncorrected
# path even though the driver is actually plotting axis_skew-corrected
# geometry: the position read from the driver is run back through the
# inverse of the same transform before it reaches the on-screen overlay.
# None outside of a real hardware stage.
_current_skew: tuple[float, str, float, float, float] | None = None

# The one uniform scale "absorb" mode applies for the whole of the current
# run (see _absorb_scale_for_run). 1.0 in "clip" mode, at zero skew, and
# whenever the correction costs the design nothing — which is most of the
# time. Held for the run rather than recomputed per stage on purpose: a
# scale sized to each stage's own ink would size each layer differently and
# pull a multi-layer drawing apart.
_run_absorb_scale: float = 1.0

if hasattr(dripfeed, "feed_sm"):
    _orig_feed_sm = dripfeed.feed_sm

    def _feed_sm_and_emit_position(ad_ref, move, drip_logger):
        _orig_feed_sm(ad_ref, move, drip_logger)
        if getattr(_suppress_position_emit, "active", False):
            return
        x_in = ad_ref.pen.phys.xpos
        y_in = ad_ref.pen.phys.ypos
        if x_in is not None and y_in is not None:
            x_mm, y_mm = x_in * 25.4, y_in * 25.4
            skew = _current_skew
            if skew is not None and skew[0]:
                x_mm, y_mm = axis_skew.inverse_skew_point(x_mm, y_mm, *skew)
            state.emit_position(x_mm, y_mm, ad_ref.pen.phys.z_up is False)

    dripfeed.feed_sm = _feed_sm_and_emit_position
else:
    # A vendored axidrawinternal release that renamed/removed feed_sm must not
    # take the whole service down at import — this only costs live position
    # updates (the pen cursor, the draw-stream overlay), not plotting itself.
    log.error("plot_worker: axidrawinternal.dripfeed.feed_sm not found — "
             "live pen-position updates (pen cursor, draw stream) are disabled")

STOPPED_COMPLETED = 0
STOPPED_PROGRAMMATIC_PAUSE = 1
STOPPED_BUTTON_PAUSE = 102
STOPPED_SOFTWARE_PAUSE = 103
_PAUSED_CODES = {STOPPED_PROGRAMMATIC_PAUSE, STOPPED_BUTTON_PAUSE, STOPPED_SOFTWARE_PAUSE}

_STOPPED_MESSAGES = {
    101: "Could not connect to the plotter. Check that it is powered on and plugged in.",
    104: "Lost connection to the plotter during the plot.",
}


_STOPPED_CODES = {101: "plot_connect_failed", 104: "plot_connection_lost"}


def _format_stopped(code: int) -> str:
    return _STOPPED_MESSAGES.get(code, f"plot stopped unexpectedly (code {code})")


def _stopped_code(code: int) -> tuple[str, dict | None]:
    """The translation key and arguments for _format_stopped's sentence."""
    key = _STOPPED_CODES.get(code)
    if key is not None:
        return key, None
    return "plot_stopped_unexpectedly", {"code": code}


def _fail(job_id: str, message: str, code: str,
          params: dict | None = None, **fields) -> None:
    """Fail a job with a message the card can render in the user's language.

    The English sentence is stored too and is what the logs and any older
    client see; the browser prefers the key when it recognizes it (see
    jobErrorText in static/app.js). Keys live under `joberror.` in
    static/i18n — a new one has to be added to all ten catalogs, which
    tests/test_i18n.py enforces.
    """
    state.update_job(job_id, status="failed", error=message,
                     error_code=code, error_params=params, **fields)


# Shared control state for the worker thread -------------------------------

_current_ad: axidraw.AxiDraw | None = None
_preview_proc: subprocess.Popen | None = None
_cancel_flag = threading.Event()           # cancel the active job
_continue_event = threading.Event()        # continue: pen change within a job, or next job
_calibrate_event = threading.Event()       # set alongside _continue_event to request a calibration plot from the awaiting_pen_change pause
_calibration_filename: str | None = None   # set alongside _calibrate_event to request a calibration/ library file instead of the job's own calibration layers
_optical_reg_event = threading.Event()     # set alongside _continue_event to request a camera layer-registration measurement from the pause
_optical_reg_probe_mm: float | None = None  # nominal probe-cross offset for that measurement (None -> config default); doubled on an auto-retry
_optical_reg_probe_index = 0               # probe crosses drawn so far this run — each gets its own lane, so no two land on the same spot
_optical_reg_ref_drawn = False             # did this run actually get its reference cross down? nothing may be measured against a mark that isn't there
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()

# Pending live speed/pen-height changes, applied from the worker thread at the
# next pause_check() checkpoint (see _LiveAdjustAxiDraw) — the only hook
# plot_run() calls repeatedly on the worker thread while a stage is in
# progress, and the one safe place to touch the serial port without racing
# the worker thread's own writes.
_live_settings_lock = threading.Lock()
_pending_live_settings: dict | None = None

# Coalesces background estimated_total_seconds recomputes triggered by live
# speed/acceleration changes (see set_live_plot_settings /
# _recompute_live_estimate) — each request bumps the token, so a superseded
# recompute notices before (or after) running its slow preview subprocess and
# drops its result instead of overwriting a newer one.
_live_estimate_lock = threading.Lock()
_live_estimate_token = 0

_poll_thread: threading.Thread | None = None
_stop_polling = threading.Event()
_BUTTON_POLL_INTERVAL_S = 0.3

_position_thread: threading.Thread | None = None
_stop_position = threading.Event()
_POSITION_POLL_INTERVAL_S = 0.1

_preview_cache: "OrderedDict[str, dict]" = OrderedDict()
_PREVIEW_CACHE_MAX = 20
# Preview is CPU-heavy (pyaxidraw simulation). The plot worker AND the plan
# queue both call _run_preview; this lock guarantees only one preview
# subprocess at a time so they don't fight over cores on the Pi.
_preview_lock = threading.Lock()

# How much resident memory one preview subprocess may take before it is killed.
#
# The estimate is the single most expensive thing this program does: pyaxidraw
# plans every stroke in pure Python, and on a drawing dense enough it will ask
# for more memory than the board has. There is no cheap property of an SVG that
# predicts this -- a 17,110-subpath hatched drawing measured 113MB and 11.5s,
# while a 10,786-subpath fragment of a generative one measured 810MB and 144s --
# so it is not a thing to guess at from the document. It is a thing to watch.
#
# Watching works because the preview is a subprocess: it can be killed on its
# own, and killing it costs an estimate rather than the machine. The plot path
# runs the same preview before it touches any hardware, so this bound protects
# the plot too -- which matters, because _run_stage runs pyaxidraw *in-process*
# and an OOM there would take the server down mid-stroke with the pen on paper.
#
# 1000MB leaves room on a 3.7GB board for the browser, the camera and whatever
# else the user is running, and every drawing measured that a person actually
# wanted to plot came in far below it.
PREVIEW_RSS_LIMIT_MB = 1000


# Resolved once, at module level, so a test can point the watchdog at a
# stand-in that grows on demand instead of spending ninety seconds growing a
# real preview past a gigabyte.
_PREVIEW_RUNNER = Path(__file__).parent / "preview_runner.py"


class DrawingTooComplex(RuntimeError):
    """The preview was killed for exceeding PREVIEW_RSS_LIMIT_MB.

    Distinct from a preview that merely failed: this one says the machine
    cannot measure this drawing, which is also the answer to whether it can
    plot it. Raised out of _run_preview so no caller can mistake it for an
    ordinary "no estimate available" and carry on to the plotter.
    """

    def __init__(self, peak_mb: float) -> None:
        super().__init__(f"preview exceeded {PREVIEW_RSS_LIMIT_MB}MB (peaked at {peak_mb:.0f}MB)")
        self.peak_mb = peak_mb


def _proc_rss_mb(pid: int) -> float:
    """Resident size of ``pid`` in MB, or 0 if it has already gone."""
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError):
        pass
    return 0.0

_UPLOAD_DIR_LAZY: Path | None = None


def _uploads() -> Path:
    global _UPLOAD_DIR_LAZY
    if _UPLOAD_DIR_LAZY is None:
        from .main import UPLOAD_DIR
        _UPLOAD_DIR_LAZY = UPLOAD_DIR
    return _UPLOAD_DIR_LAZY


# Preview cache ------------------------------------------------------------

def _preview_cache_key(svg_path: Path, layer_indices: list[int], job: dict) -> str:
    h = hashlib.sha1()
    try:
        h.update(svg_path.read_bytes())
    except Exception:
        h.update(str(svg_path).encode())
    payload = {
        "layers": sorted(layer_indices),
        "paper_w": job["paper_width_mm"],
        "paper_h": job["paper_height_mm"],
        "mt": job["margin_top_mm"],
        "mr": job["margin_right_mm"],
        "mb": job["margin_bottom_mm"],
        "ml": job["margin_left_mm"],
        "fit": job["fit_content"],
        "ts": job.get("transform_scale", 1.0),
        "tr": job.get("transform_rotation_deg", 0.0),
        "tx": job.get("transform_offset_x_mm", 0.0),
        "ty": job.get("transform_offset_y_mm", 0.0),
        "model": config.PLOTTER_MODEL,
        # The active machine is an input to the estimate, not just to the
        # plot: the bed is what the simulation clips against (see
        # _run_preview), and auto-rotate changes the geometry
        # transform_to_paper hands it. A cache hit skips both, so without
        # these two a machine switch would serve an estimate computed for the
        # previous machine.
        "bed": list(machine_bounds_mm()),
        "rot": config.MACHINE_AUTO_ROTATE,
        "sd": job["speed_pendown"],
        "su": job["speed_penup"],
        "acc": job["acceleration"],
        # Per-layer speed overrides (API-set) don't change the geometry, but
        # they do change the timing, so two jobs that differ only in an
        # override must not share a cached estimate.
        "ovr": [[s.get("index"), s.get("speed_pendown"), s.get("speed_penup"),
                 s.get("acceleration")]
                for s in job.get("layer_selections") or []],
    }
    h.update(json.dumps(payload, sort_keys=True).encode())
    return h.hexdigest()


def _preview_cache_get(key: str) -> dict | None:
    if key in _preview_cache:
        _preview_cache.move_to_end(key)
        return dict(_preview_cache[key])
    return None


def _preview_cache_put(key: str, value: dict) -> None:
    _preview_cache[key] = dict(value)
    _preview_cache.move_to_end(key)
    while len(_preview_cache) > _PREVIEW_CACHE_MAX:
        _preview_cache.popitem(last=False)


# Background polling -------------------------------------------------------

def _position_poll_loop() -> None:
    # Draw-stream position is emitted directly from dripfeed.feed_sm (see
    # _feed_sm_and_emit_position above) now, not sampled here. This loop just
    # watches for the pen lifting so a pending pause-at-pen-up can fire.
    while not _stop_position.is_set():
        ad = _current_ad
        if ad is not None and hasattr(ad, "pen") and hasattr(ad.pen, "phys"):
            try:
                if ad.pen.phys.z_up is True and state.pause_at_pen_up_pending():
                    state.set_pause_at_pen_up_pending(False)
                    try:
                        ad.transmit_pause_request()
                    except Exception:
                        log.exception("pen-lift pause request failed")
            except Exception:
                pass
        _stop_position.wait(_POSITION_POLL_INTERVAL_S)


def _start_position_poll() -> None:
    global _position_thread
    _stop_position.clear()
    _position_thread = threading.Thread(target=_position_poll_loop, daemon=True)
    _position_thread.start()


def _stop_position_poll() -> None:
    global _position_thread
    _stop_position.set()
    t = _position_thread
    if t is not None and t.is_alive() and threading.current_thread() is not t:
        t.join(timeout=1.0)
    _position_thread = None
    state.set_pause_at_pen_up_pending(False)


# Pen-change pauses (awaiting_pen_change) are deliberately excluded: they
# only resume via the UI/API Continue action, never the physical button, so
# the user has a chance to calibrate / jog the pen / nudge the origin first.
_BUTTON_ACTIVE_STATUSES = ("paused",)


def _button_poll_loop(job_id: str) -> None:
    port = None
    pressed_status: str | None = None
    try:
        port = ebb_serial.openPort()
        if port is None:
            return
        try:
            ebb_motion.QueryPRGButton(port, verbose=False)
        except Exception:
            return
        while not _stop_polling.is_set():
            job = state.get_job(job_id)
            if job is None or job["status"] not in _BUTTON_ACTIVE_STATUSES:
                return
            try:
                response = ebb_motion.QueryPRGButton(port, verbose=False)
            except Exception:
                break
            if response and str(response).strip().startswith("1"):
                pressed_status = job["status"]
                break
            _stop_polling.wait(_BUTTON_POLL_INTERVAL_S)
    finally:
        if port is not None:
            try:
                ebb_serial.closePort(port)
            except Exception:
                pass

    if pressed_status is None:
        return
    job = state.get_job(job_id)
    if job is None or job["status"] != pressed_status:
        return
    if pressed_status == "paused":
        threading.Thread(target=_safe_resume, daemon=True).start()


def _safe_resume() -> None:
    try:
        resume_active()
    except Exception:
        log.exception("auto-resume via button press failed")


def _start_button_poll(job_id: str) -> None:
    global _poll_thread
    _stop_polling.clear()
    _poll_thread = threading.Thread(target=_button_poll_loop, args=(job_id,), daemon=True)
    _poll_thread.start()


def _stop_button_poll() -> None:
    global _poll_thread
    _stop_polling.set()
    t = _poll_thread
    if t is not None and t.is_alive() and threading.current_thread() is not t:
        t.join(timeout=2.0)
    _poll_thread = None


# pyaxidraw wrappers -------------------------------------------------------

# Maps PLOTTER_MODEL to the ad.params attribute pair the driver reads for
# real travel bounds (see AxiDraw.update_options()). Models not listed (1, 8,
# and anything unrecognized) fall back to x/y_travel_default. Only used to
# find the slot _apply_bed_size overwrites with the active machine's bed —
# which is also why the model number itself has no effect on a plot: whichever
# slot it selects is the one that gets replaced.
_MODEL_TRAVEL_PARAMS = {
    2: ("x_travel_V3A3", "y_travel_V3A3"),
    3: ("x_travel_V3XLX", "y_travel_V3XLX"),
    4: ("x_travel_MiniKit", "y_travel_MiniKit"),
    5: ("x_travel_SEA1", "y_travel_SEA1"),
    6: ("x_travel_SEA2", "y_travel_SEA2"),
    7: ("x_travel_V3B6", "y_travel_V3B6"),
}


def machine_bounds_mm() -> tuple[float, float]:
    """The working area the carriage can actually reach, in mm: the active
    machine profile's bed, taken at face value.

    The profile is believed in both directions, including past the travel of
    whichever AxiDraw model happens to be configured. Every stock AxiDraw is
    landscape — the long axis is X — so measuring a machine against the model
    table amputates any build of a different shape: a portrait machine gets
    cut off at the model's short axis, and everything past it is dropped at
    plot time with nothing but the paper-too-big warning to explain why. The
    profile describes the user's own machine, so an over-stated bed lets the
    carriage be driven into its end stops; that number is theirs to get right,
    and no table can second-guess it.

    This is the single answer every bounds question has to use: the driver's
    clip limits (_apply_bed_size), the jog/nudge readings, and the card's
    paper-too-big warning.
    """
    machine = config.active_machine()
    return machine["width_mm"], machine["height_mm"]


def _carriage_on_bed_mm() -> tuple[float, float]:
    """Where the carriage stands in real bed coordinates, in motor space.

    The three position values are measured from the *declared* origin, which
    isn't necessarily the machine's own corner (see set_origin), so placing the
    carriage on the bed means adding all three back together: the base, the
    idle Move offset, and any pen-change nudge standing on top of it.

    All three are true/physical mm (see _jog_carriage), so on a skewed machine
    the position the driver actually reaches isn't their raw sum — run it
    through the same correction _jog_carriage applies, or the answer describes a
    place the driver was never asked for.
    """
    base_x, base_y = state.origin_base()
    manual_x, manual_y = state.manual_origin_offset()
    nudge_x, nudge_y = state.origin_nudge()
    machine = config.active_machine()
    return axis_skew.skew_delta(
        base_x + manual_x + nudge_x, base_y + manual_y + nudge_y,
        machine["skew_deg"], machine.get("skew_true_axis", "x"))


def refresh_origin_bed_status() -> None:
    """Recompute how far past the bed's far edge the carriage stands, and
    publish it for the top control bar's warning.

    Advisory, not a guard. Standing past the far edge used to be refused
    outright on both the jog and the nudge, which made the pen impossible to
    aim on any sheet placed near the rail; now the move goes through and the
    plot is clipped at the real bed edge instead (see _apply_bed_size), with
    this reading as the thing that says so.

    Called from every place that moves the carriage or redefines the origin,
    and from the settings route when the machine profile changes — a smaller
    bed can put an already-parked carriage outside it without anything moving.
    """
    motor_x, motor_y = _carriage_on_bed_mm()
    bed_w_mm, bed_h_mm = machine_bounds_mm()
    state.set_origin_past_bed(max(0.0, motor_x - bed_w_mm),
                              max(0.0, motor_y - bed_h_mm))


def _bed_travel_params() -> tuple[str, str, float, float]:
    """The two driver params that carry travel bounds for the configured
    model, plus the active machine's bed in inches — everything needed to
    clip an AxiDraw to the real machine.

    Split out of _apply_bed_size because the preview subprocess needs the
    same figures but can't import this module (it runs as a bare script, with
    no package context). Resolving the model mapping here keeps
    _MODEL_TRAVEL_PARAMS as the single place that knows it.
    """
    x_attr, y_attr = _MODEL_TRAVEL_PARAMS.get(
        config.PLOTTER_MODEL, ("x_travel_default", "y_travel_default"))
    bed_x_mm, bed_y_mm = machine_bounds_mm()
    return x_attr, y_attr, bed_x_mm / 25.4, bed_y_mm / 25.4


def _apply_bed_size(ad: axidraw.AxiDraw) -> None:
    """Make machine_bounds_mm() a real travel-bounds limit: override the
    driver's own per-model params.x_travel_*/y_travel_* (read by
    AxiDraw.update_options() to build self.bounds, which clips out-of-bounds
    pen-down moves).

    The whole bed, measured from wherever the carriage is standing — which is
    what the carriage-moving callers (_jog_carriage, _plot_cross) want, since
    each one re-seeds the driver's position trackers itself before moving.
    _apply_plot_bounds is the version for a plot.
    """
    x_attr, y_attr, bed_x_in, bed_y_in = _bed_travel_params()
    setattr(ad.params, x_attr, bed_x_in)
    setattr(ad.params, y_attr, bed_y_in)


def _apply_plot_bounds(ad: axidraw.AxiDraw) -> None:
    """Bound a *plot* by the travel remaining in front of the carriage, so the
    driver's clipping lands on the real bed edge.

    connect() seeds the driver's position trackers at (0, 0), which is also its
    bounds minimum, so left at the full bed the driver believes it has a whole
    bed of travel starting from wherever the carriage happens to stand — with an
    outstanding jog of 50 mm it would drive 50 mm past the real far rail without
    noticing. Subtracting the carriage's own position is what closes that gap.

    That clipping is now the only thing between an aimed-past-the-edge plot and
    the end stops: the jog and nudge guards that used to refuse such a position
    outright are gone (they made the pen impossible to aim at a sheet placed
    near the rail — see refresh_origin_bed_status), and so is the pre-flight
    check that used to fail the job over a leftover jog.

    Clamped at both ends. Zero is the floor: a carriage parked outside the bed
    leaves nothing to plot into, and a negative travel bound would turn the
    driver's own bounds arithmetic into nonsense instead of clipping
    everything. The full bed is the ceiling: a position that comes out negative
    means the carriage is believed to sit behind the machine's own corner,
    which is a broken assumption about the origin rather than real travel to
    hand out, and granting extra reach on the strength of it is the one
    direction this must never round.
    """
    x_attr, y_attr, bed_x_in, bed_y_in = _bed_travel_params()
    motor_x, motor_y = _carriage_on_bed_mm()
    setattr(ad.params, x_attr, max(0.0, bed_x_in - max(0.0, motor_x) / 25.4))
    setattr(ad.params, y_attr, max(0.0, bed_y_in - max(0.0, motor_y) / 25.4))


class _LiveAdjustAxiDraw(axidraw.AxiDraw):
    """AxiDraw subclass that applies pending live speed/pen-height changes at
    the same per-segment checkpoint the driver already uses for pause
    handling — see _apply_pending_live_settings."""

    def pause_check(self):
        _apply_pending_live_settings(self)
        return super().pause_check()


def _apply_pending_live_settings(ad: axidraw.AxiDraw) -> None:
    global _pending_live_settings
    with _live_settings_lock:
        pending = _pending_live_settings
        _pending_live_settings = None
    if not pending:
        return
    speed_changed = False
    if "speed_pendown" in pending:
        ad.options.speed_pendown = pending["speed_pendown"]
        speed_changed = True
    if "speed_penup" in pending:
        ad.options.speed_penup = pending["speed_penup"]
        speed_changed = True
    if "acceleration" in pending:
        ad.options.accel = pending["acceleration"]  # read fresh per segment already
    if speed_changed:
        try:
            ad.enable_motors()
        except Exception:
            log.exception("live settings: enable_motors failed")
    pen_changed = False
    if "pen_pos_up" in pending:
        ad.options.pen_pos_up = pending["pen_pos_up"]
        pen_changed = True
    if "pen_pos_down" in pending:
        ad.options.pen_pos_down = pending["pen_pos_down"]
        pen_changed = True
    if pen_changed:
        try:
            ad.pen.servo_init(ad)
        except Exception:
            log.exception("live settings: servo_init failed")


def _run_stage(current_svg: Path, mode: str, job: dict,
               stage: dict | None = None) -> tuple[int, str]:
    global _current_ad, _current_skew
    ad = _LiveAdjustAxiDraw()
    try:
        ad.plot_setup(str(current_svg))
        ad.options.mode = mode
        ad.options.model = config.PLOTTER_MODEL
        # pyaxidraw has its own independent auto-rotate (on by default, rotates
        # 90 deg CCW whenever the doc is taller than wide) layered on top of
        # whatever orientation transform_to_paper() already baked into the SVG
        # — left enabled, it silently re-rotates an already-correctly-oriented
        # portrait plot, so the preview (which only knows about our own
        # rotation) and the physical plot disagree. We always hand pyaxidraw a
        # document that's already in its final orientation, so disable this.
        ad.options.no_rotate = True
        _apply_plot_bounds(ad)
        skew_machine = config.active_machine()
        _current_skew = (skew_machine["skew_deg"], skew_machine.get("skew_true_axis", "x"),
                         job["paper_width_mm"], job["paper_height_mm"], _run_absorb_scale)
        # Per-stage speeds (a layer override resolved in _run_job) fall back to
        # the job's document/system speeds — as does a stage-less call such as
        # the calibration side-plot.
        speeds = stage if stage is not None else {}
        ad.options.speed_pendown = speeds.get("speed_pendown", job["speed_pendown"])
        ad.options.speed_penup = speeds.get("speed_penup", job["speed_penup"])
        ad.options.accel = speeds.get("acceleration", job["acceleration"])
        ad.options.pen_pos_up = job.get("pen_pos_up", config.PEN_POS_UP_DEFAULT)
        ad.options.pen_pos_down = job.get("pen_pos_down", config.PEN_POS_DOWN_DEFAULT)
        _current_ad = ad
        _start_position_poll()
        output_svg = ad.plot_run(output=True)
        return ad.plot_status.stopped, output_svg
    finally:
        _stop_position_poll()
        try:
            ad.disconnect()
        except Exception:
            pass
        _current_ad = None
        _current_skew = None


def _run_preview(preview_svg_path: Path, job: dict,
                 cancel_event: threading.Event | None = None) -> dict | None:
    global _preview_proc
    runner = _PREVIEW_RUNNER
    x_attr, y_attr, bed_x_in, bed_y_in = _bed_travel_params()
    # Measure the whole drawing, even the part that will not fit.
    #
    # The driver clips pen-down moves to the travel bounds, and clipped moves
    # are simply absent from its totals. Handing it the real bed therefore made
    # the estimate describe the clip rather than the artwork: a 1313x928mm
    # drawing on a 297x420mm bed came back as "0 metres, 0 seconds, 0 pen
    # lifts" for what is really 152 metres and close to three hours. A figure
    # of zero for a drawing full of ink reads as a broken app, and it hid the
    # thing actually worth knowing.
    #
    # So the envelope is widened to hold the page when the page is larger. Note
    # what this does *not* change: whenever the artwork fits the machine — the
    # normal case — max(bed, page) is the bed, and the estimate is identical to
    # before. It only diverges when the plot was going to be clipped anyway,
    # and there the honest number is the one describing the work. That the plot
    # will be cut short is real and separate, and is what the machine-bounds
    # warning on the card is for.
    bed_x_in = max(bed_x_in, job["paper_width_mm"] / 25.4)
    bed_y_in = max(bed_y_in, job["paper_height_mm"] / 25.4)
    # Everything else mirrors _run_stage exactly: the estimate is only
    # meaningful if it measures the same plot at the same speeds and the same
    # orientation (see no_rotate in the runner).
    options = {
        "model": config.PLOTTER_MODEL,
        "speed_pendown": job["speed_pendown"],
        "speed_penup": job["speed_penup"],
        "acceleration": job["acceleration"],
        "travel_params": [x_attr, y_attr],
        "travel_in": [bed_x_in, bed_y_in],
    }
    args = [sys.executable, str(runner), str(preview_svg_path), json.dumps(options)]
    with _preview_lock:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _preview_proc = proc
        # One watcher for both jobs. It already had to poll for cancellation, so
        # reading VmRSS on the same tick costs a file read every 200ms and gives
        # the memory bound somewhere to live -- see PREVIEW_RSS_LIMIT_MB for why
        # the bound is measured rather than predicted from the document.
        too_heavy = threading.Event()
        peak_mb = 0.0

        def _watch() -> None:
            nonlocal peak_mb
            while True:
                if proc.poll() is not None:
                    return
                rss = _proc_rss_mb(proc.pid)
                if rss > peak_mb:
                    peak_mb = rss
                if rss > PREVIEW_RSS_LIMIT_MB:
                    too_heavy.set()
                    log.warning("preview: killing subprocess at %.0fMB (limit %dMB)",
                                rss, PREVIEW_RSS_LIMIT_MB)
                    try:
                        proc.kill()  # kill, not terminate: it is why we are here
                    except Exception:
                        pass
                    return
                if cancel_event is not None and cancel_event.is_set():
                    if proc.poll() is None:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                    return
                time.sleep(0.2)

        watcher = threading.Thread(target=_watch, daemon=True)
        watcher.start()
        try:
            stdout, stderr = proc.communicate()
        finally:
            _preview_proc = None

    if too_heavy.is_set():
        raise DrawingTooComplex(peak_mb)

    if proc.returncode != 0:
        log.warning("preview subprocess exited rc=%s: %s", proc.returncode, stderr.strip())
        return None
    for line in reversed(stdout.strip().split("\n")):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def compute_preview(job: dict, svg_path: Path,
                    cancel_event: threading.Event | None = None) -> dict | None:
    """Build the combined+transformed preview SVG and run the preview
    subprocess for ``job``. Returns the estimate dict (or None on failure /
    cancel).

    Cached by ``_preview_cache`` and serialized via ``_preview_lock`` so the
    plan queue and the plot worker can both call this without racing on
    duplicate work or two simultaneous CPU-bound subprocesses.

    One known bias, documented rather than corrected: every selected layer is
    measured as a single document, while a job with pause-between-layers on
    runs N separate ``plot_run``s (see _run_staged_loop_impl), each of which
    travels out from the origin and homes again. So the figure is low for a
    multi-layer job by roughly N round trips at pen-up speed — tens of seconds
    on A3, well under a percent of a multi-hour plot, and not worth a second
    simulation per layer to recover. The progress bar is unaffected: it counts
    banked plotting spans only, so pen-change wall time is already excluded.
    """
    selections = [s for s in job["layer_selections"] if s.get("selected", True)]
    if not selections:
        return None
    all_selected = [s["index"] for s in selections]
    cache_key = _preview_cache_key(svg_path, all_selected, job)
    cached = _preview_cache_get(cache_key)
    if cached is not None:
        return cached

    if cancel_event is not None and cancel_event.is_set():
        return None

    combined = svg_path.with_name(f"{job['svg_id']}.combined.filt.svg")
    preview_svg = svg_path.with_name(f"{job['svg_id']}.preview.svg")
    try:
        svg_utils.filter_to_layers(svg_path, all_selected, combined)
        svg_utils.transform_to_paper(
            combined, preview_svg,
            job["paper_width_mm"], job["paper_height_mm"],
            job["margin_top_mm"], job["margin_right_mm"],
            job["margin_bottom_mm"], job["margin_left_mm"],
            job["fit_content"],
            transform_scale=job.get("transform_scale", 1.0),
            transform_rotation_deg=job.get("transform_rotation_deg", 0.0),
            transform_offset_x_mm=job.get("transform_offset_x_mm", 0.0),
            transform_offset_y_mm=job.get("transform_offset_y_mm", 0.0),
            machine_auto_rotate=config.MACHINE_AUTO_ROTATE,
        )
    except Exception:
        log.exception("compute_preview: filter/transform failed for job %s", job.get("job_id"))
        return None

    estimate = _run_preview(preview_svg, job, cancel_event=cancel_event)
    if estimate:
        _preview_cache_put(cache_key, estimate)
    return estimate


def _active_pen_heights() -> tuple[int, int]:
    """Pen up/down height (0-100) to use for a standalone manual pen command:
    the active job's own setting while one is loaded (e.g. mid pen-change
    pause, so a manual check uses the height that job actually plots at),
    falling back to the system default otherwise."""
    job = state.active_job()
    if job is not None:
        return (job.get("pen_pos_up", config.PEN_POS_UP_DEFAULT),
                job.get("pen_pos_down", config.PEN_POS_DOWN_DEFAULT))
    return config.PEN_POS_UP_DEFAULT, config.PEN_POS_DOWN_DEFAULT


def manual_pen(raise_pen: bool) -> None:
    """Raise or lower the pen outside of a plot, via the AxiDraw Python
    interactive API (no SVG / plot_run involved). Refuses while a real plot
    is actively driving the pen (_current_ad set) since only one process can
    hold the serial port."""
    if _current_ad is not None:
        raise RuntimeError("Plotter busy")
    ad = axidraw.AxiDraw()
    ad.interactive()
    ad.options.model = config.PLOTTER_MODEL
    ad.options.pen_pos_up, ad.options.pen_pos_down = _active_pen_heights()
    if not ad.connect():
        raise RuntimeError("Could not connect to the plotter. Check that it is powered on and plugged in.")
    try:
        ad.penup() if raise_pen else ad.pendown()
    finally:
        ad.disconnect()


def manual_motors(enable: bool) -> None:
    """Enable or disable the XY stepper motors outside of a plot. Disabling
    lets the carriage be moved by hand (e.g. to home it manually or clear a
    jam); connect() re-enables motors as a side effect, so raise the pen
    first and only then explicitly disable, mirroring the AxiDraw driver's
    own "align" mode. Refuses while a real plot is actively driving the pen,
    same as manual_pen."""
    if _current_ad is not None:
        raise RuntimeError("Plotter busy")
    ad = axidraw.AxiDraw()
    ad.interactive()
    ad.options.model = config.PLOTTER_MODEL
    ad.options.pen_pos_up, ad.options.pen_pos_down = _active_pen_heights()
    if not ad.connect():
        raise RuntimeError("Could not connect to the plotter. Check that it is powered on and plugged in.")
    try:
        if not enable:
            ad.penup()
            ebb_motion.sendDisableMotors(ad.plot_status.port, False)
    finally:
        ad.disconnect()


def _disable_motors_after_completion(job_id: str) -> None:
    """Cut torque to the XY steppers once a plot has finished and walked itself
    back to the origin, so they don't sit warm holding position (the job's
    disable_motors_on_complete option). Best-effort: the plot is already done,
    so a failure here is logged, never allowed to flip the job to `failed`.
    Called from _run_staged_loop's finally, after the run and any origin-nudge
    walk-back have released the serial port (_current_ad is None)."""
    try:
        manual_motors(enable=False)
    except Exception:
        log.exception("could not disable motors after job %s completed", job_id)


# Public control API -------------------------------------------------------

def _clear_side_action_state() -> None:
    """Drop every latched request for a side action from the pen-change pause.

    Each of these is set from an API/UI call alongside _continue_event and read
    back inside the pause loop, so one that is never consumed — a Measure or a
    Calibrate that raced a cancel, leaving the worker already past the pause —
    stays set for the life of the process. The next job's plain Continue would
    then silently run a measurement or a calibration plot nobody asked for.
    Cleared both when a run starts and when one ends, so neither a stale set nor
    a mid-run crash can carry a request across a job boundary.
    """
    global _optical_reg_probe_mm, _optical_reg_probe_index
    global _optical_reg_ref_drawn, _calibration_filename
    _optical_reg_event.clear()
    _optical_reg_probe_mm = None
    _optical_reg_probe_index = 0
    _optical_reg_ref_drawn = False
    _calibrate_event.clear()
    _calibration_filename = None


def start_plot() -> None:
    """Kick off the worker on one job if it isn't already running."""
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _cancel_flag.clear()
        _continue_event.clear()
        _clear_side_action_state()
        t = threading.Thread(target=_run_loop, daemon=True)
        globals()["_worker_thread"] = t
        t.start()


def pause_active() -> None:
    state.set_pause_at_pen_up_pending(False)
    if _current_ad is not None:
        _current_ad.transmit_pause_request()


def pause_at_pen_lift_active() -> None:
    """Soft pause: defer until the next pen lift, so the pen doesn't stop
    mid-stroke (which can leave a dot with pump-action pens). If the pen is
    already up when called, pauses immediately."""
    ad = _current_ad
    if ad is None:
        raise RuntimeError("No active plot")
    z_up = None
    try:
        z_up = getattr(ad.pen.phys, "z_up", None)
    except Exception:
        z_up = None
    if z_up is True:
        ad.transmit_pause_request()
        return
    state.set_pause_at_pen_up_pending(True)


def resume_active() -> None:
    job = state.active_job()
    if job is None or job["status"] != "paused":
        raise RuntimeError("No paused job to resume")

    # Live scenario: worker thread is blocked in the mid-stage pause-wait loop.
    # Flip status to plotting and it unblocks.
    if _worker_thread is not None and _worker_thread.is_alive():
        if not job.get("resume_path"):
            raise RuntimeError("No resume data")
        _stop_button_poll()
        state.update_job(job["job_id"], status="plotting", plotting_started_at=time.time())
        return

    # Post-restart scenario: no worker thread exists. Start a run — its
    # paused-first dispatch picks up this job and routes it to _resume_job,
    # which skips re-planning and jumps into the staged loop.
    start_plot()


REDRAW_MAX_MM = 2000  # fat-finger guard; the real ceiling is the start of the current stage


def redraw_recent(distance_mm: float) -> dict:
    """From a pause, rewind the resume point by ``distance_mm`` of pen-down
    travel and let the plot carry on — so the last stretch of drawing is traced
    again (a skipped line, a pen that ran dry) and the plot still finishes.

    Only valid while the job is ``paused`` with a resume SVG on disk (the state
    a Pause click or the EBB button leaves it in). The resume SVG is rewritten
    before the plot is un-paused: the worker only re-reads it once status flips
    back to plotting.
    """
    job = state.active_job()
    if job is None or job["status"] != "paused" or not job.get("resume_path"):
        raise RuntimeError("No paused job to redraw")
    removed_mm = svg_utils.rewind_resume_distance(Path(job["resume_path"]), distance_mm)
    # We're about to re-trace ground the run already banked, so the wall-clock
    # progress bar would read past 100%. Stretch its denominator by the same
    # share of the estimate the redraw covers.
    dp_mm = (job.get("distance_pendown_m") or 0.0) * 1000
    est = job.get("estimated_total_seconds")
    if dp_mm > 0 and est:
        extra = est * removed_mm / dp_mm
        state.update_job(job["job_id"],
                         progress_total_seconds=(job.get("progress_total_seconds") or 0.0) + extra)
    resume_active()
    return {"requested_mm": distance_mm, "rewound_mm": round(removed_mm, 1)}


def continue_next() -> None:
    """Continue past a pen-change pause, on to the job's next stage.

    The only kind of continue there is: a run ends with its own job, so there
    is no between-jobs pause left to accept.
    """
    job = state.active_job()
    if job and job["status"] == "awaiting_pen_change":
        _continue_event.set()
        return
    raise RuntimeError("Nothing to continue")


def _job_ink_bounds(job: dict, svg_path: Path
                    ) -> tuple[float, float, float, float] | None:
    """Where the *actual drawn geometry* of the job's selected layers lands on
    the page, in mm: (left, top, right, bottom), or None when nothing
    drawable is selected.

    The upload path already primed ink_cache for this file (see main.py), so
    this is normally a dict lookup rather than a vpype parse.

    On a cold cache it waits for ink_cache's worker rather than parsing the
    document here (see rect_for_blocking): the caller needs a real answer, but
    isn't entitled to run a minute of vpype at normal priority beside a moving
    pen. Raises if the measurement failed — an unreadable document is not the
    same answer as an empty one, and the caller treats "no ink" as "nothing to
    fit".

    Deliberately the *un-optimized* source SVG: optimize_svg only simplifies
    paths, never the extent they cover, so the two agree on everything that
    matters here and the cache is shared with the placement measurements.
    """
    layer_indices = [s["index"] for s in job["layer_selections"] if s.get("selected", True)]
    measured, cached_rect = ink_cache.rect_for_blocking(svg_path, layer_indices)
    if not measured:
        raise RuntimeError(f"Could not measure the drawing's ink ({svg_path.name})")
    return svg_utils.ink_bounds_mm(
        svg_path, layer_indices,
        job["paper_width_mm"], job["paper_height_mm"],
        job["margin_top_mm"], job["margin_right_mm"],
        job["margin_bottom_mm"], job["margin_left_mm"],
        job["fit_content"],
        transform_scale=job.get("transform_scale", 1.0),
        transform_rotation_deg=job.get("transform_rotation_deg", 0.0),
        transform_offset_x_mm=job.get("transform_offset_x_mm", 0.0),
        transform_offset_y_mm=job.get("transform_offset_y_mm", 0.0),
        machine_auto_rotate=config.MACHINE_AUTO_ROTATE,
        rect=cached_rect, rect_known=True,
    )


def _absorb_scale_for_run(job: dict) -> float:
    """The uniform scale "absorb" mode applies to everything this run plots.

    Computed once, from every selected layer's ink together, and then reused
    by each stage and by the calibration side-plots — see _run_absorb_scale
    for why it must not be recomputed per stage. 1.0 (nothing applied at all)
    unless the machine is in "absorb" mode with a real skew angle *and* the
    correction would genuinely push that ink off the page.

    Never raises: a machine that can't be measured falls back to plotting at
    the declared size, which is what "clip" mode does anyway, rather than
    failing a job over a fitting nicety.
    """
    machine = config.active_machine()
    if machine.get("skew_mode", "clip") != "absorb" or not machine["skew_deg"]:
        return 1.0
    try:
        ink_bounds = _job_ink_bounds(job, _uploads() / f"{job['svg_id']}.svg")
    except Exception:
        log.exception("absorb: could not measure ink for job %s; plotting at declared size",
                      job["job_id"])
        return 1.0
    return axis_skew.absorb_scale(
        machine["skew_deg"], machine.get("skew_true_axis", "x"), ink_bounds,
        job["paper_width_mm"], job["paper_height_mm"])


def _move_fits_bed(dx_mm: float, dy_mm: float) -> bool:
    """Is this single move short enough for the driver to carry out in full?

    Not a question about where the carriage is. _jog_carriage re-seeds the
    driver's position tracker at the far end of the travel before every move,
    so the whole bed length is always available in the direction of travel and
    absolute position never reaches the driver at all — which is what lets a
    jog run below the app's own zero even though the driver's bounds start
    there.

    What does not fit is a move longer than the bed itself. ad.move() clips
    the motion at the bounds but still records the *unclipped* target as the
    new position, so the app would go on believing a displacement the carriage
    never made — leaving the readout, the bed warning, the plot's own clip
    bounds and Return to Origin all measuring from a place the pen is not. Nothing else bounded
    this: the far-edge guards below are absolute, and below the origin there
    was only a confirmation, so any distance at all could be entered there.

    Measured on the motor-space delta _jog_carriage will actually send (see
    axis_skew.skew_delta), not the true/physical one this function is called
    with — on a skewed machine those differ, and it's the motor-space one
    the driver clips against.
    """
    machine = config.active_machine()
    motor_dx, motor_dy = axis_skew.skew_delta(
        dx_mm, dy_mm, machine["skew_deg"], machine.get("skew_true_axis", "x"))
    bed_w_mm, bed_h_mm = machine_bounds_mm()
    return abs(motor_dx) <= bed_w_mm and abs(motor_dy) <= bed_h_mm


def _claim_idle_machine() -> None:
    """Refuse unless the machine is idle — and, held under _worker_lock, keep
    it that way for the length of the caller's hardware operation.

    Checking state alone left a window: start_plot returns as soon as the
    worker thread is spawned, and the thread only publishes a status once it
    has already picked a job up, so a jog starting in between would have the
    serial port open when the plot tried to claim it. start_plot takes this
    same lock before spawning, so holding it across the move closes that.
    """
    t = _worker_thread
    if (t is not None and t.is_alive()) or state.snapshot()["status"] != "idle":
        raise RuntimeError("Manual jog only available while idle")
    if _current_ad is not None:
        raise RuntimeError("Plotter busy")


def _jog_carriage(dx_mm: float, dy_mm: float) -> None:
    """Physically move the carriage by true/physical (dx_mm, dy_mm), pen up,
    relative to wherever it currently is.

    On a machine with a nonzero axis-skew angle, the raw motor command that
    lands the pen (dx_mm, dy_mm) away isn't (dx_mm, dy_mm) itself — the two
    axes aren't perfectly perpendicular, so a move with any component along
    the "wrong" axis physically drifts along the other one too. Plotted
    artwork already gets this correction (axis_skew.apply_axis_skew); every
    caller here (nudge_origin, _undo_origin_nudge, manual_jog,
    manual_jog_home) needs the same correction, or each jog/nudge silently
    lands slightly off, drifting the carriage's true position away from what
    the app's own (uncorrected) bookkeeping believes — bit by bit, until the
    carriage runs into a real end stop the bookkeeping never saw coming.

    connect() resets the driver's position trackers to (0, 0) — which is also
    its software travel-bounds *minimum* — and ad.move() clips any move whose
    target falls outside those bounds while still recording the unclipped
    target as the new position. Seeding from the bounds minimum therefore makes
    every negative move a silent no-op; seeding from the centre (what this used
    to do) silently truncates anything longer than half the bed while the app
    goes on reporting the full distance in the jog readout and the preview
    overlay.

    So park each axis at the end of the bounds the move travels *away* from
    — using the corrected motor-space direction, since that's what's actually
    commanded — that leaves the entire bed length available, and since the
    guards already keep the accumulated offset inside the bed, nothing can be
    clipped. `pen.turtle` (the target/bounds tracker) and `pen.phys` (what the
    actual hardware move is computed from) are separate and must be seeded
    together, or the carriage jumps to the seeded point instead of moving
    relative to where it really is.
    """
    machine = config.active_machine()
    motor_dx, motor_dy = axis_skew.skew_delta(
        dx_mm, dy_mm, machine["skew_deg"], machine.get("skew_true_axis", "x"))
    ad = axidraw.AxiDraw()
    ad.interactive()
    ad.options.model = config.PLOTTER_MODEL
    ad.options.units = 2  # millimeters
    ad.options.pen_pos_up, ad.options.pen_pos_down = _active_pen_heights()
    _apply_bed_size(ad)
    if not ad.connect():
        raise RuntimeError("Could not connect to the plotter. Check that it is powered on and plugged in.")
    _suppress_position_emit.active = True
    try:
        ad.pen.turtle.xpos = ad.pen.phys.xpos = (
            ad.bounds[0][0] if motor_dx >= 0 else ad.bounds[1][0])
        ad.pen.turtle.ypos = ad.pen.phys.ypos = (
            ad.bounds[0][1] if motor_dy >= 0 else ad.bounds[1][1])
        ad.move(motor_dx, motor_dy)
    finally:
        _suppress_position_emit.active = False
        # A disconnect() failure here does not mean the move above failed —
        # it already happened. Swallowed the same way _run_stage's teardown
        # does, so callers (nudge_origin/_undo_origin_nudge/manual_jog) only
        # ever see an exception from this function when the carriage really
        # didn't move, and don't mistake a clean teardown failure for one.
        try:
            ad.disconnect()
        except Exception:
            pass


def _plot_cross(x_mm: float, y_mm: float, size_mm: float) -> None:
    """Plot one open cross (four arms, hollow centre) centred at (x_mm, y_mm)
    in page millimetres — i.e. relative to the plot origin the carriage is
    already sitting on — then bring the pen back to that origin, pen up.

    Its own return-to-origin is the point: a `plot`-mode run homes the carriage
    itself and the optical-reg phase needs to know exactly where the carriage
    ends up so its follow-up camera jog lands square. Skew-corrects every
    target the same way _jog_carriage does; over a few-mm cross the correction
    is tiny but it keeps the marks in the same frame the artwork plots in.

    The arm layout comes from optical_reg.cross_arms — the detector has to
    regroup these four separate strokes back into one mark, and can only size
    that grouping right if it and the drawing cannot drift apart.

    Seeds pen.turtle and pen.phys before moving, for the reason _jog_carriage
    documents at length: connect() parks the driver's trackers at (0, 0), which
    is also its travel-bounds *minimum*, and moveto() clips a target outside the
    bounds while still recording the unclipped one. Left unseeded, every arm
    reaching left of or above the carriage is silently dropped and the cross
    comes out an "L" — with the driver, and so this function's return to origin,
    believing it drew the whole thing. So park the trackers far enough inside
    the bounds that the cross's whole bounding box is reachable, and offset
    every target to match.
    """
    from . import optical_reg

    machine = config.active_machine()
    skew_deg = machine["skew_deg"]
    true_axis = machine.get("skew_true_axis", "x")

    def _pt(px: float, py: float) -> tuple[float, float]:
        return axis_skew.skew_delta(px, py, skew_deg, true_axis)

    arms = [(_pt(*a), _pt(*b))
            for a, b in optical_reg.cross_arms(x_mm, y_mm, size_mm)]
    home = _pt(0.0, 0.0)
    pts = [p for arm in arms for p in arm] + [home]
    min_x, max_x = min(p[0] for p in pts), max(p[0] for p in pts)
    min_y, max_y = min(p[1] for p in pts), max(p[1] for p in pts)

    ad = axidraw.AxiDraw()
    ad.interactive()
    ad.options.model = config.PLOTTER_MODEL
    ad.options.units = 2  # millimeters
    ad.options.pen_pos_up, ad.options.pen_pos_down = _active_pen_heights()
    _apply_bed_size(ad)
    if not ad.connect():
        raise RuntimeError("Could not connect to the plotter. Check that it is powered on and plugged in.")
    _suppress_position_emit.active = True
    try:
        (b0x, b0y), (b1x, b1y) = ad.bounds[0], ad.bounds[1]
        if (max_x - min_x) > (b1x - b0x) or (max_y - min_y) > (b1y - b0y):
            raise RuntimeError("The registration mark does not fit the bed — "
                               "reduce the mark size or the probe offset.")
        seed_x, seed_y = b0x - min_x, b0y - min_y
        ad.pen.turtle.xpos = ad.pen.phys.xpos = seed_x
        ad.pen.turtle.ypos = ad.pen.phys.ypos = seed_y
        ad.penup()
        for (ax0, ay0), (ax1, ay1) in arms:
            ad.moveto(seed_x + ax0, seed_y + ay0)
            ad.lineto(seed_x + ax1, seed_y + ay1)
        ad.penup()
        ad.moveto(seed_x + home[0], seed_y + home[1])
    finally:
        _suppress_position_emit.active = False
        try:
            ad.disconnect()
        except Exception:
            pass


def nudge_origin(dx_mm: float, dy_mm: float, confirm_below_origin: bool = False) -> None:
    """Shift the origin of the remaining (not-yet-plotted) stages by a small
    delta, to compensate for paper drift between layers during a pen-change
    pause. Belongs to this run only: never written back to the job record,
    and walked back off the carriage when the run ends (see
    _undo_origin_nudge).

    Physically jogs the carriage by the same delta (pen-up, relative), the
    same way manual_pen/manual_motors do — this *is* the correction, not just
    a preview of it: an AxiDraw has no home switches, so pyaxidraw treats
    wherever the carriage is sitting when a stage's plot connects as that
    plot's own zero (see _jog_carriage). Leaving the carriage here is what
    makes every remaining stage land shifted by this delta; _run_staged_loop
    and _run_calibration_phase render their absolute coordinates from the
    job's own offset alone, deliberately not adding the nudge again on top.

    Rejected outright — nothing is moved or stored — only if the move is longer
    than the bed and so could not be carried out in full (see _move_fits_bed).
    Landing past the bed's far edge, or with the artwork off the page, is
    allowed: the first is reported as a warning and clipped at plot time (see
    refresh_origin_bed_status and _apply_plot_bounds), and the second isn't this
    control's business at all — a nudge aims the pen, it doesn't move artwork
    relative to the page. A delta that lands *above/left of* the origin needs
    confirm_below_origin (see manual_jog for why that one is a confirmation
    rather than a refusal)."""
    job = state.active_job()
    if job is None or job["status"] != "awaiting_pen_change":
        raise RuntimeError("Origin nudge only available at a pen-change pause")
    if _current_ad is not None:
        raise RuntimeError("Plotter busy")
    x, y = state.origin_nudge()
    new_x, new_y = x + dx_mm, y + dy_mm
    manual_x, manual_y = state.manual_origin_offset()

    # A nudge longer than the bed can't be carried out in full, and the driver
    # would report the unclipped target anyway — see _move_fits_bed. That is a
    # statement about the move, not about where it lands, which is why it is
    # still a refusal when the far-edge check below it no longer is.
    if not _move_fits_bed(dx_mm, dy_mm):
        raise RuntimeError("Nudge rejected: would move past the machine bed edge.")
    # Landing past the bed's far edge used to be refused here as well. It isn't
    # any more: a sheet taped near the rail made the pen impossible to aim, and
    # the plot is clipped at the real edge now instead (_apply_plot_bounds), so
    # overshoot costs ink rather than end stops. refresh_origin_bed_status below
    # is what tells the user it happened.
    #
    # Measured from the page corner (the declared origin plus the manual jog
    # still standing on top of it), not from this run's own starting point —
    # "above or left of the origin" is a statement about the paper. Testing the
    # nudge alone let an outstanding leftward jog swallow the prompt, confirming
    # nothing while the pen sat off the sheet.
    if ((manual_x + new_x < 0 or manual_y + new_y < 0)
            and not confirm_below_origin):
        raise RuntimeError("Nudge would go above or left of the origin")

    # Move first, record second: a nudge the plotter refused (it is off, or
    # unplugged) must not leave a stored offset behind for the walk-back at
    # the end of the run to act on. See manual_jog for the same ordering.
    _jog_carriage(dx_mm, dy_mm)
    state.set_origin_nudge(new_x, new_y)
    refresh_origin_bed_status()


def _undo_origin_nudge() -> None:
    """Walk the carriage back by whatever this run's pen-change pauses nudged
    it, and clear the stored nudge.

    A nudge aligns the pen that's in the holder *now* against paper that has
    drifted mid-run (see nudge_origin) — it belongs to this run, not to the
    machine. Leaving the carriage where the last nudge put it would quietly
    hand the next run a different physical origin than this one started from,
    since nothing re-homes an AxiDraw between jobs, and each run would inherit
    the sum of every nudge before it. Clearing the stored value without
    actually moving would cause the same drift while also hiding it from the
    readout.

    The manual jog (see manual_jog) is deliberately left alone: that one is
    the user's aim at the paper and is meant to outlive a run.

    Failures are logged, not raised — the run is over and the caller is a
    finally block tidying up after an outcome (completed / cancelled / failed)
    that must not be replaced by a homing error. A failed walk-back is handed
    to the manual jog instead of dropped: the carriage is still standing where
    the nudge put it, so the displacement is real whether or not anything
    records it, and the manual jog is the session-level "how far the carriage
    is from the declared origin" that the readout shows, that the bed warning
    and the plot's clip bounds measure, and that manual_jog_home can walk back.
    Clearing it outright would leave exactly the same physical drift with
    nothing left pointing at it."""
    x, y = state.origin_nudge()
    if x or y:
        try:
            _jog_carriage(-x, -y)
        except Exception:
            log.exception("could not walk the origin nudge back to the run's origin")
            manual_x, manual_y = state.manual_origin_offset()
            state.set_manual_origin_offset(manual_x + x, manual_y + y)
    state.set_origin_nudge(0.0, 0.0)
    refresh_origin_bed_status()


def manual_jog(dx_mm: float, dy_mm: float, confirm_below_origin: bool = False) -> None:
    """Physically move the pen carriage by a small relative amount (pen-up),
    for aligning it to the paper before a plot starts. Idle-only — unlike
    nudge_origin, which corrects an active job's remaining stages mid-plot,
    this has no job to apply to; it just walks the carriage and accumulates
    the net displacement in session state so manual_jog_home knows how far
    to walk back.

    Rejected outright — nothing is moved or stored — only if the move is longer
    than the bed and so could not be carried out in full (see _move_fits_bed).
    Landing *above/left of* the origin is allowed, but only with
    confirm_below_origin: it puts the page's top-left corner off the paper the
    plot was aimed at, and the bed's own near edge is only an assumption
    anyway (the AxiDraw has no home switches, so "0" is wherever the carriage
    happened to sit at startup, not a place the machine knows) — so it's the
    user's call to make, not ours to refuse.

    Landing past the bed's *far* edge is allowed outright, and reported: this is
    a free physical-alignment tool (walking the pen to a mark on the actual
    paper), so refusing it made the pen impossible to aim at a sheet placed near
    the rail. What used to be a refusal here is now the warning published by
    refresh_origin_bed_status, and the plot itself is clipped at the real bed
    edge by _apply_plot_bounds rather than driven into the end stops.

    Deliberately doesn't check the next ready job's artwork bounds: most designs
    are plotted edge-to-edge, leaving zero slack for *any* jog, so that check
    made the tool unusable for exactly the common case it exists for."""
    with _worker_lock:
        _claim_idle_machine()
        x, y = state.manual_origin_offset()
        new_x, new_y = x + dx_mm, y + dy_mm

        # A move longer than the bed can't be executed in full, and the driver
        # reports the unclipped target regardless (see _move_fits_bed) — which
        # would point the readout, the position model and Return to Origin at a
        # place the carriage never reached. A statement about the move itself,
        # so it stays a refusal even though landing past the far edge no longer
        # is; that one is reported by refresh_origin_bed_status below.
        if not _move_fits_bed(dx_mm, dy_mm):
            raise RuntimeError("Jog rejected: would move past the machine bed edge.")
        if (new_x < 0 or new_y < 0) and not confirm_below_origin:
            raise RuntimeError("Jog would go above or left of the origin")

        # Move first, record second. The offset is what the readout shows,
        # what the bed warning and the plot's clip bounds measure, and what
        # Return to Origin walks back, so recording a move the plotter refused (powered off, cable
        # pulled — _jog_carriage raises) would point all three at a place the
        # carriage never went, and Return to Origin would then drive it that
        # far away from the real origin. manual_jog_home has always been this
        # way round; these two were not.
        _jog_carriage(dx_mm, dy_mm)
        state.set_manual_origin_offset(new_x, new_y)
        refresh_origin_bed_status()


def set_origin() -> None:
    """Declare wherever the carriage currently sits to be the page's top-left
    corner. Nothing moves: the accumulated manual jog is folded into the
    origin base and the offset resets to zero, so from here on the readout,
    the preview overlay and the plot's own clip bounds all measure from this
    spot — a plot started now puts the design's own (0, 0) right under the pen
    instead of treating the jog as a shift away from the page corner.

    Idle-only for the same reason manual_jog is: mid-run the physical origin
    is already baked into the plot that's underway, and moving the page corner
    out from under it would desynchronise the remaining stages. Touches no
    hardware, so unlike manual_jog it has nothing to guard against — the
    carriage is where it already was. It doesn't refresh the bed reading either:
    folding the offset into the base leaves their sum, which is the only thing
    refresh_origin_bed_status measures, exactly as it was."""
    with _worker_lock:
        _claim_idle_machine()
        x, y = state.manual_origin_offset()
        base_x, base_y = state.origin_base()
        state.set_origin_base(base_x + x, base_y + y)
        state.set_manual_origin_offset(0.0, 0.0)


def manual_jog_shortcut() -> None:
    """Walk the carriage to the configured Move-shortcut spot in one press,
    and — if move_shortcut_set_origin is on — declare that spot the page's
    top-left corner once it gets there.

    The shortcut names an absolute position: where the carriage should end up
    relative to the declared origin, not how far to travel. So the move is the
    difference between it and the offset already accumulated, which makes a
    second press a no-op rather than a walk twice as far.

    Composed out of manual_jog and set_origin rather than reimplementing
    either, which is what keeps the guards (idle-only, bed edge) and the
    move-first-record-second ordering identical to the buttons either side of
    it — and is why the origin is only declared after the move returns: a
    refused move must not move the page corner. The shortcut is non-negative
    (see config), so manual_jog's below-origin confirmation, which this has no
    way to ask for, is unreachable from here."""
    x, y = state.manual_origin_offset()
    manual_jog(config.MOVE_SHORTCUT_X_MM - x, config.MOVE_SHORTCUT_Y_MM - y)
    if config.MOVE_SHORTCUT_SET_ORIGIN:
        set_origin()


def manual_jog_home() -> None:
    """Physically walk the pen carriage back to the origin — undoing the net
    displacement accumulated by manual_jog, which is measured from wherever
    set_origin last put the origin, not necessarily the machine's own corner.
    Idle-only, same as manual_jog."""
    with _worker_lock:
        _claim_idle_machine()
        x, y = state.manual_origin_offset()
        if x == 0.0 and y == 0.0:
            return
        _jog_carriage(-x, -y)
        state.set_manual_origin_offset(0.0, 0.0)
        refresh_origin_bed_status()


def set_live_pen_heights(pen_pos_up: int | None, pen_pos_down: int | None,
                         test: str) -> None:
    """Live-adjust pen up/down height during a pen-change pause: persists the
    new height(s) onto the active job and immediately moves the pen so the
    user can see/feel the result, mirroring the camera settings' live-preview
    behaviour. Refuses while a real plot is actively driving the pen, same as
    manual_pen/manual_motors."""
    job = state.active_job()
    if job is None or job["status"] != "awaiting_pen_change":
        raise RuntimeError("Pen height can only be live-adjusted at a pen-change pause")
    if _current_ad is not None:
        raise RuntimeError("Plotter busy")
    updates = {}
    if pen_pos_up is not None:
        updates["pen_pos_up"] = pen_pos_up
    if pen_pos_down is not None:
        updates["pen_pos_down"] = pen_pos_down
    if updates:
        state.update_job(job["job_id"], **updates)

    ad = axidraw.AxiDraw()
    ad.interactive()
    ad.options.model = config.PLOTTER_MODEL
    ad.options.pen_pos_up, ad.options.pen_pos_down = _active_pen_heights()
    if not ad.connect():
        raise RuntimeError("Could not connect to the plotter. Check that it is powered on and plugged in.")
    try:
        ad.pendown() if test == "down" else ad.penup()
    finally:
        ad.disconnect()


def set_live_plot_settings(speed_pendown: int | None = None, speed_penup: int | None = None,
                           acceleration: int | None = None, pen_pos_up: int | None = None,
                           pen_pos_down: int | None = None) -> None:
    """Live-adjust speed/pen-height while a stage is actively plotting. Applied
    at the next pause_check() checkpoint (i.e. the next motion/pen command) —
    see _LiveAdjustAxiDraw. Persists onto the job so the UI reflects it and
    later stages default to it (a per-stage speed override, used for
    layer-encoded speeds in multi-stage jobs, can still take precedence at the
    next stage boundary — same limitation set_live_pen_heights already has for
    pen height across stages)."""
    job = state.active_job()
    if job is None or job["status"] != "plotting" or _current_ad is None:
        raise RuntimeError("Plotter is not actively plotting")
    updates = {}
    for key, val in (("speed_pendown", speed_pendown), ("speed_penup", speed_penup),
                     ("acceleration", acceleration), ("pen_pos_up", pen_pos_up),
                     ("pen_pos_down", pen_pos_down)):
        if val is not None:
            updates[key] = val
    if not updates:
        return
    global _pending_live_settings
    with _live_settings_lock:
        _pending_live_settings = {**(_pending_live_settings or {}), **updates}
    state.update_job(job["job_id"], **updates)

    if any(k in updates for k in ("speed_pendown", "speed_penup", "acceleration")):
        _schedule_live_estimate_recompute(job["job_id"])


def _schedule_live_estimate_recompute(job_id: str) -> None:
    """Kick off a background re-estimate after a live speed/acceleration
    change. Pen height alone never reaches here — preview_runner.py doesn't
    take pen height as input, so it can't move the estimate."""
    global _live_estimate_token
    with _live_estimate_lock:
        _live_estimate_token += 1
        token = _live_estimate_token
    threading.Thread(target=_recompute_live_estimate, args=(job_id, token), daemon=True).start()


def run_elapsed_seconds(job: dict) -> float:
    """How long this run has actually been plotting: every span already banked
    by _bank_run_time, plus the one currently in progress. plotting_started_at
    on its own only measures the current stage — it's reset at every stage
    boundary and on every resume."""
    banked = job.get("run_elapsed_seconds") or 0.0
    started = job.get("plotting_started_at")
    if started and job.get("status") == "plotting":
        banked += max(0.0, time.time() - started)
    return banked


def _bank_run_time(job_id: str) -> None:
    """Close the current plotting span and fold it into run_elapsed_seconds.
    Called whenever a stage stops driving the pen, for any reason."""
    job = state.get_job(job_id)
    if job is None or not job.get("plotting_started_at"):
        return
    state.update_job(job_id,
                     run_elapsed_seconds=run_elapsed_seconds(job),
                     plotting_started_at=None)


def _estimate_fields(estimate: dict) -> dict:
    """The estimate columns to write onto a job.

    ``progress_total_seconds`` starts as a copy of the estimate and is the
    only one _recompute_live_estimate rewrites, so ``estimated_total_seconds``
    stays a plain "how long this job takes" figure for the card to display
    instead of turning into an elapsed-plus-remaining hybrid that means
    something different depending on when you read it.
    """
    return {**estimate, "progress_total_seconds": estimate["estimated_total_seconds"]}


def _recompute_live_estimate(job_id: str, token: int) -> None:
    """Re-run the full-document preview under the job's now-current speed/
    acceleration, so the card's estimate and the sticky progress bar (see
    startSharedElapsed in app.js) both reflect the new pace — without
    disturbing the elapsed clock.

    The fresh figure lands in estimated_total_seconds as-is. The progress
    bar's denominator can't just take it, though: the recompute re-estimates
    the whole document, which would throw away however much has already been
    plotted at the old pace. So the fraction of the OLD denominator already
    elapsed is treated as the fraction of the job behind us, and only the
    remaining fraction is rescaled to the new total.

    Coalesced via _live_estimate_token: a superseded request (an even newer
    live-setting change landed) drops its result rather than clobbering one
    that's more current, checked both before the slow preview subprocess
    runs and after.
    """
    # This recompute is secondary to the plot actually running — see
    # app/workload.py for why that also protects what lands on paper.
    workload.deprioritize()

    job = state.get_job(job_id)
    if job is None:
        return
    old_total = job.get("progress_total_seconds") or job.get("estimated_total_seconds")
    started_at = job.get("plotting_started_at")
    if not old_total or not started_at:
        return
    elapsed_at_trigger = run_elapsed_seconds(job)

    with _live_estimate_lock:
        if token != _live_estimate_token:
            return

    svg_path = _effective_svg_path(job)
    try:
        with workload.heavy("live-estimate"):
            estimate = compute_preview(job, svg_path)
    except DrawingTooComplex:
        # Only ever refines a number on screen. A plot is physically running,
        # so the estimate simply stays as it was.
        log.warning("live-estimate: job %s too complex to re-estimate", job_id)
        return

    with _live_estimate_lock:
        if token != _live_estimate_token:
            return
    if not estimate:
        return

    job = state.get_job(job_id)
    if job is None or job.get("status") != "plotting" or job.get("plotting_started_at") != started_at:
        return  # job finished, paused, or moved on while we were computing

    progress_frac = min(1.0, max(0.0, elapsed_at_trigger) / old_total)
    remaining = max(0.0, (1 - progress_frac) * estimate["estimated_total_seconds"])
    state.update_job(job_id, **estimate,
                     progress_total_seconds=run_elapsed_seconds(job) + remaining)


def trigger_calibration() -> None:
    """Run a one-shot plot of every layer with type='calibration', then return
    to the awaiting_pen_change pause. Only valid while the active job is
    paused at a pen change AND has at least one calibration layer."""
    job = state.active_job()
    if job is None or job["status"] != "awaiting_pen_change":
        raise RuntimeError("Calibration plot only available at a pen-change pause")
    has_cal = any(s.get("type") == "calibration"
                  for s in (job.get("layer_selections") or []))
    if not has_cal:
        raise RuntimeError("This job has no calibration layers")
    # Set _calibrate_event first; the wait loop checks it after waking on
    # _continue_event, so order matters.
    _calibrate_event.set()
    _continue_event.set()


def list_calibration_files() -> list[str]:
    """Filenames (not paths) of standalone calibration SVGs in config.CALIBRATION_DIR,
    sorted for a stable UI listing."""
    try:
        return sorted(p.name for p in config.CALIBRATION_DIR.glob("*.svg") if p.is_file())
    except OSError:
        return []


def trigger_calibration_file(filename: str) -> None:
    """Run a one-shot plot of a standalone SVG from the calibration/ library,
    then return to the awaiting_pen_change pause. Same mechanics as
    trigger_calibration(), but for a file that isn't part of the job."""
    job = state.active_job()
    if job is None or job["status"] != "awaiting_pen_change":
        raise RuntimeError("Calibration plot only available at a pen-change pause")
    # Reject path separators outright rather than silently stripping them —
    # a stripped name could collide with an unrelated file.
    if filename != Path(filename).name:
        raise RuntimeError("Invalid calibration filename")
    if not (config.CALIBRATION_DIR / filename).is_file():
        raise RuntimeError("Calibration file not found")
    global _calibration_filename
    _calibration_filename = filename
    _calibrate_event.set()
    _continue_event.set()


def cancel_active() -> None:
    job = state.active_job()
    if job is None:
        raise RuntimeError("No active job")

    # Post-restart: no worker thread to signal. Flip to cancelled directly;
    # the pen is wherever the user left it — they'll need to home manually.
    if _worker_thread is None or not _worker_thread.is_alive():
        state.update_job(job["job_id"], status="cancelled", resume_path=None)
        state.set_active(None)
        return

    st = job["status"]
    if st == "plotting":
        _cancel_flag.set()
        pause_active()
    elif st == "plotting_calibration":
        # Same shape as plotting: stop the AxiDraw mid-stroke; the calibration
        # phase sees _cancel_flag and homes via res_home before bailing.
        _cancel_flag.set()
        pause_active()
    elif st == "planning":
        _cancel_flag.set()
        if _preview_proc is not None:
            try:
                _preview_proc.terminate()
            except Exception:
                pass
    elif st in ("awaiting_optimize", "optimizing"):
        # The plot worker is sitting in optimize_queue.request_for_job's
        # poll loop — flipping the cancel flag is enough; it will yank the
        # task out of the queue (or kill the inflight subprocess if it's ours)
        # without disturbing unrelated upload-time optimizations.
        _cancel_flag.set()
    elif st == "ready":
        # The plan-cache fast path skips the `planning` status, so a job can be
        # active and still reading as `ready` for the moment it takes to
        # optimize/plan. _run_job checks the flag before it touches hardware.
        _cancel_flag.set()
    elif st == "awaiting_pen_change":
        _cancel_flag.set()
        _continue_event.set()
    elif st == "paused":
        _stop_button_poll()
        _cancel_flag.set()
        # The pause-wait loop polls the job's status (not _cancel_flag), so
        # flipping to 'homing' is what actually unblocks it. The loop then
        # runs res_home with the saved resume_path and marks the job cancelled.
        state.update_job(job["job_id"], status="homing")
    else:
        raise RuntimeError(f"Cannot cancel job in status '{st}'")


def shutdown_gracefully(timeout_s: float = 30.0) -> None:
    job = state.active_job()
    if job and job["status"] in ("plotting", "homing") and _current_ad is not None:
        log.info("graceful shutdown: pausing active job %s", job["job_id"])
        try:
            _current_ad.transmit_pause_request()
        except Exception:
            log.exception("graceful shutdown: transmit_pause_request failed")
    _stop_button_poll()
    _stop_position_poll()
    t = _worker_thread
    if t is not None and t.is_alive():
        t.join(timeout=timeout_s)
        if t.is_alive():
            log.warning("graceful shutdown: worker thread did not exit within %ss", timeout_s)


# Plot run -----------------------------------------------------------------

def _run_loop() -> None:
    """Plot one job, then stop.

    A paused job takes priority: one interrupted mid-run by a service restart
    has a half-drawn sheet still on the bed and has to be finished before
    anything fresh starts on top of it. Otherwise the topmost `ready` job is
    the one Plot meant (see state.next_ready_job) — the list is an ordering,
    not a batch to work through.

    The thread ends with that job. Nothing advances to the next one on its
    own, so the paper can be changed with the machine genuinely idle — which
    is also what makes the manual jog and Set origin available again between
    jobs, rather than locked out by a run that is still nominally in progress.
    """
    try:
        if _cancel_flag.is_set():
            _cancel_flag.clear()
            return
        paused = state.next_paused_job()
        job = paused if paused is not None else state.next_ready_job()
        if job is None:
            return
        state.set_active(job["job_id"])
        try:
            if paused is not None:
                _resume_job(job["job_id"])
            else:
                _run_job(job["job_id"])
        finally:
            state.set_active(None)
            _cancel_flag.clear()
    except Exception:
        log.exception("plot run crashed")
    finally:
        _stop_button_poll()


def _optimize_cache_key(job: dict) -> str:
    """Snapshot of the inputs that govern the .opt.svg contents.

    Stored on the job after a successful run so we re-optimize only when the
    user changes a setting that would actually change the output.
    """
    return "|".join([
        f"t={float(job.get('optimize_svg_tolerance_mm', 0.10)):.4f}",
        f"lm={int(bool(job.get('optimize_svg_linemerge', True)))}",
        f"ls={int(bool(job.get('optimize_svg_linesimplify', True)))}",
        f"so={int(bool(job.get('optimize_svg_linesort', True)))}",
        f"rl={int(bool(job.get('optimize_svg_reloop', True)))}",
    ])


def _effective_svg_path(job: dict) -> Path:
    """The SVG path to feed to filter_to_layers / transform_to_paper.

    Beginner mode: if optimization is enabled and the cached .opt.svg is on
    disk, that's the one downstream uses. Otherwise we fall back to the raw
    upload.

    Expert mode: the .opt.svg (if any) was produced explicitly by the user
    via POST /jobs/{id}/optimize-expert/execute, independent of optimize_svg
    — serve it whenever it exists. It wins over the grid: switching a job to
    Expert only greys the Grid section out, so the card goes on PATCHing
    grid_enabled from the still-checked box while the queue (which no-ops for
    expert mode) stops refreshing the tiled file. Reading it here would serve a
    grid nothing can rebuild and ignore the file Execute just wrote.

    Grid: when the "Grid" module is on and the tiled {svg_id}.grid.svg is
    current, that supersedes the optimized file — it is already the optimized
    geometry, tiled. Current, not merely present: the file outlives the settings
    that built it, and serving a stale arrangement means the preview, the export
    and the plot all disagree with the UI. Until a rebuild lands we fall through
    to the un-tiled file.
    """
    src = _uploads() / f"{job['svg_id']}.svg"
    opt_path = src.with_name(f"{job['svg_id']}.opt.svg")
    if job.get("optimize_mode", "beginner") == "expert":
        return opt_path if opt_path.exists() else src
    if job.get("grid_enabled") and optimize_queue.grid_is_current(job):
        return src.with_name(f"{job['svg_id']}.grid.svg")
    if not job.get("optimize_svg"):
        return src
    return opt_path if opt_path.exists() else src


def _run_optimize_phase(job_id: str, src_path: Path, stages: list) -> Path | None:
    """Run vpype on ``src_path`` when the job is in beginner mode with
    ``optimize_svg`` enabled.

    Return the SVG path the rest of the pipeline should use:
      - the cached/freshly-produced ``.opt.svg`` when optimization ran,
      - ``src_path`` unchanged when optimization is disabled or the job is in
        expert mode — expert mode's .opt.svg (if any) was already produced by
        an explicit Execute click and is never re-run at plot time,
      - ``None`` when optimization failed or the user cancelled — the caller
        should return immediately, the job has already been marked
        ``failed`` / ``cancelled``.

    Optimization is delegated to ``optimize_queue`` so we share the single
    vpype worker with upload-time pre-optimizations. While we wait our turn
    the job sits in ``awaiting_optimize``; once our task is picked up the
    queue calls back and we flip to ``optimizing``.
    """
    job = state.get_job(job_id)
    if job is None or job.get("optimize_mode", "beginner") != "beginner" \
            or not (job.get("optimize_svg") or job.get("grid_enabled")):
        return _effective_svg_path(job) if job is not None else src_path

    opt_path = src_path.with_name(f"{job['svg_id']}.opt.svg")
    cache_key = _optimize_cache_key(job)
    # A grid job always goes through the queue (which no-ops instantly via its
    # own fast path when both the .opt.svg and .grid.svg are fresh), so the
    # tiled file is rebuilt whenever a grid setting changed.
    if opt_path.exists() and job.get("optimized_with_key") == cache_key \
            and not job.get("grid_enabled"):
        return opt_path

    state.update_job(job_id,
                     status="awaiting_optimize",
                     started_at=time.time(),
                     plotting_started_at=None,
                     error=None, error_code=None, error_params=None,
                     stages=stages,
                     current_stage_index=0)

    def _on_running() -> None:
        # Same-status updates are fine (the queue's fast-path can race us into
        # 'awaiting_optimize'), but a real transition flips us to optimizing.
        state.update_job(job_id, status="optimizing")

    settings = optimize_queue.settings_from_job(job)
    ok, err = optimize_queue.request_for_job(
        job["svg_id"], settings, _on_running, _cancel_flag,
    )

    if not ok:
        if err == "cancelled" or _cancel_flag.is_set():
            _cancel_flag.clear()
            try:
                opt_path.unlink(missing_ok=True)
            except OSError:
                pass
            state.update_job(job_id, status="cancelled")
            return None
        _fail(job_id, f"Optimization failed: {err or 'unknown error'}",
              "optimize_failed", {"message": err or "unknown error"})
        return None

    state.update_job(job_id, optimized_with_key=cache_key)
    # Re-resolve: with grid on this is now {svg_id}.grid.svg.
    return _effective_svg_path(state.get_job(job_id) or job)


def _run_calibration_phase(job_id: str, svg_path: Path) -> None:
    """Plot every type='calibration' layer of the job, regardless of the
    `selected` flag. Runs as a self-contained side plot from inside the
    awaiting_pen_change pause: no resume tracking, no stage advancement.

    Honours _cancel_flag — if the user hits cancel during the calibration
    plot, the AxiDraw is paused, we home with res_home, and return. The
    caller (the pause-wait loop in _run_staged_loop) then sees _cancel_flag
    and finalises the main job as cancelled.
    """
    job = state.get_job(job_id)
    if job is None:
        return
    cal_indices = [
        s["index"] for s in (job.get("layer_selections") or [])
        if s.get("type") == "calibration"
    ]
    if not cal_indices:
        return  # endpoint should have rejected — defensive

    state.update_job(job_id,
                     status="plotting_calibration",
                     plotting_started_at=time.time())

    filt = svg_path.with_name(f"{job['svg_id']}.cal.filt.svg")
    cal_svg = svg_path.with_name(f"{job['svg_id']}.cal.svg")

    output_svg = ""
    stopped = STOPPED_COMPLETED
    try:
        # A calibration side-plot is not part of the deliverable, so it must
        # not drag along the document's un-layered content.
        svg_utils.filter_to_layers(svg_path, cal_indices, filt, include_orphans=False)
        # Any pending origin nudge is already realized physically: nudge_origin
        # jogged the carriage there, and pyaxidraw treats wherever it's
        # sitting when this plot connects as its own zero (it has no home
        # switches to know otherwise — see _jog_carriage). Adding the nudge
        # into the transform here too would apply it a second time on top of
        # the jog that already moved the carriage.
        svg_utils.transform_to_paper(
            filt, cal_svg,
            job["paper_width_mm"], job["paper_height_mm"],
            job["margin_top_mm"], job["margin_right_mm"],
            job["margin_bottom_mm"], job["margin_left_mm"],
            job["fit_content"],
            transform_scale=job.get("transform_scale", 1.0),
            transform_rotation_deg=job.get("transform_rotation_deg", 0.0),
            transform_offset_x_mm=job.get("transform_offset_x_mm", 0.0),
            transform_offset_y_mm=job.get("transform_offset_y_mm", 0.0),
            machine_auto_rotate=config.MACHINE_AUTO_ROTATE,
        )
        # The run's own absorb scale, not one measured from the calibration
        # marks: they exist to be read against the artwork they accompany, so
        # they have to sit in the same frame it does.
        cal_machine = config.active_machine()
        axis_skew.apply_axis_skew(
            cal_svg, cal_machine["skew_deg"], cal_machine.get("skew_true_axis", "x"),
            job["paper_width_mm"], job["paper_height_mm"], _run_absorb_scale)
        stopped, output_svg = _run_stage(cal_svg, "plot", job)
    except IndexError:
        log.warning("plotink IndexError during calibration plot")
        return
    except Exception:
        log.exception("calibration plot setup failed")
        return

    if stopped in _PAUSED_CODES and _cancel_flag.is_set():
        # User cancelled mid-calibration. Home from where we stopped, then
        # leave _cancel_flag set so the caller cancels the main job.
        resume_path = svg_path.with_name(f"{job['svg_id']}.cal.resume.svg")
        try:
            resume_path.write_text(output_svg, encoding="utf-8")
            _run_stage(resume_path, "res_home", job)
        except Exception:
            log.exception("calibration cancel: res_home failed")
        try:
            resume_path.unlink(missing_ok=True)
        except OSError:
            pass
        return

    if stopped != STOPPED_COMPLETED:
        # Treat anything other than success/cancel as an unhelpful warning —
        # the user is right there, can re-run calibration or continue.
        log.warning("calibration plot ended with stopped=%s", stopped)


def _run_calibration_file_phase(job_id: str, filename: str) -> None:
    """Plot a standalone SVG from the calibration/ library, transformed onto
    the job's current paper/margins/origin-nudge. Same self-contained side-plot
    shape as _run_calibration_phase, for a file that isn't part of the job."""
    job = state.get_job(job_id)
    if job is None:
        return
    src = config.CALIBRATION_DIR / filename
    if not src.is_file():
        log.warning("calibration file vanished: %s", filename)
        return

    state.update_job(job_id, status="plotting_calibration", plotting_started_at=time.time())

    scratch = _uploads() / f"_calfile_{job['svg_id']}.svg"
    output_svg = ""
    stopped = STOPPED_COMPLETED
    try:
        # A pending origin nudge is already realized physically by the
        # carriage jog in nudge_origin — see the matching note in
        # _run_calibration_phase — so it isn't added into the transform here.
        svg_utils.transform_to_paper(
            src, scratch,
            job["paper_width_mm"], job["paper_height_mm"],
            job["margin_top_mm"], job["margin_right_mm"],
            job["margin_bottom_mm"], job["margin_left_mm"],
            # The calibration file's own page size rarely matches the job's
            # configured paper exactly, so always fit it to the actual sheet
            # rather than trusting the job's fit_content preference (which is
            # about the job's own artwork, not this diagnostic overlay).
            fit_content=True,
            transform_scale=job.get("transform_scale", 1.0),
            transform_rotation_deg=job.get("transform_rotation_deg", 0.0),
            transform_offset_x_mm=job.get("transform_offset_x_mm", 0.0),
            transform_offset_y_mm=job.get("transform_offset_y_mm", 0.0),
            # Reconcile the calibration file's orientation to the job's paper
            # regardless of the machine-wide auto-rotate setting: any value
            # other than "off" makes _auto_rotation_deg rotate mismatched
            # content 90° to match the page, which is what keeps all four
            # corner marks on a landscape-drawn file lined up with a
            # portrait-configured (or vice versa) paper. Using config.MACHINE_
            # AUTO_ROTATE here left this unreconciled whenever that setting
            # was "off" (the default), which is why only the one corner that
            # happened to already be in-bounds ever plotted.
            machine_auto_rotate="portrait",
        )
        # Same frame as the artwork — see _run_calibration_phase.
        calfile_machine = config.active_machine()
        axis_skew.apply_axis_skew(
            scratch, calfile_machine["skew_deg"],
            calfile_machine.get("skew_true_axis", "x"),
            job["paper_width_mm"], job["paper_height_mm"], _run_absorb_scale)
        stopped, output_svg = _run_stage(scratch, "plot", job)
    except IndexError:
        log.warning("plotink IndexError during calibration-file plot")
        return
    except Exception:
        log.exception("calibration-file plot setup failed")
        return
    finally:
        scratch.unlink(missing_ok=True)

    if stopped in _PAUSED_CODES and _cancel_flag.is_set():
        resume_path = _uploads() / f"_calfile_{job['svg_id']}.resume.svg"
        try:
            resume_path.write_text(output_svg, encoding="utf-8")
            _run_stage(resume_path, "res_home", job)
        except Exception:
            log.exception("calibration-file cancel: res_home failed")
        finally:
            resume_path.unlink(missing_ok=True)
        return

    if stopped != STOPPED_COMPLETED:
        log.warning("calibration-file plot ended with stopped=%s", stopped)


# Optical layer registration -------------------------------------------------
#
# A carriage-mounted macro camera measures how far a pen-change left the next
# layer off the first, and the result is offered to the user as an origin
# nudge they confirm (it never moves the carriage itself — Apply routes the
# dx/dy through nudge_origin). See app/optical_reg.py for the geometry and
# app/camera.py grab_gray_frames for the stream-safe frame pull.

def _plot_reference_fiducial(job_id: str) -> None:
    """Draw the once-per-run reference cross at M with the layer-1 pen, at the
    end of stage 0. A failure here is logged, never fatal — the job can still
    plot, just without an optical reference to measure against.

    Records whether the mark actually got down. Measuring is refused unless it
    did: with no reference on the paper the phase would draw probe crosses on
    the artwork and then report the distance between two of *those* as the
    misalignment.
    """
    global _optical_reg_ref_drawn
    try:
        _plot_cross(config.OPTICAL_REG_MARK_X_MM, config.OPTICAL_REG_MARK_Y_MM,
                    config.OPTICAL_REG_MARK_SIZE_MM)
    except Exception:
        log.warning("optical-reg: reference cross failed for job %s", job_id,
                    exc_info=True)
        return
    _optical_reg_ref_drawn = True
    state.set_optical_reg_ready(True)


def _reg_tolerance_mm() -> float:
    """How far a measured separation may sit from the nominal probe offset and
    still be believed — i.e. the largest misalignment worth reporting.

    It is optical_reg_max_correction_mm, with headroom for that much on *both*
    axes at once (a factor sqrt(2), rounded up). Everything about the probe
    layout is sized off this one number, so a tighter max correction buys a
    tighter, more reliably readable pattern on the paper.
    """
    return 1.6 * config.OPTICAL_REG_MAX_CORRECTION_MM


def _probe_offset(index: int, probe_mm: float) -> tuple[float, float]:
    """Nominal offset of probe cross `index` from the reference cross.

    Every probe drawn during a run stays on the paper, so they march along their
    own lane in x rather than all landing on M + (p, p): otherwise the second
    pen change draws on top of the first probe and measures a blend of two pens,
    and a widen retry measures against the merged blob it just made.

    The lane pitch is what keeps the pattern unambiguous. `measure` picks the
    pair whose separation best matches the offset asked for, so no *other* pair
    of marks may sit within the tolerance of it. The two that come closest are
    a reference-to-wrong-probe pair (off by a whole lane) and a
    probe-to-probe pair (off by hypot(p, p)); the pitch and the probe floor in
    _run_optical_reg_phase keep both comfortably outside.
    """
    return probe_mm + index * 2.4 * config.OPTICAL_REG_MAX_CORRECTION_MM, probe_mm


def _run_optical_reg_phase(job_id: str, probe_mm: float) -> None:
    """From an awaiting_pen_change pause: draw a probe cross near the reference
    with the current pen (at the offset _probe_offset gives this measurement),
    image the two together, and publish the measured misalignment as a proposed
    nudge for the user to confirm. Returns the carriage to the pause origin and
    the job to awaiting_pen_change. Never raises — a camera or plotter hiccup
    here becomes a 'failed' reading, not a stranded job.

    Touches none of origin_base / manual_origin_offset / origin_nudge: it only
    measures and proposes. Applying the correction is a separate nudge_origin
    call the user makes from the UI.
    """
    from . import optical_reg

    job = state.get_job(job_id)
    if job is None:
        return

    mm_per_px = config.OPTICAL_REG_MM_PER_PX
    rot = config.OPTICAL_REG_CAM_ROTATION_DEG
    mx, my = config.OPTICAL_REG_MARK_X_MM, config.OPTICAL_REG_MARK_Y_MM
    cx, cy = config.OPTICAL_REG_CAM_OFFSET_X_MM, config.OPTICAL_REG_CAM_OFFSET_Y_MM
    max_probe = config.OPTICAL_REG_PROBE_OFFSET_MAX_MM

    # Two floors on the probe offset, either of which a hand-lowered setting
    # can breach — and both of which the feature originally shipped below, so
    # the first attempt always drew a probe overlapping the reference and
    # merged with it:
    #   * 1.25x the mark size, or the two crosses touch;
    #   * enough that a probe-to-probe pair can't be mistaken for the real pair
    #     at _reg_tolerance_mm (see _probe_offset).
    floor = max(1.25 * config.OPTICAL_REG_MARK_SIZE_MM,
                1.5 * _reg_tolerance_mm() / math.sqrt(2.0))
    state.update_job(job_id, status="measuring_registration")
    probe = max(min(probe_mm, max_probe), floor)
    try:
        while True:
            state.set_optical_reg("measuring", probe_mm=probe)
            outcome = _measure_registration_once(optical_reg, probe, mx, my,
                                                 cx, cy, mm_per_px, rot)
            if outcome != "widen":
                return
            if probe * 2.0 > max_probe:
                state.set_optical_reg(
                    "failed", probe_mm=probe,
                    reason="The two crosses overlap even at the widest probe "
                           "offset — use a larger mark size.")
                return
            probe *= 2.0
    except Exception:
        log.exception("optical-reg: measurement failed for job %s", job_id)
        state.set_optical_reg("failed", probe_mm=probe,
                              reason="Measurement could not be completed.")
    finally:
        # Restore the pause only if we still own the status — a cancel landing
        # mid-measurement moves it to 'cancelled', which the pause loop then
        # finalises. Restoring blindly would be an invalid transition.
        cur = state.get_job(job_id)
        if cur is not None and cur["status"] == "measuring_registration":
            state.update_job(job_id, status="awaiting_pen_change")


def _measure_registration_once(optical_reg, probe: float, mx: float, my: float,
                               cx: float, cy: float, mm_per_px: float,
                               rot: float) -> str:
    """One probe-cross + grab + measure cycle. Returns "done" (a reading or a
    terminal failure was published) or "widen" (crosses merged — retry bigger)."""
    global _optical_reg_probe_index
    size = config.OPTICAL_REG_MARK_SIZE_MM
    ox, oy = _probe_offset(_optical_reg_probe_index, probe)

    # Both marks plus their own width have to land inside one frame, and the
    # lanes walk the probe further out with every measurement in the run. Say so
    # rather than grabbing a frame with half the pattern outside it.
    span_mm = math.hypot(ox, oy) + size
    fov_mm = mm_per_px * min(config.CAMERA_RESOLUTION_WIDTH,
                             config.CAMERA_RESOLUTION_HEIGHT)
    if span_mm > fov_mm:
        state.set_optical_reg(
            "failed", probe_mm=probe,
            reason=f"The marks would not fit the camera's view "
                   f"({span_mm:.0f} mm needed, {fov_mm:.0f} mm visible) — "
                   f"restart the job to reset the pattern.")
        return "done"

    # Camera jog: pen at (M + O/2 - C) puts the camera centre on the midpoint
    # between the two crosses. Walked back exactly after the grab.
    jog_x = mx + ox / 2.0 - cx
    jog_y = my + oy / 2.0 - cy

    _plot_cross(mx + ox, my + oy, size)
    _optical_reg_probe_index += 1
    _jog_carriage(jog_x, jog_y)
    try:
        frame = camera.grab_median_gray(config.OPTICAL_REG_FRAMES)
    finally:
        # The walk-back must not replace the reason we got here: a plotter that
        # dropped out mid-measurement would otherwise surface as its own error
        # and bury the camera failure that actually stopped us.
        try:
            _jog_carriage(-jog_x, -jog_y)
        except Exception:
            log.exception("optical-reg: could not return the carriage to the "
                          "pause position")
    if frame is None:
        state.set_optical_reg("failed", probe_mm=probe,
                              reason="No camera frame — is the stream up?")
        return "done"

    exp_px = optical_reg.mm_to_px((ox, oy), mm_per_px, rot)
    tol_mm = _reg_tolerance_mm()
    got = optical_reg.measure(frame, exp_px,
                              group_px=optical_reg.cross_gap(size) / mm_per_px,
                              tol_px=tol_mm / mm_per_px)
    if got is None:
        state.set_optical_reg("failed", probe_mm=probe,
                              reason="No cross found in the frame.")
        return "done"
    if not got["separable"]:
        return "widen"

    sep_mm = optical_reg.px_to_mm(got["sep_px"], mm_per_px, rot)
    lim = config.OPTICAL_REG_MAX_CORRECTION_MM
    ndx, ndy = -(sep_mm[0] - ox), -(sep_mm[1] - oy)
    # Refuse rather than clamp. A reading past the limit is not a correction the
    # user should be one click away from applying: silently pinning it to the
    # limit turns "this measurement is wrong" into a plausible-looking few
    # millimetres, which is exactly what a misread pattern produces.
    if abs(ndx) > lim or abs(ndy) > lim:
        state.set_optical_reg(
            "failed", probe_mm=probe,
            reason=f"Measured offset ({ndx:+.1f}, {ndy:+.1f}) mm is past the "
                   f"{lim:.1f} mm limit — check the marks, or raise the limit "
                   f"if the pen change really is that far off.")
        return "done"

    try:
        ref_c, probe_c = _reg_preview_centers(got["sep_px"], frame.shape)
        camera.write_optical_reg_preview(
            optical_reg.annotate(frame, ref_c, probe_c))
    except Exception:
        log.warning("optical-reg: preview render failed", exc_info=True)

    state.set_optical_reg("measured", dx_mm=round(ndx, 3), dy_mm=round(ndy, 3),
                          confidence=round(got["confidence"], 3), probe_mm=probe)
    return "done"


def _reg_preview_centers(sep_px, shape):
    """Place the two centres symmetrically about the frame centre for the
    annotated preview (measure() reports only their difference)."""
    h, w = shape[:2]
    mid = (w / 2.0, h / 2.0)
    return ((mid[0] - sep_px[0] / 2.0, mid[1] - sep_px[1] / 2.0),
            (mid[0] + sep_px[0] / 2.0, mid[1] + sep_px[1] / 2.0))


def trigger_optical_reg(probe_mm: float | None = None) -> None:
    """Request a camera layer-registration measurement from the current
    pen-change pause. Only valid while the active job is paused at a pen change
    and the camera is calibrated."""
    job = state.active_job()
    if job is None or job["status"] != "awaiting_pen_change":
        raise RuntimeError("Optical registration only available at a pen-change pause")
    if not config.CAMERA_ENABLED:
        raise RuntimeError("Camera is not enabled")
    if config.OPTICAL_REG_MM_PER_PX <= 0:
        raise RuntimeError("Camera is not calibrated for optical registration")
    # Not just the job's opt-in: the reference cross must actually be on the
    # paper. It is drawn once, at the end of stage 0, so a job that never asked
    # for it and a run resumed past that stage both have nothing to measure
    # against — and measuring anyway means drawing probe crosses on the artwork
    # and reporting the gap between two of them as the misalignment. The UI
    # hides the button in both cases; this is the same gate for the API.
    if not job.get("optical_reg") or not _optical_reg_ref_drawn:
        raise RuntimeError("No optical registration reference mark for this run")
    global _optical_reg_probe_mm
    _optical_reg_probe_mm = (config.OPTICAL_REG_PROBE_OFFSET_MM if probe_mm is None
                             else max(0.2, probe_mm))
    _optical_reg_event.set()
    _continue_event.set()


def optical_reg_calibrate() -> dict:
    """One-shot image-scale + rotation calibration, run from idle. Assumes the
    carriage has been jogged over a clear patch of paper. Draws a cross there,
    jogs the carriage two known short vectors watching how far the cross slides
    in the frame, solves the pixel<->mm similarity, and stores it. Returns the
    fitted values plus the resulting field-of-view in mm.
    """
    from . import optical_reg

    with _worker_lock:
        _claim_idle_machine()
        if not config.CAMERA_ENABLED:
            raise RuntimeError("Camera is not enabled")

        d = 3.0
        size = config.OPTICAL_REG_MARK_SIZE_MM
        frames_n = config.OPTICAL_REG_FRAMES

        def _center() -> tuple[float, float]:
            frame = camera.grab_median_gray(frames_n)
            if frame is None:
                raise RuntimeError("No camera frame — is the stream up?")
            got = optical_reg.cross_center(frame)
            if got is None:
                raise RuntimeError("Calibration cross not visible — check the camera aim and focus")
            return got[0], got[1]

        _plot_cross(0.0, 0.0, size)
        # Every jog is walked back, including on the way out: a cross that never
        # came into focus would otherwise leave the carriage parked wherever the
        # sequence gave up, with the app's own bookkeeping none the wiser.
        walked_x = walked_y = 0.0

        def _step(dx: float, dy: float) -> None:
            nonlocal walked_x, walked_y
            _jog_carriage(dx, dy)
            walked_x += dx
            walked_y += dy

        try:
            c0 = _center()
            _step(d, 0.0)
            c1 = _center()
            _step(-d, d)
            c2 = _center()
        finally:
            if walked_x or walked_y:
                try:
                    _jog_carriage(-walked_x, -walked_y)
                except Exception:
                    log.exception("optical-reg: could not return the carriage "
                                  "after calibration")

        # Camera moved +d in x, then (-d, +d): the paper-fixed cross appears to
        # move the opposite way, so the mm vector paired with each pixel shift
        # is negated.
        pairs = [((-d, 0.0), (c1[0] - c0[0], c1[1] - c0[1])),
                 ((d, -d), (c2[0] - c1[0], c2[1] - c1[1]))]
        mm_per_px, rot, rms = optical_reg.solve_scale_rotation(pairs)
        if not (0.0 < mm_per_px < 5.0) or rms > 0.5:
            raise RuntimeError("Calibration reading is inconsistent — check focus, "
                               "lighting, and the camera hflip/vflip settings")

        frame_w = config.CAMERA_RESOLUTION_WIDTH
        frame_h = config.CAMERA_RESOLUTION_HEIGHT
        fov_mm = mm_per_px * min(frame_w, frame_h)
        config.update(optical_reg_mm_per_px=mm_per_px,
                      optical_reg_cam_rotation_deg=rot)
        return {"mm_per_px": round(mm_per_px, 5),
                "cam_rotation_deg": round(rot, 3),
                "fov_mm": round(fov_mm, 1),
                "rms_mm": round(rms, 4)}


def _resume_job(job_id: str) -> None:
    """Resume a job left in 'paused' by a service restart.

    Skips the planning/re-staging block of _run_job: the job's stages list and
    current_stage_index are already on disk. If a resume_path is set we
    continue from that partial SVG via res_plot; otherwise we're at a clean
    stage boundary (an awaiting_pen_change checkpoint) and the next stage is
    re-rendered fresh.
    """
    job = state.get_job(job_id)
    if job is None:
        return
    svg_path = _effective_svg_path(job)
    first_mode = "res_plot" if job.get("resume_path") else "plot"
    _run_staged_loop(job_id, svg_path, first_mode=first_mode)


def _run_job(job_id: str) -> None:
    """Optimize (optional) + plan + plot one job, possibly across multiple
    stages with pen-change pauses between."""
    job = state.get_job(job_id)
    if job is None:
        return

    # Build stages from the job's selections + pause_between_layers. Entries
    # with `selected: false` represent layers the user has toggled off in the
    # UI but whose metadata (name/type) we still want to preserve — skip them
    # when planning the plot.
    selections = [s for s in job["layer_selections"] if s.get("selected", True)]
    pause_between = job.get("pause_between_layers", True)
    _SPEED_KEYS = ("speed_pendown", "speed_penup", "acceleration")

    def _stage_speeds(sel: dict) -> dict:
        # A per-layer speed override falls back to the job's document/system
        # speed for any axis it doesn't set.
        return {k: sel.get(k, job[k]) for k in _SPEED_KEYS}

    # A per-layer speed override only takes effect if its layer is plotted as
    # its own stage (one plot_run = one speed set), so any override forces
    # per-layer staging even when pause_between_layers is off.
    has_speed_override = any(any(k in s for k in _SPEED_KEYS) for s in selections)
    if len(selections) > 1 and (pause_between or has_speed_override):
        stages = [{
            "layer_indices": [s["index"]],
            "labels": [s["label"]],
            "status": "pending",
            **_stage_speeds(s),
        } for s in selections]
    else:
        # One combined stage. Speeds can't vary within a single plot_run, so
        # only a lone selected layer can still carry its own override.
        speeds = (_stage_speeds(selections[0]) if len(selections) == 1
                  else {k: job[k] for k in _SPEED_KEYS})
        stages = [{
            "layer_indices": [s["index"] for s in selections],
            "labels": [s["label"] for s in selections],
            "status": "pending",
            **speeds,
        }]

    svg_path = _uploads() / f"{job['svg_id']}.svg"

    # No pre-flight bounds check. A leftover manual jog used to fail the job
    # here if it pushed the artwork off the page, which meant the one control
    # that exists for aiming the pen at real paper could quietly make the job
    # unplottable. Running past the page edge is a crop, same as it has always
    # been for a design placed off-page by its own offset/scale/rotation; and
    # running past the *bed* edge is clipped at the rail by _apply_plot_bounds
    # rather than driven into the end stops. The carriage's position relative
    # to the bed is reported instead (refresh_origin_bed_status), so the user
    # sees it without the plot being blocked over it.
    #
    # Dropping the check also drops the ink measurement it needed, which on a
    # cold cache stalled every plot for up to a minute before any hardware was
    # touched.
    optimized = _run_optimize_phase(job_id, svg_path, stages)
    if optimized is None:
        return  # phase already marked the job as cancelled/failed
    svg_path = optimized

    # --- Planning (preview) -------------------------------------------------
    # Fast path: if the plan queue already populated _preview_cache (and the
    # job's estimate fields), don't flip to "planning" — go straight from
    # optimizing/queued to plotting via _run_staged_loop. Avoids a stale
    # estimate flicker and a one-frame "Planning" pill in the UI.
    selections_for_preview = [s for s in job["layer_selections"] if s.get("selected", True)]
    all_selected = [s["index"] for s in selections_for_preview]
    cached_estimate = _preview_cache_get(_preview_cache_key(svg_path, all_selected, job)) \
        if selections_for_preview else None

    if cached_estimate is not None:
        state.update_job(job_id,
                         stages=stages,
                         current_stage_index=0,
                         started_at=time.time(),
                         plotting_started_at=None,
                         run_elapsed_seconds=0.0,
                         resume_path=None,
                         error=None, error_code=None, error_params=None,
                         plan_status="ready",
                         **_estimate_fields(cached_estimate))
    else:
        state.update_job(job_id,
                         stages=stages,
                         current_stage_index=0,
                         status="planning",
                         started_at=time.time(),
                         plotting_started_at=None,
                         run_elapsed_seconds=0.0,
                         resume_path=None,
                         error=None, error_code=None, error_params=None,
                         estimated_total_seconds=None,
                         progress_total_seconds=None,
                         distance_pendown_m=None,
                         distance_total_m=None,
                         pen_lifts=None)

        # Already known to be unmeasurable: refuse without spending another
        # minute and another gigabyte rediscovering it. plan_status is reset to
        # "pending" whenever the job is edited (plan_queue.enqueue), including
        # when optimization settings change the effective document, so this
        # cannot latch on a drawing the user has since simplified.
        if job.get("plan_status") == "too_complex":
            log.warning("plot: job %s refused, previously measured too complex", job_id)
            svg_complexity.request(svg_path)
            _fail(job_id,
                  "This drawing is too complex for this machine to plan. "
                  "Nothing was sent to the plotter. See the card for which "
                  "vpype setting would bring it into range.",
                  "too_complex", resume_path=None)
            return

        try:
            estimate = compute_preview(job, svg_path)
        except DrawingTooComplex as exc:
            # This is the one refusal that has to happen here rather than
            # downstream. _run_stage hands the document to pyaxidraw
            # *in-process*, so a drawing that cannot be measured inside a
            # killable subprocess cannot be planned inside the server either —
            # and there it would take the whole service down mid-stroke, with
            # the pen still on the paper. Nothing has been sent to the plotter
            # at this point, and nothing will be.
            log.warning("plot: job %s too complex to plan: %s", job_id, exc)
            svg_complexity.request(svg_path)
            _fail(job_id,
                  "This drawing is too complex for this machine to plan. "
                  "Nothing was sent to the plotter. See the card for which "
                  "vpype setting would bring it into range.",
                  "too_complex", resume_path=None,
                  plan_status="too_complex", plan_error=str(exc))
            return

        if _cancel_flag.is_set():
            state.update_job(job_id, status="cancelled")
            return

        if estimate:
            state.update_job(job_id, plan_status="ready", **_estimate_fields(estimate))

    # A cancel that landed during the optimize/plan phases while the job was
    # still showing as `queued` (the plan-cache fast path never flips it to
    # `planning`) has nothing else to act on — catch it before touching hardware.
    if _cancel_flag.is_set():
        _cancel_flag.clear()
        state.update_job(job_id, status="cancelled")
        return

    # --- Stages -------------------------------------------------------------
    _run_staged_loop(job_id, svg_path, first_mode="plot")


def _run_staged_loop(job_id: str, svg_path: Path, first_mode: str) -> None:
    # Start a plot recording at the genuine beginning of a fresh run only
    # (not a res_plot resume, and not a mid-job stage past the first). A
    # camera problem should never block a plot, so failures here are logged,
    # not raised.
    job = state.get_job(job_id)
    if (job and job.get("record_plot") and first_mode == "plot"
            and job.get("current_stage_index", 0) == 0):
        try:
            camera.start_recording(
                job_id, mode=job.get("record_mode"),
                timelapse_interval_s=job.get("record_timelapse_interval_s"),
                speed_multiplier=job.get("record_speed_multiplier"),
            )
        except RuntimeError:
            log.warning("camera: could not start recording for job %s", job_id, exc_info=True)

    # Resolve "absorb" mode's shrink once, here, before any stage renders:
    # every stage of this run and every calibration side-plot inside it has to
    # be scaled by the same number or the layers stop registering with each
    # other. Cleared at the end of the run, so nothing outside one leaks it.
    global _run_absorb_scale
    _run_absorb_scale = _absorb_scale_for_run(job) if job else 1.0
    want_motor_disable = bool(job and job.get("disable_motors_on_complete"))

    # Origin nudge is session-only: whatever the pen-change pauses in this run
    # accumulated is undone as soon as the run ends (completed/cancelled/
    # failed) so the next run starts from the job's own saved offset again.
    try:
        _run_staged_loop_impl(job_id, svg_path, first_mode)
    finally:
        _run_absorb_scale = 1.0
        _undo_origin_nudge()
        state.set_optical_reg("idle")
        state.set_optical_reg_ready(False)
        _clear_side_action_state()
        if camera.is_recording_job(job_id):
            camera.stop_recording()
        # Last thing, after the nudge walk-back above has done any moving of its
        # own: cut motor torque if the job asked for it and actually finished
        # (not on cancel/failure). The job record is gone by now when
        # delete_on_complete also fired — that only happens on the completed
        # path, so a missing job means completed.
        if want_motor_disable:
            done = state.get_job(job_id)
            if done is None or done.get("status") == "completed":
                _disable_motors_after_completion(job_id)


def _run_staged_loop_impl(job_id: str, svg_path: Path, first_mode: str) -> None:
    mode = first_mode
    while True:
        job = state.get_job(job_id)
        if job is None:
            return
        i = job["current_stage_index"]
        if i >= len(job["stages"]):
            state.update_job(job_id, status="completed", resume_path=None)
            return

        stage = job["stages"][i]

        if mode == "res_plot":
            current_svg = Path(job["resume_path"])
        else:
            # Rendering happens before any hardware is touched, and a bad
            # document (an unparseable viewBox, a truncated file) raises here.
            # Unhandled, that exception unwinds all the way to _run_loop,
            # which logs and returns — leaving the job sitting in its
            # pre-plot status with an empty error field and no worker thread
            # left to move it, so every later Plot click just repeats the
            # crash. Fail the job instead: the user gets a reason, and the
            # queue survives to run the next one.
            try:
                filtered = svg_path.with_name(f"{job['svg_id']}.s{i}.filt.svg")
                # Drawable content that belongs to no layer has no stage of its
                # own, and every stage re-renders from the same source — so it
                # goes in the first stage only, or it gets drawn once per stage,
                # over itself, while the preview shows it once.
                svg_utils.filter_to_layers(svg_path, stage["layer_indices"], filtered,
                                           include_orphans=(i == 0))
                current_svg = svg_path.with_name(f"{job['svg_id']}.s{i}.svg")
                # A fine origin nudge dialed in at a pen-change pause (see
                # nudge_origin) is already realized physically — the carriage
                # sits wherever the jog left it, and pyaxidraw treats that as
                # its own zero for this stage's plot (no home switches to know
                # otherwise). Baking the nudge into this transform too would
                # apply it a second time on top of the jog that already moved
                # the carriage.
                svg_utils.transform_to_paper(
                    filtered, current_svg,
                    job["paper_width_mm"], job["paper_height_mm"],
                    job["margin_top_mm"], job["margin_right_mm"],
                    job["margin_bottom_mm"], job["margin_left_mm"],
                    job["fit_content"],
                    transform_scale=job.get("transform_scale", 1.0),
                    transform_rotation_deg=job.get("transform_rotation_deg", 0.0),
                    transform_offset_x_mm=job.get("transform_offset_x_mm", 0.0),
                    transform_offset_y_mm=job.get("transform_offset_y_mm", 0.0),
                    machine_auto_rotate=config.MACHINE_AUTO_ROTATE,
                )
                stage_machine = config.active_machine()
                axis_skew.apply_axis_skew(
                    current_svg, stage_machine["skew_deg"],
                    stage_machine.get("skew_true_axis", "x"),
                    job["paper_width_mm"], job["paper_height_mm"], _run_absorb_scale)
            except Exception:
                log.exception("could not render stage %s of job %s", i, job_id)
                _fail(job_id,
                      "Could not prepare this layer for plotting — the SVG "
                      "may be malformed. Nothing was sent to the plotter.",
                      "layer_prepare_failed", resume_path=None)
                return

        # Flag this stage as current on the job's stages list
        new_stages = [dict(s) for s in job["stages"]]
        new_stages[i] = dict(new_stages[i], status="current")
        state.update_job(job_id,
                         stages=new_stages,
                         status="plotting",
                         plotting_started_at=time.time())

        try:
            stopped, output_svg = _run_stage(current_svg, mode, job, stage)
        except IndexError:
            log.warning("plotink IndexError; treating as plotter-not-ready")
            _fail(job_id,
                  "Plotter not ready. Wait a moment after power-on and try again.",
                  "plotter_not_ready")
            return
        # The pen has stopped moving for this stage, whatever the outcome —
        # close the span so the progress bar keeps counting the run rather
        # than restarting at the next stage boundary.
        _bank_run_time(job_id)

        if stopped in _PAUSED_CODES:
            resume_path = svg_path.with_name(f"{job['svg_id']}.s{i}.resume.svg")
            try:
                resume_path.write_text(output_svg, encoding="utf-8")
            except OSError:
                # The plotter has already physically stopped at this point —
                # if we can't save resume progress (e.g. disk full), the job
                # must still leave 'plotting' or it's stuck there forever with
                # no worker thread left to move it (every future pause/cancel
                # click 409s on an invalid transition until a service restart).
                log.exception("failed to write resume SVG for job %s", job_id)
                _cancel_flag.clear()
                _stop_button_poll()
                _fail(job_id,
                      "Could not save plot progress (disk full or write "
                      "error). The plotter has stopped physically; home it "
                      "manually before starting another job.",
                      "progress_write_failed", resume_path=None)
                return
            if _cancel_flag.is_set():
                _cancel_flag.clear()
                state.update_job(job_id, status="homing", resume_path=str(resume_path))
                try:
                    _run_stage(resume_path, "res_home", job, stage)
                except Exception:
                    log.exception("res_home failed")
                state.update_job(job_id, status="cancelled", resume_path=None)
                return
            state.update_job(job_id, status="paused", resume_path=str(resume_path))
            _start_button_poll(job_id)
            if camera.is_recording_job(job_id):
                camera.pause_recording()
            # Wait for either resume or cancel via /resume or /cancel
            # We poll the status: when it flips to plotting, continue the loop.
            # When it flips to homing/cancelled, exit.
            while True:
                current = state.get_job(job_id)
                if current is None:
                    return
                st = current["status"]
                if st == "plotting":
                    _stop_button_poll()
                    if camera.is_recording_job(job_id):
                        camera.resume_recording()
                    mode = "res_plot"
                    break
                if st in ("cancelled", "homing"):
                    _stop_button_poll()
                    if st == "cancelled":
                        return
                    # homing
                    try:
                        _run_stage(Path(current["resume_path"]), "res_home", job, stage)
                    except Exception:
                        log.exception("res_home failed")
                    state.update_job(job_id, status="cancelled", resume_path=None)
                    return
                time.sleep(0.1)
            continue

        if stopped != STOPPED_COMPLETED:
            code, params = _stopped_code(stopped)
            _fail(job_id, _format_stopped(stopped), code, params)
            return

        # Stage complete
        new_stages = [dict(s) for s in state.get_job(job_id)["stages"]]
        new_stages[i] = dict(new_stages[i], status="done")
        next_i = i + 1
        state.update_job(job_id, stages=new_stages, current_stage_index=next_i, resume_path=None)
        if config.WEBHOOK_ON_LAYER_COMPLETE:
            notify.fire("layer_complete", job,
                        stage_label=", ".join(new_stages[i].get("labels", [])),
                        stage_index=i, stage_count=len(new_stages))

        if next_i < len(new_stages):
            if job.get("pause_between_layers", True) and len(new_stages) > 1:
                # Manual-only pause: no button polling here (see
                # _BUTTON_ACTIVE_STATUSES) — only /queue/continue (UI/API)
                # resumes it, giving the user a chance to calibrate, jog the
                # pen, and nudge the origin first.
                # The layer-1 pen is still mounted here: drop the once-per-run
                # optical-registration reference cross before the swap.
                if (i == 0 and job.get("optical_reg") and config.CAMERA_ENABLED
                        and config.OPTICAL_REG_MM_PER_PX > 0):
                    _plot_reference_fiducial(job_id)
                state.update_job(job_id, status="awaiting_pen_change")
                state.set_optical_reg("idle")
                if camera.is_recording_job(job_id):
                    camera.pause_recording()
                while True:
                    _continue_event.wait()
                    _continue_event.clear()
                    if _cancel_flag.is_set():
                        _cancel_flag.clear()
                        state.update_job(job_id, status="cancelled")
                        return
                    if _optical_reg_event.is_set():
                        _optical_reg_event.clear()
                        global _optical_reg_probe_mm
                        probe = (_optical_reg_probe_mm
                                 if _optical_reg_probe_mm is not None
                                 else config.OPTICAL_REG_PROBE_OFFSET_MM)
                        _optical_reg_probe_mm = None
                        _run_optical_reg_phase(job_id, probe)
                        if _cancel_flag.is_set():
                            _cancel_flag.clear()
                            state.update_job(job_id, status="cancelled",
                                             resume_path=None)
                            return
                        state.update_job(job_id, status="awaiting_pen_change")
                        continue
                    if _calibrate_event.is_set():
                        _calibrate_event.clear()
                        global _calibration_filename
                        filename, _calibration_filename = _calibration_filename, None
                        if filename:
                            _run_calibration_file_phase(job_id, filename)
                        else:
                            _run_calibration_phase(job_id, svg_path)
                        if _cancel_flag.is_set():
                            _cancel_flag.clear()
                            state.update_job(job_id, status="cancelled",
                                             resume_path=None)
                            return
                        # Back to the pause point; loop and wait for the next
                        # continue / calibrate / cancel. Recording (if this
                        # job owns it) stays paused throughout — the
                        # calibration side-plot isn't part of the deliverable.
                        state.update_job(job_id, status="awaiting_pen_change")
                        continue
                    # Plain continue → break out and run the next stage.
                    if camera.is_recording_job(job_id):
                        camera.resume_recording()
                    break
            mode = "plot"
            continue
        # No more stages
        state.update_job(job_id, status="completed", resume_path=None)
        if config.WEBHOOK_ON_JOB_COMPLETE:
            notify.fire("job_complete", job)
        if job.get("delete_on_complete", False):
            from .main import delete_svg_files
            svg_id = job.get("svg_id")
            state.remove_job(job_id)
            delete_svg_files(svg_id)
        return


# Cancel-aware cancel from the 'homing' status:
# We piggyback on the _cancel_flag path above. If user clicks cancel while
# paused, the cancel branch inside the pause-wait converts the paused job to
# homing, runs res_home, then cancelled. The worker never blocks uninterruptibly.
