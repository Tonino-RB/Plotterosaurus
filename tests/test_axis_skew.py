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


def _physical_forward(mx, my, skew_deg, true_axis):
    """What a machine with this axis defect actually draws in physical space
    when commanded to (mx, my) with no correction applied — the ground truth
    axis_skew.skew_matrix's correction is designed to cancel. Derived
    independently from the correction formulas themselves: a pure shear,
    matching the same model app.js's skewAngleDeg calculator assumes (see
    tests/test_static_js.py's _diagonals_for_skew).

    Pivoted at motor (0, 0) and nowhere else, deliberately. That is a fact
    about the machine — an AxiDraw has no home switches, so its zero is
    wherever the carriage stood when the plot connected — not a parameter.
    An earlier version of this helper took the correction's own pivot and
    used it here too, which quietly made every pivot look correct: the same
    number cancelled on both sides of the comparison. Fixing the model to
    the origin is what lets test_a_pivot_only_translates_the_result below
    say something real about the pivot.
    """
    tan_t = math.tan(math.radians(skew_deg))
    if true_axis == "y":
        return mx, my + mx * tan_t
    return mx + my * tan_t, my


POINTS = [(0.0, 0.0), (210.0, 0.0), (0.0, 297.0), (210.0, 297.0), (57.0, 133.0)]


PAGE = (210.0, 297.0)


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [0.1, 1.5, -2.3, 5.0, -5.0])
def test_the_correction_lands_artwork_exactly_where_it_was_placed(true_axis, skew_deg):
    """The correction is exact — not "exact up to a shift". Every commanded
    point is physically drawn at the point it was asked for, to the last
    float, so a design centred on the page is drawn centred on the page.

    That is a consequence of anchoring the shear at motor (0, 0), and the
    reason there is no pivot argument to pass: the machine's own shear
    pivots there and cannot be moved, so any other anchor would draw the
    right shape in the wrong place (see
    test_moving_the_anchor_would_slide_the_whole_drawing).
    """
    matrix = axis_skew.skew_matrix(skew_deg, true_axis, *PAGE)
    for px, py in POINTS:
        mx, my = _apply_matrix(matrix, px, py)
        phys = _physical_forward(mx, my, skew_deg, true_axis)
        assert phys == pytest.approx((px, py), abs=1e-9)


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [0.5, -2.3])
def test_moving_the_anchor_would_slide_the_whole_drawing(true_axis, skew_deg):
    """Why the anchor is not a knob, kept as an executable note rather than a
    claim in a comment.

    Correcting about any point other than motor (0, 0) still draws the right
    *shape* — the offset below is one and the same for every point — but the
    whole drawing lands translated by anchor * tan(skew_deg). At 1 degree on
    A3 that is 2.6 mm across the paper, which would take a design centred on
    the page and draw it off centre. The page centre is an appealing anchor
    for splitting the correction's cost evenly between two edges; this is the
    reason it is absorb_scale's job instead, where it costs no displacement.
    """
    tan_t = math.tan(math.radians(skew_deg))
    cx, cy = PAGE[0] / 2, PAGE[1] / 2
    offsets = []
    for px, py in POINTS:
        # The same shear, anchored at the page centre instead of the origin.
        if true_axis == "y":
            mx, my = px, py - (px - cx) * tan_t
        else:
            mx, my = px - (py - cy) * tan_t, py
        phys = _physical_forward(mx, my, skew_deg, true_axis)
        offsets.append((phys[0] - px, phys[1] - py))

    for off in offsets[1:]:
        assert off == pytest.approx(offsets[0], abs=1e-9)
    expected = (cy * tan_t, 0.0) if true_axis == "x" else (0.0, cx * tan_t)
    assert offsets[0] == pytest.approx(expected, abs=1e-9)
    assert offsets[0] != pytest.approx((0.0, 0.0), abs=1e-6)


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [0.4, 2.0, -3.1])
@pytest.mark.parametrize("scale", [1.0, 0.97])
def test_the_correction_never_deforms_what_it_draws(true_axis, skew_deg, scale):
    """The property the whole module exists to provide: whatever the machine
    physically draws from corrected commands is *similar* to what was asked
    for — every distance in the same proportion, so nothing is stretched,
    squashed or sheared. At scale 1.0 that proportion is exactly 1 and the
    drawing is congruent; under absorb's shrink it is `scale`, uniformly,
    on both axes at once.
    """
    matrix = axis_skew.skew_matrix(skew_deg, true_axis, *PAGE, scale)
    drawn = [_physical_forward(*_apply_matrix(matrix, px, py), skew_deg, true_axis)
             for px, py in POINTS]
    for i in range(len(POINTS)):
        for j in range(i + 1, len(POINTS)):
            want = math.dist(POINTS[i], POINTS[j]) * scale
            assert math.dist(drawn[i], drawn[j]) == pytest.approx(want, abs=1e-9)


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
        name: _physical_forward(x, y, skew_deg, "x")
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
def test_the_origin_is_the_transforms_fixed_point(true_axis):
    """The page's own origin corner — where the carriage stands when the plot
    connects — must not move, whatever the angle or the paper. Everything
    else about the correction is measured from it."""
    for paper in [(210.0, 297.0), (430.0, 296.9), (100.0, 150.0)]:
        matrix = axis_skew.skew_matrix(3.2, true_axis, *paper)
        assert _apply_matrix(matrix, 0.0, 0.0) == pytest.approx((0.0, 0.0), abs=1e-9)


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
        phys_x, phys_y = _physical_forward(mdx, mdy, skew_deg, true_axis)
        assert phys_x == pytest.approx(dx, abs=1e-9)
        assert phys_y == pytest.approx(dy, abs=1e-9)


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [0.1, 2.7, -4.9])
def test_skew_delta_matches_skew_matrixs_linear_part(true_axis, skew_deg):
    """Applying skew_matrix to two points and taking the difference of the
    results must equal skew_delta applied to the difference of the points —
    confirms skew_delta really is just skew_matrix's linear part.

    At scale 1.0, and only there: absorb's shrink is a decision about fitting
    artwork onto paper, not a property of the machine, so a carriage asked to
    move 10 mm must move 10 mm whatever the artwork is doing. skew_delta has
    no scale parameter at all, which is how that stays true."""
    matrix = axis_skew.skew_matrix(skew_deg, true_axis, 210.0, 297.0)
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
    """The property that matters most to anyone who has never measured a
    skew: at 0° the file is not even opened, let alone rewritten."""
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


