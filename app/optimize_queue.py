"""Single-worker FIFO queue for SVG optimization (vpype).

Two entry points:

* ``enqueue_for_upload(svg_id)`` — fire-and-forget, called from ``/upload``.
  Pre-optimizes the SVG using the current config defaults so plot start is
  faster. No-op if ``OPTIMIZE_SVG_DEFAULT`` is off.
* ``request_for_job(svg_id, settings, on_running, cancel_flag)`` — synchronous,
  called from the plot worker. Enqueues the job's optimize task (or joins an
  in-flight matching one) and blocks until it's done. The caller owns the job
  status; we just call back when our task starts running so the caller can
  flip ``awaiting_optimize`` → ``optimizing``.

Why a single shared queue rather than letting both run in parallel: vpype is
CPU-heavy and the device this runs on (a Pi) is small. Two simultaneous vpype
processes would just fight over the cores.

Caching: the cached output ``<svg_id>.opt.svg`` is keyed by the optimize
settings (linemerge / linesimplify / linesort / reloop / tolerance). The same
settings_key is recorded in ``state._svgs[svg_id]`` so a service restart can
tell whether the on-disk file still matches.
"""
import logging
import threading
from pathlib import Path
from typing import Callable

from lxml import etree

from . import config, state, svg_optimize, svg_utils, workload

log = logging.getLogger(__name__)


def settings_key(settings: dict) -> str:
    """Stable string key for an optimize-settings dict.

    Mirrors the cache key used per-job in plot_worker so a job whose
    ``optimized_with_key`` already matches the on-disk ``.opt.svg`` shortcuts
    around us entirely.
    """
    return "|".join([
        f"t={float(settings['tolerance_mm']):.4f}",
        f"lm={int(bool(settings['linemerge']))}",
        f"ls={int(bool(settings['linesimplify']))}",
        f"so={int(bool(settings['linesort']))}",
        f"rl={int(bool(settings['reloop']))}",
    ])


def grid_settings_key(settings: dict) -> str:
    """Cache key for the {svg_id}.grid.svg derivative.

    Built on top of ``settings_key`` because the grid pass tiles the *optimized*
    file — a change to any optimize toggle changes what gets tiled. The grid
    dict carries the copy count, the x/y spacing, the cutting-marks toggle and
    the sheet + margins (which set the cell size). The drawing's own dimensions
    are not in the key: a given svg_id has fixed content, so the arrangement is
    deterministic without them. The job's transform is not in the key either —
    it acts on the whole tiled sheet downstream, not on the tiled file. Nor is
    ``spacing_linked``: it only steers the card's two inputs, not the output.
    ``fit`` is in the key: page vs ink fit changes what gets tiled.
    """
    g = settings.get("grid")
    if not g:
        return f"{settings_key(settings)}|g=0"
    return (f"{settings_key(settings)}"
            f"|g={g['copies']}x{g['spacing_x_mm']:.3f}x{g['spacing_y_mm']:.3f}"
            f"x{int(g['cut_marks'])}x{g['fit']}"
            f"x{g['paper_w_mm']:.1f}x{g['paper_h_mm']:.1f}"
            f"x{g['ml']:.1f}x{g['mr']:.1f}x{g['mt']:.1f}x{g['mb']:.1f}")


def settings_from_config() -> dict:
    return {
        "tolerance_mm": float(config.OPTIMIZE_SVG_TOLERANCE_DEFAULT_MM),
        "linemerge": bool(config.OPTIMIZE_SVG_LINEMERGE_DEFAULT),
        "linesimplify": bool(config.OPTIMIZE_SVG_LINESIMPLIFY_DEFAULT),
        "linesort": bool(config.OPTIMIZE_SVG_LINESORT_DEFAULT),
        "reloop": bool(config.OPTIMIZE_SVG_RELOOP_DEFAULT),
        # Grid is inherently per-job — never run at upload / bootstrap time.
        "grid": None,
    }


