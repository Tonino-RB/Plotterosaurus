"""app.axis_skew: the hardware axis-skew correction applied to a stage's
already-placed SVG, right before it's handed to the plotter driver.

The correction maps a physically-correct design point to the motor-space
command that lands the pen there once the machine's own axis defect is
accounted for. Rather than trusting the algebra, _physical_forward below is
an independently-written model of what an *uncorrected* skewed machine
actually draws — the correction's job is to exactly cancel it.
"""
import math
import shutil
from pathlib import Path

import pytest
from lxml import etree

from app import axis_skew, svg_utils

FIXTURES = Path(__file__).parent / "fixtures"


def _apply_matrix(matrix_str: str, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = (float(n) for n in matrix_str[len("matrix("):-1].split(","))
    return a * x + c * y + e, b * x + d * y + f


def _physical_forward(mx, my, skew_deg, true_axis, cx, cy):
    """What a machine with this axis defect actually draws in physical
    space when commanded to (mx, my) with no correction applied — the
    ground truth axis_skew.skew_matrix's correction is designed to cancel.
    Derived independently from the correction formulas themselves: a pure
    shear, matching the same model app.js's skewAngleDeg calculator assumes
    (see tests/test_static_js.py's _diagonals_for_skew)."""
    tan_t = math.tan(math.radians(skew_deg))
    x, y = mx - cx, my - cy
    if true_axis == "y":
        phys_x, phys_y = x, y + x * tan_t
    else:
        phys_x, phys_y = x + y * tan_t, y
    return phys_x + cx, phys_y + cy


POINTS = [(0.0, 0.0), (210.0, 0.0), (0.0, 297.0), (210.0, 297.0), (57.0, 133.0)]


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [0.1, 1.5, -2.3, 5.0, -5.0])
def test_correction_round_trips_through_the_physical_model(true_axis, skew_deg):
    cx, cy = 105.0, 148.5
    matrix = axis_skew.skew_matrix(skew_deg, true_axis, cx, cy)
    for px, py in POINTS:
        mx, my = _apply_matrix(matrix, px, py)
        phys_x, phys_y = _physical_forward(mx, my, skew_deg, true_axis, cx, cy)
        assert phys_x == pytest.approx(px, abs=1e-9)
        assert phys_y == pytest.approx(py, abs=1e-9)


@pytest.mark.parametrize("skew_deg", [0.5, 3.0, -0.5, -3.0])
def test_sign_matches_the_existing_calculators_convention(skew_deg):
    """app.js's skewAngleDeg defines positive skew as: the top-left/
    bottom-right diagonal (d1) is the longer one, because the machine
    drifts +x as it travels down the page. An uncorrected square drawn by
    a machine with true_axis="x" and this skew_deg must reproduce that same
    d1 > d2 relationship for positive skew_deg (and the reverse for
    negative), tying axis_skew's sign convention to the calculator's."""
    L = 100.0
    corners = {"tl": (0.0, 0.0), "tr": (L, 0.0), "bl": (0.0, L), "br": (L, L)}
    physical = {
        name: _physical_forward(x, y, skew_deg, "x", 0.0, 0.0)
        for name, (x, y) in corners.items()
    }
    def dist(a, b):
        return math.hypot(physical[a][0] - physical[b][0], physical[a][1] - physical[b][1])
    d1 = dist("tl", "br")
    d2 = dist("tr", "bl")
    assert (d1 > d2) == (skew_deg > 0)


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [0.1, 2.7, -4.9])
def test_inverse_skew_point_is_the_exact_algebraic_inverse(true_axis, skew_deg):
    cx, cy = 105.0, 148.5
    matrix = axis_skew.skew_matrix(skew_deg, true_axis, cx, cy)
    for px, py in POINTS:
        mx, my = _apply_matrix(matrix, px, py)
        back_x, back_y = axis_skew.inverse_skew_point(mx, my, skew_deg, true_axis, cx, cy)
        assert back_x == pytest.approx(px, abs=1e-9)
        assert back_y == pytest.approx(py, abs=1e-9)


