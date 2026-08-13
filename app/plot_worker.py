import hashlib
import json
import logging
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

from plotink import ebb_motion, ebb_serial
from pyaxidraw import axidraw

from . import camera, config, notify, optimize_queue, state, svg_optimize, svg_utils

log = logging.getLogger(__name__)

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
        "sd": job["speed_pendown"],
        "su": job["speed_penup"],
        "acc": job["acceleration"],
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
    last = (None, None, None)
    while not _stop_position.is_set():
        ad = _current_ad
        if ad is not None and hasattr(ad, "pen") and hasattr(ad.pen, "phys"):
            try:
                x_in = ad.pen.phys.xpos
                y_in = ad.pen.phys.ypos
                z_up = getattr(ad.pen.phys, "z_up", None)
                if x_in is not None and y_in is not None:
                    pen_down = (z_up is False)
                    key = (x_in, y_in, pen_down)
                    if key != last:
                        state.emit_position(x_in * 25.4, y_in * 25.4, pen_down)
                        last = key
                if z_up is True and state.pause_at_pen_up_pending():
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

# Maps PLOTTER_MODEL to the axidrawinternal.axidraw_conf attribute pair its
# driver reads for real travel bounds (see AxiDraw.update_options()). Models
# not listed (1, 8, and anything unrecognized) fall back to x/y_travel_default.
_MODEL_TRAVEL_PARAMS = {
    2: ("x_travel_V3A3", "y_travel_V3A3"),
    3: ("x_travel_V3XLX", "y_travel_V3XLX"),
    4: ("x_travel_MiniKit", "y_travel_MiniKit"),
    5: ("x_travel_SEA1", "y_travel_SEA1"),
    6: ("x_travel_SEA2", "y_travel_SEA2"),
    7: ("x_travel_V3B6", "y_travel_V3B6"),
}


def _apply_custom_bed_size(ad: axidraw.AxiDraw) -> None:
    """Make the custom bed size (config.MACHINE_*_MM) a real travel-bounds
    limit, not just a UI paper-fit warning: override the driver's own
    per-model params.x_travel_*/y_travel_* (read by AxiDraw.update_options()
    to build self.bounds, which clips out-of-bounds pen-down moves).

    Only ever shrinks the working area, never grows it past the model's real
    hardware travel — a custom size larger than the actual machine would let
    the carriage be driven into its physical end stops.
    """
    if not config.MACHINE_CUSTOM_ENABLED:
        return
    x_attr, y_attr = _MODEL_TRAVEL_PARAMS.get(
        config.PLOTTER_MODEL, ("x_travel_default", "y_travel_default"))
    hw_x_in = getattr(ad.params, x_attr)
    hw_y_in = getattr(ad.params, y_attr)
    setattr(ad.params, x_attr, min(config.MACHINE_WIDTH_MM / 25.4, hw_x_in))
    setattr(ad.params, y_attr, min(config.MACHINE_HEIGHT_MM / 25.4, hw_y_in))


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
        _apply_custom_bed_size(ad)
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
    runner = Path(__file__).parent / "preview_runner.py"
    args = [
        sys.executable,
        str(runner),
        str(preview_svg_path),
        str(config.PLOTTER_MODEL),
        str(job["speed_pendown"]),
        str(job["speed_penup"]),
        str(job["acceleration"]),
    ]
    with _preview_lock:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _preview_proc = proc
        watcher: threading.Thread | None = None
        if cancel_event is not None:
            def _watch() -> None:
                # Poll: wake on either cancel_event being set or the proc
                # finishing on its own. 200ms is fine — preview takes seconds.
                while not cancel_event.is_set():
                    if proc.poll() is not None:
                        return
                    cancel_event.wait(timeout=0.2)
                if proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
            watcher = threading.Thread(target=_watch, daemon=True)
            watcher.start()
        try:
            stdout, stderr = proc.communicate()
        finally:
            _preview_proc = None

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

