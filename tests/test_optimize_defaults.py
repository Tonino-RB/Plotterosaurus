"""The pre-optimization trigger contract.

Two guarantees, stated here because no other module owns them and both have
quiet failure modes (a vpype run nobody asked for; a stale ``.opt.svg`` that
outlives the toggle that made it):

1. Nothing optimizes on upload or job-create unless the pre-opt *defaults* say
   Optimize is on. The background prefetch that makes Plot an instant cache hit
   is kept — but only when ``config.OPTIMIZE_SVG_DEFAULT`` is set. With it off,
   an upload and its auto-created job run zero vpype.
2. Clearing "Optimize SVG" (or its last sub-option) reverts. The preview, the
   plot and the export all fall back to the uploaded drawing the moment the
   toggle goes off, whatever ``.opt.svg`` / ``optimized_with_key`` is left on
   disk or on the record.

These already hold; the tests pin them. ``config.json`` is not sandboxed, so the
default is moved with ``monkeypatch.setattr(config, ...)``, never
``config.update()`` (see tests/test_camera_recording.py). The ``client`` fixture
starts no lifespan, so neither queue's worker thread runs and
``optimize_queue._pending`` is safe to read; nothing drains it, so every test
filters it by its own ``svg_id`` and cleans up after itself.
"""
from pathlib import Path

import pytest

from app import config, main, optimize_queue, plan_queue, plot_worker, state

FIXTURE = Path(__file__).parent / "fixtures" / "multi-layer.svg"


def _place_source(svg_id: str) -> Path:
    path = main.UPLOAD_DIR / f"{svg_id}.svg"
    path.write_bytes(FIXTURE.read_bytes())
    return path


def _pending_for(svg_id: str) -> list:
    with optimize_queue._lock:
        return [t for t in optimize_queue._pending if t.svg_id == svg_id]


def _cleanup(svg_id: str) -> None:
    optimize_queue.cancel(svg_id)  # drops pending tasks + clears the svg status
    state.clear_svg_status(f"{svg_id}:grid")
    for leftover in main.UPLOAD_DIR.glob(f"{svg_id}*"):
        leftover.unlink(missing_ok=True)


# --- Guarantee 1: nothing optimizes unless the default is on ------------------

def test_enqueue_for_upload_is_a_noop_when_the_default_is_off(monkeypatch):
    monkeypatch.setattr(config, "OPTIMIZE_SVG_DEFAULT", False)
    svg_id = "_optdef_up_off"
    _place_source(svg_id)
    try:
        optimize_queue.enqueue_for_upload(svg_id)
        assert _pending_for(svg_id) == []
        assert state.get_svg_status(svg_id) is None
    finally:
        _cleanup(svg_id)


def test_enqueue_for_upload_prefetches_when_the_default_is_on(monkeypatch):
    """The other half of guarantee 1: with the default on, the upload-time
    prefetch still fires — otherwise Plot silently stops being an instant cache
    hit and nothing would catch it."""
    monkeypatch.setattr(config, "OPTIMIZE_SVG_DEFAULT", True)
    svg_id = "_optdef_up_on"
    _place_source(svg_id)
    try:
        optimize_queue.enqueue_for_upload(svg_id)
        expected_key = optimize_queue.settings_key(
            optimize_queue.settings_from_config())
        mine = _pending_for(svg_id)
        assert len(mine) == 1
        assert mine[0].kind == "upload"
        assert mine[0].settings_key == expected_key
        st = state.get_svg_status(svg_id)
        assert st and st["status"] == "pending"
        assert st["settings_key"] == expected_key
    finally:
        _cleanup(svg_id)


def test_bootstrap_from_disk_is_a_noop_when_the_default_is_off(monkeypatch):
    monkeypatch.setattr(config, "OPTIMIZE_SVG_DEFAULT", False)
    svg_id = "_optdefbootoff"  # dot-free: bootstrap skips stems containing "."
    _place_source(svg_id)
    try:
        optimize_queue.bootstrap_from_disk()
        assert _pending_for(svg_id) == []
        assert state.get_svg_status(svg_id) is None
    finally:
        _cleanup(svg_id)


