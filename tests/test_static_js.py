"""Syntax check for the browser code.

static/app.js is ~3,600 lines of vanilla JavaScript with no build step, which
means nothing between an editor and a user's browser ever looks at it. A stray
brace ships silently and the whole UI is dead on load. This is the cheapest
possible guard against that: compile the file and see if the engine objects.

It is a *syntax* check, not a test of behaviour — quickjs has no DOM, so the
source is wrapped in a function that is compiled and never called.
"""
from pathlib import Path

import pytest

quickjs = pytest.importorskip(
    "quickjs",
    reason="quickjs not installed — pip install -r requirements-dev.txt",
)

STATIC = Path(__file__).parent.parent / "static"


@pytest.mark.parametrize("name", sorted(p.name for p in STATIC.glob("*.js")))
def test_javascript_parses(name: str) -> None:
    source = (STATIC / name).read_text()
    # Wrapped so top-level declarations are compiled but nothing executes;
    # there is no window or document here to execute against.
    quickjs.Context().eval("(function(){\n" + source + "\n})")


# effectivePlacement ------------------------------------------------------
#
# This one function does get executed, because it is the only geometry the
# browser still does for itself and a sign error in it would go unnoticed by
# everything else here. It is lifted out of the file by name rather than
# reimplemented, so what runs is the shipped source.

def _extract_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    depth, i = 0, source.index("{", start)
    for end in range(i, len(source)):
        if source[end] == "{":
            depth += 1
        elif source[end] == "}":
            depth -= 1
            if depth == 0:
                return source[start:end + 1]
    raise AssertionError(f"unbalanced braces in {name}")


# A placement answer as the server returns it, computed at scale 1 / offset 0.
BASE = """
const cardCtx = new Map();
cardCtx.set("j", {
  placement: {
    rotation_deg: 20, fit_scale: 0.5, user_scale: 0.5,
    layout_width_mm: 100, layout_height_mm: 80,
    footprint_width_mm: 40, footprint_height_mm: 30,
    center_x_mm: 20, center_y_mm: 15,
  },
  placementAt: { scale: 1, offx: 0, offy: 0 },
});
"""


def evaluate(job: str) -> dict:
    import json

    source = (STATIC / "app.js").read_text()
    ctx = quickjs.Context()
    ctx.eval(BASE + "\n" + _extract_function(source, "effectivePlacement"))
    return json.loads(ctx.eval(f"JSON.stringify(effectivePlacement({job}))"))


def test_an_unmoved_editor_renders_the_servers_answer_untouched():
    out = evaluate('{job_id:"j", transform_scale:1, '
                   'transform_offset_x_mm:0, transform_offset_y_mm:0}')
    assert (out["center_x_mm"], out["center_y_mm"]) == (20, 15)
    assert (out["footprint_width_mm"], out["footprint_height_mm"]) == (40, 30)


def test_offset_translates_the_cached_answer():
    """The drag the regression report was about: sliding X has to move the
    artwork now, not when the mouse is released."""
    out = evaluate('{job_id:"j", transform_scale:1, '
                   'transform_offset_x_mm:12, transform_offset_y_mm:-7}')
    assert (out["center_x_mm"], out["center_y_mm"]) == (32, 8)
    # A translation and nothing else.
    assert (out["footprint_width_mm"], out["footprint_height_mm"]) == (40, 30)
    assert out["rotation_deg"] == 20


def test_scale_grows_the_footprint_from_its_pinned_corner():
    """Matches the engine: the footprint's top-left is the fixed point of a
    scale change (test_placement_engine.py pins this on the Python side)."""
    out = evaluate('{job_id:"j", transform_scale:2, '
                   'transform_offset_x_mm:0, transform_offset_y_mm:0}')
    assert (out["footprint_width_mm"], out["footprint_height_mm"]) == (80, 60)
    # left = center - w/2 was 0 and stays 0; top was 0 and stays 0.
    assert out["center_x_mm"] - out["footprint_width_mm"] / 2 == 0
    assert out["center_y_mm"] - out["footprint_height_mm"] / 2 == 0


def test_scale_and_offset_compose():
    out = evaluate('{job_id:"j", transform_scale:2, '
                   'transform_offset_x_mm:5, transform_offset_y_mm:5}')
    assert (out["center_x_mm"], out["center_y_mm"]) == (45, 35)


def test_without_a_server_answer_there_is_nothing_to_render():
    """The preview must not invent a placement — it draws nothing until the
    server has spoken once."""
    source = (STATIC / "app.js").read_text()
    ctx = quickjs.Context()
    ctx.eval("const cardCtx = new Map();\n"
             + _extract_function(source, "effectivePlacement"))
    assert ctx.eval('effectivePlacement({job_id:"missing"}) === null') is True