@pytest.mark.parametrize("true_axis", ["x", "y"])
def test_pivot_is_the_transforms_fixed_point(true_axis):
    cx, cy = 63.0, 21.5
    matrix = axis_skew.skew_matrix(3.2, true_axis, cx, cy)
    x, y = _apply_matrix(matrix, cx, cy)
    assert x == pytest.approx(cx, abs=1e-9)
    assert y == pytest.approx(cy, abs=1e-9)


def _rendered_stage_svg(tmp_path):
    """A paper-mm SVG the way plot_worker builds one for a real stage: the
    fixture filtered and run through transform_to_paper, exactly as
    _run_staged_loop_impl does before calling axis_skew.apply_axis_skew."""
    src = tmp_path / "square.svg"
    shutil.copy(FIXTURES / "square.svg", src)
    filtered = tmp_path / "filtered.svg"
    info = svg_utils.parse_layers(src)
    indices = [layer["index"] for layer in info["layers"]]
    svg_utils.filter_to_layers(src, indices, filtered)
    rendered = tmp_path / "rendered.svg"
    svg_utils.transform_to_paper(
        filtered, rendered,
        paper_width_mm=210.0, paper_height_mm=297.0,
        margin_top_mm=10.0, margin_right_mm=10.0,
        margin_bottom_mm=10.0, margin_left_mm=10.0,
        fit_content=True,
    )
    return rendered


def test_apply_axis_skew_is_a_true_no_op_at_zero(tmp_path):
    rendered = _rendered_stage_svg(tmp_path)
    before = rendered.read_bytes()
    axis_skew.apply_axis_skew(rendered, 0.0, "x", 210.0, 297.0)
    assert rendered.read_bytes() == before


def test_apply_axis_skew_wraps_content_without_touching_page_size(tmp_path):
    rendered = _rendered_stage_svg(tmp_path)
    root_before = etree.parse(str(rendered)).getroot()
    width, height, viewbox = root_before.get("width"), root_before.get("height"), root_before.get("viewBox")
    placement_transform = root_before.find(f"{{{svg_utils.SVG_NS}}}g").get("transform")

    axis_skew.apply_axis_skew(rendered, 2.5, "x", 210.0, 297.0)

    root_after = etree.parse(str(rendered)).getroot()
    assert root_after.get("width") == width
    assert root_after.get("height") == height
    assert root_after.get("viewBox") == viewbox

    outer = root_after.find(f"{{{svg_utils.SVG_NS}}}g")
    assert outer.get("transform") == axis_skew.skew_matrix(2.5, "x", 105.0, 148.5)
    inner = outer.find(f"{{{svg_utils.SVG_NS}}}g")
    assert inner.get("transform") == placement_transform


def test_transform_to_paper_never_applies_skew_itself(tmp_path):
    """The preview cache and the browser placement preview both render
    through svg_utils.transform_to_paper directly, never through
    axis_skew. Its output must be identical regardless of the machine's
    configured skew — proving those preview paths stay untouched by
    construction, since transform_to_paper has no skew parameter at all
    and never consults config."""
    src = tmp_path / "square.svg"
    shutil.copy(FIXTURES / "square.svg", src)
    filtered = tmp_path / "filtered.svg"
    info = svg_utils.parse_layers(src)
    indices = [layer["index"] for layer in info["layers"]]
    svg_utils.filter_to_layers(src, indices, filtered)

    out = tmp_path / "out.svg"
    svg_utils.transform_to_paper(
        filtered, out,
        paper_width_mm=210.0, paper_height_mm=297.0,
        margin_top_mm=10.0, margin_right_mm=10.0,
        margin_bottom_mm=10.0, margin_left_mm=10.0,
        fit_content=True,
    )
    root = etree.parse(str(out)).getroot()
    # Exactly the one placement <g> — no second, skew-wrapping <g>.
    assert len(root.findall(f"{{{svg_utils.SVG_NS}}}g")) == 1


# ── Full-bleed (zero-margin) designs ────────────────────────────────────
#
# The plan flagged a real residual risk: a corner-pivoted correction can
# push commanded coordinates below the driver's travel-bounds minimum of 0
# for content that runs edge-to-edge with no margin — and a center pivot
# only halves that, it doesn't eliminate it. These tests quantify exactly
# how large that excursion is, rather than leaving it as an unverified claim.

