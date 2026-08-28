"""The "Save As" export feature: app/export.py and GET /jobs/{id}/export.

Every converter is exercised against a real fixture and its output is checked
for the shape of the format it claims to be — not byte-for-byte (vpype and
cairosvg versions move), just "is this actually a PNG / PDF / G-code file".
"""
from pathlib import Path

import pytest

from app import export

FIXTURE = Path(__file__).parent / "fixtures" / "multi-layer.svg"


# app/export.py directly --------------------------------------------------

def test_svg_export_is_the_source_bytes(tmp_path):
    out = tmp_path / "o.svg"
    export.export(FIXTURE, out, "svg")
    assert out.read_bytes() == FIXTURE.read_bytes()


def test_png_export_has_png_magic(tmp_path):
    out = tmp_path / "o.png"
    export.export(FIXTURE, out, "png")
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_png_transparent_differs_from_white(tmp_path):
    white, clear = tmp_path / "w.png", tmp_path / "c.png"
    export.export(FIXTURE, white, "png", transparent=False)
    export.export(FIXTURE, clear, "png", transparent=True)
    assert white.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert clear.read_bytes() != white.read_bytes()


def test_pdf_export_has_pdf_magic(tmp_path):
    out = tmp_path / "o.pdf"
    export.export(FIXTURE, out, "pdf")
    assert out.read_bytes()[:5] == b"%PDF-"


def test_gcode_export_is_millimetre_gcode(tmp_path):
    out = tmp_path / "o.gcode"
    export.export(FIXTURE, out, "gcode")
    text = out.read_text()
    assert "G21" in text          # millimetres, not the inch-based `gcode` profile
    assert "G01" in text          # at least one draw move


def test_hpgl_export_looks_like_hpgl(tmp_path):
    out = tmp_path / "o.hpgl"
    export.export(FIXTURE, out, "hpgl")
    text = out.read_text()
    assert text.startswith("IN;")
    assert ";PD" in text          # pen-down segments


def _hpgl_abs_points(text):
    """Every absolute HPGL coordinate pair (PU + PD), in mm (40 units/mm)."""
    import re
    body = text.split("PA;", 1)[-1]
    nums = [float(v) / 40.0 for v in re.findall(r"-?\d+(?:\.\d+)?", body)]
    return list(zip(nums[0::2], nums[1::2]))


def test_hpgl_multilayer_geometry_is_faithful(tmp_path):
    """All three layers of multi-layer.svg land in the true 90 x 65 mm ink box
    — the absolute profile (see app/vpype_export.toml) has no per-layer origin
    reset to desync, and export.export flattens to one pen."""
    out = tmp_path / "o.hpgl"
    export.export(FIXTURE, out, "hpgl")
    pts = _hpgl_abs_points(out.read_text())
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    assert max(xs) - min(xs) == pytest.approx(90.0, abs=0.2)
    assert max(ys) - min(ys) == pytest.approx(65.0, abs=0.2)
    assert "SP2" not in out.read_text()   # flattened to one pen


def test_hpgl_curve_does_not_drift(tmp_path):
    """A finely tessellated circle keeps its radius: absolute coordinates round
    each point once (bounded), rather than accumulating like relative deltas.
    'near-square.svg' is an r=80mm circle (160mm bbox)."""
    circle = Path(__file__).parent / "fixtures" / "near-square.svg"
    out = tmp_path / "c.hpgl"
    export.export(circle, out, "hpgl")
    xs = [p[0] for p in _hpgl_abs_points(out.read_text())]
    ys = [p[1] for p in _hpgl_abs_points(out.read_text())]
    assert max(xs) - min(xs) == pytest.approx(160.0, abs=0.2)
    assert max(ys) - min(ys) == pytest.approx(160.0, abs=0.2)


def test_unknown_format_raises(tmp_path):
    with pytest.raises(export.ExportError):
        export.export(FIXTURE, tmp_path / "x", "eps")


def test_missing_source_raises(tmp_path):
    with pytest.raises(export.ExportError):
        export.export(tmp_path / "nope.svg", tmp_path / "o.svg", "svg")


