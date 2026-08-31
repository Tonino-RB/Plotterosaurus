"""Measured ink rectangles, computed once per file and never on a request.

The preview asks where the artwork lands and how big it is. The first half is
arithmetic. The second half needs vpype to read and vectorize the whole
document, which on the 8MB drawings this plotter is actually used for takes
15 to 75 seconds.

That measurement used to run inside the request handler, and the arrangement
was worse than merely slow:

- The browser re-asks on every WebSocket state broadcast, and the plot worker
  broadcasts often. Each ask aborted the previous *fetch*, but aborting a fetch
  does not stop the server thread already inside vpype — so a new 75-second
  parse started while the old ones kept running, several deep.
- FastAPI runs sync handlers in a threadpool 40 wide, so nothing capped how
  many could pile up. Four cores of Raspberry Pi vanish quickly, and the whole
  machine — event loop, plotter serial I/O, the UI — goes with them.
- Each parse wrote a multi-megabyte filtered copy of the document to the SD
  card first, and leaked it if the service restarted mid-measurement.
- The cache key included the selected layers, so toggling a layer bought a
  whole new parse of the same unchanged file.

So: one worker thread, one parse per file ever, every layer measured in that
parse, and the request path reduced to a dictionary lookup. A selection's
rectangle is the union of its layers'. Callers that ask before the answer
exists get told it is not ready rather than made to wait for it, because on
this hardware waiting means a blank canvas for over a minute.
"""
import logging
import queue
import threading
import time
from collections import OrderedDict
from pathlib import Path

from . import svg_utils, workload

log = logging.getLogger(__name__)

# Keyed on (path, mtime_ns): the measurement depends on nothing else, and a
# file that changed is a different file.
_CACHE_MAX = 32
_cache: "OrderedDict[tuple, dict[int, tuple]]" = OrderedDict()
_pending: set = set()
# A Condition rather than a plain Lock so a caller that genuinely cannot carry
# on without an answer can wait for the worker instead of measuring the file
# itself on whatever thread it happens to be on — see rect_for_blocking.
_lock = threading.Condition()

# Files whose measurement raised, as {key: (attempts, monotonic time)}. A
# failure is not the same answer as an empty document: caching {} for one would
# leave a drawing whose parse lost a race to a temp-file collision, or hit a
# momentary memory pinch, permanently reading as "measured, nothing there" with
# no way to retry. So a failure is retried, but not freely — each attempt is a
# full vpype parse, and the UI re-asks on every state broadcast. After
# _FAIL_ATTEMPTS tries the empty result is cached for real, because by then the
# document itself is the likeliest explanation.
_FAIL_ATTEMPTS = 3
_FAIL_RETRY_S = 300.0
_failed: "OrderedDict[tuple, tuple[int, float]]" = OrderedDict()

_work: "queue.Queue[tuple]" = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def sweep_orphaned_temps(uploads: Path) -> int:
    """Delete leaked ``tmp*.svg`` scratch files. Returns how many went.

    ``ink_rect_doc_mm`` writes a filtered copy of the document before reading
    it and unlinks it in a ``finally``. That finally does not run when the
    service is killed mid-parse, and a parse of a real drawing lasts long
    enough that restarts land inside one regularly: 234MB had accumulated.

    Only ``tmp*`` is touched, and only in the uploads directory. Everything
    else there is load-bearing — ``.opt.svg`` is what gets plotted when
    optimization is on, and ``.s0.svg`` / ``.resume.svg`` are how an
    interrupted plot picks up where it stopped. Those are never candidates.
    """
    removed = 0
    # tmp*.svg  — scratch from an interrupted ink measurement.
    # .*.partial.svg — a half-written optimize/normalize output whose rename never
    #              happened (see svg_optimize). The real file, if there is one,
    #              is untouched next to it.
    for pattern in ("tmp*.svg", ".*.partial.svg"):
        for leftover in uploads.glob(pattern):
            try:
                leftover.unlink()
                removed += 1
            except OSError:
                log.debug("ink_cache: could not remove %s", leftover, exc_info=True)
    if removed:
        log.info("ink_cache: removed %d orphaned scratch file(s)", removed)
    return removed


