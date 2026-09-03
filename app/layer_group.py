"""Re-partition an uploaded SVG's top-level layer structure for a job's
``layer_mode`` (see app/main.py ``_regroup_svg``).

The default mode, ``layer``, uses the SVG's Inkscape layers as-is and never
comes here. The two non-default modes each write a standalone layered SVG that
the rest of the pipeline treats as an ordinary upload — the job points at it by
its own id while the mode is active, so ``filter_to_layers`` / ``ink_cache`` /
placement / ``optimize_queue`` need no changes.

* ``group`` — when a single wrapper <g> hides the real structure, descend one
  level so each of its child groups becomes a top-level layer. lxml only; the
  wrapper's own transform is pushed down onto the children so nothing moves.
* ``pen``   — one layer per distinct (stroke-width, stroke-colour) pair, via
  vpype's ``read --attr``. Geometry is re-emitted as polylines, the same as the
  Optimize SVG step.
"""
import logging
import os
import subprocess
import sys
from pathlib import Path

from lxml import etree

from . import svg_utils

log = logging.getLogger(__name__)

LAYER_TAG = svg_utils.LAYER_TAG
GROUPMODE_ATTR = svg_utils.GROUPMODE_ATTR
LABEL_ATTR = svg_utils.LABEL_ATTR


class RegroupError(RuntimeError):
    """Raised when a drawing can't be re-partitioned for the requested mode."""


def regroup(src_svg: Path, mode: str, out_svg: Path) -> None:
    """Write ``out_svg`` as a layered SVG partitioned for ``mode``."""
    if mode == "group":
        _regroup_by_groups(src_svg, out_svg)
    elif mode == "pen":
        _regroup_by_pen(src_svg, out_svg)
    else:
        raise RegroupError(f"unknown layer mode: {mode!r}")


# --- group -----------------------------------------------------------------

def _regroup_by_groups(src_svg: Path, out_svg: Path) -> None:
    tree = etree.parse(str(src_svg))
    root = tree.getroot()

    top_groups = [el for el in root if el.tag == LAYER_TAG]
    if len(top_groups) == 1:
        wrapper = top_groups[0]
        wrap_tf = wrapper.get("transform")
        at = root.index(wrapper)
        for offset, child in enumerate(list(wrapper)):
            if wrap_tf and isinstance(child.tag, str):
                child_tf = child.get("transform")
                child.set("transform",
                          f"{wrap_tf} {child_tf}" if child_tf else wrap_tf)
            root.insert(at + offset, child)
        root.remove(wrapper)

    svg_utils.normalize_layer_root(root)
    tree.write(str(out_svg), xml_declaration=True, encoding="utf-8")


# --- pen -----------------------------------------------------------------

def _vpype_cmd() -> list[str]:
    venv_bin = Path(sys.executable).parent / "vpype"
    if venv_bin.is_file():
        return [str(venv_bin)]
    return [sys.executable, "-m", "vpype_cli"]


def _regroup_by_pen(src_svg: Path, out_svg: Path) -> None:
    # A bare "read" still drops a pen dot and misreads inherit-ed strokes — the
    # latter would also bucket every such stroke into one pen. Repair first, the
    # same as the optimize path (see svg_utils.prepare_for_vpype).
    svg_utils.prepare_for_vpype(src_svg)
    tmp = out_svg.with_name(f".{out_svg.stem}.partial{out_svg.suffix}")
    cmd = _vpype_cmd() + [
        "read", "--attr", "stroke", "--attr", "stroke-width", str(src_svg),
        "write", str(tmp),
    ]
    log.info("regroup(pen): %s", " ".join(cmd))
    env = {**os.environ, "VPYPE_NO_COLOR": "1"}
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        msg = (proc.stderr.strip() or proc.stdout.strip()
               or f"vpype exited with code {proc.returncode}")
        raise RegroupError(msg.splitlines()[-1])
    if not tmp.exists():
        # vpype exits 0 and writes nothing when there's no plottable geometry.
        raise RegroupError("the document contains no plottable geometry")
    _label_pen_layers(tmp)
    os.replace(tmp, out_svg)


def _style_value(g, name: str) -> str | None:
    v = g.get(name)
    if v is not None:
        return v
    for decl in (g.get("style") or "").split(";"):
        key, sep, val = decl.partition(":")
        if sep and key.strip() == name:
            return val.strip()
    return None


def _label_pen_layers(svg_path: Path) -> None:
    """Give each vpype ``read --attr`` layer a readable label. The leading
    ``Pen N`` ordinal keeps every label's first digit group distinct, so vpype
    won't later merge two pens (see svg_utils._vpype_layer_id)."""
    tree = etree.parse(str(svg_path))
    root = tree.getroot()
    for n, g in enumerate(svg_utils._top_level_layers(root), start=1):
        g.set(GROUPMODE_ATTR, "layer")
        colour = _style_value(g, "stroke") or "no stroke"
        width_px = _style_value(g, "stroke-width")
        try:
            width = f"{round(float(width_px) / svg_utils.PX_PER_MM, 2):g} mm"
        except (TypeError, ValueError):
            width = "default width"
        g.set(LABEL_ATTR, f"Pen {n} · {width} · {colour}")
    svg_utils.decollide_layer_labels(root)   # ordinals already differ; belt and braces
    tree.write(str(svg_path), xml_declaration=True, encoding="utf-8")
