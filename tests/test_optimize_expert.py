"""Expert mode's stacking Execute and its Undo.

Execute no longer rebuilds ``{svg_id}.opt.svg`` from the raw upload every time —
it runs the command boxes on top of the current ``.opt.svg`` and snapshots the
previous result to ``{svg_id}.opt.undo.{n}.svg`` so ``undo_last`` can step back
one Execute at a time. ``optimize_expert_undo_depth`` on the job record counts
the steps that remain.

Driven the way test_grid.py drives ``optimize_queue._process``: build the job
by hand, call ``optimize_expert_queue._process`` synchronously with real vpype,
assert on file *bytes* rather than geometry.
"""
import json
from pathlib import Path

import pytest

from app import main, optimize_expert_queue, plan_queue, plot_worker, state, svg_optimize

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE = FIXTURES / "multi-layer.svg"

# A non-idempotent vpype verb: applying it twice is visibly different from once,
# so a stacked Execute cannot be mistaken for a replaced one.
SHIFT = "translate 10mm 0"


@pytest.fixture
def expert_job():
    """A ready, expert-mode job with its raw SVG on disk. Yields (job_id, svg_id)."""
    svg_id = "_expert_undo"
    (main.UPLOAD_DIR / f"{svg_id}.svg").write_bytes(SOURCE.read_bytes())
    rec = state.add_job({
        "svg_id": svg_id, "filename": "multi-layer.svg",
        "layer_selections": [{"index": 0, "label": "art"}],
        "optimize_mode": "expert",
        "paper_width_mm": 210.0, "paper_height_mm": 297.0,
        "margin_top_mm": 0.0, "margin_right_mm": 0.0,
        "margin_bottom_mm": 0.0, "margin_left_mm": 0.0,
    })
    job_id = rec["job_id"]
    try:
        yield job_id, svg_id
    finally:
        plan_queue.cancel(job_id)
        optimize_expert_queue.forget(job_id)
        state.remove_job(job_id)
        for p in main.UPLOAD_DIR.glob(f"{svg_id}*"):
            p.unlink()


def _execute(job_id: str, svg_id: str, *cmds: str) -> None:
    """Run one Execute with the given command box(es), synchronously."""
    raw = main.UPLOAD_DIR / f"{svg_id}.svg"
    boxes = list(cmds) + [""] * (3 - len(cmds))
    optimize_expert_queue._process(
        optimize_expert_queue._Task(job_id, svg_id, raw, boxes))


def _depth(job_id: str) -> int:
    return state.get_job(job_id).get("optimize_expert_undo_depth", 0)


def _snapshots(svg_id: str) -> list[Path]:
    return sorted(main.UPLOAD_DIR.glob(f"{svg_id}.opt.undo.*.svg"))


def _opt(svg_id: str) -> Path:
    return main.UPLOAD_DIR / f"{svg_id}.opt.svg"


# --- Execute stacks --------------------------------------------------------

def test_first_execute_reads_the_raw_upload(expert_job):
    job_id, svg_id = expert_job
    _execute(job_id, svg_id, SHIFT)

    assert _opt(svg_id).exists()
    assert _depth(job_id) == 1
    assert _snapshots(svg_id) == []


def test_second_execute_stacks_and_snapshots_the_first_result(expert_job):
    job_id, svg_id = expert_job
    _execute(job_id, svg_id, SHIFT)
    b1 = _opt(svg_id).read_bytes()

    _execute(job_id, svg_id, SHIFT)
    b2 = _opt(svg_id).read_bytes()

    assert b2 != b1                                   # shifted again, not rebuilt
    assert _depth(job_id) == 2
    snaps = _snapshots(svg_id)
    assert [p.name for p in snaps] == [f"{svg_id}.opt.undo.2.svg"]
    assert snaps[0].read_bytes() == b1               # the level-1 result, verbatim


# --- Undo ----------------------------------------------------------------

