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


# skew_delta ------------------------------------------------------------------
# The same correction as skew_matrix, but for a relative move (plot_worker's
# manual jog / origin nudge / Return to Origin) rather than an absolute
# point — so there's no pivot argument at all.

DELTAS = [(0.0, 0.0), (10.0, 0.0), (0.0, 10.0), (7.0, -3.0), (-25.0, 40.0)]


@pytest.mark.parametrize("skew_deg", [0.0, -0.0])
def test_skew_delta_is_a_true_no_op_at_zero(skew_deg):
    for dx, dy in DELTAS:
        assert axis_skew.skew_delta(dx, dy, skew_deg, "x") == (dx, dy)
        assert axis_skew.skew_delta(dx, dy, skew_deg, "y") == (dx, dy)


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [0.1, 1.5, -2.3, 5.0, -5.0])
def test_skew_delta_round_trips_through_the_physical_model(true_axis, skew_deg):
    """The same ground-truth model test_correction_round_trips_through_the_
    physical_model uses for skew_matrix, but with the pivot at the origin —
    valid because a relative move's correction never depends on the pivot."""
    for dx, dy in DELTAS:
        mdx, mdy = axis_skew.skew_delta(dx, dy, skew_deg, true_axis)
        phys_x, phys_y = _physical_forward(mdx, mdy, skew_deg, true_axis, 0.0, 0.0)
        assert phys_x == pytest.approx(dx, abs=1e-9)
        assert phys_y == pytest.approx(dy, abs=1e-9)


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [0.1, 2.7, -4.9])
def test_skew_delta_matches_skew_matrixs_linear_part(true_axis, skew_deg):
    """Applying skew_matrix (pivoted anywhere) to two points and taking the
    difference of the results must equal skew_delta applied to the
    difference of the points — confirms skew_delta really is just
    skew_matrix's linear part, pivot dropped."""
    cx, cy = 40.0, -12.0
    matrix = axis_skew.skew_matrix(skew_deg, true_axis, cx, cy)
    p1, p2 = (12.0, 34.0), (-8.0, 61.0)
    m1 = _apply_matrix(matrix, *p1)
    m2 = _apply_matrix(matrix, *p2)
    expected = (m2[0] - m1[0], m2[1] - m1[1])
    actual = axis_skew.skew_delta(p2[0] - p1[0], p2[1] - p1[1], skew_deg, true_axis)
    assert actual[0] == pytest.approx(expected[0], abs=1e-9)
    assert actual[1] == pytest.approx(expected[1], abs=1e-9)


@pytest.mark.parametrize("true_axis", ["x", "y"])
def test_skew_delta_is_linear(true_axis):
    """The cumulative bed-edge guards in plot_worker (nudge_origin,
    manual_jog) depend on this exactly: the sum of corrected increments must
    equal the correction of the summed increment."""
    skew_deg = 3.7
    a, b = (11.0, -6.0), (4.5, 19.0)
    sum_then_correct = axis_skew.skew_delta(a[0] + b[0], a[1] + b[1], skew_deg, true_axis)
    corrected_a = axis_skew.skew_delta(*a, skew_deg, true_axis)
    corrected_b = axis_skew.skew_delta(*b, skew_deg, true_axis)
    correct_then_sum = (corrected_a[0] + corrected_b[0], corrected_a[1] + corrected_b[1])
    assert sum_then_correct[0] == pytest.approx(correct_then_sum[0], abs=1e-9)
    assert sum_then_correct[1] == pytest.approx(correct_then_sum[1], abs=1e-9)


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
    axis_skew.apply_axis_skew(rendered, 0.0, "x")
    assert rendered.read_bytes() == before


