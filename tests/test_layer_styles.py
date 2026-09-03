"""Per-layer pen colour / stroke width — the ``layer_styles`` overrides and the
``{svg_id}.styled.svg`` they render into.

``svg_utils.apply_layer_styles`` is the writer; ``_effective_svg_path`` is where
the styled file enters every downstream consumer (preview, plot, export). PATCH
/jobs/{id} carries the overrides and drops them on a ``layer_mode`` switch.
"""
from pathlib import Path

import pytest
from lxml import etree

from app import main, plot_worker, state, svg_utils

TestClient = pytest.importorskip(
    "starlette.testclient", reason="httpx not installed"
).TestClient

# Two top-level layers; layer A carries a conflicting stroke on the group's own
# style, on a child attribute, and in a child's style. viewBox is 2 user units
# per mm so a width conversion is visible.
VIEWBOX_2X = """<svg xmlns="http://www.w3.org/2000/svg"
  xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
  width="100mm" height="50mm" viewBox="0 0 200 100">
  <g inkscape:groupmode="layer" inkscape:label="A" style="stroke:#111111;stroke-width:2">
    <path d="M0,0 L10,10" stroke="#222222" stroke-width="3"/>
    <path d="M0,0 L10,10" style="stroke:#333333;stroke-width:4;fill:none"/>
  </g>
  <g inkscape:groupmode="layer" inkscape:label="B">
    <path d="M0,0 L10,10" stroke="#999999"/>
  </g>
</svg>"""

RAW_PX = """<svg xmlns="http://www.w3.org/2000/svg"
  xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
  width="96px" height="96px">
  <g inkscape:groupmode="layer" inkscape:label="only">
    <path d="M0,0 L10,10" stroke="#000000" stroke-width="1"/>
  </g>
</svg>"""


def _layers(path):
    return svg_utils._top_level_layers(etree.parse(str(path)).getroot())


def _style_props(el):
    out = {}
    for decl in (el.get("style") or "").split(";"):
        name, sep, val = decl.partition(":")
        if sep:
            out[name.strip()] = val.strip()
    return out


# --- apply_layer_styles ----------------------------------------------------

def test_sets_group_stroke_and_clears_descendants(tmp_path):
    src = tmp_path / "in.svg"; src.write_text(VIEWBOX_2X)
    out = tmp_path / "out.svg"
    svg_utils.apply_layer_styles(src, out, [{"index": 0, "stroke": "#ff0000"}])
    gs = _layers(out)
    assert gs[0].get("stroke") == "#ff0000"
    assert "stroke" not in _style_props(gs[0])            # group style entry cleared
    kids = list(gs[0])
    assert kids[0].get("stroke") is None                  # child attribute gone
    assert "stroke" not in _style_props(kids[1])          # child style entry cleared
    assert _style_props(kids[1]).get("fill") == "none"    # sibling prop kept
    assert gs[1].get("stroke") is None                    # other layer untouched
    assert list(gs[1])[0].get("stroke") == "#999999"


def test_width_uses_document_scale(tmp_path):
    src = tmp_path / "in.svg"; src.write_text(VIEWBOX_2X)
    out = tmp_path / "out.svg"
    # viewBox 200 wide over 100mm => 2 user units per mm.
    svg_utils.apply_layer_styles(src, out, [{"index": 0, "stroke_width_mm": 0.5}])
    assert float(_layers(out)[0].get("stroke-width")) == pytest.approx(1.0)


def test_width_falls_back_to_96dpi_without_a_viewbox(tmp_path):
    src = tmp_path / "raw.svg"; src.write_text(RAW_PX)
    out = tmp_path / "raw-out.svg"
    svg_utils.apply_layer_styles(src, out, [{"index": 0, "stroke_width_mm": 1.0}])
    assert float(_layers(out)[0].get("stroke-width")) == pytest.approx(96 / 25.4, rel=1e-3)


def test_combined_entry_sets_both(tmp_path):
    src = tmp_path / "in.svg"; src.write_text(VIEWBOX_2X)
    out = tmp_path / "out.svg"
    svg_utils.apply_layer_styles(
        src, out, [{"index": 1, "stroke": "#0000ff", "stroke_width_mm": 0.25}])
    g = _layers(out)[1]
    assert g.get("stroke") == "#0000ff"
    assert float(g.get("stroke-width")) == pytest.approx(0.5)


def test_out_of_range_index_is_ignored(tmp_path):
    src = tmp_path / "in.svg"; src.write_text(VIEWBOX_2X)
    out = tmp_path / "out.svg"
    svg_utils.apply_layer_styles(src, out, [{"index": 9, "stroke": "#ff0000"},
                                            {"index": 0, "stroke": "#00ff00"}])
    gs = _layers(out)
    assert len(gs) == 2
    assert gs[0].get("stroke") == "#00ff00"


# --- _effective_svg_path -------------------------------------------------

