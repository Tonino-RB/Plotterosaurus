"""Curves, which cost nothing like polylines do.

Every other fixture in this corpus is flat geometry. That was a gap with teeth:
a polyline arrives already flattened, so byte size roughly predicts cost, while
a bezier is expanded by whatever reads it and byte size predicts nothing. The
curved fixture here is *smaller* than the polyline one and, at the flattening
setting this code used to use, produced 330 times more geometry:

    heavy_svg   3.0 MB polylines   309 thousand points    400 MB    9.7 s
    curvy_svg   2.4 MB beziers     102 million points    2690 MB   54.5 s

2.69GB on a 3.7GB board that is also running a browser is not a slow
measurement, it is the machine going down. These tests exist so that stays
fixed, and so the reason it is safe to fix stays true.
"""
import time

import pytest

from app import svg_utils

from .conftest import placement_with_ink

A4 = {"paper_width_mm": 210.0, "paper_height_mm": 297.0}


def placement(client, job, **overrides):
    res = client.post(f"/jobs/{job['job_id']}/placement", json={**A4, **overrides})
    assert res.status_code == 200, res.text
    return res.json()


# Why loosening the flattening is free --------------------------------------

@pytest.mark.parametrize("shape", [
    '<circle cx="200" cy="200" r="180" fill="none" stroke="#000"/>',
    '<path fill="none" stroke="#000" d="M20,200 C20,50 380,50 380,200"/>',
    '<path fill="none" stroke="#000" d="M20,200 A180,180 0 0 1 380,200"/>',
    '<ellipse cx="200" cy="200" rx="180" ry="90" fill="none" stroke="#000"/>',
])
def test_flattening_detail_does_not_move_the_bounding_box(tmp_path, shape):
    """The load-bearing fact behind BOUNDS_QUANTIZATION.

    A chord approximation is inscribed, so a coarser one can only understate a
    curve whose extreme falls mid-arc — every shape here has one. Measured, the
    understatement at the setting in use is 36 to 49 *nanometres* on <path>
    geometry and exactly zero on <circle>/<ellipse>, which vpype resolves
    analytically.

    One micron of tolerance: three hundred times finer than the line a pen
    actually lays down, and twenty times above the error being allowed for. If
    a future vpype starts reading extremes off the samples instead, the error
    grows with the setting and this is what fails.
    """
    import vpype

    path = tmp_path / "shape.svg"
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape='
                    '"http://www.inkscape.org/namespaces/inkscape" width="400mm" '
                    'height="400mm" viewBox="0 0 400 400">'
                    '<g inkscape:groupmode="layer" inkscape:label="a">'
                    f'{shape}</g></svg>')

    fine = vpype.read_multilayer_svg(str(path), quantization=0.001).bounds()
    used = vpype.read_multilayer_svg(
        str(path), quantization=svg_utils.BOUNDS_QUANTIZATION).bounds()

    error_mm = max(abs(a - b) for a, b in zip(used, fine)) / svg_utils.PX_PER_MM
    assert error_mm < 0.001, f"bounds moved by {error_mm * 1000:.3f} um"


def test_the_plot_path_never_sees_the_measurement_setting():
    """BOUNDS_QUANTIZATION is only safe because nothing that drives the pen
    reads it. The machine is driven by pyaxidraw parsing the SVG itself, and
    the one vpype pass that changes what gets drawn — Optimize SVG — is a
    separate CLI invocation with its own tolerance.
    """
    import inspect

    from app import plot_worker, svg_optimize

    for module in (plot_worker, svg_optimize):
        source = inspect.getsource(module)
        assert "BOUNDS_QUANTIZATION" not in source, (
            f"{module.__name__} reads the measurement-only flattening setting")
        assert "read_multilayer_svg" not in source, (
            f"{module.__name__} measures with vpype on a path that reaches the plot")


# The curved document, end to end -------------------------------------------

