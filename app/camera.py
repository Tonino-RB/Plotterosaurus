"""Plot recording via a Camera Module 3 + MediaMTX.

MediaMTX (a separate systemd service, see systemd/mediamtx.service) owns the
camera: it reads the sensor directly via its native `rpiCamera` source,
serves the live RTSP/HLS/WebRTC stream, and writes recording segments to
disk. This module never touches the camera or an encoder itself — it only
drives MediaMTX's local HTTP Control API (127.0.0.1:9997, no auth, see
https://mediamtx.org/docs/usage/control-api) and post-processes the segment
files MediaMTX produces with ffmpeg, handing the result to
app/upload_queue.py when a cloud target is configured.

Realtime and sped-up modes use MediaMTX's native continuous segment
recording (`record`/`recordPath`), toggled on/off for pause/resume — each
on/off cycle starts a new segment file, and stop_recording() concatenates
every segment produced during the session losslessly with ffmpeg. Timelapse
mode skips MediaMTX recording entirely and instead grabs a single JPEG frame
from the RTSP stream every N seconds via a short-lived ffmpeg call, so a long
plot doesn't cost full continuous-capture storage for footage nobody will
watch at real speed.

MediaMTX being unreachable degrades to a logged warning wherever possible —
a camera problem should never take down a plot.
"""
import json
import logging
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config, state, upload_queue

log = logging.getLogger(__name__)

MEDIAMTX_API = "http://127.0.0.1:9997"
PATH_NAME = "cam"
RTSP_PORT = 8554
HLS_PORT = 8888
WEBRTC_PORT = 8889

# camera_flicker_mode -> MediaMTX's rpiCameraFlickerPeriod (microseconds).
_FLICKER_PERIOD_US = {"off": 0, "50hz": 10_000, "60hz": 8_333}

# Free space start_recording insists on before it will start capturing.
# Capture is only half the cost: finalizing concatenates the segments into a
# second copy on the same filesystem (a third in sped_up mode, which writes
# concat_raw.mp4 first), so a recording needs 2-3x its own size free by the
# time it ends. Nothing here knows how long the plot will run, so the guard
# asks for an hour of capture at the configured bitrate plus the copies that
# implies, floored so a very low bitrate can't shrink the check to nothing.
# Filling the card is worse than losing the footage: state._persist() swallows
# its own write error, so the job queue silently stops being saved.
_GUARD_HOURS = 1.0
_GUARD_COPIES = 3
_GUARD_FLOOR_BYTES = 2 << 30

BASE_DIR = Path(__file__).resolve().parent.parent
SEGMENTS_DIR = BASE_DIR / "recordings" / "_segments"
# Where the segments of a finalize that failed are parked. Deliberately not a
# subdirectory of SEGMENTS_DIR: stop_orphaned_recording() clears that whole
# tree at startup, and these are the one thing in it worth keeping.
FAILED_SEGMENTS_DIR = BASE_DIR / "recordings" / "_failed_segments"

# Serializes recording start/pause/resume/stop so a job hook and a manual
# button click can't race MediaMTX into an inconsistent state.
_lock = threading.Lock()

_timelapse_thread: threading.Thread | None = None
_stop_timelapse = threading.Event()

# The active recording session's parameters, used by stop_recording() to know
# how to finalize. None while idle.
_session: dict | None = None


# MediaMTX Control API -------------------------------------------------------

def _api_patch(path: str, body: dict) -> bool:
    req = urllib.request.Request(
        f"{MEDIAMTX_API}{path}",
        data=json.dumps(body).encode("utf-8"),
        method="PATCH",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=5).close()
        return True
    except (urllib.error.URLError, OSError):
        log.warning("camera: MediaMTX PATCH %s unreachable", path)
        return False
    except Exception:
        log.exception("camera: MediaMTX PATCH %s failed", path)
        return False