# ── What the correction costs: travel, not accuracy ─────────────────────
#
# Nothing is displaced or deformed (proved above), so the only thing the
# correction can cost is room to move in: a point `h` mm down the driving
# axis is commanded `h * tan(skew)` further across than it draws. The driver
# clips motor coordinates at 0 and at the page edge, so that widened
# footprint is what "clip" and "absorb" are actually deciding between.

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


def _ink_bounds(svg_path: Path, paper_w: float, paper_h: float):
    filtered = svg_path.with_name("filtered.svg")
    svg_utils.filter_to_layers(svg_path, [0], filtered)
    return svg_utils.ink_bounds_mm(
        filtered, [0], paper_width_mm=paper_w, paper_height_mm=paper_h,
        margin_top_mm=0.0, margin_right_mm=0.0,
        margin_bottom_mm=0.0, margin_left_mm=0.0, fit_content=True,
    )


def _corners(ink):
    left, top, right, bottom = ink
    return [(left, top), (right, top), (right, bottom), (left, bottom)]


def _motor_box(ink, skew_deg, true_axis, paper_w, paper_h, scale=1.0):
    """The bounding box of the commands sent to the driver — what its travel
    bounds and page clip actually see."""
    matrix = axis_skew.skew_matrix(skew_deg, true_axis, paper_w, paper_h, scale)
    pts = [_apply_matrix(matrix, x, y) for x, y in _corners(ink)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _drawn_box(ink, skew_deg, true_axis, paper_w, paper_h, scale=1.0):
    """The bounding box of what the skewed machine physically puts on paper."""
    matrix = axis_skew.skew_matrix(skew_deg, true_axis, paper_w, paper_h, scale)
    pts = [_physical_forward(*_apply_matrix(matrix, x, y), skew_deg, true_axis)
           for x, y in _corners(ink)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def test_full_bleed_fixture_really_has_zero_margin(tmp_path):
    """Sanity check on the fixture itself, before trusting any test built
    on top of it: with zero job margins, the ink fills the whole page."""
    bounds = _ink_bounds(_full_bleed_svg(tmp_path, 100.0, 150.0), 100.0, 150.0)
    # vpype's own px<->mm conversion introduces float noise around 1e-4mm —
    # utterly immaterial to a pen plotter, but too coarse for a 1e-6 bound.
    assert bounds == pytest.approx((0.0, 0.0, 100.0, 150.0), abs=1e-3)


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [0.1, 0.3, 1.0, 5.0, -5.0])
def test_full_bleed_needs_one_edges_worth_of_extra_travel(tmp_path, true_axis, skew_deg):
    """For ink that already touches all four edges, the commands run past one
    page edge by exactly perpendicular-dimension * tan(skew) — and not at all
    past the other, nor on the axis the correction leaves alone.

    Which edge follows the sign, and it is the *commands* that overrun, not
    the drawing: the ink itself is drawn exactly on the page (see
    test_the_correction_lands_artwork_exactly_where_it_was_placed). This is
    the room absorb has to buy, measured rather than asserted."""
    paper_w, paper_h = 100.0, 150.0
    ink = _ink_bounds(_full_bleed_svg(tmp_path, paper_w, paper_h), paper_w, paper_h)
    left, top, right, bottom = _motor_box(ink, skew_deg, true_axis, paper_w, paper_h)

    tan_t = abs(math.tan(math.radians(skew_deg)))
    if true_axis == "x":
        lo, hi, limit = left, right, paper_w
        untouched, untouched_limit = (top, bottom), paper_h
        expected = paper_h * tan_t
    else:
        lo, hi, limit = top, bottom, paper_h
        untouched, untouched_limit = (left, right), paper_w
        expected = paper_w * tan_t

    # abs=1e-3: absorbs the same ~1e-4mm vpype measurement noise as above,
    # still three orders of magnitude tighter than anything a pen can draw.
    under, over = -lo, hi - limit
    if skew_deg > 0:
        assert (under, over) == pytest.approx((expected, 0.0), abs=1e-3)
    else:
        assert (under, over) == pytest.approx((0.0, expected), abs=1e-3)
    assert untouched == pytest.approx((0.0, untouched_limit), abs=1e-3)


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [0.2, 1.0, 5.0, -0.2, -1.0, -5.0])
def test_a_design_with_room_to_spare_is_not_touched_at_all(true_axis, skew_deg):
    """The common case, and the one the old page-sized reserve got wrong. A
    design that leaves the correction the travel it needs is plotted at its
    declared size, in its declared place, in either mode — its commands stay
    on the page, so absorb has nothing to do."""
    paper_w, paper_h = 200.0, 300.0
    cx, cy = paper_w / 2, paper_h / 2
    ink = (cx - 30.0, cy - 20.0, cx + 30.0, cy + 20.0)

    assert axis_skew.absorb_scale(skew_deg, true_axis, ink, paper_w, paper_h) == 1.0
    motor = _motor_box(ink, skew_deg, true_axis, paper_w, paper_h)
    assert motor[0] >= 0 and motor[1] >= 0
    assert motor[2] <= paper_w and motor[3] <= paper_h
    assert _drawn_box(ink, skew_deg, true_axis, paper_w, paper_h) == pytest.approx(ink, abs=1e-9)


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [0.2, 1.0, 5.0, -0.2, -1.0, -5.0])
@pytest.mark.parametrize("ink_size", [(60.0, 40.0), (200.0, 300.0)])
def test_a_centred_design_is_drawn_centred(true_axis, skew_deg, ink_size):
    """The requirement in one line, checked on the paper rather than in
    motor space, and for full-bleed ink (which absorb does shrink) as well as
    ink with room (which it does not). Neither the correction nor absorb's
    shrink may slide artwork across the page."""
    paper_w, paper_h = 200.0, 300.0
    cx, cy = paper_w / 2, paper_h / 2
    w, h = ink_size
    ink = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
    scale = axis_skew.absorb_scale(skew_deg, true_axis, ink, paper_w, paper_h)
    left, top, right, bottom = _drawn_box(ink, skew_deg, true_axis, paper_w, paper_h, scale)
    assert (left + right) / 2 == pytest.approx(cx, abs=1e-9)
    assert (top + bottom) / 2 == pytest.approx(cy, abs=1e-9)


