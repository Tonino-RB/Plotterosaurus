"""Optical layer registration: read a camera frame, return a millimetre offset.

Pure and hardware-free — numpy/uint8 image in, floats out. No camera, no
plotter, no file I/O. ``plot_worker`` grabs the frame (via ``camera``), drives
the carriage, and feeds the result of ``measure`` into ``nudge_origin``; this
module only does the geometry.

The measurement is *differential*. The plotter draws two open crosses near the
same nominal spot — one with the layer-1 pen (the reference, at ``M``), one
with the current pen at a deliberate nominal offset ``O`` (the probe, at
``M + O``) so the two never overlap however different the nib widths are. The
camera (carriage-mounted, so it does not move when the pen is swapped) images
both in one frame. The pixel vector between the two cross centres, minus the
known ``O``, is how far the current pen landed from the reference — i.e. the
correction to dial into the origin nudge.

Two things spare a full camera-to-bed calibration:

- **Differential.** Both crosses sit in the same small patch, so lens
  distortion and perspective are common-mode and cancel in their difference;
  only a local scale (``mm_per_px``) and rotation (``cam_rotation_deg``)
  matter, and those come from one jog-and-look calibration.
- **Line-fit centres.** Each cross centre is the intersection of least-squares
  lines fitted to its two arms, not the centroid of its dark pixels. A thicker
  or inkier arm shifts the centroid; it does not move the fitted arm axis, so
  the centre is width-independent — which is the whole point when pen 1 is a
  1 mm marker and pen 2 a 0.1 mm fineliner.
"""
import math

import numpy as np
from scipy import ndimage

# Paper minus ink has to differ by at least this (0-255) for a frame to hold a
# readable mark — below it there is nothing to fit and detection bails, which
# is how a blank / unfocused / lens-capped frame turns into a clean "no mark".
_MIN_INK_CONTRAST = 25
# A connected component smaller than this (px) is noise, not a cross.
_MIN_COMPONENT_PX = 60
# Each arm of a cross must carry at least this many pixels for a line fit.
_MIN_ARM_PX = 12
_MIN_TOTAL_PX = 30
_REFINE_ITERS = 5
_CONVERGE_PX = 0.02


def _ink_threshold(img: np.ndarray) -> float | None:
    """Grey level below which a pixel is ink, or None if the patch has no
    real dark feature (paper and 'ink' too close to tell apart).

    Ink is a small fraction of any frame, so the split is a fixed ratio of the
    paper level (which the 60th percentile pins down robustly) rather than a
    histogram valley that a sparse mark would not create.
    """
    paper = float(np.percentile(img, 60))
    if float(img.min()) > paper - _MIN_INK_CONTRAST:
        return None
    return 0.55 * paper


def _dark_mask(img: np.ndarray) -> np.ndarray:
    thr = _ink_threshold(img)
    if thr is None:
        return np.zeros(img.shape, dtype=bool)
    return img < thr