def apply_camera_settings() -> None:
    """Push the current camera_* config to MediaMTX. Called after Settings save
    and on startup so a service restart doesn't lose non-default values."""
    if not config.CAMERA_ENABLED:
        return
    _api_patch(f"/v3/config/paths/patch/{PATH_NAME}", {
        "rpiCameraWidth": config.CAMERA_RESOLUTION_WIDTH,
        "rpiCameraHeight": config.CAMERA_RESOLUTION_HEIGHT,
        "rpiCameraFPS": config.CAMERA_FPS,
        "rpiCameraBitrate": config.CAMERA_BITRATE,
        # Half-second keyframe interval instead of MediaMTX's own 60-frame
        # (2s @ 30fps) default, so a WebRTC viewer reconnecting (e.g. after
        # this same PATCH restarts the camera pipeline, or just opening the
        # settings modal) waits less for the first decodable frame.
        "rpiCameraIDRPeriod": max(1, config.CAMERA_FPS // 2),
        "rpiCameraAfMode": config.CAMERA_AF_MODE,
        "rpiCameraLensPosition": config.CAMERA_LENS_POSITION,
        "rpiCameraAfSpeed": config.CAMERA_AF_SPEED,
        "rpiCameraBrightness": config.CAMERA_BRIGHTNESS,
        "rpiCameraContrast": config.CAMERA_CONTRAST,
        "rpiCameraSaturation": config.CAMERA_SATURATION,
        "rpiCameraSharpness": config.CAMERA_SHARPNESS,
        "rpiCameraEV": config.CAMERA_EV,
        "rpiCameraExposure": config.CAMERA_EXPOSURE_MODE,
        "rpiCameraShutter": config.CAMERA_SHUTTER_US,
        "rpiCameraAWB": config.CAMERA_AWB_MODE,
        "rpiCameraGain": config.CAMERA_GAIN,
        "rpiCameraDenoise": config.CAMERA_DENOISE,
        "rpiCameraFlickerPeriod": _FLICKER_PERIOD_US[config.CAMERA_FLICKER_MODE],
        "rpiCameraHFlip": config.CAMERA_HFLIP,
        "rpiCameraVFlip": config.CAMERA_VFLIP,
    })


def stop_orphaned_recording() -> None:
    """Tell MediaMTX to stop recording, once, at startup.

    Recording state lives only in this process, so a crash or restart mid-plot
    leaves MediaMTX happily writing segments with nothing left to ever stop it
    — it fills the disk. Called from the app's lifespan only; deliberately not
    folded into apply_camera_settings(), which also runs on every Settings
    save and would kill a recording in progress.
    """
    if not config.CAMERA_ENABLED:
        return
    _api_patch(f"/v3/config/paths/patch/{PATH_NAME}", {"record": False})
    shutil.rmtree(SEGMENTS_DIR, ignore_errors=True)


def set_focus(af_mode: str, lens_position: float) -> None:
    """Live focus control for the settings-modal slider. Persists the value
    so it survives a MediaMTX/service restart."""
    if not config.CAMERA_ENABLED:
        raise RuntimeError("Camera is not enabled")
    if af_mode not in ("auto", "manual", "continuous"):
        raise RuntimeError("Invalid autofocus mode")
    ok = _api_patch(f"/v3/config/paths/patch/{PATH_NAME}", {
        "rpiCameraAfMode": af_mode,
        "rpiCameraLensPosition": lens_position,
    })
    if not ok:
        raise RuntimeError("Could not reach the camera service (MediaMTX)")
    config.update(camera_af_mode=af_mode, camera_lens_position=lens_position)


def status(request_host: str) -> dict:
    rec_status, job_id = state.recording()
    return {
        "camera_enabled": config.CAMERA_ENABLED,
        "recording_status": rec_status,
        "recording_job_id": job_id,
        "rtsp_url": f"rtsp://{request_host}:{RTSP_PORT}/{PATH_NAME}",
        "hls_url": f"http://{request_host}:{HLS_PORT}/{PATH_NAME}/index.m3u8",
        "webrtc_view_url": f"http://{request_host}:{WEBRTC_PORT}/{PATH_NAME}",
    }


# Free space ------------------------------------------------------------------

def required_free_bytes() -> int:
    """How much room start_recording wants before it will capture anything."""
    hour = config.CAMERA_BITRATE / 8 * 3600 * _GUARD_HOURS
    return int(max(_GUARD_FLOOR_BYTES, hour * _GUARD_COPIES))


def _free_bytes() -> int | None:
    """Free space where a recording actually lands, or None if it can't be
    measured. MediaMTX's segments go under the install dir and the finished
    file goes to the output folder, which the user can point at another
    volume — the tighter of the two is the one that runs out."""
    measured = [f for f in (upload_queue.free_bytes(),
                            upload_queue.free_bytes(BASE_DIR)) if f is not None]
    return min(measured) if measured else None


# Recording lifecycle ---------------------------------------------------------

def start_recording(job_id: str | None, mode: str | None = None,
                    timelapse_interval_s: float | None = None,
                    speed_multiplier: float | None = None) -> None:
    if not config.CAMERA_ENABLED:
        raise RuntimeError("Camera is not enabled")
    with _lock:
        rec_status, _ = state.recording()
        if rec_status != "idle":
            raise RuntimeError("A recording is already in progress")
        free = _free_bytes()
        if free is not None and free < required_free_bytes():
            raise RuntimeError("Not enough free disk space to record")
        mode = mode or config.CAMERA_RECORDING_MODE_DEFAULT
        session_id = job_id or f"manual-{int(time.time())}"
        segments_dir = SEGMENTS_DIR / session_id
        segments_dir.mkdir(parents=True, exist_ok=True)

        global _session
        _session = {
            "job_id": job_id,
            "session_id": session_id,
            "started_at": time.time(),
            "mode": mode,
            "interval_s": timelapse_interval_s or config.CAMERA_TIMELAPSE_INTERVAL_S_DEFAULT,
            "multiplier": speed_multiplier or config.CAMERA_SPEED_MULTIPLIER_DEFAULT,
            "segments_dir": segments_dir,
        }

        if mode == "timelapse":
            _start_timelapse(segments_dir, _session["interval_s"])
        else:
            ok = _api_patch(f"/v3/config/paths/patch/{PATH_NAME}", {
                "record": True,
                # %path is required by MediaMTX even though there's only ever
                # one path ("cam") here — it rejects a recordPath lacking it.
                "recordPath": str(segments_dir / "%path_%Y-%m-%d_%H-%M-%S-%f"),
                "recordFormat": "fmp4",
            })
            if not ok:
                _session = None
                shutil.rmtree(segments_dir, ignore_errors=True)
                raise RuntimeError("Could not start recording (MediaMTX unreachable)")
        state.set_recording("recording", job_id)


def pause_recording() -> None:
    with _lock:
        rec_status, job_id = state.recording()
        if rec_status != "recording":
            return
        if _session["mode"] == "timelapse":
            _stop_timelapse_thread()
        else:
            _api_patch(f"/v3/config/paths/patch/{PATH_NAME}", {"record": False})
        state.set_recording("paused", job_id)


def resume_recording() -> None:
    with _lock:
        rec_status, job_id = state.recording()
        if rec_status != "paused":
            return
        if _session["mode"] == "timelapse":
            _start_timelapse(_session["segments_dir"], _session["interval_s"])
        else:
            _api_patch(f"/v3/config/paths/patch/{PATH_NAME}", {"record": True})
        state.set_recording("recording", job_id)


def stop_recording() -> None:
    with _lock:
        rec_status, _ = state.recording()
        if rec_status == "idle":
            return
        session = _session
        if session["mode"] == "timelapse":
            _stop_timelapse_thread()
        else:
            _api_patch(f"/v3/config/paths/patch/{PATH_NAME}", {"record": False})
        state.set_recording("idle", None)
        globals()["_session"] = None
    # Finalize (ffmpeg concat/speedup, optional rclone) off the lock — it can
    # take a while and shouldn't block the next recording from starting.
    threading.Thread(target=_finalize, args=(session,), daemon=True).start()


def is_recording_job(job_id: str) -> bool:
    _, active_job_id = state.recording()
    return active_job_id == job_id


# Finalization -----------------------------------------------------------

def _run_ffmpeg(args: list[str], timeout: float | None = 1800) -> None:
    subprocess.run(["ffmpeg", *args], check=True, capture_output=True,
                   text=True, timeout=timeout)


def _assemble(session: dict, segments_dir: Path, mode: str, final_path: Path) -> bool:
    """Turn one session's segments into final_path. False if there was
    nothing to assemble; raises whatever ffmpeg raises."""
    if mode == "timelapse":
        if not sorted(segments_dir.glob("frame_*.jpg")):
            log.warning("camera: no timelapse frames captured for %s", session["session_id"])
            return False
        # No timeout on an encode: its runtime is proportional to the footage
        # and the Pi's CPU is slow, so a wall-clock cap can only ever fire on
        # a legitimate job. The concat below is a stream copy, where a cap is
        # a real watchdog rather than a guillotine.
        _run_ffmpeg([
            "-y", "-framerate", "24",
            "-i", str(segments_dir / "frame_%06d.jpg"),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(final_path),
        ], timeout=None)
        return True

    segments = sorted(segments_dir.rglob("*.mp4"))
    if not segments:
        log.warning("camera: no recorded segments for %s", session["session_id"])
        return False
    concat_list = segments_dir / "concat.txt"
    concat_list.write_text("".join(f"file '{s.resolve()}'\n" for s in segments))
    target = final_path if mode == "realtime" else segments_dir / "concat_raw.mp4"
    _run_ffmpeg(["-y", "-f", "concat", "-safe", "0",
                "-i", str(concat_list), "-c", "copy", str(target)])
    if mode == "sped_up":
        multiplier = session["multiplier"]
        # setpts alone only re-times the frames — every one of them still goes
        # through libx264, so a five-hour plot was hours of software encoding
        # for a video the user asked to be minutes long. The fps filter drops
        # the frames setpts made redundant *before* the encoder sees them, so
        # the pass costs what its output is worth rather than what its input
        # was.
        _run_ffmpeg([
            "-y", "-i", str(target),
            "-vf", f"setpts=PTS/{multiplier},fps={config.CAMERA_FPS}",
            "-an", str(final_path),
        ], timeout=None)
    return True


def _keep_failed_segments(session: dict, stamp: str, error: str) -> None:
    """Park the segments of a failed finalize where they survive, with a note
    saying what went wrong.

    An unconditional rmtree used to run here, on the failure paths as well as
    the success one: ffmpeg fell over — out of disk, or killed by its own
    30-minute timeout — and the source footage it had been assembling was
    deleted with it, so the recording was simply gone. The segments *are* the
    recording; they are worth more than the tidiness of the folder.
    """
    src: Path = session["segments_dir"]
    dest = FAILED_SEGMENTS_DIR / f"{session['session_id']}-{stamp}"
    try:
        FAILED_SEGMENTS_DIR.mkdir(parents=True, exist_ok=True)
        n = 1
        while dest.exists():  # never overwrite an earlier rescue
            dest = FAILED_SEGMENTS_DIR / f"{session['session_id']}-{stamp}-{n}"
            n += 1
        shutil.move(str(src), str(dest))
    except Exception:
        log.exception("camera: could not move the segments of %s aside",
                      session["session_id"])
        dest = src
    try:
        (dest / "error.txt").write_text(error.strip() + "\n")
    except OSError:
        log.warning("camera: could not write the failure note for %s",
                    session["session_id"])
    log.error("camera: finalize failed for %s — raw footage kept in %s",
              session["session_id"], dest)


def failed_finalizes() -> list[dict]:
    """Recordings that never became a video, still on disk as raw segments.

    Read off the directory rather than remembered in memory, so the warning in
    the recordings panel survives the restart the user is likely to try next.
    """
    try:
        dirs = sorted(p for p in FAILED_SEGMENTS_DIR.iterdir() if p.is_dir())
    except OSError:
        return []
    rows = []
    for d in dirs:
        try:
            error = (d / "error.txt").read_text().strip()
        except OSError:
            error = ""
        try:
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        except OSError:
            size = 0
        rows.append({"name": d.name, "path": str(d), "size_bytes": size,
                     "error": error[:500]})
    return rows


def _finalize(session: dict) -> None:
    segments_dir: Path = session["segments_dir"]
    mode = session["mode"]
    # Stamp the time into the name: a job recording is keyed by job_id, and
    # re-plotting a job would otherwise overwrite the previous take.
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(session["started_at"]))
    final_path = upload_queue.output_dir() / f"{session['session_id']}-{stamp}.mp4"
    if mode != "timelapse":
        # Let MediaMTX flush the segment part that was in flight when
        # recording stopped (recordPartDuration defaults to 1s). Waited for
        # here rather than in stop_recording(), which holds the module lock on
        # the plot worker's thread while it finishes a job.
        time.sleep(1.5)
    error: str | None = None
    try:
        produced = _assemble(session, segments_dir, mode, final_path)
    except FileNotFoundError:
        error = "ffmpeg is not installed"
    except subprocess.CalledProcessError as e:
        log.error("camera: ffmpeg failed for %s: %s", session["session_id"],
                  (e.stderr or "")[-2000:])
        error = (e.stderr or "").strip()[-500:] or f"ffmpeg exited {e.returncode}"
    except Exception as e:
        log.exception("camera: finalize failed for %s", session["session_id"])
        error = str(e) or type(e).__name__
    if error is not None:
        # A half-written output would otherwise be uploaded as if it were the
        # recording; the segments kept below are the copy worth recovering.
        final_path.unlink(missing_ok=True)
        _keep_failed_segments(session, stamp, error)
        return

    shutil.rmtree(segments_dir, ignore_errors=True)
    if not produced:
        return
    upload_queue.enqueue(final_path)
    upload_queue.enforce_retention(keep=final_path.name)


