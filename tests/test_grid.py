"""The "Grid" layout module: arrangement maths + the pure-SVG tiling pass.

Grid is a reversible per-job derivative (like Optimize SVG): ``grid_svg`` deep-
copies the drawing into each cell with one SVG transform, so curves and stroke
styling survive — the only change is the layout. These tests pin the arrangement
helper and ``{svg_id}.grid.svg``; the reversibility wiring is covered where the
``_effective_svg_path`` contract lives.
"""
from pathlib import Path

import pytest
from lxml import etree

from app import optimize_queue, placement, state, svg_optimize, svg_utils

FIXTURES = Path(__file__).parent / "fixtures"

# A curve that must not be flattened, a butt-capped stroke the tiler forces
# round, and a shared <defs> gradient. Built inline rather than as a fixture
# file so it stays out of the placement corpus (placement_cases.py globs
# tests/fixtures/*.svg). Non-square (120 x 80) so a rotation is visible.
_CURVE_SRC = (
    '<svg xmlns="http://www.w3.org/2000/svg"'
    ' xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"'
    ' width="120mm" height="80mm" viewBox="0 0 120 80">'
    '<defs><linearGradient id="fade"><stop offset="0" stop-color="#000"/>'
    '</linearGradient></defs>'
    '<g inkscape:groupmode="layer" inkscape:label="sweep">'
    '<path d="M10 40 C 30 5, 90 5, 110 40 S 90 75 10 40 Z" fill="none"'
    ' stroke="#111" stroke-width="0.6" stroke-linecap="butt"/></g>'
    '<g inkscape:groupmode="layer" inkscape:label="ring">'
    '<circle cx="60" cy="40" r="18" fill="none" stroke="#222"/></g>'
    '</svg>'
)


def _curve_src(tmp_path: Path) -> Path:
    p = tmp_path / "curve-src.svg"
    p.write_text(_CURVE_SRC)
    return p


# --- arrangement() ------------------------------------------------------------

@pytest.mark.parametrize("copies, paper, content, expected", [
    # A3 portrait, 4 copies of an A5-ish card -> 2x2 (each cell is A5), upright.
    (4, (297, 420), (148, 210), (2, 2, False)),
    # A3 landscape, 8 copies of an A6 card -> 4x2 (each cell is A6), upright.
    (8, (420, 297), (105, 148), (4, 2, False)),
    # 2 copies of a square -> a single split of the sheet; rotating a square
    # changes nothing, so upright.
    (2, (210, 297), (100, 100), (1, 2, False)),
    # 6 of a portrait card on a portrait A3 sheet: 2x3, and turning each copy
    # into its wider 148.5x140 cell packs it ~6% larger, so rotated wins.
    (6, (297, 420), (105, 148), (2, 3, True)),
    # Two A4-portrait copies on a portrait A3 sheet: the way two A4 pages fit an
    # A3 is 1x2 with each copy turned 90 degrees, not stacked at 70% scale.
    (2, (297, 420), (210, 297), (1, 2, True)),
    # ...and on a landscape A3 sheet the same two sit side by side, upright.
    (2, (420, 297), (210, 297), (2, 1, False)),
])
def test_arrangement_matches_documented_cases(copies, paper, content, expected):
    assert svg_optimize.arrangement(copies, *paper, *content) == expected


def test_arrangement_covers_every_copy():
    for copies in range(2, 33):
        cols, rows, _rot = svg_optimize.arrangement(copies, 297, 420)
        assert cols * rows >= copies
        assert cols >= 1 and rows >= 1


def test_arrangement_falls_back_to_sheet_aspect_without_content_size():
    # No content dims: still deterministic, still covers the count.
    cols, rows, _rot = svg_optimize.arrangement(4, 297, 420)
    assert cols * rows >= 4


# --- grid_svg() -------------------------------------------------------------

def _page_mm(path: Path) -> tuple[float, float]:
    root = etree.parse(str(path)).getroot()
    w, h = svg_utils.svg_size_mm(root)
    return round(w, 1), round(h, 1)


def _ink_mm(path: Path) -> tuple[float, float, float, float]:
    layers = [l["index"] for l in svg_utils.parse_layers(path)["layers"]]
    return svg_utils.ink_rect_doc_mm(path, layers)


def test_grid_svg_tiles_and_keeps_layers(tmp_path):
    src = FIXTURES / "multi-layer.svg"
    base = svg_utils.parse_layers(src)
    out = tmp_path / "g.svg"

    cols, rows = 3, 2
    cell_w, cell_h = 140.0, 148.5
    svg_optimize.grid_svg(src, out, cols, rows, cell_w, cell_h, 0.0, 0.0)

    tiled = svg_utils.parse_layers(out)
    # Every source layer survives, in order, with its label.
    assert [l["label"] for l in tiled["layers"]] == [l["label"] for l in base["layers"]]
    # Page is exactly the grid's bounding box.
    assert _page_mm(out) == (cols * cell_w, rows * cell_h)
    # Geometry is replicated once per cell.
    assert tiled["subpath_count"] == base["subpath_count"] * cols * rows


def test_grid_forces_round_caps(tmp_path):
    """vpype's `write` emits no stroke-linecap, so `force_round_caps` (run by
    _run_grid_phase after the tile) sets round on every layer group — a
    butt-capped source stroke renders round."""
    src = _curve_src(tmp_path)   # its stroke is stroke-linecap="butt"
    out = tmp_path / "g.svg"
    svg_optimize.grid_svg(src, out, 2, 2, 100.0, 100.0, 0.0, 0.0)
    svg_utils.force_round_caps(out)

    root = etree.parse(str(out)).getroot()
    layers = svg_utils._top_level_layers(root)
    assert layers
    for g in layers:
        assert g.get("stroke-linecap") == "round"
        assert g.get("stroke-linejoin") == "round"


# A drawing that declares the pen once on the layer <g> and writes
# stroke-width="inherit" / stroke="inherit" on every path — the shape Inkscape
# and DrawingBotV3 export. vpype reads "inherit" as 0 / unset, so unless
# resolve_inherit_presentation (run at upload and by the optimize queue) has
# inlined it first, the tiled layer comes out stroke-width="0.0" and forced
# black: geometry and plot fine, preview blank.
_INHERIT_SRC = (
    '<svg xmlns="http://www.w3.org/2000/svg"'
    ' xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"'
    ' width="120mm" height="80mm" viewBox="0 0 120 80">'
    '<g inkscape:groupmode="layer" inkscape:label="fine" fill="none"'
    ' stroke="#0088ff" stroke-width="0.35">'
    '<path d="M10 10 L110 70" stroke="inherit" stroke-width="inherit"/></g>'
    '<g inkscape:groupmode="layer" inkscape:label="bold" fill="none"'
    ' stroke="#ff2d54" stroke-width="0.7">'
    '<path d="M10 70 L110 10" stroke="inherit" stroke-width="inherit"/></g>'
    '</svg>'
)


def test_resolve_inherit_presentation_inlines_ancestor_values(tmp_path):
    src = tmp_path / "s.svg"
    src.write_text(_INHERIT_SRC)

    assert svg_utils.resolve_inherit_presentation(src) is True

    root = etree.parse(str(src)).getroot()
    paths = root.findall(".//{http://www.w3.org/2000/svg}path")
    assert [p.get("stroke") for p in paths] == ["#0088ff", "#ff2d54"]
    assert [p.get("stroke-width") for p in paths] == ["0.35", "0.7"]
    # Idempotent: a second pass finds nothing left to inline.
    assert svg_utils.resolve_inherit_presentation(src) is False


