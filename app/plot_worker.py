import hashlib
import json
import logging
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

from axidrawinternal import dripfeed
from plotink import ebb_motion, ebb_serial
from pyaxidraw import axidraw

from . import camera, config, ink_cache, notify, optimize_queue, state, svg_complexity, svg_utils, workload

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

if hasattr(dripfeed, "feed_sm"):
    _orig_feed_sm = dripfeed.feed_sm

    def _feed_sm_and_emit_position(ad_ref, move, drip_logger):
        _orig_feed_sm(ad_ref, move, drip_logger)
        if getattr(_suppress_position_emit, "active", False):
            return
        x_in = ad_ref.pen.phys.xpos
        y_in = ad_ref.pen.phys.ypos
        if x_in is not None and y_in is not None:
            state.emit_position(x_in * 25.4, y_in * 25.4, ad_ref.pen.phys.z_up is False)

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


def _format_stopped(code: int) -> str:
    return _STOPPED_MESSAGES.get(code, f"plot stopped unexpectedly (code {code})")


# Shared control state for the worker thread -------------------------------

_current_ad: axidraw.AxiDraw | None = None
_preview_proc: subprocess.Popen | None = None
_cancel_flag = threading.Event()           # cancel the active job
_continue_event = threading.Event()        # continue: pen change within a job, or next job
_calibrate_event = threading.Event()       # set alongside _continue_event to request a calibration plot from the awaiting_pen_change pause
_calibration_filename: str | None = None   # set alongside _calibrate_event to request a calibration/ library file instead of the job's own calibration layers
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
    clip limits (_apply_bed_size), the jog/nudge guards, and the card's
    paper-too-big warning.
    """
    machine = config.active_machine()
    return machine["width_mm"], machine["height_mm"]


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
    pen-down moves)."""
    x_attr, y_attr, bed_x_in, bed_y_in = _bed_travel_params()
    setattr(ad.params, x_attr, bed_x_in)
    setattr(ad.params, y_attr, bed_y_in)


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
    global _current_ad
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
        _apply_bed_size(ad)
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


# Public control API -------------------------------------------------------

def start_plot() -> None:
    """Kick off the worker on one job if it isn't already running."""
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _cancel_flag.clear()
        _continue_event.clear()
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


