"""Where ink lands on paper. The one answer, computed once.

This is deliberately pure — floats in, floats out, no lxml, no vpype, no file
I/O. Everything that needs to know where the artwork goes calls ``compute``
and reads the result: ``svg_utils.transform_to_paper`` renders it into a
``<g transform>``, ``svg_utils.ink_bounds_mm`` maps measured geometry through
it, and the web UI is handed the answer rather than deriving its own.

It exists because that last sentence used to be false. The same geometry was
worked out independently in three places — the SVG writer, the bounds check,
and the browser preview — and the copies drifted: the bounds guard and the
plot disagreed about whether out-of-canvas ink existed, and the estimate was
simulated against a different machine than the plot ran on. Each was a
separate bug with a separate fix, and nothing stopped a fourth copy appearing.
One implementation is what makes those structurally impossible instead of
individually corrected.

The placement rules it encodes:

- **The canvas is the composition.** A document's own width/height is a
  deliberate statement about size and margins, so "fit to page" scales the
  canvas, never the ink inside it. Fitting to ink would silently recompose
  artwork that was laid out with intentional empty space.
- **Anchor at the margin box's top-left.** The content's rotated top-left
  corner lands on the margin box's top-left corner, so a design's own (0, 0)
  meets the page's origin.
- **Auto-rotate turns the artwork with the page.** A machine profile that
  forces an orientation rotates the content to match, or it would just sit
  sideways in a swapped page.
- **`meet`, not stretch.** The viewBox-to-viewport mapping is SVG's default
  ``preserveAspectRatio="xMidYMid meet"``: one uniform scale, the smaller of
  the two axis ratios, centred.
"""
import math
from dataclasses import dataclass

# Two dimensions this close together are the same length as far as placement
# is concerned. Sized for unit-conversion rounding (a document that is square
# in its authoring tool can arrive as 200.0 x 199.98mm after a px->mm trip),
# not as a design tolerance — anything larger would start silently ignoring
# real, intended aspect differences.
SQUARE_EPSILON_MM = 0.05

AUTO_ROTATE_OFF = "off"


