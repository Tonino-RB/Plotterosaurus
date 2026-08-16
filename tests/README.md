# Placement corpus

Characterization tests for the SVG placement pipeline — the math that decides
where ink lands on paper.

## What these assert

That placement has not **changed**. Not that it is correct.

That distinction is the whole point. The correct answers were never written
down, and the same geometry is currently computed independently in
`transform_to_paper`, `ink_bounds_mm`, and the browser — copies that have
already drifted apart. Before any of that can be safely collapsed into one
engine, we need a record of what the code does today, so the extraction has an
unambiguous finish line: rows that are right stay byte-identical, rows that are
wrong change in exactly the way the fix predicts.

## Running

```bash
venv/bin/pip install -r requirements-dev.txt      # once
venv/bin/python -m pytest -m "not real" -q        # the everyday one, ~90s
venv/bin/python -m pytest -q                      # everything, minutes
```

Test-only dependencies are deliberately kept out of `requirements.txt`, so
`install.sh` never pulls a test runner onto a plotter.

**Tests never touch the running plotter's data.** `_sandbox_server_state` in
`conftest.py` redirects `state.json` and `uploads/` to a temp directory for the
whole session, and `test_sandbox.py` asserts it took. This is not hygiene, it
is damage control: `app.state` persists to the repo's own `state.json`, and
pytest never calls `state.init()`, so an unsandboxed run starts with an empty
in-memory queue and writes it straight over the live one. That destroyed a real
queue before the fixture existed — and because the service keeps state in
memory and rewrites the file on its next change, the loss stayed invisible
until the next restart. Never read `UPLOAD_DIR` with a from-import; go through
the module so the redirect is seen.

Everything below assumes the `venv/bin/python -m pytest` prefix. Running bare
`pytest` may pick up a system interpreter that has no `lxml` or `vpype`.

### Flags worth knowing

| Command | What it does |
|---|---|
| `-q` | One dot per test. The default choice — 180 cases scroll past in three lines. |
| `-v` | One line per case, named. Use when you want to see *which* cases ran. |
| `-x` | Stop at the first failure. Best when a change broke a lot and you want one clean diff to read instead of forty. |
| `--ff` | Run previously-failed cases first. Pairs well with `-x` while iterating. |
| `-k EXPR` | Run only cases whose id matches. This is the one you'll use most — see below. |
| `-l` | Show local variables on failure. |
| `--tb=short` | Shorter tracebacks; `--tb=no` for just the pass/fail list. |

### Selecting cases with `-k`

Case ids are `fixture|scenario`, so `-k` slices the corpus either way:

```bash
# One fixture, every scenario — "how does this document behave everywhere?"
venv/bin/python -m pytest tests/ -v -k "ink-overflows-canvas"

# One scenario, every fixture — "what does fit-to-page do to everything?"
venv/bin/python -m pytest tests/ -v -k "a4p-fit"

# Narrow to one case with `and`
venv/bin/python -m pytest tests/ -v -k "near-square and portraitbed"

# Everything auto-rotate touches, stopping at the first break
venv/bin/python -m pytest tests/ -x -k "autorotate"

# Both squareness fixtures — the A9 blast radius
venv/bin/python -m pytest tests/ -v -k "square"

# Rotation scenarios only
venv/bin/python -m pytest tests/ -k "rot90 or rot45"

# Everything except the big-canvas fixture
venv/bin/python -m pytest tests/ -k "not ink-in-corner"
```

`-k` matches substrings and understands `and` / `or` / `not`, so
`-k "fit and not a4l"` is valid.

Two gotchas, both learned the hard way:

**Don't put the `|` from a case id inside `-k`.** `-k "square|portraitbed-autorotate"`
looks like it selects one case; it silently selects *all 180*, because `-k`
parses `|` as an operator rather than literal text. Use `and` to narrow
(`-k "square and portraitbed"`) or address the case exactly by node id:

```bash
venv/bin/python -m pytest "tests/test_placement.py::test_placement_matches_golden[square|portraitbed-autorotate]"
```

**Substrings overlap.** `-k "square"` matches `near-square` too, so that one
selects both squareness fixtures (20 cases), not one. Usually what you want —
just don't mistake it for an exact match.

### A typical debugging loop

```bash
venv/bin/python -m pytest tests/ -q                    # what broke?
venv/bin/python -m pytest tests/ -x -k "autorotate" -v # narrow to one family
# ...fix the code...
venv/bin/python -m pytest tests/ -q                    # all green again?
```

## When a test fails

A failure means placement moved. Either you meant it or you didn't.

**You didn't** — that's the suite doing its job. Read the diff and fix the code.

**You did** — read the diff *first*, confirm every changed row is one you
expected, then re-record:

```bash
venv/bin/python -m tests.regen_golden
git diff tests/golden/placement.json
```

Never regenerate to turn a red test green without reading what moved. A golden
file updated on autopilot is worse than no test, because it looks like coverage.

## Layout

