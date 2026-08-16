import itertools
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

# Label given to the layer that collects content found outside any layer.
LAYERLESS_LABEL = "Layerless elements"


PX_PER_MM = 96.0 / 25.4

# Absolute CSS length units, as millimetres per unit. Relative units (%, em,
# ex, ch) aren't resolvable without a rendering context, so they return None
# and the caller falls back to the viewBox.
_UNIT_TO_MM: dict[str, float] = {
    "mm": 1.0,
    "cm": 10.0,
    "in": 25.4,
    "px": 25.4 / 96.0,
    "": 25.4 / 96.0,   # unitless == px
    "pt": 25.4 / 72.0,
    "pc": 25.4 / 6.0,
    "q": 0.25,
}


def parse_dim_to_mm(s: str) -> float | None:
    """Parse an SVG length attribute into millimetres. Accepts mm, cm, in, pt,
    pc, Q, px (or unitless = px)."""
    if not s:
        return None
    m = re.match(r"^\s*([\d.eE+\-]+)\s*([a-zA-Z%]*)\s*$", s)
    if not m:
        return None
    value = float(m.group(1))
    unit = (m.group(2) or "px").lower()
    factor = _UNIT_TO_MM.get(unit)
    return value * factor if factor is not None else None


_VIEWBOX_SEP_RE = re.compile(r"[\s,]+")


def parse_viewbox(s: str) -> tuple[float, float, float, float] | None:
    """Parse a viewBox attribute into (min_x, min_y, width, height), or None
    if it's missing or unusable.

    SVG separates the four numbers with comma-wsp, so "0,0,595,842" is as
    valid as "0 0 595 842" and both turn up in real exports. Splitting on
    whitespace alone leaves the commas glued to the numbers, where the
    comma-separated form parsed as a size of None (svg_size_mm) and raised
    ValueError outright (transform_to_paper). Both call sites go through here
    so they can't disagree about the same attribute again.
    """
    if not s:
        return None
    parts = [p for p in _VIEWBOX_SEP_RE.split(s.strip()) if p]
    if len(parts) != 4:
        return None
    try:
        x, y, w, h = (float(p) for p in parts)
    except ValueError:
        return None
    return x, y, w, h


def _top_level_layers(root):
    return [g for g in root if g.tag == LAYER_TAG and g.get(GROUPMODE_ATTR) == "layer"]


# Top-level elements that paint something. Anything else at the top level
# (defs, style, metadata, title, sodipodi:namedview, ...) is structural and
# must survive filtering untouched or the kept layers stop rendering.
_DRAWABLE_TAGS = {
    f"{{{SVG_NS}}}{name}" for name in (
        "path", "line", "polyline", "polygon", "circle", "ellipse", "rect",
        "text", "image", "use", "g", "svg", "switch", "foreignObject",
    )
}


def _top_level_orphans(root):
    """Drawable top-level elements that sit outside any Inkscape layer.

    Inkscape always wraps content in layers, but plenty of other producers
    don't. This content has no layer index, so it can't be selected or
    deselected — and without special handling it would be copied into *every*
    per-layer stage and drawn once per stage (see filter_to_layers)."""
    return [el for el in root
            if el.tag in _DRAWABLE_TAGS
            and not (el.tag == LAYER_TAG and el.get(GROUPMODE_ATTR) == "layer")]


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
        vb = parse_viewbox(root.get("viewBox", ""))
        if vb is not None:
            _, _, vb_w, vb_h = vb
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
        layers.append({"index": i, "label": g.get(LABEL_ATTR) or f"Layer {i + 1}"})
    width_mm, height_mm = svg_size_mm(root)
    return {
        "layers": layers,
        "width": root.get("width", ""),
        "height": root.get("height", ""),
        "viewBox": root.get("viewBox", ""),
        "width_mm": width_mm,
        "height_mm": height_mm,
        "has_orphan_content": bool(_top_level_orphans(root)),
    }


_DIGIT_GROUP_RE = re.compile(r"\d+")


def _vpype_layer_id(label: str, elem_id: str, order: int) -> int:
    """The layer id vpype will give a top-level group, replicated from
    ``vpype.io.read_multilayer_svg``: the first contiguous group of digits in
    the ``inkscape:label``, else in the ``id``, else the group's 1-based
    appearance order. An id of 0 is bumped to 1."""
    m = _DIGIT_GROUP_RE.search(label) or _DIGIT_GROUP_RE.search(elem_id)
    if m is None:
        return order
    return int(m.group()) or 1