def test_undo_restores_the_previous_execute_verbatim(expert_job):
    job_id, svg_id = expert_job
    _execute(job_id, svg_id, SHIFT)
    b1 = _opt(svg_id).read_bytes()
    _execute(job_id, svg_id, SHIFT)

    new_depth = optimize_expert_queue.undo_last(
        job_id, svg_id, main.UPLOAD_DIR / f"{svg_id}.svg")

    assert new_depth == 1
    assert _depth(job_id) == 1
    assert _opt(svg_id).read_bytes() == b1
    assert _snapshots(svg_id) == []                   # snapshot consumed


def test_undo_past_the_first_execute_returns_to_the_raw_upload(expert_job):
    job_id, svg_id = expert_job
    _execute(job_id, svg_id, SHIFT)

    assert optimize_expert_queue.undo_last(
        job_id, svg_id, main.UPLOAD_DIR / f"{svg_id}.svg") == 0
    assert _depth(job_id) == 0
    assert not _opt(svg_id).exists()

    job = state.get_job(job_id)
    assert plot_worker._effective_svg_path(job) == main.UPLOAD_DIR / f"{svg_id}.svg"


def test_undo_with_nothing_to_undo_returns_none(expert_job):
    job_id, svg_id = expert_job
    assert optimize_expert_queue.undo_last(
        job_id, svg_id, main.UPLOAD_DIR / f"{svg_id}.svg") is None


# --- A failed / cancelled Execute is a no-op ----------------------------

def test_a_failed_execute_rolls_back_to_the_previous_level(expert_job):
    job_id, svg_id = expert_job
    _execute(job_id, svg_id, SHIFT)
    b1 = _opt(svg_id).read_bytes()

    _execute(job_id, svg_id, "notarealvpypeverb")     # vpype exits non-zero

    assert _opt(svg_id).read_bytes() == b1            # level 1 intact
    assert _depth(job_id) == 1
    assert _snapshots(svg_id) == []                   # no orphan snapshot


def test_a_cancelled_execute_touches_nothing(expert_job):
    job_id, svg_id = expert_job
    _execute(job_id, svg_id, SHIFT)
    b1 = _opt(svg_id).read_bytes()

    raw = main.UPLOAD_DIR / f"{svg_id}.svg"
    task = optimize_expert_queue._Task(job_id, svg_id, raw, [SHIFT, "", ""])
    task.cancel_event.set()
    optimize_expert_queue._process(task)

    assert _opt(svg_id).read_bytes() == b1
    assert _depth(job_id) == 1
    assert _snapshots(svg_id) == []


# --- Persistence -------------------------------------------------------

def test_undo_depth_is_persisted_to_state_json(expert_job):
    job_id, svg_id = expert_job
    _execute(job_id, svg_id, SHIFT)
    _execute(job_id, svg_id, SHIFT)

    on_disk = json.loads(Path(state.STATE_PATH).read_text())
    row = next(j for j in on_disk["queue"] if j["job_id"] == job_id)
    assert row["optimize_expert_undo_depth"] == 2


# --- Library exclusion ----------------------------------------------------

def test_undo_snapshots_are_not_library_entries(expert_job, client):
    job_id, svg_id = expert_job
    _execute(job_id, svg_id, SHIFT)
    _execute(job_id, svg_id, SHIFT)
    assert _snapshots(svg_id)                         # snapshot really is on disk

    entries = client.get("/library").json()["entries"]
    ours = [e for e in entries if e["svg_id"] == svg_id]
    assert {e["key"] for e in ours} == {svg_id, f"{svg_id}:opt"}
    assert not any(".opt.undo." in e["key"] for e in entries)


# --- Endpoint guard -----------------------------------------------------

def test_undo_endpoint_400s_when_there_is_nothing_to_undo(expert_job, client):
    job_id, _ = expert_job
    res = client.post(f"/jobs/{job_id}/optimize-expert/undo")
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "nothing_to_undo"
