"""What to tell the user about a drawing too complex to estimate or plot.

A preview killed for exceeding `plot_worker.PREVIEW_RSS_LIMIT_MB` tells the
user the machine cannot measure their drawing. Stopping there just moves the
dead end, so this module answers the follow-up question — which vpype setting
brings the drawing back into range — and answers it cheaply.

It replaces the measurement it protects against. The estimate this stands in
for needs 9-16GB on a 3.7GB board; the analysis below is measured at 4.6s and
151MB on the same 130,990-subpath document, and its cost is O(n) rather than
pyaxidraw's ~n^1.21, so it stays bounded as documents grow.

What it computes is the distance from each subpath end to the nearest end of a
*different* subpath. That is the quantity `linemerge --tolerance` is compared
against, so the fraction of ends falling inside a given tolerance is the
fraction that tolerance can join. On the drawing that prompted this:

    mean gap 0.108mm, median 0.072mm
    0.1mm -> 66.2% of ends joinable     (leaves ~76,900 subpaths)
    1.0mm -> 99.5% of ends joinable     (leaves ~4,300 subpaths)

Advisory, not a prediction. Two models that tried to predict the exact
resulting count were tested and dropped: consecutive-gap ordering ignores that
linemerge searches globally and may reverse lines (predicted 105,114 against
4,259 measured), and KD-tree connected components over-merges because an end
can only ever join one partner (predicted 766, and cost 60s/443MB). Neither is
needed — whatever the user runs, the file is recounted afterwards and the
guard re-decides on the real number.
"""
import logging
import queue
import re
import threading
from collections import OrderedDict
from pathlib import Path

from lxml import etree

from . import svg_utils, workload

log = logging.getLogger(__name__)

# Tolerances offered, in mm. Coarse on purpose: this is a suggestion the user
# types into a panel, not a tuned constant, and the gap distributions that get
# this far are nowhere near tight enough for finer steps to mean anything.
_TOLERANCE_LADDER = (0.1, 0.2, 0.5, 1.0, 2.0, 5.0)

# Fraction of subpath ends a tolerance must be able to join before it is worth
# recommending. 0.995 is where the prompting drawing collapses by ~31x; 0.981
# (0.5mm on that file) was not enough to clear the block.
_TARGET_JOINABLE = 0.99

# Above this the KD-tree query is skipped and only the count is reported.
# Memory here is linear -- endpoints plus one k=6 neighbour query -- and was
# measured at 151MB for 130,990 subpaths, so this caps the analysis near
# ~400MB rather than letting a pathological document trade one crash for
# another.
ANALYSIS_MAX_SUBPATHS = 1_000_000

_NUM = re.compile(r"-?\d*\.?\d+(?:[eE][-+]?\d+)?")

