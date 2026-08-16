"""Syntax check for the browser code.

static/app.js is ~3,600 lines of vanilla JavaScript with no build step, which
means nothing between an editor and a user's browser ever looks at it. A stray
brace ships silently and the whole UI is dead on load. This is the cheapest
possible guard against that: compile the file and see if the engine objects.

It is a *syntax* check, not a test of behaviour — quickjs has no DOM, so the
source is wrapped in a function that is compiled and never called.
"""
from pathlib import Path

import pytest

quickjs = pytest.importorskip(
    "quickjs",
    reason="quickjs not installed — pip install -r requirements-dev.txt",
)

STATIC = Path(__file__).parent.parent / "static"


@pytest.mark.parametrize("name", sorted(p.name for p in STATIC.glob("*.js")))
def test_javascript_parses(name: str) -> None:
    source = (STATIC / name).read_text()
    # Wrapped so top-level declarations are compiled but nothing executes;
    # there is no window or document here to execute against.
    quickjs.Context().eval("(function(){\n" + source + "\n})")
