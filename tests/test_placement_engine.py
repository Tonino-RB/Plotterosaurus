"""Unit tests for the placement engine itself.

These differ in kind from test_placement.py. Those are characterization tests
— they assert only that behaviour hasn't *changed*, which is all you can
assert about rules nobody had written down. These assert the rules stated in
app/placement.py's docstring, now that they are settled. When the two
disagree, this file is right and the golden file needs regenerating.
"""
import math

import pytest

from app import placement

A4_P = dict(paper_width_mm=210.0, paper_height_mm=297.0)
NO_MARGINS = dict(margin_top_mm=0.0, margin_right_mm=0.0,
                  margin_bottom_mm=0.0, margin_left_mm=0.0)


def place(doc_w, doc_h, viewbox=None, **kw):
    args = {**A4_P, **NO_MARGINS, "fit_content": False, **kw}
    return placement.compute(doc_w, doc_h, viewbox, **args)


# The canvas is the composition -------------------------------------------

def test_fit_scales_the_canvas_not_the_ink():
    """A drawing occupying a corner of a large canvas keeps its position
    within that canvas; fit sizes the canvas to the page."""
    p = place(1000.0, 1000.0, fit_content=True)
    # 1000mm square onto 210x297 -> limited by width.
    assert p.fit_scale == pytest.approx(210.0 / 1000.0)
    assert p.footprint_w_mm == pytest.approx(210.0)


def test_fit_off_leaves_scale_alone():
    assert place(1000.0, 1000.0, fit_content=False).fit_scale == 1.0


def test_fit_uses_the_rotated_bounding_box():
    """At 45 degrees a square canvas needs sqrt(2) more room, so fit has to
    shrink further than it would unrotated."""
    upright = place(100.0, 100.0, fit_content=True)
    turned = place(100.0, 100.0, fit_content=True, transform_rotation_deg=45.0)
    assert turned.fit_scale == pytest.approx(upright.fit_scale / math.sqrt(2))


# Anchoring ----------------------------------------------------------------

def test_anchors_to_the_margin_box_top_left():
    """The content's top-left lands on the margin box's top-left — so the
    document's centre sits half a footprint in from there."""
    p = place(100.0, 80.0, margin_left_mm=12.0, margin_top_mm=7.0)
    assert p.center_x_mm == pytest.approx(12.0 + 50.0)
    assert p.center_y_mm == pytest.approx(7.0 + 40.0)


def test_offset_shifts_the_anchor():
    base = place(100.0, 80.0)
    moved = place(100.0, 80.0, transform_offset_x_mm=20.0, transform_offset_y_mm=-10.0)
    assert moved.center_x_mm - base.center_x_mm == pytest.approx(20.0)
    assert moved.center_y_mm - base.center_y_mm == pytest.approx(-10.0)


# Auto-rotate --------------------------------------------------------------

@pytest.mark.parametrize("doc_w,doc_h,paper,expected", [
    # Portrait doc on a landscape page under a policy -> turn it.
    (100.0, 200.0, (297.0, 210.0), 90.0),
    # Landscape doc on a landscape page -> already matches.
    (200.0, 100.0, (297.0, 210.0), 0.0),
    # Portrait doc on a portrait page -> already matches.
    (100.0, 200.0, (210.0, 297.0), 0.0),
])
def test_auto_rotate_turns_content_to_match_the_page(doc_w, doc_h, paper, expected):
    p = placement.compute(
        doc_w, doc_h, None, paper[0], paper[1], **NO_MARGINS,
        fit_content=False, machine_auto_rotate="landscape")
    assert p.rotation_deg == expected


def test_auto_rotate_is_off_by_default():
    p = place(100.0, 200.0, paper_width_mm=297.0, paper_height_mm=210.0)
    assert p.rotation_deg == 0.0


def test_square_content_is_never_auto_rotated():
    """A9: a square matches every orientation, so turning it only moves the
    artwork. A strict > used to class it as portrait and earn it 90 degrees."""
    p = placement.compute(200.0, 200.0, None, 297.0, 210.0, **NO_MARGINS,
                          fit_content=False, machine_auto_rotate="landscape")
    assert p.rotation_deg == 0.0


def test_square_tolerance_is_sized_for_unit_rounding():
    """Just inside the epsilon is square; just outside is a real aspect
    difference and must still rotate. Both are barely-landscape documents on
    a portrait page, so only the squareness exemption decides the outcome."""
    def rotation(doc_h):
        return placement.compute(200.0, doc_h, None, 210.0, 297.0, **NO_MARGINS,
                                 fit_content=False,
                                 machine_auto_rotate="portrait").rotation_deg

    assert rotation(200.0 - placement.SQUARE_EPSILON_MM / 2) == 0.0
    assert rotation(200.0 - placement.SQUARE_EPSILON_MM * 10) == 90.0


