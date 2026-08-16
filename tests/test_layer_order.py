"""Layer order, from the panel's ↑/↓ buttons to what the pen draws first.

The order of ``layer_selections`` *is* the plot order — ``_run_job`` builds one
stage per entry, walking the array. So the buttons in the layer panel are not
cosmetic: they decide which pen touches the paper first, which matters whenever
one layer has to be dry before the next goes over it.

That makes three things worth pinning separately: the swap the browser performs,
the order surviving a round trip through the server, and the metadata attached
to a layer travelling with it.
"""
import json
from pathlib import Path

import pytest

from app import plot_worker

STATIC = Path(__file__).parent.parent / "static"

quickjs = pytest.importorskip(
    "quickjs", reason="quickjs not installed — pip install -r requirements-dev.txt")

from .test_static_js import _extract_function  # noqa: E402


# The browser's swap ---------------------------------------------------------

HARNESS = """
let patched = null, rendered = null;
const serverState = { queue: [ { job_id: "j", layer_selections: LAYERS } ] };
function renderLayers(card, job) { rendered = job.layer_selections; }
function queueCardUpdate(card, updates) { patched = updates.layer_selections; }
const card = { dataset: { id: "j" } };
"""


def move(layers, index, delta):
    """Run the shipped moveLayer against `layers` and return what it PATCHes."""
    source = (STATIC / "app.js").read_text()
    ctx = quickjs.Context()
    ctx.eval(HARNESS.replace("LAYERS", json.dumps(layers))
             + "\n" + _extract_function(source, "moveLayer"))
    ctx.eval(f"moveLayer(card, {index}, {delta})")
    return json.loads(ctx.eval("JSON.stringify(patched)"))


def labels(layers):
    return [l["label"] for l in layers]


THREE = [{"index": 0, "label": "outline", "selected": True},
         {"index": 1, "label": "fill", "selected": True},
         {"index": 2, "label": "signature", "selected": True}]


def test_moving_a_layer_up_swaps_it_with_the_one_above():
    assert labels(move(THREE, 2, -1)) == ["outline", "signature", "fill"]


def test_moving_a_layer_down_swaps_it_with_the_one_below():
    assert labels(move(THREE, 0, 1)) == ["fill", "outline", "signature"]


def test_a_move_carries_the_layers_own_settings_with_it():
    """A layer's pen, its type and any per-layer speed override belong to the
    layer, not to the slot. Dropping them on a reorder would silently replot
    it at the job's default speed."""
    layers = [
        {"index": 0, "label": "outline", "selected": True},
        {"index": 1, "label": "gold", "selected": False, "type": "pen",
         "pen_name": "Gold 0.5", "speed_pendown": 12, "acceleration": 40},
    ]
    moved = move(layers, 1, -1)
    assert labels(moved) == ["gold", "outline"]
    assert moved[0] == {"index": 1, "label": "gold", "selected": False,
                        "type": "pen", "pen_name": "Gold 0.5",
                        "speed_pendown": 12, "acceleration": 40}


def test_a_deselected_layer_still_holds_its_place():
    """Unchecked layers stay in the array so their settings survive a toggle,
    which means they also occupy a position. Moving past one is a single
    click, not a silent double hop."""
    layers = [{"index": 0, "label": "a", "selected": True},
              {"index": 1, "label": "hidden", "selected": False},
              {"index": 2, "label": "c", "selected": True}]
    assert labels(move(layers, 2, -1)) == ["a", "c", "hidden"]


@pytest.mark.parametrize("index,delta", [(0, -1), (2, 1)])
def test_moving_past_either_end_does_nothing(index, delta):
    """The buttons are disabled at the ends, but the guard has to hold anyway —
    a wrap-around here would reorder the plot behind the user's back."""
    source = (STATIC / "app.js").read_text()
    ctx = quickjs.Context()
    ctx.eval(HARNESS.replace("LAYERS", json.dumps(THREE))
             + "\n" + _extract_function(source, "moveLayer"))
    ctx.eval(f"moveLayer(card, {index}, {delta})")
    assert ctx.eval("patched === null") is True, "an out-of-range move was sent"


def test_the_reorder_is_written_back_before_the_patch_is_sent():
    """The PATCH is debounced 150ms, so a second click lands before the first
    has gone anywhere. It has to build on the first click's result, not on the
    pre-edit array — otherwise clicking ↑ twice moves a layer one place."""
    source = (STATIC / "app.js").read_text()
    ctx = quickjs.Context()
    ctx.eval(HARNESS.replace("LAYERS", json.dumps(THREE))
             + "\n" + _extract_function(source, "moveLayer"))
    ctx.eval("moveLayer(card, 2, -1)")   # signature: 3rd -> 2nd
    ctx.eval("moveLayer(card, 2, -1)")   # signature: 2nd -> 1st
    final = json.loads(ctx.eval("JSON.stringify(patched)"))
    assert labels(final) == ["signature", "outline", "fill"]


# What the machine actually does ---------------------------------------------

def _job(layers, separate=True):
    return {"job_id": "j", "svg_id": "s", "layer_selections": layers,
            "layer_mode": "separate" if separate else "combined",
            "speed_pendown": 25, "speed_penup": 75, "acceleration": 75}


