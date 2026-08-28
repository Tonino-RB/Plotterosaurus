"""app.camera's finalize path and app.upload_queue's retention/disk guard.

Two data-loss bugs are pinned here, both of which the recording pipeline used
to have:

* Finalizing deleted the segment directory in a `finally:` — so ffmpeg running
  out of disk, or being killed by its own 30-minute timeout, destroyed the
  footage it had been assembling. The segments *are* the recording; nothing
  else holds a copy.
* Nothing ever deleted a finished recording, and nothing checked the card
  before starting a new one. When the card fills, `state._persist()` logs its
  write failure and carries on, so the job queue silently stops being saved.

`config.update()` is never called here: conftest sandboxes state.json and
uploads/ but not config.json, so a test that used it would write the running
plotter's real settings. Everything goes through monkeypatch instead.
"""
import os
import subprocess
import time
from pathlib import Path

import pytest

from app import camera, config, state, upload_queue


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """An isolated recordings folder, segment tree and failed-segment tree."""
    out = tmp_path / "recordings"
    out.mkdir()
    monkeypatch.setattr(config, "CAMERA_OUTPUT_FOLDER", str(out))
    monkeypatch.setattr(config, "CAMERA_RCLONE_TARGET", None)
    monkeypatch.setattr(config, "CAMERA_RETENTION_GB", 0.0)
    monkeypatch.setattr(camera, "SEGMENTS_DIR", tmp_path / "_segments")
    monkeypatch.setattr(camera, "FAILED_SEGMENTS_DIR", tmp_path / "_failed")
    # _finalize waits for MediaMTX to flush its last part; nothing here is
    # MediaMTX, so the wait is pure test latency.
    monkeypatch.setattr(camera.time, "sleep", lambda *_: None)
    upload_queue._uploads.clear()
    yield out
    upload_queue._uploads.clear()


def _session(mode="realtime", session_id="job-1"):
    """A stopped session with one segment on disk, shaped as
    start_recording() leaves it."""
    segments_dir = camera.SEGMENTS_DIR / session_id
    segments_dir.mkdir(parents=True)
    (segments_dir / "cam_2026-01-01_12-00-00-000000.mp4").write_bytes(b"segment")
    return {"job_id": None, "session_id": session_id, "started_at": 1767268800.0,
            "mode": mode, "interval_s": 5.0, "multiplier": 4.0,
            "segments_dir": segments_dir}


# Finalize: the failure path must not destroy the footage -------------------

def test_failed_finalize_keeps_the_segments(rig, monkeypatch):
    session = _session()

    def boom(args, timeout=1800):
        # What running out of disk mid-concat looks like from here.
        raise subprocess.CalledProcessError(1, "ffmpeg", stderr="No space left on device")

    monkeypatch.setattr(camera, "_run_ffmpeg", boom)
    camera._finalize(session)

    assert not session["segments_dir"].exists(), "segments were left in the live tree"
    kept = list(camera.FAILED_SEGMENTS_DIR.iterdir())
    assert len(kept) == 1
    assert (kept[0] / "cam_2026-01-01_12-00-00-000000.mp4").read_bytes() == b"segment"


def test_failed_finalize_is_reported(rig, monkeypatch):
    monkeypatch.setattr(camera, "_run_ffmpeg", lambda *a, **k: (_ for _ in ()).throw(
        subprocess.CalledProcessError(1, "ffmpeg", stderr="No space left on device")))
    camera._finalize(_session())

    rows = camera.failed_finalizes()
    assert len(rows) == 1
    assert rows[0]["name"].startswith("job-1-")
    assert "No space left on device" in rows[0]["error"]
    assert rows[0]["size_bytes"] > 0


def test_failed_finalize_removes_the_half_written_output(rig, monkeypatch):
    session = _session()
    out = upload_queue.output_dir()

    def half_write(args, timeout=1800):
        Path(args[-1]).write_bytes(b"truncated")
        raise subprocess.CalledProcessError(1, "ffmpeg", stderr="killed")

    monkeypatch.setattr(camera, "_run_ffmpeg", half_write)
    camera._finalize(session)

    assert not list(out.glob("*.mp4")), "a partial file was left to be uploaded"


