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


def apply_axis_skew(svg_path: Path, skew_deg: float, true_axis: str,
                     paper_width_mm: float, paper_height_mm: float) -> None:
    """Wrap ``svg_path``'s existing root content in one more outer ``<g
    transform>``, in place, applying the hardware axis-skew correction
    pivoted at the page's own center. A no-op when ``skew_deg`` is
    approximately zero, the default for the overwhelming majority of
    machines.

    Must only be called on an SVG freshly rendered by
    ``svg_utils.transform_to_paper`` — never on a resume SVG, which already
    carries this wrap from its original render, or double-applying would
    compound the correction on every pause/resume.
    """
    if abs(skew_deg) < 1e-9:
        return
    tree = etree.parse(str(svg_path))
    root = tree.getroot()
    matrix = skew_matrix(skew_deg, true_axis,
                          paper_width_mm / 2, paper_height_mm / 2)
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
