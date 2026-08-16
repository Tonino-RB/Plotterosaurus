"""What placement costs on a document the size of real work.

The preview stopped deriving geometry and now renders whatever the server
returns, which makes the *cost* of that answer a correctness property: a
preview that arrives seven seconds late is a blank canvas, and a blank canvas
is indistinguishable from a broken app. That is not hypothetical. It shipped.

The cause was that `/placement` answered two questions at once. Placement is
arithmetic over the document's size and viewBox — 12 microseconds, no matter
how big the file. The ink rectangle needs vpype to re-read and vectorize the
entire document — 7.7 seconds on a 6MB drawing. Bundling them made every
preview wait on the slow half, and no test noticed, because every fixture in
`fixtures/` is one rectangle in a 400-byte file where both halves are instant.

So these tests are all the same test asked several ways: does the interactive
path stay away from the work that scales? Most are deterministic; two are
stopwatches — see `test_slider_drag_stays_interactive` for why the budgets sit
where they do.
"""
import statistics
import time
from pathlib import Path

import pytest
from lxml import etree

from app import config, main, svg_utils

from .conftest import placement_with_ink
# main.UPLOAD_DIR is read through the module, never from-imported: the
# sandbox fixture in conftest.py rebinds it (see _sandbox_server_state).

FIXTURES = Path(__file__).parent / "fixtures"

A4 = {"paper_width_mm": 210.0, "paper_height_mm": 297.0}
WITH_INK = {"include_ink": True}


def placement(client, job, **overrides):
    res = client.post(f"/jobs/{job['job_id']}/placement", json={**A4, **overrides})
    assert res.status_code == 200, res.text
    return res.json()


# The invariant ------------------------------------------------------------

def test_placement_never_measures_ink(client, job_from_svg, monkeypatch):
    """The render path must not reach vpype. At all.

    `include_ink` defaulting to False is *how* that is true today, not the
    property itself — a future field could re-introduce the parse without ever
    touching that default. So poison the measurement outright: if answering a
    placement request calls it, this fails, whatever the mechanism.
    """
    job = job_from_svg(FIXTURES / "mm-canvas.svg")

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "placement measured ink — the preview now waits on a full vpype "
            "read of the document before it can draw anything"
        )

    monkeypatch.setattr(svg_utils, "ink_rect_doc_mm", forbidden)

    body = placement(client, job)
    assert body["ink_measured"] is False
    assert body["layout_width_mm"] > 0        # still fully answered


def test_document_geometry_is_parsed_once_per_file(client, heavy_job, monkeypatch):
    """Reading three attributes off the root element still costs a full lxml
    parse — 45ms here, 76ms on the 4.5MB original. That is per request, on a
    path that fires on every slider tick, so it has to be cached. Nothing but
    the file itself can change the answer, so one parse is the correct number
    however many placements are asked about it.
    """
    parsed = []

    class CountingEtree:
        def parse(self, source, *args, **kwargs):
            parsed.append(str(source))
            return etree.parse(source, *args, **kwargs)

    monkeypatch.setattr(main, "etree", CountingEtree())

    for width in range(200, 212):             # a drag across the paper-width field
        placement(client, heavy_job, paper_width_mm=float(width))

    assert parsed == [str(main.UPLOAD_DIR / f"{heavy_job['svg_id']}.svg")]


def test_a_stale_document_is_never_served(client, heavy_job):
    """The geometry cache is keyed on mtime, so rewriting the file has to
    change the answer. Worth pinning separately: the bug this cache prevents is
    slowness, but the bug a *wrong* cache key causes is a preview showing some
    other drawing's placement, which is much worse than a slow one.
    """
    assert placement(client, heavy_job)["doc_width_mm"] == pytest.approx(928.158,
                                                                        abs=0.01)
    (main.UPLOAD_DIR / f"{heavy_job['svg_id']}.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="50mm" height="70mm" '
        'viewBox="0 0 50 70"><path d="M0 0 L50 70" stroke="#000"/></svg>')
    assert placement(client, heavy_job)["doc_width_mm"] == pytest.approx(50.0)


# The stopwatches ----------------------------------------------------------

