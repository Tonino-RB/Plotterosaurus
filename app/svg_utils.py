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

# The grid module's optional cutting marks (see add_cut_marks) live in a layer
# of their own, appended last so it takes the layer index one past the artwork.
# It is a normal top-level layer — listed, selectable, reorderable like any
# other — and this attribute is just a marker so the job wiring can find it by
# position without matching a label the user is free to rename. Whether the
# layer exists at all is the Grid card's "Cut marks" checkbox; whether it plots
# on a given run is its own row in the layer list.
CUT_MARKS_ATTR = "data-plotterosaurus-cut-marks"
CUT_MARKS_LABEL = "Cut marks"

# Cutting marks sit only *between* copies: a short tick where an interior cut
# reaches the sheet edge (a join between two copies), a small cross where two
# interior cuts meet (a join between four). Lengths in mm — small enough to trim
# away, long enough that a pen leaves something to line a ruler up with.
CUT_TICK_MM = 1.0          # edge tick, drawn inward along the cut
CUT_CROSS_ARM_MM = 1.0     # each of the four arms of an interior cross (2mm tip to tip)
CUT_MARK_STROKE_MM = 0.2   # hairline; the pen nib decides the real width


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


def _is_layer(g) -> bool:
    return g.tag == LAYER_TAG and g.get(GROUPMODE_ATTR) == "layer"


def _is_cut_marks_layer(g) -> bool:
    """The grid's cutting-marks layer. Identified by our own attribute, never by
    label — an upload is free to have a layer the user called "Cut marks", and
    the user is free to rename this one. It is a normal indexed layer; this
    predicate is only for the job wiring that needs to point at it by position."""
    return _is_layer(g) and g.get(CUT_MARKS_ATTR) is not None


