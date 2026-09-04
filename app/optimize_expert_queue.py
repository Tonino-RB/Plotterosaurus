"""Single-worker queue for expert-mode custom vpype command execution.

Unlike optimize_queue (beginner mode, run automatically at plot time) and
plan_queue (preview estimate, run eagerly on job create/edit), a task here is
only ever created by an explicit user click on "Execute" in the expert-mode
panel (POST /jobs/{id}/optimize-expert/execute). It shares the same
single-worker / one-heavy-job-at-a-time discipline (app/workload.py) so an
expert run never competes with a beginner-mode optimize, a plan estimate, or
the plotter itself for CPU — see that module's docstring for why.

Progress is exposed as a capped in-memory line buffer per job (the UI polls
GET /jobs/{id}/optimize-expert/status), since vpype gives no numeric percent
— only its own stdout/stderr as it runs. Execute stacks: each run applies the
three boxes on top of the current .opt.svg (the raw upload only for the very
first run), snapshotting the previous result to {svg_id}.opt.undo.{n}.svg so
undo_last() can step back one Execute at a time. optimize_expert_undo_depth on
the job record counts how many steps remain.
"""
import logging
import os
import shutil
import threading
from pathlib import Path

from . import config, plan_queue, state, svg_optimize, svg_utils, workload

log = logging.getLogger(__name__)

_MAX_LOG_LINES = 500


class _Task:
    __slots__ = ("job_id", "svg_id", "src_path", "box_texts",
                 "started", "done", "ok", "error", "cancel_event",
                 "log_lines", "log_lock")

    def __init__(self, job_id: str, svg_id: str, src_path: Path, box_texts: list[str]) -> None:
        self.job_id = job_id
        self.svg_id = svg_id
        self.src_path = src_path
        self.box_texts = box_texts
        self.started = threading.Event()
        self.done = threading.Event()
        self.ok = False
        self.error: str | None = None
        self.cancel_event = threading.Event()
        self.log_lines: list[str] = []
        self.log_lock = threading.Lock()

    def append_log(self, line: str) -> None:
        with self.log_lock:
            self.log_lines.append(line)
            if len(self.log_lines) > _MAX_LOG_LINES:
                del self.log_lines[: len(self.log_lines) - _MAX_LOG_LINES]

    def log_text(self) -> str:
        with self.log_lock:
            return "\n".join(self.log_lines)


_pending: list[_Task] = []
_inflight: _Task | None = None
_last: dict[str, _Task] = {}  # job_id -> most recently finished task
_lock = threading.Lock()
_wakeup = threading.Event()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()
_shutdown = threading.Event()


# Lifecycle -----------------------------------------------------------------