def cross_center(img: np.ndarray, roi: tuple[int, int, int, int] | None = None
                 ) -> tuple[float, float, float] | None:
    """Sub-pixel centre of one open cross, as ``(x, y, score)`` in full-image
    pixels, or ``None`` if no usable cross is in ``roi``.

    ``roi`` is ``(y0, y1, x0, x1)`` (defaults to the whole image). ``score`` is
    0..1: arm balance times fit tightness — a clean, symmetric ``+`` scores
    near 1, a lopsided smudge near 0.
    """
    if roi is None:
        y0, y1, x0, x1 = 0, img.shape[0], 0, img.shape[1]
    else:
        y0, y1, x0, x1 = roi
    sub = img[y0:y1, x0:x1].astype(np.float64)
    if sub.size == 0:
        return None
    paper = float(np.percentile(sub, 90))
    if paper - float(sub.min()) < _MIN_INK_CONTRAST:
        return None
    # Ink weight: how far below paper each pixel is. Weighting the fits by this
    # (rather than a hard 0/1 mask) is what makes the centre sub-pixel — a
    # half-covered edge pixel pulls the arm line by half as much, so a mark
    # centred at x.3 does not snap to the pixel grid at x.5.
    weight = np.clip(paper - sub, 0.0, None)
    ink = weight > 0.25 * paper
    ys, xs = np.nonzero(ink)
    if xs.size < _MIN_TOTAL_PX:
        return None
    # +0.5: pixel index i covers [i, i+1); its centre — the sample point the
    # fit reasons about — is at i + 0.5.
    x = xs.astype(np.float64) + 0.5
    y = ys.astype(np.float64) + 0.5
    w = weight[ys, xs]
    rw = np.sqrt(w)

    # Two lines: horizontal arm  y = m_h * x + b_h,  vertical arm  x = m_v * y + b_v.
    # Seed both through the weighted centroid, then alternate: assign each ink
    # pixel to the line it sits closer to, refit (weighted) each line from its
    # pixels, repeat. A thick or heavily-inked arm holds its pixels near its
    # own centreline, so the fit tracks that line and the intersection is
    # width-independent.
    m_h, b_h = 0.0, float(np.average(y, weights=w))
    m_v, b_v = 0.0, float(np.average(x, weights=w))
    cx = cy = 0.0
    nh = nv = 0
    for _ in range(_REFINE_ITERS):
        d_h = np.abs(y - (m_h * x + b_h))
        d_v = np.abs(x - (m_v * y + b_v))
        h = d_h <= d_v
        v = ~h
        nh, nv = int(h.sum()), int(v.sum())
        if nh < _MIN_ARM_PX or nv < _MIN_ARM_PX:
            return None
        m_h, b_h = np.linalg.lstsq(
            np.column_stack([x[h] * rw[h], rw[h]]), y[h] * rw[h], rcond=None)[0]
        m_v, b_v = np.linalg.lstsq(
            np.column_stack([y[v] * rw[v], rw[v]]), x[v] * rw[v], rcond=None)[0]
        denom = 1.0 - m_v * m_h
        if abs(denom) < 1e-9:
            return None
        new_cx = (m_v * b_h + b_v) / denom
        new_cy = m_h * new_cx + b_h
        moved = math.hypot(new_cx - cx, new_cy - cy)
        cx, cy = new_cx, new_cy
        if moved < _CONVERGE_PX:
            break

    if not (0 <= cx <= sub.shape[1] and 0 <= cy <= sub.shape[0]):
        return None

    balance = min(nh, nv) / max(nh, nv)
    resid = math.sqrt((np.sum((y[h] - (m_h * x[h] + b_h)) ** 2)
                       + np.sum((x[v] - (m_v * y[v] + b_v)) ** 2)) / (nh + nv))
    score = balance / (1.0 + resid)
    return float(cx + x0), float(cy + y0), float(score)


def measure(img: np.ndarray, expected_sep_px: tuple[float, float]) -> dict | None:
    """Pixel vector from the reference cross to the probe cross.

    ``expected_sep_px`` is where the probe cross should sit relative to the
    reference (the nominal offset ``O`` rotated/scaled into pixels) — used to
    orient the pair and to sanity-check the reading.

    Returns ``{"sep_px": (dx, dy) | None, "separable": bool,
    "confidence": float}``. ``separable`` is False when the two crosses merged
    into one blob (nibs far too different, offset too small) — the caller then
    retries with a bigger offset. ``None`` means no mark was found at all.
    """
    lbl, n = ndimage.label(_dark_mask(img))
    if n == 0:
        return None
    sizes = ndimage.sum_labels(np.ones_like(lbl, dtype=np.float64), lbl,
                               index=np.arange(1, n + 1))
    big = [int(i) + 1 for i in np.argsort(sizes)[::-1]
           if sizes[i] >= _MIN_COMPONENT_PX]
    if not big:
        return None
    if len(big) < 2:
        return {"sep_px": None, "separable": False, "confidence": 0.0}

    centers, scores = [], []
    for ci in big[:2]:
        cys, cxs = np.nonzero(lbl == ci)
        got = cross_center(img, (int(cys.min()), int(cys.max()) + 1,
                                 int(cxs.min()), int(cxs.max()) + 1))
        if got is None:
            return {"sep_px": None, "separable": False, "confidence": 0.0}
        centers.append(np.array(got[:2]))
        scores.append(got[2])

    a, b = centers
    exp = np.asarray(expected_sep_px, dtype=np.float64)
    if math.hypot(*((b - a) - exp)) <= math.hypot(*((a - b) - exp)):
        ref, probe = a, b
    else:
        ref, probe = b, a
    sep = probe - ref
    scale = max(1.0, math.hypot(*exp))
    dev = math.hypot(*(sep - exp))
    confidence = min(scores) / (1.0 + dev / scale)
    return {"sep_px": (float(sep[0]), float(sep[1])),
            "separable": True, "confidence": float(confidence)}


