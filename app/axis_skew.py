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


def skew_matrix(skew_deg: float, true_axis: str,
                 pivot_x_mm: float, pivot_y_mm: float) -> str:
    """The SVG ``matrix(...)`` string mapping a physically correct point to
    the motor-space command that lands it there, pivoted at
    ``(pivot_x_mm, pivot_y_mm)`` so the correction splits across the pivot
    instead of piling onto one edge of the page.

    ``true_axis`` names the axis assumed to travel in a straight line; the
    other axis is the one being corrected relative to it.
    """
    tan_t = math.tan(math.radians(skew_deg))
    if true_axis == "y":
        a, b, c, d = 1.0, -tan_t, 0.0, 1.0
    else:
        a, b, c, d = 1.0, 0.0, -tan_t, 1.0
    cx, cy = pivot_x_mm, pivot_y_mm
    e = cx - a * cx - c * cy
    f = cy - b * cx - d * cy
    return f"matrix({a},{b},{c},{d},{e},{f})"


def skew_delta(dx_mm: float, dy_mm: float, skew_deg: float, true_axis: str) -> tuple[float, float]:
    """The motor-space ``(dx_mm, dy_mm)`` that produces a true physical
    displacement of ``(dx_mm, dy_mm)`` — the same correction ``skew_matrix``
    applies to plotted artwork, but for a relative move rather than an
    absolute point.

    No pivot: a displacement's correction is the linear part of
    ``skew_matrix`` alone, since the pivot/translation term cancels out of
    any difference between two points. Used by
    ``plot_worker._jog_carriage`` so that manual jogs, origin nudges, and
    Return to Origin all land where they were actually asked to, on a
    machine whose axes aren't perfectly perpendicular — the same physical
    correction plotted strokes already get.
    """
    if abs(skew_deg) < 1e-9:
        return dx_mm, dy_mm
    tan_t = math.tan(math.radians(skew_deg))
    if true_axis == "y":
        return dx_mm, dy_mm - dx_mm * tan_t
    return dx_mm - dy_mm * tan_t, dy_mm


def apply_axis_skew(svg_path: Path, skew_deg: float, true_axis: str) -> None:
    """Wrap ``svg_path``'s existing root content in one more outer ``<g
    transform>``, in place, applying the hardware axis-skew correction
    pivoted at the page's own origin ``(0, 0)`` — the plotter's declared
    physical home — so that corner never moves. A no-op when ``skew_deg`` is
    approximately zero, the default for the overwhelming majority of
    machines.

    Must only be called on an SVG freshly rendered by
    ``svg_utils.transform_to_paper`` — never on a resume SVG, which already
    carries this wrap from its original render, or double-applying would
    compound the correction on every pause/resume. If ``apply_skew_absorb``
    is also being applied, it must run first, so its wrap ends up inner to
    this one (the excursion this shears by is sized assuming the absorb
    squeeze already ran).
    """
    if abs(skew_deg) < 1e-9:
        return
    tree = etree.parse(str(svg_path))
    root = tree.getroot()
    matrix = skew_matrix(skew_deg, true_axis, 0.0, 0.0)
    wrapper = etree.SubElement(root, f"{{{SVG_NS}}}g")
    wrapper.set("transform", matrix)
    for child in list(root)[:-1]:
        wrapper.append(child)
    tree.write(str(svg_path), xml_declaration=True, encoding="utf-8")


def _absorb_scale_offset(skew_deg: float, true_axis: str,
                          paper_width_mm: float, paper_height_mm: float
                          ) -> tuple[float, float] | None:
    """The ``(scale, offset)`` a squeeze along the corrected axis needs to
    reserve exactly ``tan(|skew_deg|) * perpendicular_dimension`` on
    whichever single edge ``apply_axis_skew``'s ``(0, 0)``-pivoted shear
    will push content toward. ``None`` when ``skew_deg`` is approximately
    zero (nothing to absorb). Shared by ``skew_absorb_matrix`` (the forward
    map) and ``inverse_absorb_point`` (its exact algebraic inverse)."""
    if abs(skew_deg) < 1e-9:
        return None
    tan_t = math.tan(math.radians(skew_deg))
    if true_axis == "x":
        dim, span = paper_height_mm, paper_width_mm
    else:
        dim, span = paper_width_mm, paper_height_mm
    reserve = abs(tan_t) * dim
    scale = max(0.0, (span - reserve) / span) if span > 0 else 0.0
    offset = reserve if tan_t > 0 else 0.0
    return scale, offset