def test_temp_file_is_cleaned_up(tmp_path):
    out = tmp_path / "o.pdf"
    export.export(FIXTURE, out, "pdf")
    leftovers = [p for p in tmp_path.iterdir() if p.name != "o.pdf"]
    assert leftovers == []


# GET /jobs/{id}/export --------------------------------------------------

@pytest.fixture
def export_job(job_from_svg):
    # optimize off → _effective_svg_path is the raw upload, so no vpype
    # optimize run has to complete before the export source exists.
    return job_from_svg(FIXTURE, optimize_svg=False)


@pytest.mark.parametrize("fmt,magic", [
    ("svg", b"<"),
    ("png", b"\x89PNG"),
    ("pdf", b"%PDF-"),
])
def test_endpoint_returns_attachment(client, export_job, fmt, magic):
    res = client.get(f"/jobs/{export_job['job_id']}/export", params={"fmt": fmt})
    assert res.status_code == 200, res.text
    assert res.content[:len(magic)] == magic
    cd = res.headers["content-disposition"]
    assert "attachment" in cd
    assert cd.endswith(f'.{fmt}"') or f".{fmt}" in cd


def test_endpoint_filename_is_the_drawing_name(client, export_job):
    res = client.get(f"/jobs/{export_job['job_id']}/export", params={"fmt": "pdf"})
    assert res.status_code == 200
    # multi-layer.svg → multi-layer.pdf
    assert 'filename="multi-layer.pdf"' in res.headers["content-disposition"]


def test_endpoint_png_transparent_query(client, export_job):
    white = client.get(f"/jobs/{export_job['job_id']}/export",
                       params={"fmt": "png"})
    clear = client.get(f"/jobs/{export_job['job_id']}/export",
                       params={"fmt": "png", "bg": "transparent"})
    assert white.status_code == clear.status_code == 200
    assert white.content != clear.content


def test_endpoint_bad_format_is_422(client, export_job):
    res = client.get(f"/jobs/{export_job['job_id']}/export", params={"fmt": "eps"})
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "export_bad_format"


def test_endpoint_unknown_job_is_404(client):
    res = client.get("/jobs/deadbeef/export", params={"fmt": "svg"})
    assert res.status_code == 404


# "As plotted" scope --------------------------------------------------

MULTI = [{"index": 0, "label": "outline"},
         {"index": 1, "label": "detail"},
         {"index": 2, "label": "signature"}]


def test_build_placed_svg_respects_layer_selection(tmp_path, job_from_svg):
    job = job_from_svg(FIXTURE, layers=[MULTI[0], MULTI[1],
                                        {**MULTI[2], "selected": False}])
    out = tmp_path / "p.svg"
    export.build_placed_svg(job, FIXTURE, out, apply_skew=False)
    # signature layer dropped → its path data ("M70 68") must be gone
    assert "70 68" not in out.read_text()


def test_build_placed_svg_skew_is_opt_in(tmp_path, job_from_svg, monkeypatch):
    """apply_skew gates the shear; a zero-skew machine is a no-op either way."""
    from app import config
    job = job_from_svg(FIXTURE, layers=MULTI)
    monkeypatch.setattr(config, "active_machine",
                        lambda: {"skew_deg": 2.0, "skew_true_axis": "x"})

    flat = tmp_path / "flat.svg"
    export.build_placed_svg(job, FIXTURE, flat, apply_skew=False)
    assert "matrix(" not in flat.read_text()        # picture stays square

    skewed = tmp_path / "skewed.svg"
    export.build_placed_svg(job, FIXTURE, skewed, apply_skew=True)
    assert "matrix(" in skewed.read_text()          # toolpath carries the shear

    monkeypatch.setattr(config, "active_machine",
                        lambda: {"skew_deg": 0.0, "skew_true_axis": "x"})
    nomachine = tmp_path / "nomachine.svg"
    export.build_placed_svg(job, FIXTURE, nomachine, apply_skew=True)
    assert "matrix(" not in nomachine.read_text()