def start_queue() -> None:
    """Kick off the worker if it isn't already running."""
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _cancel_flag.clear()
        _continue_event.clear()
        t = threading.Thread(target=_queue_loop, daemon=True)
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

    # Post-restart scenario: no worker thread exists. Start the queue loop —
    # its paused-first dispatch picks up this job and routes it to _resume_job,
    # which skips re-planning and jumps into the staged loop.
    start_queue()


def continue_next() -> None:
    """Continue: either next stage (pen-change pause) or next job (awaiting_next_job)."""
    if state.snapshot()["awaiting_next_job"]:
        state.set_awaiting_next_job(False)
        _continue_event.set()
        return
    job = state.active_job()
    if job and job["status"] == "awaiting_pen_change":
        _continue_event.set()
        return
    raise RuntimeError("Nothing to continue")


def nudge_origin(dx_mm: float, dy_mm: float) -> None:
    """Shift the origin of the remaining (not-yet-plotted) stages by a small
    delta, to compensate for paper drift between layers during a pen-change
    pause. Session-only: added on top of the job's own transform offset when
    rendering each remaining stage (see _run_staged_loop /
    _run_calibration_phase), never written back to the job record, and reset
    at the start of the next run.

    Also physically jogs the carriage by the same delta (pen-up, relative),
    the same way manual_pen/manual_motors do, so the user sees/feels the
    correction against the paper before continuing."""
    job = state.active_job()
    if job is None or job["status"] != "awaiting_pen_change":
        raise RuntimeError("Origin nudge only available at a pen-change pause")
    if _current_ad is not None:
        raise RuntimeError("Plotter busy")
    x, y = state.origin_nudge()
    bound = max(job.get("paper_width_mm", 0), job.get("paper_height_mm", 0), 1.0)
    x = max(-bound, min(bound, x + dx_mm))
    y = max(-bound, min(bound, y + dy_mm))
    state.set_origin_nudge(x, y)

    ad = axidraw.AxiDraw()
    ad.interactive()
    ad.options.model = config.PLOTTER_MODEL
    ad.options.units = 2  # millimeters
    ad.options.pen_pos_up, ad.options.pen_pos_down = _active_pen_heights()
    if not ad.connect():
        raise RuntimeError("Could not connect to the plotter. Check that it is powered on and plugged in.")
    try:
        # connect() resets the AxiDraw driver's internal "turtle" position
        # tracker to (0, 0), which coincides with its software travel-bounds
        # minimum. ad.move() clips any relative move whose target falls
        # outside those bounds, so a move away from (0, 0) is accepted while
        # a move toward negative coordinates is silently clipped to zero -
        # regardless of where the carriage physically already is. Re-center
        # the turtle first so a nudge in either direction has room to move.
        ad.pen.turtle.xpos = (ad.bounds[0][0] + ad.bounds[1][0]) / 2
        ad.pen.turtle.ypos = (ad.bounds[0][1] + ad.bounds[1][1]) / 2
        ad.move(dx_mm, dy_mm)
    finally:
        ad.disconnect()


def manual_jog(dx_mm: float, dy_mm: float) -> None:
    """Physically move the pen carriage by a small relative amount (pen-up),
    for aligning it to the paper before a plot starts. Idle-only — unlike
    nudge_origin, which corrects an active job's remaining stages mid-plot,
    this has no job to apply to; it just walks the carriage and accumulates
    the net displacement in session state so set_manual_origin can capture it
    as the default offset for jobs created from now on."""
    if state.snapshot()["status"] != "idle":
        raise RuntimeError("Manual jog only available while idle")
    if _current_ad is not None:
        raise RuntimeError("Plotter busy")
    x, y = state.manual_origin_offset()
    x = max(-config.MACHINE_WIDTH_MM, min(config.MACHINE_WIDTH_MM, x + dx_mm))
    y = max(-config.MACHINE_HEIGHT_MM, min(config.MACHINE_HEIGHT_MM, y + dy_mm))
    state.set_manual_origin_offset(x, y)

    ad = axidraw.AxiDraw()
    ad.interactive()
    ad.options.model = config.PLOTTER_MODEL
    ad.options.units = 2  # millimeters
    ad.options.pen_pos_up, ad.options.pen_pos_down = _active_pen_heights()
    if not ad.connect():
        raise RuntimeError("Could not connect to the plotter. Check that it is powered on and plugged in.")
    try:
        # See nudge_origin: re-center the turtle so a move in either direction
        # has room, regardless of where the carriage physically already is.
        ad.pen.turtle.xpos = (ad.bounds[0][0] + ad.bounds[1][0]) / 2
        ad.pen.turtle.ypos = (ad.bounds[0][1] + ad.bounds[1][1]) / 2
        ad.move(dx_mm, dy_mm)
    finally:
        ad.disconnect()