def start() -> None:
    """Start the expert-execute worker thread (idempotent)."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _shutdown.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="optimize-expert-queue")
        _thread.start()


def shutdown(timeout_s: float = 5.0) -> None:
    """Ask the worker to drain and exit. Kills any in-flight subprocess."""
    _shutdown.set()
    _wakeup.set()
    with _lock:
        if _inflight is not None:
            _inflight.cancel_event.set()
            svg_optimize.cancel_current()
    t = _thread
    if t is not None and t.is_alive() and threading.current_thread() is not t:
        t.join(timeout=timeout_s)


# Public API ------------------------------------------------------------

def enqueue_execute(job_id: str, svg_id: str, src_path: Path, box_texts: list[str]) -> _Task:
    """Queue an expert-mode Execute. Replaces any pending/inflight run for the
    same job — Execute is a deliberate one-off, not something to dedupe or
    stack up multiple requests of."""
    task = _Task(job_id, svg_id, src_path, box_texts)
    with _lock:
        global _pending
        _pending = [t for t in _pending if t.job_id != job_id]
        if _inflight is not None and _inflight.job_id == job_id:
            _inflight.cancel_event.set()
            svg_optimize.cancel_current()
        _pending.append(task)
        _wakeup.set()
    return task


def get_status(job_id: str) -> dict:
    """Current state for the UI's status poll.

    ``status`` is one of "idle" (never run, or forgotten), "running", "done",
    "error". ``log`` is the buffered output so far / from the last run.
    """
    with _lock:
        if _inflight is not None and _inflight.job_id == job_id:
            t = _inflight
            return {"status": "running", "log": t.log_text(), "error": None}
        for t in _pending:
            if t.job_id == job_id:
                return {"status": "running", "log": t.log_text(), "error": None}
        t = _last.get(job_id)
    if t is None:
        return {"status": "idle", "log": "", "error": None}
    return {"status": "done" if t.ok else "error", "log": t.log_text(), "error": t.error}


def cancel(job_id: str) -> None:
    """Drop a pending task or kill an inflight one for ``job_id``."""
    with _lock:
        global _pending
        kept: list[_Task] = []
        for t in _pending:
            if t.job_id == job_id:
                t.cancel_event.set()
                t.done.set()
            else:
                kept.append(t)
        _pending = kept
        if _inflight is not None and _inflight.job_id == job_id:
            _inflight.cancel_event.set()
            svg_optimize.cancel_current()


def forget(job_id: str) -> None:
    """Drop any record of ``job_id`` — called on job/SVG deletion so a stale
    status/log doesn't linger for an id that no longer exists."""
    cancel(job_id)
    with _lock:
        _last.pop(job_id, None)


def undo_last(job_id: str, svg_id: str, raw_path: Path) -> int | None:
    """Step ``{svg_id}.opt.svg`` back one Execute. Returns the new undo depth,
    or ``None`` when there is nothing to undo.

    Synchronous — pure file moves. The caller (main) has already refused this
    while a run for the job is in flight, and the single worker guarantees no
    Execute is touching these files concurrently.
    """
    job = state.get_job(job_id)
    if job is None:
        return None
    depth = int(job.get("optimize_expert_undo_depth", 0) or 0)
    if depth <= 0:
        return None

    folder = raw_path.parent
    opt_path = folder / f"{svg_id}.opt.svg"
    if depth == 1:
        opt_path.unlink(missing_ok=True)             # back to the raw upload
        new_depth = 0
    else:
        snap = _undo_snapshot(folder, svg_id, depth)
        if snap.exists():
            os.replace(snap, opt_path)               # restore + consume the snapshot
            new_depth = depth - 1
        else:
            log.warning("optimize_expert_queue: undo snapshot %s missing; "
                        "reverting job %s to the raw upload", snap.name, job_id)
            opt_path.unlink(missing_ok=True)
            _clear_snapshots(folder, svg_id)
            new_depth = 0

    _invalidate_estimate(job_id, optimize_expert_undo_depth=new_depth)
    return new_depth


# Internals ---------------------------------------------------------------

def _undo_snapshot(folder: Path, svg_id: str, level: int) -> Path:
    """The file holding the ``.opt.svg`` bytes to restore when undoing *from*
    ``level`` (i.e. back to ``level - 1``). Level 1 has none — undoing it just
    removes ``.opt.svg`` and the raw upload shows through again."""
    return folder / f"{svg_id}.opt.undo.{level}.svg"


def _clear_snapshots(folder: Path, svg_id: str) -> None:
    for p in folder.glob(f"{svg_id}.opt.undo.*.svg"):
        p.unlink(missing_ok=True)