def normalize_layer_structure(svg_path: Path) -> bool:
    """Rewrite ``svg_path`` in place so every drawable top-level element is
    addressable as a layer and no two layers collapse into one inside vpype.
    Returns True if anything was changed.

    Fixes two failure modes, both of them silent before this ran:

    - Content outside any layer. Only ``inkscape:groupmode="layer"`` groups
      count as layers, so a loose <path>, or the plain <g> groups written by
      producers other than Inkscape, had no layer index at all: not listable,
      not selectable, no pen or speed of its own, just force-fed into the
      first stage (see _top_level_orphans and filter_to_layers). Plain groups
      are promoted in place — they are layers in every sense but the attribute
      their producer didn't write — and whatever is left over is collected
      into one LAYERLESS_LABEL layer at the position it was found.

    - Labels that map to the same vpype layer id. vpype takes that id from the
      first digit group of the label (see _vpype_layer_id), so "Contours"
      (no digits, so its position: 1) and "Layer 1" both resolve to 1 and get
      merged into a single layer by "Optimize SVG". reconcile_layers restores
      the layer *count* afterwards, but by then the geometry has been folded
      into the wrong layer: one plots nothing, the other plots twice. Colliding
      layers get a free id prefixed onto their label, which wins because vpype
      reads the first digit group.
    """
    tree = etree.parse(str(svg_path))
    root = tree.getroot()
    changed = False

    for pos, el in enumerate([e for e in root if e.tag == LAYER_TAG], start=1):
        if el.get(GROUPMODE_ATTR) == "layer":
            continue
        el.set(GROUPMODE_ATTR, "layer")
        if not el.get(LABEL_ATTR):
            el.set(LABEL_ATTR, el.get("id") or f"Layer {pos}")
        changed = True

    orphans = _top_level_orphans(root)
    if orphans:
        wrapper = etree.Element(LAYER_TAG)
        wrapper.set(GROUPMODE_ATTR, "layer")
        wrapper.set(LABEL_ATTR, LAYERLESS_LABEL)
        root.insert(root.index(orphans[0]), wrapper)
        for el in orphans:
            wrapper.append(el)
        changed = True

    # Resolve id collisions in two passes: every layer that claims an id first
    # keeps it, so the replacement ids handed out below can't collide with a
    # later layer's own id.
    layers = _top_level_layers(root)
    ids = [_vpype_layer_id(g.get(LABEL_ATTR) or "", g.get("id") or "", order)
           for order, g in enumerate(layers, start=1)]
    taken: set[int] = set()
    clashing = []
    for g, lid in zip(layers, ids):
        if lid in taken:
            clashing.append(g)
        else:
            taken.add(lid)
    for g in clashing:
        free = next(i for i in itertools.count(1) if i not in taken)
        taken.add(free)
        g.set(LABEL_ATTR, f"{free} {g.get(LABEL_ATTR) or ''}".strip())
        changed = True

    if changed:
        tree.write(str(svg_path), xml_declaration=True, encoding="utf-8")
    return changed


def filter_to_layers(svg_path: Path, keep_indices: list[int], out_path: Path,
                     include_orphans: bool = True) -> None:
    """Write ``out_path`` containing only the layers in ``keep_indices``.

    ``include_orphans`` controls drawable top-level content that belongs to no
    layer (see _top_level_orphans). A job is plotted as one stage per layer,
    and each stage re-renders from the same source, so that content has to be
    included in exactly one stage — pass True for the first stage and False
    for the rest, or it gets drawn once per stage.
    """
    tree = etree.parse(str(svg_path))
    root = tree.getroot()
    keep = set(keep_indices)
    for i, g in enumerate(_top_level_layers(root)):
        if i not in keep:
            g.getparent().remove(g)
    if not include_orphans:
        for el in _top_level_orphans(root):
            el.getparent().remove(el)
    tree.write(str(out_path), xml_declaration=True, encoding="utf-8")


