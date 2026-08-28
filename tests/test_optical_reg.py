"""The optical-registration geometry, exercised on synthetic frames.

`optical_reg` is pure (uint8 image in, floats out), so every case here renders
its own crosses at a known sub-pixel position and checks what comes back. The
load-bearing one is `test_center_is_width_independent`: the whole reason the
module fits arm lines instead of taking a pixel centroid is so a 1 mm nib and a
0.1 mm nib still register against each other.
"""
import math

import numpy as np
import pytest

from app import optical_reg


def _render(shape, crosses, arm=16.0, thick=2.0, ss=8):
    """Grey frame (255 paper, 0 ink) with each `(cx, cy, thick?)` cross drawn
    as a filled plus, supersampled ss× for honest sub-pixel edges."""
    h, w = shape
    yy, xx = np.mgrid[0:h * ss, 0:w * ss].astype(np.float64)
    x = (xx + 0.5) / ss
    y = (yy + 0.5) / ss
    ink = np.zeros((h * ss, w * ss), dtype=np.float64)
    for cross in crosses:
        cx, cy = cross[0], cross[1]
        t = cross[2] if len(cross) > 2 else thick
        horiz = (np.abs(y - cy) <= t / 2) & (np.abs(x - cx) <= arm)
        vert = (np.abs(x - cx) <= t / 2) & (np.abs(y - cy) <= arm)
        ink = np.maximum(ink, (horiz | vert).astype(np.float64))
    cov = ink.reshape(h, ss, w, ss).mean(axis=(1, 3))
    return np.clip(255.0 * (1.0 - cov), 0, 255).astype(np.uint8)


def test_cross_center_recovers_a_known_center():
    img = _render((90, 90), [(45.3, 41.6)])
    cx, cy, score = optical_reg.cross_center(img)
    assert abs(cx - 45.3) < 0.3
    assert abs(cy - 41.6) < 0.3
    assert score > 0.5


def test_center_is_width_independent():
    """A thin arm and a fat arm of the same cross must give the same centre:
    the pixel mass differs several ×, the fitted axis does not."""
    thin = optical_reg.cross_center(_render((90, 90), [(44.7, 45.2, 1.5)]))
    fat = optical_reg.cross_center(_render((90, 90), [(44.7, 45.2, 9.0)]))
    assert thin is not None and fat is not None
    assert math.hypot(thin[0] - fat[0], thin[1] - fat[1]) < 0.3
    # and both are on the true centre, not pulled off it
    assert abs(fat[0] - 44.7) < 0.3 and abs(fat[1] - 45.2) < 0.3


def test_measure_returns_the_offset_between_two_crosses():
    img = _render((140, 190), [(55.4, 66.7), (55.4 + 40.0, 66.7 + 9.0)])
    out = optical_reg.measure(img, expected_sep_px=(40.0, 9.0))
    assert out["separable"] is True
    dx, dy = out["sep_px"]
    assert abs(dx - 40.0) < 0.5
    assert abs(dy - 9.0) < 0.5
    assert out["confidence"] > 0.4


def test_measure_orients_the_pair_by_the_expected_offset():
    """Whichever cross the labeller happens to hand back first, sep_px points
    from reference to probe."""
    img = _render((130, 190), [(120.0, 62.0), (78.0, 62.0)])  # probe is left
    out = optical_reg.measure(img, expected_sep_px=(-42.0, 0.0))
    dx, dy = out["sep_px"]
    assert abs(dx - (-42.0)) < 0.6
    assert abs(dy) < 0.6


def test_measure_flags_overlapping_crosses_as_not_separable():
    img = _render((90, 110), [(48.0, 45.0), (56.0, 45.0)])  # 8px apart, arms 16
    out = optical_reg.measure(img, expected_sep_px=(8.0, 0.0))
    assert out["separable"] is False
    assert out["sep_px"] is None


def test_measure_returns_none_on_a_blank_frame():
    assert optical_reg.measure(np.full((60, 60), 240, np.uint8),
                               expected_sep_px=(10.0, 0.0)) is None


def test_near_blank_frame_finds_no_cross():
    """A frame whose darkest feature is within _MIN_INK_CONTRAST of paper has
    nothing to fit — detection bails rather than locking onto noise."""
    faint = _render((90, 90), [(45.0, 45.0)]).astype(np.int16)
    faint = np.clip(255 - (255 - faint) // 20, 0, 255).astype(np.uint8)
    assert optical_reg.cross_center(faint) is None


def test_px_to_mm_round_trips_through_mm_to_px():
    for rot in (0.0, 7.5, -20.0):
        mm = optical_reg.px_to_mm((13.0, -4.0), mm_per_px=0.05,
                                  cam_rotation_deg=rot)
        px = optical_reg.mm_to_px(mm, mm_per_px=0.05, cam_rotation_deg=rot)
        assert abs(px[0] - 13.0) < 1e-9
        assert abs(px[1] - (-4.0)) < 1e-9


def test_solve_scale_rotation_recovers_scale_and_angle():
    mm_per_px, rot = 0.04, 6.0
    pairs = []
    for mm_vec in ((3.0, 0.0), (0.0, 3.0), (2.0, -1.0)):
        px = optical_reg.mm_to_px(mm_vec, mm_per_px, rot)
        pairs.append((mm_vec, px))
    got_scale, got_rot, rms = optical_reg.solve_scale_rotation(pairs)
    assert abs(got_scale - mm_per_px) < 1e-6
    assert abs(got_rot - rot) < 1e-4
    assert rms < 1e-6


def test_solve_scale_rotation_residual_exposes_inconsistent_samples():
    bad = [((3.0, 0.0), (75.0, 0.0)),
           ((0.0, 3.0), (0.0, -75.0)),   # mirrored: y move seen inverted
           ((3.0, 3.0), (75.0, 75.0))]
    _, _, rms = optical_reg.solve_scale_rotation(bad)
    assert rms > 1.0


def test_annotate_marks_both_centers_without_touching_shape():
    img = _render((70, 90), [(30.0, 35.0), (60.0, 35.0)])
    rgb = optical_reg.annotate(img, (30.0, 35.0), (60.0, 35.0))
    assert rgb.shape == (70, 90, 3)
    assert rgb.dtype == np.uint8
    assert tuple(rgb[35, 30]) == (0, 220, 0)
    assert tuple(rgb[35, 60]) == (220, 0, 220)