def skew_absorb_matrix(skew_deg: float, true_axis: str,
                        paper_width_mm: float, paper_height_mm: float) -> str | None:
    """The SVG ``matrix(...)`` string that reserves, on whichever single
    page edge ``apply_axis_skew``'s ``(0, 0)``-pivoted shear will push
    content toward, exactly the worst-case excursion
    ``tan(|skew_deg|) * perpendicular_dimension`` — compressing only the
    axis the shear moves, leaving the other axis and the near edge
    untouched. ``None`` when ``skew_deg`` is approximately zero (nothing to
    absorb).

    Composing this with ``skew_matrix`` at the same ``skew_deg``/
    ``true_axis`` keeps every corner of even full-bleed content inside
    ``[0, paper_width_mm] x [0, paper_height_mm]`` after the shear.
    """
    scale_offset = _absorb_scale_offset(skew_deg, true_axis, paper_width_mm, paper_height_mm)
    if scale_offset is None:
        return None
    scale, offset = scale_offset
    if true_axis == "x":
        return f"matrix({scale},0,0,1,{offset},0)"
    return f"matrix(1,0,0,{scale},0,{offset})"


def apply_skew_absorb(svg_path: Path, skew_deg: float, true_axis: str,
                       paper_width_mm: float, paper_height_mm: float) -> None:
    """Wrap ``svg_path``'s existing root content in one more outer ``<g
    transform>``, in place, applying ``skew_absorb_matrix``. A no-op when
    ``skew_deg`` is approximately zero. Must run before ``apply_axis_skew``
    when both apply, so its wrap ends up inner (squeezed first, then
    sheared) — the order the excursion math assumes."""
    matrix = skew_absorb_matrix(skew_deg, true_axis, paper_width_mm, paper_height_mm)
    if matrix is None:
        return
    tree = etree.parse(str(svg_path))
    root = tree.getroot()
    wrapper = etree.SubElement(root, f"{{{SVG_NS}}}g")
    wrapper.set("transform", matrix)
    for child in list(root)[:-1]:
        wrapper.append(child)
    tree.write(str(svg_path), xml_declaration=True, encoding="utf-8")


def inverse_skew_point(x_mm: float, y_mm: float, skew_deg: float, true_axis: str,
                        pivot_x_mm: float, pivot_y_mm: float) -> tuple[float, float]:
    """The exact algebraic inverse of ``skew_matrix``'s map: a motor-space
    position -> the pristine design position it was commanded from. Used to
    keep the live draw-stream showing the uncorrected path."""
    tan_t = math.tan(math.radians(skew_deg))
    mx, my = x_mm - pivot_x_mm, y_mm - pivot_y_mm
    if true_axis == "y":
        px, py = mx, my + mx * tan_t
    else:
        px, py = mx + my * tan_t, my
    return px + pivot_x_mm, py + pivot_y_mm


def inverse_absorb_point(x_mm: float, y_mm: float, skew_deg: float, true_axis: str,
                          paper_width_mm: float, paper_height_mm: float) -> tuple[float, float]:
    """The exact algebraic inverse of ``skew_absorb_matrix``'s map. Used
    alongside ``inverse_skew_point`` to keep the live draw-stream showing
    the pristine, pre-correction design position when ``absorb`` mode is
    active — apply this one second, since the squeeze it undoes was applied
    first (inner to the shear) by ``apply_skew_absorb``."""
    scale_offset = _absorb_scale_offset(skew_deg, true_axis, paper_width_mm, paper_height_mm)
    if scale_offset is None:
        return x_mm, y_mm
    scale, offset = scale_offset
    if scale <= 0:
        return x_mm, y_mm
    if true_axis == "x":
        return (x_mm - offset) / scale, y_mm
    return x_mm, (y_mm - offset) / scale
