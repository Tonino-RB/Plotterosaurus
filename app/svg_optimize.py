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
import math
import os
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

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
) -> list[str]:
    cmd = _vpype_cmd()
    cmd += ["read", str(src)]
    if linemerge:
        cmd += ["linemerge", "--tolerance", f"{tolerance_mm}mm"]
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


def _run_vpype(cmd: list[str], tmp: Path, dst: Path, label: str) -> None:
    """Run one cancellable vpype pipeline that writes ``tmp``, then publish it
    as ``dst``. Raises ``OptimizeError`` on failure or empty output.

    Shared by ``optimize_svg`` and ``grid_svg``, which otherwise carried this
    same block twice: the two have to stay in step with ``cancel_current()``'s
    ``_current_proc`` contract and with the ``.partial`` naming rule ``_partial``
    documents, and a fix applied to one copy silently missed the other.

    ``run_expert`` is deliberately not a caller — it streams vpype's output line
    by line and enforces its own timeout, which is a different shape.
    """
    log.info("%s: %s", label, " ".join(cmd))
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
        # vpype puts the message itself last, under any traceback above it.
        last_line = msg.splitlines()[-1] if msg else f"rc={proc.returncode}"
        raise OptimizeError(last_line)
    if not tmp.exists():
        # See normalize_layers: vpype exits 0 and writes nothing when the
        # document holds no plottable geometry. Raising the queue's own error
        # type means _process reports it as a failed optimization rather than
        # letting a FileNotFoundError escape as "internal error".
        raise OptimizeError("the document contains no plottable geometry")
    # Only now does the finished file become visible under its real name.
    os.replace(tmp, dst)