_CACHE_MAX = 16
_cache: "OrderedDict[tuple, dict]" = OrderedDict()
_pending: set = set()
_lock = threading.Lock()
_work: "queue.Queue[tuple]" = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def _key(path: Path) -> tuple | None:
    """Keyed on (path, mtime) exactly as ink_cache is: a file that changed is a
    different file, which is also what makes re-assessment after the user
    optimizes fall out for free rather than needing invalidation."""
    try:
        return (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None


def _endpoints(svg_path: Path):
    """First and last point of every subpath, in user units.

    Only the ends matter, so each subpath is read from its head and tail rather
    than tokenized whole -- the difference between touching a few dozen bytes
    per stroke and parsing half a million coordinate pairs.
    """
    root = etree.parse(str(svg_path)).getroot()
    starts, ends = [], []
    for el in root.iter(f"{{{svg_utils.SVG_NS}}}path"):
        d = el.get("d") or ""
        for chunk in re.split(r"(?=[Mm])", d):
            if not chunk or chunk[0] not in "Mm":
                continue
            head = _NUM.findall(chunk[:64])
            if len(head) < 2:
                continue
            tail = _NUM.findall(chunk[-64:])
            starts.append((float(head[0]), float(head[1])))
            ends.append((float(tail[-2]), float(tail[-1])) if len(tail) >= 2
                        else (float(head[0]), float(head[1])))
    return starts, ends


def _analyze(svg_path: Path) -> dict:
    import numpy as np
    from scipy.spatial import cKDTree

    count = svg_utils.parse_layers(svg_path)["subpath_count"]
    result = {"subpath_count": count, "mean_gap_mm": None, "median_gap_mm": None,
              "recommended_tolerance_mm": None, "joinable_fraction": None}
    if count > ANALYSIS_MAX_SUBPATHS:
        log.info("svg_complexity: %s has %d subpaths, past the analysis cap",
                 svg_path.name, count)
        return result

    starts, ends = _endpoints(svg_path)
    n = len(starts)
    if n < 2:
        return result

    pts = np.vstack([np.array(starts, dtype=np.float64), np.array(ends, dtype=np.float64)])
    owner = np.concatenate([np.arange(n), np.arange(n)])
    tree = cKDTree(pts)
    # k=6: the nearest few neighbours of an end are usually its own partner and
    # duplicates at the same coordinate, so look past them for one belonging to
    # a different subpath.
    dist, idx = tree.query(pts, k=6, workers=-1)
    best = np.full(len(pts), np.inf)
    for k in range(1, dist.shape[1]):
        closer = (owner[idx[:, k]] != owner) & (dist[:, k] < best)
        best[closer] = dist[closer, k]
    # A subpath is joinable at t if *either* end has a partner within t.
    per = np.minimum(best[:n], best[n:]) / svg_utils.PX_PER_MM
    per = per[np.isfinite(per)]
    if not len(per):
        return result

    result["mean_gap_mm"] = float(per.mean())
    result["median_gap_mm"] = float(np.median(per))
    for tol in _TOLERANCE_LADDER:
        frac = float((per <= tol).mean())
        if frac >= _TARGET_JOINABLE:
            result["recommended_tolerance_mm"] = tol
            result["joinable_fraction"] = frac
            break
    else:
        result["recommended_tolerance_mm"] = _TOLERANCE_LADDER[-1]
        result["joinable_fraction"] = float((per <= _TOLERANCE_LADDER[-1]).mean())
    return result


def _run() -> None:
    """One file at a time, sharing the single heavy slot with the ink, optimize
    and plan workers so this can never be the thing that starves a moving pen."""
    workload.deprioritize()
    while True:
        key = _work.get()
        try:
            with workload.heavy("complexity"):
                data = _analyze(Path(key[0]))
        except Exception:
            log.exception("svg_complexity: could not analyze %s", key[0])
            data = {}
        with _lock:
            _cache[key] = data
            _cache.move_to_end(key)
            while len(_cache) > _CACHE_MAX:
                _cache.popitem(last=False)
            _pending.discard(key)


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="svg-complexity", daemon=True)
            _worker.start()


def request(path: Path) -> None:
    """Analyze ``path`` in the background if it hasn't been. Idempotent."""
    key = _key(path)
    if key is None:
        return
    with _lock:
        if key in _cache or key in _pending:
            return
        _pending.add(key)
    _ensure_worker()
    _work.put(key)


def peek(path: Path) -> dict | None:
    """The analysis for ``path`` if it is already known, without starting one.

    The read path uses this rather than ``get``: only a drawing that actually
    defeated the preview is worth analyzing, and that decision belongs to the
    callers that saw it happen, not to whichever request happens to render a
    card first.
    """
    key = _key(path)
    if key is None:
        return None
    with _lock:
        data = _cache.get(key)
        return dict(data) if data is not None else None


def get(path: Path) -> dict | None:
    """The analysis for ``path``, or None while it is still being computed.

    Never blocks: callers render the count they already have and pick the
    recommendation up on a later poll, the same contract ink_cache uses.
    """
    key = _key(path)
    if key is None:
        return None
    with _lock:
        data = _cache.get(key)
        if data is not None:
            _cache.move_to_end(key)
            return dict(data)
    request(path)
    return None