def reconcile_layers(reference_path: Path, target_path: Path) -> None:
    """Re-align ``target_path``'s layer sequence with ``reference_path``'s.

    vpype drops any layer it found nothing plottable in (a text-only layer, an
    image, an empty group), so the optimized SVG can have fewer layers than
    the upload it came from. It also writes the layers it kept in layer-id
    order (see _vpype_layer_id), which is not necessarily the order they
    appeared in. Layers are addressed by position everywhere downstream (see
    filter_to_layers), and those positions come from the upload — so a dropped
    or reordered layer silently shifts the others and the wrong artwork gets
    plotted.

    Rather than teach every caller about two numbering schemes, restore the
    original sequence in place: walk the reference's labels in order, matching
    each to the next same-labelled layer in the target, and splice in an empty
    placeholder wherever the target has none. Target layers that match nothing
    (e.g. content vpype folded into a layer of its own) keep their relative
    order at the end. Rewrites ``target_path`` only if the sequence it already
    had differs from the one that was rebuilt.
    """
    ref_root = etree.parse(str(reference_path)).getroot()
    tree = etree.parse(str(target_path))
    root = tree.getroot()

    ref_labels = [g.get(LABEL_ATTR) or f"Layer {i + 1}"
                  for i, g in enumerate(_top_level_layers(ref_root))]
    target_layers = _top_level_layers(root)
    remaining = list(target_layers)

    ordered: list = []
    for label in ref_labels:
        match = next((g for g in remaining
                      if (g.get(LABEL_ATTR) or "") == label), None)
        if match is not None:
            remaining.remove(match)
            ordered.append(match)
        else:
            placeholder = etree.Element(LAYER_TAG)
            placeholder.set(GROUPMODE_ATTR, "layer")
            placeholder.set(LABEL_ATTR, label)
            ordered.append(placeholder)
    ordered.extend(remaining)

    if ordered == target_layers:
        return
    # Re-attach in the reference's order, starting where the layers already
    # were, so the structural siblings (defs/style/namedview) stay put.
    at = root.index(target_layers[0]) if target_layers else len(root)
    for g in target_layers:
        root.remove(g)
    for offset, g in enumerate(ordered):
        root.insert(at + offset, g)
    tree.write(str(target_path), xml_declaration=True, encoding="utf-8")


def _content_footprint_mm(
    root,
    paper_width_mm: float,
    paper_height_mm: float,
    margin_top_mm: float,
    margin_right_mm: float,
    margin_bottom_mm: float,
    margin_left_mm: float,
    fit_content: bool,
    transform_scale: float,
    transform_rotation_deg: float,
    machine_custom_enabled: bool,
    machine_auto_rotate: str,
) -> tuple[float, float, float, float]:
    """Sizing math shared by transform_to_paper and ink_bounds_mm: the
    content's on-page footprint (its rotated bounding box, at fit_content's
    scale if enabled). Returns (footprint_w_mm, footprint_h_mm,
    total_rotation_deg, total_mm_scale)."""
    orig_w_mm, orig_h_mm = svg_size_mm(root)
    orig_w_mm = orig_w_mm or paper_width_mm
    orig_h_mm = orig_h_mm or paper_height_mm

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
    return bbox_w_per_unit * total_mm_scale, bbox_h_per_unit * total_mm_scale, total_rotation_deg, total_mm_scale


