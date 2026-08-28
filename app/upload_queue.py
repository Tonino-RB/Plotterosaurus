"""Single-worker queue that pushes finished recordings to the rclone target.

The copy used to be a bare daemon thread spawned per recording: one attempt,
no record of it anywhere, no second chance. A Wi-Fi drop, an expired token, a
service restart mid-transfer — any of them left the file sitting in the output
folder forever. "Delete local after upload" then silently kept everything, and
nothing in the UI said why.

So uploads come through here instead. One at a time (a Pi's uplink is not
faster for running two transfers at once), retried with backoff until the
remote confirms them, and re-discovered by a sweep of the output folder at
startup and every few minutes — which is what makes an interrupted upload
resume: the process may die mid-transfer, but the file is still on disk, and
the next sweep picks it back up.

"Resume" is per-attempt, not per-byte. rclone restarts a failed file from zero
on the next attempt; the chunked-upload session belongs to the rclone process
that died with it and is not ours to persist. What is guaranteed is that the
attempt keeps happening.

Nothing here is persisted. It doesn't need to be: the output folder *is* the
queue, and rebuilding the in-memory state from it is exactly what the sweep
does.
"""
import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import config, workload

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent

# Retry backoff in seconds, indexed by attempt count; the last value repeats,
# so a target that is down for a day is retried four times an hour rather than
# either giving up or hammering it.
_BACKOFF = (30, 60, 120, 300, 900)

# A sweep skips files touched more recently than this — ffmpeg may still be
# writing one. camera._finalize enqueues explicitly once ffmpeg has returned,
# so a just-finished recording never waits for this.
_SETTLE_S = 60.0

_SWEEP_INTERVAL_S = 300.0

# Kill an rclone that has produced no output for this long. It emits stats
# every second while alive, so silence means it is wedged, not slow, and the
# single worker slot is too valuable to leave to a hung transfer.
_STALL_S = 300.0

_MEDIA_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm"}

# name -> {status, percent, bytes, total_bytes, error, attempts, retry_at,
#          queued_at}. status is one of queued/uploading/uploaded/failed.
_uploads: dict[str, dict] = {}
_lock = threading.Lock()
_wakeup = threading.Event()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()
_shutdown = threading.Event()
_proc: subprocess.Popen | None = None


# Output folder ----------------------------------------------------------

def output_dir() -> Path:
    """The configured local recordings folder, created if missing."""
    out_dir = Path(config.CAMERA_OUTPUT_FOLDER)
    if not out_dir.is_absolute():
        out_dir = BASE_DIR / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def free_bytes(path: Path | None = None) -> int | None:
    """Free space on the volume holding `path` (the output folder by
    default), or None if it can't be measured."""
    try:
        return shutil.disk_usage(path if path is not None else output_dir()).free
    except OSError:
        return None


def is_recording_file(path: Path) -> bool:
    """Recordings only. The folder also holds camera scratch — the segment
    directory and the optical-registration still — which are named with a
    leading underscore and must never be uploaded or offered for deletion."""
    return (path.is_file() and not path.name.startswith("_")
            and path.suffix.lower() in _MEDIA_SUFFIXES)


def path_for(name: str) -> Path:
    """Resolve a client-supplied recording name inside the output folder.

    Raises ValueError for anything that isn't a plain recording filename
    directly in that folder — this is the only thing standing between the
    delete/preview routes and the rest of the disk.
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("Invalid recording name")
    out = output_dir().resolve()
    path = (out / name).resolve()
    if path.parent != out or not is_recording_file(path):
        raise ValueError("Invalid recording name")
    return path


# Lifecycle --------------------------------------------------------------

def start() -> None:
    """Start the upload worker thread (idempotent)."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _shutdown.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="upload-queue")
        _thread.start()


def shutdown(timeout_s: float = 5.0) -> None:
    """Signal the worker to stop and kill any in-flight rclone.

    The half-transferred file stays on disk, which is the point: the next
    startup sweep finds it and starts the upload over.
    """
    _shutdown.set()
    _wakeup.set()
    proc = _proc
    if proc is not None and proc.poll() is None:
        proc.kill()
    t = _thread
    if t is not None and t.is_alive() and threading.current_thread() is not t:
        t.join(timeout=timeout_s)


# Public API -------------------------------------------------------------

def enqueue(path: Path) -> None:
    """Queue one finished recording for upload. No-op without a target."""
    if not config.CAMERA_RCLONE_TARGET:
        return
    name = path.name
    with _lock:
        entry = _uploads.get(name)
        if entry and entry["status"] in ("queued", "uploading"):
            return
        _uploads[name] = _new_entry()
    _wakeup.set()


def retry(name: str) -> None:
    """Re-queue a failed upload immediately, resetting its backoff."""
    with _lock:
        entry = _uploads.get(name)
        if entry and entry["status"] == "uploading":
            return
        _uploads[name] = _new_entry()
    _wakeup.set()