def test_apply_axis_skew_wraps_content_without_touching_page_size(tmp_path):
    rendered = _rendered_stage_svg(tmp_path)
    root_before = etree.parse(str(rendered)).getroot()
    width, height, viewbox = root_before.get("width"), root_before.get("height"), root_before.get("viewBox")
    placement_transform = root_before.find(f"{{{svg_utils.SVG_NS}}}g").get("transform")

    axis_skew.apply_axis_skew(rendered, 2.5, "x")

    root_after = etree.parse(str(rendered)).getroot()
    assert root_after.get("width") == width
    assert root_after.get("height") == height
    assert root_after.get("viewBox") == viewbox

    outer = root_after.find(f"{{{svg_utils.SVG_NS}}}g")
    assert outer.get("transform") == axis_skew.skew_matrix(2.5, "x", 0.0, 0.0)
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
# apply_axis_skew pivots at the page's own origin (0, 0) — the plotter's
# declared physical home — rather than the page center, specifically so
# that corner never moves. A shear still grows the bounding box on the far
# side, though: content that runs edge-to-edge with no margin can still be
# pushed past the *opposite* edge. These tests quantify exactly how large
# that excursion is, rather than leaving it as an unverified claim.

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
    """For ink that already touches all four edges, the (0,0)-pivoted
    correction leaves the near edge (the one running through the origin)
    exactly touching the page boundary — zero excursion — while the far
    edge shifts uniformly by exactly perpendicular-dimension * tan(skew) on
    the corrected axis, entirely to one side (which side depends on the
    sign of skew_deg), and not at all on the true axis. This is the exact,
    closed-form size of the room 'absorb' mode has to reserve."""
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

    matrix = axis_skew.skew_matrix(skew_deg, true_axis, 0.0, 0.0)
    corrected = [_apply_matrix(matrix, x, y) for x, y in corners]

    theta = math.radians(skew_deg)
    if true_axis == "x":
        corrected_axis_vals = [x for x, _ in corrected]
        corrected_lo, corrected_hi = 0.0, paper_w
        true_axis_vals = [y for _, y in corrected]
        true_lo, true_hi = 0.0, paper_h
        expected_excursion = paper_h * abs(math.tan(theta))
    else:
        corrected_axis_vals = [y for _, y in corrected]
        corrected_lo, corrected_hi = 0.0, paper_h
        true_axis_vals = [x for x, _ in corrected]
        true_lo, true_hi = 0.0, paper_w
        expected_excursion = paper_w * abs(math.tan(theta))

    # abs=1e-3: absorbs the same ~1e-4mm vpype measurement noise as above,
    # still three orders of magnitude tighter than anything a pen can draw.
    below = corrected_lo - min(corrected_axis_vals)
    above = max(corrected_axis_vals) - corrected_hi
    if skew_deg > 0:
        assert below == pytest.approx(expected_excursion, abs=1e-3)
        assert above == pytest.approx(0.0, abs=1e-3)
    else:
        assert below == pytest.approx(0.0, abs=1e-3)
        assert above == pytest.approx(expected_excursion, abs=1e-3)

    true_below = true_lo - min(true_axis_vals)
    true_above = max(true_axis_vals) - true_hi
    assert true_below == pytest.approx(0.0, abs=1e-3)
    assert true_above == pytest.approx(0.0, abs=1e-3)


def test_full_bleed_at_realistic_skew_stays_within_a_couple_millimetres(tmp_path):
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
    matrix = axis_skew.skew_matrix(0.3, "x", 0.0, 0.0)
    xs = [_apply_matrix(matrix, x, y)[0] for x, y in corners]
    worst = max(0.0 - min(xs), max(xs) - paper_w)
    assert worst < 1.8


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
    axis_skew.apply_axis_skew(rendered, 5.0, "x")

    root = etree.parse(str(rendered)).getroot()
    assert root.get("width") == f"{paper_w}mm"
    assert root.get("height") == f"{paper_h}mm"
    rect = root.find(f".//{{{svg_utils.SVG_NS}}}rect")
    assert (rect.get("x"), rect.get("y"), rect.get("width"), rect.get("height")) == (
        "0", "0", str(paper_w), str(paper_h))


# ── "absorb" mode: skew_absorb_matrix / apply_skew_absorb ──────────────────


