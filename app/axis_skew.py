"""Hardware axis-skew correction: on a machine whose two motor axes aren't
perfectly perpendicular, a design that is square commands a parallelogram
in the physical world unless corrected. This module maps a physically
correct point to the motor-space command that lands the pen there once
that fixed hardware defect is taken into account.

The defect is modeled as a plain shear: a pure move along ``true_axis``
lands exactly where commanded, while a pure move along the other axis
drifts, proportional to ``tan(skew_deg)``, along ``true_axis`` too. This is
the same model ``static/app.js``'s ``skewAngleDeg`` assumes when it turns a
plotted test square's diagonals into ``skew_deg`` in the first place
(``d1^2 - d2^2 = 4 * side^2 * tan(skew)``), so a measurement made with that
calculator and a correction applied here always describe the same machine.

**The shear is anchored at motor (0, 0) and cannot be moved.** The machine's
own defect pivots there — an AxiDraw has no home switches, so its zero is
wherever the carriage stood when the plot connected — and only a correction
pivoted at that same point is exact. Pivot the correction anywhere else and
every point still draws the right *shape*, but the whole drawing lands
translated by ``pivot * tan(skew_deg)``: at 1 degree on A3, pivoting at the
page centre slides the artwork 2.6 mm across the paper. So it is anchored at
the origin, and a design centred on the page is drawn centred on the page.

What the correction *does* cost is travel. A point ``h`` mm down the driving
axis is commanded ``h * tan(skew_deg)`` further across than it draws, so a
design's motor-space footprint is ``ink_height * tan(skew_deg)`` wider than
the design itself — and the driver clips motor coordinates hard at 0 and at
the page edge. Designs with that much margin are unaffected and land exactly
where they were placed. Designs without it are what ``skew_mode`` decides
between: ``clip`` accepts the clip, ``absorb`` buys the room with a uniform
scale (see ``absorb_scale``) taken about the page centre, which keeps the
result centred where it was and shares the reclaimed margin evenly between
the two edges.

Pure and config-free: callers pass ``skew_deg``/``true_axis`` explicitly,
the same way ``svg_utils.transform_to_paper`` takes ``machine_auto_rotate``
as a parameter rather than importing ``config`` itself. Applied strictly
after placement (``app/placement.py``) has already decided where content
lands on the page — this module knows nothing about placement, only about
the axis defect.
"""
import math
from pathlib import Path

from lxml import etree

from .svg_utils import SVG_NS

TRUE_AXES = ("x", "y")


def _coefficients(skew_deg: float, true_axis: str,
                   paper_width_mm: float, paper_height_mm: float,
                   scale: float) -> tuple[float, float, float, float, float, float]:
    """The six numbers of the whole correction, as an SVG matrix's
    ``(a, b, c, d, e, f)``: a uniform scale by ``scale`` about the page
    centre, then the shear about motor ``(0, 0)``.

    One place computes them, so ``skew_matrix`` and ``inverse_skew_point``
    cannot drift apart — the inverse is taken from these numbers rather than
    derived by hand a second time.
    """
    tan_t = math.tan(math.radians(skew_deg))
    cx, cy = paper_width_mm / 2.0, paper_height_mm / 2.0
    k = 1.0 - scale
    if true_axis == "y":
        return scale, -scale * tan_t, 0.0, scale, k * cx, k * (cy - tan_t * cx)
    return scale, 0.0, -scale * tan_t, scale, k * (cx - tan_t * cy), k * cy


def skew_matrix(skew_deg: float, true_axis: str,
                 paper_width_mm: float, paper_height_mm: float,
                 scale: float = 1.0) -> str:
    """The SVG ``matrix(...)`` string mapping a physically correct point to
    the motor-space command that lands it there.

    ``true_axis`` names the axis assumed to travel in a straight line; the
    other axis is the one being corrected relative to it. ``scale`` is
    ``absorb`` mode's uniform shrink (see ``absorb_scale``); at the default
    1.0 this is the bare correction and nothing about the artwork's size or
    position changes at all.

    There is no pivot argument, because there is no choice to make: see the
    module docstring. The shear is anchored at the origin so the drawing
    lands where it was placed, and the scale — which only ever runs when
    absorb has decided artwork does not fit — is taken about the page centre
    so what it reclaims is shared evenly between the two edges.
    """
    return "matrix({},{},{},{},{},{})".format(
        *_coefficients(skew_deg, true_axis, paper_width_mm, paper_height_mm, scale))


def skew_delta(dx_mm: float, dy_mm: float, skew_deg: float, true_axis: str) -> tuple[float, float]:
    """The motor-space ``(dx_mm, dy_mm)`` that produces a true physical
    displacement of ``(dx_mm, dy_mm)`` — the same correction ``skew_matrix``
    applies to plotted artwork, but for a relative move rather than an
    absolute point.

    No pivot: a displacement's correction is the shear alone, since the
    pivot/translation term cancels out of any difference between two points.
    No ``scale`` either, and deliberately so — ``absorb``'s shrink is a
    decision about fitting artwork onto paper, not a property of the machine,
    and a carriage asked to move 10 mm must move 10 mm whatever the artwork
    is doing.

    Used by ``plot_worker._jog_carriage`` so that manual jogs, origin nudges,
    and Return to Origin all land where they were actually asked to, on a
    machine whose axes aren't perfectly perpendicular — the same physical
    correction plotted strokes already get.
    """
    if abs(skew_deg) < 1e-9:
        return dx_mm, dy_mm
    tan_t = math.tan(math.radians(skew_deg))
    if true_axis == "y":
        return dx_mm, dy_mm - dx_mm * tan_t
    return dx_mm - dy_mm * tan_t, dy_mm


