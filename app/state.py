import asyncio
import json
import logging
import os
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = BASE_DIR / "state.json"
UPLOAD_DIR = BASE_DIR / "uploads"
# See emit_position / draw_trace_snapshot below.
DRAW_TRACE_PATH = BASE_DIR / "draw_trace.jsonl"

# Jobs that were mid-run when the service died need normalization on load.
# With a valid resume_path on disk the worker's existing res_plot flow can
# continue them; otherwise we can't recover position so they become failed.
# `plotting_calibration` is handled separately — it has no checkpoint, but
# falling back to awaiting_pen_change is harmless (user re-runs calibration
# if they want), so we don't lump it in here.
_IN_FLIGHT_STATUSES = {"planning", "plotting", "homing", "awaiting_pen_change"}

# Permitted job-status transitions, validated centrally in update_job /
# update_job_silent. Same-status updates (no actual transition) are exempt,
# as is the startup rehydrate code in _load_from_disk — that path normalises
# orphaned in-flight statuses by direct mutation, not as a real transition.
_VALID_TRANSITIONS: dict[str, set[str]] = {
    # `plotting` is allowed straight from queued/awaiting_optimize/optimizing
    # so the plot worker can skip the `planning` status when the preview is
    # already cached (the plan queue ran ahead of the user's Plot click).
    # "failed" straight from "queued" covers _run_job's pre-flight bounds
    # check, which can reject a job before optimize/plan ever starts.
    # "cancelled" straight from "queued" covers a cancel that lands during the
    # optimize/plan phases: the plan-cache fast path never flips the job to
    # `planning`, so it is still reading as queued when the worker picks the
    # flag up (see _run_job / cancel_active).
    # A job the user has uploaded but not committed to plotting. It sits in the
    # queue list, fully editable, and the plot worker cannot see it —
    # next_queued_job matches on "queued" alone, so a draft is skipped by
    # construction rather than by a check that could be forgotten. The only way
    # out is the user pressing Queue (see main.queue_job); a draft that is no
    # longer wanted is deleted, never cancelled, so there is nothing else here.
    "draft":                {"queued"},
    "queued":               {"awaiting_optimize", "optimizing", "planning", "plotting",
                             "cancelled", "failed"},
    "awaiting_optimize":    {"optimizing", "planning", "plotting", "cancelled", "failed"},
    "optimizing":           {"planning", "plotting", "cancelled", "failed"},
    # "failed" is reachable from every state that renders a stage
    # (_run_staged_loop_impl builds the stage SVG before flipping the job to
    # `plotting`, so a malformed document surfaces while the job still reads
    # as planning / paused / awaiting_pen_change). Without it the worker's own
    # error handler would raise InvalidTransition and strand the job it was
    # trying to fail cleanly.
    "planning":             {"plotting", "cancelled", "failed"},
    "plotting":             {"paused", "homing", "awaiting_pen_change",
                             "completed", "failed"},
    "paused":               {"plotting", "homing", "cancelled", "failed"},
    "awaiting_pen_change":  {"plotting", "plotting_calibration", "cancelled",
                             "failed"},
    "plotting_calibration": {"awaiting_pen_change", "cancelled", "failed"},
    "homing":               {"cancelled"},
    "completed":            {"queued"},
    "failed":               {"queued"},
    "cancelled":            {"queued"},
}


class InvalidTransition(RuntimeError):
    """Raised when update_job is asked to perform a status change that
    violates _VALID_TRANSITIONS. Surfaces real bugs early rather than
    silently corrupting the state machine."""


def _check_status_transition(job_id: str, current: str | None, new: str) -> None:
    if new == current:
        return
    allowed = _VALID_TRANSITIONS.get(current or "", set())
    if new in allowed:
        return
    log.error("state: invalid transition %s → %s for job %s", current, new, job_id)
    raise InvalidTransition(f"job {job_id}: {current!r} → {new!r}")

