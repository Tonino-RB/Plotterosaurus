import math
import re

from lxml import etree
from pathlib import Path

SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
NS = {"svg": SVG_NS, "inkscape": INKSCAPE_NS}

LAYER_TAG = f"{{{SVG_NS}}}g"
GROUPMODE_ATTR = f"{{{INKSCAPE_NS}}}groupmode"
LABEL_ATTR = f"{{{INKSCAPE_NS}}}label"


def parse_dim_to_mm(s: str) -> float | None:
    """Parse an SVG length attribute into millimetres. Accepts mm, cm, in, px (or unitless = px)."""
    if not s:
        return None
    m = re.match(r"^\s*([\d.eE+\-]+)\s*([a-zA-Z%]*)\s*$", s)
    if not m:
        return None
    value = float(m.group(1))
    unit = (m.group(2) or "px").lower()
    if unit == "mm":
        return value
    if unit == "cm":
        return value * 10.0
    if unit == "in":
        return value * 25.4
    if unit == "px" or unit == "":
        return value * 25.4 / 96.0
    return None


def _top_level_layers(root):
    return [g for g in root if g.tag == LAYER_TAG and g.get(GROUPMODE_ATTR) == "layer"]


def svg_size_mm(root) -> tuple[float | None, float | None]:
    """Physical size in mm from the width/height attributes, falling back to
    the viewBox (treated as CSS px at 96dpi) when width/height are missing or
    use a non-physical unit like `%`. This mirrors how vpype resolves the same
    ambiguity, so a job's plotted size stays consistent whether or not
    "Optimize SVG" is enabled.
    """
    w = parse_dim_to_mm(root.get("width", ""))
    h = parse_dim_to_mm(root.get("height", ""))
    if w is None or h is None:
        vb = root.get("viewBox", "")
        parts = vb.split() if vb else []
        if len(parts) == 4:
            vb_w, vb_h = float(parts[2]), float(parts[3])
            if w is None and vb_w:
                w = vb_w * 25.4 / 96.0
            if h is None and vb_h:
                h = vb_h * 25.4 / 96.0
    return w, h


def parse_layers(svg_path: Path) -> dict:
    tree = etree.parse(str(svg_path))
    root = tree.getroot()
    layers = []
    for i, g in enumerate(_top_level_layers(root)):
        label = g.get(LABEL_ATTR) or f"Layer {i + 1}"
        layers.append(
            {
                "index": i,
                "label": label,
                "addressable": bool(label) and label[0].isdigit(),
            }
        )
    width_mm, height_mm = svg_size_mm(root)
    return {
        "layers": layers,
        "width": root.get("width", ""),
        "height": root.get("height", ""),
        "viewBox": root.get("viewBox", ""),
        "width_mm": width_mm,
        "height_mm": height_mm,
    }


def filter_to_layers(svg_path: Path, keep_indices: list[int], out_path: Path) -> None:
    tree = etree.parse(str(svg_path))
    root = tree.getroot()
    keep = set(keep_indices)
    for i, g in enumerate(_top_level_layers(root)):
        if i not in keep:
            g.getparent().remove(g)
    tree.write(str(out_path), xml_declaration=True, encoding="utf-8")