def test_two_failures_of_the_same_session_both_survive(rig, monkeypatch):
    monkeypatch.setattr(camera, "_run_ffmpeg", lambda *a, **k: (_ for _ in ()).throw(
        subprocess.CalledProcessError(1, "ffmpeg", stderr="nope")))
    camera._finalize(_session())
    camera._finalize(_session())  # same job, same start time -> same stamp
    assert len(list(camera.FAILED_SEGMENTS_DIR.iterdir())) == 2


def test_successful_finalize_clears_the_segments(rig, monkeypatch):
    session = _session()

    def write_output(args, timeout=1800):
        Path(args[-1]).write_bytes(b"video")

    monkeypatch.setattr(camera, "_run_ffmpeg", write_output)
    camera._finalize(session)

    assert not session["segments_dir"].exists()
    assert not camera.FAILED_SEGMENTS_DIR.exists()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(session["started_at"]))
    assert [p.name for p in upload_queue.output_dir().glob("*.mp4")] == \
        [f"job-1-{stamp}.mp4"]


def test_nothing_recorded_is_not_a_failure(rig, monkeypatch):
    """An empty segment directory means MediaMTX never wrote anything — there
    is no footage to preserve, so it should not be reported as recoverable."""
    session = _session()
    for f in session["segments_dir"].iterdir():
        f.unlink()
    monkeypatch.setattr(camera, "_run_ffmpeg", lambda *a, **k: pytest.fail("nothing to do"))
    camera._finalize(session)

    assert not session["segments_dir"].exists()
    assert camera.failed_finalizes() == []


# Finalize: the sped-up pass costs what its output is worth -----------------

def test_sped_up_drops_frames_and_is_not_time_limited(rig, monkeypatch):
    """setpts alone re-times without dropping anything, so libx264 still had
    to encode every captured frame — hours of work for a video the user asked
    to be minutes long, against a 30-minute timeout that killed it (and, back
    then, the footage with it)."""
    calls = []

    def record(args, timeout=1800):
        calls.append((args, timeout))
        Path(args[-1]).write_bytes(b"video")

    monkeypatch.setattr(camera, "_run_ffmpeg", record)
    monkeypatch.setattr(config, "CAMERA_FPS", 30)
    camera._finalize(_session(mode="sped_up"))

    concat_args, concat_timeout = calls[0]
    assert "copy" in concat_args and concat_timeout == 1800

    speed_args, speed_timeout = calls[1]
    vf = speed_args[speed_args.index("-vf") + 1]
    assert vf == "setpts=PTS/4.0,fps=30"
    assert speed_timeout is None


# Starting: refuse rather than half-fill the card ---------------------------

def test_start_recording_refuses_when_the_card_is_nearly_full(rig, monkeypatch):
    monkeypatch.setattr(config, "CAMERA_ENABLED", True)
    monkeypatch.setattr(upload_queue, "free_bytes", lambda path=None: 100 << 20)
    monkeypatch.setattr(camera, "_api_patch", lambda *a, **k: pytest.fail("started anyway"))

    with pytest.raises(RuntimeError, match="free disk space"):
        camera.start_recording(None)
    assert state.recording() == ("idle", None)


def test_required_free_space_follows_the_bitrate(monkeypatch):
    """The guard has to scale: at 20 Mbps the card empties four times as fast
    as it does at the 5 Mbps default."""
    monkeypatch.setattr(config, "CAMERA_BITRATE", 5_000_000)
    low = camera.required_free_bytes()
    monkeypatch.setattr(config, "CAMERA_BITRATE", 20_000_000)
    assert camera.required_free_bytes() == pytest.approx(low * 4, rel=0.01)