| Path | What it is |
|---|---|
| `conftest.py` | The state/uploads sandbox, the heavy synthetic document, the job factory |
| `fixtures/*.svg` | 18 hand-written documents, each isolating one property |
| `placement_cases.py` | The 10 scenarios, and the pipeline one case runs through |
| `golden/placement.json` | 180 recorded answers — generated, never hand-edited |
| `regen_golden.py` | Rewrites the golden file from current behaviour |
| `test_placement_engine.py` | Unit specs for `app/placement.py` |
| `test_placement_scale.py` | What placement *costs* on a document the size of real work |
| `test_curves.py` | Curve-heavy input: the cost, and why the fix for it is safe |
| `test_workload.py` | The shared budget that keeps background work off the plotter |
| `test_estimate.py` | What the time/distance estimate measures, and atomic writes |
| `test_real_svgs.py` | The suite pointed at whatever is in `real/` (marked `real`) |
| `test_static_js.py` | Compiles `static/*.js`; runs `effectivePlacement` under quickjs |
| `test_sandbox.py` | Asserts the suite is not writing to the live plotter's data |
| `real/` | Gitignored. Drop your own drawings here and they get covered. |

### Size is a property, and the fixtures do not have it

Every file in `fixtures/` is under 600 bytes, and that blind spot shipped a
bug: the placement endpoint was benchmarked against a document containing one
rectangle, then took seven seconds per preview on a real drawing, which reads
to a user as a broken app rather than a slow one. Small fixtures answer "is the
math right?" and answer it well; they cannot answer "is this still usable?",
because every cost in the pipeline scales with element count and they have no
elements.

So there are three heavy lanes. `conftest.py::heavy_svg` builds a ~3MB, 4-layer,
3,600-polyline document once per session — deterministic, no repo bloat, and
shaped from the exports that exposed the regression. And `real/` picks up
whatever you put in it: the markup no fixture author thinks to type, from files
too large and too personal to commit.

### Curves are not big polylines

`conftest.py::curvy_svg` is the third, and it exists because polyline fixtures
cannot express the cost of a curve. A polyline arrives already flat; a bezier
is expanded by whatever reads it, so byte size stops predicting anything:

| | size | after flattening | peak RSS | time |
|---|---|---|---|---|
| `heavy_svg` (polylines) | 3.0 MB | 309 thousand points | 400 MB | 9.7 s |
| `curvy_svg` (beziers) | 2.4 MB | 102 million points | **2.69 GB** | 54.5 s |

The smaller file was 330 times the geometry, and 2.69GB on a 3.7GB board that
is also running a browser is a crash, not a slow measurement. `test_curves.py`
holds that fixed and — just as important — holds true the reason the fix is
safe, which is that `BOUNDS_QUANTIZATION` cannot reach the plot.

These are the slowest tests in the suite (~60s of the ~90s total). `-k "not
curves"` skips them while you are iterating on something else.

**Do not put digits in a fixture's layer labels.** vpype derives a layer id
from the first group of digits in `inkscape:label`, so `curves0` and `curves1`
both become layer 1 and their geometry merges — a per-layer measurement then
reports one layer's bounds for two. Uploads get this repaired by
`normalize_layer_structure`; fixtures written by hand do not. Both heavy
fixtures use word labels (`outline`, `ink`, `wash`) for exactly this reason,
and `test_curves.py` pins the normalized-collision case.

`test_placement_engine.py` is a different kind of test from the rest of this
directory. The golden suite asserts only that behaviour hasn't *changed*;
these assert that the placement rules are what we decided they should be —
the canvas is the composition, anchor at the margin box's top-left,
auto-rotate turns the artwork with the page, `meet` rather than stretch. When
the two disagree, the specs are right and the golden file needs regenerating.

Each case runs the sequence the app really runs — `normalize_layer_structure`
→ `parse_layers` → `filter_to_layers` → `transform_to_paper` → `ink_bounds_mm`
— and records the document size, the layer list, the output page, the
`<g transform>` string, and the ink rectangle. Those are the outputs the engine
extraction must preserve.

## Fixture rules

**One property per fixture.** `aspect-mismatch.svg` exists to probe
`preserveAspectRatio="meet"`, nothing else.

**Non-square canvases unless squareness is the point.** Only `square.svg` and
`near-square.svg` are square. This is not cosmetic: an earlier version had ten
incidentally-square fixtures, and a one-line auto-rotate change lit up 11 cases
instead of 2 — real signal buried under fixtures that were never meant to be
testing that. A diff should name its own cause.

**Fixtures are immutable inputs.** `normalize_layer_structure` rewrites its
input in place, so every case copies to a tmp dir first. Never let a test write
into `fixtures/`.

## Known-bad rows

`OPEN_FINDINGS` in `placement_cases.py` maps each open finding to the fixtures
that demonstrate it. It is documentation, not an assertion: when you fix one,
those are the rows expected to move, and a diff reaching further than that list
means the fix reached further than intended.

Currently recorded: **A6** (canvas crop only applied when Optimize SVG is on)
and **A7** (documents with nothing plottable are accepted silently).

A9 was the first entry retired, and it is the worked example of why this file
exists. Extracting `app/placement.py` rewrote every line of the placement
math; the corpus reported a two-line behavioural diff, and both lines were the
A9 fix the entry predicted. A refactor that large landing that precisely is
only checkable because the baseline existed first.
