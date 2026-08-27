import itertools
import re

from lxml import etree
from pathlib import Path

from . import placement

SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
NS = {"svg": SVG_NS, "inkscape": INKSCAPE_NS}

LAYER_TAG = f"{{{SVG_NS}}}g"
GROUPMODE_ATTR = f"{{{INKSCAPE_NS}}}groupmode"
LABEL_ATTR = f"{{{INKSCAPE_NS}}}label"

# Label given to the layer that collects content found outside any layer.
LAYERLESS_LABEL = "Layerless elements"


PX_PER_MM = 96.0 / 25.4

# How finely vpype flattens curves when all we want is a bounding box.
#
# This is a measurement-only setting and it cannot reach the plot: the machine
# is driven by pyaxidraw reading the SVG itself (plot_worker._run_stage), which
# does its own flattening, and the one vpype pass that *does* affect what gets
# drawn — the optional Optimize SVG step — is a separate CLI invocation with
# its own tolerance that this does not touch.
#
# It matters enormously for curves. A 2.36MB drawing of cubic beziers flattened
# at 0.1 expands to 102 million points and peaks at 2.69GB of RSS, which on a
# 3.7GB Pi already running a browser is a crash rather than a slow measurement.
# At 1.0 the same file takes 380MB and 23s instead of 2.69GB and 55s.
#
# And it costs nothing, because vpype resolves shape extremes analytically
# rather than reading them off the flattened samples: measured across 0.01 to
# 10.0 on circles, arcs and beziers, the reported bounds are bit-identical.
# Polyline documents are unaffected either way — they arrive already flat.
BOUNDS_QUANTIZATION = 1.0

# How many separate strokes a document holds, for reporting only.
#
# This deliberately gates nothing. The obvious idea -- refuse anything past N
# subpaths -- was tried and measured wrong: a real 17,110-subpath hatched
# drawing previews in 113MB and 11.5s, while a 10,786-subpath fragment of a
# dense generative one takes 810MB and 144s. Point counts invert the same way
# (215,200 against ~80,000). No cheap property of the document orders these
# two correctly, so any threshold drawn here refuses drawings that plot fine.
#
# The real bound is measured instead, by watching the preview subprocess and
# killing it if it grows past plot_worker.PREVIEW_RSS_LIMIT_MB. What the count
# below is for is telling the user what they are looking at, and giving
# svg_complexity something to describe.
_COUNTED_AS_ONE = frozenset(
    f"{{{SVG_NS}}}{t}" for t in ("polyline", "polygon", "line", "rect", "circle", "ellipse")
)
_PATH_TAG = f"{{{SVG_NS}}}path"


def count_subpaths(root) -> int:
    """How many separate pen-down strokes this document holds.

    A subpath, not an element: the drawings that break this machine arrive as a
    few hundred <path> elements carrying six figures of moveto commands between
    them, so element counts say nothing useful. Every M/m starts a new stroke,
    and in path data letters only ever appear as commands, so counting the two
    characters is exact without tokenizing 7MB of coordinates.
    """
    total = 0
    for el in root.iter():
        tag = el.tag
        if not isinstance(tag, str):
            continue  # comments and PIs
        if tag == _PATH_TAG:
            d = el.get("d") or ""
            total += d.count("M") + d.count("m")
        elif tag in _COUNTED_AS_ONE:
            total += 1
    return total

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
        # Rides along with the parse the caller already paid for; see
        # count_subpaths for why this number gates estimating and plotting.
        "subpath_count": count_subpaths(root),
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


def decollide_layer_labels(root) -> bool:
    """Prefix a free integer onto any top-level layer whose label would resolve
    to the same vpype layer id as an earlier one (see _vpype_layer_id).
    Mutates ``root`` in place; returns True if any label was rewritten.

    vpype takes that id from the first digit group of the label, so "Contours"
    (no digits, so its position: 1) and "Layer 1" both resolve to 1 and get
    merged into a single layer by "Optimize SVG". reconcile_layers restores the
    layer *count* afterwards, but by then the geometry has been folded into the
    wrong layer: one plots nothing, the other plots twice. The prefixed id wins
    because vpype reads the first digit group.
    """
    # Two passes: every layer that claims an id first keeps it, so the
    # replacement ids handed out below can't collide with a later layer's own.
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
    return bool(clashing)