# Timelapse snapshot loop -----------------------------------------------------

def _start_timelapse(segments_dir: Path, interval_s: float) -> None:
    global _timelapse_thread
    _stop_timelapse.clear()
    _timelapse_thread = threading.Thread(
        target=_timelapse_loop, args=(segments_dir, interval_s), daemon=True)
    _timelapse_thread.start()


def _stop_timelapse_thread() -> None:
    global _timelapse_thread
    _stop_timelapse.set()
    t = _timelapse_thread
    if t is not None and t.is_alive():
        t.join(timeout=5.0)
    _timelapse_thread = None


def _timelapse_loop(segments_dir: Path, interval_s: float) -> None:
    # Continue numbering across a pause/resume within the same session so
    # frames stay in order for the final assembly.
    existing = sorted(segments_dir.glob("frame_*.jpg"))
    n = int(existing[-1].stem.split("_")[1]) + 1 if existing else 0
    rtsp_url = f"rtsp://127.0.0.1:{RTSP_PORT}/{PATH_NAME}"
    while not _stop_timelapse.is_set():
        frame_path = segments_dir / f"frame_{n:06d}.jpg"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-rtsp_transport", "tcp", "-i", rtsp_url,
                 "-frames:v", "1", str(frame_path)],
                check=True, capture_output=True, timeout=15,
            )
            n += 1
        except Exception:
            log.warning("camera: timelapse snapshot failed", exc_info=True)
        _stop_timelapse.wait(interval_s)