def test_resolve_inherit_presentation_drops_unresolvable(tmp_path):
    src = tmp_path / "s.svg"
    src.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"'
        ' xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"'
        ' width="10mm" height="10mm" viewBox="0 0 10 10">'
        '<g inkscape:groupmode="layer" inkscape:label="x">'
        '<path d="M0 0 L10 10" stroke-width="inherit"/></g></svg>'
    )
    svg_utils.resolve_inherit_presentation(src)
    path = etree.parse(str(src)).getroot().find(
        ".//{http://www.w3.org/2000/svg}path")
    assert path.get("stroke-width") is None


def test_grid_keeps_layer_declared_strokes_visible(tmp_path):
    """With inherit resolved before vpype tiles, each tiled layer keeps a
    positive width and its own colour instead of collapsing to
    stroke-width="0.0" and black."""
    src = tmp_path / "s.svg"
    src.write_text(_INHERIT_SRC)
    out = tmp_path / "g.svg"

    svg_utils.resolve_inherit_presentation(src)
    svg_optimize.grid_svg(src, out, 2, 2, 100.0, 100.0, 0.0, 0.0)

    layers = svg_utils._top_level_layers(etree.parse(str(out)).getroot())
    assert [g.get("stroke") for g in layers] == ["#0088ff", "#ff2d54"]
    for g in layers:
        assert float(g.get("stroke-width")) > 0.0


# A pen dot is authored as a zero-length subpath — a bare moveto, or a
# self-referential lineto — which vpype's reader silently drops, so a drawing
# with a dot in it loses it the moment Grid (or Optimize SVG) runs vpype.
# _expand_degenerate_geometry gives such an element a 0.001-unit tail first.
_DOT_SRC = (
    '<svg xmlns="http://www.w3.org/2000/svg"'
    ' xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"'
    ' width="100mm" height="80mm" viewBox="0 0 100 80">'
    '<g inkscape:groupmode="layer" inkscape:label="red">'
    '<path d="M10 10 L90 40" fill="none" stroke="#e8503a" stroke-width="0.8"/>'
    '<path d="M50 45" fill="none" stroke="#e8503a" stroke-width="2"'
    ' stroke-linecap="round"/>'
    '<path d="M60 45 L60 45 Z" fill="none" stroke="#e8503a" stroke-width="2"'
    ' stroke-linecap="round"/>'
    '</g></svg>'
)