_queue: list[dict] = []
# svg_id -> {"status": "pending"|"optimizing"|"ready"|"failed",
#            "settings_key": str | None,
#            "error": str | None,
#            "updated_at": float}
# Tracks the optimize lifecycle of an uploaded SVG, independently of any job
# that may reference it. Persisted in state.json so a service restart can tell
# whether the on-disk .opt.svg still matches the settings it was produced with.
_svgs: dict[str, dict] = {}
# svg_id -> {"filename": str, "uploaded_at": float, "pre_optimized": bool,
#            "derived_from": str | None}
# What the uploads folder holds, independently of whether a job still points at
# it. Kept separate from _svgs above rather than folded into it: that one tracks
# an optimize run and is cleared when the run is cancelled (clear_svg_status),
# which would take the original filename with it — and the filename is the only
# thing that makes a library row readable, since the file on disk is named after
# an 8-character uuid fragment.
#
# `derived_from` marks a copy promoted out of a parent's .opt.svg (see
# main._promote_optimized), so a second promotion of the same parent reuses it
# instead of copying the file again.
_uploads_meta: dict[str, dict] = {}
_active_id: str | None = None
# Sticky version of _active_id: sees the same job IDs but never reverts to
# None when a run ends. Lets the frontend keep showing the delta overlay
# (see effectiveDeltaForJob in app.js) on the job that was just running —
# manual_origin_offset/origin_nudge aren't cleared by a cancel either — even
# across a page reload, when active_id has already gone back to None and
# there's no client-side memory of what it used to be.
_last_active_id: str | None = None
_awaiting_next_job: bool = False
_pause_at_pen_up_pending: bool = False
_last_pen_position: dict | None = None
# Writer for the active job's draw-stream trace (see emit_position /
# draw_trace_snapshot). Backed by a file rather than an in-memory list: a
# multi-hour job emits one point per motion segment (see
# plot_worker._feed_sm_and_emit_position), and an unbounded in-process list of
# that many points was OOM-killing long multi-layer jobs. Only opened when
# draw_stream_enabled, since most installs never read the trace at all.
_draw_trace_fp = None
_draw_trace_job_id: str | None = None
# Always None: nothing sets it, and the setter that used to has been removed as
# dead code. The field stays in snapshot() because API.md publishes it in the
# WebSocket state payload, so dropping it is a wire-format change for external
# clients (the macOS companion app) to gain one line. Per-job failures are
# reported on the job's own `error` instead, which is what the UI reads.
_error: str | None = None
# Fine origin nudge dialed in during an awaiting_pen_change pause (see
# plot_worker.nudge_origin). Belongs to one run: it applies to that run's
# remaining stages and is walked back off the carriage when the run ends (see
# plot_worker._undo_origin_nudge), so the next run starts from the same
# physical origin this one did.
_origin_nudge: dict = {"x_mm": 0.0, "y_mm": 0.0}
# Net displacement accumulated by idle-only manual jogging (see
# plot_worker.manual_jog), so manual_jog_home knows how far to walk back.
# Session-only, unrelated to _origin_nudge above (that one corrects an active
# job mid-plot).
_manual_origin_offset: dict = {"x_mm": 0.0, "y_mm": 0.0}
# Where on the bed the page's top-left corner has been declared to be (see
# plot_worker.set_origin) — (0, 0), the machine's own corner, until the user
# redefines it. _manual_origin_offset above is measured from *this*, not from
# the machine corner, so only the guard that keeps the carriage clear of the
# far rail has to add the two back together. Session-only, same as the rest.
_origin_base: dict = {"x_mm": 0.0, "y_mm": 0.0}
# Camera recording state (see app/camera.py). job_id is None for a manually
# started recording that isn't tied to any job.
_recording: dict = {"status": "idle", "job_id": None}  # idle | recording | paused

_clients: set = set()
_event_queue: asyncio.Queue | None = None
_loop: asyncio.AbstractEventLoop | None = None
# Serializes writes to STATE_PATH. The asyncio loop, the plot worker thread,
# and the optimize worker thread can all call _persist concurrently. Without
# this lock two writers would race on the same .tmp path: one renames it
# away, the other crashes its os.replace with FileNotFoundError.
_persist_lock = threading.Lock()


def init(loop: asyncio.AbstractEventLoop) -> None:
    global _event_queue, _loop
    _loop = loop
    _event_queue = asyncio.Queue()
    _load_from_disk()