def sweep() -> None:
    """Enqueue every recording in the output folder that isn't already queued.

    This is the recovery path: whatever an interrupted upload, a crash, or a
    reboot left behind gets picked back up here.
    """
    if not config.CAMERA_RCLONE_TARGET:
        return
    now = time.time()
    try:
        files = [p for p in output_dir().iterdir() if is_recording_file(p)]
    except OSError:
        log.warning("upload_queue: could not scan the recordings folder", exc_info=True)
        return
    with _lock:
        names = {p.name for p in files}
        # Forget entries whose file is gone (uploaded and deleted, or removed
        # by the user), so the list the UI renders matches the disk.
        for gone in [n for n in _uploads if n not in names]:
            del _uploads[gone]
        for path in files:
            entry = _uploads.get(path.name)
            if entry is not None and entry["status"] != "failed":
                continue
            if entry is None:
                try:
                    if now - path.stat().st_mtime < _SETTLE_S:
                        continue  # ffmpeg may still be writing it
                except OSError:
                    continue
                _uploads[path.name] = _new_entry()
    _wakeup.set()


def list_recordings() -> dict:
    """Everything the recordings panel needs: the local files and, for each,
    where its upload has got to."""
    target = config.CAMERA_RCLONE_TARGET or ""
    try:
        files = sorted((p for p in output_dir().iterdir() if is_recording_file(p)),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    now = time.time()
    rows = []
    with _lock:
        for path in files:
            try:
                st = path.stat()
            except OSError:
                continue
            entry = _uploads.get(path.name)
            rows.append({
                "name": path.name,
                "size_bytes": st.st_size,
                "modified": st.st_mtime,
                "upload_status": entry["status"] if entry else ("idle" if target else "local_only"),
                "percent": entry["percent"] if entry else 0,
                "uploaded_bytes": entry["bytes"] if entry else 0,
                "error": entry["error"] if entry else None,
                "attempts": entry["attempts"] if entry else 0,
                "retry_in_s": max(0, round(entry["retry_at"] - now)) if entry and entry["retry_at"] else 0,
            })
    return {
        "rclone_target": target,
        "rclone_installed": shutil.which("rclone") is not None,
        "delete_local": bool(config.CAMERA_RCLONE_DELETE_LOCAL),
        "retention_gb": config.CAMERA_RETENTION_GB,
        "free_bytes": free_bytes(),
        "recordings": rows,
    }


def delete(name: str) -> None:
    """Delete one local recording. Cancels its upload if it's in flight."""
    path = path_for(name)
    with _lock:
        entry = _uploads.pop(name, None)
        if entry and entry["status"] == "uploading":
            proc = _proc
            if proc is not None and proc.poll() is None:
                proc.kill()
    path.unlink(missing_ok=True)


def enforce_retention(keep: str | None = None) -> None:
    """Delete the oldest recordings until the folder fits camera_retention_gb.

    Called after each finished recording, which is the only moment the folder
    grows. Nothing used to delete a finished recording ever, and a five-hour
    realtime capture is ~7GB, so a handful of plots was enough to fill the
    card — at which point state._persist() starts logging its write failures
    and quietly discarding the job queue.

    Two things are never deleted: `keep`, the recording that just finished
    (deleting it the instant it lands would be a strange way to record), and
    anything whose cloud upload hasn't landed yet — a stuck upload wins over
    the cap, since the alternative is deleting the only copy that exists.
    """
    if config.CAMERA_RETENTION_GB <= 0:
        return
    limit = config.CAMERA_RETENTION_GB * (1 << 30)
    try:
        files = sorted((p for p in output_dir().iterdir() if is_recording_file(p)),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        log.warning("upload_queue: could not scan the recordings folder", exc_info=True)
        return
    total = 0.0
    for path in files:
        try:
            total += path.stat().st_size
        except OSError:
            continue
        if total <= limit or path.name == keep:
            continue
        with _lock:
            entry = _uploads.get(path.name)
        if config.CAMERA_RCLONE_TARGET:
            # No entry is not the same as nothing to wait for. sweep() skips a
            # file younger than _SETTLE_S in case ffmpeg is still writing it,
            # and only runs every _SWEEP_INTERVAL_S — so a recording that just
            # landed has no entry yet, and `keep` only covers the newest one.
            # Deleting on an absent entry would destroy the only copy of the
            # recording before its upload was ever attempted.
            if entry is None or entry["status"] != "uploaded":
                continue
        elif entry is not None and entry["status"] != "uploaded":
            continue
        try:
            path.unlink()
        except OSError:
            log.warning("upload_queue: could not delete %s for retention", path.name,
                        exc_info=True)
            continue
        with _lock:
            _uploads.pop(path.name, None)
        log.info("upload_queue: deleted %s — over the %.1fGB retention cap",
                 path.name, config.CAMERA_RETENTION_GB)


# Internals --------------------------------------------------------------

def _new_entry() -> dict:
    return {"status": "queued", "percent": 0, "bytes": 0, "total_bytes": 0,
            "error": None, "attempts": 0, "retry_at": 0.0, "queued_at": time.time()}


def _next_due() -> tuple[str | None, float]:
    """The oldest entry ready to run, plus how long to sleep if none is."""
    now = time.time()
    due: str | None = None
    oldest = float("inf")
    wait = _SWEEP_INTERVAL_S
    with _lock:
        for name, entry in _uploads.items():
            if entry["status"] in ("uploading", "uploaded"):
                continue
            if entry["retry_at"] > now:
                wait = min(wait, entry["retry_at"] - now)
                continue
            if entry["queued_at"] < oldest:
                oldest, due = entry["queued_at"], name
    return due, (0.0 if due else max(1.0, wait))


def _loop() -> None:
    # Uploads are network-bound, but the JSON-stats parsing and rclone's own
    # hashing are not free; like every other background worker here it yields
    # to the plot thread (see app/workload.py).
    workload.deprioritize()
    next_sweep = 0.0
    while not _shutdown.is_set():
        if time.monotonic() >= next_sweep:
            sweep()
            next_sweep = time.monotonic() + _SWEEP_INTERVAL_S
        name, wait = _next_due()
        if name is None:
            _wakeup.wait(min(wait, _SWEEP_INTERVAL_S))
            _wakeup.clear()
            continue
        try:
            _upload(name)
        except Exception:
            log.exception("upload_queue: unexpected error uploading %s", name)
            _fail(name, "internal error")


def _fail(name: str, error: str) -> None:
    with _lock:
        entry = _uploads.get(name)
        if entry is None:
            return
        entry["attempts"] += 1
        delay = _BACKOFF[min(entry["attempts"] - 1, len(_BACKOFF) - 1)]
        entry.update(status="failed", error=error, retry_at=time.time() + delay)


def _upload(name: str) -> None:
    global _proc
    target = config.CAMERA_RCLONE_TARGET
    if not target:
        with _lock:
            _uploads.pop(name, None)
        return
    if not shutil.which("rclone"):
        _fail(name, "rclone is not installed")
        return
    try:
        path = path_for(name)
    except ValueError:
        with _lock:
            _uploads.pop(name, None)
        return

    size = path.stat().st_size
    with _lock:
        entry = _uploads.get(name)
        if entry is None:
            return
        entry.update(status="uploading", percent=0, bytes=0, total_bytes=size,
                     error=None, retry_at=0.0)

    # --stats-log-level NOTICE because stats are logged at INFO, which the
    # default log level swallows; --use-json-log so the percentage can be read
    # off `stats` instead of scraped out of rclone's human-readable block.
    cmd = ["rclone", "copy", str(path), target,
           "--use-json-log", "--stats", "1s", "--stats-log-level", "NOTICE"]
    last_error: str | None = None
    activity = [time.monotonic()]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True)
    except OSError as e:
        _fail(name, str(e))
        return
    _proc = proc
    stall = threading.Thread(target=_stall_watch, args=(proc, activity),
                             daemon=True, name="upload-stall")
    stall.start()
    try:
        for line in proc.stderr:
            activity[0] = time.monotonic()
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            stats = rec.get("stats")
            if stats:
                total = stats.get("totalBytes") or size
                done = stats.get("bytes") or 0
                with _lock:
                    entry = _uploads.get(name)
                    if entry is None:
                        break
                    entry["bytes"] = done
                    entry["total_bytes"] = total
                    entry["percent"] = min(100, int(done * 100 / total)) if total else 0
            elif rec.get("level") in ("error", "critical", "fatal"):
                last_error = (rec.get("msg") or "").strip()[:200]
        rc = proc.wait()
    finally:
        _proc = None
        activity[0] = float("inf")  # release the watchdog

    with _lock:
        cancelled = name not in _uploads
    if cancelled:  # deleted underneath us
        return
    if _shutdown.is_set():
        return
    if rc != 0:
        _fail(name, last_error or f"rclone exited {rc}")
        log.warning("upload_queue: upload of %s failed (%s)", name,
                    last_error or f"exit {rc}")
        return

    with _lock:
        entry = _uploads.get(name)
        if entry is not None:
            entry.update(status="uploaded", percent=100, bytes=entry["total_bytes"],
                         error=None, retry_at=0.0)
    log.info("upload_queue: uploaded %s to %s", name, target)

    if config.CAMERA_RCLONE_DELETE_LOCAL:
        try:
            path.unlink()
        except OSError:
            log.exception("upload_queue: failed to delete %s after upload", name)
            return
        with _lock:
            _uploads.pop(name, None)


def _stall_watch(proc: subprocess.Popen, activity: list) -> None:
    while proc.poll() is None:
        if time.monotonic() - activity[0] > _STALL_S:
            log.warning("upload_queue: rclone produced no output for %ds — killing it",
                        int(_STALL_S))
            proc.kill()
            return
        time.sleep(5.0)
