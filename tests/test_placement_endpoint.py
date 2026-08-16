"""HTTP-level tests for POST /jobs/{job_id}/placement.

The preview stopped deriving geometry and now renders whatever this returns,
so the contract matters: the browser has no fallback if a field goes missing
or changes meaning. These check the wire shape and, most importantly, that the
answer is identical to the one the plot itself will use.
"""
import shutil
from pathlib import Path

import pytest

from app import main, state, svg_utils
from app.main import UPLOAD_DIR

TestClient = pytest.importorskip(
    "starlette.testclient", reason="httpx not installed"
).TestClient

FIXTURES = Path(__file__).parent / "fixtures"

# Fields the preview reads. Losing any of these silently breaks the UI.
REQUIRED_FIELDS = {
    "doc_width_mm", "doc_height_mm", "rotation_deg", "fit_scale", "user_scale",
    "layout_width_mm", "layout_height_mm", "footprint_width_mm",
    "footprint_height_mm", "center_x_mm", "center_y_mm", "ink", "ink_measured",
}

# Asking for ink costs a full vpype re-read of the file — seconds on a real
# drawing. The preview must never pay that, so it is opt-in.
WITH_INK = {"include_ink": True}

A4 = {"paper_width_mm": 210.0, "paper_height_mm": 297.0}


@pytest.fixture
def client():
    # No lifespan: it starts the plot worker's background queues, which this
    # has no business touching.
    return TestClient(main.app)


@pytest.fixture
def job(request):
    """A job backed by a real fixture SVG, cleaned up afterwards."""
    fixture = getattr(request, "param", "mm-canvas.svg")
    svg_id = "_test_placement"
    shutil.copy(FIXTURES / fixture, UPLOAD_DIR / f"{svg_id}.svg")
    record = state.add_job({
        "svg_id": svg_id, "filename": fixture,
        "layer_selections": [{"index": 0, "label": "art"}],
        **A4,
        "margin_top_mm": 0.0, "margin_right_mm": 0.0,
        "margin_bottom_mm": 0.0, "margin_left_mm": 0.0,
        "fit_content": False, "transform_scale": 1.0,
        "transform_rotation_deg": 0.0,
        "transform_offset_x_mm": 0.0, "transform_offset_y_mm": 0.0,
        "speed_pendown": 25, "speed_penup": 75, "acceleration": 75,
        "optimize_svg": False,
    })
    yield record
    state.remove_job(record["job_id"])
    for leftover in UPLOAD_DIR.glob(f"{svg_id}*"):
        leftover.unlink()


def test_returns_every_field_the_preview_reads(client, job):
    body = client.post(f"/jobs/{job['job_id']}/placement", json=A4).json()
    assert REQUIRED_FIELDS <= set(body)


def test_ink_is_opt_in(client, job):
    """Regression guard. The preview render path asks for placement only; if
    ink ever becomes part of the default response again, every preview waits
    on a vpype parse of the whole document before it can draw anything —
    seconds of blank canvas on a real drawing."""
    body = client.post(f"/jobs/{job['job_id']}/placement", json=A4).json()
    assert body["ink_measured"] is False
    assert body["ink"] is None
    # Placement itself is still fully answered.
    assert body["layout_width_mm"] > 0


def test_all_numbers_are_plain_json(client, job):
    """vpype measures with numpy. np.float64 happens to serialize, but the
    wire contract is plain numbers and must not depend on that."""
    body = client.post(f"/jobs/{job['job_id']}/placement", json={**A4, **WITH_INK}).json()
    numeric = [v for k, v in body.items() if k != "ink"]
    assert all(isinstance(v, (int, float)) for v in numeric)
    assert all(isinstance(v, float) for v in body["ink"].values())


@pytest.mark.parametrize("overrides", [
    {},
    {"fit_content": True},
    {"fit_content": True, "transform_rotation_deg": 45.0},
    {"transform_scale": 0.5, "transform_offset_x_mm": 20.0, "transform_offset_y_mm": -10.0},
    {"margin_top_mm": 10.0, "margin_left_mm": 15.0, "fit_content": True},
])
def test_ink_matches_what_the_plot_will_do(client, job, overrides):
    """The whole point of the endpoint. If this drifts, the preview is lying
    about where the pen goes — which is the class of bug it exists to end."""
    from app import config

    query = {**A4, **overrides, **WITH_INK}
    body = client.post(f"/jobs/{job['job_id']}/placement", json=query).json()
    ink = body["ink"]
    assert ink is not None

    expected = svg_utils.ink_bounds_mm(
        UPLOAD_DIR / f"{job['svg_id']}.svg", [0],
        query["paper_width_mm"], query["paper_height_mm"],
        query.get("margin_top_mm", 0.0), query.get("margin_right_mm", 0.0),
        query.get("margin_bottom_mm", 0.0), query.get("margin_left_mm", 0.0),
        query.get("fit_content", False),
        transform_scale=query.get("transform_scale", 1.0),
        transform_rotation_deg=query.get("transform_rotation_deg", 0.0),
        transform_offset_x_mm=query.get("transform_offset_x_mm", 0.0),
        transform_offset_y_mm=query.get("transform_offset_y_mm", 0.0),
        machine_auto_rotate=config.MACHINE_AUTO_ROTATE,
    )
    got = (ink["left_mm"], ink["top_mm"],
           ink["left_mm"] + ink["width_mm"], ink["top_mm"] + ink["height_mm"])
    assert got == pytest.approx(tuple(float(v) for v in expected))


@pytest.mark.parametrize("job", ["text-only.svg"], indirect=True)
def test_nothing_plottable_reports_null_ink(client, job):
    """A7: a document of live text draws nothing. The UI has to be able to
    tell that apart from a document it simply hasn't measured yet."""
    body = client.post(f"/jobs/{job['job_id']}/placement", json={**A4, **WITH_INK}).json()
    assert body["ink_measured"] is True   # measured, and found nothing
    assert body["ink"] is None
    # The placement itself is still valid — only the ink is absent.
    assert body["doc_width_mm"] == pytest.approx(210.0)


def test_empty_layer_selection_reports_null_ink(client, job):
    body = client.post(f"/jobs/{job['job_id']}/placement",
                       json={**A4, **WITH_INK, "layer_indices": []}).json()
    assert body["ink"] is None


def test_unknown_job_is_404(client):
    assert client.post("/jobs/nope/placement", json=A4).status_code == 404


def test_nonpositive_paper_is_rejected(client, job):
    res = client.post(f"/jobs/{job['job_id']}/placement",
                      json={"paper_width_mm": 0, "paper_height_mm": 297})
    assert res.status_code == 422
