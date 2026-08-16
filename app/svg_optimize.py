"""vpype subprocess wrapper for SVG optimization.

The plot worker invokes ``optimize_svg`` before planning when a job has
``optimize=True``. The pipeline composes a vpype command line from the four
toggles (linemerge / linesimplify / linesort / reloop); tolerance feeds the
two commands that accept it.

vpype's default ``read``/``write`` round-trip preserves Inkscape layer
structure (``inkscape:groupmode="layer"`` and ``inkscape:label``), so
downstream ``svg_utils.filter_to_layers`` keeps working on the optimized file.
"""
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)


class OptimizeError(RuntimeError):
    """Raised when vpype exits non-zero."""


_current_proc: subprocess.Popen | None = None
_proc_lock = threading.Lock()


def _vpype_cmd() -> list[str]:
    """Locate the vpype CLI. Prefer the venv binary; fall back to module form."""
    venv_bin = Path(sys.executable).parent / "vpype"
    if venv_bin.is_file():
        return [str(venv_bin)]
    return [sys.executable, "-m", "vpype_cli"]


def build_pipeline(
    src: Path,
    dst: Path,
    tolerance_mm: float,
    linemerge: bool,
    linesimplify: bool,
    linesort: bool,
    reloop: bool,
    min_length_enabled: bool = False,
    min_length_mm: float = 1.0,
) -> list[str]:
    cmd = _vpype_cmd()
    cmd += ["read", str(src)]
    if linemerge:
        cmd += ["linemerge", "--tolerance", f"{tolerance_mm}mm"]
    if min_length_enabled:
        cmd += ["filter", "--min-length", f"{min_length_mm}mm"]
    if linesimplify:
        cmd += ["linesimplify", "--tolerance", f"{tolerance_mm}mm"]
    if linesort:
        cmd += ["linesort"]
    if reloop:
        cmd += ["reloop"]
    cmd += ["write", str(dst)]
    return cmd


def _partial(dst: Path) -> Path:
    """The sibling scratch name a file is built under before it is renamed.

    The ``.svg`` stays last: vpype's ``write`` picks its output format from the
    file extension, and a name ending ``.partial`` makes it write nothing at
    all — successfully, with return code 0, which is a memorably unhelpful way
    to fail. Leading dot so the scratch file sorts out of the way and cannot
    collide with the ``{svg_id}.*`` globs used to clean up a job.
    """
    return dst.with_name(f".{dst.stem}.partial{dst.suffix}")


def _atomic_copy(src: Path, dst: Path) -> None:
    """Copy so ``dst`` never exists in a partial state — see optimize_svg."""
    tmp = _partial(dst)
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def optimize_svg(
    src: Path,
    dst: Path,
    tolerance_mm: float,
    linemerge: bool,
    linesimplify: bool,
    linesort: bool,
    reloop: bool,
    min_length_enabled: bool = False,
    min_length_mm: float = 1.0,
) -> None:
    """Produce ``dst`` from ``src``. Raises ``OptimizeError`` on vpype failure.

    ``dst`` appears atomically. vpype used to write it in place, which meant
    the optimized file existed — empty, then half-parsed — for the whole run.
    Every reader resolves the optimized path by asking whether it exists
    (``plot_worker._effective_svg_path``), so during those seconds the preview
    could load a truncated document, and so could a plot. A partially written
    SVG handed to the plotter is the worst failure this program has: the
    machine would faithfully draw whatever fragment parsed.

    Writing to a sibling temp file and renaming closes that window. ``rename``
    within a directory is atomic on POSIX, so a reader sees the previous
    complete file or the new complete one, never the middle of one.
    """
    if not any([linemerge, linesimplify, linesort, reloop, min_length_enabled]):
        # No-op pipeline: copy source to keep downstream code uniform.
        _atomic_copy(src, dst)
        return
    tmp = _partial(dst)
    cmd = build_pipeline(src, tmp, tolerance_mm, linemerge, linesimplify, linesort, reloop,
                         min_length_enabled, min_length_mm)
    log.info("optimize: %s", " ".join(cmd))
    env = {**os.environ, "VPYPE_NO_COLOR": "1"}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env)
    with _proc_lock:
        global _current_proc
        _current_proc = proc
    try:
        stdout, stderr = proc.communicate()
    finally:
        with _proc_lock:
            _current_proc = None
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        msg = (stderr.strip() or stdout.strip() or
               f"vpype exited with code {proc.returncode}")
        # Keep it short — multi-line tracebacks just bloat the UI error pill.
        first_line = msg.splitlines()[-1] if msg else f"rc={proc.returncode}"
        raise OptimizeError(first_line)
    if not tmp.exists():
        # See normalize_layers: vpype exits 0 and writes nothing when the
        # document holds no plottable geometry. Raising the queue's own error
        # type means _process reports it as a failed optimization rather than
        # letting a FileNotFoundError escape as "internal error".
        raise OptimizeError("the document contains no plottable geometry")
    # Only now does the optimized file become visible under its real name.
    os.replace(tmp, dst)


def normalize_layers(src: Path, dst: Path) -> bool:
    """Write ``dst`` from ``src`` via a bare vpype read/write round-trip.
    Returns True if ``dst`` was written, False if there was nothing to write.

    vpype's ``read`` imports any top-level SVG content that isn't already
    inside an Inkscape layer group into layer 1 (see its own docs), and
    ``write`` re-emits it as a proper ``inkscape:groupmode="layer"`` group.
    Used to repair SVGs that have elements sitting outside any layer, which
    ``svg_utils.parse_layers`` otherwise reports as having no layers at all.
    Raises ``OptimizeError`` on vpype failure.

    A document with nothing plottable in it — empty, or holding only
    text/images/defs — is not a failure and is not an empty output either:
    vpype exits 0 and declines to create the file at all ("no geometry to
    save, no file created"). Renaming unconditionally turned that into a
    FileNotFoundError, which is not an OptimizeError, so it escaped the
    caller's handler and surfaced as a 500 on /upload while leaving the
    uploaded file orphaned on disk. Report it as False and let the caller
    fall back to its own no-layers rejection.
    """
    cmd = _vpype_cmd() + [
        "read", str(src),
        "name", "--layer", "1", "Vpype Auto Layer",
        "write", str(_partial(dst)),
    ]
    log.info("normalize_layers: %s", " ".join(cmd))
    env = {**os.environ, "VPYPE_NO_COLOR": "1"}
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        _partial(dst).unlink(missing_ok=True)
        msg = (proc.stderr.strip() or proc.stdout.strip() or
               f"vpype exited with code {proc.returncode}")
        first_line = msg.splitlines()[-1] if msg else f"rc={proc.returncode}"
        raise OptimizeError(first_line)
    if not _partial(dst).exists():
        return False
    os.replace(_partial(dst), dst)
    return True


def cancel_current() -> None:
    """Kill the in-flight vpype subprocess, if any. Called from the cancel path."""
    with _proc_lock:
        proc = _current_proc
    if proc is None:
        return
    try:
        proc.terminate()
    except Exception:
        log.exception("optimize: terminate failed")