def _layered_src(dot: str) -> str:
    """A two-layer drawing: a ``keep`` layer with a real path, and a ``New
    Layer`` holding one real path plus ``dot`` — so the layer survives whatever
    vpype does to the dot, and losing the dot is silent."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg"'
        ' xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"'
        ' width="100mm" height="80mm" viewBox="0 0 100 80">'
        '<g inkscape:groupmode="layer" inkscape:label="keep">'
        '<path d="M8,8 L92,8" fill="none" stroke="#111" stroke-width="0.8"/></g>'
        '<g inkscape:groupmode="layer" inkscape:label="New Layer">'
        '<path d="M20,58 L40,72 L55,56" fill="none" stroke="#e8503a" stroke-width="1"/>'
        f'{dot}'
        '</g></svg>'
    )


# The forms a point-sized mark reaches vpype in — all render as a round-capped
# dot, all get dropped by ``read`` unmitigated. "comma-path" is the one a real
# drawing (Mountain.svg) lost; the zero-radius circle and the arc blob are the
# same bug in another shape. _DOT_SRC is the space-form, trailing-Z,
# dot-only-layer path variant.
_DOT_MARKUP = {
    "comma-path": '<path d="M62.4,45.0 L62.4,45.0" fill="none" stroke="#e8503a"'
                  ' stroke-width="1" stroke-linecap="round"/>',
    "zero-circle": '<circle cx="62.4" cy="45" r="0" fill="none" stroke="#e8503a"'
                   ' stroke-width="1" stroke-linecap="round"/>',
    "arc-blob": '<path d="M62.4,45 a0 0 0 0 1 0 0" fill="none" stroke="#e8503a"'
                ' stroke-width="1" stroke-linecap="round"/>',
}
_LAYERED_DOT_SRC = _layered_src(_DOT_MARKUP["comma-path"])


@pytest.mark.parametrize("d, changed", [
    ("M50 45", True),
    ("M52 45 Z", True),
    ("M54 45 L54 45", True),
    ("M56 45 l0 0", True),
    ("M62.4,165.0 L62.4,165.0", True),              # comma form, no trailing Z (Mountain.svg)
    ("M60 45 a0 0 0 0 1 0 0", True),                # arc blob — analytic bbox is a point
    ("M10 10 L90 40", False),                       # a real segment
    ("M10 10 C 30 5, 70 5, 90 40", False),          # curved — real, left alone
    ("M20 200 A180 180 0 0 1 380 200", False),      # real arc — left alone
    ("M0 0 L10 10 M5 5 Z", False),                  # one real subpath is enough
])
def test_expand_degenerate_geometry_only_touches_point_sized_paths(d, changed):
    root = etree.fromstring(
        f'<svg xmlns="http://www.w3.org/2000/svg"><path d="{d}"/></svg>')
    assert svg_utils._expand_degenerate_geometry(root) is changed
    if changed:
        assert " l0.001 0" in root[0].get("d")
        assert root[0].get("d").endswith(("Z", "z", "0"))   # tail sits before a close
    else:
        assert root[0].get("d") == d


@pytest.mark.parametrize("elem, attrs, floored", [
    ("circle", 'cx="10" cy="10" r="0"', True),
    ("circle", 'cx="10" cy="10"', True),                # missing r reads as 0
    ("ellipse", 'cx="5" cy="5" rx="0" ry="0"', True),
    ("circle", 'cx="10" cy="10" r="1e-6"', False),      # above eps — real
    ("circle", 'cx="50" cy="50" r="30"', False),
    ("ellipse", 'cx="5" cy="5" rx="180" ry="90"', False),
])
def test_expand_degenerate_geometry_floors_point_sized_round_shapes(elem, attrs, floored):
    root = etree.fromstring(
        f'<svg xmlns="http://www.w3.org/2000/svg"><{elem} {attrs}/></svg>')
    assert svg_utils._expand_degenerate_geometry(root) is floored
    keys = ("r",) if elem == "circle" else ("rx", "ry")
    if floored:
        assert all(root[0].get(k) == "0.001" for k in keys)
    assert svg_utils._expand_degenerate_geometry(root) is False   # idempotent


def test_grid_keeps_a_zero_length_dot(tmp_path):
    src = tmp_path / "dot.svg"
    src.write_text(_DOT_SRC)
    svg_utils.normalize_layer_structure(src)                # the upload path
    out = tmp_path / "g.svg"
    svg_optimize.grid_svg(src, out, 2, 2, 45.0, 35.0, 0.0, 0.0)

    root = etree.parse(str(out)).getroot()
    drawn = [k for g in svg_utils._top_level_layers(root) for k in g]
    # 3 source elements survive vpype now (1 segment + 2 dots), one per cell.
    assert len(drawn) == 3 * 4


def test_optimize_queue_keeps_a_dot_in_a_pre_normalize_upload(tmp_path):
    """resolve/expand also run in _process_phases, so a file uploaded before the
    normalize change keeps its dots on its next tiling run."""
    from app import main, optimize_queue

    svg_id = "_grid_dot_it"
    task = _grid_task(svg_id, optimize_svg=False)
    # Overwrite the helper's fixture with the un-normalized dot drawing; the
    # task only reads it when _process runs.
    (main.UPLOAD_DIR / f"{svg_id}.svg").write_text(_DOT_SRC)
    try:
        optimize_queue._process(task)
        assert task.ok
        grid_file = main.UPLOAD_DIR / f"{svg_id}.grid.svg"
        root = etree.parse(str(grid_file)).getroot()
        drawn = [k for g in svg_utils._top_level_layers(root) for k in g]
        assert len(drawn) == 3 * 4
    finally:
        _cleanup(svg_id)


def _new_layer_children(grid_file: Path) -> list:
    root = etree.parse(str(grid_file)).getroot()
    layers = {g.get(svg_utils.LABEL_ATTR): list(g)
              for g in svg_utils._top_level_layers(root)}
    assert set(layers) == {"keep", "New Layer"}, sorted(layers)
    return layers["New Layer"]


@pytest.mark.parametrize("form", list(_DOT_MARKUP), ids=list(_DOT_MARKUP))
def test_grid_keeps_a_point_sized_mark_in_a_populated_layer(tmp_path, form):
    """A point-sized mark — zero-length path, zero-radius circle, arc blob —
    alongside a real path in the same layer: the layer survives regardless, so
    the mark going missing is silent. It must be kept, one per cell."""
    src = tmp_path / f"{form}.svg"
    src.write_text(_layered_src(_DOT_MARKUP[form]))
    svg_utils.normalize_layer_structure(src)                # the upload path
    out = tmp_path / "g.svg"
    svg_optimize.grid_svg(src, out, 2, 2, 45.0, 35.0, 0.0, 0.0)

    assert len(_new_layer_children(out)) == 2 * 4           # (real path + mark) per cell


def test_optimize_queue_keeps_a_comma_form_dot_in_a_populated_layer(tmp_path):
    """Same, through the queue's phase-0 expand on a pre-normalize upload with
    Optimize SVG off (Mountain.svg's settings)."""
    from app import main, optimize_queue

    svg_id = "_grid_layered_dot_it"
    task = _grid_task(svg_id, optimize_svg=False)
    (main.UPLOAD_DIR / f"{svg_id}.svg").write_text(_LAYERED_DOT_SRC)
    try:
        optimize_queue._process(task)
        assert task.ok
        assert len(_new_layer_children(main.UPLOAD_DIR / f"{svg_id}.grid.svg")) == 2 * 4
    finally:
        _cleanup(svg_id)


def test_grid_svg_spacing_pads_every_side(tmp_path):
    """Spacing pads every side of every copy: it widens the sheet by 2*spacing
    per column/row and insets the outer copies by one spacing (so neighbours end
    up 2*spacing apart)."""
    src = FIXTURES / "multi-layer.svg"
    flush = tmp_path / "flush.svg"
    padded = tmp_path / "padded.svg"
    svg_optimize.grid_svg(src, flush, 2, 2, 95.0, 95.0, 0.0, 0.0)
    svg_optimize.grid_svg(src, padded, 2, 2, 95.0, 95.0, 10.0, 10.0)

    # Same cell, +10 spacing -> pitch 115, so each axis grows by 2*10 per cell.
    assert _page_mm(flush) == (190.0, 190.0)
    assert _page_mm(padded) == (230.0, 230.0)

    fl = svg_utils.ink_rect_doc_mm(flush, [l["index"] for l in svg_utils.parse_layers(flush)["layers"]])
    pd = svg_utils.ink_rect_doc_mm(padded, [l["index"] for l in svg_utils.parse_layers(padded)["layers"]])
    # The top-left copy is inset by one spacing from the sheet edge it was flush
    # against before.
    assert pd[0] - fl[0] == pytest.approx(10.0, abs=0.5)
    assert pd[1] - fl[1] == pytest.approx(10.0, abs=0.5)
    # ...and so is the bottom-right copy, from the far edge.
    assert (230.0 - pd[2]) - (190.0 - fl[2]) == pytest.approx(10.0, abs=0.5)
    assert (230.0 - pd[3]) - (190.0 - fl[3]) == pytest.approx(10.0, abs=0.5)


def test_grid_svg_x_and_y_spacing_are_independent(tmp_path):
    """Each axis takes its own spacing into the pitch, with no cross-talk."""
    src = FIXTURES / "multi-layer.svg"
    out = tmp_path / "g.svg"
    cols, rows = 3, 2
    svg_optimize.grid_svg(src, out, cols, rows, 80.0, 60.0, 10.0, 4.0)
    # pitch_w = 80 + 2*10 = 100, pitch_h = 60 + 2*4 = 68.
    assert _page_mm(out) == (cols * 100.0, rows * 68.0)


def test_grid_svg_page_fit_fills_each_cell_with_the_page(tmp_path):
    """Default ``fit="page"`` fits the drawing's whole *page* to the cell, so
    the framed pages fill the sheet while each copy's ink keeps the margin it
    had — its extent is the source's, scaled, not the whole cell."""
    src = FIXTURES / "multi-layer.svg"
    out = tmp_path / "g.svg"
    src_w, src_h = _page_mm(src)                       # 100 x 75
    s_left, s_top, s_right, s_bot = _ink_mm(src)       # (5, 5, 95, 70)

    cols, rows, cell_w, cell_h = 3, 3, 140.0, 99.0
    svg_optimize.grid_svg(src, out, cols, rows, cell_w, cell_h, 0.0, 0.0)

    page_w, page_h = _page_mm(out)
    left, top, right, bottom = _ink_mm(out)
    assert (page_w, page_h) == (420.0, 297.0)
    # Nothing leaves the sheet the grid declared.
    assert left >= -0.5 and top >= -0.5
    assert right <= page_w + 0.5 and bottom <= page_h + 0.5
    # Height binds (99/75 < 140/100): the page fits 75mm -> 99mm and the three
    # framed pages fill the sheet top to bottom. The ink, though, is inset by
    # the source's own top/bottom margin, scaled by that same factor.
    scale = min(cell_w / src_w, cell_h / src_h)
    assert top == pytest.approx(s_top * scale, abs=0.5)
    assert bottom - top == pytest.approx(
        page_h - (s_top + (src_h - s_bot)) * scale, abs=1.0)
    # ...i.e. it does *not* run edge to edge the way ink fit would.
    assert bottom - top < page_h - 5.0


def test_grid_svg_ink_fit_runs_each_copy_to_the_cell_edges(tmp_path):
    """``fit="ink"`` is the opt-out: the drawn geometry alone is fitted, so a
    copy is blown up until its ink touches the binding cell edge."""
    src = FIXTURES / "multi-layer.svg"
    out = tmp_path / "g.svg"
    cols, rows, cell_w, cell_h = 3, 3, 140.0, 99.0
    svg_optimize.grid_svg(src, out, cols, rows, cell_w, cell_h, 0.0, 0.0,
                          fit="ink")

    page_w, page_h = _page_mm(out)
    left, top, right, bottom = _ink_mm(out)
    assert (page_w, page_h) == (420.0, 297.0)
    # Height binds and the ink now runs the full sheet, no margin kept.
    assert bottom - top == pytest.approx(page_h, abs=0.5)


def test_grid_svg_survives_a_spacing_wider_than_its_cell(tmp_path):
    src = FIXTURES / "multi-layer.svg"
    out = tmp_path / "g.svg"
    nat_cell = 50.0
    s = svg_optimize.clamp_spacing_mm(200.0, nat_cell)   # -> 12.5
    cell = nat_cell - 2 * s                              # -> 25.0
    svg_optimize.grid_svg(src, out, 2, 2, cell, cell, s, s)

    page_w, page_h = _page_mm(out)
    left, top, right, bottom = _ink_mm(out)
    assert left >= -0.5 and top >= -0.5
    assert right <= page_w + 0.5 and bottom <= page_h + 0.5


@pytest.mark.parametrize("spacing, nat_cell, expected", [
    (10.0, 100.0, 10.0),    # comfortably inside the cell — untouched
    (-5.0, 100.0, 0.0),     # negative spacing floors at zero
    (200.0, 100.0, 25.0),   # capped at a quarter of the natural cell
    (30.0, 100.0, 25.0),    # 2*30 would leave the copy under half — clamped
])
def test_clamp_spacing_leaves_the_copy_half_its_cell(spacing, nat_cell, expected):
    assert svg_optimize.clamp_spacing_mm(spacing, nat_cell) == pytest.approx(expected)


def test_grid_svg_survives_an_empty_layer(tmp_path):
    src = tmp_path / "src.svg"
    src.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"'
        ' xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"'
        ' width="80mm" height="60mm" viewBox="0 0 80 60">'
        '<g inkscape:groupmode="layer" inkscape:label="ink">'
        '<path d="M10 10 L70 50" fill="none" stroke="#000"/></g>'
        '<g inkscape:groupmode="layer" inkscape:label="empty"></g>'
        '</svg>'
    )
    out = tmp_path / "g.svg"
    svg_optimize.grid_svg(src, out, 2, 2, 80.0, 60.0, 0.0, 0.0)
    # reconcile_layers (run by the queue) re-inserts a dropped-empty layer so
    # indices stay aligned; do the same here and assert both labels come back.
    svg_utils.reconcile_layers(src, out)
    labels = [l["label"] for l in svg_utils.parse_layers(out)["layers"]]
    assert labels == ["ink", "empty"]


