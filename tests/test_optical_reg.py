"""The optical-registration geometry, exercised on synthetic frames.

`optical_reg` is pure (uint8 image in, floats out), so every case here renders
its own crosses at a known sub-pixel position and checks what comes back.

Two renderers, deliberately:

* `_render` draws a *solid* plus. It pins the behaviour of a mark that is one
  connected component, which is what `measure(group_px=0)` assumes.
* `_render_mm` draws what the plotter actually puts on the paper — the four
  separate arms of `optical_reg.cross_arms`, at a real nib width and a real
  mm-per-pixel scale. Nothing here may assume a shape the plotter cannot draw:
  the detector once shipped able to read only solid crosses, while `_plot_cross`
  drew open ones, and the whole feature was dead on hardware because every case
  in this file was written against the shape it could read.

The load-bearing cases are `test_center_is_width_independent` and
`test_measure_reads_every_nib_pair`: the whole reason the module fits arm lines
instead of taking a pixel centroid is so a 0.1 mm fineliner and a 1.4 mm marker
still register against each other.
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


# Nib widths the pens in use actually span, mm.
NIBS = (0.1, 0.2, 0.4, 0.7, 1.0, 1.4)
SIZE_MM = 3.0
MAX_CORRECTION_MM = 3.0
# What plot_worker derives from those two (see _reg_tolerance_mm / _probe_offset
# / the probe floor in _run_optical_reg_phase). Mirrored rather than imported so
# a change to the worker's geometry shows up here as a failure to explain,
# not as a silently different test.
TOL_MM = 1.6 * MAX_CORRECTION_MM
PROBE_MM = max(1.25 * SIZE_MM, 1.5 * TOL_MM / math.sqrt(2.0))
LANE_MM = 2.4 * MAX_CORRECTION_MM


def _render_mm(crosses, mm_per_px, size_mm=SIZE_MM, pad_mm=None):
    """Grey frame of the real open-cross geometry: `crosses` is
    `[(cx_mm, cy_mm, nib_mm)]`, positioned from the top-left of the frame.

    Antialiased analytically — a one-pixel coverage ramp at each stroke's edge —
    rather than by supersampling, which on a frame this size is minutes of work
    for the same sub-pixel edges.
    """
    pad = size_mm if pad_mm is None else pad_mm
    w_mm = max(c[0] for c in crosses) + size_mm / 2 + pad
    h_mm = max(c[1] for c in crosses) + size_mm / 2 + pad
    w, h = int(math.ceil(w_mm / mm_per_px)), int(math.ceil(h_mm / mm_per_px))
    xg = ((np.arange(w) + 0.5) * mm_per_px)[None, :]
    yg = ((np.arange(h) + 0.5) * mm_per_px)[:, None]
    cov = np.zeros((h, w), dtype=np.float64)
    for cx, cy, nib in crosses:
        r = nib / 2.0
        for (x0, y0), (x1, y1) in optical_reg.cross_arms(cx, cy, size_mm):
            vx, vy = x1 - x0, y1 - y0
            t = np.clip(((xg - x0) * vx + (yg - y0) * vy) / (vx * vx + vy * vy),
                        0.0, 1.0)
            dx, dy = xg - (x0 + t * vx), yg - (y0 + t * vy)
            dist = np.sqrt(dx * dx + dy * dy)
            cov = np.maximum(cov, np.clip((r + 0.5 * mm_per_px - dist) / mm_per_px,
                                          0.0, 1.0))
    return np.clip(255.0 * (1.0 - cov), 0, 255).astype(np.uint8)


def _read_pair(crosses, mm_per_px, offset_mm):
    """measure() over `crosses`, wired up the way plot_worker wires it, and
    converted back to the millimetre separation it claims to see."""
    img = _render_mm(crosses, mm_per_px)
    got = optical_reg.measure(
        img, optical_reg.mm_to_px(offset_mm, mm_per_px, 0.0),
        group_px=optical_reg.cross_gap(SIZE_MM) / mm_per_px,
        tol_px=TOL_MM / mm_per_px)
    if got is None or not got["separable"]:
        return None, got
    return optical_reg.px_to_mm(got["sep_px"], mm_per_px, 0.0), got


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


# The real mark: four separate arms, a real nib, a real scale -----------------

@pytest.mark.parametrize("nib_ref,nib_probe", [
    (0.1, 0.1), (0.1, 0.2), (0.2, 0.1),      # near-identical fineliners
    (0.1, 1.4), (1.4, 0.1),                  # the extremes, both ways round
    (0.4, 0.7), (1.0, 1.4), (1.4, 1.4),
])
@pytest.mark.parametrize("mm_per_px", [0.02, 0.05])
def test_measure_reads_every_nib_pair(nib_ref, nib_probe, mm_per_px):
    """Whatever pens the two layers use, the separation comes back right.

    This is the case the shipped detector could not pass at all: an open cross
    drawn with anything under a ~1 mm nib leaves four disconnected arms, so the
    two largest components were two arms of one cross and every measurement
    reported "not separable".
    """
    mis = (0.4, -0.3)
    ref = (SIZE_MM, SIZE_MM, nib_ref)
    probe = (ref[0] + PROBE_MM + mis[0], ref[1] + PROBE_MM + mis[1], nib_probe)
    sep, got = _read_pair([ref, probe], mm_per_px, (PROBE_MM, PROBE_MM))
    assert sep is not None, f"not separable: {got}"
    assert sep[0] == pytest.approx(PROBE_MM + mis[0], abs=0.1)
    assert sep[1] == pytest.approx(PROBE_MM + mis[1], abs=0.1)
    # A clean pair of marks should read as a confident one, whatever the nibs.
    assert got["confidence"] > 0.5


@pytest.mark.parametrize("nib", NIBS)
def test_center_is_nib_independent_on_the_real_mark(nib):
    """The centreline, not the ink. A 1.4 mm marker lays fourteen times the
    pixels of a 0.1 mm fineliner around the same intersection; the fitted
    centre must not move."""
    mm_per_px = 0.02
    thin = optical_reg.cross_center(_render_mm([(3.0, 3.0, 0.1)], mm_per_px))
    got = optical_reg.cross_center(_render_mm([(3.0, 3.0, nib)], mm_per_px))
    assert thin is not None and got is not None
    assert math.hypot(got[0] - thin[0], got[1] - thin[1]) * mm_per_px < 0.05


def test_an_open_cross_needs_its_arms_regrouped():
    """Why `group_px` exists, stated as a test: without it the four arms of one
    cross are four components and the pair is never found."""
    mm_per_px = 0.05
    crosses = [(SIZE_MM, SIZE_MM, 0.2),
               (SIZE_MM + PROBE_MM, SIZE_MM + PROBE_MM, 0.2)]
    img = _render_mm(crosses, mm_per_px)
    exp = optical_reg.mm_to_px((PROBE_MM, PROBE_MM), mm_per_px, 0.0)
    assert optical_reg.measure(img, exp)["separable"] is False
    assert optical_reg.measure(
        img, exp, group_px=optical_reg.cross_gap(SIZE_MM) / mm_per_px,
        tol_px=TOL_MM / mm_per_px)["separable"] is True


@pytest.mark.parametrize("probes_on_paper", [2, 3])
def test_earlier_probe_crosses_do_not_hijack_the_pair(probes_on_paper):
    """Every probe cross a run draws stays on the paper. The pair is the one
    matching the offset just asked for — not the two biggest blobs, which after
    a widen retry or a third layer are two marks that were never a pair."""
    mm_per_px = 0.05
    mis = (0.4, -0.3)
    k = probes_on_paper - 1
    offset = (PROBE_MM + k * LANE_MM, PROBE_MM)
    ref = (SIZE_MM, SIZE_MM, 1.4)
    crosses = [ref] + [
        (ref[0] + PROBE_MM + j * LANE_MM + mis[0], ref[1] + PROBE_MM + mis[1], 0.1)
        for j in range(probes_on_paper)
    ]
    sep, got = _read_pair(crosses, mm_per_px, offset)
    assert sep is not None, f"not separable: {got}"
    assert sep[0] == pytest.approx(offset[0] + mis[0], abs=0.1)
    assert sep[1] == pytest.approx(offset[1] + mis[1], abs=0.1)


def test_a_reading_further_off_than_the_tolerance_is_not_a_reading():
    """The pair has to look like the pair. Two marks separated by nothing near
    the offset asked for are not it, however clean they are."""
    mm_per_px = 0.05
    ref = (SIZE_MM, SIZE_MM, 0.4)
    probe = (ref[0] + PROBE_MM + 3 * TOL_MM, ref[1] + PROBE_MM, 0.4)
    sep, got = _read_pair([ref, probe], mm_per_px, (PROBE_MM, PROBE_MM))
    assert sep is None
    assert got["separable"] is False


def test_mm_to_px_refuses_an_uncalibrated_scale():
    with pytest.raises(ValueError, match="not calibrated"):
        optical_reg.mm_to_px((1.0, 1.0), mm_per_px=0.0, cam_rotation_deg=0.0)