def absorb_scale(skew_deg: float, true_axis: str,
                  ink_bounds: tuple[float, float, float, float] | None,
                  paper_width_mm: float, paper_height_mm: float) -> float:
    """The uniform scale ``absorb`` mode applies about the page centre: the
    largest factor at most 1 that keeps this design's *motor-space* footprint
    inside the window the driver will actually plot. ``1.0`` — nothing
    applied at all — whenever the correction costs the design nothing, which
    is every design with ``ink_height * tan(skew_deg)`` of margin to give.

    Measured from ``ink_bounds`` (where the selected layers' actual drawn
    geometry lands on the page, in mm — see ``svg_utils.ink_bounds_mm``), not
    from the page. Sizing this from the page instead assumed every design was
    full-bleed and so shrank every design, including ones with centimetres of
    clear paper on both sides that the correction could never have used up.

    Uniform, and about the page centre, which is what makes it the answer to
    "share the margin evenly": one factor for both axes, so no proportion in
    the artwork changes, and no translation, so nothing slides across the
    paper. Full-bleed artwork comes back centred, with an equal strip of
    white paper reclaimed at each of the two edges — rather than the whole
    strip at the one edge the correction happens to push toward.

    ``allowed`` is the page, widened to whatever the design already claimed
    for itself: artwork deliberately run off the page is a crop, visible in
    the preview and clipped at plot time like always, and pulling it back in
    here would silently overrule it. Absorb pays for what the *shear* added,
    nothing else.
    """
    if abs(skew_deg) < 1e-9 or ink_bounds is None:
        return 1.0
    left, top, right, bottom = ink_bounds
    tan_t = math.tan(math.radians(skew_deg))
    cx, cy = paper_width_mm / 2.0, paper_height_mm / 2.0
    if true_axis == "y":
        # y is the corrected axis; its drift grows with distance along x.
        lo, hi, centre, page = top, bottom, cy, paper_height_mm
        drive_lo, drive_hi, drive_centre = left, right, cx
    else:
        lo, hi, centre, page = left, right, cx, paper_width_mm
        drive_lo, drive_hi, drive_centre = top, bottom, cy

    # Both edges of the motor-space footprint are affine in the scale: the
    # page centre's own drift does not move with it, and everything else
    # scales with the design. Solving for the largest scale that keeps both
    # edges inside the window is then two divisions, not a search.
    fixed = centre - tan_t * drive_centre
    drifts = (-tan_t * (drive_lo - drive_centre), -tan_t * (drive_hi - drive_centre))
    span_lo = (lo - centre) + min(drifts)
    span_hi = (hi - centre) + max(drifts)
    allowed_lo, allowed_hi = min(0.0, lo), max(page, hi)

    scale = 1.0
    if span_lo < 0:
        scale = min(scale, (allowed_lo - fixed) / span_lo)
    if span_hi > 0:
        scale = min(scale, (allowed_hi - fixed) / span_hi)
    # A scale of zero or less would erase the drawing rather than fit it —
    # only reachable from a degenerate page/ink combination, and plotting at
    # the declared size (what "clip" does) is the safer answer than plotting
    # nothing at all.
    return scale if 0.0 < scale < 1.0 else 1.0


def apply_axis_skew(svg_path: Path, skew_deg: float, true_axis: str,
                     paper_width_mm: float, paper_height_mm: float,
                     scale: float = 1.0) -> None:
    """Wrap ``svg_path``'s existing root content in one more outer ``<g
    transform>``, in place, applying ``skew_matrix``. A no-op when
    ``skew_deg`` is approximately zero and there is no scaling to do — the
    default for the overwhelming majority of machines, and one that does not
    even open the file.

    Must only be called on an SVG freshly rendered by
    ``svg_utils.transform_to_paper`` — never on a resume SVG, which already
    carries this wrap from its original render, or double-applying would
    compound the correction on every pause/resume.
    """
    if abs(skew_deg) < 1e-9 and scale == 1.0:
        return
    tree = etree.parse(str(svg_path))
    root = tree.getroot()
    wrapper = etree.SubElement(root, f"{{{SVG_NS}}}g")
    wrapper.set("transform", skew_matrix(skew_deg, true_axis,
                                         paper_width_mm, paper_height_mm, scale))
    for child in list(root)[:-1]:
        wrapper.append(child)
    tree.write(str(svg_path), xml_declaration=True, encoding="utf-8")


def inverse_skew_point(x_mm: float, y_mm: float, skew_deg: float, true_axis: str,
                        paper_width_mm: float, paper_height_mm: float,
                        scale: float = 1.0) -> tuple[float, float]:
    """The exact algebraic inverse of ``skew_matrix``'s map: a motor-space
    position -> the pristine design position it was commanded from. Used to
    keep the live draw-stream showing the uncorrected path.

    Inverted from ``_coefficients`` rather than re-derived, so it cannot fall
    out of step with the forward transform.
    """
    a, b, c, d, e, f = _coefficients(skew_deg, true_axis,
                                     paper_width_mm, paper_height_mm, scale)
    det = a * d - b * c
    if det == 0:
        return x_mm, y_mm
    u, v = x_mm - e, y_mm - f
    return (d * u - c * v) / det, (-b * u + a * v) / det
