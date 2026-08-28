"""A failed ink measurement must not masquerade as an empty document.

`ink_cache` answers `(measured, rect)`, and the UI draws a real distinction
between the two halves: not-measured means "ask again", measured-with-no-rect
means "this document genuinely draws nothing". Caching {} for a parse that
*raised* collapses that distinction permanently, for as long as the file's
mtime is unchanged — so a temp-file race or a momentary memory pinch leaves the
size readout and the bounds overlay blank with no way to retry.
"""
import time
from pathlib import Path

import pytest

from app import ink_cache


@pytest.fixture
def svg(tmp_path):
    p = tmp_path / "one-line.svg"
    p.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm" '
        'viewBox="0 0 100 50"><path d="M10 10 L90 40" stroke="black" '
        'fill="none"/></svg>'
    )
    return p


@pytest.fixture(autouse=True)
def clean_cache():
    """The cache is module-global and the worker thread outlives any one test."""
    for store in (ink_cache._cache, ink_cache._failed):
        store.clear()
    ink_cache._pending.clear()
    yield
    for store in (ink_cache._cache, ink_cache._failed):
        store.clear()
    ink_cache._pending.clear()


def _settle(path, timeout=30.0):
    """Wait for the worker to finish whatever `request` just queued."""
    key = ink_cache._key(path)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with ink_cache._lock:
            if key not in ink_cache._pending:
                return
        time.sleep(0.02)
    raise AssertionError("ink measurement never finished")


def test_transient_failure_is_retried_not_cached_as_empty(svg, monkeypatch):
    monkeypatch.setattr(ink_cache, "_FAIL_RETRY_S", 0.0)
    calls = []

    def flaky(path):
        calls.append(str(path))
        if len(calls) == 1:
            raise OSError("temp file lost a race")
        return {0: {"rect": (0.0, 0.0, 100.0, 50.0), "length_mm": 89.0}}

    monkeypatch.setattr(ink_cache.svg_utils, "measure_layers", flaky)

    ink_cache.request(svg)
    _settle(svg)
    # The failure is not an answer: the caller is told to ask again.
    assert ink_cache.rect_for(svg, [0]) == (False, None)

    _settle(svg)                        # rect_for re-requested it
    measured, rect = ink_cache.rect_for(svg, [0])
    assert len(calls) == 2, f"{len(calls)} parses; the retry did not happen"
    assert measured is True
    assert rect == (0.0, 0.0, 100.0, 50.0)


def test_retry_is_rate_limited(svg, monkeypatch):
    """Each attempt is a full vpype parse and the UI re-asks on every state
    broadcast. Retrying must not turn a broken file into a parse loop."""
    calls = []

    def always_fails(path):
        calls.append(str(path))
        raise OSError("nope")

    monkeypatch.setattr(ink_cache.svg_utils, "measure_layers", always_fails)

    ink_cache.request(svg)
    _settle(svg)
    for _ in range(25):                 # 25 broadcasts' worth of asking
        assert ink_cache.rect_for(svg, [0]) == (False, None)
    assert len(calls) == 1, f"{len(calls)} parses inside the retry window"


def test_a_file_that_keeps_failing_settles_as_empty(svg, monkeypatch):
    """Retrying forever is its own bug. After a bounded number of tries the
    document itself is the likeliest explanation, so the empty answer stands."""
    monkeypatch.setattr(ink_cache, "_FAIL_RETRY_S", 0.0)
    calls = []

    def always_fails(path):
        calls.append(str(path))
        raise OSError("malformed")

    monkeypatch.setattr(ink_cache.svg_utils, "measure_layers", always_fails)

    for _ in range(ink_cache._FAIL_ATTEMPTS):
        ink_cache.request(svg)
        _settle(svg)
    assert len(calls) == ink_cache._FAIL_ATTEMPTS

    measured, rect = ink_cache.rect_for(svg, [0])
    assert measured is True and rect is None
    _settle(svg)
    assert len(calls) == ink_cache._FAIL_ATTEMPTS, "still parsing after settling"
