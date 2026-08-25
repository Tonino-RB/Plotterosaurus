"""The suite must not touch the running plotter's data.

This is here because it already went wrong. `app.state` persists to the repo's
own state.json, and pytest never calls `state.init()`, so the in-memory queue
starts empty and the first add/remove writes that empty queue straight over
the live one. A real queue of real jobs was destroyed that way.

What made it survive so long unnoticed is that the service keeps state in
memory and rewrites the file on its next change, so the damage is invisible
until a restart — by which time the test run that caused it is long finished
and looks unrelated.

`_sandbox_server_state` in conftest.py redirects the paths. These assert it
actually took, because a fixture that silently stops working reintroduces the
exact failure it was written to prevent.
"""
import json
from pathlib import Path

from app import main, optimize_queue, plot_worker, state

REPO = Path(__file__).resolve().parent.parent


def test_state_is_not_the_live_state_file():
    assert state.STATE_PATH != REPO / "state.json"
    assert REPO not in state.STATE_PATH.parents or "tmp" in str(state.STATE_PATH)


def test_draw_trace_is_not_the_live_trace_file():
    assert state.DRAW_TRACE_PATH != REPO / "draw_trace.jsonl"
    assert REPO not in state.DRAW_TRACE_PATH.parents or "tmp" in str(state.DRAW_TRACE_PATH)


def test_uploads_are_not_the_live_upload_dir():
    live = REPO / "uploads"
    assert main.UPLOAD_DIR != live
    assert state.UPLOAD_DIR != live
    # Both queues cache the path on first use; the fixture seeds that cache, so
    # a lazily-resolved real directory cannot sneak back in.
    assert plot_worker._uploads() != live
    assert optimize_queue._uploads() != live


def test_the_live_state_file_still_parses():
    """A truncated or half-written state.json is the shape the damage took.
    If a run ever corrupts it again, say so here rather than at the next
    service restart."""
    live = REPO / "state.json"
    if not live.exists():
        return
    data = json.loads(live.read_text())
    assert isinstance(data.get("queue"), list)