def normalize_layer_root(root) -> bool:
    """Make ``root`` usable as a layered document: promote plain top-level <g>
    to Inkscape layers, gather loose drawable content into one LAYERLESS_LABEL
    layer, de-collide labels. Mutates ``root`` in place; returns True if
    anything changed. See normalize_layer_structure for the why.
    """
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

    if decollide_layer_labels(root):
        changed = True
    return changed


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

    - Labels that map to the same vpype layer id (see decollide_layer_labels).
    """
    tree = etree.parse(str(svg_path))
    root = tree.getroot()
    changed = normalize_layer_root(root)
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


def rewind_resume_distance(resume_path: Path, distance_mm: float) -> float:
    """Move a resume SVG's plot point back by ``distance_mm`` of pen-down travel.

    A paused plot's resume SVG carries a ``<plotdata>`` element whose
    ``pause_dist`` attribute is the pen-down travel completed at the pause, an
    integer in µm (see axidrawinternal.plot_status.ResumeStatus). ``res_plot``
    replays from that distance to the end. Subtracting from it makes the replay
    start earlier, so the last ``distance_mm`` of drawing is traced again before
    the plot carries on — the recovery move for a skipped line or a pen that ran
    dry mid-stroke.

    Clamped at 0 (never negative: a negative ``pause_dist`` is the "no resume
    data" flag and would stop res_plot from seeding the pen position). At 0,
    res_plot's crop keeps the whole current stage — the honest result when the
    ask reaches past the start of the layer being plotted.

    Only ``pause_dist`` is touched; ``pause_ref`` (the record of the original
    pause) and ``last_x``/``last_y`` (the physical pen position, which res_plot
    travels from) are left alone. Returns the mm actually removed.
    """
    tree = etree.parse(str(resume_path))
    node = tree.xpath("//*[local-name()='plotdata']")[0]
    old_um = int(node.get("pause_dist"))
    new_um = max(0, old_um - round(distance_mm * 1000))
    node.set("pause_dist", str(new_um))
    tree.write(str(resume_path), xml_declaration=True, encoding="utf-8")
    return (old_um - new_um) / 1000


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


def _placement_for(
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
    transform_offset_x_mm: float,
    transform_offset_y_mm: float,
    machine_auto_rotate: str,
) -> placement.Placement:
    """Read the document's own geometry off ``root`` and resolve a placement.

    The only thing this adds over ``placement.compute`` is pulling the size
    and viewBox out of the XML — the placement engine is kept free of lxml so
    it stays a pure function that can be reasoned about and tested on its own.
    """
    doc_w_mm, doc_h_mm = svg_size_mm(root)
    return placement.compute(
        doc_w_mm, doc_h_mm, parse_viewbox(root.get("viewBox", "")),
        paper_width_mm, paper_height_mm,
        margin_top_mm, margin_right_mm, margin_bottom_mm, margin_left_mm,
        fit_content,
        transform_scale=transform_scale,
        transform_rotation_deg=transform_rotation_deg,
        transform_offset_x_mm=transform_offset_x_mm,
        transform_offset_y_mm=transform_offset_y_mm,
        machine_auto_rotate=machine_auto_rotate,
    )


def ink_rect_doc_mm(svg_path: Path,
                    layer_indices: list[int]) -> tuple[float, float, float, float] | None:
    """The drawn geometry's bounding box in *document* millimetres, as
    (xmin, ymin, xmax, ymax). None when the given layers hold nothing
    plottable.

    Deliberately independent of placement. Where the artwork ends up on paper
    depends on paper size, margins, fit, rotation, scale and offset — all of
    which change while a user drags a slider — but *this* depends only on the
    document and which layers are selected. Separating them is what lets the
    expensive half (a vpype parse) happen once, off the request path, while the
    page mapping stays cheap arithmetic (see Placement.doc_mm_rect_to_page).

    Prefer ``ink_rects_by_layer`` for anything that measures more than one
    selection of the same file: this reads and writes a filtered copy of the
    document per call, where that reads every layer in one pass. What remains
    here is ``ink_bounds_mm``, which answers a single one-off question, and the
    tests that use it as an independent second opinion on the cached path.

    vpype reports geometry in CSS pixels of the physical document — it has
    already applied the viewBox-to-viewport mapping — so the only conversion
    needed is px to mm. Mixing those pixels with user-unit maths inflates
    every measurement by 96/25.4.
    """
    import os
    import tempfile

    import vpype

    fd, tmp_name = tempfile.mkstemp(dir=svg_path.parent, suffix=".svg")
    tmp = Path(tmp_name)
    os.close(fd)
    try:
        filter_to_layers(svg_path, layer_indices, tmp)
        bounds = vpype.read_multilayer_svg(str(tmp), quantization=BOUNDS_QUANTIZATION).bounds()
    finally:
        tmp.unlink(missing_ok=True)
    if bounds is None:
        return None
    xmin, ymin, xmax, ymax = (v / PX_PER_MM for v in bounds)
    return xmin, ymin, xmax, ymax


def measure_layers(svg_path: Path) -> dict[int, dict]:
    """Every layer's ink bounding box and pen-down path length, in document mm,
    keyed by layer index — one vpype read of the whole document, versus one per
    layer combination the user might select. That difference is the whole
    point: on an 8MB drawing a single read costs 15-75 seconds, and asking for
    a fresh one per layer toggle — with the UI re-requesting on every state
    broadcast while an answer is outstanding — is how several piled up at once
    and took four cores of Raspberry Pi with them.

    Any selection's rectangle is the union of its layers', and its pen-down
    distance is their sum, so measuring each layer once answers every question
    that can be asked about the file. Layers vpype found nothing plottable in
    are absent rather than present-and-empty.

    No temp file either. Writing a filtered copy of the document first would
    mean multiple megabytes onto an SD card per call, orphaned entirely if the
    service restarts mid-parse.
    """
    import vpype

    document = vpype.read_multilayer_svg(str(svg_path), quantization=BOUNDS_QUANTIZATION)
    # vpype numbers layers by its own rule, not by document order; replicate it
    # to get back to the indices everything else addresses layers by.
    root = etree.parse(str(svg_path)).getroot()
    result: dict[int, dict] = {}
    for index, group in enumerate(_top_level_layers(root)):
        vpype_id = _vpype_layer_id(group.get(LABEL_ATTR) or "",
                                   group.get("id") or "", index + 1)
        layer = document.layers.get(vpype_id)
        if layer is None:
            continue
        bounds = layer.bounds()
        result[index] = {
            "rect": tuple(v / PX_PER_MM for v in bounds) if bounds is not None else None,
            "length_mm": layer.length() / PX_PER_MM,
        }
    return result


def ink_rects_by_layer(svg_path: Path) -> dict[int, tuple[float, float, float, float]]:
    """Every layer's ink bounding box in document mm, keyed by layer index.
    See ``measure_layers`` — this is the bounds half of that one read."""
    return {i: v["rect"] for i, v in measure_layers(svg_path).items() if v["rect"] is not None}


def union_rect(rects) -> tuple[float, float, float, float] | None:
    """The bounding box of several (xmin, ymin, xmax, ymax) boxes, or None."""
    rects = [r for r in rects if r is not None]
    if not rects:
        return None
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


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
    machine_auto_rotate: str = placement.AUTO_ROTATE_OFF,
    rect: tuple[float, float, float, float] | None = None,
    rect_known: bool = False,
) -> tuple[float, float, float, float] | None:
    """Where the *actual drawn geometry* of the given layers lands on the
    page, in mm: (left, top, right, bottom). None if there is nothing
    drawable to measure.

    This differs from the placement of the document itself only in what gets
    mapped: the canvas still drives the scale (see placement.compute), but the
    reported extent is the ink's true bounding box, which for most designs
    sits well inside the canvas.

    vpype reports geometry in CSS pixels of the *physical* document — it has
    already applied the viewBox-to-viewport mapping — so the only conversion
    needed is px to mm, and the result is a position in document millimetres,
    which is exactly what Placement.doc_mm_rect_to_page consumes. Mixing those
    pixels with user-unit maths inflates every measurement by 96/25.4.

    Pass ``rect`` with ``rect_known=True`` to reuse an already-measured
    doc-mm rect (e.g. from ink_cache) instead of paying for another vpype
    parse here — a caller on a hot path (like a mid-plot nudge check) should
    prefer that over calling this on every request. ``rect_known=False``
    (the default) always measures fresh, same as before this parameter
    existed.
    """
    if not rect_known:
        rect = ink_rect_doc_mm(svg_path, layer_indices)
    if rect is None:
        return None
    root = etree.parse(str(svg_path)).getroot()
    place = _placement_for(
        root, paper_width_mm, paper_height_mm,
        margin_top_mm, margin_right_mm, margin_bottom_mm, margin_left_mm,
        fit_content, transform_scale, transform_rotation_deg,
        transform_offset_x_mm, transform_offset_y_mm, machine_auto_rotate,
    )
    return place.doc_mm_rect_to_page(*rect)


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
    machine_auto_rotate: str = placement.AUTO_ROTATE_OFF,
) -> None:
    """Write a new SVG sized to the paper, with the source content wrapped in
    the ``<g transform>`` that places it (see app/placement.py).

    The output uses mm as its user-unit coordinate space
    (viewBox = 0 0 paper_w paper_h) so pyaxidraw renders it 1:1 on the bed.
    """
    tree = etree.parse(str(svg_path))
    root = tree.getroot()
    place = _placement_for(
        root, paper_width_mm, paper_height_mm,
        margin_top_mm, margin_right_mm, margin_bottom_mm, margin_left_mm,
        fit_content, transform_scale, transform_rotation_deg,
        transform_offset_x_mm, transform_offset_y_mm, machine_auto_rotate,
    )

    nsmap = {k: v for k, v in root.nsmap.items() if k}
    nsmap[None] = SVG_NS
    new_root = etree.Element(f"{{{SVG_NS}}}svg", nsmap=nsmap)
    new_root.set("width", f"{paper_width_mm}mm")
    new_root.set("height", f"{paper_height_mm}mm")
    new_root.set("viewBox", f"0 0 {paper_width_mm} {paper_height_mm}")

    group = etree.SubElement(new_root, f"{{{SVG_NS}}}g")
    group.set("transform", place.group_transform())
    for child in list(root):
        group.append(child)

    etree.ElementTree(new_root).write(str(out_path), xml_declaration=True, encoding="utf-8")