def optimize_svg(
    src: Path,
    dst: Path,
    tolerance_mm: float,
    linemerge: bool,
    linesimplify: bool,
    linesort: bool,
    reloop: bool,
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
    if not any([linemerge, linesimplify, linesort, reloop]):
        # No-op pipeline: copy source to keep downstream code uniform.
        _atomic_copy(src, dst)
        return
    tmp = _partial(dst)
    cmd = build_pipeline(src, tmp, tolerance_mm, linemerge, linesimplify, linesort, reloop)
    _run_vpype(cmd, tmp, dst, "optimize")


def arrangement(copies: int, paper_w_mm: float, paper_h_mm: float,
                content_w_mm: float | None = None,
                content_h_mm: float | None = None) -> tuple[int, int]:
    """Best columns x rows to fit ``copies`` cells on a ``paper_w`` x ``paper_h``
    sheet: the split that lets each copy be scaled up the most while keeping its
    aspect ratio. Falls back to the sheet's own aspect when the drawing has no
    resolvable size.

    ``cols * rows`` is >= ``copies`` and may exceed it (e.g. 5 -> 3x2); the extra
    cells are filled too. Common counts (2, 4, 6, 8, 9, 12, 16) land exactly.
    """
    copies = max(1, int(copies))
    w = paper_w_mm if paper_w_mm and paper_w_mm > 0 else 1.0
    h = paper_h_mm if paper_h_mm and paper_h_mm > 0 else 1.0
    cw = content_w_mm if content_w_mm and content_w_mm > 0 else w
    ch = content_h_mm if content_h_mm and content_h_mm > 0 else h
    best_key: tuple | None = None
    best: tuple[int, int] = (1, copies)
    for cols in range(1, copies + 1):
        rows = math.ceil(copies / cols)
        fit = min((w / cols) / cw, (h / rows) / ch)
        # Largest copies win; ties break toward fewer wasted cells, then toward
        # an arrangement whose own aspect is closest to the sheet's.
        key = (round(fit, 6), -(cols * rows - copies),
               -abs((cols / rows) - (w / h)))
        if best_key is None or key > best_key:
            best_key, best = key, (cols, rows)
    return best


def clamp_gutter_mm(gutter_mm: float, cell_w_mm: float, cell_h_mm: float) -> float:
    """The largest gutter that still leaves a cell to draw in.

    ``layout -m`` subtracts *twice* the margin from the cell and scales the copy
    by ``min((w - 2m) / content_w, (h - 2m) / content_h)`` with no check that the
    result is positive (see vpype_cli/operations.py). A gutter at or above the
    cell size therefore produces a negative scale: every copy comes out mirrored,
    enlarged and off the page, with vpype exiting 0 and reporting nothing.

    Capped at half the smaller cell dimension, so the drawing always keeps at
    least half of it. Clamped rather than refused for the same reason the job
    fields in main.py are (``_CLAMP_RANGES``): the user dragged a slider past
    what this sheet can hold, they didn't ask for the request to fail.
    """
    return min(max(0.0, gutter_mm), 0.5 * min(cell_w_mm, cell_h_mm))


def grid_svg(src: Path, dst: Path, cols: int, rows: int,
             cell_w_mm: float, cell_h_mm: float, gutter_mm: float) -> None:
    """Tile ``src`` into a ``cols`` x ``rows`` grid, resizing each copy to fit a
    ``cell_w`` x ``cell_h`` mm cell (minus ``gutter_mm`` between copies). Writes
    ``dst`` atomically. Raises ``OptimizeError`` on vpype failure / empty output.

    ``gutter_mm`` is expected to have been through ``clamp_gutter_mm`` already —
    the caller needs the clamped value too, to put the cutting marks on the same
    lines the copies were inset to (see optimize_queue._run_grid_phase).

    Same atomic-write shape as ``optimize_svg`` (see its docstring): vpype writes
    a sibling ``.partial`` file which is renamed into place, so a reader sees the
    previous complete file or the new one, never a half-tiled document.
    """
    tmp = _partial(dst)
    inset = max(0.0, gutter_mm) / 2.0
    # vpype's `layout` enforces portrait unless --landscape is given: it runs the
    # size through _normalize_page_size, which swaps any page whose width exceeds
    # its height. Without this flag every landscape cell is silently fitted to a
    # portrait box of the same dimensions, so the copies come out too small in
    # one axis and overflow their pitch in the other.
    landscape = ["-l"] if cell_w_mm > cell_h_mm else []
    cmd = _vpype_cmd() + [
        "begin",
        "grid", "-o", f"{cell_w_mm}mm", f"{cell_h_mm}mm", str(int(cols)), str(int(rows)),
        "read", str(src),
        "layout", *landscape, "-m", f"{inset}mm", f"{cell_w_mm}mmx{cell_h_mm}mm",
        "end",
        "write", str(tmp),
    ]
    _run_vpype(cmd, tmp, dst, "grid")


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


def build_custom_pipeline(src: Path, dst: Path, box_texts: list[str]) -> list[str]:
    """Compose a vpype command line from expert mode's raw command boxes.

    Each non-blank box's text is ``shlex.split`` and chained onto the command
    line, in order, between ``read`` and ``write`` — one vpype pipeline, as
    opposed to beginner mode's fixed four-toggle pipeline. Raises
    ``ValueError`` (from shlex) on malformed quoting.
    """
    cmd = _vpype_cmd()
    cmd += ["read", str(src)]
    for text in box_texts:
        text = (text or "").strip()
        if not text:
            continue
        cmd += shlex.split(text)
    cmd += ["write", str(dst)]
    return cmd


def run_custom_pipeline(
    src: Path,
    dst: Path,
    box_texts: list[str],
    on_output: Callable[[str], None] | None = None,
    timeout_s: float = 180.0,
) -> None:
    """Produce ``dst`` from ``src`` using expert mode's raw command boxes.

    Same atomic-write shape as ``optimize_svg`` (see its docstring), but the
    pipeline is arbitrary user-typed text rather than four fixed toggles, so
    two things beginner mode doesn't need are added here:

    - ``on_output`` is called with each line of the subprocess's combined
      stdout/stderr as it runs, so a caller can show live progress.
    - A wall-clock ``timeout_s`` bounds the run — raw command text could
      describe a pipeline that never finishes, and a stuck subprocess would
      hold the single heavy-work slot (app/workload.py) indefinitely. A timer
      thread terminates the process if it's still running past the deadline,
      independent of whether it's producing output.
    """
    non_blank = [t for t in box_texts if (t or "").strip()]
    if not non_blank:
        _atomic_copy(src, dst)
        return
    tmp = _partial(dst)
    try:
        cmd = build_custom_pipeline(src, tmp, box_texts)
    except ValueError as e:
        raise OptimizeError(f"could not parse command: {e}") from e
    log.info("optimize (expert): %s", " ".join(cmd))
    env = {**os.environ, "VPYPE_NO_COLOR": "1"}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, env=env)
    with _proc_lock:
        global _current_proc
        _current_proc = proc

    timed_out = threading.Event()

    def _on_timeout() -> None:
        timed_out.set()
        try:
            proc.terminate()
        except Exception:
            pass

    timer = threading.Timer(timeout_s, _on_timeout)
    timer.daemon = True
    timer.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if on_output is not None:
                on_output(line.rstrip("\n"))
        proc.wait()
    finally:
        timer.cancel()
        with _proc_lock:
            _current_proc = None

    if timed_out.is_set():
        tmp.unlink(missing_ok=True)
        raise OptimizeError(f"timed out after {timeout_s:.0f}s")
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise OptimizeError(f"vpype exited with code {proc.returncode}")
    if not tmp.exists():
        raise OptimizeError("the document contains no plottable geometry")
    os.replace(tmp, dst)


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