def _full_bleed_svg(tmp_path: Path, width_mm: float, height_mm: float) -> Path:
    """A design with ink touching all four edges of its own canvas — no
    bleed/margin between the artwork and the page at all."""
    svg_path = tmp_path / "full_bleed.svg"
    svg_path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
        f'width="{width_mm}mm" height="{height_mm}mm" '
        f'viewBox="0 0 {width_mm} {height_mm}">'
        f'<g inkscape:groupmode="layer" inkscape:label="art">'
        f'<rect x="0" y="0" width="{width_mm}" height="{height_mm}" '
        f'fill="none" stroke="#000"/>'
        f'</g></svg>'
    )
    return svg_path


def test_full_bleed_fixture_really_has_zero_margin(tmp_path):
    """Sanity check on the fixture itself, before trusting any test built
    on top of it: with zero job margins, the ink fills the whole page."""
    src = _full_bleed_svg(tmp_path, 100.0, 150.0)
    filtered = tmp_path / "filtered.svg"
    svg_utils.filter_to_layers(src, [0], filtered)
    left, top, right, bottom = svg_utils.ink_bounds_mm(
        filtered, [0], paper_width_mm=100.0, paper_height_mm=150.0,
        margin_top_mm=0.0, margin_right_mm=0.0, margin_bottom_mm=0.0, margin_left_mm=0.0,
        fit_content=True,
    )
    # vpype's own px<->mm conversion introduces float noise around 1e-4mm —
    # utterly immaterial to a pen plotter, but too coarse for a 1e-6 bound.
    assert (left, top, right, bottom) == pytest.approx((0.0, 0.0, 100.0, 150.0), abs=1e-3)


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [0.1, 0.3, 1.0, 5.0, -5.0])
def test_full_bleed_excursion_beyond_the_page_matches_closed_form(tmp_path, true_axis, skew_deg):
    """For ink that already touches all four edges, correction pivoted at
    the page's center pushes the corrected geometry beyond [0, paper] by
    exactly (perpendicular-dimension / 2) * tan(skew) on the corrected
    axis, and not at all on the true axis — the shear model leaves it
    untouched by construction. This is the exact, closed-form size of the
    residual clipping risk flagged in the plan — not just a claim."""
    paper_w, paper_h = 100.0, 150.0
    src = _full_bleed_svg(tmp_path, paper_w, paper_h)
    filtered = tmp_path / "filtered.svg"
    svg_utils.filter_to_layers(src, [0], filtered)
    left, top, right, bottom = svg_utils.ink_bounds_mm(
        filtered, [0], paper_width_mm=paper_w, paper_height_mm=paper_h,
        margin_top_mm=0.0, margin_right_mm=0.0, margin_bottom_mm=0.0, margin_left_mm=0.0,
        fit_content=True,
    )
    corners = [(left, top), (right, top), (left, bottom), (right, bottom)]

    cx, cy = paper_w / 2, paper_h / 2
    matrix = axis_skew.skew_matrix(skew_deg, true_axis, cx, cy)
    corrected = [_apply_matrix(matrix, x, y) for x, y in corners]

    theta = math.radians(skew_deg)
    if true_axis == "x":
        corrected_axis_vals = [x for x, _ in corrected]
        corrected_lo, corrected_hi = 0.0, paper_w
        true_axis_vals = [y for _, y in corrected]
        true_lo, true_hi = 0.0, paper_h
        expected_corrected_excursion = (paper_h / 2) * abs(math.tan(theta))
        expected_true_excursion = 0.0
    else:
        corrected_axis_vals = [y for _, y in corrected]
        corrected_lo, corrected_hi = 0.0, paper_h
        true_axis_vals = [x for x, _ in corrected]
        true_lo, true_hi = 0.0, paper_w
        expected_corrected_excursion = (paper_w / 2) * abs(math.tan(theta))
        expected_true_excursion = 0.0

    # abs=1e-3: absorbs the same ~1e-4mm vpype measurement noise as above,
    # still three orders of magnitude tighter than anything a pen can draw.
    below = corrected_lo - min(corrected_axis_vals)
    above = max(corrected_axis_vals) - corrected_hi
    assert below == pytest.approx(expected_corrected_excursion, abs=1e-3)
    assert above == pytest.approx(expected_corrected_excursion, abs=1e-3)

    true_below = true_lo - min(true_axis_vals)
    true_above = max(true_axis_vals) - true_hi
    assert true_below == pytest.approx(expected_true_excursion, abs=1e-3)
    assert true_above == pytest.approx(expected_true_excursion, abs=1e-3)