def test_bootstrap_from_disk_prefetches_when_the_default_is_on(monkeypatch):
    monkeypatch.setattr(config, "OPTIMIZE_SVG_DEFAULT", True)
    svg_id = "_optdefbooton"
    _place_source(svg_id)
    with optimize_queue._lock:
        before = {id(t) for t in optimize_queue._pending}
    try:
        optimize_queue.bootstrap_from_disk()
        mine = _pending_for(svg_id)
        assert len(mine) == 1
        assert mine[0].settings_key == optimize_queue.settings_key(
            optimize_queue.settings_from_config())
    finally:
        # bootstrap scans the whole sandbox uploads dir; undo every task it
        # added, not just this drawing's, so nothing leaks into later tests.
        with optimize_queue._lock:
            added = {t.svg_id for t in optimize_queue._pending
                     if id(t) not in before}
        for sid in added:
            _cleanup(sid)


def test_new_job_with_optimize_omitted_does_not_optimize(client, monkeypatch):
    """End to end: default off, upload, create a job without any optimize field.
    No task is queued for the drawing, no .opt.svg is written, and every read
    path resolves to the raw upload."""
    monkeypatch.setattr(config, "OPTIMIZE_SVG_DEFAULT", False)
    up = client.post("/upload", files={
        "file": ("multi-layer.svg", FIXTURE.read_bytes(), "image/svg+xml")})
    assert up.status_code == 200, up.text
    svg_id = up.json()["id"]
    layers = up.json()["layers"]
    res = client.post("/jobs", json={
        "svg_id": svg_id, "filename": "multi-layer.svg",
        "layer_selections": [{"index": l["index"], "label": l["label"]}
                             for l in layers],
        "paper_width_mm": 210.0, "paper_height_mm": 297.0,
    })
    assert res.status_code == 200, res.text
    job = res.json()
    try:
        assert job["optimize_svg"] is False
        assert not (main.UPLOAD_DIR / f"{svg_id}.opt.svg").exists()
        assert _pending_for(svg_id) == []
        assert plot_worker._effective_svg_path(job) \
            == main.UPLOAD_DIR / f"{svg_id}.svg"
    finally:
        plan_queue.cancel(job["job_id"])
        state.remove_job(job["job_id"])
        state.drop_upload_meta(svg_id)
        _cleanup(svg_id)


@pytest.mark.parametrize("default", [True, False])
def test_raw_post_jobs_does_not_optimize_even_with_the_default_on(
        client, monkeypatch, default):
    """POST /jobs with the optimize fields omitted takes _OptimizeCreateFields'
    own conservative defaults (master off, sub-toggles on) regardless of the
    Settings default. The browser's buildJobPayload is what injects the Settings
    values into the request; the endpoint itself never consults config. This is
    the wall that keeps a default-on install from optimizing a bare API POST it
    was not asked to."""
    monkeypatch.setattr(config, "OPTIMIZE_SVG_DEFAULT", default)
    up = client.post("/upload", files={
        "file": ("multi-layer.svg", FIXTURE.read_bytes(), "image/svg+xml")})
    assert up.status_code == 200, up.text
    svg_id = up.json()["id"]
    res = client.post("/jobs", json={
        "svg_id": svg_id, "filename": "multi-layer.svg",
        "layer_selections": [{"index": l["index"], "label": l["label"]}
                             for l in up.json()["layers"]],
        "paper_width_mm": 210.0, "paper_height_mm": 297.0,
    })
    assert res.status_code == 200, res.text
    job = res.json()
    try:
        assert job["optimize_svg"] is False
        assert job["optimize_svg_linemerge"] is True
        assert job["optimize_svg_linesimplify"] is True
        assert job["optimize_svg_linesort"] is True
        assert job["optimize_svg_reloop"] is True
        # No job-kind optimize task: enqueue_for_job no-ops on optimize_svg
        # False. (With the default on, the upload-time prefetch still queues its
        # own "upload"-kind task — that half is covered above.)
        assert [t for t in _pending_for(svg_id) if t.kind == "job"] == []
    finally:
        plan_queue.cancel(job["job_id"])
        state.remove_job(job["job_id"])
        state.drop_upload_meta(svg_id)
        _cleanup(svg_id)


