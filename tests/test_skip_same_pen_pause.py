"""The "don't pause between same-pen layers" job option.

`pause_between_layers` stops the plot at every layer boundary so a pen can be
swapped. When two consecutive layers are drawn with the same pen — same stroke
colour *and* width — there is nothing to swap, and `skip_same_pen_pause` rolls
straight on instead of parking in `awaiting_pen_change`.

Two things carry that: `svg_utils.layer_pens` reads a representative
`(colour, width)` off each layer of the un-optimized source, and
`plot_worker._same_tool` decides whether a boundary is skippable from the pens
of the two stages either side. The gate itself in `_run_staged_loop_impl` is one
`and not skip_pause` clause; it is reconstructed here (the loop touches
hardware) the same way `test_layer_order.py` reconstructs the stage builder.
"""
from pathlib import Path

import pytest

from app import plot_worker, svg_utils

# Widths as the pipeline sees them: unitless stroke-width is CSS px, converted
# to document mm via PX_PER_MM and rounded to 2dp (see svg_utils._stroke_width_mm).
W2 = round(2 / svg_utils.PX_PER_MM, 2)
W4 = round(4 / svg_utils.PX_PER_MM, 2)

# alpha / bravo: pen A (bravo re-declares it via inline style + an mm unit that
# lands on the same rounded value). charlie: pen A's colour, wider. delta: pen
# A's width, red. echo: pen declared once on the layer <g>. foxtrot: fill only.
PEN_RUNS_SVG = f"""<svg xmlns="http://www.w3.org/2000/svg"
  xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
  width="160mm" height="90mm" viewBox="0 0 160 90">
  <g inkscape:groupmode="layer" inkscape:label="alpha">
    <path d="M10 10 L60 40" fill="none" stroke="#1a1a1a" stroke-width="2"/>
    <path d="M10 20 L60 50" fill="none" stroke="#1a1a1a" stroke-width="2"/>
  </g>
  <g inkscape:groupmode="layer" inkscape:label="bravo">
    <path d="M10 30 L60 60" fill="none" style="stroke:#1a1a1a;stroke-width:{W2}mm"/>
  </g>
  <g inkscape:groupmode="layer" inkscape:label="charlie">
    <path d="M10 40 L60 70" fill="none" stroke="#1a1a1a" stroke-width="4"/>
  </g>
  <g inkscape:groupmode="layer" inkscape:label="delta">
    <path d="M10 50 L60 80" fill="none" stroke="#b22222" stroke-width="2"/>
  </g>
  <g inkscape:groupmode="layer" inkscape:label="echo" stroke="#1a1a1a" stroke-width="2">
    <path d="M80 10 L140 40" fill="none"/>
  </g>
  <g inkscape:groupmode="layer" inkscape:label="foxtrot">
    <rect x="80" y="50" width="40" height="20" fill="#3cb371"/>
  </g>
</svg>"""


@pytest.fixture
def pen_runs(tmp_path) -> Path:
    p = tmp_path / "pen-runs.svg"
    p.write_text(PEN_RUNS_SVG)
    return p


# svg_utils.layer_pens -----------------------------------------------------

def test_layer_pens_reads_colour_and_width_per_layer(pen_runs):
    pens = svg_utils.layer_pens(pen_runs)
    assert pens == {
        0: ("#1a1a1a", W2),   # alpha  — attributes
        1: ("#1a1a1a", W2),   # bravo  — inline style, mm unit, same rounded pen
        2: ("#1a1a1a", W4),   # charlie — same colour, wider
        3: ("#b22222", W2),   # delta  — same width, red
        4: ("#1a1a1a", W2),   # echo   — pen on the layer <g>, child bare
        5: None,              # foxtrot — nothing stroked
    }


def test_layer_pens_with_styles_folds_in_the_layer_panel_overrides(pen_runs):
    pens = svg_utils.layer_pens(pen_runs)
    # charlie (2) is pen A's colour but wider; delta (3) is pen A's width but
    # red. Overriding each back to pen A makes both match alpha/bravo.
    styles = [
        {"index": 2, "stroke_width_mm": W2},
        {"index": 3, "stroke": "#1a1a1a"},
    ]
    merged = svg_utils.layer_pens_with_styles(pens, styles)
    assert merged[2] == ("#1a1a1a", W2)
    assert merged[3] == ("#1a1a1a", W2)
    assert merged[0] == pens[0]          # untouched layers pass through


def test_layer_pens_with_styles_needs_both_halves_to_resolve(pen_runs):
    pens = svg_utils.layer_pens(pen_runs)
    # foxtrot (5) has no measured pen; a colour-only override can't complete the
    # pair, so it stays None and the pause is kept.
    assert svg_utils.layer_pens_with_styles(
        pens, [{"index": 5, "stroke": "#1a1a1a"}])[5] is None
    # width too -> now a full pair.
    assert svg_utils.layer_pens_with_styles(
        pens, [{"index": 5, "stroke": "#1a1a1a", "stroke_width_mm": W2}])[5] \
        == ("#1a1a1a", W2)