def test_full_bleed_at_realistic_skew_costs_under_a_couple_of_millimetres(tmp_path):
    """The ±5° cap is a sanity bound, not a typical measurement — real
    hardware skew on a reasonably assembled machine is usually well under
    1°. At a realistic 0.3° on an A4-sized page, even edge-to-edge artwork
    only needs a millimetre and a half of extra travel."""
    paper_w, paper_h = 210.0, 297.0
    ink = _ink_bounds(_full_bleed_svg(tmp_path, paper_w, paper_h), paper_w, paper_h)
    left, _, right, _ = _motor_box(ink, 0.3, "x", paper_w, paper_h)
    assert max(-left, right - paper_w) < 1.6


def test_apply_axis_skew_on_a_full_bleed_stage_svg_does_not_error(tmp_path):
    """End-to-end smoke test through the real pipeline, at zero margin: no
    exception, page size untouched, and the rect's own raw coordinates are
    unchanged (only the wrapping transform carries the correction)."""
    paper_w, paper_h = 100.0, 150.0
    src = _full_bleed_svg(tmp_path, paper_w, paper_h)
    filtered = tmp_path / "filt.svg"
    svg_utils.filter_to_layers(src, [0], filtered)
    rendered = tmp_path / "rendered.svg"
    svg_utils.transform_to_paper(
        filtered, rendered,
        paper_width_mm=paper_w, paper_height_mm=paper_h,
        margin_top_mm=0.0, margin_right_mm=0.0,
        margin_bottom_mm=0.0, margin_left_mm=0.0, fit_content=True,
    )
    axis_skew.apply_axis_skew(rendered, 5.0, "x", paper_w, paper_h)

    root = etree.parse(str(rendered)).getroot()
    assert root.get("width") == f"{paper_w}mm"
    assert root.get("height") == f"{paper_h}mm"
    rect = root.find(f".//{{{svg_utils.SVG_NS}}}rect")
    assert (rect.get("x"), rect.get("y"), rect.get("width"), rect.get("height")) == (
        "0", "0", str(paper_w), str(paper_h))


