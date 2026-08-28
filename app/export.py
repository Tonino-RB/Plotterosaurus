"""Convert a job's processed drawing to other file formats for download.

Two scopes:

* **As optimized** — the input is whatever ``plot_worker._effective_svg_path``
  resolves to (the vpype ``.opt.svg`` once optimization has run, else the raw
  upload). The drawing in its own coordinate space: no placement, no skew, no
  layer selection. Feed it to another tool that does its own placement.
* **As plotted** (``build_placed_svg``) — the drawing rendered as a plot would
  lay it down: only the selected layers, positioned on the page by the job's
  placement settings (layers merged, not staged). G-code and HPGL are also
  sheared by the *active machine's* axis skew — that correction only belongs
  in a machine toolpath, so SVG / PNG / PDF stay square. This is the
  post-processing output for driving another plotter / GRBL machine directly.

Formats:

* ``svg``   — the file itself.
* ``png``   — via cairosvg; white or transparent background.
* ``pdf``   — via cairosvg.
* ``gcode`` — vpype ``gwrite --profile gcodemm`` (millimetres, absolute, Y-up);
  the profile ships with the ``vpype-gcode`` plugin.
* ``hpgl``  — vpype ``gwrite --profile hpgl_abs`` (absolute HPGL at the format's
  native 40 units/mm), defined in ``vpype_export.toml`` beside this file — see
  there for why the plugin's own ``HPGL-R`` profile is not used. Read with
  ``--single-layer`` so the whole drawing is pen ``SP1``. Not framed to a
  plotter page (coordinates may be negative).
"""
from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

FORMATS = ("svg", "png", "pdf", "gcode", "hpgl")

_EXT = {"svg": "svg", "png": "png", "pdf": "pdf", "gcode": "gcode", "hpgl": "hpgl"}

_MEDIA = {
    "svg": "image/svg+xml",
    "png": "image/png",
    "pdf": "application/pdf",
    "gcode": "text/plain; charset=utf-8",
    "hpgl": "text/plain; charset=utf-8",
}

_GWRITE_PROFILE = {"gcode": "gcodemm", "hpgl": "hpgl_abs"}

# hpgl_abs is defined here rather than in vpype-gcode's bundled profiles.
_VPYPE_CONFIG = Path(__file__).with_name("vpype_export.toml")

_TIMEOUT_S = 60.0


class ExportError(RuntimeError):
    """Raised when a conversion fails, times out, or produces nothing."""


def extension(fmt: str) -> str:
    return _EXT[fmt]


def media_type(fmt: str) -> str:
    return _MEDIA[fmt]


def _vpype_cmd() -> list[str]:
    """Locate the vpype CLI — venv binary first, module form as a fallback.
    Mirrors ``svg_optimize._vpype_cmd`` / ``layer_group._vpype_cmd``."""
    venv_bin = Path(sys.executable).parent / "vpype"
    if venv_bin.is_file():
        return [str(venv_bin)]
    return [sys.executable, "-m", "vpype_cli"]