@dataclass(frozen=True)
class Placement:
    """A resolved placement: everything downstream needs, already worked out.

    Coordinates come in two flavours and the distinction matters. *Document
    mm* is a position within the source document's own physical page — what
    vpype reports after applying the viewBox mapping. *User units* are the
    viewBox's own coordinate space, which is what an SVG transform operates
    in. ``mm_scale`` and ``user_scale`` are the same scaling expressed in each.
    """

    rotation_deg: float          # auto-rotate + the job's own rotation
    fit_scale: float             # from "fit to page"; 1.0 when off
    mm_scale: float              # document mm -> page mm
    user_scale: float            # viewBox user units -> page mm
    footprint_w_mm: float        # the canvas's rotated, scaled on-page size
    footprint_h_mm: float
    center_x_mm: float           # where the document's centre lands on the page
    center_y_mm: float
    doc_center_x_mm: float       # the document's own centre, in document mm
    doc_center_y_mm: float
    vb_center_x: float           # the viewBox's own centre, in user units
    vb_center_y: float

    def group_transform(self) -> str:
        """The ``transform`` attribute for the group wrapping the source
        content, in the output SVG's mm coordinate space."""
        return (
            f"translate({self.center_x_mm},{self.center_y_mm}) "
            f"rotate({self.rotation_deg}) "
            f"scale({self.user_scale}) "
            f"translate({-self.vb_center_x},{-self.vb_center_y})"
        )

    def doc_mm_to_page(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        """Map a point from document mm onto the page, in mm."""
        rad = math.radians(self.rotation_deg)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        dx = (x_mm - self.doc_center_x_mm) * self.mm_scale
        dy = (y_mm - self.doc_center_y_mm) * self.mm_scale
        return (
            self.center_x_mm + dx * cos_r - dy * sin_r,
            self.center_y_mm + dx * sin_r + dy * cos_r,
        )

    def doc_mm_rect_to_page(self, xmin: float, ymin: float,
                            xmax: float, ymax: float) -> tuple[float, float, float, float]:
        """Map an axis-aligned rectangle from document mm onto the page,
        returning its on-page bounding box as (left, top, right, bottom).

        All four corners are mapped rather than just two: under rotation a
        rectangle's *size* is position-independent but its *position* is not,
        so mapping a corner pair reports the right dimensions in the wrong
        place.
        """
        corners = [self.doc_mm_to_page(x, y)
                   for x in (xmin, xmax) for y in (ymin, ymax)]
        xs = [c[0] for c in corners]
        ys = [c[1] for c in corners]
        return min(xs), min(ys), max(xs), max(ys)


def _auto_rotation_deg(paper_width_mm: float, paper_height_mm: float,
                       doc_width_mm: float, doc_height_mm: float,
                       machine_auto_rotate: str) -> float:
    """The extra rotation a machine's auto-rotate policy adds to the artwork.

    The policy forces the *page* into a fixed orientation (the caller has
    already swapped the paper dimensions); this turns the content with it, or
    it would sit undersized and sideways on the swapped page.

    Square content is exempt. It matches every orientation equally, so
    rotating it accomplishes nothing but moving the artwork — and a strict
    ``>`` comparison classes a square document as portrait, which used to earn
    it a pointless 90 degrees on any landscape page.
    """
    if machine_auto_rotate == AUTO_ROTATE_OFF:
        return 0.0
    if abs(doc_width_mm - doc_height_mm) <= SQUARE_EPSILON_MM:
        return 0.0
    page_landscape = paper_width_mm > paper_height_mm
    content_landscape = doc_width_mm > doc_height_mm
    return 90.0 if page_landscape != content_landscape else 0.0


def compute(
    doc_width_mm: float | None,
    doc_height_mm: float | None,
    viewbox: tuple[float, float, float, float] | None,
    paper_width_mm: float,
    paper_height_mm: float,
    margin_top_mm: float,
    margin_right_mm: float,
    margin_bottom_mm: float,
    margin_left_mm: float,
    fit_content: bool,
    transform_scale: float = 1.0,
    transform_rotation_deg: float = 0.0,
    transform_offset_x_mm: float = 0.0,
    transform_offset_y_mm: float = 0.0,
    machine_auto_rotate: str = AUTO_ROTATE_OFF,
) -> Placement:
    """Resolve a placement.

    ``doc_width_mm`` / ``doc_height_mm`` may be None for a document that
    states no resolvable size; the paper's own dimensions stand in, which is
    the same assumption as treating the sheet as the canvas. ``viewbox`` may
    be None, in which case user units are document millimetres.
    """
    doc_w = doc_width_mm or paper_width_mm
    doc_h = doc_height_mm or paper_height_mm

    if viewbox is not None:
        vb_x, vb_y, vb_w, vb_h = viewbox
    else:
        vb_x, vb_y, vb_w, vb_h = 0.0, 0.0, doc_w, doc_h

    rotation_deg = transform_rotation_deg + _auto_rotation_deg(
        paper_width_mm, paper_height_mm, doc_w, doc_h, machine_auto_rotate)

    # The canvas's bounding box once rotated, per unit of scale. Sizing
    # against the *rotated* box is what keeps "fit to page" inside the page at
    # any angle rather than only at 0 and 180 degrees.
    rad = math.radians(rotation_deg)
    cos_r, sin_r = abs(math.cos(rad)), abs(math.sin(rad))
    bbox_w_per_unit = doc_w * cos_r + doc_h * sin_r
    bbox_h_per_unit = doc_w * sin_r + doc_h * cos_r

    available_w = max(0.0, paper_width_mm - margin_left_mm - margin_right_mm)
    available_h = max(0.0, paper_height_mm - margin_top_mm - margin_bottom_mm)
    if (fit_content and bbox_w_per_unit > 0 and bbox_h_per_unit > 0
            and available_w > 0 and available_h > 0):
        fit_scale = min(available_w / bbox_w_per_unit, available_h / bbox_h_per_unit)
    else:
        fit_scale = 1.0

    mm_scale = fit_scale * transform_scale
    footprint_w_mm = bbox_w_per_unit * mm_scale
    footprint_h_mm = bbox_h_per_unit * mm_scale

    # Source user units -> page mm, via SVG's default "meet" mapping: the
    # smaller of the two axis ratios, applied uniformly. Taking the x ratio
    # alone stretches any document whose width/height aspect differs from its
    # viewBox aspect, and the browser applies `meet`, so the plot would come
    # out a size the preview never showed.
    if vb_w and vb_h:
        vb_to_mm = min(doc_w / vb_w, doc_h / vb_h)
    elif vb_w:
        vb_to_mm = doc_w / vb_w
    else:
        vb_to_mm = 1.0

    return Placement(
        rotation_deg=rotation_deg,
        fit_scale=fit_scale,
        mm_scale=mm_scale,
        user_scale=mm_scale * vb_to_mm,
        footprint_w_mm=footprint_w_mm,
        footprint_h_mm=footprint_h_mm,
        # Anchor the rotated footprint's top-left on the margin box's
        # top-left, expressed as where the document's centre has to land.
        center_x_mm=margin_left_mm + footprint_w_mm / 2 + transform_offset_x_mm,
        center_y_mm=margin_top_mm + footprint_h_mm / 2 + transform_offset_y_mm,
        doc_center_x_mm=doc_w / 2,
        doc_center_y_mm=doc_h / 2,
        vb_center_x=vb_x + vb_w / 2,
        vb_center_y=vb_y + vb_h / 2,
    )
