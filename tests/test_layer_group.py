"""``layer_mode`` re-partitioning: app/layer_group.py.

The default mode uses the SVG's Inkscape layers untouched. The other two write
a standalone layered SVG the rest of the pipeline treats as an ordinary upload,
so what matters here is that the partition is right and that its labels survive
the vpype optimize + reconcile round trip the same way a normal upload's do.
"""
from lxml import etree

import pytest

from app import layer_group, svg_optimize, svg_utils


def _labels(path):
    return [l["label"] for l in svg_utils.parse_layers(path)["layers"]]


def _vpype_ids(path):
    root = etree.parse(str(path)).getroot()
    return [svg_utils._vpype_layer_id(g.get(svg_utils.LABEL_ATTR) or "",
                                      g.get("id") or "", i + 1)
            for i, g in enumerate(svg_utils._top_level_layers(root))]


# --- group ---------------------------------------------------------------

WRAPPED = """<svg xmlns="http://www.w3.org/2000/svg"
  xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
  width="160mm" height="90mm" viewBox="0 0 160 90">
  <g id="wrap" transform="translate(8,4)">
    <g inkscape:label="ridges"><path d="M0,0 L120,0" stroke="#111"/></g>
    <g inkscape:label="valleys"><path d="M0,20 L120,20" stroke="#111"/></g>
    <g inkscape:label="horizon"><path d="M0,40 L120,40" stroke="#111"/></g>
  </g>
</svg>"""

TWO_LAYERS = """<svg xmlns="http://www.w3.org/2000/svg"
  xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
  width="160mm" height="90mm" viewBox="0 0 160 90">
  <g inkscape:groupmode="layer" inkscape:label="under"><path d="M0,0 L120,0" stroke="#111"/></g>
  <g inkscape:groupmode="layer" inkscape:label="over"><path d="M0,20 L120,20" stroke="#111"/></g>
</svg>"""


def test_group_descends_one_level_into_a_lone_wrapper(tmp_path):
    src = tmp_path / "in.svg"
    src.write_text(WRAPPED)
    out = tmp_path / "out.svg"
    layer_group.regroup(src, "group", out)
    assert _labels(out) == ["ridges", "valleys", "horizon"]


def test_group_pushes_the_wrappers_transform_onto_the_children(tmp_path):
    src = tmp_path / "in.svg"
    src.write_text(WRAPPED)
    out = tmp_path / "out.svg"
    layer_group.regroup(src, "group", out)
    tfs = [g.get("transform")
           for g in etree.parse(str(out)).getroot()
           if g.tag == svg_utils.LAYER_TAG]
    assert tfs == ["translate(8,4)"] * 3


def test_group_leaves_an_already_flat_document_alone(tmp_path):
    src = tmp_path / "in.svg"
    src.write_text(TWO_LAYERS)
    out = tmp_path / "out.svg"
    layer_group.regroup(src, "group", out)
    assert _labels(out) == ["under", "over"]


# --- pen -----------------------------------------------------------------

PENS = """<svg xmlns="http://www.w3.org/2000/svg"
  xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
  width="160mm" height="90mm" viewBox="0 0 160 90">
  <g inkscape:groupmode="layer" inkscape:label="everything">
    <path d="M10,10 L150,10" stroke="#1a1815" stroke-width="0.3" fill="none"/>
    <path d="M10,20 L150,20" stroke="#1a1815" stroke-width="0.3" fill="none"/>
    <path d="M10,30 L150,30" stroke="#c0392b" stroke-width="0.3" fill="none"/>
    <path d="M10,40 L150,40" stroke="#c0392b" stroke-width="0.8" fill="none"/>
    <rect x="10" y="55" width="140" height="25" stroke="#2e86c1" stroke-width="1.2" fill="none"/>
  </g>
</svg>"""


def test_pen_makes_one_layer_per_stroke_width_and_colour(tmp_path):
    src = tmp_path / "in.svg"
    src.write_text(PENS)
    out = tmp_path / "out.svg"
    layer_group.regroup(src, "pen", out)
    labels = _labels(out)
    assert len(labels) == 4                       # 3 colours, one split by width
    assert all(lbl.startswith("Pen ") for lbl in labels)
    assert any("#1a1815" in lbl and "0.3 mm" in lbl for lbl in labels)
    assert any("#c0392b" in lbl and "0.8 mm" in lbl for lbl in labels)


def test_pen_labels_keep_distinct_vpype_ids(tmp_path):
    src = tmp_path / "in.svg"
    src.write_text(PENS)
    out = tmp_path / "out.svg"
    layer_group.regroup(src, "pen", out)
    ids = _vpype_ids(out)
    assert len(set(ids)) == len(ids), ids


def test_pen_buckets_survive_optimize_and_reconcile(tmp_path):
    src = tmp_path / "in.svg"
    src.write_text(PENS)
    grouped = tmp_path / "grouped.svg"
    layer_group.regroup(src, "pen", grouped)
    before = _labels(grouped)

    opt = tmp_path / "grouped.opt.svg"
    svg_optimize.optimize_svg(grouped, opt, tolerance_mm=0.1, linemerge=True,
                              linesimplify=True, linesort=True, reloop=True)
    svg_utils.reconcile_layers(grouped, opt)
    assert _labels(opt) == before          # vpype did not merge two pens into one


def test_unknown_mode_is_rejected(tmp_path):
    src = tmp_path / "in.svg"
    src.write_text(TWO_LAYERS)
    with pytest.raises(layer_group.RegroupError):
        layer_group.regroup(src, "sideways", tmp_path / "out.svg")