def settings_from_job(job: dict) -> dict:
    grid = None
    if job.get("grid_enabled"):
        grid = {
            "copies": int(job.get("grid_copies", 4)),
            "spacing_x_mm": float(job.get("grid_spacing_x_mm", 0.0)),
            "spacing_y_mm": float(job.get("grid_spacing_y_mm", 0.0)),
            "cut_marks": bool(job.get("grid_cut_marks", False)),
            "fit": "ink" if job.get("grid_fit") == "ink" else "page",
            "paper_w_mm": float(job.get("paper_width_mm", 210.0)),
            "paper_h_mm": float(job.get("paper_height_mm", 297.0)),
            "ml": float(job.get("margin_left_mm", 0.0)),
            "mr": float(job.get("margin_right_mm", 0.0)),
            "mt": float(job.get("margin_top_mm", 0.0)),
            "mb": float(job.get("margin_bottom_mm", 0.0)),
        }
    if not job.get("optimize_svg"):
        # Grid alone is enough to run a task, and phase 1 read the four toggles
        # straight off the job record — so a job with Optimize SVG *off* and
        # Grid on got its geometry simplified anyway, and tiled the result. All
        # four off makes optimize_svg() take its copy-through no-op path, and
        # the grid tiles the drawing as uploaded. The tolerance is canonicalised
        # with them so dragging a slider that now changes nothing cannot
        # invalidate the key and spend a vpype run re-tiling the same geometry.
        return {"tolerance_mm": 0.10, "linemerge": False, "linesimplify": False,
                "linesort": False, "reloop": False, "grid": grid}
    return {
        "tolerance_mm": float(job.get("optimize_svg_tolerance_mm", 0.10)),
        "linemerge": bool(job.get("optimize_svg_linemerge", True)),
        "linesimplify": bool(job.get("optimize_svg_linesimplify", True)),
        "linesort": bool(job.get("optimize_svg_linesort", True)),
        "reloop": bool(job.get("optimize_svg_reloop", True)),
        "grid": grid,
    }


# Internal task representation --------------------------------------------

class _Task:
    __slots__ = ("svg_id", "settings", "settings_key", "grid", "grid_key", "kind",
                 "started", "done", "ok", "error", "cancelled", "waiters")

    def __init__(self, svg_id: str, settings: dict, sk: str, kind: str) -> None:
        self.svg_id = svg_id
        self.settings = settings
        self.settings_key = sk
        # Phase 2: tile the optimized file into a grid. None when the job has
        # grid disabled. grid_key covers both phases (grid of optimized).
        self.grid = settings.get("grid")
        self.grid_key = grid_settings_key(settings)
        self.kind = kind  # "upload" or "job"
        self.started = threading.Event()
        self.done = threading.Event()
        self.ok = False
        self.error: str | None = None
        self.cancelled = False
        # How many request_for_job callers are blocked on this task. Tasks are
        # deduplicated (see _enqueue), so the plot worker and the plan queue
        # can be waiting on the *same* task — one of them giving up must not
        # kill the vpype run the other still needs.
        self.waiters = 0


_pending: list[_Task] = []
_inflight: _Task | None = None
_lock = threading.Lock()
_wakeup = threading.Event()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()
_shutdown = threading.Event()

_UPLOAD_DIR_LAZY: Path | None = None


def _uploads() -> Path:
    """Defer the import of UPLOAD_DIR until first call to dodge a startup
    circular: main → optimize_queue → main."""
    global _UPLOAD_DIR_LAZY
    if _UPLOAD_DIR_LAZY is None:
        from .main import UPLOAD_DIR
        _UPLOAD_DIR_LAZY = UPLOAD_DIR
    return _UPLOAD_DIR_LAZY


# Lifecycle ---------------------------------------------------------------

