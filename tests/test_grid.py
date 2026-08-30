"""The "Grid" layout module: arrangement maths + the vpype tiling pass.

Grid is a reversible per-job derivative (like Optimize SVG): vpype tiles the
whole drawing to fill the sheet, each copy resized to its cell. These tests
pin the pure arrangement helper and the one vpype call that produces
``{svg_id}.grid.svg``; the reversibility wiring is covered where the
``_effective_svg_path`` contract lives.
"""
from pathlib import Path

import pytest
from lxml import etree

from app import optimize_queue, placement, state, svg_optimize, svg_utils

FIXTURES = Path(__file__).parent / "fixtures"


# --- arrangement() ------------------------------------------------------------

@pytest.mark.parametrize("copies, paper, content, expected", [
    # A3 portrait, 4 copies of an A5-ish card -> 2x2 (each cell is A5).
    (4, (297, 420), (148, 210), (2, 2)),
    # A3 landscape, 8 copies of an A6 card -> 4x2 (each cell is A6).
    (8, (420, 297), (105, 148), (4, 2)),
    # 2 copies of a square -> a single split of the sheet, longest side halved.
    (2, (210, 297), (100, 100), (1, 2)),
    # 6 factors exactly on a portrait sheet.
    (6, (297, 420), (105, 148), (2, 3)),
])
def test_arrangement_matches_documented_cases(copies, paper, content, expected):
    assert svg_optimize.arrangement(copies, *paper, *content) == expected


def test_arrangement_covers_every_copy():
    for copies in range(2, 33):
        cols, rows = svg_optimize.arrangement(copies, 297, 420)
        assert cols * rows >= copies
        assert cols >= 1 and rows >= 1


def test_arrangement_falls_back_to_sheet_aspect_without_content_size():
    # No content dims: still deterministic, still covers the count.
    cols, rows = svg_optimize.arrangement(4, 297, 420)
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
    svg_optimize.grid_svg(src, out, cols, rows, cell_w, cell_h, gutter_mm=0.0)

    tiled = svg_utils.parse_layers(out)
    # Every source layer survives, in order, with its label.
    assert [l["label"] for l in tiled["layers"]] == [l["label"] for l in base["layers"]]
    # Page is exactly the grid's bounding box.
    assert _page_mm(out) == (cols * cell_w, rows * cell_h)
    # Geometry is replicated once per cell.
    assert tiled["subpath_count"] == base["subpath_count"] * cols * rows


def test_grid_svg_gutter_insets_each_copy(tmp_path):
    src = FIXTURES / "multi-layer.svg"
    flush = tmp_path / "flush.svg"
    gapped = tmp_path / "gapped.svg"
    svg_optimize.grid_svg(src, flush, 2, 2, 100.0, 100.0, gutter_mm=0.0)
    svg_optimize.grid_svg(src, gapped, 2, 2, 100.0, 100.0, gutter_mm=10.0)

    # Same page (the pitch is unchanged); the ink just occupies less of it.
    assert _page_mm(flush) == _page_mm(gapped)
    layers = [l["index"] for l in svg_utils.parse_layers(flush)["layers"]]
    flush_ink = svg_utils.ink_rect_doc_mm(flush, layers)
    gapped_ink = svg_utils.ink_rect_doc_mm(gapped, layers)
    fw = flush_ink[2] - flush_ink[0]
    gw = gapped_ink[2] - gapped_ink[0]
    assert gw < fw


def test_grid_svg_fills_a_landscape_cell(tmp_path):
    """vpype's `layout` normalises any page size to portrait unless --landscape
    is passed, so a wide cell was silently fitted to a box of its own dimensions
    swapped: copies too small across, and a bottom row hanging off the sheet."""
    src = FIXTURES / "multi-layer.svg"
    out = tmp_path / "g.svg"
    # 8-up on A3 landscape: arrangement() picks 3x3, cells 140 x 99.
    cols, rows, cell_w, cell_h = 3, 3, 140.0, 99.0
    svg_optimize.grid_svg(src, out, cols, rows, cell_w, cell_h, gutter_mm=0.0)

    page_w, page_h = _page_mm(out)
    left, top, right, bottom = _ink_mm(out)
    # Nothing may leave the sheet the grid just declared.
    assert left >= -0.5 and top >= -0.5
    assert right <= page_w + 0.5
    assert bottom <= page_h + 0.5
    # With the cell the right way round, the copies' height is what binds
    # (99/65 < 140/90), so each row fills its cell exactly and the rows together
    # fill the page. Swap the cell and width binds instead, leaving every copy
    # short: 71.5mm of ink in a 99mm row.
    assert bottom - top == pytest.approx(page_h, abs=0.5)


