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