def px_to_mm(vec_px: tuple[float, float], mm_per_px: float,
             cam_rotation_deg: float) -> tuple[float, float]:
    """Turn an image-space pixel vector into a bed-space millimetre vector."""
    th = math.radians(cam_rotation_deg)
    cos_t, sin_t = math.cos(th), math.sin(th)
    x = mm_per_px * vec_px[0]
    y = mm_per_px * vec_px[1]
    return (cos_t * x - sin_t * y, sin_t * x + cos_t * y)


def mm_to_px(vec_mm: tuple[float, float], mm_per_px: float,
             cam_rotation_deg: float) -> tuple[float, float]:
    """Inverse of :func:`px_to_mm` — the expected pixel separation for a known
    millimetre offset."""
    th = math.radians(-cam_rotation_deg)
    cos_t, sin_t = math.cos(th), math.sin(th)
    x = vec_mm[0] / mm_per_px
    y = vec_mm[1] / mm_per_px
    return (cos_t * x - sin_t * y, sin_t * x + cos_t * y)


def solve_scale_rotation(pairs: list[tuple[tuple[float, float], tuple[float, float]]]
                         ) -> tuple[float, float, float]:
    """Fit the image scale and rotation from jog-and-look samples.

    ``pairs`` is ``[(mm_vec, px_vec), ...]`` — for each known carriage move in
    millimetres, the pixel displacement of a cross centre it produced. Needs at
    least two non-parallel moves. Returns ``(mm_per_px, cam_rotation_deg,
    rms_residual_mm)``; a large residual means the samples disagree (a mirrored
    image from hflip/vflip, or a bad detection) and the caller should reject it.
    """
    rows, rhs = [], []
    for (mx, my), (px, py) in pairs:
        rows.append([px, -py]); rhs.append(mx)
        rows.append([py, px]); rhs.append(my)
    a_mat = np.asarray(rows, dtype=np.float64)
    r_vec = np.asarray(rhs, dtype=np.float64)
    (a, b), *_ = np.linalg.lstsq(a_mat, r_vec, rcond=None)
    mm_per_px = math.hypot(a, b)
    rotation = math.degrees(math.atan2(b, a))
    rms = float(np.sqrt(np.mean((a_mat @ np.array([a, b]) - r_vec) ** 2)))
    return mm_per_px, rotation, rms


def annotate(img: np.ndarray, ref_px: tuple[float, float],
             probe_px: tuple[float, float]) -> np.ndarray:
    """RGB copy of the grey frame with the two centres and the offset vector
    drawn on, for the confirmation preview."""
    rgb = np.stack([img, img, img], axis=-1).astype(np.uint8)
    _draw_line(rgb, ref_px, probe_px, (255, 176, 0))
    _draw_marker(rgb, ref_px, (0, 220, 0))
    _draw_marker(rgb, probe_px, (220, 0, 220))
    return rgb


def _draw_marker(rgb: np.ndarray, p: tuple[float, float], color, arm: int = 12
                 ) -> None:
    h, w = rgb.shape[:2]
    x, y = int(round(p[0])), int(round(p[1]))
    if 0 <= y < h:
        rgb[y, max(0, x - arm):min(w, x + arm + 1)] = color
    if 0 <= x < w:
        rgb[max(0, y - arm):min(h, y + arm + 1), x] = color


def _draw_line(rgb: np.ndarray, p0, p1, color) -> None:
    h, w = rgb.shape[:2]
    x0, y0, x1, y1 = p0[0], p0[1], p1[0], p1[1]
    steps = max(1, int(round(math.hypot(x1 - x0, y1 - y0))))
    for t in np.linspace(0.0, 1.0, steps + 1):
        x, y = int(round(x0 + t * (x1 - x0))), int(round(y0 + t * (y1 - y0)))
        if 0 <= x < w and 0 <= y < h:
            rgb[y, x] = color