def _load_from_disk() -> None:
    """Rehydrate the queue from state.json. Called once at startup.

    Skips jobs whose source SVG has been deleted — they're unrecoverable.
    Normalizes statuses so an interrupted plot surfaces as 'paused' (if a
    resume SVG is on disk, OR if it was a clean awaiting_pen_change boundary)
    or 'failed' otherwise, never as 'plotting'.
    """
    global _queue, _active_id
    if not STATE_PATH.exists():
        return
    try:
        data = json.loads(STATE_PATH.read_text())
    except Exception:
        log.exception("state: could not parse %s; starting empty", STATE_PATH)
        return
    raw = data.get("queue") or []
    rehydrated: list[dict] = []
    for job in raw:
        if not isinstance(job, dict) or "job_id" not in job or "svg_id" not in job:
            continue
        if not (UPLOAD_DIR / f"{job['svg_id']}.svg").exists():
            log.info("state: dropping job %s — source SVG missing", job.get("job_id"))
            continue
        status = job.get("status")
        resume_path = job.get("resume_path")
        resume_ok = bool(resume_path) and Path(resume_path).exists()
        if status in ("awaiting_optimize", "optimizing"):
            # Crashed before plotting started — no pen state to recover. Send
            # the job back to queued so the user can plot it again with one
            # click. The optimize phase will re-run on its own.
            job["status"] = "queued"
            job["error"] = None
            job["resume_path"] = None
            job["stages"] = []
            job["current_stage_index"] = 0
        elif status == "awaiting_pen_change":
            # Clean checkpoint between stages: no resume SVG needed — the next
            # stage will be filtered/rendered from current_stage_index fresh.
            job["status"] = "paused"
            job["resume_path"] = None
        elif status == "plotting_calibration":
            # Calibration has no resume SVG. Treat it like an awaiting_pen_change
            # rehydrate (which has the same shape — clean stage boundary, pen
            # somewhere unknown): mark paused, user resumes, next stage is
            # re-rendered from current_stage_index fresh.
            job["status"] = "paused"
            job["resume_path"] = None
        elif status in _IN_FLIGHT_STATUSES:
            # planning/plotting/homing: pen was somewhere mid-motion. We can
            # recover only if plot_run had time to write a resume SVG.
            if resume_ok:
                job["status"] = "paused"
            else:
                job["status"] = "failed"
                job["error"] = "Service restarted mid-plot before a resume point was reached."
                job["resume_path"] = None
        elif status == "paused" and not resume_ok:
            job["status"] = "failed"
            job["error"] = "Resume data missing after service restart."
            job["resume_path"] = None
        rehydrated.append(job)
    _queue = rehydrated

    # Surface the first paused job as the UI's "active" one so the Resume
    # button is wired up without needing a live worker thread.
    for j in _queue:
        if j["status"] == "paused":
            _active_id = j["job_id"]
            break

    # Reload SVG optimize statuses. Drop entries whose source SVG is gone, and
    # demote any "pending"/"optimizing" leftovers from a crashed worker to a
    # safe state — the new worker will re-enqueue if appropriate.
    raw_svgs = data.get("svgs") or {}
    if isinstance(raw_svgs, dict):
        for svg_id, entry in raw_svgs.items():
            if not isinstance(entry, dict):
                continue
            if not (UPLOAD_DIR / f"{svg_id}.svg").exists():
                continue
            status = entry.get("status")
            if status not in ("ready", "failed"):
                # In-flight at crash time. We can't trust an .opt.svg the worker
                # may have been mid-write to — drop the whole entry so the
                # bootstrap re-enqueue picks it up cleanly.
                continue
            _svgs[svg_id] = {
                "status": status,
                "settings_key": entry.get("settings_key"),
                "error": entry.get("error"),
                "updated_at": float(entry.get("updated_at") or 0.0),
            }

    # Reload upload metadata, dropping anything whose file has gone. A missing
    # entry is survivable (the library falls back to showing the svg_id), a
    # stale one is not — it would advertise a file that cannot be selected.
    raw_uploads = data.get("uploads") or {}
    if isinstance(raw_uploads, dict):
        for svg_id, entry in raw_uploads.items():
            if not isinstance(entry, dict):
                continue
            if not (UPLOAD_DIR / f"{svg_id}.svg").exists():
                continue
            _uploads_meta[svg_id] = {
                "filename": str(entry.get("filename") or ""),
                "uploaded_at": float(entry.get("uploaded_at") or 0.0),
                "pre_optimized": bool(entry.get("pre_optimized")),
                "derived_from": entry.get("derived_from") or None,
            }

    log.info("state: loaded %d job(s) from %s", len(_queue), STATE_PATH)