# --- Guarantee 2: clearing an option reverts ---------------------------------

def test_unchecking_optimize_reverts_effective_path_to_the_raw_upload(
        client, job_from_svg):
    """With Optimize on and its .opt.svg built, downstream reads the optimized
    file. PATCH the master toggle off and every read path swings straight back
    to the uploaded drawing — the leftover .opt.svg and the now-stale
    optimized_with_key do not get a vote."""
    job = job_from_svg(
        FIXTURE, optimize_svg=True, optimize_svg_linemerge=True,
        optimize_svg_linesimplify=True, optimize_svg_linesort=True,
        optimize_svg_reloop=True, optimize_svg_tolerance_mm=0.10)
    job_id, svg_id = job["job_id"], job["svg_id"]
    raw = main.UPLOAD_DIR / f"{svg_id}.svg"
    opt = main.UPLOAD_DIR / f"{svg_id}.opt.svg"
    try:
        settings = optimize_queue.settings_from_job(state.get_job(job_id))
        key = optimize_queue.settings_key(settings)
        task = optimize_queue._Task(svg_id, settings, key, "job")
        optimize_queue._process(task)
        assert task.ok
        state.update_job(job_id, optimized_with_key=key)

        assert opt.exists()
        assert plot_worker._effective_svg_path(state.get_job(job_id)) == opt

        r = client.patch(f"/jobs/{job_id}", json={"optimize_svg": False})
        assert r.status_code == 200, r.text
        assert r.json()["optimize_svg"] is False

        fresh = state.get_job(job_id)
        assert plot_worker._effective_svg_path(fresh) == raw
        assert opt.exists()  # revert does not delete the derivative
        assert fresh["optimized_with_key"] == key  # nor clear the stale key
        assert client.get(f"/jobs/{job_id}/svg").content == raw.read_bytes()
    finally:
        plan_queue.cancel(job_id)
        optimize_queue.cancel(svg_id)
        state.clear_svg_status(f"{svg_id}:grid")


def test_unchecking_one_optimize_sub_toggle_invalidates_the_opt_cache_key():
    """Master still on, one sub-option off: the optimize cache key moves, so
    _run_optimize_phase's reuse guard (optimized_with_key == cache_key) misses
    and the reduced pipeline re-runs instead of the stale .opt.svg being
    served. Master off collapses to the canonical all-off key."""
    base = {
        "optimize_mode": "beginner", "optimize_svg": True,
        "optimize_svg_linemerge": True, "optimize_svg_linesimplify": True,
        "optimize_svg_linesort": True, "optimize_svg_reloop": True,
        "optimize_svg_tolerance_mm": 0.10, "grid_enabled": False,
    }
    key_all_on = plot_worker._optimize_cache_key(base)

    for sub in ("optimize_svg_linemerge", "optimize_svg_linesimplify",
                "optimize_svg_linesort", "optimize_svg_reloop"):
        assert plot_worker._optimize_cache_key({**base, sub: False}) != key_all_on
    assert plot_worker._optimize_cache_key(
        {**base, "optimize_svg_tolerance_mm": 0.25}) != key_all_on

    stale = {**base, "optimize_svg_linesort": False,
             "optimized_with_key": key_all_on}
    assert plot_worker._optimize_cache_key(stale) != stale["optimized_with_key"]

    off = plot_worker._optimize_cache_key({**base, "optimize_svg": False})
    assert off != key_all_on
    assert off == optimize_queue.settings_key(
        optimize_queue.settings_from_job(
            {"optimize_svg": False, "optimize_svg_tolerance_mm": 0.10}))