def test_placement_on_a_curved_document_is_still_arithmetic(client, curvy_job):
    """Placement reads three root attributes, so curves change nothing about
    it. Worth asserting rather than assuming: this is the render path, and it
    is the one thing that must not scale with drawing complexity at all."""
    start = time.perf_counter()
    body = placement(client, curvy_job)
    cold = time.perf_counter() - start

    assert body["ink_measured"] is False
    assert body["layout_width_mm"] > 0
    assert cold < 1.0, f"first placement of a curved document took {cold:.2f}s"

    timings = []
    for i in range(20):
        t = time.perf_counter()
        placement(client, curvy_job, transform_rotation_deg=float(i))
        timings.append(time.perf_counter() - t)
    timings.sort()
    median = timings[len(timings) // 2]
    assert median < 0.100, f"median {median * 1000:.1f}ms dragging on curves"


def test_measuring_a_curved_document_stays_within_memory(client, curvy_job):
    """The crash, as a test.

    Bounded by resident set rather than by time, because time was never what
    killed the board. At the old flattening setting this same file peaked at
    2.69GB; 1GB leaves a wide margin over the measured 380MB while still being
    far under anything survivable-by-accident.
    """
    import os
    import threading

    peak = [0]
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            try:
                status = open(f"/proc/{os.getpid()}/status").read()
                peak[0] = max(peak[0], int(status.split("VmRSS:")[1].split()[0]))
            except (OSError, IndexError):
                pass
            time.sleep(0.1)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        body = placement_with_ink(client, curvy_job, A4)
    finally:
        stop.set()
        watcher.join(timeout=1)

    assert body["ink"] is not None and body["ink"]["width_mm"] > 0
    peak_mb = peak[0] / 1024
    assert peak_mb < 1024, f"measuring a curved document peaked at {peak_mb:.0f} MB"


def test_layers_that_collide_in_vpype_measure_correctly_once_normalized(tmp_path):
    """`ink_rects_by_layer` reads the whole document and maps vpype's layer ids
    back to layer indices. That mapping is only one-to-one after
    `normalize_layer_structure` has run, which is what the upload path does.

    vpype takes a layer id from the first group of digits in the label, so
    "curve1" and "curve2" would otherwise both land on... different ids, but
    "pass1a"/"pass1b" collide on 1 and their geometry merges — silently
    reporting one layer's bounds for both. This is the case that made the
    curved fixture's own labels wrong, so it is worth pinning on the real path
    rather than trusting that uploads always normalize.
    """
    path = tmp_path / "collide.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape='
        '"http://www.inkscape.org/namespaces/inkscape" width="400mm" '
        'height="400mm" viewBox="0 0 400 400">'
        '<g inkscape:groupmode="layer" inkscape:label="pass1a">'
        '<path fill="none" stroke="#000" d="M10,10 C60,10 60,60 10,60"/></g>'
        '<g inkscape:groupmode="layer" inkscape:label="pass1b">'
        '<path fill="none" stroke="#000" d="M300,300 C350,300 350,350 300,350"/>'
        '</g></svg>')
    svg_utils.normalize_layer_structure(path)

    rects = svg_utils.ink_rects_by_layer(path)
    for index in (0, 1):
        assert svg_utils.union_rect([rects.get(index)]) == pytest.approx(
            svg_utils.ink_rect_doc_mm(path, [index])), f"layer {index}"
    # And the two really are distinct regions, so a merge would have shown up.
    assert rects[0][2] < rects[1][0]


def test_curved_layers_union_the_same_way_flat_ones_do(curvy_svg):
    """The per-layer measurement has to be right on curves too — a subset's
    rectangle is the union of its layers', and that is what the endpoint
    serves instead of re-reading the file per selection."""
    rects = svg_utils.ink_rects_by_layer(curvy_svg)
    assert len(rects) == 4, f"expected four measured layers, got {sorted(rects)}"

    # One subset, not every subset: each comparison costs a whole extra read of
    # the document, and the exhaustive check over all layer combinations
    # already runs against the flat fixtures, where it is cheap. What is new
    # here is only that curves survive the same mapping.
    subset = [1, 2]
    assert svg_utils.union_rect(rects.get(i) for i in subset) == pytest.approx(
        svg_utils.ink_rect_doc_mm(curvy_svg, subset))