# --- rotation for a better fit --------------------------------------------

def test_grid_rotates_copies_for_a_better_fit_end_to_end(tmp_path):
    """_run_grid_phase on 2 copies of a portrait drawing on a portrait A3 sheet:
    arrangement picks 1x2 with each copy turned 90 (the "two A4 in an A3" fit)
    and the page is the margin box."""
    from app import main

    svg_id = "_grid_rot_e2e"
    (main.UPLOAD_DIR / f"{svg_id}.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"'
        ' xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"'
        ' width="210mm" height="297mm" viewBox="0 0 210 297">'
        '<g inkscape:groupmode="layer" inkscape:label="art">'
        '<rect x="10" y="10" width="190" height="277" fill="none" stroke="#000"/>'
        '</g></svg>')
    job = {
        "svg_id": svg_id, "grid_enabled": True, "grid_copies": 2,
        "grid_spacing_x_mm": 0.0, "grid_spacing_y_mm": 0.0, "paper_width_mm": 297.0, "paper_height_mm": 420.0,
        "margin_left_mm": 0.0, "margin_right_mm": 0.0,
        "margin_top_mm": 0.0, "margin_bottom_mm": 0.0, "optimize_svg": False,
    }
    settings = optimize_queue.settings_from_job(job)
    task = optimize_queue._Task(svg_id, settings,
                                optimize_queue.settings_key(settings), "job")
    try:
        optimize_queue._process(task)
        assert task.ok
        grid_file = main.UPLOAD_DIR / f"{svg_id}.grid.svg"
        assert _page_mm(grid_file) == (297.0, 420.0)
        left, top, right, bottom = _ink_mm(grid_file)
        # Two copies stacked in one column, each turned to landscape: the ink is
        # wider than one row is tall.
        one_copy_h = (bottom - top) - 420.0 / 2
        assert (right - left) > one_copy_h
        # Page fit at scale 1.0 (297x210 page into a 297x210 cell): the copy
        # keeps its own 10mm border, so the ink spans the sheet less 2x that.
        assert right - left == pytest.approx(297.0 - 20.0, abs=2.0)
    finally:
        for p in main.UPLOAD_DIR.glob(f"{svg_id}*"):
            p.unlink()
        state.clear_svg_status(svg_id)
        state.clear_svg_status(f"{svg_id}:grid")


def test_staged_plot_filters_a_gridded_layer_to_all_its_copies(tmp_path):
    """grid + pause_between_layers: a stage filters the gridded file by layer
    index, so plotting layer 1 draws that layer of *every* copy."""
    src = FIXTURES / "multi-layer.svg"
    base = svg_utils.parse_layers(src)
    tiled = tmp_path / "g.svg"
    cols, rows = 2, 3
    svg_optimize.grid_svg(src, tiled, cols, rows, 100.0, 100.0, 0.0, 0.0)

    stage = tmp_path / "s0.svg"
    svg_utils.filter_to_layers(tiled, [1], stage, include_orphans=False)
    kept = svg_utils.parse_layers(stage)
    assert [l["label"] for l in kept["layers"]] == [base["layers"][1]["label"]]
    per_copy = base_layer_subpaths(src, 1, tmp_path)
    assert kept["subpath_count"] == per_copy * cols * rows


def base_layer_subpaths(src: Path, index: int, tmp_path: Path) -> int:
    # Scratch goes in tmp_path, never beside the fixture: an interrupted run
    # would otherwise leave an untracked dotfile in tests/fixtures/, which the
    # rest of the suite treats as read-only.
    tmp = tmp_path / f"{src.stem}.one.svg"
    svg_utils.filter_to_layers(src, [index], tmp, include_orphans=False)
    return svg_utils.parse_layers(tmp)["subpath_count"]


def test_queue_produces_both_derivatives_and_records_grid_status(tmp_path):
    """The optimize_queue task runs optimize (phase 1) then grid (phase 2),
    leaving {svg_id}.grid.svg on disk and a ':grid' status entry ready."""
    from app import main

    svg_id = "_grid_it"
    (main.UPLOAD_DIR / f"{svg_id}.svg").write_bytes((FIXTURES / "multi-layer.svg").read_bytes())
    job = {
        "svg_id": svg_id, "grid_enabled": True, "grid_copies": 4,
        "grid_spacing_x_mm": 0.0, "grid_spacing_y_mm": 0.0,
        "paper_width_mm": 297.0, "paper_height_mm": 420.0,
        "margin_left_mm": 0.0, "margin_right_mm": 0.0,
        "margin_top_mm": 0.0, "margin_bottom_mm": 0.0,
        "optimize_svg": True,
        "optimize_svg_linemerge": True, "optimize_svg_linesimplify": False,
        "optimize_svg_linesort": False, "optimize_svg_reloop": False,
        "optimize_svg_tolerance_mm": 0.1,
    }
    settings = optimize_queue.settings_from_job(job)
    task = optimize_queue._Task(svg_id, settings,
                                optimize_queue.settings_key(settings), "job")
    try:
        optimize_queue._process(task)
        assert task.ok
        assert (main.UPLOAD_DIR / f"{svg_id}.opt.svg").exists()
        grid_file = main.UPLOAD_DIR / f"{svg_id}.grid.svg"
        assert grid_file.exists()
        st = state.get_svg_status(f"{svg_id}:grid")
        assert st and st["status"] == "ready"
        assert st["settings_key"] == task.grid_key
        # 4 copies on a 297x420 sheet -> the tiled page is that sheet.
        assert _page_mm(grid_file) == (297.0, 420.0)
    finally:
        for p in main.UPLOAD_DIR.glob(f"{svg_id}*"):
            p.unlink()
        state.clear_svg_status(svg_id)
        state.clear_svg_status(f"{svg_id}:grid")