def test_policy_direction_only_switches_auto_rotate_on_or_off():
    """Non-obvious, and worth pinning so nobody "fixes" it: the engine reads
    the page's *actual* orientation, not the policy's name. The policy has
    already been applied by whoever swapped the paper dimensions, so
    "portrait" and "landscape" behave identically here — only "off" differs."""
    def rotation(policy):
        return placement.compute(100.0, 200.0, None, 297.0, 210.0, **NO_MARGINS,
                                 fit_content=False,
                                 machine_auto_rotate=policy).rotation_deg

    assert rotation("portrait") == rotation("landscape") == 90.0
    assert rotation(placement.AUTO_ROTATE_OFF) == 0.0


def test_auto_rotate_adds_to_the_jobs_own_rotation():
    p = placement.compute(100.0, 200.0, None, 297.0, 210.0, **NO_MARGINS,
                          fit_content=False, transform_rotation_deg=15.0,
                          machine_auto_rotate="landscape")
    assert p.rotation_deg == pytest.approx(105.0)


# viewBox mapping ----------------------------------------------------------

def test_meet_uses_the_smaller_axis_ratio():
    """width/height 2:1 against a 1:1 viewBox: `meet` scales uniformly by the
    smaller ratio, so 1 user unit is 1mm — not the 2mm an x-only ratio gives."""
    p = place(200.0, 100.0, viewbox=(0.0, 0.0, 100.0, 100.0))
    assert p.user_scale == pytest.approx(1.0)


def test_no_viewbox_means_user_units_are_millimetres():
    assert place(150.0, 100.0, viewbox=None).user_scale == pytest.approx(1.0)


def test_viewbox_offset_is_recentred():
    p = place(100.0, 100.0, viewbox=(-50.0, -50.0, 100.0, 100.0))
    assert (p.vb_center_x, p.vb_center_y) == (0.0, 0.0)


def test_missing_document_size_falls_back_to_the_paper():
    p = place(None, None)
    assert (p.doc_center_x_mm, p.doc_center_y_mm) == (105.0, 148.5)


# Mapping geometry onto the page -------------------------------------------

def test_document_centre_maps_to_the_placement_centre():
    p = place(100.0, 80.0, transform_offset_x_mm=5.0)
    assert p.doc_mm_to_page(50.0, 40.0) == pytest.approx((p.center_x_mm, p.center_y_mm))


def test_unrotated_rect_maps_straight_through():
    p = place(100.0, 100.0)
    assert p.doc_mm_rect_to_page(10.0, 20.0, 30.0, 40.0) == pytest.approx((10.0, 20.0, 30.0, 40.0))


def test_rotated_rect_reports_position_not_just_size():
    """All four corners are mapped: under rotation a rectangle's size is
    position-independent but its location is not."""
    p = place(100.0, 100.0, transform_rotation_deg=90.0)
    left, top, right, bottom = p.doc_mm_rect_to_page(0.0, 0.0, 20.0, 10.0)
    assert (right - left, bottom - top) == pytest.approx((10.0, 20.0))
    # A quarter turn about the canvas centre sends the top-left corner to the
    # top-right, so the mapped box no longer starts at the origin.
    assert left == pytest.approx(90.0)
    assert top == pytest.approx(0.0)


def test_scale_expands_about_the_placement_centre():
    p = place(100.0, 100.0, transform_scale=2.0)
    left, top, right, bottom = p.doc_mm_rect_to_page(0.0, 0.0, 100.0, 100.0)
    assert (right - left, bottom - top) == pytest.approx((200.0, 200.0))


# Properties the browser extrapolates along ---------------------------------
#
# The preview renders the server's answer, and during a drag it has to keep
# moving between answers or the artwork freezes until the mouse is released.
# It does that by extrapolating the last answer along the axis being dragged,
# which is only sound while these two properties hold. They are asserted here,
# in the engine's own suite, so the browser's assumption cannot quietly stop
# being true. See effectivePlacement() in static/app.js.

def test_offset_translates_the_placement_and_nothing_else():
    """Offset enters `compute` at exactly one place, additively. Everything
    the preview draws with — rotation, scales, footprint, the document and
    viewBox centres — is identical, so the browser can follow an offset drag
    by translating the answer it already has."""
    base = place(150.0, 100.0, fit_content=True, transform_rotation_deg=20.0)
    moved = place(150.0, 100.0, fit_content=True, transform_rotation_deg=20.0,
                  transform_offset_x_mm=17.0, transform_offset_y_mm=-9.0)

    assert moved.center_x_mm == pytest.approx(base.center_x_mm + 17.0)
    assert moved.center_y_mm == pytest.approx(base.center_y_mm - 9.0)
    for field in ("rotation_deg", "fit_scale", "mm_scale", "user_scale",
                  "footprint_w_mm", "footprint_h_mm", "doc_center_x_mm",
                  "doc_center_y_mm", "vb_center_x", "vb_center_y"):
        assert getattr(moved, field) == pytest.approx(getattr(base, field)), field