def transform_to_paper(
    svg_path: Path,
    out_path: Path,
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
    machine_custom_enabled: bool = False,
    machine_auto_rotate: str = "off",
) -> None:
    """Write a new SVG sized to the paper, with the source SVG's content wrapped in
    a <g transform="..."> that centers it within the margin box, optionally scales
    it to fit, and applies the user's scale/rotation/offset around the content center.

    The output SVG uses mm as its user-unit coordinate space (viewBox = 0 0 paper_w paper_h)
    so pyaxidraw renders it 1:1 on the plotter bed.
    """
    tree = etree.parse(str(svg_path))
    root = tree.getroot()

    orig_w_mm, orig_h_mm = svg_size_mm(root)
    orig_w_mm = orig_w_mm or paper_width_mm
    orig_h_mm = orig_h_mm or paper_height_mm

    vb = root.get("viewBox", "")
    if vb:
        parts = vb.split()
        vb_x, vb_y, vb_w, vb_h = (float(p) for p in parts[:4])
    else:
        vb_x, vb_y = 0.0, 0.0
        vb_w, vb_h = orig_w_mm, orig_h_mm

    # A custom machine bed with auto-rotate forces the paper into a fixed
    # orientation (see caller); the artwork must turn with it too, or it just
    # sits undersized/sideways on the swapped page. Add 90 deg whenever the
    # content's own natural orientation doesn't match the paper's — mirrors
    # the same decision made client-side for the preview (app.js).
    auto_rotate_deg = 0.0
    if machine_custom_enabled and machine_auto_rotate != "off":
        page_landscape = paper_width_mm > paper_height_mm
        content_landscape = orig_w_mm > orig_h_mm
        if page_landscape != content_landscape:
            auto_rotate_deg = 90.0
    total_rotation_deg = transform_rotation_deg + auto_rotate_deg

    available_w = max(0.0, paper_width_mm - margin_left_mm - margin_right_mm)
    available_h = max(0.0, paper_height_mm - margin_top_mm - margin_bottom_mm)

    # fit_content sizes content against its *rotated* bounding box (at the
    # combined auto + manual rotation), so "Fit to page" keeps the content
    # within the page at any rotation angle instead of only the unrotated one.
    rot_rad = math.radians(total_rotation_deg)
    cos_r, sin_r = abs(math.cos(rot_rad)), abs(math.sin(rot_rad))
    bbox_w_per_unit = orig_w_mm * cos_r + orig_h_mm * sin_r
    bbox_h_per_unit = orig_w_mm * sin_r + orig_h_mm * cos_r
    if fit_content and bbox_w_per_unit > 0 and bbox_h_per_unit > 0 and available_w > 0 and available_h > 0:
        fit_scale = min(available_w / bbox_w_per_unit, available_h / bbox_h_per_unit)
    else:
        fit_scale = 1.0

    total_mm_scale = fit_scale * transform_scale
    # Source user units -> paper mm.
    user_scale = total_mm_scale * (orig_w_mm / vb_w) if vb_w else total_mm_scale

    # Rotate/scale the content around its own center; that center lands at
    # (center_x_mm, center_y_mm) on the paper, shifted by the user's offset.
    # Anchor the content's own *rotated* top-left corner (at its rendered,
    # fit_scale'd size) to the margin box's top-left corner rather than
    # centering it — so a design's own (0,0) lines up with the page's origin
    # by default, whether or not "Fit to page" scaled it down. Using the
    # rotated bbox (bbox_w_per_unit/bbox_h_per_unit) instead of the raw
    # orig_w_mm/orig_h_mm matters once total_rotation_deg != 0/180: for
    # non-square content the rotated footprint is a different size than the
    # unrotated one, so anchoring off the unrotated size drifts the content
    # off the page edge. Mirrors offX/offY/cX/cY in updatePreviewTransform()
    # (app.js).
    center_x_mm = margin_left_mm + (bbox_w_per_unit * total_mm_scale) / 2 + transform_offset_x_mm
    center_y_mm = margin_top_mm + (bbox_h_per_unit * total_mm_scale) / 2 + transform_offset_y_mm
    vb_center_x = vb_x + vb_w / 2
    vb_center_y = vb_y + vb_h / 2

    nsmap = {k: v for k, v in root.nsmap.items() if k}
    nsmap[None] = SVG_NS
    new_root = etree.Element(f"{{{SVG_NS}}}svg", nsmap=nsmap)
    new_root.set("width", f"{paper_width_mm}mm")
    new_root.set("height", f"{paper_height_mm}mm")
    new_root.set("viewBox", f"0 0 {paper_width_mm} {paper_height_mm}")

    group = etree.SubElement(new_root, f"{{{SVG_NS}}}g")
    group.set(
        "transform",
        f"translate({center_x_mm},{center_y_mm}) "
        f"rotate({total_rotation_deg}) "
        f"scale({user_scale}) "
        f"translate({-vb_center_x},{-vb_center_y})",
    )
    for child in list(root):
        group.append(child)

    etree.ElementTree(new_root).write(str(out_path), xml_declaration=True, encoding="utf-8")