def _delta_correction_mm(job: dict, svg_path: Path,
                         dx_mm: float, dy_mm: float) -> tuple[float, float] | None:
    """How far a delta on top of the job's own placement (a manual jog and/or
    an origin nudge) needs to move, on top of what it already is, so it's no
    *worse* than the job's own placement alone (delta = 0). Returns
    (correction_dx, correction_dy) — add these to the current delta — or
    None if the delta isn't making things worse than the design's own
    baseline already was.

    The job's own transform (offset/scale/rotation/fit_content) is the fixed
    baseline and is *never* itself the reason to block: content placed
    partly (or entirely) off the page by the design's own settings is a
    deliberate choice, visible in the preview — pyaxidraw just clips it at
    plot time, same as always. Only a delta that pushes the placement
    *further* off the page than that baseline already was is worth stopping
    for, since the delta itself is invisible in the preview (a manual jog
    moves the physical carriage; an origin nudge is dialed in blind between
    layers) — the user has no way to see it coming. Comparing against the
    baseline's own overflow (rather than demanding a perfect fit) is what
    keeps an already off-page design from being blocked by a zero, or even a
    corrective, delta.

    Measures the *actual drawn geometry* of the job's selected layers (see
    svg_utils.ink_bounds_mm), not the SVG document's canvas size — a design
    is commonly much smaller than its own canvas, and checking against the
    canvas left zero slack for perfectly reasonable jogs/nudges on any
    design that happens to fill its canvas edge-to-edge. Uses the
    *un-optimized* source SVG: optimize_svg only simplifies paths, it never
    changes the actual drawn extent in any way that would matter here."""
    layer_indices = [s["index"] for s in job["layer_selections"] if s.get("selected", True)]
    # The upload path already primed ink_cache for this file (see main.py),
    # so this is a dict lookup, not a vpype parse — a nudge/jog check that
    # re-measured the whole document itself is the multi-second delay a user
    # feels between clicking "confirm" and the carriage actually moving.
    measured, cached_rect = ink_cache.rect_for(svg_path, layer_indices)
    ink_bounds = svg_utils.ink_bounds_mm(
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
        rect=cached_rect, rect_known=measured,
    )
    if ink_bounds is None:
        return None  # nothing drawable selected — nothing to protect
    base_left, base_top, base_right, base_bottom = ink_bounds

    def axis_correction(base_min: float, base_max: float, page_max: float, delta: float) -> float:
        base_neg = max(0.0, -base_min)
        base_pos = max(0.0, base_max - page_max)
        cur_neg = max(0.0, -(base_min + delta))
        cur_pos = max(0.0, (base_max + delta) - page_max)
        if cur_neg > base_neg + 1e-6:
            return (-base_min - base_neg) - delta
        if cur_pos > base_pos + 1e-6:
            return (page_max + base_pos - base_max) - delta
        return 0.0

    corr_x = axis_correction(base_left, base_right, job["paper_width_mm"], dx_mm)
    corr_y = axis_correction(base_top, base_bottom, job["paper_height_mm"], dy_mm)
    if corr_x == 0.0 and corr_y == 0.0:
        return None
    return corr_x, corr_y


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
    never made — leaving the readout, the pre-flight check and Return to
    Origin all measuring from a place the pen is not. Nothing else bounded
    this: the far-edge guards below are absolute, and below the origin there
    was only a confirmation, so any distance at all could be entered there.
    """
    bed_w_mm, bed_h_mm = machine_bounds_mm()
    return abs(dx_mm) <= bed_w_mm and abs(dy_mm) <= bed_h_mm


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
    """Physically move the carriage by (dx_mm, dy_mm), pen up, relative to
    wherever it currently is.

    connect() resets the driver's position trackers to (0, 0) — which is also
    its software travel-bounds *minimum* — and ad.move() clips any move whose
    target falls outside those bounds while still recording the unclipped
    target as the new position. Seeding from the bounds minimum therefore makes
    every negative move a silent no-op; seeding from the centre (what this used
    to do) silently truncates anything longer than half the bed while the app
    goes on reporting the full distance in the jog readout and the preview
    overlay.

    So park each axis at the end of the bounds the move travels *away* from —
    that leaves the entire bed length available, and since the guards already
    keep the accumulated offset inside the bed, nothing can be clipped.
    `pen.turtle` (the target/bounds tracker) and `pen.phys` (what the actual
    hardware move is computed from) are separate and must be seeded together,
    or the carriage jumps to the seeded point instead of moving relative to
    where it really is.
    """
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
            ad.bounds[0][0] if dx_mm >= 0 else ad.bounds[1][0])
        ad.pen.turtle.ypos = ad.pen.phys.ypos = (
            ad.bounds[0][1] if dy_mm >= 0 else ad.bounds[1][1])
        ad.move(dx_mm, dy_mm)
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

    Rejected outright — nothing is moved or stored — if the resulting delta
    would push the paper past the machine bed's far edge, or the content's
    bounding box past the page edge. A delta that lands *above/left of* the
    origin needs confirm_below_origin (see manual_jog for why that one is a
    confirmation rather than a refusal)."""
    job = state.active_job()
    if job is None or job["status"] != "awaiting_pen_change":
        raise RuntimeError("Origin nudge only available at a pen-change pause")
    if _current_ad is not None:
        raise RuntimeError("Plotter busy")
    x, y = state.origin_nudge()
    new_x, new_y = x + dx_mm, y + dy_mm
    # The delta the run actually plots at is the idle manual jog plus this
    # nudge (nothing re-homes the carriage between them), so both guards below
    # have to see both — checking the nudge alone lets two individually-fine
    # deltas add up to one that runs off the page, or into the rail.
    base_x, base_y = state.origin_base()
    manual_x, manual_y = state.manual_origin_offset()

    # Overshoot on the far side runs the carriage into its own end stops, so
    # the paper's origin corner has to stay inside the bed's travel envelope.
    # Measured in real bed coordinates — the declared origin (see set_origin)
    # is not necessarily the machine's own corner, and the manual jog is still
    # physically applied — so all three add up to where the carriage would
    # actually stand. (Overshoot of the *page* is comparatively benign —
    # pyaxidraw clips it, same as artwork that runs past the page edge, see
    # _delta_correction_mm — so this only guards the bed's own outer extent,
    # not the paper size on top of it.) A single nudge longer than the bed is
    # refused alongside it, for the different reason in _move_fits_bed.
    bed_w_mm, bed_h_mm = machine_bounds_mm()
    if (base_x + manual_x + new_x > bed_w_mm
            or base_y + manual_y + new_y > bed_h_mm
            or not _move_fits_bed(dx_mm, dy_mm)):
        raise RuntimeError("Nudge rejected: would move past the machine bed edge.")
    # Measured from the page corner (the declared origin plus the manual jog
    # still standing on top of it), not from this run's own starting point —
    # "above or left of the origin" is a statement about the paper, and the
    # bed guard directly above already reasons in that same absolute frame.
    # Testing the nudge alone let an outstanding leftward jog swallow the
    # prompt, confirming nothing while the pen sat off the sheet.
    if ((manual_x + new_x < 0 or manual_y + new_y < 0)
            and not confirm_below_origin):
        raise RuntimeError("Nudge would go above or left of the origin")
    x, y = new_x, new_y

    svg_path = _uploads() / f"{job['svg_id']}.svg"
    correction = _delta_correction_mm(job, svg_path, manual_x + x, manual_y + y)
    if correction is not None:
        cx, cy = correction
        raise RuntimeError(
            f"Nudge rejected: would push the artwork off the page. Nudge back by "
            f"({cx:+.1f}, {cy:+.1f}) mm to bring it back onto the page."
        )
    # Move first, record second: a nudge the plotter refused (it is off, or
    # unplugged) must not leave a stored offset behind for the walk-back at
    # the end of the run to act on. See manual_jog for the same ordering.
    _jog_carriage(dx_mm, dy_mm)
    state.set_origin_nudge(x, y)


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
    is from the declared origin" that the readout shows, that _run_job's
    pre-flight check measures, and that manual_jog_home can walk back.
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


