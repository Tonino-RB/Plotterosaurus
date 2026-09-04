"""Shared fixtures.

Three things live here that no single test module should own: an HTTP client,
a factory that turns any SVG on disk into a real queue job, and — the reason
this file exists at all — inputs at the scale the plotter actually sees.

Every fixture in `fixtures/` is under 600 bytes. That was a blind spot, and it
cost us: the placement endpoint was benchmarked against a document containing
one rectangle, shipped, and then took seven seconds per preview on a real
drawing. Small fixtures answer "is the math right?" and answer it well. They
cannot answer "is this still usable?", because every cost in this pipeline
scales with element count and none of them have any elements.

So `heavy_svg` builds a document the size of real work — a few megabytes of
polylines across several layers — and `real_svgs` lets you point the suite at
your own drawings, which is the only way to cover the markup no synthetic
document thinks to produce.
"""
import math
import os
import time
from pathlib import Path

import pytest

from app import main, optimize_queue, plot_worker, state

# Never `from app.main import UPLOAD_DIR`: the sandbox fixture below rebinds
# that attribute, and a from-import would keep pointing at the real uploads
# directory. Read it through the module, at call time.

TestClient = pytest.importorskip(
    "starlette.testclient", reason="httpx not installed"
).TestClient


# Keep the tests off the running plotter's data ----------------------------

@pytest.fixture(scope="session", autouse=True)
def _sandbox_server_state(tmp_path_factory):
    """Point state.json and uploads/ at a temp directory for the whole session.

    Autouse and session-scoped because getting this wrong is not a test
    failure, it is data loss: `app.state` writes to the repo's own state.json,
    and pytest never calls `state.init()`, so the in-memory queue starts empty
    and the first `add_job`/`remove_job` persists that empty queue straight
    over the live one. It cost a real queue of real jobs before this existed.
    The service holding the same file open is what made it look survivable —
    it rewrites state.json on the next change, so the damage only becomes
    visible after a restart, which can be hours later.

    Nothing here is a test of persistence. If something ever does need to test
    that, it should assert against this sandboxed path, never the real one.
    """
    sandbox = tmp_path_factory.mktemp("server")
    uploads = sandbox / "uploads"
    uploads.mkdir()

    state.STATE_PATH = sandbox / "state.json"
    state.UPLOAD_DIR = uploads
    state.DRAW_TRACE_PATH = sandbox / "draw_trace.jsonl"
    main.UPLOAD_DIR = uploads
    # Both queues cache the constant on first use; seed the cache instead so
    # they never resolve the real one.
    optimize_queue._UPLOAD_DIR_LAZY = uploads
    plot_worker._UPLOAD_DIR_LAZY = uploads

    yield uploads


# Heavy synthetic document -------------------------------------------------
#
# Sized and shaped from the two files that exposed the regression: Inkscape
# exports of hatched artwork, 4.5MB/8.0MB, ~8,000 and ~14,500 elements, almost
# all of them polylines, spread over a handful of layers, on a large-format
# canvas whose viewBox units are not millimetres.
#
# 3MB reproduces every cost in the pipeline at the right order of magnitude
# (~45ms to parse, ~4s for vpype to measure) while keeping the fixture cheap
# enough to build once per session. Going bigger sharpens nothing: the failure
# mode being guarded against is four orders of magnitude wide.

HEAVY_LABELS = ("outline", "fill", "shading", "accent")
HEAVY_LAYERS = len(HEAVY_LABELS)
HEAVY_POLYLINES_PER_LAYER = 900
HEAVY_POINTS_PER_POLYLINE = 40


def _build_heavy_svg() -> str:
    """A polyline-dense multi-layer document. Deterministic — no randomness,
    so a failure is reproducible and the ink rectangle is a fixed number."""
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg"',
        ' xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"',
        # Real large-format export: centimetre units, viewBox in px-ish units.
        ' width="92.81583333cm" height="131.25979167cm" viewBox="0 0 3508 4961">',
    ]
    for layer in range(HEAVY_LAYERS):
        out.append('<g inkscape:groupmode="layer" '
                   f'inkscape:label="{HEAVY_LABELS[layer]}">')  # digit-free: see CURVY_LABELS
        for i in range(HEAVY_POLYLINES_PER_LAYER):
            phase = (layer * HEAVY_POLYLINES_PER_LAYER + i) * 0.017
            points = []
            for k in range(HEAVY_POINTS_PER_POLYLINE):
                t = k / (HEAVY_POINTS_PER_POLYLINE - 1)
                x = 100 + t * 3300
                y = 100 + layer * 1200 + 400 * (0.5 + 0.5 * math.sin(phase + t * 6.283))
                points.append(f"{x:.4f},{y:.4f}")
            out.append('<polyline fill="none" stroke="#000" stroke-width="0.8" '
                       f'points="{" ".join(points)}"/>')
        out.append("</g>")
    out.append("</svg>")
    return "\n".join(out)