def test_grid_svg_survives_a_gutter_wider_than_its_cell(tmp_path):
    """`layout -m` subtracts twice the margin with no positivity check, so an
    over-wide gutter used to invert the scale: mirrored, enlarged, off-page."""
    src = FIXTURES / "multi-layer.svg"
    out = tmp_path / "g.svg"
    cell = 50.0
    svg_optimize.grid_svg(src, out, 2, 2, cell, cell,
                          gutter_mm=svg_optimize.clamp_gutter_mm(200.0, cell, cell))

    page_w, page_h = _page_mm(out)
    left, top, right, bottom = _ink_mm(out)
    assert left >= -0.5 and top >= -0.5
    assert right <= page_w + 0.5 and bottom <= page_h + 0.5


@pytest.mark.parametrize("gutter, cell_w, cell_h, expected", [
    (10.0, 100.0, 80.0, 10.0),    # comfortably inside the cell — untouched
    (-5.0, 100.0, 80.0, 0.0),     # negative gutters floor at zero
    (200.0, 100.0, 80.0, 40.0),   # capped at half the *smaller* dimension
    (80.0, 100.0, 80.0, 40.0),    # exactly the cell height is still too much
])
def test_clamp_gutter_leaves_the_copy_half_its_cell(gutter, cell_w, cell_h, expected):
    assert svg_optimize.clamp_gutter_mm(gutter, cell_w, cell_h) == pytest.approx(expected)


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
    svg_optimize.grid_svg(src, out, 2, 2, 80.0, 60.0, gutter_mm=0.0)
    # reconcile_layers (run by the queue) re-inserts a dropped-empty layer so
    # indices stay aligned; do the same here and assert both labels come back.
    svg_utils.reconcile_layers(src, out)
    labels = [l["label"] for l in svg_utils.parse_layers(out)["layers"]]
    assert labels == ["ink", "empty"]


def test_staged_plot_filters_a_gridded_layer_to_all_its_copies(tmp_path):
    """grid + pause_between_layers: a stage filters the gridded file by layer
    index, so plotting layer 1 draws that layer of *every* copy."""
    src = FIXTURES / "multi-layer.svg"
    base = svg_utils.parse_layers(src)
    tiled = tmp_path / "g.svg"
    cols, rows = 2, 3
    svg_optimize.grid_svg(src, tiled, cols, rows, 100.0, 100.0, 0.0)

    stage = tmp_path / "s0.svg"
    svg_utils.filter_to_layers(tiled, [1], stage, include_orphans=False)
    kept = svg_utils.parse_layers(stage)
    assert [l["label"] for l in kept["layers"]] == [base["layers"][1]["label"]]
    per_copy = base_layer_subpaths(src, 1)
    assert kept["subpath_count"] == per_copy * cols * rows


def base_layer_subpaths(src: Path, index: int) -> int:
    tmp = src.parent / f".{src.stem}.one.svg"
    try:
        svg_utils.filter_to_layers(src, [index], tmp, include_orphans=False)
        return svg_utils.parse_layers(tmp)["subpath_count"]
    finally:
        tmp.unlink(missing_ok=True)