def _invalidate_estimate(job_id: str, **extra_fields) -> None:
    """Drop the job's on-record time/distance estimate and re-queue it.

    An expert transform (Execute or Undo) can change geometry/distances
    arbitrarily, so the estimate from before is stale — same reasoning as
    api.update_job dropping it on any edit. Preview/layers already refresh from
    the new .opt.svg once the frontend sees the run as done; the estimate needs
    the same nudge or it'd keep showing the pre-transform numbers until some
    unrelated edit happened to touch it. ``extra_fields`` rides along on the
    same write (the caller's new optimize_expert_undo_depth)."""
    try:
        plan_queue.cancel(job_id)
        state.update_job(
            job_id,
            estimated_total_seconds=None,
            progress_total_seconds=None,
            distance_pendown_m=None,
            distance_total_m=None,
            pen_lifts=None,
            plan_status=None,
            plan_error=None,
            **extra_fields,
        )
        fresh = state.get_job(job_id)
        if fresh is not None:
            plan_queue.enqueue(fresh)
    except Exception:
        log.exception("optimize_expert_queue: failed to refresh estimate for job %s", job_id)


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
            log.exception("optimize_expert_queue: unexpected error processing job %s", task.job_id)
            task.ok = False
            task.error = "internal error"
        finally:
            with _lock:
                _inflight = None
                _last[task.job_id] = task
            task.started.set()
            task.done.set()


def _process(task: _Task) -> None:
    if task.cancel_event.is_set():
        task.ok = False
        task.error = "cancelled"
        return
    if not task.src_path.exists():
        task.ok = False
        task.error = "source SVG missing"
        return

    task.started.set()

    # The three boxes run on top of vpype's own "read", which drops a pen dot
    # and misreads inherit-ed strokes; repair the source first, the same as the
    # beginner-mode optimize (see svg_utils.prepare_for_vpype).
    svg_utils.prepare_for_vpype(task.src_path)

    folder = task.src_path.parent
    opt_path = folder / f"{task.svg_id}.opt.svg"

    # Execute stacks onto the current result. ``level`` is how many Executes
    # are already baked into .opt.svg; if the file is gone (its library row was
    # deleted out from under the job) the stack is meaningless — start over
    # from the raw upload.
    job = state.get_job(task.job_id)
    level = int((job or {}).get("optimize_expert_undo_depth", 0) or 0)
    if not opt_path.exists():
        _clear_snapshots(folder, task.svg_id)
        level = 0

    made_snapshot = opt_path.exists()
    snap_path = _undo_snapshot(folder, task.svg_id, level + 1) if made_snapshot else None
    if snap_path is not None:
        try:
            shutil.copyfile(opt_path, snap_path)
        except OSError:
            log.exception("optimize_expert_queue: could not snapshot %s for undo", opt_path)
            snap_path = None
            made_snapshot = False

    def _rollback() -> None:
        """Put .opt.svg back to ``level`` after a failed/cancelled Execute."""
        if snap_path is not None and snap_path.exists():
            os.replace(snap_path, opt_path)         # restore + consume the snapshot
        elif snap_path is None:
            opt_path.unlink(missing_ok=True)        # level 0 -> raw shows through

    src = opt_path if made_snapshot else task.src_path
    try:
        # One heavy job at a time across all background subsystems
        # (app/workload.py) — never runs alongside a beginner-mode optimize,
        # a plan estimate, or the ink cache.
        with workload.heavy("optimize-expert"):
            svg_optimize.run_custom_pipeline(
                src, opt_path, task.box_texts,
                on_output=task.append_log,
                timeout_s=config.OPTIMIZE_EXPERT_TIMEOUT_S,
            )
    except svg_optimize.OptimizeError as e:
        _rollback()
        if task.cancel_event.is_set():
            task.ok = False
            task.error = "cancelled"
            return
        task.ok = False
        task.error = str(e)
        return

    if task.cancel_event.is_set():
        # Cancelled mid-write but vpype somehow returned 0 anyway — drop the
        # output and put the previous level back.
        _rollback()
        task.ok = False
        task.error = "cancelled"
        return

    # vpype drops any layer it found nothing plottable in, which would shift
    # every later layer's index (see optimize_queue._process for the same fix).
    try:
        svg_utils.reconcile_layers(task.src_path, opt_path)
    except Exception:
        log.exception("optimize_expert_queue: layer reconcile failed for job %s", task.job_id)
        _rollback()
        task.ok = False
        task.error = "could not re-align layers after optimization"
        return

    task.ok = True
    _invalidate_estimate(task.job_id, optimize_expert_undo_depth=level + 1)