def _key(path: Path) -> tuple | None:
    try:
        return (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None


def _run() -> None:
    """Measure queued files, one at a time, forever.

    Serial on purpose. Two concurrent vpype reads of an 8MB document do not
    finish in half the time on a four-core Pi — they finish in twice the time
    each, while starving the event loop that is meant to be driving a plotter.
    The shared budget in `workload` extends that across the optimize and plan
    queues too, so three drawings uploaded together are measured one at a time
    rather than three abreast.
    """
    workload.deprioritize()
    while True:
        key = _work.get()
        try:
            with workload.heavy("ink"):
                measurements = svg_utils.measure_layers(Path(key[0]))
        except Exception:
            log.exception("ink_cache: could not measure %s", key[0])
            with _lock:
                attempts = _failed.get(key, (0, 0.0))[0] + 1
                _pending.discard(key)
                if attempts < _FAIL_ATTEMPTS:
                    _failed[key] = (attempts, time.monotonic())
                    _failed.move_to_end(key)
                    while len(_failed) > _CACHE_MAX:
                        _failed.popitem(last=False)
                    _lock.notify_all()
                    continue
                _failed.pop(key, None)
                measurements = {}
        with _lock:
            _cache[key] = measurements
            _cache.move_to_end(key)
            while len(_cache) > _CACHE_MAX:
                _cache.popitem(last=False)
            _failed.pop(key, None)
            _pending.discard(key)
            _lock.notify_all()


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="ink-measure", daemon=True)
            _worker.start()


def request(path: Path, force: bool = False) -> None:
    """Measure ``path`` in the background if it has not been measured already.

    Idempotent and cheap to call repeatedly — which matters, because the UI
    does exactly that.

    ``force`` re-attempts a file inside the failure back-off window. The
    back-off is there because the UI re-asks on every state broadcast; a
    caller that asked once, because a person pressed a button, is not what it
    is defending against, and refusing it an attempt turns one unlucky parse
    into five minutes of a plot that will not start.
    """
    key = _key(path)
    if key is None:
        return
    with _lock:
        if key in _cache or key in _pending:
            return
        failure = _failed.get(key)
        if (not force and failure is not None
                and time.monotonic() - failure[1] < _FAIL_RETRY_S):
            return
        _pending.add(key)
    _ensure_worker()
    _work.put(key)


def rect_for(path: Path, layer_indices) -> tuple[bool, tuple | None]:
    """``(measured, rect)`` for the union of ``layer_indices``, in document mm.

    ``measured`` is False when the file has not been read yet; a measurement is
    started and the caller is expected to ask again rather than block. When it
    is True, a rect of None means those layers genuinely draw nothing — a
    document of live text or raster images — which the UI has to be able to
    tell apart from "not yet known".
    """
    key = _key(path)
    if key is None:
        return False, None
    with _lock:
        measurements = _cache.get(key)
        if measurements is not None:
            _cache.move_to_end(key)
    if measurements is None:
        request(path)
        return False, None
    return True, _union(measurements, layer_indices)


def rect_for_blocking(path: Path, layer_indices) -> tuple[bool, tuple | None]:
    """``rect_for``, but waits for the measurement rather than reporting it
    not ready.

    For the callers that cannot carry on without an answer — currently the
    skew "absorb" scale, which has to know the real ink extent before the run
    can pick the size it plots at. Anything on a render path should keep using
    ``rect_for`` and ask again.

    The waiting is the point. These callers used to measure the file inline
    instead, on whatever thread they were on — a full vpype parse of the whole
    document at normal priority, outside `workload`'s single heavy slot, next
    to a pen that was very possibly moving. Worse, the UI had usually queued a
    measurement of the same file already, so the work was done twice. Here the
    one worker does it once, niced and holding the heavy slot, and every
    caller shares the result.

    ``(False, None)`` still comes back when the measurement failed outright,
    since there is then nothing to wait for.
    """
    key = _key(path)
    if key is None:
        return False, None
    request(path, force=True)
    with _lock:
        # Either the answer arrived, or the worker gave up on this file (it
        # discards the key from _pending in both cases) — a measurement that
        # is never coming must not park the caller here forever.
        _lock.wait_for(lambda: key in _cache or key not in _pending)
        measurements = _cache.get(key)
        if measurements is not None:
            _cache.move_to_end(key)
    if measurements is None:
        return False, None
    return True, _union(measurements, layer_indices)


def _union(measurements: dict, layer_indices) -> tuple | None:
    return svg_utils.union_rect(
        (measurements.get(i) or {}).get("rect") for i in layer_indices)


def lengths_for(path: Path) -> tuple[bool, dict[int, float] | None]:
    """``(measured, {layer_index: pendown_length_mm})`` for every layer in the
    document — the per-layer half of the same ``measure_layers`` read
    ``rect_for`` draws its rectangles from. Used to split a job's single
    simulated time estimate across its layers proportionally, without a
    per-layer simulation of its own (see plot_worker.compute_preview)."""
    key = _key(path)
    if key is None:
        return False, None
    with _lock:
        measurements = _cache.get(key)
        if measurements is not None:
            _cache.move_to_end(key)
    if measurements is None:
        request(path)
        return False, None
    return True, {i: v["length_mm"] for i, v in measurements.items()}