def test_queue_produces_both_derivatives_and_records_grid_status(tmp_path):
    """The optimize_queue task runs optimize (phase 1) then grid (phase 2),
    leaving {svg_id}.grid.svg on disk and a ':grid' status entry ready."""
    from app import main

    svg_id = "_grid_it"
    (main.UPLOAD_DIR / f"{svg_id}.svg").write_bytes((FIXTURES / "multi-layer.svg").read_bytes())
    job = {
        "svg_id": svg_id, "grid_enabled": True, "grid_copies": 4,
        "grid_gutter_mm": 0.0,
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
        "grid_gutter_mm": 0.0,
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
        left, _, right, _ = _ink_mm(grid_file)
        # 2x1 spans the strip. The 1x2 the sheet's own aspect used to pick
        # stacks two 48.5mm bands a third of the width instead.
        assert right - left == pytest.approx(210.0, abs=1.0)
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
        "grid_gutter_mm": 0.0,
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


def test_patch_persists_and_clamps_grid_fields(client, job_from_svg):
    job = job_from_svg(FIXTURES / "multi-layer.svg")
    res = client.patch(f"/jobs/{job['job_id']}",
                       json={"grid_enabled": True, "grid_copies": 999,
                             "grid_gutter_mm": -5})
    assert res.status_code == 200
    body = res.json()
    assert body["grid_enabled"] is True
    assert body["grid_copies"] == 64        # clamped to the max
    assert body["grid_gutter_mm"] == 0.0    # floored


def test_placement_endpoint_reports_the_tiled_size(client, job_from_svg):
    from app import main as _main
    job = job_from_svg(FIXTURES / "multi-layer.svg",
                       grid_enabled=True, paper_width_mm=297.0,
                       paper_height_mm=420.0)
    # Stand in for the queue: a ready {svg_id}.grid.svg next to the source, and
    # the status entry that marks it current for these settings.
    grid_file = _main.UPLOAD_DIR / f"{job['svg_id']}.grid.svg"
    svg_optimize.grid_svg(_main.UPLOAD_DIR / f"{job['svg_id']}.svg",
                          grid_file, 2, 2, 148.5, 210.0, 0.0)
    state.set_svg_status(
        f"{job['svg_id']}:grid", "ready",
        settings_key=optimize_queue.grid_settings_key(
            optimize_queue.settings_from_job(job)))
    try:
        res = client.post(f"/jobs/{job['job_id']}/placement",
                          json={"paper_width_mm": 297.0, "paper_height_mm": 420.0})
        assert res.status_code == 200
        body = res.json()
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
        "grid_enabled": True, "grid_copies": 4, "grid_gutter_mm": 0.0,
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


def test_gridded_document_places_onto_its_sheet(tmp_path):
    """The tiled file is sized to the sheet, so the placement engine positions
    it near-identity — margins/fit/transform then act on the whole grid."""
    src = FIXTURES / "multi-layer.svg"
    tiled = tmp_path / "g.svg"
    # 8-up on an A3 landscape sheet, no margins.
    pw, ph = 420.0, 297.0
    cols, rows = svg_optimize.arrangement(8, pw, ph, 100.0, 75.0)
    svg_optimize.grid_svg(src, tiled, cols, rows, pw / cols, ph / rows, 0.0)

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


def _dot_positions_mm(path: Path) -> tuple[list[float], list[float]]:
    layer = _cut_marks_layer(path)
    root = etree.parse(str(path)).getroot()
    doc_w, doc_h = svg_utils.svg_size_mm(root)
    _, _, vb_w, vb_h = svg_utils.parse_viewbox(root.get("viewBox"))
    xs = sorted({round(float(c.get("cx")) * doc_w / vb_w, 3) for c in layer})
    ys = sorted({round(float(c.get("cy")) * doc_h / vb_h, 3) for c in layer})
    return xs, ys


def _tiled(tmp_path: Path, cols: int, rows: int, cell_w: float, cell_h: float,
           gutter: float, marks: bool = True) -> Path:
    src = FIXTURES / "multi-layer.svg"
    out = tmp_path / "g.svg"
    svg_optimize.grid_svg(src, out, cols, rows, cell_w, cell_h, gutter)
    svg_utils.reconcile_layers(src, out)
    if marks:
        svg_utils.add_cut_marks(out, cols, rows, cell_w, cell_h, gutter)
    return out


def test_cut_marks_without_a_gutter_sit_on_every_copy_corner(tmp_path):
    out = _tiled(tmp_path, 2, 3, 100.0, 80.0, gutter=0.0)
    xs, ys = _dot_positions_mm(out)
    # Copies touch, so their corners are the cell lattice, sheet edges included.
    assert xs == [0.0, 100.0, 200.0]
    assert ys == [0.0, 80.0, 160.0, 240.0]
    assert len(_cut_marks_layer(out)) == len(xs) * len(ys)


def test_cut_marks_with_a_gutter_sit_mid_gap_between_copies(tmp_path):
    out = _tiled(tmp_path, 2, 3, 100.0, 80.0, gutter=10.0)
    xs, ys = _dot_positions_mm(out)
    # Outer dots stay on the outermost copies' own edges (half a gutter in from
    # the sheet); the interior ones move to the middle of the gap.
    assert xs == [5.0, 100.0, 195.0]
    assert ys == [5.0, 80.0, 160.0, 235.0]


def test_cut_marks_layer_takes_no_layer_index(tmp_path):
    """The marks are a real Inkscape layer, but not one any selection can
    address — the upload's layer numbering has to come back unchanged."""
    base = svg_utils.parse_layers(FIXTURES / "multi-layer.svg")
    out = _tiled(tmp_path, 2, 2, 100.0, 100.0, gutter=6.0)
    tiled = svg_utils.parse_layers(out)
    assert [(l["index"], l["label"]) for l in tiled["layers"]] \
        == [(l["index"], l["label"]) for l in base["layers"]]
    layer = _cut_marks_layer(out)
    assert layer.get(svg_utils.LABEL_ATTR) == svg_utils.CUT_MARKS_LABEL
    # And it must not answer to a vpype layer id an artwork layer already owns,
    # or a measuring pass would fold the dots into that layer's geometry.
    ids = [svg_utils._vpype_layer_id(g.get(svg_utils.LABEL_ATTR) or "",
                                     g.get("id") or "", order)
           for order, g in enumerate(svg_utils._top_level_layers(
               etree.parse(str(out)).getroot()), start=1)]
    assert svg_utils._vpype_layer_id("", layer.get("id"), len(ids) + 1) not in ids


def test_cut_marks_plot_once_across_a_staged_job(tmp_path):
    """Staged plotting filters per layer; the marks belong to no stage, so they
    ride along with the first one exactly like un-layered content."""
    out = _tiled(tmp_path, 2, 2, 100.0, 100.0, gutter=0.0)
    first, second = tmp_path / "s0.svg", tmp_path / "s1.svg"
    svg_utils.filter_to_layers(out, [0], first, include_orphans=True)
    svg_utils.filter_to_layers(out, [1], second, include_orphans=False)
    assert _cut_marks_layer(first) is not None
    assert _cut_marks_layer(second) is None


def test_cut_marks_are_opt_in(tmp_path):
    assert _cut_marks_layer(_tiled(tmp_path, 2, 2, 100.0, 100.0, 0.0, marks=False)) is None


def test_cut_marks_change_the_grid_cache_key():
    base = {
        "grid_enabled": True, "grid_copies": 4, "grid_gutter_mm": 0.0,
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


def test_patch_persists_grid_cut_marks(client, job_from_svg):
    job = job_from_svg(FIXTURES / "multi-layer.svg")
    assert job["grid_cut_marks"] is False
    res = client.patch(f"/jobs/{job['job_id']}",
                       json={"grid_enabled": True, "grid_cut_marks": True})
    assert res.status_code == 200
    assert res.json()["grid_cut_marks"] is True


def test_queue_grid_phase_adds_cut_marks(tmp_path):
    """End of the wiring: grid_cut_marks on the job puts the marks layer into
    {svg_id}.grid.svg, after the reconcile that fixes the layer numbering."""
    from app import main

    svg_id = "_grid_marks_it"
    (main.UPLOAD_DIR / f"{svg_id}.svg").write_bytes((FIXTURES / "multi-layer.svg").read_bytes())
    job = {
        "svg_id": svg_id, "grid_enabled": True, "grid_copies": 4,
        "grid_gutter_mm": 8.0, "grid_cut_marks": True,
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
        # 4 copies on a square sheet -> 2x2 cells of 100mm, gutter 8 -> inset 4.
        assert _dot_positions_mm(grid_file) == ([4.0, 100.0, 196.0],
                                                [4.0, 100.0, 196.0])
        # The artwork's own layer numbering is untouched.
        assert [l["label"] for l in svg_utils.parse_layers(grid_file)["layers"]] \
            == [l["label"] for l in svg_utils.parse_layers(FIXTURES / "multi-layer.svg")["layers"]]
    finally:
        for p in main.UPLOAD_DIR.glob(f"{svg_id}*"):
            p.unlink()
        state.clear_svg_status(svg_id)
        state.clear_svg_status(f"{svg_id}:grid")