@pytest.mark.parametrize("fit_content", [False, True])
@pytest.mark.parametrize("rotation", [0.0, 37.0, 90.0])
def test_scale_is_linear_and_pins_the_footprints_top_left(fit_content, rotation):
    """Scale multiplies the footprint and leaves everything else alone —
    including `fit_scale`, which is computed per unit of scale and so does not
    move underneath it. Anchoring at the margin box's top-left then means the
    footprint's top-left corner is the fixed point of a scale change, which is
    what lets the browser follow a scale drag from a cached answer."""
    kw = dict(fit_content=fit_content, transform_rotation_deg=rotation,
              transform_offset_x_mm=8.0, transform_offset_y_mm=3.0)
    base = place(150.0, 100.0, transform_scale=1.0, **kw)
    scaled = place(150.0, 100.0, transform_scale=2.5, **kw)

    assert scaled.footprint_w_mm == pytest.approx(base.footprint_w_mm * 2.5)
    assert scaled.footprint_h_mm == pytest.approx(base.footprint_h_mm * 2.5)
    assert scaled.user_scale == pytest.approx(base.user_scale * 2.5)
    assert scaled.rotation_deg == pytest.approx(base.rotation_deg)
    assert scaled.fit_scale == pytest.approx(base.fit_scale)

    # The top-left corner of the footprint does not move.
    assert (scaled.center_x_mm - scaled.footprint_w_mm / 2
            == pytest.approx(base.center_x_mm - base.footprint_w_mm / 2))
    assert (scaled.center_y_mm - scaled.footprint_h_mm / 2
            == pytest.approx(base.center_y_mm - base.footprint_h_mm / 2))


def test_auto_rotate_does_not_depend_on_the_jobs_own_angle():
    """The browser recovers the machine's auto-rotate contribution by
    subtracting the job's angle from the answer's `rotation_deg`. That is only
    valid because the policy looks at the paper and the document, never at the
    angle the user is dragging."""
    for policy in ("off", "portrait", "landscape"):
        contributions = {
            place(150.0, 100.0, transform_rotation_deg=angle,
                  machine_auto_rotate=policy).rotation_deg - angle
            for angle in (0.0, 17.0, 90.0, 213.5, -45.0)
        }
        assert len(contributions) == 1, (policy, contributions)


@pytest.mark.parametrize("rotation", [0.0, 30.0, 90.0, 145.0, -60.0])
def test_footprint_is_the_rotated_canvas_bounding_box(rotation):
    """With fit off, the footprint follows straight from the canvas size and
    the angle — which is what lets the browser follow a rotation drag without
    a round trip. With fit on it does not, because the angle feeds back into
    fit_scale; the browser skips extrapolation in that case."""
    doc_w, doc_h, scale = 150.0, 100.0, 1.4
    p = place(doc_w, doc_h, transform_rotation_deg=rotation, transform_scale=scale)
    rad = math.radians(rotation)
    cos, sin = abs(math.cos(rad)), abs(math.sin(rad))
    assert p.fit_scale == 1.0
    assert p.footprint_w_mm == pytest.approx((doc_w * cos + doc_h * sin) * scale)
    assert p.footprint_h_mm == pytest.approx((doc_w * sin + doc_h * cos) * scale)


@pytest.mark.parametrize("rotation", [0.0, 30.0, 90.0])
@pytest.mark.parametrize("scale", [1.0, 0.4, 2.2])
def test_the_footprints_top_left_is_the_anchor_whatever_the_transform(rotation, scale):
    """The one fact the browser's recentring rests on: the footprint's
    top-left corner sits at (margin + offset) and moves for no other reason.
    Rotation and scale resize the box about that corner."""
    p = placement.compute(
        150.0, 100.0, None, 210.0, 297.0, 7.0, 0.0, 0.0, 11.0, False,
        transform_scale=scale, transform_rotation_deg=rotation,
        transform_offset_x_mm=4.0, transform_offset_y_mm=-6.0)
    assert p.center_x_mm - p.footprint_w_mm / 2 == pytest.approx(11.0 + 4.0)
    assert p.center_y_mm - p.footprint_h_mm / 2 == pytest.approx(7.0 - 6.0)