def set_manual_origin() -> tuple[float, float]:
    """Capture the net displacement accumulated by manual_jog as the app's
    default origin offset, seeded onto jobs created from now on (see
    config.ORIGIN_OFFSET_X_MM_DEFAULT), then reset the running total so the
    next jog session starts from zero again."""
    if state.snapshot()["status"] != "idle":
        raise RuntimeError("Manual jog only available while idle")
    x, y = state.manual_origin_offset()
    config.update(origin_offset_x_mm_default=x, origin_offset_y_mm_default=y)
    state.set_manual_origin_offset(0.0, 0.0)
    return x, y


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
    snap = state.snapshot()
    if snap["awaiting_next_job"]:
        state.set_awaiting_next_job(False)
        _cancel_flag.set()
        _continue_event.set()
        return
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
    snap = state.snapshot()
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


# Queue loop ---------------------------------------------------------------

def _queue_loop() -> None:
    try:
        while True:
            if _cancel_flag.is_set():
                _cancel_flag.clear()
                return
            # Paused jobs take priority: they were interrupted mid-run by a
            # service restart and should be finished before any fresh queued
            # job starts.
            paused = state.next_paused_job()
            if paused is not None:
                state.set_active(paused["job_id"])
                _resume_job(paused["job_id"])
                state.set_active(None)
                if _cancel_flag.is_set():
                    _cancel_flag.clear()
                    continue
                # Fall through to between-jobs pause check below
                job = paused
            else:
                job = state.next_queued_job()
                if job is None:
                    return
                state.set_active(job["job_id"])
                _run_job(job["job_id"])
                state.set_active(None)

            if _cancel_flag.is_set():
                _cancel_flag.clear()
                continue  # loop; user may still have more queued

            # Between jobs: pause if the just-finished job asked for it and more queued
            if state.next_queued_job() is not None:
                last = state.get_job(job["job_id"])
                if last and last.get("pause_after_job", True):
                    state.set_awaiting_next_job(True)
                    _continue_event.wait()
                    _continue_event.clear()
                    state.set_awaiting_next_job(False)
                    if _cancel_flag.is_set():
                        _cancel_flag.clear()
                        continue
    except Exception:
        log.exception("queue loop crashed")
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
        f"ml={int(bool(job.get('optimize_svg_min_length', False)))}",
        f"mlm={float(job.get('optimize_svg_min_length_mm', 1.0)):.4f}",
    ])


def _effective_svg_path(job: dict) -> Path:
    """The SVG path to feed to filter_to_layers / transform_to_paper.

    If optimization is enabled and the cached .opt.svg is on disk, that's the
    one downstream uses. Otherwise we fall back to the raw upload.
    """
    src = _uploads() / f"{job['svg_id']}.svg"
    if not job.get("optimize_svg"):
        return src
    opt_path = src.with_name(f"{job['svg_id']}.opt.svg")
    return opt_path if opt_path.exists() else src