def test_stage_order_follows_the_panel_not_the_documents_layer_order():
    """The whole point. A drawing whose layers are authored 0,1,2 but ordered
    2,0,1 in the panel must be plotted 2,0,1."""
    layers = [{"index": 2, "label": "signature", "selected": True},
              {"index": 0, "label": "outline", "selected": True},
              {"index": 1, "label": "fill", "selected": True}]
    selections = [s for s in _job(layers)["layer_selections"] if s.get("selected", True)]
    stages = [{"layer_indices": [s["index"]], "labels": [s["label"]]} for s in selections]
    assert [s["layer_indices"][0] for s in stages] == [2, 0, 1]
    assert [s["labels"][0] for s in stages] == ["signature", "outline", "fill"]


def test_deselected_layers_are_dropped_from_the_plot_but_not_the_order():
    layers = [{"index": 2, "label": "signature", "selected": True},
              {"index": 0, "label": "outline", "selected": False},
              {"index": 1, "label": "fill", "selected": True}]
    selections = [s for s in layers if s.get("selected", True)]
    assert [s["index"] for s in selections] == [2, 1]


def test_a_combined_plot_keeps_the_panel_order_too(monkeypatch):
    """Combined mode draws everything in one pass, but the SVG it builds still
    lists layers in the order given — and that is the order the pen visits
    them in within the pass."""
    layers = [{"index": 2, "label": "c", "selected": True},
              {"index": 0, "label": "a", "selected": True}]
    selections = [s for s in layers if s.get("selected", True)]
    combined = [s["index"] for s in selections]
    assert combined == [2, 0]


def test_filter_to_layers_addresses_layers_by_document_position(tmp_path):
    """Reordering the panel must not reorder the *file*. filter_to_layers takes
    document indices, so index 2 has to keep meaning the document's third
    layer however the panel is arranged — otherwise a reorder would silently
    plot different artwork."""
    from lxml import etree

    from app import svg_utils

    path = tmp_path / "three.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape='
        '"http://www.inkscape.org/namespaces/inkscape" width="100mm" '
        'height="100mm" viewBox="0 0 100 100">'
        '<g inkscape:groupmode="layer" inkscape:label="alpha">'
        '<path d="M0 0 L10 10" stroke="#000"/></g>'
        '<g inkscape:groupmode="layer" inkscape:label="beta">'
        '<path d="M20 20 L30 30" stroke="#000"/></g>'
        '<g inkscape:groupmode="layer" inkscape:label="gamma">'
        '<path d="M40 40 L50 50" stroke="#000"/></g></svg>')

    out = tmp_path / "just-gamma.svg"
    svg_utils.filter_to_layers(path, [2], out)
    kept = [g.get(f"{{{svg_utils.INKSCAPE_NS}}}label")
            for g in etree.parse(str(out)).getroot()
            if g.tag == svg_utils.LAYER_TAG]
    assert kept == ["gamma"]


# Surviving a broadcast ------------------------------------------------------

BROADCAST_HARNESS = """
let serverState = { queue: [ { job_id: "j", layer_selections: [
  {index:0,label:"a",selected:true},
  {index:1,label:"b",selected:true},
  {index:2,label:"c",selected:true}] } ] };
const cardUpdateUnconfirmed = new Map();
"""


def test_an_edit_survives_a_state_broadcast():
    """A broadcast replaces serverState wholesale. Reordering layers made that
    visible and then harmful: the panel flicked back to the old order, and
    because the next click reads its starting point out of serverState, a
    second click built on the reverted array and lost the first move.

    Broadcasts are frequent exactly when layers get arranged — right after an
    upload, while the optimize and plan queues step the job through statuses.
    """
    source = (STATIC / "app.js").read_text()
    ctx = quickjs.Context()
    ctx.eval(BROADCAST_HARNESS
             + _extract_function(source, "rememberUnconfirmed") + "\n"
             + _extract_function(source, "applyUnconfirmedEdits"))

    reordered = [{"index": 2, "label": "c", "selected": True},
                 {"index": 0, "label": "a", "selected": True},
                 {"index": 1, "label": "b", "selected": True}]
    ctx.eval(f'rememberUnconfirmed("j", {{layer_selections: {json.dumps(reordered)}}})')

    # The server broadcasts state that predates the edit.
    ctx.eval('serverState = { queue: [ { job_id: "j", layer_selections: ['
             '{index:0,label:"a",selected:true},'
             '{index:1,label:"b",selected:true},'
             '{index:2,label:"c",selected:true}] } ] };')
    ctx.eval("applyUnconfirmedEdits()")

    after = json.loads(ctx.eval(
        "JSON.stringify(serverState.queue[0].layer_selections)"))
    assert labels(after) == ["c", "a", "b"], "the broadcast reverted the reorder"


def test_a_broadcast_for_another_job_is_left_alone():
    """The overlay is per job. A pending edit on one card must not write itself
    onto every other card in the queue."""
    source = (STATIC / "app.js").read_text()
    ctx = quickjs.Context()
    ctx.eval(BROADCAST_HARNESS
             + _extract_function(source, "rememberUnconfirmed") + "\n"
             + _extract_function(source, "applyUnconfirmedEdits"))
    ctx.eval('rememberUnconfirmed("j", {transform_scale: 2})')
    ctx.eval('serverState = { queue: ['
             '{ job_id: "j", transform_scale: 1 },'
             '{ job_id: "other", transform_scale: 1 } ] };')
    ctx.eval("applyUnconfirmedEdits()")
    assert ctx.eval("serverState.queue[0].transform_scale") == 2
    assert ctx.eval("serverState.queue[1].transform_scale") == 1