def export(src: Path, dst: Path, fmt: str, *, transparent: bool = False) -> None:
    """Write ``dst`` in ``fmt`` from the SVG at ``src``. Raises ``ExportError``.

    ``dst`` is written via a sibling temp file and renamed, so a reader never
    sees a half-written export (same reasoning as ``svg_optimize.optimize_svg``).
    """
    if fmt not in FORMATS:
        raise ExportError(f"unsupported format: {fmt}")
    if not src.is_file():
        raise ExportError("source drawing is missing")

    # Unique temp name so two concurrent exports of the same job+format (a
    # double-clicked Download) don't clobber each other's scratch file; the
    # leading dot keeps it out of the {svg_id}.* library/cleanup globs.
    tmp = dst.with_name(f".{dst.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        if fmt == "svg":
            _copy_svg(src, tmp)
        elif fmt in ("png", "pdf"):
            _cairosvg(src, tmp, fmt, transparent)
        elif fmt == "hpgl":
            _gwrite(src, tmp, _GWRITE_PROFILE[fmt],
                    single_layer=True, config=_VPYPE_CONFIG)
        else:
            _gwrite(src, tmp, _GWRITE_PROFILE[fmt])
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise ExportError("conversion produced no output")
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


def build_placed_svg(job: dict, src: Path, dst: Path, *, apply_skew: bool) -> None:
    """Render ``src`` into ``dst`` the way a plot of ``job`` would place it:
    keep only the selected layers, then position them on the page with the
    job's placement settings (size, margins, scale / rotation / offset,
    machine auto-rotate). One file — layers merged, not staged.

    ``apply_skew`` additionally shears the result by the *active machine's*
    axis skew. That correction only belongs in a machine toolpath (G-code /
    HPGL); a picture of the drawing (SVG / PNG / PDF) should stay square, so
    the caller passes ``apply_skew=False`` for those.

    Raises ``ExportError`` on a malformed document or an empty selection.
    Mirrors the per-stage pipeline in ``plot_worker._run_staged_loop_impl``,
    collapsed to the union of the selected layers.
    """
    from . import axis_skew, config, svg_utils

    selected = [s["index"] for s in job.get("layer_selections", [])
                if s.get("selected", True)]
    if not selected:
        raise ExportError("no layers are selected for this job")

    stem = f".{dst.name}.{uuid.uuid4().hex[:8]}"
    combined = dst.with_name(f"{stem}.layers.svg")
    work = dst.with_name(f"{stem}.placed.svg")
    try:
        svg_utils.filter_to_layers(src, selected, combined, include_orphans=True)
        svg_utils.transform_to_paper(
            combined, work,
            job["paper_width_mm"], job["paper_height_mm"],
            job["margin_top_mm"], job["margin_right_mm"],
            job["margin_bottom_mm"], job["margin_left_mm"],
            job["fit_content"],
            transform_scale=job.get("transform_scale", 1.0),
            transform_rotation_deg=job.get("transform_rotation_deg", 0.0),
            transform_offset_x_mm=job.get("transform_offset_x_mm", 0.0),
            transform_offset_y_mm=job.get("transform_offset_y_mm", 0.0),
            machine_auto_rotate=config.MACHINE_AUTO_ROTATE,
        )
        if apply_skew:
            machine = config.active_machine()
            axis_skew.apply_axis_skew(
                work, machine["skew_deg"], machine.get("skew_true_axis", "x"),
                job["paper_width_mm"], job["paper_height_mm"])
        work.replace(dst)
    except ExportError:
        raise
    except Exception as e:
        raise ExportError(f"could not place the drawing: {e}") from e
    finally:
        combined.unlink(missing_ok=True)
        work.unlink(missing_ok=True)


def _copy_svg(src: Path, dst: Path) -> None:
    dst.write_bytes(src.read_bytes())


def _cairosvg(src: Path, dst: Path, fmt: str, transparent: bool) -> None:
    # Imported lazily: cairosvg pulls a cairo binding, and nothing else in the
    # server needs it — a broken install should only break this one feature.
    try:
        import cairosvg
    except Exception as e:
        raise ExportError(f"cairosvg is not available: {e}") from e
    try:
        if fmt == "png":
            cairosvg.svg2png(
                url=str(src), write_to=str(dst),
                background_color=None if transparent else "white",
            )
        else:
            cairosvg.svg2pdf(url=str(src), write_to=str(dst))
    except Exception as e:
        raise ExportError(f"could not render {fmt.upper()}: {e}") from e


def _gwrite(src: Path, dst: Path, profile: str, *,
            single_layer: bool = False, config: Path | None = None) -> None:
    cmd = _vpype_cmd()
    if config is not None:
        cmd += ["--config", str(config)]
    cmd += ["read", "--single-layer", str(src)] if single_layer else ["read", str(src)]
    cmd += ["gwrite", "--profile", profile, str(dst)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        raise ExportError(f"timed out after {_TIMEOUT_S:.0f}s") from e
    if proc.returncode != 0:
        msg = (proc.stderr.strip() or proc.stdout.strip()
               or f"vpype exited with code {proc.returncode}")
        raise ExportError(msg.splitlines()[-1])