# ── "absorb" mode: absorb_scale ────────────────────────────────────────────
#
# absorb answers one question — "would correcting this angle push *this
# design's* commands off the page, and if so, by how little can we shrink it
# so they fit?" Three properties matter, and none of them held for the
# page-sized one-axis squeeze this replaced: it does nothing when the ink has
# room, what it does do is uniform, and it never slides anything.

PAPER = (200.0, 300.0)


@pytest.mark.parametrize("true_axis", ["x", "y"])
def test_absorb_is_one_at_zero_skew(true_axis):
    assert axis_skew.absorb_scale(0.0, true_axis, (0.0, 0.0, 200.0, 300.0), *PAPER) == 1.0


@pytest.mark.parametrize("true_axis", ["x", "y"])
def test_absorb_is_one_when_there_is_nothing_drawable_to_measure(true_axis):
    assert axis_skew.absorb_scale(3.0, true_axis, None, *PAPER) == 1.0


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [0.2, 1.0, 2.7, 5.0, -0.2, -1.0, -2.7, -5.0])
def test_absorb_shrinks_full_bleed_ink_by_exactly_enough(true_axis, skew_deg):
    """When the ink really is edge to edge, absorb has to buy the room — and
    buy exactly enough, not more. The commands come back flush inside the
    page on the corrected axis, touching the edge they were overrunning."""
    paper_w, paper_h = PAPER
    ink = (0.0, 0.0, paper_w, paper_h)
    scale = axis_skew.absorb_scale(skew_deg, true_axis, ink, *PAPER)
    assert scale < 1.0
    left, top, right, bottom = _motor_box(ink, skew_deg, true_axis, *PAPER, scale)
    assert left >= -1e-9 and top >= -1e-9
    assert right <= paper_w + 1e-9 and bottom <= paper_h + 1e-9
    # Flush: one of the two edges is exactly on the boundary, or absorb
    # bought more room than it needed to.
    touching = min(abs(left), abs(right - paper_w)) if true_axis == "x" \
        else min(abs(top), abs(bottom - paper_h))
    assert touching == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [0.2, 1.0, 5.0, -0.2, -1.0, -5.0])
