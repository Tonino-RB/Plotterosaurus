"""Plot recording via a Camera Module 3 + MediaMTX.

MediaMTX (a separate systemd service, see systemd/mediamtx.service) owns the
camera: it reads the sensor directly via its native `rpiCamera` source,
serves the live RTSP/HLS/WebRTC stream, and writes recording segments to
disk. This module never touches the camera or an encoder itself — it only
drives MediaMTX's local HTTP Control API (127.0.0.1:9997, no auth, see
https://mediamtx.org/docs/usage/control-api) and post-processes the segment
files MediaMTX produces with ffmpeg, optionally handing the result to
`rclone copy`.

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

from . import config, state

log = logging.getLogger(__name__)

MEDIAMTX_API = "http://127.0.0.1:9997"
PATH_NAME = "cam"
RTSP_PORT = 8554
HLS_PORT = 8888
WEBRTC_PORT = 8889

BASE_DIR = Path(__file__).resolve().parent.parent
SEGMENTS_DIR = BASE_DIR / "recordings" / "_segments"

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

def _output_dir() -> Path:
    out_dir = Path(config.CAMERA_OUTPUT_FOLDER)
    if not out_dir.is_absolute():
        out_dir = BASE_DIR / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _run_ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", *args], check=True, capture_output=True,
                   text=True, timeout=1800)


def _finalize(session: dict) -> None:
    segments_dir: Path = session["segments_dir"]
    mode = session["mode"]
    # Stamp the time into the name: a job recording is keyed by job_id, and
    # re-plotting a job would otherwise overwrite the previous take.
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(session["started_at"]))
    final_path = _output_dir() / f"{session['session_id']}-{stamp}.mp4"
    if mode != "timelapse":
        # Let MediaMTX flush the segment part that was in flight when
        # recording stopped (recordPartDuration defaults to 1s). Waited for
        # here rather than in stop_recording(), which holds the module lock on
        # the plot worker's thread while it finishes a job.
        time.sleep(1.5)
    try:
        if mode == "timelapse":
            frames = sorted(segments_dir.glob("frame_*.jpg"))
            if not frames:
                log.warning("camera: no timelapse frames captured for %s", session["session_id"])
                return
            _run_ffmpeg([
                "-y", "-framerate", "24",
                "-i", str(segments_dir / "frame_%06d.jpg"),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(final_path),
            ])
        else:
            segments = sorted(segments_dir.rglob("*.mp4"))
            if not segments:
                log.warning("camera: no recorded segments for %s", session["session_id"])
                return
            concat_list = segments_dir / "concat.txt"
            concat_list.write_text("".join(f"file '{s.resolve()}'\n" for s in segments))
            target = final_path if mode == "realtime" else segments_dir / "concat_raw.mp4"
            _run_ffmpeg(["-y", "-f", "concat", "-safe", "0",
                        "-i", str(concat_list), "-c", "copy", str(target)])
            if mode == "sped_up":
                multiplier = session["multiplier"]
                _run_ffmpeg([
                    "-y", "-i", str(target),
                    "-vf", f"setpts=PTS/{multiplier}", "-an", str(final_path),
                ])
    except FileNotFoundError:
        log.error("camera: ffmpeg is not installed — cannot finalize recording %s",
                  session["session_id"])
        return
    except subprocess.CalledProcessError as e:
        log.error("camera: ffmpeg failed for %s: %s", session["session_id"],
                  (e.stderr or "")[-2000:])
        return
    except Exception:
        log.exception("camera: finalize failed for %s", session["session_id"])
        return
    finally:
        shutil.rmtree(segments_dir, ignore_errors=True)

    if config.CAMERA_RCLONE_TARGET:
        threading.Thread(target=_rclone_copy, args=(final_path,), daemon=True).start()


def _rclone_copy(path: Path) -> None:
    if not shutil.which("rclone"):
        log.warning("camera: rclone not installed — skipping cloud sync for %s", path)
        return
    try:
        subprocess.run(["rclone", "copy", str(path), config.CAMERA_RCLONE_TARGET],
                       check=True, capture_output=True, text=True, timeout=1800)
    except Exception:
        log.exception("camera: rclone copy failed for %s", path)
        return
    if config.CAMERA_RCLONE_DELETE_LOCAL:
        try:
            path.unlink()
        except OSError:
            log.exception("camera: failed to delete local recording %s after upload", path)


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