def _top_level_layers(root):
    return [g for g in root if _is_layer(g)]


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
            if el.tag in _DRAWABLE_TAGS and not _is_layer(el)]


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
    layer, de-collide labels, inline ``inherit`` presentation attributes, give
    point-sized geometry a hair of length. Mutates ``root`` in place; returns
    True if anything changed. See normalize_layer_structure for the why.
    """
    changed = _resolve_inherit_on_root(root)
    changed = _expand_degenerate_geometry(root) or changed

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

    Fixes four failure modes, all of them silent before this ran:

    - Content outside any layer. Only ``inkscape:groupmode="layer"`` groups
      count as layers, so a loose <path>, or the plain <g> groups written by
      producers other than Inkscape, had no layer index at all: not listable,
      not selectable, no pen or speed of its own, just force-fed into the
      first stage (see _top_level_orphans and filter_to_layers). Plain groups
      are promoted in place — they are layers in every sense but the attribute
      their producer didn't write — and whatever is left over is collected
      into one LAYERLESS_LABEL layer at the position it was found.

    - Labels that map to the same vpype layer id (see decollide_layer_labels).

    - ``stroke-width="inherit"`` / ``stroke="inherit"`` on the geometry, with
      the real value on the layer <g> (Inkscape, DrawingBotV3, …). vpype's
      reader does not resolve ``inherit`` — it takes the width as 0 and the
      colour as unset — so an optimized or tiled copy renders blank and
      one-colour even though the plot is fine (see _resolve_inherit_on_root).

    - Point-sized geometry — a bare ``M x y`` dot, a self-referential
      ``M x y L x y``, a zero-radius ``<circle>`` / ``<ellipse>``, a tiny arc
      blob. vpype's reader drops any zero-length subpath (and any round shape
      with a zero radius), so a pen dot vanishes on the way through Optimize
      SVG or Grid. Given a 0.001-unit tail / radius so it survives (see
      _expand_degenerate_geometry).
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


# Presentation attributes an element may set to the literal "inherit". vpype's
# SVG reader does not resolve that keyword — it reads stroke-width="inherit" as
# 0 and stroke="inherit" as unset — so a drawing that declares the pen once on
# the layer <g> and writes "inherit" on every path (Inkscape, DrawingBotV3, …)
# comes back out of grid_svg with stroke-width="0.0" and every layer forced
# black: a blank-looking preview, though the geometry and the plot are fine.
_INHERIT_ATTRS = (
    "stroke", "stroke-width", "fill", "stroke-linecap", "stroke-linejoin",
    "stroke-miterlimit", "stroke-dasharray", "stroke-opacity", "fill-opacity",
    "opacity",
)


def _resolve_inherit_on_root(root) -> bool:
    """Replace ``attr="inherit"`` on every element with the concrete value from
    its nearest ancestor that sets it, dropping the attribute when no ancestor
    resolves it (so the reader's own default applies). Mutates ``root`` in
    place; returns whether anything changed.
    """
    changed = False
    for el in root.iter("*"):   # elements only — skip comments / PIs
        for attr in _INHERIT_ATTRS:
            if el.get(attr) != "inherit":
                continue
            resolved = _nearest_set_attr(el, attr)
            if resolved is None:
                del el.attrib[attr]
            else:
                el.set(attr, resolved)
            changed = True
    return changed


def resolve_inherit_presentation(svg_path: Path) -> bool:
    """``_resolve_inherit_on_root`` against a file, rewriting ``svg_path`` in
    place (atomically — temp sibling + ``os.replace`` — since a caller may pass
    an already-published file a preview fetch can read at any moment). Returns
    whether anything changed.

    Inlining the value the browser would have computed anyway is enough for
    vpype to pick up the real pen width and colour. ``normalize_layer_root``
    already does this for every upload; this is the standalone entry the
    optimize queue uses to also cover files uploaded before that did.
    """
    tree = etree.parse(str(svg_path))
    if not _resolve_inherit_on_root(tree.getroot()):
        return False
    import os
    tmp = svg_path.with_name(f"{svg_path.name}.inherit.tmp")
    tree.write(str(tmp), xml_declaration=True, encoding="utf-8")
    os.replace(tmp, svg_path)
    return True


def _nearest_set_attr(el, attr: str) -> str | None:
    """The value of ``attr`` from the closest ancestor of ``el`` that sets it to
    something other than ``inherit``; ``None`` if none do."""
    parent = el.getparent()
    while parent is not None:
        val = parent.get(attr)
        if val and val != "inherit":
            return val
        parent = parent.getparent()
    return None


# vpype's ``read`` silently discards any subpath with zero length — an ``M`` with
# no drawing command, ``M x y Z``, ``M x y L x y``, ``M x y l 0 0``. That is
# exactly how a pen-plotter *dot* is authored (pen down, pen up), so a drawing
# with a dot in it loses it the moment Optimize SVG or Grid runs vpype over it.
# Anything with real extent (down to ~0.01mm, verified through a 0.125x tile
# downscale) survives, so the repair is to give a genuinely point-sized element
# a 0.001-unit tail before vpype ever sees it. A zero-radius ``<circle>`` /
# ``<ellipse>`` (svgelements drops it outright) and a point-sized arc/curve
# ``<path>`` blob are the same bug in another shape, and get the same treatment.
_CURVE_CMD_RE = re.compile(r"[CcSsQqTtAa]")
_PATH_TOKEN_RE = re.compile(
    r"[MmLlHhVvZz]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
_POINT_NUM_RE = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
# User-unit slop below which two coordinates are "the same point".
_DEGENERATE_EPS = 1e-7
_DEGENERATE_TAIL = " l0.001 0"
# Floor for a point-sized <circle>/<ellipse>: svgelements treats a round shape
# with r/rx/ry == 0 as degenerate and emits nothing, so give it the tail's worth
# of extent instead.
_DEGENERATE_RADIUS = "0.001"


def _straightline_path_points(d: str) -> list[tuple[float, float]] | None:
    """Every point a straight-line-only path ``d`` visits, or ``None`` if it
    uses curves/arcs (assume it has real extent) or can't be parsed."""
    toks = _PATH_TOKEN_RE.findall(d)
    pts: list[tuple[float, float]] = []
    cur = start = (0.0, 0.0)
    cmd: str | None = None
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in "Zz":
                cur = start
                pts.append(cur)
            continue
        if cmd is None:
            return None
        try:
            if cmd in "Mm":
                x, y = float(toks[i]), float(toks[i + 1])
                i += 2
                if cmd == "m":
                    x, y = cur[0] + x, cur[1] + y
                cur = start = (x, y)
                pts.append(cur)
                cmd = "L" if cmd == "M" else "l"   # implicit lineto follows
            elif cmd in "Ll":
                x, y = float(toks[i]), float(toks[i + 1])
                i += 2
                if cmd == "l":
                    x, y = cur[0] + x, cur[1] + y
                cur = (x, y)
                pts.append(cur)
            elif cmd in "Hh":
                x = float(toks[i])
                i += 1
                cur = (cur[0] + x if cmd == "h" else x, cur[1])
                pts.append(cur)
            elif cmd in "Vv":
                y = float(toks[i])
                i += 1
                cur = (cur[0], cur[1] + y if cmd == "v" else y)
                pts.append(cur)
            else:
                return None
        except (IndexError, ValueError):
            return None
    return pts


def _curve_path_span(d: str) -> tuple[float, float] | None:
    """``(width, height)`` of a curved path's true bounding box, resolved
    analytically by svgelements (arcs included, so a mid-arc bulge counts).
    ``None`` if svgelements is unavailable or the data yields no box, in which
    case the caller leaves the path alone — as it did for every curve before."""
    try:
        from svgelements import Path as _SvgPath
        box = _SvgPath(d).bbox()
    except Exception:
        return None
    if not box:
        return None
    x0, y0, x1, y1 = box
    return float(x1) - float(x0), float(y1) - float(y0)


def _expand_degenerate_geometry(root) -> bool:
    """Give every point-sized ``<path>`` / ``<line>`` / ``<polyline>`` /
    ``<polygon>`` a 0.001-unit tail, and floor a point-sized ``<circle>`` /
    ``<ellipse>`` radius to 0.001, so ``vpype read`` keeps it. Mutates ``root``
    in place; returns whether anything changed."""
    changed = False
    for el in root.iter("*"):
        tag = el.tag
        if not isinstance(tag, str):
            continue
        local = tag.rsplit("}", 1)[-1]
        if local == "path":
            d = (el.get("d") or "").strip()
            if not d:
                continue
            if _CURVE_CMD_RE.search(d):
                # A curved/arc dot — Inkscape writes a tiny circle as an arc
                # path. Its d is short; skip the analytic bbox on real artwork.
                if len(d) > 256:
                    continue
                span = _curve_path_span(d)
                if span is None \
                        or span[0] > _DEGENERATE_EPS or span[1] > _DEGENERATE_EPS:
                    continue
            else:
                pts = _straightline_path_points(d)
                if not pts:
                    continue
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                if max(xs) - min(xs) > _DEGENERATE_EPS \
                        or max(ys) - min(ys) > _DEGENERATE_EPS:
                    continue
            if d[-1] in "Zz":
                el.set("d", f"{d[:-1].rstrip()}{_DEGENERATE_TAIL} {d[-1]}")
            else:
                el.set("d", f"{d}{_DEGENERATE_TAIL}")
            changed = True
        elif local == "line":
            x1, y1 = el.get("x1", "0"), el.get("y1", "0")
            x2, y2 = el.get("x2", "0"), el.get("y2", "0")
            try:
                if abs(float(x1) - float(x2)) <= _DEGENERATE_EPS \
                        and abs(float(y1) - float(y2)) <= _DEGENERATE_EPS:
                    el.set("x2", f"{float(x2) + 0.001:.10g}")
                    changed = True
            except ValueError:
                continue
        elif local in ("polyline", "polygon"):
            nums = _POINT_NUM_RE.findall(el.get("points") or "")
            if len(nums) < 2:
                continue
            coords = [float(n) for n in nums[: len(nums) // 2 * 2]]
            xs, ys = coords[0::2], coords[1::2]
            if max(xs) - min(xs) > _DEGENERATE_EPS or max(ys) - min(ys) > _DEGENERATE_EPS:
                continue
            el.set("points",
                   f"{el.get('points').strip()} {xs[0] + 0.001:.10g},{ys[0]:.10g}")
            changed = True
        elif local in ("circle", "ellipse"):
            keys = ("r",) if local == "circle" else ("rx", "ry")
            try:
                radii = [float(el.get(k) or 0.0) for k in keys]
            except ValueError:
                continue                        # a unit or "auto" — assume real
            if any(v > _DEGENERATE_EPS for v in radii):
                continue
            for k in keys:
                el.set(k, _DEGENERATE_RADIUS)
            changed = True
    return changed


def expand_degenerate_geometry(svg_path: Path) -> bool:
    """``_expand_degenerate_geometry`` against a file, rewritten in place
    atomically (temp sibling + ``os.replace``). Returns whether anything changed.

    ``normalize_layer_root`` already does this for every upload; this is the
    standalone entry the optimize queue uses to also cover files uploaded before
    that landed (mirrors ``resolve_inherit_presentation``)."""
    tree = etree.parse(str(svg_path))
    if not _expand_degenerate_geometry(tree.getroot()):
        return False
    import os
    tmp = svg_path.with_name(f"{svg_path.name}.dots.tmp")
    tree.write(str(tmp), xml_declaration=True, encoding="utf-8")
    os.replace(tmp, svg_path)
    return True


def prepare_for_vpype(svg_path: Path) -> bool:
    """Repair ``svg_path`` in place so a ``vpype read`` of it neither drops
    geometry nor misreads pens: resolve ``inherit`` presentation attributes
    (else vpype takes the width as 0 and the colour as unset) and give
    point-sized geometry — a bare-moveto pen dot, a zero-radius circle — a
    0.001-unit tail (else vpype discards it). Returns whether anything changed.

    ``normalize_layer_structure`` already does both for every upload; this is
    the entry every *other* vpype consumer (optimize, grid, pen mode, expert
    mode, export) calls first, so a file uploaded before that landed — or one
    reached by a path that skips it — still survives the round trip intact.
    Atomic per pass, so a preview fetch racing the rewrite sees one whole file.
    """
    changed = resolve_inherit_presentation(svg_path)
    changed = expand_degenerate_geometry(svg_path) or changed
    return changed


def force_round_caps(svg_path: Path) -> None:
    """Set ``stroke-linecap`` / ``stroke-linejoin`` to ``round`` on every
    top-level layer group, in place.

    vpype's ``write`` emits neither, so a grid document renders with the SVG
    default butt caps — short strokes look clipped square and the preview
    disagrees with what the round nib actually leaves. The attribute is
    inherited by every path in the layer that does not set its own.
    """
    tree = etree.parse(str(svg_path))
    root = tree.getroot()
    for g in _top_level_layers(root):
        g.set("stroke-linecap", "round")
        g.set("stroke-linejoin", "round")
    tree.write(str(svg_path), xml_declaration=True, encoding="utf-8")


def _interior_cut_coords(n: int, cell_mm: float, spacing_mm: float) -> list[float]:
    """The ``n - 1`` interior cut-line coordinates along one axis of an
    ``n``-cell run (empty when ``n < 2``).

    Each sits in the middle of the ``2 * spacing`` gap between the two copies it
    separates — which, with the cells laid at pitch ``cell + 2 * spacing``, is
    just ``k * pitch``. With no spacing that collapses onto their shared edge.
    """
    s = max(0.0, spacing_mm)
    return [k * (cell_mm + 2.0 * s) for k in range(1, n)]


def add_cut_marks(svg_path: Path, cols: int, rows: int,
                  cell_w_mm: float, cell_h_mm: float,
                  spacing_x_mm: float, spacing_y_mm: float) -> None:
    """Append a cutting-marks layer to a tiled document, in place.

    Marks sit only *between* copies, never on the sheet's own corners or edges
    (nor on the outer copies' own edges, even though the spacing now insets
    them):

    * a ``CUT_TICK_MM`` tick, drawn inward along the cut, where an interior cut
      line reaches the sheet edge — the join between two copies;
    * a cross of two ``2 * CUT_CROSS_ARM_MM`` strokes where two interior cut
      lines meet — the join between four copies.

    ``cell_w_mm``/``cell_h_mm``/``spacing_*_mm`` are the ones the tiling ran with
    (see svg_optimize.grid_svg): a cut line sits mid-gap, which is exactly where
    the copies were spaced apart.

    Appended last, so it takes the layer index one past the artwork's — the
    index main._sync_cut_marks_selection points the "Cut marks" row at. The
    CUT_MARKS_ATTR tag is just a durable handle on the layer (tests, Inkscape,
    future callers) that survives the user renaming it. ink_cache will now
    measure it like any layer; the marks fall inside the tiled document's own
    box, which placement lays out in full, so they still cannot push anything
    off the page.
    """
    tree = etree.parse(str(svg_path))
    root = tree.getroot()

    # Document mm -> the user units an SVG coordinate is written in.
    doc_w_mm, doc_h_mm = svg_size_mm(root)
    viewbox = parse_viewbox(root.get("viewBox", ""))
    if viewbox and doc_w_mm and doc_h_mm:
        vb_x, vb_y, vb_w, vb_h = viewbox
        per_mm_x, per_mm_y = vb_w / doc_w_mm, vb_h / doc_h_mm
    else:
        vb_x, vb_y = 0.0, 0.0
        per_mm_x = per_mm_y = PX_PER_MM

    sx, sy = max(0.0, spacing_x_mm), max(0.0, spacing_y_mm)
    sheet_w = cols * (cell_w_mm + 2.0 * sx)
    sheet_h = rows * (cell_h_mm + 2.0 * sy)
    xs = _interior_cut_coords(cols, cell_w_mm, spacing_x_mm)
    ys = _interior_cut_coords(rows, cell_h_mm, spacing_y_mm)

    layer = etree.SubElement(root, LAYER_TAG)
    layer.set(GROUPMODE_ATTR, "layer")
    layer.set(LABEL_ATTR, CUT_MARKS_LABEL)
    layer.set(CUT_MARKS_ATTR, "1")
    # vpype reads a layer's id from the first digit group of its label, else of
    # its id (see _vpype_layer_id). Claim one no artwork layer already answers
    # to, or a measuring pass would fold the marks into that layer's geometry.
    taken = {_vpype_layer_id(g_.get(LABEL_ATTR) or "", g_.get("id") or "", order)
             for order, g_ in enumerate(_top_level_layers(root), start=1)}
    layer.set("id", f"cutmarks{next(i for i in itertools.count(1) if i not in taken)}")
    layer.set("fill", "none")
    layer.set("stroke", "#000000")
    layer.set("stroke-linecap", "butt")
    layer.set("stroke-width", f"{CUT_MARK_STROKE_MM * per_mm_x:.4f}")

    def _seg(x1: float, y1: float, x2: float, y2: float) -> None:
        line = etree.SubElement(layer, f"{{{SVG_NS}}}line")
        line.set("x1", f"{vb_x + x1 * per_mm_x:.4f}")
        line.set("y1", f"{vb_y + y1 * per_mm_y:.4f}")
        line.set("x2", f"{vb_x + x2 * per_mm_x:.4f}")
        line.set("y2", f"{vb_y + y2 * per_mm_y:.4f}")

    t, a = CUT_TICK_MM, CUT_CROSS_ARM_MM
    # Edge ticks, pointing inward so nothing lands off the sheet.
    for x in xs:
        _seg(x, 0.0, x, t)
        _seg(x, sheet_h - t, x, sheet_h)
    for y in ys:
        _seg(0.0, y, t, y)
        _seg(sheet_w - t, y, sheet_w, y)
    # Crosses where two interior cuts meet.
    for x in xs:
        for y in ys:
            _seg(x - a, y, x + a, y)
            _seg(x, y - a, x, y + a)

    tree.write(str(svg_path), xml_declaration=True, encoding="utf-8")


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