def manual_jog(dx_mm: float, dy_mm: float, confirm_below_origin: bool = False) -> None:
    """Physically move the pen carriage by a small relative amount (pen-up),
    for aligning it to the paper before a plot starts. Idle-only — unlike
    nudge_origin, which corrects an active job's remaining stages mid-plot,
    this has no job to apply to; it just walks the carriage and accumulates
    the net displacement in session state so manual_jog_home knows how far
    to walk back.

    Rejected outright — nothing is moved or stored — if it would run the
    carriage past the machine bed's far edge, or that is simply longer than
    the bed and so could not be carried out in full (see _move_fits_bed).
    Landing *above/left of* the origin is allowed, but only with
    confirm_below_origin: it puts the page's top-left corner off the paper the
    plot was aimed at, and the bed's own near edge is only an assumption
    anyway (the AxiDraw has no home switches, so "0" is wherever the carriage
    happened to sit at startup, not a place the machine knows) — so it's the
    user's call to make, not ours to refuse.

    Deliberately doesn't also check the next ready job's artwork bounds the
    way nudge_origin does: this is a free physical-alignment tool (walking
    the pen to a mark on the actual paper), and most designs are plotted
    edge-to-edge, leaving zero slack for *any* jog — checking content bounds
    here would make the tool unusable for exactly the common case it exists
    for. _run_job's pre-flight check is the real backstop: it catches a
    leftover jog that's actually a problem right before a plot starts,
    with a precise correction and a one-click fix in the UI."""
    with _worker_lock:
        _claim_idle_machine()
        x, y = state.manual_origin_offset()
        new_x, new_y = x + dx_mm, y + dy_mm

        # The offset is measured from the declared origin (see set_origin),
        # which isn't necessarily the machine's own corner — so the far-edge
        # guard has to add the two back together to get a real bed coordinate.
        # Past that edge the carriage hits its own end stops, so it's a hard
        # refusal, as is a move too long for the driver to execute in full
        # (see _move_fits_bed).
        base_x, base_y = state.origin_base()
        bed_w_mm, bed_h_mm = machine_bounds_mm()
        if (base_x + new_x > bed_w_mm or base_y + new_y > bed_h_mm
                or not _move_fits_bed(dx_mm, dy_mm)):
            raise RuntimeError("Jog rejected: would move past the machine bed edge.")
        if (new_x < 0 or new_y < 0) and not confirm_below_origin:
            raise RuntimeError("Jog would go above or left of the origin")

        # Move first, record second. The offset is what the readout shows,
        # what the pre-flight check measures and what Return to Origin walks
        # back, so recording a move the plotter refused (powered off, cable
        # pulled — _jog_carriage raises) would point all three at a place the
        # carriage never went, and Return to Origin would then drive it that
        # far away from the real origin. manual_jog_home has always been this
        # way round; these two were not.
        _jog_carriage(dx_mm, dy_mm)
        state.set_manual_origin_offset(new_x, new_y)