def _persist() -> None:
    """Atomically write the queue to state.json. Called after every mutation.

    Writes to a sibling tmp file and renames so a crash mid-write can't
    corrupt the file the next boot reads. Serialised under ``_persist_lock``
    because the snapshot also needs to be a coherent point-in-time view of
    ``_queue``+``_svgs`` rather than a half-applied write from another thread.
    """
    with _persist_lock:
        try:
            tmp = STATE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(
                {"queue": _queue, "svgs": _svgs, "uploads": _uploads_meta},
                indent=2) + "\n")
            os.replace(tmp, STATE_PATH)
        except Exception:
            log.exception("state: failed to persist %s", STATE_PATH)


def snapshot() -> dict:
    return {
        "queue": [deepcopy(j) for j in _queue],
        "svgs": {k: dict(v) for k, v in _svgs.items()},
        "active_id": _active_id,
        "last_active_id": _last_active_id,
        "awaiting_next_job": _awaiting_next_job,
        "pause_at_pen_up_pending": _pause_at_pen_up_pending,
        "last_pen_position": dict(_last_pen_position) if _last_pen_position else None,
        "origin_nudge_x_mm": _origin_nudge["x_mm"],
        "origin_nudge_y_mm": _origin_nudge["y_mm"],
        "manual_origin_offset_x_mm": _manual_origin_offset["x_mm"],
        "manual_origin_offset_y_mm": _manual_origin_offset["y_mm"],
        "recording_status": _recording["status"],
        "recording_job_id": _recording["job_id"],
        "status": _derive_top_status(),
        "error": _error,
    }


def _derive_top_status() -> str:
    if _awaiting_next_job:
        return "awaiting_next_job"
    if _active_id is None:
        # Any errored/completed jobs don't count; idle unless queue is non-empty queued
        return "idle"
    job = _get(_active_id)
    return job["status"] if job else "idle"


def _get(job_id: str) -> dict | None:
    for j in _queue:
        if j["job_id"] == job_id:
            return j
    return None


def get_job(job_id: str) -> dict | None:
    j = _get(job_id)
    return deepcopy(j) if j else None


def active_job() -> dict | None:
    return _get(_active_id) if _active_id else None


def add_job(job: dict) -> dict:
    record = _make_record(job)
    _queue.append(record)
    _persist()
    _broadcast()
    return deepcopy(record)


def _make_record(data: dict) -> dict:
    return {
        "job_id": uuid.uuid4().hex[:8],
        "status": "queued",
        "created_at": time.time(),
        "stages": [],
        "current_stage_index": 0,
        "started_at": None,
        # Start of the span the pen is currently plotting — reset at every
        # stage boundary and every resume. run_elapsed_seconds banks the spans
        # already finished, so the two together measure the whole run (see
        # plot_worker.run_elapsed_seconds); plotting_started_at on its own
        # would restart the progress bar at each layer.
        "plotting_started_at": None,
        "run_elapsed_seconds": 0.0,
        "estimated_total_seconds": None,
        # Denominator for the progress bar. Starts equal to
        # estimated_total_seconds and is the only one a live speed change
        # recalibrates, so the displayed estimate stays a plain estimate.
        "progress_total_seconds": None,
        "distance_pendown_m": None,
        "distance_total_m": None,
        "pen_lifts": None,
        "resume_path": None,
        "error": None,
        # Set alongside "error" only when the job was blocked by a leftover
        # manual jog (see plot_worker._run_job / _delta_correction_mm): the
        # exact (dx, dy) nudge that would bring the artwork back onto the
        # page, so the UI can offer a "nudge back" button instead of forcing
        # a full return-to-origin. Cleared on requeue.
        "jog_hint_dx_mm": None,
        "jog_hint_dy_mm": None,
        **data,
    }