def test_required_free_space_has_a_floor(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_BITRATE", 100_000)
    assert camera.required_free_bytes() == camera._GUARD_FLOOR_BYTES


# Retention -----------------------------------------------------------------

def _recording_file(out, name, size, age_s):
    path = out / name
    path.write_bytes(b"\0" * size)
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


def test_retention_deletes_the_oldest_over_the_cap(rig, monkeypatch):
    out = rig
    monkeypatch.setattr(config, "CAMERA_RETENTION_GB", 2 / 1024)  # 2 MB
    old = _recording_file(out, "old.mp4", 1 << 20, age_s=300)
    mid = _recording_file(out, "mid.mp4", 1 << 20, age_s=200)
    new = _recording_file(out, "new.mp4", 1 << 20, age_s=100)

    upload_queue.enforce_retention()

    assert new.exists() and mid.exists()
    assert not old.exists()


def test_retention_never_deletes_the_recording_that_just_finished(rig, monkeypatch):
    out = rig
    monkeypatch.setattr(config, "CAMERA_RETENTION_GB", 1 / 2048)  # 0.5 MB
    fresh = _recording_file(out, "fresh.mp4", 1 << 20, age_s=1)

    upload_queue.enforce_retention(keep=fresh.name)

    assert fresh.exists()


def test_retention_leaves_a_recording_whose_upload_has_not_landed(rig, monkeypatch):
    out = rig
    monkeypatch.setattr(config, "CAMERA_RETENTION_GB", 1 / 1024)  # 1 MB
    pending = _recording_file(out, "pending.mp4", 1 << 20, age_s=300)
    _recording_file(out, "new.mp4", 1 << 20, age_s=100)
    upload_queue._uploads[pending.name] = upload_queue._new_entry()

    upload_queue.enforce_retention()

    assert pending.exists(), "deleted the only copy of a recording still to upload"


def test_retention_waits_for_the_sweep_before_deleting_an_unknown_file(rig, monkeypatch):
    """With a cloud target set, no upload record means "not asked yet", not
    "nothing to wait for".

    enforce_retention runs the instant a recording finalizes. sweep() skips a
    file younger than _SETTLE_S in case ffmpeg is still writing it and only
    runs every _SWEEP_INTERVAL_S, so a recording can be on disk with no entry
    for minutes — and `keep` only covers the newest one. Treating that as
    uploaded destroys the only copy before the upload was ever attempted.
    """
    out = rig
    monkeypatch.setattr(config, "CAMERA_RETENTION_GB", 1 / 1024)   # 1 MB
    monkeypatch.setattr(config, "CAMERA_RCLONE_TARGET", "remote:plots")
    unswept = _recording_file(out, "unswept.mp4", 1 << 20, age_s=300)
    _recording_file(out, "new.mp4", 1 << 20, age_s=100)
    upload_queue._uploads.pop(unswept.name, None)

    upload_queue.enforce_retention()

    assert unswept.exists(), "deleted a recording the upload queue had not seen yet"


def test_retention_still_reclaims_when_there_is_no_cloud_target(rig, monkeypatch):
    """The other half of the same rule: with no target configured there is no
    upload to wait for, so the cap is the only thing keeping the card clear
    and an unknown file is a plain deletion candidate."""
    out = rig
    monkeypatch.setattr(config, "CAMERA_RETENTION_GB", 1 / 1024)   # 1 MB
    monkeypatch.setattr(config, "CAMERA_RCLONE_TARGET", None)
    old = _recording_file(out, "old.mp4", 1 << 20, age_s=300)
    _recording_file(out, "new.mp4", 1 << 20, age_s=100)
    upload_queue._uploads.pop(old.name, None)

    upload_queue.enforce_retention()

    assert not old.exists()


def test_retention_off_by_default_in_this_rig_keeps_everything(rig):
    out = rig
    old = _recording_file(out, "old.mp4", 4 << 20, age_s=300)
    upload_queue.enforce_retention()
    assert old.exists()