def set_origin() -> None:
    """Declare wherever the carriage currently sits to be the page's top-left
    corner. Nothing moves: the accumulated manual jog is folded into the
    origin base and the offset resets to zero, so from here on the readout,
    the preview overlay and _run_job's pre-flight check all measure from this
    spot — a plot started now puts the design's own (0, 0) right under the pen
    instead of treating the jog as a shift away from the page corner.

    Idle-only for the same reason manual_jog is: mid-run the physical origin
    is already baked into the plot that's underway, and moving the page corner
    out from under it would desynchronise the remaining stages. Touches no
    hardware, so unlike manual_jog it has nothing to guard against — the
    carriage is where it already was, and the offset it leaves behind (zero)
    is trivially inside the bed."""
    with _worker_lock:
        _claim_idle_machine()
        x, y = state.manual_origin_offset()
        base_x, base_y = state.origin_base()
        state.set_origin_base(base_x + x, base_y + y)
        state.set_manual_origin_offset(0.0, 0.0)


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
    — serve it whenever it exists.
    """
    src = _uploads() / f"{job['svg_id']}.svg"
    opt_path = src.with_name(f"{job['svg_id']}.opt.svg")
    if job.get("optimize_mode", "beginner") == "expert":
        return opt_path if opt_path.exists() else src
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
            or not job.get("optimize_svg"):
        return _effective_svg_path(job) if job is not None else src_path

    opt_path = src_path.with_name(f"{job['svg_id']}.opt.svg")
    cache_key = _optimize_cache_key(job)
    if opt_path.exists() and job.get("optimized_with_key") == cache_key:
        return opt_path

    state.update_job(job_id,
                     status="awaiting_optimize",
                     started_at=time.time(),
                     plotting_started_at=None,
                     error=None,
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
        state.update_job(job_id, status="failed",
                         error=f"Optimization failed: {err or 'unknown error'}")
        return None

    state.update_job(job_id, optimized_with_key=cache_key)
    return opt_path


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

    # Pre-flight check: refuse to touch hardware if a leftover manual jog
    # (see manual_jog) pushes the artwork off the page. AxiDraw has no
    # hardware home switches, so wherever the carriage physically sits when a
    # plot starts becomes its new logical (0, 0) — an un-homed manual jog is
    # a real, invisible-in-preview physical origin shift, unlike the job's
    # own offset/scale/rotation/fit-to-page, which the preview already shows
    # and which pyaxidraw clips at plot time same as always if it runs past
    # the edge — that's a deliberate crop, not blocked here (see
    # _delta_correction_mm).
    manual_x, manual_y = state.manual_origin_offset()
    correction = _delta_correction_mm(job, svg_path, manual_x, manual_y)
    if correction is not None:
        cx, cy = correction
        state.update_job(job_id, status="failed",
                         error=f"A leftover manual jog puts the artwork off the page. "
                               f"Nudge back by ({cx:+.1f}, {cy:+.1f}) mm to bring it "
                               "onto the page, then plot again.",
                         jog_hint_dx_mm=cx, jog_hint_dy_mm=cy)
        return

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
                         error=None,
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
                         error=None,
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
            state.update_job(
                job_id, status="failed", resume_path=None,
                error="This drawing is too complex for this machine to plan. "
                      "Nothing was sent to the plotter. See the card for which "
                      "vpype setting would bring it into range.")
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
            state.update_job(
                job_id, status="failed", resume_path=None,
                plan_status="too_complex", plan_error=str(exc),
                error="This drawing is too complex for this machine to plan. "
                      "Nothing was sent to the plotter. See the card for which "
                      "vpype setting would bring it into range.")
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

    # Origin nudge is session-only: whatever the pen-change pauses in this run
    # accumulated is undone as soon as the run ends (completed/cancelled/
    # failed) so the next run starts from the job's own saved offset again.
    try:
        _run_staged_loop_impl(job_id, svg_path, first_mode)
    finally:
        _undo_origin_nudge()
        if camera.is_recording_job(job_id):
            camera.stop_recording()


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
            except Exception:
                log.exception("could not render stage %s of job %s", i, job_id)
                state.update_job(
                    job_id, status="failed", resume_path=None,
                    error="Could not prepare this layer for plotting — the SVG "
                          "may be malformed. Nothing was sent to the plotter.")
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
            state.update_job(job_id, status="failed",
                             error="Plotter not ready. Wait a moment after power-on and try again.")
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
                state.update_job(job_id, status="failed", resume_path=None,
                                 error="Could not save plot progress (disk full or write "
                                       "error). The plotter has stopped physically; home it "
                                       "manually before starting another job.")
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
            state.update_job(job_id, status="failed", error=_format_stopped(stopped))
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
                state.update_job(job_id, status="awaiting_pen_change")
                if camera.is_recording_job(job_id):
                    camera.pause_recording()
                while True:
                    _continue_event.wait()
                    _continue_event.clear()
                    if _cancel_flag.is_set():
                        _cancel_flag.clear()
                        state.update_job(job_id, status="cancelled")
                        return
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