def update_job(job_id: str, **updates) -> dict | None:
    j = _get(job_id)
    if j is None:
        return None
    if "status" in updates:
        _check_status_transition(job_id, j.get("status"), updates["status"])
    j.update(updates)
    _persist()
    _broadcast()
    return deepcopy(j)


def update_job_silent(job_id: str, **updates) -> None:
    """Same as update_job but no broadcast — for tight-loop fields we don't need to stream."""
    j = _get(job_id)
    if j is not None:
        if "status" in updates:
            _check_status_transition(job_id, j.get("status"), updates["status"])
        j.update(updates)
        _persist()


def remove_job(job_id: str) -> bool:
    global _queue
    before = len(_queue)
    _queue = [j for j in _queue if j["job_id"] != job_id]
    if len(_queue) < before:
        _persist()
        _broadcast()
        return True
    return False


def move_job(job_id: str, new_index: int) -> bool:
    global _queue
    j = _get(job_id)
    if j is None:
        return False
    _queue = [x for x in _queue if x["job_id"] != job_id]
    new_index = max(0, min(new_index, len(_queue)))
    _queue.insert(new_index, j)
    _persist()
    _broadcast()
    return True


def set_active(job_id: str | None) -> None:
    global _active_id, _last_active_id, _draw_trace_fp, _draw_trace_job_id
    _active_id = job_id
    if job_id is not None:
        _last_active_id = job_id
        # Called exactly once per genuine run start (queue_loop wraps each
        # _run_job/_resume_job with set_active(id) ... set_active(None)), so
        # this is the one reliable "a new plot is starting" signal — unlike
        # comparing job_id in emit_position below, which can't tell a
        # requeued run (same job_id) from the one that just finished.
        _draw_trace_job_id = job_id
        if _draw_trace_fp is not None:
            _draw_trace_fp.close()
            _draw_trace_fp = None
        if config.DRAW_STREAM_ENABLED:
            try:
                # Line-buffered so a concurrent reader (draw_trace_snapshot)
                # never sees a partial line. Never allowed to fail the run —
                # trace recording is a bonus, not a plotting dependency.
                _draw_trace_fp = open(DRAW_TRACE_PATH, "w", buffering=1)
            except OSError:
                log.exception("state: could not open %s", DRAW_TRACE_PATH)
    _broadcast()


def set_awaiting_next_job(flag: bool) -> None:
    global _awaiting_next_job
    _awaiting_next_job = flag
    _broadcast()


def set_pause_at_pen_up_pending(flag: bool) -> None:
    global _pause_at_pen_up_pending
    new = bool(flag)
    if new == _pause_at_pen_up_pending:
        return
    _pause_at_pen_up_pending = new
    _broadcast()


def pause_at_pen_up_pending() -> bool:
    return _pause_at_pen_up_pending


def set_origin_nudge(x_mm: float, y_mm: float) -> None:
    global _origin_nudge
    if _origin_nudge["x_mm"] == x_mm and _origin_nudge["y_mm"] == y_mm:
        return
    _origin_nudge = {"x_mm": x_mm, "y_mm": y_mm}
    _broadcast()


def origin_nudge() -> tuple[float, float]:
    return _origin_nudge["x_mm"], _origin_nudge["y_mm"]


def set_manual_origin_offset(x_mm: float, y_mm: float) -> None:
    global _manual_origin_offset
    if (_manual_origin_offset["x_mm"] == x_mm
            and _manual_origin_offset["y_mm"] == y_mm):
        return
    _manual_origin_offset = {"x_mm": x_mm, "y_mm": y_mm}
    _broadcast()


def manual_origin_offset() -> tuple[float, float]:
    return _manual_origin_offset["x_mm"], _manual_origin_offset["y_mm"]


def set_origin_base(x_mm: float, y_mm: float) -> None:
    global _origin_base
    if _origin_base["x_mm"] == x_mm and _origin_base["y_mm"] == y_mm:
        return
    _origin_base = {"x_mm": x_mm, "y_mm": y_mm}


def origin_base() -> tuple[float, float]:
    return _origin_base["x_mm"], _origin_base["y_mm"]