def test_queue_arranges_for_the_margin_box_not_the_sheet():
    """The cells are carved out of the margin box, so that is what the columns x
    rows split has to be decided on. A4 portrait with 100mm top and bottom
    margins is a landscape strip: two copies belong side by side."""
    from app import main

    svg_id = "_grid_margins"
    (main.UPLOAD_DIR / f"{svg_id}.svg").write_bytes((FIXTURES / "multi-layer.svg").read_bytes())
    job = {
        "svg_id": svg_id, "grid_enabled": True, "grid_copies": 2,
        "grid_spacing_x_mm": 0.0, "grid_spacing_y_mm": 0.0,
        "paper_width_mm": 210.0, "paper_height_mm": 297.0,
        "margin_left_mm": 0.0, "margin_right_mm": 0.0,
        "margin_top_mm": 100.0, "margin_bottom_mm": 100.0,
        "optimize_svg": True,
        "optimize_svg_linemerge": True, "optimize_svg_linesimplify": False,
        "optimize_svg_linesort": False, "optimize_svg_reloop": False,
        "optimize_svg_tolerance_mm": 0.1,
    }
    settings = optimize_queue.settings_from_job(job)
    task = optimize_queue._Task(svg_id, settings,
                                optimize_queue.settings_key(settings), "job")
    try:
        optimize_queue._process(task)
        assert task.ok
        grid_file = main.UPLOAD_DIR / f"{svg_id}.grid.svg"
        # The tiled page is the margin box either way — the arrangement shows up
        # in how much of it the ink covers.
        assert _page_mm(grid_file) == (210.0, 97.0)
        left, top, right, bottom = _ink_mm(grid_file)
        # 2x1 spans the strip: near the full 210mm wide. The 1x2 the sheet's own
        # aspect used to pick stacks two ~63mm bands a third of the width.
        assert right - left > 190.0
        assert bottom - top < 90.0
    finally:
        for p in main.UPLOAD_DIR.glob(f"{svg_id}*"):
            p.unlink()
        state.clear_svg_status(svg_id)
        state.clear_svg_status(f"{svg_id}:grid")


def _grid_task(svg_id: str, **overrides) -> "optimize_queue._Task":
    """A grid-enabled task for ``svg_id``, with the source SVG in place."""
    from app import main

    (main.UPLOAD_DIR / f"{svg_id}.svg").write_bytes(
        (FIXTURES / "multi-layer.svg").read_bytes())
    job = {
        "svg_id": svg_id, "grid_enabled": True, "grid_copies": 4,
        "grid_spacing_x_mm": 0.0, "grid_spacing_y_mm": 0.0,
        "paper_width_mm": 297.0, "paper_height_mm": 420.0,
        "margin_left_mm": 0.0, "margin_right_mm": 0.0,
        "margin_top_mm": 0.0, "margin_bottom_mm": 0.0,
        "optimize_svg": True,
        "optimize_svg_linemerge": True, "optimize_svg_linesimplify": False,
        "optimize_svg_linesort": False, "optimize_svg_reloop": False,
        "optimize_svg_tolerance_mm": 0.1,
        **overrides,
    }
    settings = optimize_queue.settings_from_job(job)
    return optimize_queue._Task(svg_id, settings,
                                optimize_queue.settings_key(settings), "job")


def _cleanup(svg_id: str) -> None:
    from app import main

    for f in main.UPLOAD_DIR.glob(f"{svg_id}*"):
        f.unlink()
    state.clear_svg_status(svg_id)
    state.clear_svg_status(f"{svg_id}:grid")


def test_a_failed_tiling_fails_the_task(monkeypatch):
    """Falling back to the un-tiled file would put one copy on the sheet and
    call it a success — on a plotter that spends the paper and the pen time
    before the user can see anything went wrong."""
    svg_id = "_grid_boom"
    task = _grid_task(svg_id)

    def boom(*a, **kw):
        raise svg_optimize.OptimizeError("vpype fell over")

    monkeypatch.setattr(svg_optimize, "grid_svg", boom)
    try:
        optimize_queue._process(task)
        assert task.ok is False
        assert "could not tile the sheet" in task.error
        st = state.get_svg_status(f"{svg_id}:grid")
        assert st and st["status"] == "failed"
    finally:
        _cleanup(svg_id)


def test_a_failed_optimize_phase_settles_the_grid_status(monkeypatch):
    """Phase 1 failing never reaches phase 2, so the ':grid' entry _enqueue set
    to 'pending' used to sit there for good — read by app.js as "still
    building", and persisted to state.json so a restart brought it back."""
    svg_id = "_grid_stuck"
    task = _grid_task(svg_id)
    # Stand in for _enqueue, which sets this before the task is dispatched.
    state.set_svg_status(f"{svg_id}:grid", "pending", settings_key=task.grid_key)

    def boom(*a, **kw):
        raise svg_optimize.OptimizeError("no geometry")

    monkeypatch.setattr(svg_optimize, "optimize_svg", boom)
    try:
        optimize_queue._process(task)
        assert task.ok is False
        st = state.get_svg_status(f"{svg_id}:grid")
        assert st and st["status"] == "failed"
    finally:
        _cleanup(svg_id)


def test_a_cancelled_task_leaves_no_grid_status_behind():
    svg_id = "_grid_cancel"
    task = _grid_task(svg_id)
    state.set_svg_status(f"{svg_id}:grid", "pending", settings_key=task.grid_key)
    task.cancelled = True
    try:
        optimize_queue._process(task)
        assert state.get_svg_status(f"{svg_id}:grid") is None
    finally:
        _cleanup(svg_id)


def test_grid_alone_does_not_smuggle_in_optimization():
    """Grid is enough on its own to run a task, and phase 1 used to read the
    four toggles straight off the job record — so Optimize SVG off plus Grid on
    tiled simplified geometry the user had turned simplification off for."""
    from app import main

    svg_id = "_grid_raw"
    task = _grid_task(svg_id, optimize_svg=False,
                      optimize_svg_linemerge=True, optimize_svg_linesimplify=True)
    try:
        optimize_queue._process(task)
        assert task.ok
        # Phase 1 took its copy-through no-op path: byte-for-byte the upload.
        assert (main.UPLOAD_DIR / f"{svg_id}.opt.svg").read_bytes() \
            == (FIXTURES / "multi-layer.svg").read_bytes()
        assert (main.UPLOAD_DIR / f"{svg_id}.grid.svg").exists()
    finally:
        _cleanup(svg_id)


def test_settings_from_job_reports_the_toggles_that_will_actually_run():
    off = optimize_queue.settings_from_job({
        "optimize_svg": False, "optimize_svg_linemerge": True,
        "optimize_svg_linesimplify": True, "optimize_svg_linesort": True,
        "optimize_svg_reloop": True, "optimize_svg_tolerance_mm": 5.0,
    })
    assert not any([off["linemerge"], off["linesimplify"],
                    off["linesort"], off["reloop"]])
    # The tolerance goes with them, so a slider that now changes nothing cannot
    # invalidate the cache key and spend a vpype run re-tiling.
    assert off["tolerance_mm"] == 0.10


def test_settings_from_job_carries_the_fit_mode():
    on = optimize_queue.settings_from_job({"grid_enabled": True, "optimize_svg": False,
                                           "optimize_svg_tolerance_mm": 0.1})
    assert on["grid"]["fit"] == "page"                      # the default
    ink = optimize_queue.settings_from_job({"grid_enabled": True, "grid_fit": "ink",
                                            "optimize_svg": False,
                                            "optimize_svg_tolerance_mm": 0.1})
    assert ink["grid"]["fit"] == "ink"


def test_fit_mode_changes_the_grid_cache_key():
    base = {
        "grid_enabled": True, "grid_copies": 4,
        "grid_spacing_x_mm": 0.0, "grid_spacing_y_mm": 0.0,
        "paper_width_mm": 297.0, "paper_height_mm": 420.0,
        "margin_left_mm": 0.0, "margin_right_mm": 0.0,
        "margin_top_mm": 0.0, "margin_bottom_mm": 0.0,
        "optimize_svg": False, "optimize_svg_tolerance_mm": 0.1,
    }
    key = lambda j: optimize_queue.grid_settings_key(optimize_queue.settings_from_job(j))
    assert key(base) != key({**base, "grid_fit": "ink"})