def test_effective_path_styles_only_with_overrides(tmp_path, monkeypatch):
    up = tmp_path / "uploads"; up.mkdir()
    monkeypatch.setattr(plot_worker, "_UPLOAD_DIR_LAZY", up)
    sid = "sid00001"
    (up / f"{sid}.svg").write_text(VIEWBOX_2X)
    base = {"svg_id": sid, "job_id": "j"}
    assert plot_worker._effective_svg_path(base).name == f"{sid}.svg"
    assert plot_worker._effective_svg_path({**base, "layer_styles": []}).name == f"{sid}.svg"

    styled = plot_worker._effective_svg_path(
        {**base, "layer_styles": [{"index": 0, "stroke": "#ff0000"}]})
    assert styled.name == f"{sid}.styled.svg"
    assert _layers(styled)[0].get("stroke") == "#ff0000"


def test_styled_file_is_rewritten_when_the_override_changes_back(tmp_path, monkeypatch):
    up = tmp_path / "uploads"; up.mkdir()
    monkeypatch.setattr(plot_worker, "_UPLOAD_DIR_LAZY", up)
    monkeypatch.setattr(plot_worker, "_styled_built", {})
    sid = "sid00004"
    (up / f"{sid}.svg").write_text(VIEWBOX_2X)

    def styled(hex_):
        return plot_worker._effective_svg_path(
            {"svg_id": sid, "job_id": "j",
             "layer_styles": [{"index": 0, "stroke": hex_}]})

    assert _layers(styled("#ff0000"))[0].get("stroke") == "#ff0000"
    assert _layers(styled("#0000ff"))[0].get("stroke") == "#0000ff"
    # Same path, different content — the single-entry cache must not serve blue.
    assert _layers(styled("#ff0000"))[0].get("stroke") == "#ff0000"


def test_effective_path_styles_on_top_of_the_optimized_file(tmp_path, monkeypatch):
    up = tmp_path / "uploads"; up.mkdir()
    monkeypatch.setattr(plot_worker, "_UPLOAD_DIR_LAZY", up)
    sid = "sid00002"
    (up / f"{sid}.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (up / f"{sid}.opt.svg").write_text(VIEWBOX_2X)   # stand-in for a real optimize
    out = plot_worker._effective_svg_path(
        {"svg_id": sid, "job_id": "j", "optimize_svg": True,
         "layer_styles": [{"index": 1, "stroke": "#123456"}]})
    assert out.name == f"{sid}.styled.svg"
    assert _layers(out)[1].get("stroke") == "#123456"


def test_bad_override_falls_back_to_the_base_file(tmp_path, monkeypatch):
    up = tmp_path / "uploads"; up.mkdir()
    monkeypatch.setattr(plot_worker, "_UPLOAD_DIR_LAZY", up)
    sid = "sid00003"
    (up / f"{sid}.svg").write_text(VIEWBOX_2X)
    monkeypatch.setattr(svg_utils, "apply_layer_styles",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = plot_worker._effective_svg_path(
        {"svg_id": sid, "job_id": "j",
         "layer_styles": [{"index": 0, "stroke": "#ff0000"}]})
    assert out.name == f"{sid}.svg"


# --- PATCH /jobs/{id} --------------------------------------------------

@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def job():
    sid = "_test_layer_styles"
    (main.UPLOAD_DIR / f"{sid}.svg").write_text(VIEWBOX_2X)
    rec = state.add_job({
        "svg_id": sid, "source_svg_id": sid, "filename": "in.svg",
        "layer_selections": [{"index": 0, "label": "A"}, {"index": 1, "label": "B"}],
        "layer_mode": "layer", "optimize_svg": False,
        "paper_width_mm": 210.0, "paper_height_mm": 297.0,
        "margin_top_mm": 0.0, "margin_right_mm": 0.0,
        "margin_bottom_mm": 0.0, "margin_left_mm": 0.0,
        "fit_content": False,
    })
    yield rec
    state.remove_job(rec["job_id"])


def test_patch_round_trips_layer_styles(client, job):
    styles = [{"index": 0, "stroke": "#ff0000", "stroke_width_mm": 0.4}]
    r = client.patch(f"/jobs/{job['job_id']}", json={"layer_styles": styles})
    assert r.status_code == 200
    assert r.json()["layer_styles"] == styles
    assert state.get_job(job["job_id"])["layer_styles"] == styles


def test_layer_mode_switch_clears_layer_styles(client, job):
    client.patch(f"/jobs/{job['job_id']}",
                 json={"layer_styles": [{"index": 0, "stroke": "#ff0000"}]})
    r = client.patch(f"/jobs/{job['job_id']}", json={"layer_mode": "group"})
    assert r.status_code == 200
    assert r.json()["layer_styles"] == []


def test_save_as_svg_bakes_in_the_overrides(client, job):
    client.patch(f"/jobs/{job['job_id']}", json={"layer_styles": [
        {"index": 0, "stroke": "#ff0000", "stroke_width_mm": 0.4},
    ]})
    r = client.get(f"/jobs/{job['job_id']}/export?fmt=svg")
    assert r.status_code == 200
    groups = list(etree.fromstring(r.content).iter(f"{{{svg_utils.SVG_NS}}}g"))
    strokes = [g.get("stroke") for g in groups]
    assert "#ff0000" in strokes
    # viewBox 200 over 100mm => 2 user units per mm, so 0.4mm is written as 0.8.
    widths = [float(g.get("stroke-width")) for g in groups if g.get("stroke-width")]
    assert any(abs(w - 0.8) < 1e-6 for w in widths)