def set_recording(status: str, job_id: str | None) -> None:
    global _recording
    if _recording["status"] == status and _recording["job_id"] == job_id:
        return
    _recording = {"status": status, "job_id": job_id}
    _broadcast()


def recording() -> tuple[str, str | None]:
    return _recording["status"], _recording["job_id"]


def set_svg_status(svg_id: str, status: str,
                   settings_key: str | None = None,
                   error: str | None = None) -> None:
    _svgs[svg_id] = {
        "status": status,
        "settings_key": settings_key,
        "error": error,
        "updated_at": time.time(),
    }
    _persist()
    _broadcast()


def clear_svg_status(svg_id: str) -> None:
    if _svgs.pop(svg_id, None) is not None:
        _persist()
        _broadcast()


def get_svg_status(svg_id: str) -> dict | None:
    e = _svgs.get(svg_id)
    return dict(e) if e else None


# Upload metadata ----------------------------------------------------------

def set_upload_meta(svg_id: str, filename: str, *,
                    pre_optimized: bool = False,
                    derived_from: str | None = None) -> None:
    _uploads_meta[svg_id] = {
        "filename": filename,
        "uploaded_at": time.time(),
        "pre_optimized": pre_optimized,
        "derived_from": derived_from,
    }
    _persist()


def get_upload_meta(svg_id: str) -> dict | None:
    e = _uploads_meta.get(svg_id)
    return dict(e) if e else None


def all_upload_meta() -> dict[str, dict]:
    return {k: dict(v) for k, v in _uploads_meta.items()}


def drop_upload_meta(svg_id: str) -> None:
    if _uploads_meta.pop(svg_id, None) is not None:
        _persist()


def next_queued_job() -> dict | None:
    for j in _queue:
        if j["status"] == "queued":
            return j
    return None


def next_paused_job() -> dict | None:
    for j in _queue:
        if j["status"] == "paused":
            return j
    return None


def broadcast() -> None:
    _broadcast()


def _broadcast() -> None:
    if _loop is None or _event_queue is None:
        return
    payload = {"type": "state", **snapshot()}
    _loop.call_soon_threadsafe(_event_queue.put_nowait, payload)


def emit_position(x_mm: float, y_mm: float, pen_down: bool) -> None:
    global _last_pen_position
    _last_pen_position = {"x_mm": x_mm, "y_mm": y_mm, "pen_down": pen_down}
    # Append every sample for the active job to disk (see DRAW_TRACE_PATH
    # above) so the /draw-stream OBS overlay can replay what's already been
    # drawn after a browser refresh, instead of starting blank. _draw_trace_fp
    # is only non-None while draw_stream_enabled and a job is active (opened
    # in set_active); writes stop as soon as active_id goes back to None on
    # completion, but the file itself is left alone so a finished job's trace
    # is still replayable until the next job actually starts, matching
    # last_active_id's "hold the last run" behaviour above.
    if _draw_trace_fp is not None and _active_id is not None:
        job = _get(_active_id)
        stage_index = (job or {}).get("current_stage_index", 0)
        _draw_trace_fp.write(json.dumps(
            {"x_mm": x_mm, "y_mm": y_mm, "pen_down": pen_down,
             "stage_index": stage_index}) + "\n")
    if _loop is None or _event_queue is None:
        return
    payload = {"type": "position", "x_mm": x_mm, "y_mm": y_mm, "pen_down": pen_down}
    _loop.call_soon_threadsafe(_event_queue.put_nowait, payload)


def draw_trace_snapshot() -> dict:
    points: list[dict] = []
    try:
        with open(DRAW_TRACE_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    points.append(json.loads(line))
                except ValueError:
                    # Last line of a file still being written to can be cut
                    # mid-write; skip rather than fail the whole snapshot.
                    continue
    except OSError:
        pass
    return {"job_id": _draw_trace_job_id, "points": points}


async def drain_events() -> None:
    assert _event_queue is not None
    while True:
        payload = await _event_queue.get()
        text = json.dumps(payload)
        dead = []
        for ws in list(_clients):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            _clients.discard(ws)


def add_client(ws) -> None:
    _clients.add(ws)


def remove_client(ws) -> None:
    _clients.discard(ws)