def test_absorb_reclaims_the_same_margin_at_both_edges(true_axis, skew_deg):
    """"Share it evenly" made concrete. Full-bleed artwork comes back off the
    machine centred, with an identical strip of white paper at each of the
    two edges on the corrected axis — not all of it at one edge, which is
    what the page-corner reserve used to do."""
    paper_w, paper_h = PAPER
    ink = (0.0, 0.0, paper_w, paper_h)
    scale = axis_skew.absorb_scale(skew_deg, true_axis, ink, *PAPER)
    left, top, right, bottom = _drawn_box(ink, skew_deg, true_axis, *PAPER, scale)
    assert left == pytest.approx(paper_w - right, abs=1e-9)
    assert top == pytest.approx(paper_h - bottom, abs=1e-9)
    assert left > 0 and top > 0


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [0.5, 2.7, 5.0, -0.5, -2.7, -5.0])
def test_absorb_never_deforms_what_it_scales(true_axis, skew_deg):
    """Uniform means uniform. The page-sized squeeze this replaced scaled one
    axis only, so a plotted 100mm square came off the machine 1.2% narrower
    than it was tall. Here a square commanded through the whole correction —
    scale and shear together — is physically drawn as a square again, its
    size down by exactly the one scale factor."""
    paper_w, paper_h = PAPER
    scale = axis_skew.absorb_scale(skew_deg, true_axis, (0.0, 0.0, paper_w, paper_h), *PAPER)
    matrix = axis_skew.skew_matrix(skew_deg, true_axis, paper_w, paper_h, scale)
    square = [(50.0, 80.0), (150.0, 80.0), (150.0, 180.0), (50.0, 180.0)]
    drawn = [_physical_forward(*_apply_matrix(matrix, x, y), skew_deg, true_axis)
             for x, y in square]
    sides = [math.dist(drawn[i], drawn[(i + 1) % 4]) for i in range(4)]
    # All four sides equal (still a square) and both diagonals equal (still
    # right-angled), at exactly the declared size times the scale.
    assert sides == pytest.approx([100.0 * scale] * 4, abs=1e-9)
    assert math.dist(drawn[0], drawn[2]) == pytest.approx(math.dist(drawn[1], drawn[3]), abs=1e-9)


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [1.0, -1.0])
def test_absorb_does_not_pull_a_deliberately_cropped_design_back_onto_the_page(
        true_axis, skew_deg):
    """Artwork placed to run off the page is a crop the user can see in the
    preview, and pyaxidraw clips it at plot time like always — the same
    principle _delta_correction_mm follows. Absorb pays for what the *shear*
    added and nothing else, so a design already twice the page's size is
    shrunk by the correction's share only, not squashed onto the sheet."""
    paper_w, paper_h = PAPER
    cx, cy = paper_w / 2, paper_h / 2
    ink = (cx - paper_w, cy - paper_h, cx + paper_w, cy + paper_h)
    scale = axis_skew.absorb_scale(skew_deg, true_axis, ink, *PAPER)
    # Shrunk a little (the correction did widen its footprint) but nowhere
    # near the ~0.5 that fitting this onto the page would demand.
    assert 0.9 < scale < 1.0
    # The window absorb is fitting into is the page widened to whatever this
    # design already claimed — so the commands land inside that, and touch it,
    # rather than being pulled all the way back onto the paper. (Only the
    # binding edge touches: the scale is uniform, so the other end comes in
    # with a little slack rather than being stretched out to meet its limit.)
    i = 0 if true_axis == "x" else 1
    allowed_lo, allowed_hi = min(0.0, ink[i]), max(PAPER[i], ink[i + 2])
    lo, hi = _motor_box(ink, skew_deg, true_axis, *PAPER, scale)[i::2]
    assert lo >= allowed_lo - 1e-9 and hi <= allowed_hi + 1e-9
    assert min(abs(lo - allowed_lo), abs(hi - allowed_hi)) == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("true_axis", ["x", "y"])
@pytest.mark.parametrize("skew_deg", [0.1, 2.7, -4.9])
@pytest.mark.parametrize("scale", [1.0, 0.98, 0.5])
def test_inverse_skew_point_is_the_exact_algebraic_inverse(true_axis, skew_deg, scale):
    """What keeps the live draw-stream drawing the design rather than the
    motor path — including absorb's scale, which it has to undo too."""
    matrix = axis_skew.skew_matrix(skew_deg, true_axis, 210.0, 297.0, scale)
    for px, py in POINTS:
        mx, my = _apply_matrix(matrix, px, py)
        back = axis_skew.inverse_skew_point(
            mx, my, skew_deg, true_axis, 210.0, 297.0, scale)
        assert back == pytest.approx((px, py), abs=1e-9)


def test_apply_axis_skew_applies_absorb_and_shear_as_one_wrap(tmp_path):
    """One transform, one <g>: the scale and the shear compose into a single
    matrix, so there is no ordering constraint left to get wrong (there used
    to be two wraps and a rule about which went inside)."""
    rendered = _rendered_stage_svg(tmp_path)
    placement_transform = etree.parse(str(rendered)).getroot().find(
        f"{{{svg_utils.SVG_NS}}}g").get("transform")

    axis_skew.apply_axis_skew(rendered, 2.5, "x", 210.0, 297.0, 0.97)

    root = etree.parse(str(rendered)).getroot()
    outer = root.find(f"{{{svg_utils.SVG_NS}}}g")
    assert outer.get("transform") == axis_skew.skew_matrix(2.5, "x", 210.0, 297.0, 0.97)
    inner = outer.find(f"{{{svg_utils.SVG_NS}}}g")
    assert inner.get("transform") == placement_transform