@pytest.fixture(scope="session")
def heavy_svg(tmp_path_factory) -> Path:
    """A ~3MB, 3,600-polyline, 4-layer document. Built once per session, into
    a tmp dir — a multi-megabyte binary has no business in a git repository,
    least of all a public one."""
    path = tmp_path_factory.mktemp("heavy") / "heavy.svg"
    path.write_text(_build_heavy_svg())
    return path


# Heavy curved document ----------------------------------------------------
#
# The polyline fixture above covers hatched exports. It cannot cover curves,
# and curves are a different cost entirely: a polyline arrives already flat,
# while a bezier is expanded by whatever reads it. Byte size stops predicting
# anything — this fixture is smaller than heavy_svg and, at the flattening
# setting the code used to use, produced 102 million points and 2.69GB of RSS
# against heavy_svg's 309 thousand and 400MB.
#
# That gap is not academic. It is the difference between a measurement and an
# out-of-memory crash on a 3.7GB board that is also running a browser, and no
# fixture in the corpus could express it until this one.

CURVY_LABELS = ("ink", "wash", "detail", "signature")
CURVY_LAYERS = len(CURVY_LABELS)
CURVY_PATHS_PER_LAYER = 700
CURVY_SEGMENTS = 14


def _build_curvy_svg() -> str:
    """Flowing cubic beziers — organic pen work, as opposed to hatch fill.

    Deterministic, and genuinely curved: every segment gets two distinct
    control points, so nothing here is a straight line wearing a C command.
    """
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg"',
        ' xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"',
        ' width="92.81583333cm" height="131.25979167cm" viewBox="0 0 3508 4961">',
    ]
    for layer in range(CURVY_LAYERS):
        # Digit-free labels on purpose: vpype derives a layer id from the
        # first group of digits in the label, so "curves0"/"curves1" would
        # both become layer 1 and their geometry would merge. Real exports
        # name layers "White", "Gold", "hatched".
        out.append('<g inkscape:groupmode="layer" '
                   f'inkscape:label="{CURVY_LABELS[layer]}">')
        for i in range(CURVY_PATHS_PER_LAYER):
            phase = (layer * CURVY_PATHS_PER_LAYER + i) * 0.021
            x, y = 120.0, 120.0 + layer * 1180 + 300 * math.sin(phase)
            d = [f"M {x:.3f},{y:.3f}"]
            for s in range(CURVY_SEGMENTS):
                t = s / CURVY_SEGMENTS
                c1x = x + 60 + 40 * math.cos(phase + t * 5.1)
                c1y = y + 150 * math.sin(phase * 1.7 + t * 4.3)
                c2x = x + 150 + 50 * math.sin(phase + t * 3.9)
                c2y = y - 140 * math.cos(phase * 1.3 + t * 6.1)
                x += 230
                y += 190 * math.sin(phase + t * 2.7)
                d.append(f"C {c1x:.3f},{c1y:.3f} {c2x:.3f},{c2y:.3f} {x:.3f},{y:.3f}")
            out.append('<path fill="none" stroke="#000" stroke-width="0.8" '
                       f'd="{" ".join(d)}"/>')
        out.append("</g>")
    out.append("</svg>")
    return "\n".join(out)


@pytest.fixture(scope="session")
def curvy_svg(tmp_path_factory) -> Path:
    """A ~2.4MB, 2,800-path, 39,200-segment bezier document, 4 layers."""
    path = tmp_path_factory.mktemp("curvy") / "curvy.svg"
    path.write_text(_build_curvy_svg())
    return path


# Real drawings ------------------------------------------------------------

REAL_SVG_ENV = "PLOTTEROSAURUS_REAL_SVGS"
REAL_SVG_DIR = Path(__file__).parent / "real"


