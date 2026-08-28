"""app.upload_queue — the recordings → rclone path.

Uses a local directory as the rclone target. rclone treats a plain path as a
filesystem remote, so `rclone copy file /tmp/dst` exercises the same code path
as `rclone copy file onedrive:...` right down to the JSON stats parsing, with
no network and no credentials.

What matters here is what the old fire-and-forget thread got wrong: an upload
that fails must be *retried*, and the file must survive to be retried with.
"""
import os
import shutil
import time

import pytest

from app import config, upload_queue

pytestmark = pytest.mark.skipif(shutil.which("rclone") is None,
                                reason="rclone is not installed")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """An isolated output folder + local target, with the module's in-memory
    upload table cleared so tests can't leak state into each other."""
    out = tmp_path / "recordings"
    out.mkdir()
    dst = tmp_path / "cloud"
    dst.mkdir()
    monkeypatch.setattr(config, "CAMERA_OUTPUT_FOLDER", str(out))
    monkeypatch.setattr(config, "CAMERA_RCLONE_TARGET", str(dst))
    monkeypatch.setattr(config, "CAMERA_RCLONE_DELETE_LOCAL", False)
    upload_queue._uploads.clear()
    yield out, dst
    upload_queue._uploads.clear()


def _recording(out, name="job-20260101-120000.mp4", size=1 << 20):
    path = out / name
    path.write_bytes(b"\0" * size)
    return path


def _entry(name):
    return upload_queue._uploads.get(name)


# Uploading ---------------------------------------------------------------

def test_upload_copies_to_target_and_reports_100_percent(rig):
    out, dst = rig
    path = _recording(out)
    upload_queue.enqueue(path)
    upload_queue._upload(path.name)  # run the worker body inline
    assert (dst / path.name).read_bytes() == path.read_bytes()
    assert _entry(path.name)["status"] == "uploaded"
    assert _entry(path.name)["percent"] == 100
    assert _entry(path.name)["total_bytes"] == path.stat().st_size


def test_delete_local_removes_the_file_only_after_a_confirmed_upload(rig, monkeypatch):
    out, dst = rig
    monkeypatch.setattr(config, "CAMERA_RCLONE_DELETE_LOCAL", True)
    path = _recording(out)
    upload_queue.enqueue(path)
    upload_queue._upload(path.name)
    assert (dst / path.name).exists()
    assert not path.exists()
    # And the entry goes with it, so the panel shows an empty folder rather
    # than a ghost row for a file that is no longer there.
    assert _entry(path.name) is None


def test_failed_upload_keeps_the_file_and_schedules_a_retry(rig, monkeypatch):
    out, _ = rig
    monkeypatch.setattr(config, "CAMERA_RCLONE_TARGET", "no_such_remote:nowhere")
    monkeypatch.setattr(config, "CAMERA_RCLONE_DELETE_LOCAL", True)
    path = _recording(out)
    upload_queue.enqueue(path)
    upload_queue._upload(path.name)
    entry = _entry(path.name)
    assert entry["status"] == "failed"
    assert entry["error"]
    assert entry["attempts"] == 1
    assert entry["retry_at"] > time.time()
    # The whole point: delete-after-upload must not delete what never uploaded.
    assert path.exists()


def test_backoff_grows_with_each_attempt(rig):
    out, _ = rig
    path = _recording(out)
    upload_queue.enqueue(path)
    delays = []
    for _ in range(3):
        upload_queue._fail(path.name, "boom")
        delays.append(_entry(path.name)["retry_at"] - time.time())
    assert delays[0] < delays[1] < delays[2]


# Recovering interrupted uploads -----------------------------------------

def test_sweep_requeues_a_file_left_behind_by_an_interrupted_upload(rig):
    out, _ = rig
    path = _recording(out)
    # Older than the settle window, i.e. nothing is still writing it.
    old = time.time() - 3600
    os.utime(path, (old, old))
    upload_queue.sweep()
    assert _entry(path.name)["status"] == "queued"