def test_patch_persists_and_clamps_grid_fields(client, job_from_svg):
    job = job_from_svg(FIXTURES / "multi-layer.svg")
    res = client.patch(f"/jobs/{job['job_id']}",
                       json={"grid_enabled": True, "grid_copies": 999,
                             "grid_fit": "ink",
                             "grid_spacing_x_mm": 999, "grid_spacing_y_mm": -5,
                             "grid_spacing_linked": False})
    assert res.status_code == 200
    body = res.json()
    assert body["grid_enabled"] is True
    assert body["grid_copies"] == 64             # clamped to the max
    assert body["grid_fit"] == "ink"             # round-trips
    assert body["grid_spacing_x_mm"] == 100.0    # clamped to the max
    assert body["grid_spacing_y_mm"] == 0.0      # floored
    assert body["grid_spacing_linked"] is False  # round-trips


def test_placement_endpoint_reports_the_tiled_size(client, job_from_svg):
    from app import main as _main
    job = job_from_svg(FIXTURES / "multi-layer.svg",
                       grid_enabled=True, paper_width_mm=297.0,
                       paper_height_mm=420.0)
    # Stand in for the queue: a ready {svg_id}.grid.svg next to the source, and
    # the status entry that marks it current for these settings.
    grid_file = _main.UPLOAD_DIR / f"{job['svg_id']}.grid.svg"
    svg_optimize.grid_svg(_main.UPLOAD_DIR / f"{job['svg_id']}.svg",
                          grid_file, 2, 2, 148.5, 210.0, 0.0, 0.0)
    state.set_svg_status(
        f"{job['svg_id']}:grid", "ready",
        settings_key=optimize_queue.grid_settings_key(
            optimize_queue.settings_from_job(job)))
    try:
        res = client.post(f"/jobs/{job['job_id']}/placement",
                          json={"paper_width_mm": 297.0, "paper_height_mm": 420.0})
        assert res.status_code == 200
        body = res.json()
        # The served document is the tiled sheet, so its size is the sheet's.
        assert body["doc_width_mm"] == pytest.approx(297.0, abs=0.5)
        assert body["doc_height_mm"] == pytest.approx(420.0, abs=0.5)
    finally:
        grid_file.unlink(missing_ok=True)
        state.clear_svg_status(f"{job['svg_id']}:grid")


# --- which file the job actually reads ----------------------------------------

def _grid_job(svg_id: str, **overrides) -> dict:
    """A grid-enabled job record with all three files on disk, and the ':grid'
    status the queue writes once the tiled file is built."""
    from app import main

    for suffix in (".svg", ".opt.svg", ".grid.svg"):
        (main.UPLOAD_DIR / f"{svg_id}{suffix}").write_bytes(
            (FIXTURES / "multi-layer.svg").read_bytes())
    job = {
        "svg_id": svg_id, "optimize_mode": "beginner", "optimize_svg": True,
        "grid_enabled": True, "grid_copies": 4, "grid_spacing_x_mm": 0.0, "grid_spacing_y_mm": 0.0,
        "grid_cut_marks": False,
        "paper_width_mm": 297.0, "paper_height_mm": 420.0,
        "margin_left_mm": 0.0, "margin_right_mm": 0.0,
        "margin_top_mm": 0.0, "margin_bottom_mm": 0.0,
        "optimize_svg_linemerge": True, "optimize_svg_linesimplify": True,
        "optimize_svg_linesort": True, "optimize_svg_reloop": True,
        "optimize_svg_tolerance_mm": 0.1,
        **overrides,
    }
    state.set_svg_status(
        f"{svg_id}:grid", "ready",
        settings_key=optimize_queue.grid_settings_key(
            optimize_queue.settings_from_job(job)))
    return job


def test_a_grid_built_for_other_settings_is_not_served():
    """Existence is not currency: between changing a setting and the rebuild
    landing, every read path would answer for the previous arrangement while the
    card shows the new one."""
    from app import main, plot_worker

    svg_id = "_grid_stale"
    job = _grid_job(svg_id)
    try:
        assert plot_worker._effective_svg_path(job) \
            == main.UPLOAD_DIR / f"{svg_id}.grid.svg"
        # The user asks for 9 copies; the file on disk is still the 4-up one.
        assert plot_worker._effective_svg_path({**job, "grid_copies": 9}) \
            == main.UPLOAD_DIR / f"{svg_id}.opt.svg"
    finally:
        _cleanup(svg_id)


def test_expert_mode_serves_its_own_optimize_over_a_leftover_grid():
    """Switching to Expert only greys the Grid section out — the card keeps
    PATCHing grid_enabled, and the queue stops refreshing the tiled file. The
    .opt.svg the user's Execute wrote is what plots."""
    from app import main, plot_worker

    svg_id = "_grid_expert"
    job = _grid_job(svg_id, optimize_mode="expert")
    try:
        assert plot_worker._effective_svg_path(job) \
            == main.UPLOAD_DIR / f"{svg_id}.opt.svg"
    finally:
        _cleanup(svg_id)


def test_turning_grid_off_clears_its_status():
    svg_id = "_grid_toggled_off"
    state.set_svg_status(f"{svg_id}:grid", "ready", settings_key="whatever")
    job = {"svg_id": svg_id, "grid_enabled": False, "optimize_svg": True,
           "optimize_svg_tolerance_mm": 0.1}
    try:
        optimize_queue._enqueue(svg_id, optimize_queue.settings_from_job(job),
                                kind="job")
        assert state.get_svg_status(f"{svg_id}:grid") is None
    finally:
        optimize_queue.cancel(svg_id)


def test_an_upload_scan_leaves_a_live_grid_status_alone():
    """enqueue_for_upload and bootstrap_from_disk pass grid=None for every SVG
    on disk. Clearing on that would wipe every grid status at startup."""
    svg_id = "_grid_upload_scan"
    state.set_svg_status(f"{svg_id}:grid", "ready", settings_key="whatever")
    try:
        optimize_queue._enqueue(svg_id, optimize_queue.settings_from_config(),
                                kind="upload")
        assert state.get_svg_status(f"{svg_id}:grid") is not None
    finally:
        optimize_queue.cancel(svg_id)


def test_svg_meta_reports_the_source_size_beside_the_tiled_one(client, job_from_svg):
    """svg-meta describes whatever the job would plot, which for a grid job is
    the tiled sheet. The card's arrangement readout needs the drawing's own
    size — computing it from the sheet picks a different columns x rows than
    the one the server tiled to."""
    from app import main as _main

    job = job_from_svg(FIXTURES / "multi-layer.svg",
                       grid_enabled=True, paper_width_mm=297.0,
                       paper_height_mm=420.0)
    grid_file = _main.UPLOAD_DIR / f"{job['svg_id']}.grid.svg"
    svg_optimize.grid_svg(_main.UPLOAD_DIR / f"{job['svg_id']}.svg",
                          grid_file, 2, 2, 148.5, 210.0, 0.0, 0.0)
    state.set_svg_status(
        f"{job['svg_id']}:grid", "ready",
        settings_key=optimize_queue.grid_settings_key(
            optimize_queue.settings_from_job(job)))
    try:
        body = client.get(f"/jobs/{job['job_id']}/svg-meta").json()
        # The served document is the sheet...
        assert body["width_mm"] == pytest.approx(297.0, abs=0.5)
        # ...and the drawing it was tiled from is still reported beside it.
        assert body["source_width_mm"] == pytest.approx(100.0)
        assert body["source_height_mm"] == pytest.approx(75.0)
    finally:
        grid_file.unlink(missing_ok=True)
        state.clear_svg_status(f"{job['svg_id']}:grid")