def test_slider_drag_stays_interactive(client, heavy_job):
    """A budget, not a benchmark.

    Wall-clock assertions rot into flaky tests when they are tuned near the
    real number, so this one is not: the steady state is ~0.6ms and the
    failure being guarded against is ~4,000ms. 100ms sits two orders of
    magnitude above the former and forty times below the latter, which leaves
    it immune to a loaded Pi and still fatal to any re-coupling of vpype.

    Median rather than max, deliberately — one scheduler hiccup inside a
    twenty-request loop is not a regression, and treating it as one is how
    people learn to ignore a test suite.
    """
    placement(client, heavy_job)               # warm: this measures the drag, not the load

    timings = []
    for i in range(20):
        start = time.perf_counter()
        placement(client, heavy_job, transform_scale=1.0 + i / 100)
        timings.append(time.perf_counter() - start)

    median = statistics.median(timings)
    assert median < 0.100, f"median placement {median * 1000:.1f}ms on a heavy document"


def test_first_placement_of_a_heavy_document_is_not_a_stall(client, heavy_job):
    """The cold path: first preview after upload, nothing cached. It pays one
    lxml parse and nothing else. Budgeted at 1s against a measured ~150ms,
    because the regression it stands in for took 7,700ms."""
    start = time.perf_counter()
    placement(client, heavy_job)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"first placement took {elapsed:.2f}s"


# The slow half, where it belongs ------------------------------------------

def test_ink_at_scale_is_correct_and_cached(client, heavy_job):
    """Ink measurement is allowed to be slow — it is off the render path — but
    it has to be right, and it must not be paid twice for one file. The second
    request here is what the size readout hits while the user nudges the
    artwork around: the placement changed, the ink never did.
    """
    body = placement_with_ink(client, heavy_job, A4)
    assert body["ink_measured"] is True
    ink = body["ink"]
    assert ink is not None and ink["width_mm"] > 0

    expected = svg_utils.ink_bounds_mm(
        main.UPLOAD_DIR / f"{heavy_job['svg_id']}.svg", [0, 1, 2, 3],
        210.0, 297.0, 0.0, 0.0, 0.0, 0.0, False,
        machine_auto_rotate=config.MACHINE_AUTO_ROTATE,
    )
    assert (ink["left_mm"], ink["top_mm"]) == pytest.approx(
        (float(expected[0]), float(expected[1])))

    start = time.perf_counter()
    placement(client, heavy_job, transform_offset_x_mm=5.0, **WITH_INK)
    assert time.perf_counter() - start < 0.100, "ink was re-measured for the same file"


def test_asking_for_ink_never_blocks_the_request(client, heavy_job):
    """The crash, as a test.

    Measuring took up to 75 seconds on a real drawing, and it used to happen
    inside the handler. The browser re-asks on every WebSocket state broadcast,
    and aborting a fetch does not stop the server thread already inside vpype —
    so parses stacked up, several deep, across a threadpool 40 wide, until four
    cores of Raspberry Pi had nothing left for the event loop or the plotter.

    An unmeasured file must therefore come back immediately saying so, not
    eventually saying the answer.
    """
    start = time.perf_counter()
    body = placement(client, heavy_job, include_ink=True)
    elapsed = time.perf_counter() - start
    assert body["ink_measured"] is False
    assert elapsed < 0.5, f"cold ink request blocked for {elapsed:.2f}s"
    # And the placement half is fully answered regardless.
    assert body["layout_width_mm"] > 0


def test_repeated_asks_do_not_stack_up_measurements(client, heavy_job, monkeypatch):
    """The UI asks again on every broadcast. Each ask must be a lookup, not a
    new parse — one measurement per file, however many times it is requested.
    """
    from app import ink_cache

    calls = []
    real = svg_utils.ink_rects_by_layer

    def counting(path):
        calls.append(str(path))
        return real(path)

    monkeypatch.setattr(ink_cache.svg_utils, "ink_rects_by_layer", counting)

    for _ in range(25):                       # 25 broadcasts' worth of asking
        placement(client, heavy_job, include_ink=True)
    placement_with_ink(client, heavy_job, A4)  # let the one measurement finish
    for _ in range(25):
        placement(client, heavy_job, include_ink=True)

    assert len(calls) == 1, f"{len(calls)} vpype reads for one file"