def ink_bounds_mm(
    svg_path: Path,
    layer_indices: list[int],
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
) -> tuple[float, float, float, float] | None:
    """Where the *actual drawn geometry* of the given layers (not the SVG's
    document canvas) would land on the page under transform_to_paper's
    placement rules, in mm: (left, top, right, bottom). None if there's no
    geometry to measure (no layers selected, or they're empty of drawable
    content).

    fit_content's *scale factor* is still driven by the canvas size, exactly
    like transform_to_paper itself — only the reported extent switches to
    the ink's true bounding box. That box is measured with vpype (already a
    dependency — see svg_optimize.py) on a temporary file filtered to just
    the given layers, then mapped through the same rotate/scale-about-centre
    that transform_to_paper's <g transform=...> applies, corner by corner, so
    a rotated design's true footprint (not just its unrotated one) is reported
    correctly regardless of where the ink sits within its own canvas.

    vpype reports geometry in CSS pixels of the *physical* document (it has
    already applied the viewBox-to-viewport mapping), not in the SVG's own
    user units — so the only conversion needed here is px to mm, and the
    result is a position within the orig_w_mm x orig_h_mm document. Mixing
    those pixels with user-unit maths inflates every measurement by 96/25.4.
    """
    import os
    import tempfile

    import vpype

    tree = etree.parse(str(svg_path))
    root = tree.getroot()
    footprint_w_mm, footprint_h_mm, total_rotation_deg, total_mm_scale = _content_footprint_mm(
        root, paper_width_mm, paper_height_mm,
        margin_top_mm, margin_right_mm, margin_bottom_mm, margin_left_mm,
        fit_content, transform_scale, transform_rotation_deg,
        machine_custom_enabled, machine_auto_rotate,
    )
    orig_w_mm, orig_h_mm = svg_size_mm(root)
    orig_w_mm = orig_w_mm or paper_width_mm
    orig_h_mm = orig_h_mm or paper_height_mm
    # transform_to_paper maps the document's own centre onto this point.
    doc_center_x_mm, doc_center_y_mm = orig_w_mm / 2, orig_h_mm / 2
    center_x_mm = margin_left_mm + footprint_w_mm / 2 + transform_offset_x_mm
    center_y_mm = margin_top_mm + footprint_h_mm / 2 + transform_offset_y_mm

    fd, tmp_name = tempfile.mkstemp(dir=svg_path.parent, suffix=".svg")
    tmp = Path(tmp_name)
    os.close(fd)
    try:
        filter_to_layers(svg_path, layer_indices, tmp)
        bounds = vpype.read_multilayer_svg(str(tmp), quantization=0.1).bounds()
    finally:
        tmp.unlink(missing_ok=True)
    if bounds is None:
        return None
    xmin, ymin, xmax, ymax = (v / PX_PER_MM for v in bounds)

    rad = math.radians(total_rotation_deg)
    cos_r, sin_r = math.cos(rad), math.sin(rad)

    def to_page(x_mm: float, y_mm: float) -> tuple[float, float]:
        dx = (x_mm - doc_center_x_mm) * total_mm_scale
        dy = (y_mm - doc_center_y_mm) * total_mm_scale
        return (
            center_x_mm + dx * cos_r - dy * sin_r,
            center_y_mm + dx * sin_r + dy * cos_r,
        )

    corners = [to_page(x, y) for x in (xmin, xmax) for y in (ymin, ymax)]
    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
    return min(xs), min(ys), max(xs), max(ys)


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

    vb = parse_viewbox(root.get("viewBox", ""))
    if vb is not None:
        vb_x, vb_y, vb_w, vb_h = vb
    else:
        # No viewBox, or one that doesn't hold four numbers. Falling back to
        # the document's own size treats user units as mm, which is the same
        # assumption the no-viewBox case already made.
        vb_x, vb_y = 0.0, 0.0
        vb_w, vb_h = orig_w_mm, orig_h_mm

    footprint_w_mm, footprint_h_mm, total_rotation_deg, total_mm_scale = _content_footprint_mm(
        root, paper_width_mm, paper_height_mm,
        margin_top_mm, margin_right_mm, margin_bottom_mm, margin_left_mm,
        fit_content, transform_scale, transform_rotation_deg,
        machine_custom_enabled, machine_auto_rotate,
    )
    # Source user units -> paper mm. The viewBox-to-viewport mapping is SVG's
    # default preserveAspectRatio="xMidYMid meet": one uniform scale, the
    # smaller of the two axis ratios, with the result centred in the viewport.
    # Taking the x ratio alone stretches any document whose width/height
    # aspect differs from its viewBox aspect — the browser preview applies
    # `meet`, so the plot would come out a different size than it showed.
    # Centring is already handled below: the viewBox centre is translated onto
    # the viewport (document) centre.
    if vb_w and vb_h:
        vb_to_mm = min(orig_w_mm / vb_w, orig_h_mm / vb_h)
    elif vb_w:
        vb_to_mm = orig_w_mm / vb_w
    else:
        vb_to_mm = 1.0
    user_scale = total_mm_scale * vb_to_mm

    # Rotate/scale the content around its own center; that center lands at
    # (center_x_mm, center_y_mm) on the paper, shifted by the user's offset.
    # Anchor the content's own *rotated* top-left corner (at its rendered,
    # fit_scale'd size) to the margin box's top-left corner rather than
    # centering it — so a design's own (0,0) lines up with the page's origin
    # by default, whether or not "Fit to page" scaled it down. Using the
    # rotated footprint instead of the raw orig_w_mm/orig_h_mm matters once
    # total_rotation_deg != 0/180: for non-square content the rotated
    # footprint is a different size than the unrotated one, so anchoring off
    # the unrotated size drifts the content off the page edge. Mirrors
    # offX/offY/cX/cY in updatePreviewTransform() (app.js).
    center_x_mm = margin_left_mm + footprint_w_mm / 2 + transform_offset_x_mm
    center_y_mm = margin_top_mm + footprint_h_mm / 2 + transform_offset_y_mm
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