def test_gridded_document_places_onto_its_sheet(tmp_path):
    """The tiled file is sized to the sheet, so the placement engine positions
    it near-identity — margins/fit/transform then act on the whole grid."""
    src = FIXTURES / "multi-layer.svg"
    tiled = tmp_path / "g.svg"
    # 8-up on an A3 landscape sheet, no margins.
    pw, ph = 420.0, 297.0
    cols, rows, rot = svg_optimize.arrangement(8, pw, ph, 100.0, 75.0)
    svg_optimize.grid_svg(src, tiled, cols, rows, pw / cols, ph / rows, 0.0, 0.0,
                          rotate_copies=rot)

    root = etree.parse(str(tiled)).getroot()
    dw, dh = svg_utils.svg_size_mm(root)
    place = placement.compute(
        dw, dh, svg_utils.parse_viewbox(root.get("viewBox", "")),
        pw, ph, 0, 0, 0, 0, fit_content=False,
    )
    assert place.footprint_w_mm == pytest.approx(pw, abs=1.0)
    assert place.footprint_h_mm == pytest.approx(ph, abs=1.0)
    assert place.rotation_deg == 0


# --- cutting marks ------------------------------------------------------------

def _cut_marks_layer(path: Path):
    root = etree.parse(str(path)).getroot()
    found = [g for g in root if svg_utils._is_cut_marks_layer(g)]
    return found[0] if found else None


def _cut_segments_mm(path: Path) -> list[tuple[float, float, float, float]]:
    """Every mark segment as an mm-space (x1, y1, x2, y2), rounded."""
    layer = _cut_marks_layer(path)
    root = etree.parse(str(path)).getroot()
    doc_w, doc_h = svg_utils.svg_size_mm(root)
    _, _, vb_w, vb_h = svg_utils.parse_viewbox(root.get("viewBox"))
    sx, sy = doc_w / vb_w, doc_h / vb_h
    out = []
    for ln in layer:
        out.append((round(float(ln.get("x1")) * sx, 3), round(float(ln.get("y1")) * sy, 3),
                    round(float(ln.get("x2")) * sx, 3), round(float(ln.get("y2")) * sy, 3)))
    return out


def _tiled(tmp_path: Path, cols: int, rows: int, cell_w: float, cell_h: float,
           spacing: float, marks: bool = True) -> Path:
    src = FIXTURES / "multi-layer.svg"
    out = tmp_path / "g.svg"
    svg_optimize.grid_svg(src, out, cols, rows, cell_w, cell_h, spacing, spacing)
    svg_utils.reconcile_layers(src, out)
    if marks:
        svg_utils.add_cut_marks(out, cols, rows, cell_w, cell_h, spacing, spacing)
    return out


def test_cut_marks_are_edge_ticks_and_interior_crosses(tmp_path):
    """Nothing at the sheet corners: a 1mm tick where each interior cut reaches
    the sheet edge (between two copies), a 2mm cross where two cuts meet
    (between four)."""
    out = _tiled(tmp_path, 2, 3, 100.0, 80.0, spacing=0.0)   # sheet 200 x 240
    segs = _cut_segments_mm(out)
    # 1 interior vertical cut (x=100) -> a tick at each of y=0 and y=240.
    # 2 interior horizontal cuts (y=80, 160) -> a tick at each of x=0 and x=200.
    # 1*2 interior crossings -> 2 cross-lines each.
    assert (100.0, 0.0, 100.0, 1.0) in segs
    assert (100.0, 239.0, 100.0, 240.0) in segs
    assert (0.0, 80.0, 1.0, 80.0) in segs
    assert (199.0, 160.0, 200.0, 160.0) in segs
    assert (99.0, 80.0, 101.0, 80.0) in segs        # cross arm, horizontal
    assert (100.0, 79.0, 100.0, 81.0) in segs       # cross arm, vertical
    assert len(segs) == 2 * 1 + 2 * 2 + 2 * (1 * 2)  # 4 ticks + 2 crossings

    # No segment sits on a sheet corner.
    for x1, y1, x2, y2 in segs:
        for (x, y) in ((x1, y1), (x2, y2)):
            assert (round(x), round(y)) not in {(0, 0), (200, 0), (0, 240), (200, 240)}


def test_cut_marks_with_spacing_land_mid_gap(tmp_path):
    out = _tiled(tmp_path, 2, 3, 90.0, 70.0, spacing=10.0)   # sheet 220 x 270
    segs = _cut_segments_mm(out)
    # pitch_w = 90 + 2*10 = 110, so the interior vertical cut is at 1*110 = 110,
    # mid-way through the 2*10 gap. Ticks at y=0 / y=270.
    assert (110.0, 0.0, 110.0, 1.0) in segs
    assert (110.0, 269.0, 110.0, 270.0) in segs
    # pitch_h = 70 + 2*10 = 90 -> interior horizontal cuts at 90 and 180.
    assert (0.0, 90.0, 1.0, 90.0) in segs
    assert (219.0, 180.0, 220.0, 180.0) in segs


def test_cut_marks_layer_is_the_last_indexed_layer(tmp_path):
    """The marks are appended last, so they take the layer index one past the
    artwork — a normal, addressable layer."""
    base = svg_utils.parse_layers(FIXTURES / "multi-layer.svg")
    out = _tiled(tmp_path, 2, 2, 100.0, 100.0, spacing=6.0)
    tiled = svg_utils.parse_layers(out)
    assert [(l["index"], l["label"]) for l in tiled["layers"]] \
        == [(l["index"], l["label"]) for l in base["layers"]] \
        + [(len(base["layers"]), svg_utils.CUT_MARKS_LABEL)]
    layer = _cut_marks_layer(out)
    assert layer.get(svg_utils.LABEL_ATTR) == svg_utils.CUT_MARKS_LABEL
    # It still must not answer to a vpype layer id an artwork layer already owns,
    # or a measuring pass would fold the marks into that layer's geometry.
    artwork = svg_utils._top_level_layers(etree.parse(str(out)).getroot())[:-1]
    ids = [svg_utils._vpype_layer_id(g.get(svg_utils.LABEL_ATTR) or "",
                                     g.get("id") or "", order)
           for order, g in enumerate(artwork, start=1)]
    assert svg_utils._vpype_layer_id("", layer.get("id"), len(ids) + 1) not in ids


def test_cut_marks_stage_filters_to_the_marks_layer(tmp_path):
    """The marks are their own indexed layer now, so a stage filtered to that
    index carries them and no other stage does."""
    out = _tiled(tmp_path, 2, 2, 100.0, 100.0, spacing=0.0)
    n = len(svg_utils.parse_layers(FIXTURES / "multi-layer.svg")["layers"])
    marks_stage, art_stage = tmp_path / "s0.svg", tmp_path / "s1.svg"
    svg_utils.filter_to_layers(out, [n], marks_stage, include_orphans=False)
    svg_utils.filter_to_layers(out, [0], art_stage, include_orphans=True)
    assert _cut_marks_layer(marks_stage) is not None
    assert _cut_marks_layer(art_stage) is None


def test_cut_marks_are_opt_in(tmp_path):
    assert _cut_marks_layer(_tiled(tmp_path, 2, 2, 100.0, 100.0, 0.0, marks=False)) is None