def test_build_placed_svg_needs_a_selection(tmp_path, job_from_svg):
    job = job_from_svg(FIXTURE, layers=[{**MULTI[0], "selected": False}])
    with pytest.raises(export.ExportError):
        export.build_placed_svg(job, FIXTURE, tmp_path / "p.svg", apply_skew=False)


@pytest.fixture
def placed_job(job_from_svg):
    return job_from_svg(FIXTURE, layers=MULTI, optimize_svg=False,
                        paper_width_mm=210.0, paper_height_mm=297.0,
                        margin_top_mm=15.0, margin_right_mm=15.0,
                        margin_bottom_mm=15.0, margin_left_mm=15.0)


@pytest.mark.parametrize("fmt", ["svg", "png", "pdf", "gcode", "hpgl"])
def test_placed_endpoint_returns_attachment(client, placed_job, fmt):
    res = client.get(f"/jobs/{placed_job['job_id']}/export",
                     params={"fmt": fmt, "placed": "true"})
    assert res.status_code == 200, res.text
    assert f'.placed.{fmt}"' in res.headers["content-disposition"]


def test_placed_gcode_is_page_framed(client, placed_job):
    """Placed output sits inside the 210 x 297 page; the un-placed export uses
    the document's own 100 x 75 coordinates."""
    import re
    plain = client.get(f"/jobs/{placed_job['job_id']}/export",
                       params={"fmt": "gcode"}).text
    placed = client.get(f"/jobs/{placed_job['job_id']}/export",
                        params={"fmt": "gcode", "placed": "true"}).text
    assert placed != plain
    xs = [float(v) for v in re.findall(r"X(-?[\d.]+)", placed)]
    ys = [float(v) for v in re.findall(r"Y(-?[\d.]+)", placed)]
    assert min(xs) >= 0 and max(xs) <= 210.01
    assert min(ys) >= 0 and max(ys) <= 297.01
    # Y is flipped about the 297mm page, so the art (near the top) lands well
    # past the document's own 75mm height — proof the page frame was applied.
    assert max(ys) > 100


def test_placed_svg_has_placement_but_no_skew(client, placed_job, monkeypatch):
    """The user's rule: SVG/PNG/PDF get optimisation + placement + transform,
    never the skew shear."""
    from app import config
    monkeypatch.setattr(config, "active_machine",
                        lambda: {"skew_deg": 3.0, "skew_true_axis": "x"})
    optimized = client.get(f"/jobs/{placed_job['job_id']}/export",
                           params={"fmt": "svg"}).text
    placed = client.get(f"/jobs/{placed_job['job_id']}/export",
                        params={"fmt": "svg", "placed": "true"}).text
    assert "matrix(" not in placed        # not sheared
    assert placed != optimized            # but placement was applied


def test_placed_gcode_carries_the_skew(client, placed_job, monkeypatch):
    import re
    from app import config

    def bbox(gcode):
        xs = [float(v) for v in re.findall(r"X(-?[\d.]+)", gcode)]
        return max(xs) - min(xs)

    monkeypatch.setattr(config, "active_machine",
                        lambda: {"skew_deg": 0.0, "skew_true_axis": "x"})
    straight = client.get(f"/jobs/{placed_job['job_id']}/export",
                          params={"fmt": "gcode", "placed": "true"}).text
    monkeypatch.setattr(config, "active_machine",
                        lambda: {"skew_deg": 4.0, "skew_true_axis": "x"})
    skewed = client.get(f"/jobs/{placed_job['job_id']}/export",
                        params={"fmt": "gcode", "placed": "true"}).text

    assert skewed != straight
    # shearing a page-tall rectangle about X widens its X footprint
    assert bbox(skewed) > bbox(straight) + 1.0


def test_placed_endpoint_no_selection_is_422(client, job_from_svg):
    job = job_from_svg(FIXTURE, layers=[{**MULTI[0], "selected": False}],
                       optimize_svg=False)
    res = client.get(f"/jobs/{job['job_id']}/export",
                     params={"fmt": "svg", "placed": "true"})
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "select_one_layer"
