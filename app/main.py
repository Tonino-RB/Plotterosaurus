import asyncio
import json as _json
import logging
import shutil
import subprocess
import threading
import uuid
from collections import OrderedDict

log = logging.getLogger(__name__)
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import (
    Depends, FastAPI, File, Form, Header, HTTPException,
    Request, UploadFile, WebSocket, WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from lxml import etree

from . import (
    camera, config, export, ink_cache, layer_group, notify, optimize_expert_queue,
    optimize_queue, placement, plan_queue, plot_worker, state, svg_complexity,
    svg_optimize, svg_utils, updates,
)

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"

# Mirror of static/app.js PAPER_SIZES — portrait dims in mm. Used by the
# /api/v1/jobs endpoint to resolve paper_size.name into dimensions.
PAPER_PRESETS: dict[str, tuple[float, float]] = {
    "A0": (841, 1189), "A1": (594, 841), "A2": (420, 594),
    "A3": (297, 420),  "A4": (210, 297), "A5": (148, 210),
    "B0": (1000, 1414), "B1": (707, 1000), "B2": (500, 707),
    "B3": (353, 500),  "B4": (250, 353), "B5": (176, 250),
    "Letter": (216, 279), "Legal": (216, 356), "Ledger": (279, 432),
    "ANSI-C": (432, 559), "ANSI-D": (559, 864), "ANSI-E": (864, 1118),
}

LENGTH_UNIT_TO_MM: dict[str, float] = {"mm": 1.0, "cm": 10.0, "in": 25.4}

# Ceiling on an uploaded SVG, in bytes. Nothing else in the stack caps this —
# there is no reverse proxy, and uvicorn does not limit a request body — so
# without it one POST is read whole into the memory of a 3.7GB board that is
# also running a browser, and a stream of them fills the SD card.
#
# 32MB is deliberately generous: the largest drawings this is built for are
# around 8MB (see tests/conftest.py), so the limit only ever catches something
# that was never going to plot anyway.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
_UPLOAD_CHUNK = 1024 * 1024


async def _read_capped(file: UploadFile) -> bytes:
    """Read an upload, refusing anything past ``MAX_UPLOAD_BYTES``.

    Chunked so an oversized body is rejected while it arrives rather than
    after it has already been materialized in full — which is the entire
    point, since holding it is the failure being prevented.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise _coded(413, "file_too_large",
                         limit_mb=MAX_UPLOAD_BYTES // (1024 * 1024))
        chunks.append(chunk)
    return b"".join(chunks)


# Web-UI-facing errors carry a stable {code, params} detail instead of an
# English string, so the browser can localize them (see apiErrText in app.js).
# Only the unprefixed (web UI) routes use these; the /api/v1/* routes keep
# their plain-string details for external clients.
def _coded(status: int, code: str, **params) -> HTTPException:
    detail: dict = {"code": code}
    if params:
        detail["params"] = params
    return HTTPException(status, detail=detail)


def _normalize_svg_layers(path: Path, info: dict) -> dict:
    """Make the uploaded SVG's layer structure usable, and re-parse if it was
    changed. Promotes layerless content into real layers and de-collides layer
    labels (see svg_utils.normalize_layer_structure); anything it leaves
    behind still goes through the vpype fallback below.

    If the SVG has no Inkscape layers, some content is likely sitting
    outside any layer group. Fold it into one via a bare vpype read/write
    round-trip and re-parse. Best-effort: on vpype failure, the original
    (empty-layers) info is returned unchanged so the caller's existing
    no-layers handling still applies."""
    try:
        if svg_utils.normalize_layer_structure(path):
            info = svg_utils.parse_layers(path)
    except Exception:
        log.exception("layer normalization failed for %s", path.name)
    if info["layers"]:
        return info
    # vpype's `write` infers the output format from the file extension, so
    # the temp file must still end in .svg.
    tmp = path.with_name(f"{path.stem}.tmp.svg")
    try:
        svg_optimize.normalize_layers(path, tmp)
    except svg_optimize.OptimizeError:
        tmp.unlink(missing_ok=True)
        return info
    # vpype exits 0 but writes no file at all when the document has nothing
    # plottable in it ("no geometry to save, no file created") — an SVG that is
    # empty, or holds only text/images. Without this check the replace() below
    # raises FileNotFoundError, which isn't an OptimizeError, so it escapes as
    # a 500 instead of the caller's clean "no layers" rejection.
    if not tmp.exists():
        return info
    tmp.replace(path)
    try:
        return svg_utils.parse_layers(path)
    except Exception:
        return info


# The plot worker raises RuntimeError with a stable message; map the known ones
# to codes, and fall back to a generic code that carries the raw text.
_WORKER_ERROR_CODES: dict[str, str] = {
    "No active plot": "no_active_plot",
    "No paused job to resume": "no_paused_job",
    "No paused job to redraw": "redraw_not_paused",
    "No resume data": "no_resume_data",
    "Nothing to continue": "nothing_to_continue",
    "Calibration plot only available at a pen-change pause": "calibrate_not_at_pause",
    "Origin nudge only available at a pen-change pause": "nudge_not_at_pause",
    "Optical registration only available at a pen-change pause": "optical_reg_not_at_pause",
    "Camera is not calibrated for optical registration": "optical_reg_not_calibrated",
    "Manual jog only available while idle": "jog_not_idle",
    # Not failures — the UI turns these two into a confirm/cancel prompt and
    # retries with confirm_below_origin set.
    "Jog would go above or left of the origin": "jog_below_origin",
    "Nudge would go above or left of the origin": "nudge_below_origin",
    "This job has no calibration layers": "no_calibration_layers",
    "Invalid calibration filename": "invalid_calibration_filename",
    "Calibration file not found": "calibration_file_not_found",
    "Pen height can only be live-adjusted at a pen-change pause": "pen_height_not_at_pause",
    "Plotter is not actively plotting": "not_plotting",
    "No active job": "no_active_job",
    "Plotter busy": "plotter_busy",
    "Could not connect to the plotter. Check that it is powered on and plugged in.": "cannot_connect",
    "Camera is not enabled": "camera_not_enabled",
    "A recording is already in progress": "recording_in_progress",
    "Could not start recording (MediaMTX unreachable)": "camera_unreachable",
    "Could not reach the camera service (MediaMTX)": "camera_unreachable",
    "Invalid autofocus mode": "invalid_af_mode",
}


def _worker_error(e: RuntimeError) -> HTTPException:
    code = _WORKER_ERROR_CODES.get(str(e))
    if code:
        return _coded(409, code)
    return _coded(409, "worker_error", detail=str(e))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    state.init(asyncio.get_running_loop())
    optimize_queue.start()
    plan_queue.start()
    optimize_expert_queue.start()
    optimize_queue.bootstrap_from_disk()
    plan_queue.bootstrap_from_state()
    camera.apply_camera_settings()
    camera.stop_orphaned_recording()
    # Scratch files leaked by a measurement the last shutdown interrupted.
    ink_cache.sweep_orphaned_temps(UPLOAD_DIR)
    drain_task = asyncio.create_task(state.drain_events())
    try:
        yield
    finally:
        await asyncio.get_running_loop().run_in_executor(None, plot_worker.shutdown_gracefully)
        # Tear down preprocessing workers after the plot worker so any
        # in-flight upload pre-opt or background plan finishes cleanly when
        # nothing else is competing for CPU.
        plan_queue.shutdown()
        optimize_queue.shutdown()
        optimize_expert_queue.shutdown()
        drain_task.cancel()


app = FastAPI(lifespan=lifespan)

# SVG is XML: the most compressible thing this server owns, and the biggest.
# An 8MB drawing goes over the wire at 2.78MB, a 4.5MB one at 0.88MB, and
# app.js at roughly a quarter of its size. Nothing about the bytes changes —
# the browser reconstructs them exactly, and the plot never touches this path
# at all, since the machine is driven from the file on disk.
#
# 1KB floor so the compression does not cost more than it saves on the small
# JSON the UI polls with.
#
# Level 1, not Starlette's default of 9. That default is a bad trade on this
# hardware and was measured making the largest files *slower to arrive than
# sending them uncompressed* — total time to deliver an 8MB drawing over a
# 40Mb/s link:
#
#     level 1     0.28s to compress   3.19 MB    0.92s   <- chosen
#     none        0.00s               8.04 MB    1.61s
#     level 6     1.46s               2.78 MB    2.02s
#     level 9     2.63s               2.75 MB    3.18s
#
# SVG is repetitive enough that level 1 already captures most of the ratio —
# 39.7% of the original against 34.3% at level 9 — and the CPU it does not
# spend is CPU the plotter and the background queues are competing for.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=1)


class _ImmutableStatic(StaticFiles):
    """Static assets, cached hard.

    Asset URLs already carry ``__ASSET_VERSION__`` (see the index handler), so
    a file's URL changes whenever the file does. That makes the content
    genuinely immutable per URL, and the default — an ETag with no
    Cache-Control — means the browser instead asks about every asset on every
    load and waits for four 304s before it can start.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


app.mount("/static", _ImmutableStatic(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    # Stamp the running version onto the asset URLs so a browser always fetches
    # fresh app.js / style.css after a self-update instead of serving a stale
    # cached copy. Read per request so static edits show up without a restart.
    html = (STATIC_DIR / "index.html").read_text()
    return HTMLResponse(html.replace("__ASSET_VERSION__", config.APP_VERSION))


@app.get("/draw-stream")
def draw_stream_page():
    # Standalone page for an OBS Browser Source — not part of the SPA, so it
    # gets its own route rather than living under the immutably-cached
    # /static mount (see _ImmutableStatic below).
    html = (STATIC_DIR / "draw-stream.html").read_text()
    return HTMLResponse(html.replace("__ASSET_VERSION__", config.APP_VERSION))


@app.get("/draw-stream/trace")
def draw_stream_trace():
    # Streamed (see state.draw_trace_snapshot_lines) rather than built into one
    # JSON response: a multi-hour job's trace can be hundreds of thousands of
    # points, and materializing that in memory here defeats the point of
    # having moved it to disk in the first place.
    return StreamingResponse(state.draw_trace_snapshot_lines(),
                             media_type="application/x-ndjson")


# SVG storage -------------------------------------------------------------

@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    data = await _read_capped(file)
    # Sniff the bytes before writing: SVG starts with '<' (optionally after a
    # UTF-8 BOM and whitespace). Anything binary (JPG/PNG/PDF/...) fails fast
    # with a clean message rather than the lxml parse trace.
    head = data.lstrip(b"\xef\xbb\xbf").lstrip()
    if not head.startswith(b"<"):
        raise _coded(400, "not_svg")
    svg_id = uuid.uuid4().hex[:8]
    path = UPLOAD_DIR / f"{svg_id}.svg"
    path.write_bytes(data)
    try:
        info = svg_utils.parse_layers(path)
    except Exception:
        path.unlink(missing_ok=True)
        raise _coded(400, "invalid_svg")
    info = _normalize_svg_layers(path, info)
    # Nothing downstream can use a layerless SVG, and the browser used to
    # discover that for itself and abandon the upload — leaving the file (and
    # a queued optimize task for it) behind forever. Reject it here, before
    # anything is enqueued, so the file goes away with the request.
    if not info["layers"]:
        path.unlink(missing_ok=True)
        raise _coded(400, "no_layers")
    # Record the name the user knows this drawing by. The file on disk is named
    # after a uuid fragment, and the job record's copy of the filename dies with
    # the job — so without this the library can only ever show "a792743a".
    state.set_upload_meta(svg_id, file.filename or "upload.svg")
    optimize_queue.enqueue_for_upload(svg_id)
    # Start measuring now, in the background. The size readout and the bounds
    # overlay both want this, and on a real drawing it takes long enough that
    # beginning at upload rather than at first glance is the difference between
    # the number being there and the user watching a spinner.
    ink_cache.request(path)
    return {"id": svg_id, "filename": file.filename or "upload.svg", **info}


# Draw-stream background image ---------------------------------------------
# A single shared "paper" photo/texture, used as the /draw-stream page's
# background (see app/main.py's draw_stream_page). Fixed filename rather than
# a job-scoped id: there is exactly one background image at a time, shared
# across jobs. Presence of the file *is* the on/off state — no separate
# enabled flag to keep in sync.

_DRAW_STREAM_BG_STEM = "draw_stream_background"
_DRAW_STREAM_BG_EXTS = (("png", "image/png"), ("jpg", "image/jpeg"))


@app.post("/draw-stream/background")
async def upload_draw_stream_background(file: UploadFile = File(...)):
    data = await _read_capped(file)
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "png"
    elif data[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    else:
        raise _coded(400, "not_image")
    for old_ext, _ in _DRAW_STREAM_BG_EXTS:
        (UPLOAD_DIR / f"{_DRAW_STREAM_BG_STEM}.{old_ext}").unlink(missing_ok=True)
    (UPLOAD_DIR / f"{_DRAW_STREAM_BG_STEM}.{ext}").write_bytes(data)
    return {"ok": True}


@app.get("/draw-stream/background")
def get_draw_stream_background():
    for ext, media in _DRAW_STREAM_BG_EXTS:
        path = UPLOAD_DIR / f"{_DRAW_STREAM_BG_STEM}.{ext}"
        if path.exists():
            return FileResponse(path, media_type=media)
    raise HTTPException(404)


@app.delete("/draw-stream/background")
def delete_draw_stream_background():
    for ext, _ in _DRAW_STREAM_BG_EXTS:
        (UPLOAD_DIR / f"{_DRAW_STREAM_BG_STEM}.{ext}").unlink(missing_ok=True)
    return {"ok": True}


@app.get("/svg/{svg_id}")
def get_svg(svg_id: str):
    path = UPLOAD_DIR / f"{svg_id}.svg"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(str(path), media_type="image/svg+xml")


@app.get("/jobs/{job_id}/svg")
def get_job_svg(job_id: str):
    """The SVG the job would actually plot right now — the optimized .opt.svg
    when "Optimize SVG" is on and its cache is ready, otherwise the raw
    upload. Lets the on-screen preview show the same geometry that gets sent
    to the machine instead of always the pre-optimization original."""
    job = state.get_job(job_id)
    if job is None:
        raise HTTPException(404)
    path = plot_worker._effective_svg_path(job)
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(str(path), media_type="image/svg+xml")


@app.get("/jobs/{job_id}/export")
def export_job(job_id: str, fmt: str, bg: str = "white", placed: bool = False):
    """Download the job's processed drawing in another file format.

    ``placed=false`` (default): the same file GET /jobs/{id}/svg serves — the
    optimized .opt.svg when optimization has run, else the raw upload — in the
    document's own coordinates, no placement or layer filtering.

    ``placed=true``: the drawing rendered as a plot would lay it down —
    selected layers only, positioned on the page by the job's settings, then
    sheared by the *active machine's* axis skew (see app/export.py's
    build_placed_svg). This is the post-processing output for driving another
    plotter directly.

    Synchronous on purpose — FastAPI runs it in a worker thread, so the
    blocking vpype/cairosvg call doesn't stall the event loop.
    """
    job = state.get_job(job_id)
    if job is None:
        raise HTTPException(404)
    if fmt not in export.FORMATS:
        raise _coded(422, "export_bad_format")
    src = plot_worker._effective_svg_path(job)
    if not src.exists():
        raise HTTPException(404)

    ext = export.extension(fmt)
    if placed:
        if not any(s.get("selected", True) for s in job.get("layer_selections", [])):
            raise _coded(422, "select_one_layer")
        # Skew is a machine correction — it belongs only in a toolpath that
        # drives the machine (G-code / HPGL). SVG / PNG / PDF are pictures of
        # the drawing and stay square. Placement + transform apply to all.
        apply_skew = fmt in ("gcode", "hpgl")
        convert_src = UPLOAD_DIR / (
            f"{job['svg_id']}.export.placed-src{'' if apply_skew else '-flat'}.svg")
        try:
            export.build_placed_svg(job, src, convert_src, apply_skew=apply_skew)
        except export.ExportError as e:
            raise _coded(422, "export_failed", message=str(e))
        out = UPLOAD_DIR / f"{job['svg_id']}.export.placed.{ext}"
    else:
        convert_src = src
        out = UPLOAD_DIR / f"{job['svg_id']}.export.{ext}"

    try:
        export.export(convert_src, out, fmt,
                      transparent=(fmt == "png" and bg == "transparent"))
    except export.ExportError as e:
        raise _coded(422, "export_failed", message=str(e))

    meta = state.get_upload_meta(job["svg_id"]) or {}
    name = job.get("filename") or meta.get("filename") or f"{job['svg_id']}.svg"
    stem = Path(name).stem
    suffix = ".placed" if placed else ""
    return FileResponse(str(out), media_type=export.media_type(fmt),
                        filename=f"{stem}{suffix}.{ext}")


# Library ------------------------------------------------------------------
#
# The uploads folder, addressable. Every SVG that has ever been uploaded stays
# on disk after its job is deleted, and nothing reclaimed it — an empty queue
# sat on top of 19MB of drawings and their derivatives, and every restart
# re-ran vpype over all of it (see optimize_queue.bootstrap_from_disk).
#
# Deliberately not solved by sweeping at startup. These files are the user's
# own artwork; deleting it to reclaim space is the wrong default. So the folder
# is made visible and the decisions are handed over: replot a previous drawing,
# delete one, or empty everything nothing is using.
#
# A drawing with a cached optimization appears twice — the original and the
# optimized copy — because they plot differently and choosing between them is
# the point. Selecting the optimized row promotes it to a source of its own
# (see _promote_optimized) so the rest of the pipeline needs to know nothing
# about variants.

LIBRARY_SOURCE = "source"
LIBRARY_OPTIMIZED = "optimized"


def _library_svg_ids() -> set[str]:
    """Every svg_id the uploads folder holds, derived from the filenames.

    An upload is ``<svg_id>.svg`` where svg_id is a uuid fragment with no dot
    in it; everything else in the folder is a derivative of one
    (``.opt.svg``, ``.preview.svg``, ``.s0.filt.svg``, …). Both shapes are
    scanned, because a ``.opt.svg`` can outlive the source it came from.
    """
    ids: set[str] = set()
    if not UPLOAD_DIR.exists():
        return ids
    for path in UPLOAD_DIR.glob("*.svg"):
        if not path.is_file():
            continue
        # Leading dot means a scratch file mid-rename (see svg_optimize._partial).
        # Splitting one of those yields an empty id, which globs as ".*" and
        # would put every hidden file in the folder behind a single delete
        # button. They are transient and belong to no drawing — skip them; the
        # startup sweep in ink_cache is what removes the leaked ones.
        if path.name.startswith("."):
            continue
        svg_id = path.name[:-len(".svg")].split(".", 1)[0]
        if svg_id:
            ids.add(svg_id)
    return ids


def _jobs_by_svg_id() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for job in state.snapshot()["queue"]:
        out.setdefault(job.get("svg_id"), []).append(job["job_id"])
    return out


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _entry_bytes(svg_id: str, variant: str) -> int:
    """Every byte the given row is responsible for.

    A source row owns its derivatives — deleting it takes the whole
    ``{svg_id}.*`` family with it (see delete_svg_files), so reporting only the
    source file would understate what the row reclaims by several times.
    The optimized row is scoped to the one file it names, since deleting it
    leaves the source and its other derivatives alone.
    """
    if variant == LIBRARY_OPTIMIZED:
        return _size_of(UPLOAD_DIR / f"{svg_id}.opt.svg")
    return sum(_size_of(p) for p in UPLOAD_DIR.glob(f"{svg_id}.*")
               if p.name != f"{svg_id}.opt.svg")


def _library_entries() -> list[dict]:
    jobs = _jobs_by_svg_id()
    meta = state.all_upload_meta()
    entries: list[dict] = []
    for svg_id in sorted(_library_svg_ids()):
        source = UPLOAD_DIR / f"{svg_id}.svg"
        optimized = UPLOAD_DIR / f"{svg_id}.opt.svg"
        info = meta.get(svg_id) or {}
        # No metadata means the file predates the library, or was written by
        # something other than /upload. The svg_id is a poor name but an honest
        # one — better than inventing a filename that was never real.
        filename = info.get("filename") or f"{svg_id}.svg"
        job_ids = jobs.get(svg_id, [])
        common = {
            "svg_id": svg_id,
            "filename": filename,
            "uploaded_at": info.get("uploaded_at") or 0.0,
            "pre_optimized": bool(info.get("pre_optimized")),
            "derived_from": info.get("derived_from"),
            "job_ids": job_ids,
            "in_use": bool(job_ids),
        }
        if source.is_file():
            entries.append({
                **common,
                "key": svg_id,
                "variant": LIBRARY_SOURCE,
                "size_bytes": _entry_bytes(svg_id, LIBRARY_SOURCE),
                "modified_at": source.stat().st_mtime,
            })
        if optimized.is_file():
            entries.append({
                **common,
                "key": f"{svg_id}:opt",
                "variant": LIBRARY_OPTIMIZED,
                "size_bytes": _entry_bytes(svg_id, LIBRARY_OPTIMIZED),
                "modified_at": optimized.stat().st_mtime,
                # An optimized file is never itself in use: jobs reference the
                # source and resolve to this one through _effective_svg_path.
                # Deleting it only costs a re-run, so it is never protected.
                "in_use": False,
                "job_ids": [],
            })
    entries.sort(key=lambda e: e["modified_at"], reverse=True)
    return entries


@app.get("/library")
def get_library():
    entries = _library_entries()
    return {
        "entries": entries,
        "total_bytes": sum(e["size_bytes"] for e in entries),
        "reclaimable_bytes": sum(e["size_bytes"] for e in entries if not e["in_use"]),
    }


class LibrarySelect(BaseModel):
    svg_id: str
    variant: Literal["source", "optimized"] = LIBRARY_SOURCE


def _promote_optimized(svg_id: str) -> str:
    """Make ``{svg_id}.opt.svg`` a source in its own right, and return its id.

    Everything downstream — the plot worker, the placement endpoint, the ink
    cache — resolves a job's document as ``uploads/{svg_id}.svg``. Rather than
    teach all of them about a second variant, the optimized file is copied to
    its own upload id once and behaves like any other drawing from then on.

    Copied once, not per selection: a previous promotion of the same parent is
    reused if its file is still there, so replotting an optimized drawing five
    times costs one copy rather than five.
    """
    for candidate, info in state.all_upload_meta().items():
        if (info.get("derived_from") == svg_id
                and (UPLOAD_DIR / f"{candidate}.svg").is_file()):
            return candidate

    src = UPLOAD_DIR / f"{svg_id}.opt.svg"
    if not src.is_file():
        raise _coded(404, "svg_not_found")
    new_id = uuid.uuid4().hex[:8]
    dst = UPLOAD_DIR / f"{new_id}.svg"
    try:
        shutil.copyfile(src, dst)
    except OSError:
        log.exception("library: could not promote %s", src.name)
        raise _coded(500, "library_promote_failed")

    parent = state.get_upload_meta(svg_id) or {}
    stem = Path(parent.get("filename") or f"{svg_id}.svg").stem
    state.set_upload_meta(new_id, f"{stem} (optimized).svg",
                          pre_optimized=True, derived_from=svg_id)
    # Deliberately no optimize_queue.enqueue_for_upload: this file is already
    # what vpype produced, and running it through again would simplify
    # already-simplified geometry.
    ink_cache.request(dst)
    return new_id


_LAYER_MODE_SUFFIX = {"group": "groups", "pen": "pen width"}


def _regroup_svg(source_svg_id: str, mode: str) -> tuple[str, dict]:
    """Resolve the svg_id + parsed layers a job should use for ``layer_mode``.

    ``layer`` — the upload itself. ``group`` / ``pen`` — a standalone
    re-partitioned copy under its own id (see app/layer_group.py), reused
    across mode switches and hidden from the library like a promoted optimize
    copy. Falls back to the source whenever re-grouping would change nothing or
    yields nothing usable.
    """
    src = UPLOAD_DIR / f"{source_svg_id}.svg"
    if mode not in ("group", "pen"):
        return source_svg_id, svg_utils.parse_layers(src)

    # "group" only bites when a single wrapper group hides the real structure —
    # otherwise it is identical to "layer" and a copy would be dead weight.
    if mode == "group":
        try:
            root = etree.parse(str(src)).getroot()
        except Exception:
            raise _coded(422, "invalid_svg")
        if len([el for el in root if el.tag == svg_utils.LAYER_TAG]) != 1:
            return source_svg_id, svg_utils.parse_layers(src)

    tag = f"{source_svg_id}:mode:{mode}"
    for cand, meta in state.all_upload_meta().items():
        if meta.get("derived_from") == tag and (UPLOAD_DIR / f"{cand}.svg").is_file():
            return cand, svg_utils.parse_layers(UPLOAD_DIR / f"{cand}.svg")

    new_id = uuid.uuid4().hex[:8]
    dst = UPLOAD_DIR / f"{new_id}.svg"
    try:
        layer_group.regroup(src, mode, dst)
    except layer_group.RegroupError as e:
        dst.unlink(missing_ok=True)
        raise _coded(422, "regroup_failed", reason=str(e))
    info = svg_utils.parse_layers(dst)
    # Nothing usable, or "group" that just re-labelled a lone layer — not worth
    # a standalone copy; behave like "layer".
    if not info["layers"] or (mode == "group" and len(info["layers"]) < 2):
        dst.unlink(missing_ok=True)
        return source_svg_id, svg_utils.parse_layers(src)
    parent = state.get_upload_meta(source_svg_id) or {}
    stem = Path(parent.get("filename") or f"{source_svg_id}.svg").stem
    state.set_upload_meta(new_id, f"{stem} ({_LAYER_MODE_SUFFIX[mode]}).svg",
                          derived_from=tag)
    ink_cache.request(dst)
    return new_id, info


@app.post("/library/select")
def library_select(req: LibrarySelect):
    """Resolve a library row into something a job can be built from.

    Returns the svg_id to use plus the same layer/size payload ``/upload``
    returns, so the browser builds a job from a previous drawing by exactly the
    path it uses for a fresh one.
    """
    svg_id = req.svg_id
    if req.variant == LIBRARY_OPTIMIZED:
        svg_id = _promote_optimized(svg_id)

    path = UPLOAD_DIR / f"{svg_id}.svg"
    if not path.is_file():
        raise _coded(404, "svg_not_found")
    try:
        info = svg_utils.parse_layers(path)
    except Exception:
        raise _coded(422, "invalid_svg")
    if not info["layers"]:
        raise _coded(400, "no_layers")

    meta = state.get_upload_meta(svg_id) or {}
    ink_cache.request(path)
    return {
        "id": svg_id,
        "filename": meta.get("filename") or f"{svg_id}.svg",
        "pre_optimized": bool(meta.get("pre_optimized")),
        **info,
    }


@app.delete("/library/{svg_id}")
def library_delete(svg_id: str, variant: Literal["source", "optimized"] = LIBRARY_SOURCE):
    """Remove one library row.

    A source still referenced by a job is refused rather than deleted along
    with the job: losing a queued job as a side effect of tidying the folder is
    not something a delete button should be able to do.
    """
    if svg_id not in _library_svg_ids():
        raise _coded(404, "svg_not_found")
    if variant == LIBRARY_OPTIMIZED:
        opt = UPLOAD_DIR / f"{svg_id}.opt.svg"
        if not opt.is_file():
            raise _coded(404, "svg_not_found")
        optimize_queue.cancel(svg_id)
        try:
            opt.unlink()
        except OSError:
            log.exception("library: could not delete %s", opt.name)
            raise _coded(500, "library_delete_failed")
        return {"ok": True, "removed": 1}

    job_ids = _jobs_by_svg_id().get(svg_id, [])
    if job_ids:
        raise _coded(409, "library_in_use", count=len(job_ids))
    delete_svg_files(svg_id)
    state.drop_upload_meta(svg_id)
    return {"ok": True, "removed": 1}


@app.post("/library/clean")
def library_clean():
    """Delete every drawing no job is using. Files a job references survive.

    Re-checked per file rather than against one snapshot taken up front: this
    loop can run long enough for a job to be queued against a not-yet-visited
    svg_id, and a stale "in use" set would let that card's file be deleted out
    from under it.
    """
    kept = 0
    removed = 0
    freed = 0
    for svg_id in sorted(_library_svg_ids()):
        if svg_id in _jobs_by_svg_id():
            kept += 1
            continue
        freed += (_entry_bytes(svg_id, LIBRARY_SOURCE)
                  + _entry_bytes(svg_id, LIBRARY_OPTIMIZED))
        delete_svg_files(svg_id)
        state.drop_upload_meta(svg_id)
        removed += 1
    return {"ok": True, "removed": removed, "kept": kept, "freed_bytes": freed}


_svg_meta_cache: "OrderedDict[tuple, dict]" = OrderedDict()
_SVG_META_CACHE_MAX = 32


def _with_complexity(info: dict, path: Path) -> dict:
    """``info`` plus the complexity analysis, when one exists for this file.

    Merged at response time rather than cached with the rest: the analysis is
    computed on a worker thread after the fact, so it can arrive later than the
    parse it belongs to. Only ever *peeked* at — a drawing earns an analysis by
    defeating the preview (see plan_queue/plot_worker), not by being looked at.
    """
    data = svg_complexity.peek(path)
    if data is None:
        return info
    return {**info, "complexity": data}


@app.get("/jobs/{job_id}/svg-meta")
def get_job_svg_meta(job_id: str):
    """Layer list and page size for the SVG that ``/jobs/{job_id}/svg`` serves.

    The browser used to reconstruct this itself: fetch the document, run it
    through DOMParser a second time, walk the top-level groups for their
    labels, and read width/height/viewBox off the root. That is a full parse of
    a multi-megabyte file on the UI thread — measured at 281ms on a 2.4MB
    curved drawing and 368ms on a 4.5MB hatched one, blocking everything for
    the duration — to recompute six fields ``svg_utils.parse_layers`` already
    produces, field for field, and already returns from ``/upload``.

    Deliberately resolved through ``_effective_svg_path``, the same way the
    document itself is: with "Optimize SVG" on, the served file is the
    optimized one, whose layer sequence vpype can change (see
    ``reconcile_layers``). Describing the raw upload instead would hand the
    layer panel indices that do not match the artwork it is displaying — and
    those indices choose what gets plotted.
    """
    job = state.get_job(job_id)
    if job is None:
        raise HTTPException(404)
    path = plot_worker._effective_svg_path(job)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        raise HTTPException(404)

    key = (str(path), stamp)
    with _doc_geom_lock:
        hit = _svg_meta_cache.get(key)
        if hit is not None:
            _svg_meta_cache.move_to_end(key)
            return _with_complexity(hit, path)
    try:
        info = svg_utils.parse_layers(path)
    except etree.XMLSyntaxError:
        raise _coded(422, "invalid_svg")
    with _doc_geom_lock:
        _svg_meta_cache[key] = info
        _svg_meta_cache.move_to_end(key)
        while len(_svg_meta_cache) > _SVG_META_CACHE_MAX:
            _svg_meta_cache.popitem(last=False)
    return _with_complexity(info, path)


# Placement ----------------------------------------------------------------
#
# The server is the authority on where ink lands. It has to be: the browser
# can only measure the artwork with getBBox(), which counts live text and
# raster images that vpype drops on the way to the plotter, so a client that
# worked it out for itself was answering a different question than the machine.
# The preview asks this instead of deriving its own answer.


class PlacementQuery(BaseModel):
    """Placement inputs as the editor currently has them — which is not
    necessarily what the job record says, since the preview updates while the
    user is still dragging and the PATCH is still debounced."""
    paper_width_mm: float = Field(..., gt=0)
    paper_height_mm: float = Field(..., gt=0)
    margin_top_mm: float = 0.0
    margin_right_mm: float = 0.0
    margin_bottom_mm: float = 0.0
    margin_left_mm: float = 0.0
    fit_content: bool = False
    transform_scale: float = 1.0
    transform_rotation_deg: float = 0.0
    transform_offset_x_mm: float = 0.0
    transform_offset_y_mm: float = 0.0
    layer_indices: list[int] | None = None
    # Off by default, and that default is load-bearing. Placement itself is
    # arithmetic over the document's size and viewBox — microseconds. The ink
    # rectangle needs vpype to re-read the whole file, which on a real drawing
    # is seconds or minutes, not milliseconds — 10s for a 4.5MB hatched plot on
    # a Pi 4, and far worse for curves. The preview needs only the former, so
    # asking for both in one request left the canvas blank until the slow half
    # finished. See app/ink_cache.py, which now answers this from memory.
    include_ink: bool = False


_doc_geom_lock = threading.Lock()
_doc_geom_cache: "OrderedDict[tuple, tuple]" = OrderedDict()
_DOC_GEOM_CACHE_MAX = 64


def _doc_geometry(path: Path, stamp: int) -> tuple:
    """The document's own size and viewBox: (width_mm, height_mm, viewbox).

    Cached because reading three attributes off the root element still costs a
    full lxml parse, and on a 4.5MB drawing that is ~76ms — per request, on a
    path that fires on every slider tick. The values cannot change without the
    file changing, so the mtime is the whole cache key.
    """
    key = (str(path), stamp)
    with _doc_geom_lock:
        hit = _doc_geom_cache.get(key)
        if hit is not None:
            _doc_geom_cache.move_to_end(key)
            return hit
    try:
        root = etree.parse(str(path)).getroot()
    except etree.XMLSyntaxError:
        # A document that will not parse is not a server fault, and answering
        # 500 turns it into an unexplained red box in the UI. The optimized
        # file is written atomically now, so this should mean a genuinely
        # malformed upload rather than one caught mid-write.
        raise _coded(422, "invalid_svg")
    doc_w_mm, doc_h_mm = svg_utils.svg_size_mm(root)
    geom = (doc_w_mm, doc_h_mm, svg_utils.parse_viewbox(root.get("viewBox", "")))
    with _doc_geom_lock:
        _doc_geom_cache[key] = geom
        _doc_geom_cache.move_to_end(key)
        while len(_doc_geom_cache) > _DOC_GEOM_CACHE_MAX:
            _doc_geom_cache.popitem(last=False)
    return geom


@app.post("/jobs/{job_id}/placement")
def get_job_placement(job_id: str, req: PlacementQuery):
    """Resolve where this job's artwork lands, under the placement the caller
    is currently previewing.

    Returns the placement itself (so the preview can position the artwork with
    the same numbers the plot will use) and the ink rectangle (so the size
    readout and the bounds overlay report what actually gets drawn).
    """
    job = state.get_job(job_id)
    if job is None:
        raise _coded(404, "job_not_found")
    path = plot_worker._effective_svg_path(job)
    if not path.exists():
        raise _coded(404, "svg_not_found")

    indices = req.layer_indices
    if indices is None:
        indices = [s["index"] for s in job.get("layer_selections") or []
                   if s.get("selected", True)]

    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        raise _coded(404, "svg_not_found")
    doc_w_mm, doc_h_mm, viewbox = _doc_geometry(path, stamp)
    place = placement.compute(
        doc_w_mm, doc_h_mm, viewbox,
        req.paper_width_mm, req.paper_height_mm,
        req.margin_top_mm, req.margin_right_mm,
        req.margin_bottom_mm, req.margin_left_mm,
        req.fit_content,
        transform_scale=req.transform_scale,
        transform_rotation_deg=req.transform_rotation_deg,
        transform_offset_x_mm=req.transform_offset_x_mm,
        transform_offset_y_mm=req.transform_offset_y_mm,
        machine_auto_rotate=config.MACHINE_AUTO_ROTATE,
    )

    # `ink: null` means two different things, and the caller has to be able to
    # tell them apart: "this document draws nothing" versus "not measured yet".
    # ink_measured says which.
    #
    # Never measured inline. The read behind this takes up to 75 seconds on a
    # real drawing, and occupying a request thread for that long — repeatedly,
    # because the UI re-asks on every state broadcast — is what brought the Pi
    # down. ink_cache answers from memory or starts one background measurement
    # and says "not yet"; the client asks again.
    measured, rect = ink_cache.rect_for(path, indices) if req.include_ink else (False, None)
    if rect is None:
        ink = None
    else:
        left, top, right, bottom = place.doc_mm_rect_to_page(*rect)
        # float() on the way out: vpype measures with numpy, and while
        # np.float64 subclasses float and serializes fine today, the wire
        # contract here is plain JSON numbers and shouldn't depend on that.
        ink = {"left_mm": float(left), "top_mm": float(top),
               "width_mm": float(right - left), "height_mm": float(bottom - top)}

    # Every layer's pen-down length, not just the selected ones — the layer
    # panel shows a per-layer time estimate for each row regardless of which
    # are currently checked. Same cached read as `ink`, so this costs nothing
    # extra once it's measured.
    layer_lengths_mm = None
    if req.include_ink:
        _, lengths = ink_cache.lengths_for(path)
        # float() for the same reason as `ink` above: vpype measures with
        # numpy, and the wire contract is plain JSON numbers.
        if lengths is not None:
            layer_lengths_mm = {i: float(v) for i, v in lengths.items()}

    return {
        "doc_width_mm": doc_w_mm,
        "doc_height_mm": doc_h_mm,
        "rotation_deg": place.rotation_deg,
        "fit_scale": place.fit_scale,
        "user_scale": place.user_scale,
        # The unrotated, fit-scaled size of the document — the box the preview
        # lays its <svg> out at before rotating it. Returned rather than left
        # for the client to multiply out, so the browser derives no geometry
        # of its own and simply renders what it is told.
        "layout_width_mm": (doc_w_mm or req.paper_width_mm) * place.fit_scale,
        "layout_height_mm": (doc_h_mm or req.paper_height_mm) * place.fit_scale,
        "footprint_width_mm": place.footprint_w_mm,
        "footprint_height_mm": place.footprint_h_mm,
        "center_x_mm": place.center_x_mm,
        "center_y_mm": place.center_y_mm,
        # None when the selected layers hold nothing plottable — a document of
        # live text or raster images. The UI says so rather than showing a
        # size for artwork that will not be drawn. Also None whenever
        # include_ink was false; ink_measured distinguishes the two.
        "ink": ink,
        "ink_measured": measured,
        "layer_lengths_mm": layer_lengths_mm,
    }


# Jobs -------------------------------------------------------------------
#
# The optimize_svg_* fields appear in three shapes:
#   - JobCreate (web POST):           required-with-defaults
#   - JobUpdate (web PATCH):          all-Optional, no defaults
#   - ApiJobMetadata (public POST):   all-Optional, server fills missing values
# Two mixins capture the variants so we don't repeat the field list three times.

class _OptimizeCreateFields(BaseModel):
    optimize_svg: bool = False
    optimize_svg_tolerance_mm: float = 0.10
    optimize_svg_linemerge: bool = True
    optimize_svg_linesimplify: bool = True
    optimize_svg_linesort: bool = True
    optimize_svg_reloop: bool = True
    # "beginner" runs the toggles above automatically at plot start. "expert"
    # instead uses the three raw-command boxes below, run explicitly via
    # POST /jobs/{id}/optimize-expert/execute ahead of time — plot start does
    # not re-run vpype for an expert-mode job (see plot_worker._effective_svg_path).
    optimize_mode: Literal["beginner", "expert"] = "beginner"
    optimize_expert_1_enabled: bool = False
    optimize_expert_1_cmd: str = ""
    optimize_expert_2_enabled: bool = False
    optimize_expert_2_cmd: str = ""
    optimize_expert_3_enabled: bool = False
    optimize_expert_3_cmd: str = ""


class _OptimizeOptionalFields(BaseModel):
    optimize_svg: bool | None = None
    optimize_svg_tolerance_mm: float | None = None
    optimize_svg_linemerge: bool | None = None
    optimize_svg_linesimplify: bool | None = None
    optimize_svg_linesort: bool | None = None
    optimize_svg_reloop: bool | None = None
    optimize_mode: Literal["beginner", "expert"] | None = None
    optimize_expert_1_enabled: bool | None = None
    optimize_expert_1_cmd: str | None = None
    optimize_expert_2_enabled: bool | None = None
    optimize_expert_2_cmd: str | None = None
    optimize_expert_3_enabled: bool | None = None
    optimize_expert_3_cmd: str | None = None


class JobCreate(_OptimizeCreateFields):
    svg_id: str
    filename: str = "upload.svg"
    # Set when the source is a copy promoted out of a .opt.svg (see
    # /library/select). Its geometry has already been through vpype, so the
    # optimize step is forced off and the UI locks the panel — running
    # linesimplify over already-simplified paths only loses more of the
    # original curve.
    pre_optimized: bool = False
    name: str | None = None
    paper_size_name: str | None = None
    paper_name: str | None = None
    layer_selections: list[dict]
    # How the layer rows / plot stages are derived from the SVG. "group" and
    # "pen" re-partition the drawing into a standalone copy the job points at
    # (see _regroup_svg); switchable per job while it's being prepared.
    layer_mode: Literal["layer", "group", "pen"] = "layer"
    pause_between_layers: bool = True
    delete_on_complete: bool = False
    disable_motors_on_complete: bool = False
    paper_width_mm: float
    paper_height_mm: float
    margin_top_mm: float = 0.0
    margin_right_mm: float = 0.0
    margin_bottom_mm: float = 0.0
    margin_left_mm: float = 0.0
    fit_content: bool = False
    transform_scale: float = 1.0
    transform_rotation_deg: float = 0.0
    transform_offset_x_mm: float = 0.0
    transform_offset_y_mm: float = 0.0
    speed_pendown: int = 25
    speed_penup: int = 75
    acceleration: int = 75
    pen_pos_up: int = 60
    pen_pos_down: int = 30
    record_plot: bool = False
    record_mode: Literal["realtime", "timelapse", "sped_up"] | None = None
    record_timelapse_interval_s: float | None = None
    record_speed_multiplier: float | None = None
    optical_reg: bool = False


class MoveRequest(BaseModel):
    new_index: int = Field(..., ge=0)


class MachineProfile(BaseModel):
    """One entry in the machine list. `id` is absent for a machine the UI has
    just added; config assigns one (see config._normalize_machine)."""
    id: str | None = None
    name: str = Field(..., min_length=1, max_length=60)
    width_mm: float = Field(..., gt=0)
    height_mm: float = Field(..., gt=0)
    auto_rotate: Literal["off", "portrait", "landscape"] = "off"
    # Measured axis-skew angle and which axis it's measured relative to.
    # Persisted per machine and applied to plot geometry — see
    # config.MACHINE_SKEW_DEG / app.axis_skew.
    skew_deg: float = Field(0.0, ge=-config.MACHINE_SKEW_DEG_MAX,
                            le=config.MACHINE_SKEW_DEG_MAX)
    skew_true_axis: Literal["x", "y"] = "x"
    # How the plot worker reacts if correcting skew_deg would push ink past
    # the page edge — see config.MACHINE_SKEW_MODE / app.axis_skew.
    skew_mode: Literal["clip", "absorb"] = "clip"


class SettingsUpdate(BaseModel):
    plotter_model: int | None = Field(None, ge=1, le=8)
    pause_between_layers_default: bool | None = None
    layer_mode_default: Literal["layer", "group", "pen"] | None = None
    delete_on_complete_default: bool | None = None
    disable_motors_on_complete_default: bool | None = None
    speed_pendown_default: int | None = Field(None, ge=1, le=110)
    speed_penup_default: int | None = Field(None, ge=1, le=110)
    acceleration_default: int | None = Field(None, ge=1, le=100)
    pen_pos_up_default: int | None = Field(None, ge=29, le=85)
    pen_pos_down_default: int | None = Field(None, ge=29, le=85)
    optimize_svg_default: bool | None = None
    optimize_svg_tolerance_default_mm: float | None = Field(None, ge=0.01, le=10.0)
    optimize_svg_linemerge_default: bool | None = None
    optimize_svg_linesimplify_default: bool | None = None
    optimize_svg_linesort_default: bool | None = None
    optimize_svg_reloop_default: bool | None = None
    display_unit: Literal["mm", "cm", "in"] | None = None
    move_shortcut_x_mm: float | None = Field(None, ge=0.0, le=2000.0)
    move_shortcut_y_mm: float | None = Field(None, ge=0.0, le=2000.0)
    move_shortcut_set_origin: bool | None = None
    # Replaces the old machine_custom_enabled/machine_width_mm/
    # machine_height_mm/machine_auto_rotate quartet: a bed size is a property
    # of a named machine now, not a global override (see config.MACHINES).
    machines: list[MachineProfile] | None = None
    active_machine_id: str | None = None
    webhook_url: str | None = None
    webhook_on_layer_complete: bool | None = None
    webhook_on_job_complete: bool | None = None
    camera_enabled: bool | None = None
    camera_resolution_width: int | None = Field(None, gt=0)
    camera_resolution_height: int | None = Field(None, gt=0)
    camera_fps: int | None = Field(None, ge=1, le=120)
    camera_bitrate: int | None = Field(None, gt=0)
    camera_af_mode: Literal["auto", "manual", "continuous"] | None = None
    camera_lens_position: float | None = Field(None, ge=0.0, le=32.0)
    camera_af_speed: Literal["normal", "fast"] | None = None
    camera_brightness: float | None = Field(None, ge=-1.0, le=1.0)
    camera_contrast: float | None = Field(None, ge=0.0, le=16.0)
    camera_saturation: float | None = Field(None, ge=0.0, le=16.0)
    camera_sharpness: float | None = Field(None, ge=0.0, le=16.0)
    camera_ev: float | None = Field(None, ge=-10.0, le=10.0)
    camera_exposure_mode: Literal["normal", "short", "long", "custom"] | None = None
    camera_shutter_us: int | None = Field(None, ge=0)
    camera_flicker_mode: Literal["off", "50hz", "60hz"] | None = None
    camera_awb_mode: Literal["auto", "incandescent", "tungsten", "fluorescent",
                             "indoor", "daylight", "cloudy"] | None = None
    camera_gain: float | None = Field(None, ge=0.0)
    camera_denoise: Literal["off", "cdn_off", "cdn_fast", "cdn_hq"] | None = None
    camera_hflip: bool | None = None
    camera_vflip: bool | None = None
    camera_output_folder: str | None = None
    camera_rclone_target: str | None = None
    camera_rclone_delete_local: bool | None = None
    camera_recording_mode_default: Literal["realtime", "timelapse", "sped_up"] | None = None
    camera_timelapse_interval_s_default: float | None = Field(None, gt=0)
    camera_speed_multiplier_default: float | None = Field(None, gt=1.0)
    record_plot_default: bool | None = None
    optical_reg_default: bool | None = None
    optical_reg_mm_per_px: float | None = Field(None, ge=0.0)
    optical_reg_cam_rotation_deg: float | None = Field(None, ge=-180.0, le=180.0)
    optical_reg_cam_offset_x_mm: float | None = Field(None, ge=-50.0, le=50.0)
    optical_reg_cam_offset_y_mm: float | None = Field(None, ge=-50.0, le=50.0)
    optical_reg_mark_x_mm: float | None = Field(None, ge=0.0)
    optical_reg_mark_y_mm: float | None = Field(None, ge=0.0)
    optical_reg_mark_size_mm: float | None = Field(None, ge=0.5, le=20.0)
    optical_reg_probe_offset_mm: float | None = Field(None, ge=0.2, le=20.0)
    optical_reg_probe_offset_max_mm: float | None = Field(None, ge=0.5, le=40.0)
    optical_reg_frames: int | None = Field(None, ge=1, le=15)
    optical_reg_max_correction_mm: float | None = Field(None, ge=0.1, le=20.0)
    draw_stream_enabled: bool | None = None
    draw_stream_stroke_width_px: int | None = Field(None, ge=1, le=40)
    draw_stream_background: Literal["black", "white"] | None = None
    draw_stream_max_resolution_px: int | None = Field(None, ge=480, le=4096)


# Numeric job fields that get clamped on create/update. Out-of-range values
# are silently corrected to the nearest bound rather than 400'd — a slider
# overshoot or stale client default shouldn't break the request, the user
# clearly wanted the value at the limit.
_CLAMP_RANGES: dict[str, tuple[float, float]] = {
    "speed_pendown": (1, 110),
    "speed_penup": (1, 110),
    "acceleration": (1, 100),
    "pen_pos_up": (29, 85),
    "pen_pos_down": (29, 85),
    "record_timelapse_interval_s": (0.5, 3600.0),
    "record_speed_multiplier": (1.1, 60.0),
    "transform_scale": (0.01, 5.0),
    "transform_rotation_deg": (-360.0, 360.0),
    "optimize_svg_tolerance_mm": (0.01, 10.0),
}


# Job fields that must never end up None. They're Optional on JobUpdate so a
# PATCH can leave them alone, but an *explicit* null is a different thing: it
# survives exclude_unset, gets written to the record, and later reaches
# ad.options.speed_pendown as None, where the driver raises a TypeError that
# isn't the IndexError _run_staged_loop_impl catches — killing the worker
# thread and stranding the job in `plotting`. The web UI produces exactly this
# when a number field is cleared (parseInt("") is NaN, which JSON-encodes as
# null), so drop nulls rather than 400 on them: the user emptied a box, they
# didn't ask to unset the speed.
_NON_NULLABLE_JOB_FIELDS = frozenset({
    "speed_pendown", "speed_penup", "acceleration",
    "pen_pos_up", "pen_pos_down",
    "paper_width_mm", "paper_height_mm",
    "margin_top_mm", "margin_right_mm", "margin_bottom_mm", "margin_left_mm",
    "transform_scale", "transform_rotation_deg",
    "transform_offset_x_mm", "transform_offset_y_mm",
    "fit_content", "pause_between_layers", "layer_mode",
    "delete_on_complete", "disable_motors_on_complete", "record_plot",
    "optical_reg", "layer_selections",
    "optimize_svg", "optimize_svg_tolerance_mm", "optimize_svg_linemerge",
    "optimize_svg_linesimplify", "optimize_svg_linesort", "optimize_svg_reloop",
    "optimize_mode", "optimize_expert_1_enabled", "optimize_expert_1_cmd",
    "optimize_expert_2_enabled", "optimize_expert_2_cmd",
    "optimize_expert_3_enabled", "optimize_expert_3_cmd",
})


def _drop_null_job_fields(d: dict) -> None:
    for key in list(d):
        if d[key] is None and key in _NON_NULLABLE_JOB_FIELDS:
            del d[key]


def _clamp_job_fields(d: dict,
                      paper_width_mm: float | None = None,
                      paper_height_mm: float | None = None) -> None:
    """In-place clamp of numeric job fields. ``paper_*_mm`` (when known)
    bounds the transform offsets to the paper extent."""
    _drop_null_job_fields(d)
    for key, (lo, hi) in _CLAMP_RANGES.items():
        v = d.get(key)
        if v is not None:
            d[key] = max(lo, min(hi, v))
    for k in ("margin_top_mm", "margin_right_mm",
              "margin_bottom_mm", "margin_left_mm"):
        v = d.get(k)
        if v is not None:
            d[k] = max(0.0, v)
    if paper_width_mm:
        v = d.get("transform_offset_x_mm")
        if v is not None:
            d["transform_offset_x_mm"] = max(-paper_width_mm,
                                             min(paper_width_mm, v))
    if paper_height_mm:
        v = d.get("transform_offset_y_mm")
        if v is not None:
            d["transform_offset_y_mm"] = max(-paper_height_mm,
                                             min(paper_height_mm, v))


class JobUpdate(_OptimizeOptionalFields):
    layer_selections: list[dict] | None = None
    layer_mode: Literal["layer", "group", "pen"] | None = None
    name: str | None = None
    paper_size_name: str | None = None
    paper_name: str | None = None
    pause_between_layers: bool | None = None
    delete_on_complete: bool | None = None
    disable_motors_on_complete: bool | None = None
    paper_width_mm: float | None = None
    paper_height_mm: float | None = None
    margin_top_mm: float | None = None
    margin_right_mm: float | None = None
    margin_bottom_mm: float | None = None
    margin_left_mm: float | None = None
    fit_content: bool | None = None
    transform_scale: float | None = None
    transform_rotation_deg: float | None = None
    transform_offset_x_mm: float | None = None
    transform_offset_y_mm: float | None = None
    speed_pendown: int | None = None
    speed_penup: int | None = None
    acceleration: int | None = None
    pen_pos_up: int | None = None
    pen_pos_down: int | None = None
    record_plot: bool | None = None
    record_mode: Literal["realtime", "timelapse", "sped_up"] | None = None
    record_timelapse_interval_s: float | None = None
    record_speed_multiplier: float | None = None
    optical_reg: bool | None = None


@app.post("/jobs")
def create_job(req: JobCreate):
    path = UPLOAD_DIR / f"{req.svg_id}.svg"
    if not path.exists():
        raise _coded(404, "svg_not_found")
    if not any(s.get("selected", True) for s in (req.layer_selections or [])):
        raise _coded(400, "select_one_layer")
    payload = req.model_dump()
    # Enforced here rather than trusted from the client, so an API caller can't
    # queue a double-optimization either.
    if payload.get("pre_optimized"):
        payload["optimize_svg"] = False
    # Non-default layer_mode: point the job at a re-partitioned copy and take
    # its rows from that (see _regroup_svg). source_svg_id remembers the upload
    # so a later mode switch always re-groups from the original.
    payload["source_svg_id"] = payload["svg_id"]
    if payload.get("layer_mode", "layer") != "layer":
        sid, info = _regroup_svg(payload["svg_id"], payload["layer_mode"])
        payload["svg_id"] = sid
        payload["layer_selections"] = [
            {"index": l["index"], "label": l["label"]} for l in info["layers"]
        ]
    _clamp_job_fields(payload, payload.get("paper_width_mm"),
                      payload.get("paper_height_mm"))
    job = state.add_job(payload)
    optimize_queue.enqueue_for_job(job)
    plan_queue.enqueue(job)
    return job


@app.get("/jobs")
def list_jobs():
    return state.snapshot()


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    j = state.get_job(job_id)
    if j is None:
        raise HTTPException(404)
    return j


@app.patch("/jobs/{job_id}")
def update_job(job_id: str, req: JobUpdate):
    j = state.get_job(job_id)
    if j is None:
        raise _coded(404, "job_not_found")
    if j["status"] not in ("ready", "completed", "failed", "cancelled"):
        raise _coded(409, "cannot_edit_active")
    # exclude_unset so the client distinguishes "not sent" from "explicitly null"
    # — needed e.g. for paper_size_name which can be cleared back to None.
    updates = req.model_dump(exclude_unset=True)
    # Use the new paper dims if they're being changed; otherwise fall back to
    # the existing job's so transform offsets clamp against the right extent.
    paper_w = updates.get("paper_width_mm", j.get("paper_width_mm"))
    paper_h = updates.get("paper_height_mm", j.get("paper_height_mm"))
    # A pre-optimized source can never be optimized again, whatever a PATCH
    # asks for — see JobCreate.pre_optimized.
    if j.get("pre_optimized"):
        updates["optimize_svg"] = False
    # Switching layer_mode re-points the job at a re-partitioned copy (or back
    # at the original for "layer") and rebuilds its rows from that. The
    # estimate reset + re-enqueue below is exactly what this needs.
    new_mode = updates.get("layer_mode")
    if new_mode is not None and new_mode != j.get("layer_mode", "layer"):
        src_id = j.get("source_svg_id", j["svg_id"])
        sid, info = _regroup_svg(src_id, new_mode)
        updates["svg_id"] = sid
        updates["source_svg_id"] = src_id
        if "layer_selections" not in updates:
            updates["layer_selections"] = [
                {"index": l["index"], "label": l["label"]} for l in info["layers"]
            ]
    _clamp_job_fields(updates, paper_w, paper_h)
    # Editing a finished/cancelled job makes it runnable again, so the user can
    # re-plot without any extra step. One already sitting at `ready` is already
    # there.
    if j["status"] != "ready":
        updates["status"] = "ready"
        updates["error"] = None
    # Any edit can change the preview cache key, so the on-record estimate is
    # potentially stale. Drop it; plan_queue will recompute.
    updates.update({
        "estimated_total_seconds": None,
        "progress_total_seconds": None,
        "distance_pendown_m": None,
        "distance_total_m": None,
        "pen_lifts": None,
        "plan_status": None,
        "plan_error": None,
        "run_elapsed_seconds": 0.0,
    })
    _persist_expert_defaults(updates)
    state.update_job(job_id, **updates)
    # Renaming a job also renames the drawing it came from in the library, so
    # the two stay in step. Upload metadata isn't part of the state broadcast —
    # the browser re-pulls /library after a name PATCH.
    if "name" in updates:
        src_id = updates.get("source_svg_id") or j.get("source_svg_id", j["svg_id"])
        state.rename_upload(src_id, updates["name"] or j.get("filename") or f"{src_id}.svg")
    fresh = state.get_job(job_id)
    plan_queue.cancel(job_id)
    optimize_queue.enqueue_for_job(fresh)
    plan_queue.enqueue(fresh)
    return fresh


class OptimizeExpertExecute(BaseModel):
    """Body of POST /jobs/{id}/optimize-expert/execute — the three expert-mode
    boxes' current on-screen state, so Execute always runs exactly what's
    displayed rather than whatever was last saved to the job record."""
    optimize_expert_1_enabled: bool = False
    optimize_expert_1_cmd: str = ""
    optimize_expert_2_enabled: bool = False
    optimize_expert_2_cmd: str = ""
    optimize_expert_3_enabled: bool = False
    optimize_expert_3_cmd: str = ""


def _persist_expert_defaults(updates: dict) -> None:
    """Whenever an expert-mode box's command text changes, remember it as the
    pre-fill for the next new job's box (see config.OPTIMIZE_EXPERT_*_CMD_DEFAULT
    and the default-picking in api_create_job; the plain-upload flow prefills
    from these same defaults client-side, in static/app.js buildJobPayload)."""
    kw = {}
    for n in (1, 2, 3):
        key = f"optimize_expert_{n}_cmd"
        if updates.get(key) is not None:
            kw[f"{key}_default"] = updates[key]
    if kw:
        config.update(**kw)


@app.post("/jobs/{job_id}/optimize-expert/execute")
def optimize_expert_execute(job_id: str, req: OptimizeExpertExecute):
    """Run the expert-mode pipeline now, ahead of plotting (see
    plot_worker._run_optimize_phase — an expert-mode job's plot never re-runs
    vpype; this is the only thing that produces/refreshes its .opt.svg)."""
    j = state.get_job(job_id)
    if j is None:
        raise _coded(404, "job_not_found")
    if j["status"] not in ("ready", "completed", "failed", "cancelled"):
        raise _coded(409, "cannot_edit_active")
    box_texts = [
        req.optimize_expert_1_cmd if req.optimize_expert_1_enabled else "",
        req.optimize_expert_2_cmd if req.optimize_expert_2_enabled else "",
        req.optimize_expert_3_cmd if req.optimize_expert_3_enabled else "",
    ]
    if not any(t.strip() for t in box_texts):
        raise _coded(400, "no_command")
    updates = {
        "optimize_expert_1_enabled": req.optimize_expert_1_enabled,
        "optimize_expert_1_cmd": req.optimize_expert_1_cmd,
        "optimize_expert_2_enabled": req.optimize_expert_2_enabled,
        "optimize_expert_2_cmd": req.optimize_expert_2_cmd,
        "optimize_expert_3_enabled": req.optimize_expert_3_enabled,
        "optimize_expert_3_cmd": req.optimize_expert_3_cmd,
    }
    _persist_expert_defaults(updates)
    state.update_job(job_id, **updates)
    optimize_expert_queue.enqueue_execute(
        job_id, j["svg_id"], UPLOAD_DIR / f"{j['svg_id']}.svg", box_texts,
    )
    return {"ok": True}


@app.get("/jobs/{job_id}/optimize-expert/status")
def optimize_expert_status(job_id: str):
    j = state.get_job(job_id)
    if j is None:
        raise _coded(404, "job_not_found")
    return optimize_expert_queue.get_status(job_id)


def delete_svg_files(svg_id: str | None) -> None:
    # Delete the source SVG and every derivative (preview / filtered / staged /
    # resume). svg_id is a uuid4 fragment, 1:1 with a job, so globbing on it
    # can't hit another job's files.
    if not svg_id:
        return
    # Drop any pending or in-flight preprocessing first so the workers don't
    # race us by writing a fresh .opt.svg / .preview.svg right after we unlink.
    optimize_queue.cancel(svg_id)
    plan_queue.cancel_for_svg(svg_id)
    for p in UPLOAD_DIR.glob(f"{svg_id}.*"):
        try:
            p.unlink()
        except OSError:
            log.exception("delete_svg_files: failed to unlink %s", p)


def _cancel_svg_work(svg_id: str | None) -> None:
    """Drop pending/in-flight preprocessing for an SVG without touching files.

    Split out of delete_svg_files because removing a job has to stop the work
    queued on its behalf even though the drawing itself now survives.
    """
    if not svg_id:
        return
    optimize_queue.cancel(svg_id)
    plan_queue.cancel_for_svg(svg_id)


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    """Remove the job. The drawing stays in the library.

    It used to take the SVG with it, which was right when the uploads folder
    was invisible and a file had no purpose beyond its job. Now that the
    library exists to replot a previous drawing, that coupling made it useless:
    a drawing could only be reselected while a job still referenced it, so
    tidying the queue silently emptied the library too.

    Reclaiming disk is the library's own job — delete a row, or Clean up, both
    of which say plainly that a drawing is about to be destroyed. What still
    deletes the file with the job is `delete_on_complete`, because that one is
    the user asking for exactly that (see plot_worker._run_staged_loop_impl).
    """
    j = state.get_job(job_id)
    if j is None:
        raise _coded(404, "job_not_found")
    if j["status"] in ("plotting", "planning", "paused", "awaiting_pen_change", "homing"):
        raise _coded(409, "cannot_remove_active")
    svg_id = j.get("svg_id")
    state.remove_job(job_id)
    _cancel_svg_work(svg_id)
    optimize_expert_queue.forget(job_id)
    return {"ok": True}


# Public API (v1) -----------------------------------------------------------
# Routes under /api/v1/* are intended for external clients (e.g. the macOS
# companion app). They require the X-API-Key header.
#
# The web UI uses the unprefixed routes above, which have NO authentication.
# The service binds 0.0.0.0:80 (see systemd/plotterosaurus.service), so those
# routes — including GET /settings, which returns this api_key, and
# /system/shutdown — are reachable by anything on the same network. That is a
# deliberate trade for a plotter on a trusted home LAN: it keeps the UI usable
# from a phone with no login. Do not port-forward this to the internet.

def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    if not config.API_KEY:
        raise HTTPException(503, "API key not initialized")
    if x_api_key != config.API_KEY:
        raise HTTPException(401, "invalid or missing X-API-Key")


class ApiPaperSize(BaseModel):
    name: str | None = None
    width: float | None = None
    height: float | None = None
    unit: Literal["mm", "cm", "in"] = "mm"
    orientation: Literal["portrait", "landscape"] | None = None


class ApiPaper(BaseModel):
    """The physical paper stock (brand / colour / weight), as opposed to
    ``ApiPaperSize`` which carries the dimensions. Display-only."""
    name: str | None = None


class ApiPen(BaseModel):
    """The pen loaded for a layer. Display-only."""
    name: str | None = None


class ApiLayer(BaseModel):
    index: int = Field(ge=0)
    name: str | None = None
    # Known types ("pattern", "text", "svg", "calibration", "image", "map",
    # "model") get a dedicated icon in the web UI. Any other value is accepted
    # rather than rejected, and falls back to a generic icon.
    type: str | None = None
    selected: bool | None = None  # None == not specified == default True
    # Optional per-layer speed overrides. When set, they take precedence over
    # the job (document) / system speeds for that layer — see _run_job, which
    # forces per-layer staging when any override is present.
    speed_pendown: int | None = None
    speed_penup: int | None = None
    acceleration: int | None = None
    # Display-only: the pen loaded for this layer, shown after the layer name.
    pen: ApiPen | None = None


class ApiJobMetadata(_OptimizeOptionalFields):
    name: str | None = None
    paper_size: ApiPaperSize | None = None
    # Display-only: the paper stock, shown under the preview next to the size.
    paper: ApiPaper | None = None
    layers: list[ApiLayer] = Field(default_factory=list)
    layer_mode: Literal["layer", "group", "pen"] | None = None
    pause_between_layers: bool | None = None
    delete_on_complete: bool | None = None
    disable_motors_on_complete: bool | None = None
    speed_pendown: int | None = None
    speed_penup: int | None = None
    acceleration: int | None = None
    pen_pos_up: int | None = None
    pen_pos_down: int | None = None
    record_plot: bool | None = None
    record_mode: Literal["realtime", "timelapse", "sped_up"] | None = None
    record_timelapse_interval_s: float | None = None
    record_speed_multiplier: float | None = None
    optical_reg: bool | None = None
    # Request-only directive: when true AND the queue is empty at the moment of
    # the POST, kick off the worker so this job plots immediately. Not stored
    # on the job record.
    auto_plot: bool = False


def _resolve_paper(paper: ApiPaperSize | None,
                   svg_w_mm: float | None,
                   svg_h_mm: float | None) -> tuple[float, float, str | None]:
    """Return (paper_width_mm, paper_height_mm, display_name)."""
    if paper is None:
        # Auto-detect from SVG dimensions, like the web UI does on a fresh upload.
        return float(svg_w_mm or 210.0), float(svg_h_mm or 297.0), None

    factor = LENGTH_UNIT_TO_MM[paper.unit]
    w_mm: float | None = paper.width * factor if paper.width is not None else None
    h_mm: float | None = paper.height * factor if paper.height is not None else None

    if w_mm is None or h_mm is None:
        # Fall back to the named preset.
        if paper.name and paper.name in PAPER_PRESETS:
            pw, ph = PAPER_PRESETS[paper.name]
            w_mm, h_mm = float(pw), float(ph)
        elif paper.name:
            raise HTTPException(400, f"unknown paper preset: {paper.name!r}")
        else:
            raise HTTPException(400, "paper_size requires either width+height or a known name")

    if paper.orientation == "landscape" and w_mm < h_mm:
        w_mm, h_mm = h_mm, w_mm
    elif paper.orientation == "portrait" and w_mm > h_mm:
        w_mm, h_mm = h_mm, w_mm

    return w_mm, h_mm, paper.name


@app.post("/api/v1/jobs", dependencies=[Depends(require_api_key)])
async def api_create_job(file: UploadFile = File(...),
                         metadata: str | None = Form(default=None)):
    # Parse + validate metadata (the part is a JSON string in multipart/form-data).
    if metadata:
        try:
            meta_dict = _json.loads(metadata)
        except _json.JSONDecodeError as e:
            raise HTTPException(400, f"metadata is not valid JSON: {e.msg}")
        try:
            meta = ApiJobMetadata.model_validate(meta_dict)
        except ValidationError as e:
            raise HTTPException(400, f"metadata schema error: {e.errors()}")
    else:
        meta = ApiJobMetadata()

    # Persist the SVG (mirrors /upload, size cap included — an external client
    # is if anything more likely to send something unreasonable than the
    # browser is, and this route runs on the same small board).
    try:
        data = await _read_capped(file)
    except HTTPException:
        raise HTTPException(
            413, f"file exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit")
    svg_id = uuid.uuid4().hex[:8]
    path = UPLOAD_DIR / f"{svg_id}.svg"
    path.write_bytes(data)
    try:
        info = svg_utils.parse_layers(path)
    except Exception as e:
        path.unlink(missing_ok=True)
        raise HTTPException(400, f"invalid SVG: {e}")
    info = _normalize_svg_layers(path, info)
    if not info["layers"]:
        path.unlink(missing_ok=True)
        raise HTTPException(400, "SVG contains no Inkscape layers")
    # Same as /upload: record the name before anything else, so a drawing sent
    # by an API client is as readable in the library as a dropped one.
    state.set_upload_meta(svg_id, file.filename or "upload.svg")
    # Only enqueue once the SVG is known to be usable — an optimize task that
    # outlives the file it was queued for writes a .opt.svg nothing will ever
    # clean up.
    optimize_queue.enqueue_for_upload(svg_id)

    # Non-default layer_mode: swap in a re-partitioned copy the same way
    # create_job does, so layer_selections below are built from its layers.
    source_svg_id = svg_id
    layer_mode = meta.layer_mode or "layer"
    if layer_mode != "layer":
        svg_id, info = _regroup_svg(source_svg_id, layer_mode)

    paper_width_mm, paper_height_mm, paper_name = _resolve_paper(
        meta.paper_size, info.get("width_mm"), info.get("height_mm"),
    )

    # Build layer_selections: include every SVG layer, applying per-layer
    # name/type/selected overrides from metadata (keyed by SVG layer index).
    # Deselected layers are kept in the list with `selected: false` so their
    # name/type metadata survives a UI toggle off-and-on. The worker filters
    # by `selected` when planning the plot.
    overrides = {l.index: l for l in meta.layers}
    layer_selections: list[dict] = []
    for layer in info["layers"]:
        idx = layer["index"]
        ovr = overrides.get(idx)
        sel: dict = {"index": idx, "label": (ovr.name if ovr and ovr.name else layer["label"])}
        if ovr and ovr.type:
            sel["type"] = ovr.type
        if ovr and ovr.selected is False:
            sel["selected"] = False
        if ovr and ovr.pen and ovr.pen.name:
            sel["pen_name"] = ovr.pen.name
        # Optional per-layer speed overrides — clamped to the same ranges as
        # the job-level speeds (out-of-range values corrected, not rejected).
        for key in ("speed_pendown", "speed_penup", "acceleration"):
            val = getattr(ovr, key) if ovr else None
            if val is not None:
                lo, hi = _CLAMP_RANGES[key]
                sel[key] = int(max(lo, min(hi, val)))
        layer_selections.append(sel)

    if not any(s.get("selected", True) for s in layer_selections):
        path.unlink(missing_ok=True)
        raise HTTPException(400, "all layers were deselected")

    def pick(meta_val, default):
        return default if meta_val is None else meta_val

    job_payload = {
        "svg_id": svg_id,
        "source_svg_id": source_svg_id,
        "layer_mode": layer_mode,
        "filename": file.filename or "upload.svg",
        "name": meta.name,
        "paper_size_name": paper_name,
        "paper_name": meta.paper.name if meta.paper else None,
        "layer_selections": layer_selections,
        "pause_between_layers": pick(meta.pause_between_layers, config.PAUSE_BETWEEN_LAYERS_DEFAULT),
        "delete_on_complete": pick(meta.delete_on_complete, config.DELETE_ON_COMPLETE_DEFAULT),
        "disable_motors_on_complete": pick(meta.disable_motors_on_complete,
                                           config.DISABLE_MOTORS_ON_COMPLETE_DEFAULT),
        "paper_width_mm": paper_width_mm,
        "paper_height_mm": paper_height_mm,
        "margin_top_mm": 0.0,
        "margin_right_mm": 0.0,
        "margin_bottom_mm": 0.0,
        "margin_left_mm": 0.0,
        "fit_content": False,
        "transform_scale": 1.0,
        "transform_rotation_deg": 0.0,
        "transform_offset_x_mm": 0.0,
        "transform_offset_y_mm": 0.0,
        "speed_pendown": pick(meta.speed_pendown, config.SPEED_PENDOWN_DEFAULT),
        "speed_penup": pick(meta.speed_penup, config.SPEED_PENUP_DEFAULT),
        "acceleration": pick(meta.acceleration, config.ACCELERATION_DEFAULT),
        "pen_pos_up": pick(meta.pen_pos_up, config.PEN_POS_UP_DEFAULT),
        "pen_pos_down": pick(meta.pen_pos_down, config.PEN_POS_DOWN_DEFAULT),
        "record_plot": pick(meta.record_plot, config.RECORD_PLOT_DEFAULT),
        "record_mode": pick(meta.record_mode, config.CAMERA_RECORDING_MODE_DEFAULT),
        "record_timelapse_interval_s": pick(meta.record_timelapse_interval_s,
                                           config.CAMERA_TIMELAPSE_INTERVAL_S_DEFAULT),
        "record_speed_multiplier": pick(meta.record_speed_multiplier,
                                       config.CAMERA_SPEED_MULTIPLIER_DEFAULT),
        "optical_reg": pick(meta.optical_reg, config.OPTICAL_REG_DEFAULT),
        "optimize_svg": pick(meta.optimize_svg, config.OPTIMIZE_SVG_DEFAULT),
        "optimize_svg_tolerance_mm": pick(meta.optimize_svg_tolerance_mm, config.OPTIMIZE_SVG_TOLERANCE_DEFAULT_MM),
        "optimize_svg_linemerge": pick(meta.optimize_svg_linemerge, config.OPTIMIZE_SVG_LINEMERGE_DEFAULT),
        "optimize_svg_linesimplify": pick(meta.optimize_svg_linesimplify, config.OPTIMIZE_SVG_LINESIMPLIFY_DEFAULT),
        "optimize_svg_linesort": pick(meta.optimize_svg_linesort, config.OPTIMIZE_SVG_LINESORT_DEFAULT),
        "optimize_svg_reloop": pick(meta.optimize_svg_reloop, config.OPTIMIZE_SVG_RELOOP_DEFAULT),
        "optimize_mode": meta.optimize_mode or "beginner",
        "optimize_expert_1_enabled": bool(meta.optimize_expert_1_enabled),
        "optimize_expert_1_cmd": pick(meta.optimize_expert_1_cmd, config.OPTIMIZE_EXPERT_1_CMD_DEFAULT),
        "optimize_expert_2_enabled": bool(meta.optimize_expert_2_enabled),
        "optimize_expert_2_cmd": pick(meta.optimize_expert_2_cmd, config.OPTIMIZE_EXPERT_2_CMD_DEFAULT),
        "optimize_expert_3_enabled": bool(meta.optimize_expert_3_enabled),
        "optimize_expert_3_cmd": pick(meta.optimize_expert_3_cmd, config.OPTIMIZE_EXPERT_3_CMD_DEFAULT),
    }
    _clamp_job_fields(job_payload, paper_width_mm, paper_height_mm)
    # auto_plot: only kick the worker if no other job is in a runnable or
    # in-progress state (queued / paused / plotting / planning / optimizing /
    # homing / awaiting_pen_change). Terminal-state leftovers (completed /
    # failed / cancelled) don't block — they're inert.
    _TERMINAL = {"completed", "failed", "cancelled"}
    blockers_present = any(
        j["status"] not in _TERMINAL for j in state.snapshot()["queue"]
    )
    job = state.add_job(job_payload)
    optimize_queue.enqueue_for_job(job)
    plan_queue.enqueue(job)
    if meta.auto_plot and not blockers_present:
        plot_worker.start_plot()
    return job


# Queue control (public) ---------------------------------------------------
# Thin wrappers around the existing /queue/* routes with auth bolted on.

@app.post("/api/v1/queue/plot", dependencies=[Depends(require_api_key)])
def api_queue_plot():
    if not any(j["status"] == "ready" for j in state.snapshot()["queue"]):
        raise HTTPException(409, "no ready job to plot")
    active = state.active_job()
    if active is not None and active["status"] in (
        "plotting", "planning", "paused", "awaiting_pen_change", "homing",
    ):
        raise HTTPException(409, "a job is already plotting")
    plot_worker.start_plot()
    return {"ok": True}


@app.post("/api/v1/queue/pause", dependencies=[Depends(require_api_key)])
def api_queue_pause():
    job = state.active_job()
    if job is None or job["status"] != "plotting":
        raise HTTPException(409, "no active plotting job")
    plot_worker.pause_active()
    return {"ok": True}


@app.post("/api/v1/queue/pause-at-pen-up", dependencies=[Depends(require_api_key)])
def api_queue_pause_at_pen_up():
    return pause_at_pen_up_queue()


@app.post("/api/v1/queue/resume", dependencies=[Depends(require_api_key)])
def api_queue_resume():
    try:
        plot_worker.resume_active()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


class RedrawRequest(BaseModel):
    distance_mm: float


@app.post("/api/v1/queue/redraw", dependencies=[Depends(require_api_key)])
def api_queue_redraw(req: RedrawRequest):
    if not 0 < req.distance_mm <= plot_worker.REDRAW_MAX_MM:
        raise HTTPException(400, "distance_mm out of range")
    try:
        return plot_worker.redraw_recent(req.distance_mm)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/api/v1/queue/continue", dependencies=[Depends(require_api_key)])
def api_queue_continue():
    try:
        plot_worker.continue_next()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/v1/queue/cancel", dependencies=[Depends(require_api_key)])
def api_queue_cancel():
    try:
        plot_worker.cancel_active()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/v1/queue/calibrate", dependencies=[Depends(require_api_key)])
def api_queue_calibrate():
    try:
        plot_worker.trigger_calibration()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


# Per-job CRUD (public) ----------------------------------------------------
# Thin auth-gated wrappers around the internal handlers above.

@app.get("/api/v1/jobs", dependencies=[Depends(require_api_key)])
def api_list_jobs():
    return list_jobs()


@app.post("/api/v1/jobs/{job_id}/placement", dependencies=[Depends(require_api_key)])
def api_get_job_placement(job_id: str, req: PlacementQuery):
    return get_job_placement(job_id, req)


@app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def api_get_job(job_id: str):
    return get_job(job_id)


@app.patch("/api/v1/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def api_update_job(job_id: str, req: JobUpdate):
    return update_job(job_id, req)


@app.delete("/api/v1/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def api_delete_job(job_id: str):
    return delete_job(job_id)


@app.post("/api/v1/jobs/{job_id}/move", dependencies=[Depends(require_api_key)])
def api_move_job(job_id: str, req: MoveRequest):
    return move_job(job_id, req)


@app.post("/api/v1/jobs/{job_id}/requeue", dependencies=[Depends(require_api_key)])
def api_requeue_job(job_id: str):
    return requeue_job(job_id)


# Settings (public) --------------------------------------------------------

@app.get("/api/v1/settings", dependencies=[Depends(require_api_key)])
def api_get_settings():
    # Drop api_key — the caller already needed it to authenticate this request,
    # so re-emitting it would just be noise.
    snap = get_settings()
    snap.pop("api_key", None)
    return snap


@app.patch("/api/v1/settings", dependencies=[Depends(require_api_key)])
def api_patch_settings(req: SettingsUpdate):
    snap = patch_settings(req)
    snap.pop("api_key", None)
    return snap


# System (public) ---------------------------------------------------------

@app.get("/api/v1/version", dependencies=[Depends(require_api_key)])
def api_get_version():
    return get_version()


@app.post("/api/v1/system/shutdown", dependencies=[Depends(require_api_key)])
async def api_system_shutdown():
    return await system_shutdown()


@app.post("/jobs/{job_id}/move")
def move_job(job_id: str, req: MoveRequest):
    j = state.get_job(job_id)
    if j is None:
        raise _coded(404, "job_not_found")
    if j["status"] in ("plotting", "planning", "paused", "awaiting_pen_change", "homing"):
        raise _coded(409, "cannot_move_active")
    state.move_job(job_id, req.new_index)
    return {"ok": True}


@app.post("/jobs/{job_id}/requeue")
def requeue_job(job_id: str):
    j = state.get_job(job_id)
    if j is None:
        raise _coded(404, "job_not_found")
    if j["status"] == "ready":
        return j  # already runnable — nothing to do (idempotent).
    if j["status"] in ("plotting", "planning", "paused", "awaiting_pen_change", "homing"):
        raise _coded(409, "cannot_requeue_running")
    state.update_job(job_id, status="ready", error=None, resume_path=None,
                     started_at=None, plotting_started_at=None,
                     run_elapsed_seconds=0.0,
                     stages=[], current_stage_index=0,
                     plan_status=None, plan_error=None,
                     estimated_total_seconds=None,
                     progress_total_seconds=None,
                     distance_pendown_m=None,
                     distance_total_m=None,
                     pen_lifts=None,
                     jog_hint_dx_mm=None, jog_hint_dy_mm=None)
    fresh = state.get_job(job_id)
    optimize_queue.enqueue_for_job(fresh)
    plan_queue.enqueue(fresh)
    return fresh


# Queue control ----------------------------------------------------------

@app.post("/queue/start")
def start_queue():
    plot_worker.start_plot()
    return {"ok": True}


@app.post("/queue/pause")
def pause_queue():
    job = state.active_job()
    if job is None or job["status"] != "plotting":
        raise _coded(409, "no_active_plotting_job")
    plot_worker.pause_active()
    return {"ok": True}


@app.post("/queue/pause-at-pen-up")
def pause_at_pen_up_queue():
    job = state.active_job()
    if job is None or job["status"] != "plotting":
        raise _coded(409, "no_active_plotting_job")
    try:
        plot_worker.pause_at_pen_lift_active()
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/queue/resume")
def resume_queue():
    try:
        plot_worker.resume_active()
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/queue/redraw")
def redraw_queue(req: RedrawRequest):
    if not 0 < req.distance_mm <= plot_worker.REDRAW_MAX_MM:
        raise _coded(400, "redraw_distance_out_of_range")
    try:
        return plot_worker.redraw_recent(req.distance_mm)
    except RuntimeError as e:
        raise _worker_error(e)


@app.post("/queue/continue")
def continue_queue():
    try:
        plot_worker.continue_next()
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/queue/cancel")
def cancel_queue():
    try:
        plot_worker.cancel_active()
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/queue/calibrate")
def calibrate_queue():
    try:
        plot_worker.trigger_calibration()
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.get("/calibration/files")
def list_calibration_files():
    return {"files": plot_worker.list_calibration_files()}


class CalibrationSelect(BaseModel):
    filename: str


def _promote_calibration_file(filename: str) -> str:
    """Copy a standalone calibration/ SVG into the uploads folder as a source
    of its own, so the normal job-creation pipeline — which only ever resolves
    a job's document as ``uploads/{svg_id}.svg`` — can build a job from it.
    Same idea as _promote_optimized, but the parent lives outside UPLOAD_DIR
    entirely rather than being a cached derivative of one of its own rows.

    Copied once, not per selection: a previous promotion of the same file is
    reused if its upload is still there.
    """
    tag = f"calibration:{filename}"
    for candidate, info in state.all_upload_meta().items():
        if (info.get("derived_from") == tag
                and (UPLOAD_DIR / f"{candidate}.svg").is_file()):
            return candidate

    if filename != Path(filename).name:
        raise _coded(400, "invalid_calibration_filename")
    src = config.CALIBRATION_DIR / filename
    if not src.is_file():
        raise _coded(404, "calibration_file_not_found")

    new_id = uuid.uuid4().hex[:8]
    dst = UPLOAD_DIR / f"{new_id}.svg"
    try:
        shutil.copyfile(src, dst)
    except OSError:
        log.exception("calibration: could not copy %s", src.name)
        raise _coded(500, "calibration_promote_failed")

    state.set_upload_meta(new_id, filename, derived_from=tag)
    ink_cache.request(dst)
    return new_id


@app.post("/calibration/select")
def calibration_select(req: CalibrationSelect):
    """Resolve a calibration-library file into something a job can be built
    from — the same shape /library/select returns, so the browser builds a
    job from it exactly the way it builds one from an upload or a library row.
    """
    svg_id = _promote_calibration_file(req.filename)
    path = UPLOAD_DIR / f"{svg_id}.svg"
    try:
        info = svg_utils.parse_layers(path)
    except Exception:
        raise _coded(422, "invalid_svg")
    if not info["layers"]:
        raise _coded(400, "no_layers")

    meta = state.get_upload_meta(svg_id) or {}
    ink_cache.request(path)
    return {
        "id": svg_id,
        "filename": meta.get("filename") or req.filename,
        "pre_optimized": False,
        **info,
    }


class CalibrateFileRequest(BaseModel):
    filename: str


@app.post("/queue/calibrate-file")
def calibrate_file_queue(req: CalibrateFileRequest):
    try:
        plot_worker.trigger_calibration_file(req.filename)
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.get("/api/v1/calibration/files", dependencies=[Depends(require_api_key)])
def api_list_calibration_files():
    return list_calibration_files()


@app.post("/api/v1/queue/calibrate-file", dependencies=[Depends(require_api_key)])
def api_calibrate_file_queue(req: CalibrateFileRequest):
    return calibrate_file_queue(req)


class NudgeOriginRequest(BaseModel):
    dx_mm: float = 0.0
    dy_mm: float = 0.0
    confirm_below_origin: bool = False


@app.post("/queue/nudge-origin")
def nudge_origin_queue(req: NudgeOriginRequest):
    try:
        plot_worker.nudge_origin(req.dx_mm, req.dy_mm, req.confirm_below_origin)
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/api/v1/queue/nudge-origin", dependencies=[Depends(require_api_key)])
def api_nudge_origin_queue(req: NudgeOriginRequest):
    return nudge_origin_queue(req)


class OpticalRegMeasureRequest(BaseModel):
    # Nominal probe-cross offset; None -> the configured default. The
    # "measure with a bigger offset" button sends a larger value.
    probe_mm: float | None = Field(None, ge=0.2, le=40.0)


@app.post("/queue/optical-reg/measure")
def optical_reg_measure(req: OpticalRegMeasureRequest):
    try:
        plot_worker.trigger_optical_reg(req.probe_mm)
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/api/v1/queue/optical-reg/measure", dependencies=[Depends(require_api_key)])
def api_optical_reg_measure(req: OpticalRegMeasureRequest):
    return optical_reg_measure(req)


@app.get("/camera/optical-reg/preview")
def optical_reg_preview():
    if not camera.OPTICAL_REG_PREVIEW.exists():
        raise HTTPException(404, "no optical-registration preview yet")
    return FileResponse(str(camera.OPTICAL_REG_PREVIEW), media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


@app.post("/optical-reg/calibrate")
def optical_reg_calibrate():
    try:
        result = plot_worker.optical_reg_calibrate()
    except RuntimeError as e:
        raise _worker_error(e)
    return result


@app.post("/api/v1/optical-reg/calibrate", dependencies=[Depends(require_api_key)])
def api_optical_reg_calibrate():
    return optical_reg_calibrate()


class LivePenHeightRequest(BaseModel):
    pen_pos_up: int | None = Field(None, ge=29, le=85)
    pen_pos_down: int | None = Field(None, ge=29, le=85)
    test: Literal["up", "down"]


@app.post("/queue/pen-height")
def live_pen_height_queue(req: LivePenHeightRequest):
    try:
        plot_worker.set_live_pen_heights(req.pen_pos_up, req.pen_pos_down, req.test)
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/api/v1/queue/pen-height", dependencies=[Depends(require_api_key)])
def api_live_pen_height_queue(req: LivePenHeightRequest):
    return live_pen_height_queue(req)


class LivePlotSettingsRequest(BaseModel):
    speed_pendown: int | None = Field(None, ge=1, le=110)
    speed_penup: int | None = Field(None, ge=1, le=110)
    acceleration: int | None = Field(None, ge=1, le=100)
    pen_pos_up: int | None = Field(None, ge=29, le=85)
    pen_pos_down: int | None = Field(None, ge=29, le=85)


@app.post("/queue/live-settings")
def live_plot_settings_queue(req: LivePlotSettingsRequest):
    try:
        plot_worker.set_live_plot_settings(**req.model_dump())
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/api/v1/queue/live-settings", dependencies=[Depends(require_api_key)])
def api_live_plot_settings_queue(req: LivePlotSettingsRequest):
    return live_plot_settings_queue(req)


# Manual pen control -------------------------------------------------------
# Standalone pen up/down, usable any time the plotter isn't actively driving a
# real plot (idle, or paused / awaiting_pen_change) — no job or SVG involved.

@app.post("/pen/up")
def pen_up():
    try:
        plot_worker.manual_pen(raise_pen=True)
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/pen/down")
def pen_down():
    try:
        plot_worker.manual_pen(raise_pen=False)
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/api/v1/pen/up", dependencies=[Depends(require_api_key)])
def api_pen_up():
    return pen_up()


@app.post("/api/v1/pen/down", dependencies=[Depends(require_api_key)])
def api_pen_down():
    return pen_down()


# Manual motor control -------------------------------------------------------
# Standalone motor enable/disable, usable any time the plotter isn't actively
# driving a real plot — lets the carriage be moved by hand while disabled.

@app.post("/motors/enable")
def motors_enable():
    try:
        plot_worker.manual_motors(enable=True)
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/motors/disable")
def motors_disable():
    try:
        plot_worker.manual_motors(enable=False)
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/api/v1/motors/enable", dependencies=[Depends(require_api_key)])
def api_motors_enable():
    return motors_enable()


@app.post("/api/v1/motors/disable", dependencies=[Depends(require_api_key)])
def api_motors_disable():
    return motors_disable()


# Manual jog -------------------------------------------------------------
# Idle-only: walk the pen carriage into position over the paper before a plot
# starts, declare where it sits to be the page corner, and walk it back to
# that corner. Distinct from /queue/nudge-origin above, which corrects an
# active job's remaining stages mid-plot.

class ManualJogRequest(BaseModel):
    dx_mm: float = 0.0
    dy_mm: float = 0.0
    confirm_below_origin: bool = False


@app.post("/pen/jog")
def pen_jog(req: ManualJogRequest):
    try:
        plot_worker.manual_jog(req.dx_mm, req.dy_mm, req.confirm_below_origin)
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/api/v1/pen/jog", dependencies=[Depends(require_api_key)])
def api_pen_jog(req: ManualJogRequest):
    return pen_jog(req)


@app.post("/pen/jog-home")
def pen_jog_home():
    try:
        plot_worker.manual_jog_home()
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/api/v1/pen/jog-home", dependencies=[Depends(require_api_key)])
def api_pen_jog_home():
    return pen_jog_home()


@app.post("/pen/jog-shortcut")
def pen_jog_shortcut():
    try:
        plot_worker.manual_jog_shortcut()
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/api/v1/pen/jog-shortcut", dependencies=[Depends(require_api_key)])
def api_pen_jog_shortcut():
    return pen_jog_shortcut()


@app.post("/pen/set-origin")
def pen_set_origin():
    try:
        plot_worker.set_origin()
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/api/v1/pen/set-origin", dependencies=[Depends(require_api_key)])
def api_pen_set_origin():
    return pen_set_origin()


# Webhook notifications ----------------------------------------------------

@app.post("/webhook/test")
def webhook_test():
    if not config.WEBHOOK_URL:
        raise _coded(409, "webhook_not_configured")
    notify.fire("test", None)
    return {"ok": True}


# Camera / plot recording ---------------------------------------------------
# Manual controls, independent of any job's `record_plot` flag — a manual
# recording isn't tied to a job_id and is finalized under a timestamp-based
# filename. See app/camera.py for the MediaMTX-driving implementation.

class CameraRecordingStart(BaseModel):
    mode: Literal["realtime", "timelapse", "sped_up"] | None = None
    timelapse_interval_s: float | None = Field(None, gt=0)
    speed_multiplier: float | None = Field(None, gt=1.0)


@app.post("/camera/recording/start")
def camera_recording_start(req: CameraRecordingStart):
    try:
        camera.start_recording(None, mode=req.mode,
                               timelapse_interval_s=req.timelapse_interval_s,
                               speed_multiplier=req.speed_multiplier)
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.post("/camera/recording/pause")
def camera_recording_pause():
    camera.pause_recording()
    return {"ok": True}


@app.post("/camera/recording/resume")
def camera_recording_resume():
    camera.resume_recording()
    return {"ok": True}


@app.post("/camera/recording/stop")
def camera_recording_stop():
    camera.stop_recording()
    return {"ok": True}


class CameraFocusRequest(BaseModel):
    af_mode: Literal["auto", "manual", "continuous"]
    lens_position: float = Field(0.0, ge=0.0, le=32.0)


@app.post("/camera/focus")
def camera_focus(req: CameraFocusRequest):
    try:
        camera.set_focus(req.af_mode, req.lens_position)
    except RuntimeError as e:
        raise _worker_error(e)
    return {"ok": True}


@app.get("/camera/status")
def camera_status(request: Request):
    return camera.status(request.url.hostname or "localhost")


@app.post("/api/v1/camera/recording/start", dependencies=[Depends(require_api_key)])
def api_camera_recording_start(req: CameraRecordingStart):
    return camera_recording_start(req)


@app.post("/api/v1/camera/recording/pause", dependencies=[Depends(require_api_key)])
def api_camera_recording_pause():
    return camera_recording_pause()


@app.post("/api/v1/camera/recording/resume", dependencies=[Depends(require_api_key)])
def api_camera_recording_resume():
    return camera_recording_resume()


@app.post("/api/v1/camera/recording/stop", dependencies=[Depends(require_api_key)])
def api_camera_recording_stop():
    return camera_recording_stop()


@app.post("/api/v1/camera/focus", dependencies=[Depends(require_api_key)])
def api_camera_focus(req: CameraFocusRequest):
    return camera_focus(req)


@app.get("/api/v1/camera/status", dependencies=[Depends(require_api_key)])
def api_camera_status(request: Request):
    return camera_status(request)


# Settings ---------------------------------------------------------------

def _settings_payload() -> dict:
    """config.snapshot() plus everything about the machine that is derived
    from the active profile rather than stored beside it.

    The UI needs the reachable envelope to warn about a page that won't fit,
    and its orientation logic reads the same auto-rotate policy the server
    renders with — both come off whichever machine is selected. Derived, not
    stored: kept out of config.snapshot() so config.json keeps exactly one
    copy of the machine and can't end up disagreeing with itself.
    """
    snap = config.snapshot()
    bed_w_mm, bed_h_mm = plot_worker.machine_bounds_mm()
    snap["machine_effective_width_mm"] = bed_w_mm
    snap["machine_effective_height_mm"] = bed_h_mm
    snap["machine_custom_enabled"] = config.MACHINE_CUSTOM_ENABLED
    snap["machine_auto_rotate"] = config.MACHINE_AUTO_ROTATE
    snap["machine_width_mm"] = config.MACHINE_WIDTH_MM
    snap["machine_height_mm"] = config.MACHINE_HEIGHT_MM
    snap["machine_skew_deg"] = config.MACHINE_SKEW_DEG
    snap["machine_skew_true_axis"] = config.MACHINE_SKEW_TRUE_AXIS
    snap["machine_skew_mode"] = config.MACHINE_SKEW_MODE
    return snap


@app.get("/settings")
def get_settings():
    return _settings_payload()


@app.patch("/settings")
def patch_settings(req: SettingsUpdate):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise _coded(400, "no_settings")
    config.update(**updates)
    if any(k.startswith("camera_") for k in updates):
        camera.apply_camera_settings()
    return _settings_payload()


# System -----------------------------------------------------------------

@app.get("/version")
def get_version():
    return {"version": config.APP_VERSION}


# Updates ----------------------------------------------------------------

class UpdateSkip(BaseModel):
    version: str


@app.get("/update/status")
def update_status():
    return updates.get_status()


@app.post("/update/check")
def update_check():
    return updates.get_status(force=True)


@app.post("/update/skip")
def update_skip(req: UpdateSkip):
    # Only honour a skip for the version that's actually the latest, so a stale
    # tab can't suppress a release the user hasn't seen yet.
    status = updates.get_status()
    if req.version != status["latest"]:
        raise _coded(409, "update_skip_mismatch")
    updates.skip(req.version)
    return updates.get_status()


class UpdateApply(BaseModel):
    dry_run: bool = False
    force: bool = False


@app.get("/update/log")
def update_log():
    return {"log": updates.read_log()}


@app.post("/update/apply")
def update_apply(req: UpdateApply):
    # Re-check against the remote so we never kick off a pointless reset/install.
    status = updates.get_status(force=True)
    if not status["update_available"]:
        raise _coded(409, "update_already_current")
    # Don't launch a second updater on top of a running one (e.g. a double-click).
    if updates.update_in_progress():
        raise _coded(409, "update_in_progress")
    # Never restart the service mid-plot — it would wreck the running job.
    if state.snapshot()["status"] != "idle":
        raise _coded(409, "update_busy")
    # The wrapper does `git reset --hard`, which overwrites tracked files. Refuse
    # by default if any are locally modified, but let the UI confirm and retry
    # with force=true (the structured detail lets it show what will be lost).
    if not req.dry_run and not req.force:
        dirty = updates.dirty_files()
        if dirty:
            raise HTTPException(409, detail={
                "reason": "dirty",
                "message": "the app folder has local changes",
                "files": dirty,
            })
    updates.launch(dry_run=req.dry_run)
    return {
        "started": True,
        "dry_run": req.dry_run,
        "target": status["latest"],
    }


@app.post("/system/shutdown")
async def system_shutdown():
    # Delay the halt so the HTTP response flushes to the client first. Requires
    # the service user to have NOPASSWD sudo for /sbin/shutdown (set up by
    # install.sh) and the service's CapabilityBoundingSet to permit CAP_SETUID /
    # CAP_SETGID — otherwise sudo fails with "unable to change to root gid".
    async def _do():
        await asyncio.sleep(1.5)
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "/sbin/shutdown", "-h", "now",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error("shutdown failed: rc=%d stderr=%s",
                      proc.returncode, stderr.decode(errors="replace").strip())
    asyncio.create_task(_do())
    return {"ok": True}


# WebSocket --------------------------------------------------------------

@app.websocket("/ws/state")
async def ws_state(ws: WebSocket):
    await ws.accept()
    state.add_client(ws)
    try:
        await ws.send_json({"type": "state", **state.snapshot()})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        state.remove_client(ws)


@app.websocket("/api/v1/ws/state")
async def api_ws_state(ws: WebSocket):
    # Depends() doesn't work on websocket routes — check the header by hand.
    # Also accept the key as `?api_key=...` for clients that can't easily set
    # custom headers on a WebSocket handshake (e.g. browser WebSocket API).
    api_key = ws.headers.get("x-api-key") or ws.query_params.get("api_key")
    if not api_key or api_key != config.API_KEY:
        # Calling close() before accept() rejects the upgrade — Starlette
        # responds to the handshake with HTTP 403 instead of completing it.
        await ws.close()
        return
    await ws.accept()
    state.add_client(ws)
    try:
        await ws.send_json({"type": "state", **state.snapshot()})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        state.remove_client(ws)
