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
from collections import OrderedDict
from pathlib import Path

from . import svg_utils

log = logging.getLogger(__name__)

# Keyed on (path, mtime_ns): the measurement depends on nothing else, and a
# file that changed is a different file.
_CACHE_MAX = 32
_cache: "OrderedDict[tuple, dict[int, tuple]]" = OrderedDict()
_pending: set = set()
_lock = threading.Lock()

_work: "queue.Queue[tuple]" = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


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
    """
    while True:
        key = _work.get()
        try:
            rects = svg_utils.ink_rects_by_layer(Path(key[0]))
        except Exception:
            log.exception("ink_cache: could not measure %s", key[0])
            rects = {}
        with _lock:
            _cache[key] = rects
            _cache.move_to_end(key)
            while len(_cache) > _CACHE_MAX:
                _cache.popitem(last=False)
            _pending.discard(key)


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="ink-measure", daemon=True)
            _worker.start()


def request(path: Path) -> None:
    """Measure ``path`` in the background if it has not been measured already.

    Idempotent and cheap to call repeatedly — which matters, because the UI
    does exactly that.
    """
    key = _key(path)
    if key is None:
        return
    with _lock:
        if key in _cache or key in _pending:
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
        rects = _cache.get(key)
        if rects is not None:
            _cache.move_to_end(key)
    if rects is None:
        request(path)
        return False, None
    return True, svg_utils.union_rect(rects.get(i) for i in layer_indices)