def test_skew_absorb_matrix_is_none_at_zero_skew():
    assert axis_skew.skew_absorb_matrix(0.0, "x", 210.0, 297.0) is None


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [0.1, 2.7, 5.0, -0.1, -2.7, -5.0])
def test_skew_absorb_matrix_reserves_the_closed_form_room_on_one_edge_only(true_axis, skew_deg):
    """skew_absorb_matrix must reserve exactly the excursion
    test_full_bleed_excursion_beyond_the_page_matches_closed_form measures
    apply_axis_skew actually needing — on the same single edge, and leave
    the true (unaffected) axis and the near edge alone."""
    paper_w, paper_h = 100.0, 150.0
    matrix = axis_skew.skew_absorb_matrix(skew_deg, true_axis, paper_w, paper_h)
    assert matrix is not None

    theta = math.radians(skew_deg)
    if true_axis == "x":
        dim, span = paper_h, paper_w
    else:
        dim, span = paper_w, paper_h
    expected_reserve = abs(math.tan(theta)) * dim
    expected_scale = (span - expected_reserve) / span

    corners = [(0.0, 0.0), (paper_w, 0.0), (0.0, paper_h), (paper_w, paper_h)]
    squeezed = [_apply_matrix(matrix, x, y) for x, y in corners]

    if true_axis == "x":
        squeezed_axis_vals = [x for x, _ in squeezed]
        true_axis_vals = [y for _, y in squeezed]
        true_axis_before = [y for _, y in corners]
    else:
        squeezed_axis_vals = [y for _, y in squeezed]
        true_axis_vals = [x for x, _ in squeezed]
        true_axis_before = [x for x, _ in corners]

    # The true axis is completely untouched.
    assert true_axis_vals == pytest.approx(true_axis_before, abs=1e-9)
    # The squeezed axis spans exactly [0, span - reserve] or
    # [reserve, span], never touching the edge the shear will push toward.
    span_seen = max(squeezed_axis_vals) - min(squeezed_axis_vals)
    assert span_seen == pytest.approx(span * expected_scale, abs=1e-6)
    if skew_deg > 0:
        assert min(squeezed_axis_vals) == pytest.approx(expected_reserve, abs=1e-6)
        assert max(squeezed_axis_vals) == pytest.approx(span, abs=1e-6)
    else:
        assert min(squeezed_axis_vals) == pytest.approx(0.0, abs=1e-6)
        assert max(squeezed_axis_vals) == pytest.approx(span - expected_reserve, abs=1e-6)


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [0.1, 2.7, 5.0, -0.1, -2.7, -5.0])
def test_absorb_then_shear_keeps_full_bleed_corners_on_the_page(true_axis, skew_deg):
    """The end-to-end claim 'absorb' mode exists for: composing
    skew_absorb_matrix (applied first, inner) with skew_matrix's (0,0)-
    pivoted shear (applied second, outer — matching apply_skew_absorb then
    apply_axis_skew's wrap order) keeps every corner of even full-bleed
    content inside [0, paper_w] x [0, paper_h]."""
    paper_w, paper_h = 100.0, 150.0
    absorb = axis_skew.skew_absorb_matrix(skew_deg, true_axis, paper_w, paper_h)
    shear = axis_skew.skew_matrix(skew_deg, true_axis, 0.0, 0.0)

    corners = [(0.0, 0.0), (paper_w, 0.0), (0.0, paper_h), (paper_w, paper_h)]
    for x, y in corners:
        sx, sy = _apply_matrix(absorb, x, y)
        fx, fy = _apply_matrix(shear, sx, sy)
        assert -1e-6 <= fx <= paper_w + 1e-6
        assert -1e-6 <= fy <= paper_h + 1e-6


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [0.1, 2.7, -4.9])
def test_inverse_absorb_point_is_the_exact_algebraic_inverse(true_axis, skew_deg):
    paper_w, paper_h = 100.0, 150.0
    matrix = axis_skew.skew_absorb_matrix(skew_deg, true_axis, paper_w, paper_h)
    for px, py in POINTS:
        sx, sy = _apply_matrix(matrix, px, py)
        back_x, back_y = axis_skew.inverse_absorb_point(sx, sy, skew_deg, true_axis, paper_w, paper_h)
        assert back_x == pytest.approx(px, abs=1e-9)
        assert back_y == pytest.approx(py, abs=1e-9)


def test_inverse_absorb_point_is_a_no_op_at_zero_skew():
    assert axis_skew.inverse_absorb_point(12.0, 34.0, 0.0, "x", 210.0, 297.0) == (12.0, 34.0)


def test_apply_skew_absorb_is_a_true_no_op_at_zero(tmp_path):
    rendered = _rendered_stage_svg(tmp_path)
    before = rendered.read_bytes()
    axis_skew.apply_skew_absorb(rendered, 0.0, "x", 210.0, 297.0)
    assert rendered.read_bytes() == before


def test_apply_skew_absorb_wraps_inner_to_apply_axis_skew(tmp_path):
    """apply_skew_absorb must run first (its wrap ends up closest to the
    content) so the composed transform is shear(absorb(content)) — the
    order the excursion math in skew_absorb_matrix assumes."""
    rendered = _rendered_stage_svg(tmp_path)
    placement_transform = etree.parse(str(rendered)).getroot().find(
        f"{{{svg_utils.SVG_NS}}}g").get("transform")

    axis_skew.apply_skew_absorb(rendered, 2.5, "x", 210.0, 297.0)
    axis_skew.apply_axis_skew(rendered, 2.5, "x")

    root = etree.parse(str(rendered)).getroot()
    outer = root.find(f"{{{svg_utils.SVG_NS}}}g")
    assert outer.get("transform") == axis_skew.skew_matrix(2.5, "x", 0.0, 0.0)
    middle = outer.find(f"{{{svg_utils.SVG_NS}}}g")
    assert middle.get("transform") == axis_skew.skew_absorb_matrix(2.5, "x", 210.0, 297.0)
    inner = middle.find(f"{{{svg_utils.SVG_NS}}}g")
    assert inner.get("transform") == placement_transform