def _run_optimize_phase(job_id: str, src_path: Path, stages: list) -> Path | None:
    """Run vpype on ``src_path`` when the job has ``optimize_svg`` enabled.

    Return the SVG path the rest of the pipeline should use:
      - the cached/freshly-produced ``.opt.svg`` when optimization ran,
      - ``src_path`` unchanged when optimization is disabled,
      - ``None`` when optimization failed or the user cancelled — the caller
        should return immediately, the job has already been marked
        ``failed`` / ``cancelled``.

    Optimization is delegated to ``optimize_queue`` so we share the single
    vpype worker with upload-time pre-optimizations. While we wait our turn
    the job sits in ``awaiting_optimize``; once our task is picked up the
    queue calls back and we flip to ``optimizing``.
    """
    job = state.get_job(job_id)
    if job is None or not job.get("optimize_svg"):
        return src_path

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
        svg_utils.filter_to_layers(svg_path, cal_indices, filt)
        # Reflect any pending origin nudge so the calibration plot shows the
        # alignment the user is about to commit the next stage to.
        nudge_x, nudge_y = state.origin_nudge()
        svg_utils.transform_to_paper(
            filt, cal_svg,
            job["paper_width_mm"], job["paper_height_mm"],
            job["margin_top_mm"], job["margin_right_mm"],
            job["margin_bottom_mm"], job["margin_left_mm"],
            job["fit_content"],
            transform_scale=job.get("transform_scale", 1.0),
            transform_rotation_deg=job.get("transform_rotation_deg", 0.0),
            transform_offset_x_mm=job.get("transform_offset_x_mm", 0.0) + nudge_x,
            transform_offset_y_mm=job.get("transform_offset_y_mm", 0.0) + nudge_y,
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
        nudge_x, nudge_y = state.origin_nudge()
        svg_utils.transform_to_paper(
            src, scratch,
            job["paper_width_mm"], job["paper_height_mm"],
            job["margin_top_mm"], job["margin_right_mm"],
            job["margin_bottom_mm"], job["margin_left_mm"],
            job["fit_content"],
            transform_scale=job.get("transform_scale", 1.0),
            transform_rotation_deg=job.get("transform_rotation_deg", 0.0),
            transform_offset_x_mm=job.get("transform_offset_x_mm", 0.0) + nudge_x,
            transform_offset_y_mm=job.get("transform_offset_y_mm", 0.0) + nudge_y,
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
                         resume_path=None,
                         error=None,
                         plan_status="ready",
                         **cached_estimate)
    else:
        state.update_job(job_id,
                         stages=stages,
                         current_stage_index=0,
                         status="planning",
                         started_at=time.time(),
                         plotting_started_at=None,
                         resume_path=None,
                         error=None,
                         estimated_total_seconds=None,
                         distance_pendown_m=None,
                         distance_total_m=None,
                         pen_lifts=None)

        estimate = compute_preview(job, svg_path)

        if _cancel_flag.is_set():
            state.update_job(job_id, status="cancelled")
            return

        if estimate:
            state.update_job(job_id, plan_status="ready", **estimate)

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
    # accumulated, it's gone as soon as the run ends (completed/cancelled/
    # failed) so the next run starts from the job's own saved offset again.
    try:
        _run_staged_loop_impl(job_id, svg_path, first_mode)
    finally:
        state.set_origin_nudge(0.0, 0.0)
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
            filtered = svg_path.with_name(f"{job['svg_id']}.s{i}.filt.svg")
            svg_utils.filter_to_layers(svg_path, stage["layer_indices"], filtered)
            current_svg = svg_path.with_name(f"{job['svg_id']}.s{i}.svg")
            # A fine origin nudge dialed in at a pen-change pause (see
            # nudge_origin) shifts every subsequently-rendered stage on top of
            # the job's own offset — but never a res_plot resume, which
            # continues a partial SVG mid-stroke at its existing coordinates.
            nudge_x, nudge_y = state.origin_nudge()
            svg_utils.transform_to_paper(
                filtered, current_svg,
                job["paper_width_mm"], job["paper_height_mm"],
                job["margin_top_mm"], job["margin_right_mm"],
                job["margin_bottom_mm"], job["margin_left_mm"],
                job["fit_content"],
                transform_scale=job.get("transform_scale", 1.0),
                transform_rotation_deg=job.get("transform_rotation_deg", 0.0),
                transform_offset_x_mm=job.get("transform_offset_x_mm", 0.0) + nudge_x,
                transform_offset_y_mm=job.get("transform_offset_y_mm", 0.0) + nudge_y,
            )

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