def test_center_pivot_halves_the_worst_case_excursion_vs_a_corner_pivot():
    """Direct proof of the plan's "center-pivot halves the worst case"
    claim: a corner pivot at (0,0) concentrates the same total shear
    entirely on one edge, while the center pivot splits it evenly across
    both — so the single worst excursion is exactly half."""
    paper_w, paper_h = 100.0, 150.0
    skew_deg = 5.0
    corners = [(0.0, 0.0), (paper_w, 0.0), (0.0, paper_h), (paper_w, paper_h)]

    center_matrix = axis_skew.skew_matrix(skew_deg, "x", paper_w / 2, paper_h / 2)
    corner_matrix = axis_skew.skew_matrix(skew_deg, "x", 0.0, 0.0)

    center_xs = [_apply_matrix(center_matrix, x, y)[0] for x, y in corners]
    corner_xs = [_apply_matrix(corner_matrix, x, y)[0] for x, y in corners]

    center_worst = max(0.0 - min(center_xs), max(center_xs) - paper_w)
    corner_worst = max(0.0 - min(corner_xs), max(corner_xs) - paper_w)

    assert center_worst == pytest.approx(corner_worst / 2, abs=1e-9)


def test_full_bleed_at_realistic_skew_stays_within_a_fraction_of_a_millimetre(tmp_path):
    """The ±5° cap is a sanity bound, not a typical measurement — real
    hardware skew on a reasonably assembled machine is usually well under
    1°. At a realistic 0.3° on an A4-sized page, full-bleed content should
    only be pushed a hair past the page edge."""
    paper_w, paper_h = 210.0, 297.0
    src = _full_bleed_svg(tmp_path, paper_w, paper_h)
    filtered = tmp_path / "filtered.svg"
    svg_utils.filter_to_layers(src, [0], filtered)
    left, top, right, bottom = svg_utils.ink_bounds_mm(
        filtered, [0], paper_width_mm=paper_w, paper_height_mm=paper_h,
        margin_top_mm=0.0, margin_right_mm=0.0, margin_bottom_mm=0.0, margin_left_mm=0.0,
        fit_content=True,
    )
    corners = [(left, top), (right, top), (left, bottom), (right, bottom)]
    matrix = axis_skew.skew_matrix(0.3, "x", paper_w / 2, paper_h / 2)
    xs = [_apply_matrix(matrix, x, y)[0] for x, y in corners]
    worst = max(0.0 - min(xs), max(xs) - paper_w)
    assert worst < 0.9


def test_apply_axis_skew_on_a_full_bleed_stage_svg_does_not_error(tmp_path):
    """End-to-end smoke test through the real pipeline, at zero margin: no
    exception, page size untouched, and the rect's own raw coordinates are
    unchanged (only the wrapping transform carries the correction) — the
    same "no new error conditions" guarantee (C10) proven generically
    elsewhere, specifically for the edge-to-edge case this section is about."""
    paper_w, paper_h = 100.0, 150.0
    src = _full_bleed_svg(tmp_path, paper_w, paper_h)
    filtered = tmp_path / "filtered.svg"
    svg_utils.filter_to_layers(src, [0], filtered)
    rendered = tmp_path / "rendered.svg"
    svg_utils.transform_to_paper(
        filtered, rendered,
        paper_width_mm=paper_w, paper_height_mm=paper_h,
        margin_top_mm=0.0, margin_right_mm=0.0, margin_bottom_mm=0.0, margin_left_mm=0.0,
        fit_content=True,
    )
    axis_skew.apply_axis_skew(rendered, 5.0, "x", paper_w, paper_h)

    root = etree.parse(str(rendered)).getroot()
    assert root.get("width") == f"{paper_w}mm"
    assert root.get("height") == f"{paper_h}mm"
    rect = root.find(f".//{{{svg_utils.SVG_NS}}}rect")
    assert (rect.get("x"), rect.get("y"), rect.get("width"), rect.get("height")) == (
        "0", "0", str(paper_w), str(paper_h))