def test_cut_marks_single_column_has_only_side_ticks(tmp_path):
    """1xN: no interior vertical cut, so no top/bottom ticks and no crosses —
    just a left and a right tick at each interior horizontal cut."""
    out = _tiled(tmp_path, 1, 3, 120.0, 90.0, spacing=0.0)   # sheet 120 x 270
    segs = _cut_segments_mm(out)
    assert len(segs) == 2 * (3 - 1)                          # 2 ticks per interior row
    assert all(y1 == y2 for x1, y1, x2, y2 in segs)          # all horizontal
    assert {round(y1) for x1, y1, x2, y2 in segs} == {90, 180}


def test_cut_marks_change_the_grid_cache_key():
    base = {
        "grid_enabled": True, "grid_copies": 4, "grid_spacing_x_mm": 0.0, "grid_spacing_y_mm": 0.0,
        "paper_width_mm": 297.0, "paper_height_mm": 420.0,
        "margin_left_mm": 0.0, "margin_right_mm": 0.0,
        "margin_top_mm": 0.0, "margin_bottom_mm": 0.0,
        "optimize_svg": True,
        "optimize_svg_linemerge": True, "optimize_svg_linesimplify": True,
        "optimize_svg_linesort": True, "optimize_svg_reloop": True,
        "optimize_svg_tolerance_mm": 0.1,
    }
    off = optimize_queue.grid_settings_key(optimize_queue.settings_from_job(base))
    on = optimize_queue.grid_settings_key(
        optimize_queue.settings_from_job({**base, "grid_cut_marks": True}))
    assert off != on


def test_spacing_changes_the_grid_cache_key():
    base = {
        "grid_enabled": True, "grid_copies": 4,
        "grid_spacing_x_mm": 0.0, "grid_spacing_y_mm": 0.0,
        "paper_width_mm": 297.0, "paper_height_mm": 420.0,
        "margin_left_mm": 0.0, "margin_right_mm": 0.0,
        "margin_top_mm": 0.0, "margin_bottom_mm": 0.0,
        "optimize_svg": False, "optimize_svg_tolerance_mm": 0.1,
    }
    key = lambda j: optimize_queue.grid_settings_key(optimize_queue.settings_from_job(j))
    assert key(base) != key({**base, "grid_spacing_x_mm": 5.0})
    assert key(base) != key({**base, "grid_spacing_y_mm": 5.0})
    # The two axes are distinct in the key — swapping them is not a no-op.
    assert key({**base, "grid_spacing_x_mm": 5.0}) != key({**base, "grid_spacing_y_mm": 5.0})
    # ...but grid_spacing_linked never reaches it: it steers only the card.
    assert key(base) == key({**base, "grid_spacing_linked": False})


def test_patch_persists_grid_cut_marks(client, job_from_svg):
    job = job_from_svg(FIXTURES / "multi-layer.svg")
    assert job["grid_cut_marks"] is False
    res = client.patch(f"/jobs/{job['job_id']}",
                       json={"grid_enabled": True, "grid_cut_marks": True})
    assert res.status_code == 200
    assert res.json()["grid_cut_marks"] is True


def test_sync_cut_marks_selection_adds_removes_and_keeps_position():
    from app.main import _sync_cut_marks_selection as sync

    art = [{"index": 0, "label": "a"}, {"index": 1, "label": "b"}]

    off = sync(art, grid_enabled=True, grid_cut_marks=False)
    assert off == art                                            # nothing added

    on = sync(art, grid_enabled=True, grid_cut_marks=True)
    assert on[0] == {"index": 2, "label": svg_utils.CUT_MARKS_LABEL,
                     "selected": True, "cut_marks": True}         # first, index == artwork count
    assert on[1:] == art

    # A user-reordered row keeps its slot; only its index is re-pointed.
    moved = [art[0], {"index": 99, "label": "trim", "selected": False,
                      "cut_marks": True}, art[1]]
    resynced = sync(moved, grid_enabled=True, grid_cut_marks=True)
    assert [s.get("cut_marks") for s in resynced] == [None, True, None]
    assert resynced[1]["index"] == 2 and resynced[1]["label"] == "trim"
    assert resynced[1]["selected"] is False

    # Turning the grid off strips it regardless of position.
    assert sync(moved, grid_enabled=False, grid_cut_marks=True) == art


def test_patch_toggles_the_cut_marks_layer_row(client, job_from_svg):
    job = job_from_svg(FIXTURES / "multi-layer.svg",
                       layers=[{"index": 0, "label": "art"}])
    labels = lambda j: [s["label"] for s in j["layer_selections"]]

    on = client.patch(f"/jobs/{job['job_id']}",
                      json={"grid_enabled": True, "grid_cut_marks": True}).json()
    assert labels(on) == [svg_utils.CUT_MARKS_LABEL, "art"]
    assert on["layer_selections"][0]["cut_marks"] is True
    assert on["layer_selections"][0]["index"] == 1

    off = client.patch(f"/jobs/{job['job_id']}",
                       json={"grid_cut_marks": False}).json()
    assert labels(off) == ["art"]


def test_queue_grid_phase_adds_cut_marks(tmp_path):
    """End of the wiring: grid_cut_marks on the job puts the marks layer into
    {svg_id}.grid.svg, after the reconcile that fixes the layer numbering."""
    from app import main

    svg_id = "_grid_marks_it"
    (main.UPLOAD_DIR / f"{svg_id}.svg").write_bytes((FIXTURES / "multi-layer.svg").read_bytes())
    job = {
        "svg_id": svg_id, "grid_enabled": True, "grid_copies": 4,
        "grid_spacing_x_mm": 8.0, "grid_spacing_y_mm": 8.0, "grid_cut_marks": True,
        "paper_width_mm": 200.0, "paper_height_mm": 200.0,
        "margin_left_mm": 0.0, "margin_right_mm": 0.0,
        "margin_top_mm": 0.0, "margin_bottom_mm": 0.0,
        "optimize_svg": True,
        "optimize_svg_linemerge": True, "optimize_svg_linesimplify": False,
        "optimize_svg_linesort": False, "optimize_svg_reloop": False,
        "optimize_svg_tolerance_mm": 0.1,
    }
    settings = optimize_queue.settings_from_job(job)
    task = optimize_queue._Task(svg_id, settings,
                                optimize_queue.settings_key(settings), "job")
    try:
        optimize_queue._process(task)
        assert task.ok
        grid_file = main.UPLOAD_DIR / f"{svg_id}.grid.svg"
        # 4 copies on a 200mm square sheet -> 2x2, natural cell 100; spacing 8
        # per side -> cell 84, pitch 100, so the single interior cut on each
        # axis sits at 1 * pitch = 100.
        segs = _cut_segments_mm(grid_file)
        assert (100.0, 0.0, 100.0, 1.0) in segs                # top edge tick
        assert (0.0, 100.0, 1.0, 100.0) in segs                # left edge tick
        assert (99.0, 100.0, 101.0, 100.0) in segs             # centre cross arm
        assert len(segs) == 2 * 1 + 2 * 1 + 2 * (1 * 1)        # 4 ticks + 1 cross
        # The artwork's layer sequence is intact, with "Cut marks" appended.
        assert [l["label"] for l in svg_utils.parse_layers(grid_file)["layers"]] \
            == [l["label"] for l in svg_utils.parse_layers(FIXTURES / "multi-layer.svg")["layers"]] \
            + [svg_utils.CUT_MARKS_LABEL]
    finally:
        for p in main.UPLOAD_DIR.glob(f"{svg_id}*"):
            p.unlink()
        state.clear_svg_status(svg_id)
        state.clear_svg_status(f"{svg_id}:grid")