def test_sweep_leaves_a_file_that_is_still_being_written(rig):
    out, _ = rig
    path = _recording(out)  # mtime is now
    upload_queue.sweep()
    assert _entry(path.name) is None


def test_sweep_forgets_entries_whose_file_is_gone(rig):
    out, _ = rig
    path = _recording(out)
    upload_queue.enqueue(path)
    path.unlink()
    upload_queue.sweep()
    assert _entry(path.name) is None


def test_sweep_does_not_touch_camera_scratch(rig):
    out, _ = rig
    (out / "_optical_reg_preview.jpg").write_bytes(b"x")
    (out / "_segments").mkdir()
    old = time.time() - 3600
    os.utime(out / "_optical_reg_preview.jpg", (old, old))
    upload_queue.sweep()
    assert upload_queue._uploads == {}


def test_nothing_is_queued_without_a_target(rig, monkeypatch):
    out, _ = rig
    monkeypatch.setattr(config, "CAMERA_RCLONE_TARGET", "")
    path = _recording(out)
    upload_queue.enqueue(path)
    upload_queue.sweep()
    assert upload_queue._uploads == {}


# Listing and name handling ----------------------------------------------

def test_list_recordings_reports_the_folder_and_target(rig):
    out, dst = rig
    _recording(out, "a-20260101-120000.mp4")
    _recording(out, "b-20260101-130000.mp4")
    (out / "_optical_reg_preview.jpg").write_bytes(b"x")
    listing = upload_queue.list_recordings()
    assert listing["rclone_target"] == str(dst)
    assert listing["delete_local"] is False
    assert [r["name"] for r in listing["recordings"]] == [
        "b-20260101-130000.mp4", "a-20260101-120000.mp4"]  # newest first
    assert all(r["upload_status"] == "idle" for r in listing["recordings"])


@pytest.mark.parametrize("name", [
    "../../etc/passwd", "sub/dir.mp4", "..", ".hidden.mp4", "",
    "_optical_reg_preview.jpg", "notes.txt",
])
def test_path_for_rejects_anything_but_a_recording_in_the_folder(rig, name):
    with pytest.raises(ValueError):
        upload_queue.path_for(name)


def test_delete_removes_the_file_and_its_entry(rig):
    out, _ = rig
    path = _recording(out)
    upload_queue.enqueue(path)
    upload_queue.delete(path.name)
    assert not path.exists()
    assert _entry(path.name) is None


# HTTP routes -------------------------------------------------------------

def test_recordings_route_lists_the_folder(rig, client):
    out, _ = rig
    _recording(out, "a-20260101-120000.mp4")
    body = client.get("/camera/recordings").json()
    assert [r["name"] for r in body["recordings"]] == ["a-20260101-120000.mp4"]


def test_preview_route_serves_the_file_inline(rig, client):
    out, _ = rig
    path = _recording(out, size=1024)
    res = client.get(f"/camera/recordings/{path.name}")
    assert res.status_code == 200
    assert res.headers["content-type"] == "video/mp4"
    assert "inline" in res.headers["content-disposition"]
    assert res.content == path.read_bytes()


@pytest.mark.parametrize("name", ["..%2F..%2Fetc%2Fpasswd", "_optical_reg_preview.jpg"])
def test_preview_route_refuses_anything_outside_the_folder(rig, client, name):
    assert client.get(f"/camera/recordings/{name}").status_code == 404


def test_delete_route_removes_the_file(rig, client):
    out, _ = rig
    path = _recording(out)
    assert client.delete(f"/camera/recordings/{path.name}").status_code == 200
    assert not path.exists()


def test_upload_now_route_queues_the_file(rig, client):
    out, _ = rig
    path = _recording(out)
    assert client.post(f"/camera/recordings/{path.name}/upload").status_code == 200
    assert _entry(path.name)["status"] == "queued"
