"""Redraw the last stretch of a paused plot.

From a pause, `/queue/redraw` rewinds the resume SVG's `pause_dist` (pen-down
travel completed, µm) by a distance and lets the plot carry on — so a skipped
line or a stretch a dry pen missed gets traced again. The rewind is a pure
rewrite of one attribute; `res_plot` does the rest. These pin the rewrite
math, the clamp at the start of the current layer, and the guard that the job
really is paused with resume data.
"""
from pathlib import Path

import pytest
from lxml import etree

from app import main, plot_worker, state, svg_utils

# A paused plot's resume SVG: one polyline plus the <plotdata> element the
# AxiDraw stack writes. pause_dist / pause_ref are µm integers (254000 = 254mm
# of pen-down travel); last_x / last_y are the physical pen position in mm.
RESUME_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" '
    'viewBox="0 0 210 297">'
    '<polyline points="10,10 90,90 90,10" fill="none" stroke="#000"/>'
    '<plotdata application="axidraw" model="2" plob_version="1" layer="-2" '
    'pause_dist="254000" pause_ref="254000" last_x="90.0" last_y="10.0" '
    'rand_seed="1" row="0"/>'
    '</svg>'
)


def _plotdata(path: Path) -> dict:
    node = etree.parse(str(path)).xpath("//*[local-name()='plotdata']")[0]
    return dict(node.attrib)


# rewind_resume_distance ----------------------------------------------------

def test_rewind_subtracts_distance_in_um(tmp_path):
    p = tmp_path / "r.svg"
    p.write_text(RESUME_SVG)

    removed = svg_utils.rewind_resume_distance(p, 100.0)

    assert removed == 100.0
    assert _plotdata(p)["pause_dist"] == "154000"


def test_rewind_clamps_at_zero_and_reports_what_it_removed(tmp_path):
    p = tmp_path / "r.svg"
    p.write_text(RESUME_SVG)

    removed = svg_utils.rewind_resume_distance(p, 9999.0)

    assert removed == 254.0                       # only what was there
    assert _plotdata(p)["pause_dist"] == "0"      # never negative


def test_rewind_touches_only_pause_dist(tmp_path):
    p = tmp_path / "r.svg"
    p.write_text(RESUME_SVG)

    svg_utils.rewind_resume_distance(p, 50.0)

    after = _plotdata(p)
    assert after["pause_ref"] == "254000"         # original pause, kept
    assert after["last_x"] == "90.0"
    assert after["last_y"] == "10.0"
    assert after["rand_seed"] == "1"
    # still one polyline, still parseable
    root = etree.parse(str(p)).getroot()
    assert len(root.xpath("//*[local-name()='polyline']")) == 1


# redraw_recent -----------------------------------------------------------------

@pytest.fixture
def paused(monkeypatch):
    """A job in `paused` with a resume SVG on disk; yields (job, resume_path,
    resume_calls). resume_active is stubbed so nothing touches hardware."""
    resume_path = main.UPLOAD_DIR / "rd.s0.resume.svg"
    resume_path.write_text(RESUME_SVG)

    calls = []
    monkeypatch.setattr(plot_worker, "resume_active",
                        lambda: calls.append(_plotdata(resume_path)["pause_dist"]))

    job = state.add_job({
        "svg_id": "rd", "filename": "rd.svg",
        "layer_selections": [{"index": 0, "label": "art", "selected": True}],
        "paper_width_mm": 210.0, "paper_height_mm": 297.0,
        "margin_top_mm": 0.0, "margin_right_mm": 0.0,
        "margin_bottom_mm": 0.0, "margin_left_mm": 0.0,
        "fit_content": False, "transform_scale": 1.0, "transform_rotation_deg": 0.0,
        "transform_offset_x_mm": 0.0, "transform_offset_y_mm": 0.0,
        "speed_pendown": 25, "speed_penup": 75, "acceleration": 75,
        "optimize_svg": False,
    })
    state.update_job(job["job_id"], status="plotting")
    state.update_job(job["job_id"], status="paused",
                     resume_path=str(resume_path),
                     distance_pendown_m=0.5, estimated_total_seconds=600.0,
                     progress_total_seconds=600.0)
    state.set_active(job["job_id"])

    yield state.get_job(job["job_id"]), resume_path, calls

    state.set_active(None)
    state.remove_job(job["job_id"])
    for leftover in main.UPLOAD_DIR.glob("rd*"):
        leftover.unlink()


def test_redraw_rewinds_then_resumes(paused):
    job, resume_path, calls = paused

    result = plot_worker.redraw_recent(50.0)

    assert result["rewound_mm"] == 50.0
    assert _plotdata(resume_path)["pause_dist"] == "204000"
    # resume_active saw the already-rewound file — the rewrite happens first
    assert calls == ["204000"]


def test_redraw_stretches_the_progress_denominator(paused):
    job, _, _ = paused

    plot_worker.redraw_recent(50.0)

    # 50mm of 500mm pen-down travel -> +10% of the 600s estimate
    assert state.get_job(job["job_id"])["progress_total_seconds"] == pytest.approx(660.0)


def test_redraw_past_the_layer_start_clamps(paused):
    job, resume_path, calls = paused

    result = plot_worker.redraw_recent(1000.0)      # 1000mm > 254mm in the plob

    assert result["rewound_mm"] == 254.0
    assert _plotdata(resume_path)["pause_dist"] == "0"
    assert calls == ["0"]


def test_redraw_needs_a_paused_job(paused):
    job, _, calls = paused
    state.update_job(job["job_id"], status="plotting")

    with pytest.raises(RuntimeError, match="No paused job to redraw"):
        plot_worker.redraw_recent(50.0)
    assert calls == []


def test_redraw_needs_resume_data(paused):
    job, _, calls = paused
    state.update_job(job["job_id"], resume_path=None)

    with pytest.raises(RuntimeError, match="No paused job to redraw"):
        plot_worker.redraw_recent(50.0)
    assert calls == []


# HTTP -------------------------------------------------------------------------

def test_endpoint_happy_path(client, paused):
    _, resume_path, calls = paused

    res = client.post("/queue/redraw", json={"distance_mm": 50})

    assert res.status_code == 200
    assert res.json()["rewound_mm"] == 50.0
    assert _plotdata(resume_path)["pause_dist"] == "204000"
    assert calls == ["204000"]


def test_endpoint_rejects_out_of_range(client, paused):
    for bad in (0, -5, 99999):
        res = client.post("/queue/redraw", json={"distance_mm": bad})
        assert res.status_code == 400
        assert res.json()["detail"]["code"] == "redraw_distance_out_of_range"


def test_endpoint_without_a_paused_job(client):
    state.set_active(None)
    res = client.post("/queue/redraw", json={"distance_mm": 50})
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "redraw_not_paused"