def real_svgs() -> list[Path]:
    """Drop real drawings in `tests/real/` and the suite picks them up.

    That directory is gitignored, which is the point: the interesting files are
    the user's own artwork — megabytes each, and not this repo's to publish —
    but they are also the only inputs carrying what no fixture author thinks to
    type. CSS classes and <style> blocks, <use>/<defs>, clip paths, transforms
    nested three deep, embedded rasters, `width="100%"`, sixteen digits of
    coordinate precision, layers whose labels collide on a number inside vpype.

    $PLOTTEROSAURUS_REAL_SVGS overrides the location, for running against a
    library you would rather not copy:

        PLOTTEROSAURUS_REAL_SVGS=~/plots venv/bin/python -m pytest tests/ -q
    """
    root = os.environ.get(REAL_SVG_ENV)
    directory = Path(root).expanduser() if root else REAL_SVG_DIR
    if not directory.is_dir():
        return []
    # Skip the derivatives the pipeline writes beside an upload, or the suite
    # measures its own output and calls it a second opinion.
    return sorted(p for p in directory.glob("*.svg")
                  if not p.name.endswith((".preview.svg", ".combined.filt.svg",
                                          ".opt.svg")))


@pytest.fixture
def client():
    # No lifespan: it starts the plot worker's background queues, which these
    # tests have no business touching.
    return TestClient(main.app)


# Jobs ---------------------------------------------------------------------

_JOB_DEFAULTS = {
    "paper_width_mm": 210.0, "paper_height_mm": 297.0,
    "margin_top_mm": 0.0, "margin_right_mm": 0.0,
    "margin_bottom_mm": 0.0, "margin_left_mm": 0.0,
    "fit_content": False, "transform_scale": 1.0,
    "transform_rotation_deg": 0.0,
    "transform_offset_x_mm": 0.0, "transform_offset_y_mm": 0.0,
    "speed_pendown": 25, "speed_penup": 75, "acceleration": 75,
    "acceleration_penup": 75, "cornering": 10,
    "pen_rate_lower": 50, "pen_rate_raise": 75,
    "pen_delay_down": 0, "pen_delay_up": 0,
    "optimize_svg": False, "optimize_expert_undo_depth": 0,
    "grid_enabled": False, "grid_copies": 4,
    "grid_spacing_x_mm": 0.0, "grid_spacing_y_mm": 0.0,
    "grid_spacing_linked": True, "grid_cut_marks": False,
}


@pytest.fixture
def job_from_svg():
    """Factory: copy an SVG into the upload dir and queue a job for it.

    Each call gets its own upload path, which also means its own cold cache —
    the placement caches are keyed on (path, mtime), so a test that wants to
    measure a first request does not have to reach in and clear them.
    """
    created = []

    def make(source: Path, *, layers: list[dict] | None = None, **overrides) -> dict:
        svg_id = f"_test_{len(created)}_{os.getpid()}"
        (main.UPLOAD_DIR / f"{svg_id}.svg").write_bytes(Path(source).read_bytes())
        record = state.add_job({
            "svg_id": svg_id, "filename": Path(source).name,
            "layer_selections": layers or [{"index": 0, "label": "art"}],
            **_JOB_DEFAULTS, **overrides,
        })
        created.append((record["job_id"], svg_id))
        return record

    yield make

    for job_id, svg_id in created:
        state.remove_job(job_id)
        for leftover in main.UPLOAD_DIR.glob(f"{svg_id}*"):
            leftover.unlink()


def placement_with_ink(client, job, query, timeout=180.0):
    """POST /placement and wait for the ink measurement to actually land.

    Ink is measured off the request path now — the endpoint answers
    `ink_measured: false` and starts a background read, exactly as it does for
    the browser. Tests have to poll the same way the UI does, so this is not a
    test-only convenience: if this loop never terminates, neither does the
    size readout.
    """
    deadline = time.monotonic() + timeout
    while True:
        res = client.post(f"/jobs/{job['job_id']}/placement",
                          json={**query, "include_ink": True})
        assert res.status_code == 200, res.text
        body = res.json()
        if body["ink_measured"]:
            return body
        assert time.monotonic() < deadline, "ink measurement never completed"
        time.sleep(0.05)


@pytest.fixture
def heavy_job(heavy_svg, job_from_svg) -> dict:
    """The heavy document, queued, with all four layers selected."""
    layers = [{"index": i, "label": label}
              for i, label in enumerate(HEAVY_LABELS)]
    return job_from_svg(heavy_svg, layers=layers)


@pytest.fixture
def curvy_job(curvy_svg, job_from_svg) -> dict:
    """The curved document, queued, with all four layers selected."""
    layers = [{"index": i, "label": label}
              for i, label in enumerate(CURVY_LABELS)]
    return job_from_svg(curvy_svg, layers=layers)