def start() -> None:
    """Start the optimize worker thread (idempotent)."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _shutdown.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="optimize-queue")
        _thread.start()


def shutdown(timeout_s: float = 5.0) -> None:
    """Ask the worker to drain and exit. Kills any in-flight subprocess."""
    _shutdown.set()
    _wakeup.set()
    with _lock:
        if _inflight is not None:
            _inflight.cancelled = True
            svg_optimize.cancel_current()
    t = _thread
    if t is not None and t.is_alive() and threading.current_thread() is not t:
        t.join(timeout=timeout_s)


def bootstrap_from_disk() -> None:
    """Re-enqueue any uploaded SVG that has no usable cached optimization.

    Called once at app startup, after state.init. Scans the uploads dir; for
    every ``<svg_id>.svg``:
      - if there's a fresh ``.opt.svg`` matching current defaults, mark ready;
      - otherwise, if the global default is on, enqueue with current defaults.
    """
    if not config.OPTIMIZE_SVG_DEFAULT:
        return
    uploads = _uploads()
    if not uploads.exists():
        return
    defaults = settings_from_config()
    sk = settings_key(defaults)
    for svg in uploads.iterdir():
        if not svg.is_file() or svg.suffix != ".svg":
            continue
        # Skip derivative files (.opt, .preview, .filt, .resume, .combined.filt, .s0.filt, ...).
        # The original upload's stem is exactly the svg_id (8 hex chars, no dots).
        if "." in svg.stem:
            continue
        svg_id = svg.stem
        opt = svg.with_name(f"{svg_id}.opt.svg")
        existing = state.get_svg_status(svg_id)
        if opt.exists() and existing and existing.get("settings_key") == sk \
                and existing.get("status") == "ready":
            continue
        # Fire-and-forget; the worker will handle it in order.
        _enqueue(svg_id, defaults, kind="upload")


# Public API --------------------------------------------------------------

def enqueue_for_upload(svg_id: str) -> None:
    """Pre-optimize using current defaults. No-op if the global toggle is off."""
    if not config.OPTIMIZE_SVG_DEFAULT:
        return
    _enqueue(svg_id, settings_from_config(), kind="upload")


def enqueue_for_job(job: dict) -> None:
    """Pre-optimize using a freshly-created job's actual settings.

    Fire-and-forget: the plot worker still calls ``request_for_job`` later and
    will dedup onto our task if it's still inflight, or hit the cache if it
    finished. This closes the common "API client sends custom settings; the
    upload-time pre-opt with config defaults misses cache" case.

    No-op if the job has both optimize_svg and grid disabled, or is in expert
    mode — expert mode's vpype run is triggered explicitly (see
    optimize_expert_queue), not automatically here.
    """
    if job.get("optimize_mode", "beginner") != "beginner":
        return
    if not (job.get("optimize_svg") or job.get("grid_enabled")):
        return
    _enqueue(job["svg_id"], settings_from_job(job), kind="job")


def grid_is_current(job: dict) -> bool:
    """Is ``{svg_id}.grid.svg`` on disk *and* built from this job's grid
    settings as they stand now?

    Existence alone is not enough. The tiled file outlives the settings that
    produced it, so between a settings change and the rebuild landing every read
    path — the preview, /jobs/{id}/svg, svg-meta, export, placement and the plot
    itself — would answer for the previous arrangement while the UI shows the
    new one.
    """
    return _grid_is_fresh(job["svg_id"],
                          grid_settings_key(settings_from_job(job)))


def request_for_job(svg_id: str,
                    settings: dict,
                    on_running: Callable[[], None] | None,
                    cancel_flag: threading.Event) -> tuple[bool, str | None]:
    """Block until the optimize for ``svg_id`` with these settings is done.

    Returns ``(ok, error)``. Honours ``cancel_flag``: if set while pending the
    task is dropped; if set while running the subprocess is killed.
    ``on_running`` fires the moment our task actually starts (so the caller
    can flip the job from ``awaiting_optimize`` → ``optimizing``).
    """
    task = _enqueue(svg_id, settings, kind="job")
    fired = False
    with _lock:
        task.waiters += 1
    try:
        while not task.done.is_set():
            if cancel_flag.is_set():
                _cancel_task(task)
                break
            if task.started.is_set() and not fired and on_running is not None:
                on_running()
                fired = True
            # Short timeout so we re-check cancel_flag promptly.
            task.done.wait(timeout=0.1)
    finally:
        with _lock:
            task.waiters -= 1

    # If the task finished before we noticed `started` (cache fast-path), still
    # fire the callback so the caller doesn't see the status flicker past
    # awaiting_optimize.
    if not fired and task.ok and on_running is not None:
        on_running()

    if task.cancelled:
        return False, "cancelled"
    return task.ok, task.error


def cancel(svg_id: str) -> None:
    """Drop any pending tasks for ``svg_id`` and kill an in-flight one.

    Called when an SVG is deleted: there's no point optimizing a file the user
    just removed, and a stale ``.opt.svg`` would be cleaned up by
    ``delete_svg_files`` anyway.
    """
    woken_a_waiter = False
    with _lock:
        global _pending
        kept = []
        for t in _pending:
            if t.svg_id == svg_id:
                t.cancelled = True
                t.done.set()
                woken_a_waiter = True
            else:
                kept.append(t)
        _pending = kept
        if _inflight is not None and _inflight.svg_id == svg_id:
            _inflight.cancelled = True
            svg_optimize.cancel_current()
            woken_a_waiter = True
    state.clear_svg_status(svg_id)
    state.clear_svg_status(_grid_status_id(svg_id))
    if woken_a_waiter:
        _wakeup.set()


# Internals ---------------------------------------------------------------

def _opt_path(svg_id: str) -> Path:
    return _uploads() / f"{svg_id}.opt.svg"


def _grid_path(svg_id: str) -> Path:
    return _uploads() / f"{svg_id}.grid.svg"


def _src_path(svg_id: str) -> Path:
    return _uploads() / f"{svg_id}.svg"


def _grid_status_id(svg_id: str) -> str:
    """Synthetic state.svgs key for the grid derivative's status, so the
    frontend can tell 'tiled file is being built' from 'tiled file is ready'
    the same way it does for the optimized file."""
    return f"{svg_id}:grid"


def _grid_is_fresh(svg_id: str, grid_key: str) -> bool:
    st = state.get_svg_status(_grid_status_id(svg_id))
    return bool(_grid_path(svg_id).exists() and st
               and st.get("settings_key") == grid_key
               and st.get("status") == "ready")


def _enqueue(svg_id: str, settings: dict, kind: str) -> _Task:
    """Create-or-join. Always returns a task whose ``done`` event will fire."""
    sk = settings_key(settings)
    gk = grid_settings_key(settings)
    want_grid = settings.get("grid") is not None
    if not want_grid and kind == "job":
        # The job just turned Grid off. Nothing else ever clears this entry, so
        # it would stay "ready" in state.svgs for the life of the drawing, ride
        # every WebSocket frame and come back at startup. Scoped to job-kind
        # enqueues because enqueue_for_upload and bootstrap_from_disk pass
        # grid=None for every SVG on disk, and would wipe live grid statuses.
        state.clear_svg_status(_grid_status_id(svg_id))

    # Fast path: both derivatives exist and their recorded keys still match → done.
    opt = _opt_path(svg_id)
    existing = state.get_svg_status(svg_id)
    opt_fresh = (opt.exists() and existing and existing.get("settings_key") == sk
                 and existing.get("status") == "ready")
    if opt_fresh and (not want_grid or _grid_is_fresh(svg_id, gk)):
        t = _Task(svg_id, settings, sk, kind)
        t.started.set()
        t.ok = True
        t.done.set()
        return t

    with _lock:
        # Dedup against in-flight task with matching key (both phases).
        if _inflight is not None and _inflight.svg_id == svg_id \
                and _inflight.settings_key == sk and _inflight.grid_key == gk \
                and not _inflight.cancelled:
            return _inflight
        # Dedup against an already-pending matching task.
        for t in _pending:
            if t.svg_id == svg_id and t.settings_key == sk \
                    and t.grid_key == gk and not t.cancelled:
                return t

        # When a job-task arrives, drop any older pending upload-tasks for the
        # same SVG with different settings — their result would just be
        # overwritten. (We don't kill an in-flight upload-task with different
        # settings; let it finish, then we run.)
        if kind == "job":
            kept: list[_Task] = []
            for t in _pending:
                if t.svg_id == svg_id and t.settings_key != sk and t.kind == "upload":
                    t.cancelled = True
                    t.done.set()
                else:
                    kept.append(t)
            _pending[:] = kept

        new_task = _Task(svg_id, settings, sk, kind)
        _pending.append(new_task)
        _wakeup.set()

    # Don't downgrade an in-flight "optimizing" entry — another task for this
    # SVG is currently running vpype, and the UI is more useful showing that
    # than a generic "pending". The worker writes the new settings_key when
    # it picks our task up.
    existing = state.get_svg_status(svg_id)
    if not (existing and existing.get("status") == "optimizing"):
        state.set_svg_status(svg_id, "pending", settings_key=sk)
    if want_grid:
        gstat = state.get_svg_status(_grid_status_id(svg_id))
        if not (gstat and gstat.get("status") == "tiling"):
            state.set_svg_status(_grid_status_id(svg_id), "pending", settings_key=gk)
    return new_task


def _cancel_task(task: _Task) -> None:
    """Remove ``task`` if pending, or kill it if it's running.

    No-op when another caller is still waiting on the same (deduplicated)
    task — we're only withdrawing this caller's interest, not everyone's.
    """
    drop_status = False
    with _lock:
        if task.waiters > 1:
            return
        if task in _pending:
            _pending.remove(task)
            task.cancelled = True
            task.done.set()
            # If this was the only pending/inflight task for the SVG, the
            # "pending" entry the task left in state.svgs is now stale and
            # would otherwise stick in the UI until the next event.
            drop_status = (
                (_inflight is None or _inflight.svg_id != task.svg_id) and
                not any(t.svg_id == task.svg_id for t in _pending)
            )
        elif _inflight is task:
            task.cancelled = True
            svg_optimize.cancel_current()
            # The worker loop's finally clause will set task.done; _process
            # also clears state on cancellation.
    if drop_status:
        state.clear_svg_status(task.svg_id)


def _loop() -> None:
    global _inflight
    # Background work yields to the plotter and the event loop; see
    # app/workload.py for why that also protects what lands on paper.
    workload.deprioritize()
    while True:
        if _shutdown.is_set():
            return
        with _lock:
            task = _pending.pop(0) if _pending else None
            if task is not None:
                _inflight = task
        if task is None:
            _wakeup.wait()
            _wakeup.clear()
            continue

        try:
            _process(task)
        except Exception:
            log.exception("optimize_queue: unexpected error processing %s", task.svg_id)
            task.ok = False
            task.error = "internal error"
        finally:
            with _lock:
                _inflight = None
            task.started.set()  # idempotent — guarantees waiters unblock
            task.done.set()


def _process(task: _Task) -> None:
    try:
        _process_phases(task)
    finally:
        _settle_grid_status(task)


def _settle_grid_status(task: _Task) -> None:
    """Leave no ``{svg_id}:grid`` entry sitting at pending/tiling.

    Every failure and cancellation exit from ``_process_phases`` returns without
    reaching phase 2, and so without touching the entry ``_enqueue`` set to
    "pending". app.js reads anything that is neither ready nor failed as still
    building: the card's preview never reloads and its pill reads "waiting to
    build grid…" indefinitely. The entry persists to state.json too, so a
    restart brings the stuck pill back with it.
    """
    if task.grid is None:
        return
    gsid = _grid_status_id(task.svg_id)
    st = state.get_svg_status(gsid)
    if not st or st.get("status") not in ("pending", "tiling"):
        return
    if task.cancelled or not _src_path(task.svg_id).exists():
        # Nothing to report against: the work was withdrawn, or the drawing it
        # belonged to is gone (whose optimize entry _process_phases just cleared
        # for the same reason).
        state.clear_svg_status(gsid)
    else:
        state.set_svg_status(gsid, "failed", settings_key=task.grid_key,
                             error=task.error or "internal error")


def _process_phases(task: _Task) -> None:
    if task.cancelled:
        return
    src = _src_path(task.svg_id)
    if not src.exists():
        # File was deleted between enqueue and dispatch.
        task.ok = False
        task.error = "source SVG missing"
        state.clear_svg_status(task.svg_id)
        return

    # Resolve inherit + expand point-sized geometry before vpype optimizes /
    # tiles: normalize_layer_structure does it at upload, this covers files
    # uploaded before that landed (see svg_utils.prepare_for_vpype).
    svg_utils.prepare_for_vpype(src)

    state.set_svg_status(task.svg_id, "optimizing", settings_key=task.settings_key)
    task.started.set()

    opt = _opt_path(task.svg_id)
    opt_kwargs = {k: v for k, v in task.settings.items() if k != "grid"}
    try:
        # One heavy job at a time across all three queues (app/workload.py).
        with workload.heavy("optimize"):
            svg_optimize.optimize_svg(src, opt, **opt_kwargs)
    except svg_optimize.OptimizeError as e:
        # Clean up a partial file if vpype died mid-write.
        if opt.exists():
            try:
                opt.unlink()
            except OSError:
                pass
        if task.cancelled:
            task.ok = False
            task.error = "cancelled"
            state.clear_svg_status(task.svg_id)
            return
        task.ok = False
        task.error = str(e)
        state.set_svg_status(task.svg_id, "failed",
                             settings_key=task.settings_key, error=str(e))
        return

    if task.cancelled:
        # Cancelled mid-write but vpype somehow returned 0 anyway — discard the
        # output to avoid leaving a half-meaningful file behind.
        if opt.exists():
            try:
                opt.unlink()
            except OSError:
                pass
        task.ok = False
        task.error = "cancelled"
        state.clear_svg_status(task.svg_id)
        return

    # vpype drops any layer it found nothing plottable in, which would shift
    # every later layer's index and make the job plot the wrong artwork.
    # Restore the upload's layer sequence before anyone reads the result.
    try:
        svg_utils.reconcile_layers(src, opt)
    except Exception:
        log.exception("optimize_queue: layer reconcile failed for %s", task.svg_id)
        opt.unlink(missing_ok=True)
        task.ok = False
        task.error = "could not re-align layers after optimization"
        state.set_svg_status(task.svg_id, "failed",
                             settings_key=task.settings_key, error=task.error)
        return

    state.set_svg_status(task.svg_id, "ready", settings_key=task.settings_key)

    # Phase 2 — tile the optimized file into a grid (see app/svg_optimize.grid_svg).
    # A grid failure fails the whole task. Falling back to the un-tiled file
    # would put one copy on the sheet and report success, which on a plotter
    # costs the user the sheet and the pen time before they can see it went
    # wrong; the plot worker turns a failed task into a failed job instead.
    if task.grid is not None and not task.cancelled:
        if not _run_grid_phase(task, opt):
            task.ok = False
            return

    task.ok = True


def _run_grid_phase(task: _Task, pre_grid: Path) -> bool:
    """Tile ``pre_grid`` into ``{svg_id}.grid.svg``. Returns whether it worked;
    on failure ``task.error`` says why, in the words the job's card will show."""
    g = task.grid
    grid_path = _grid_path(task.svg_id)
    gsid = _grid_status_id(task.svg_id)
    try:
        root = etree.parse(str(pre_grid)).getroot()
        content_w, content_h = svg_utils.svg_size_mm(root)
        # The cells are carved out of the margin box, so that — not the whole
        # sheet — is the area the columns x rows split has to make the most of.
        # Deciding on the sheet and then filling the margin box picks the wrong
        # split whenever the two have different aspects: A4 portrait with 100mm
        # top and bottom margins is a landscape strip, and 2 copies want 2x1.
        avail_w = max(1.0, g["paper_w_mm"] - g["ml"] - g["mr"])
        avail_h = max(1.0, g["paper_h_mm"] - g["mt"] - g["mb"])
        cols, rows, rotate = svg_optimize.arrangement(
            g["copies"], avail_w, avail_h, content_w, content_h)
        # Spacing pads every side of every copy. Clamp each axis against the
        # spacing-free ("natural") cell so the cap is stable, then take 2*spacing
        # out of that cell — one spacing per side. The pitch stays the natural
        # cell, so the tiled sheet is still exactly the margin box: the spacing
        # shows up as a gap between copies (2*spacing) and an inset at the edge.
        nat_cell_w, nat_cell_h = avail_w / cols, avail_h / rows
        sx = svg_optimize.clamp_spacing_mm(g["spacing_x_mm"], nat_cell_w)
        sy = svg_optimize.clamp_spacing_mm(g["spacing_y_mm"], nat_cell_h)
        cell_w = nat_cell_w - 2.0 * sx
        cell_h = nat_cell_h - 2.0 * sy
        state.set_svg_status(gsid, "tiling", settings_key=task.grid_key)
        with workload.heavy("grid"):
            svg_optimize.grid_svg(pre_grid, grid_path, cols, rows,
                                  cell_w, cell_h, sx, sy, rotate_copies=rotate,
                                  fit=g["fit"])
        svg_utils.reconcile_layers(_src_path(task.svg_id), grid_path)
        svg_utils.force_round_caps(grid_path)
        if g["cut_marks"]:
            # After the reconcile, never before: the marks layer is not one of
            # the upload's, and matching it against a source label would move
            # it into an artwork layer's position (see svg_utils.add_cut_marks).
            svg_utils.add_cut_marks(grid_path, cols, rows, cell_w, cell_h, sx, sy)
    except Exception as e:  # noqa: BLE001 — any failure here is the job's
        grid_path.unlink(missing_ok=True)
        if task.cancelled:
            task.error = "cancelled"
            state.clear_svg_status(gsid)
            return False
        log.warning("optimize_queue: grid phase failed for %s: %s", task.svg_id, e)
        task.error = f"could not tile the sheet: {e}"
        state.set_svg_status(gsid, "failed", settings_key=task.grid_key, error=str(e))
        return False
    if task.cancelled:
        task.error = "cancelled"
        grid_path.unlink(missing_ok=True)
        state.clear_svg_status(gsid)
        return False
    state.set_svg_status(gsid, "ready", settings_key=task.grid_key)
    return True