def test_layer_pens_with_styles_passthrough_and_bad_input(pen_runs):
    pens = svg_utils.layer_pens(pen_runs)
    assert svg_utils.layer_pens_with_styles(pens, None) == pens
    assert svg_utils.layer_pens_with_styles(pens, []) == pens
    # unknown index / malformed entry are ignored; a named colour can't resolve
    # so it keeps the measured colour.
    merged = svg_utils.layer_pens_with_styles(
        pens, [{"index": 99, "stroke": "#000000"}, {"stroke": "#000000"},
               {"index": 3, "stroke": "not-a-hex"}])
    assert merged[3] == pens[3]


def test_an_override_can_flip_a_boundary_either_way(pen_runs):
    job = {"pause_between_layers": True, "skip_same_pen_pause": True}
    pens = svg_utils.layer_pens_with_styles(
        svg_utils.layer_pens(pen_runs),
        # repaint alpha so alpha->bravo now differs (that boundary comes back);
        # narrow charlie to pen A's width so bravo->charlie now matches (skip).
        [{"index": 0, "stroke": "#00ff00"}, {"index": 2, "stroke_width_mm": W2}])
    styled = [{"layer_indices": [i], "pen": pens.get(i)} for i in range(len(pens))]
    assert _boundary_pauses(job, styled) == [True, False, True, True, True]


def test_layer_pens_ignores_a_pen_carried_only_by_a_css_class(tmp_path):
    """No stylesheet cascade in the lxml read — a class-only pen is unresolvable,
    which keeps the pause (the safe direction)."""
    p = tmp_path / "class-pen.svg"
    p.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape='
        '"http://www.inkscape.org/namespaces/inkscape" width="80mm" height="60mm" '
        'viewBox="0 0 80 60"><style>.ink{stroke:#000;stroke-width:1}</style>'
        '<g inkscape:groupmode="layer" inkscape:label="only">'
        '<path class="ink" d="M0 0 L40 40"/></g></svg>')
    assert svg_utils.layer_pens(p) == {0: None}


# plot_worker._same_tool -------------------------------------------------

@pytest.mark.parametrize("a,b,expected", [
    (("#000000", 0.3), ("#000000", 0.3), True),
    (("#000000", 0.3), ("#000000", 0.5), False),   # width differs
    (("#000000", 0.3), ("#ff0000", 0.3), False),   # colour differs
    (("#000000", 0.3), None, False),               # next pen unresolved
    (None, ("#000000", 0.3), False),               # prev pen unresolved
    (None, None, False),                           # neither resolved
])
def test_same_tool(a, b, expected):
    assert plot_worker._same_tool(a, b) is expected


# The boundary gate (reconstructed) ------------------------------------

def _boundary_pauses(job: dict, stages: list[dict]) -> list[bool]:
    """Whether each boundary i -> i+1 enters awaiting_pen_change, replicating
    the guard in plot_worker._run_staged_loop_impl."""
    out = []
    for i in range(len(stages) - 1):
        skip_pause = (job.get("skip_same_pen_pause")
                      and plot_worker._same_tool(stages[i].get("pen"),
                                                 stages[i + 1].get("pen")))
        out.append(bool(job.get("pause_between_layers", True))
                   and len(stages) > 1 and not skip_pause)
    return out


def _per_layer_stages(pen_runs: Path) -> list[dict]:
    pens = svg_utils.layer_pens(pen_runs)
    return [{"layer_indices": [i], "pen": pens.get(i)} for i in range(len(pens))]


def test_only_same_pen_boundaries_are_skipped(pen_runs):
    stages = _per_layer_stages(pen_runs)
    job = {"pause_between_layers": True, "skip_same_pen_pause": True}
    # alpha->bravo same pen (skip); bravo->charlie width; charlie->delta colour;
    # delta->echo both differ; echo->foxtrot foxtrot pen is None.
    assert _boundary_pauses(job, stages) == [False, True, True, True, True]


def test_option_off_keeps_every_pause(pen_runs):
    stages = _per_layer_stages(pen_runs)
    job = {"pause_between_layers": True, "skip_same_pen_pause": False}
    assert _boundary_pauses(job, stages) == [True, True, True, True, True]


def test_no_pauses_at_all_when_pause_between_layers_is_off(pen_runs):
    stages = _per_layer_stages(pen_runs)
    job = {"pause_between_layers": False, "skip_same_pen_pause": True}
    assert _boundary_pauses(job, stages) == [False, False, False, False, False]
