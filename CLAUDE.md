# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Plotterosaurus is a self-hosted plot server for the iDraw H SE A3 and AxiDraw-class pen plotters, meant to run on a Raspberry Pi. It's a personal fork of [Plotter Hub](https://github.com/Synendo/PlotterHub). See [README.md](README.md) for features and install/deploy instructions, and [API.md](API.md) for the public `/api/v1/*` HTTP API reference.

## Commands

```bash
# Install test dependencies (once)
venv/bin/pip install -r requirements-dev.txt

# Everyday test run (~90s) — skips tests/real/
venv/bin/python -m pytest -m "not real" -q

# Full run including tests/real/ (gitignored, user-supplied drawings; minutes)
venv/bin/python -m pytest -q

# Run one test by node id, or narrow with -k (see tests/README.md for -k gotchas)
venv/bin/python -m pytest "tests/test_placement.py::test_placement_matches_golden[square|portraitbed-autorotate]"
venv/bin/python -m pytest tests/ -v -k "ink-overflows-canvas"

# Regenerate the placement golden file after an intentional placement change —
# always read the diff first, never regenerate to turn red green blindly
venv/bin/python -m tests.regen_golden
git diff tests/golden/placement.json
```

Always use `venv/bin/python -m pytest`, not a bare `pytest` — a system interpreter may lack `lxml`/`vpype`. There's no linter/formatter config in this repo (no ruff/black/mypy) and no frontend build step.

Deploy/run locally is via `./install.sh` (idempotent — installs deps, templates the systemd unit, restarts the service). Never restart `plotterosaurus.service` mid-plot: Python can't kill a thread, so a SIGTERM during `plot_run` strands the pen unless the graceful-shutdown handler gets to pause it first.

## Architecture

Backend: Python 3.13 / FastAPI / Uvicorn. Frontend: vanilla HTML/CSS/JS, no build step, no framework. State is in-memory, broadcast over a WebSocket via `asyncio.Queue`; `state.json` persists the job queue across restarts and `config.json` persists settings.

Key module layout (`app/`):

| Module | Responsibility |
|---|---|
| `main.py` | FastAPI routes: `/upload`, `/jobs`, `/queue/*`, `/pen/*`, `/motors/*`, `/camera/*`, `/webhook/*`, `/settings`, `/update/*`, `/ws/state`, plus the auth-gated `/api/v1/*` public API |
| `plot_worker.py` | The plot/resume/homing worker thread: staged-loop, pen-change-pause, calibration logic, manual jog & pen control, button-poll and position-poll threads, preview cache |
| `placement.py` | **The** placement engine — pure function(s), floats in/floats out, deciding where ink lands on paper. Every other module and the web UI consume its answer rather than deriving their own; see "Placement engine" below |
| `svg_utils.py` | Inkscape-layer parsing/filtering, and rendering a placement decision into an SVG |
| `ink_cache.py` | Per-layer ink rectangles, measured once per file on a background thread; a selection's rectangle is the union of its layers'. Requests read from memory or are told "not ready" — they never block on vpype |
| `optimize_queue.py` / `svg_optimize.py` | Single-worker FIFO queue that runs `vpype` (subprocess, cancel-killable) ahead of time on upload/job-create/edit; cached per job and reused across re-plots |
| `plan_queue.py` | Single-worker FIFO queue that pre-computes each queued job's time/distance estimate in the background |
| `workload.py` | The shared budget: one heavy background job at a time across the three background queues, all scheduled below the plot worker's own priority |
| `camera.py` | Plot recording via a Pi Camera Module 3 + MediaMTX (opt-in, `ENABLE_CAMERA=1`) |
| `state.py` | In-memory queue/job state + WebSocket broadcast |
| `config.py` | Plotter/camera/webhook/display settings, persisted to `config.json` |
| `updates.py` | Self-update: remote version check + guarded apply (opt-in) |

### Placement engine

`app/placement.py` is the single source of truth for "where does the artwork land on paper." The SVG writer, the bounds check, and the browser preview (`static/app.js`'s `effectivePlacement`) all consume its answer instead of computing their own — the browser only extrapolates along axes the engine guarantees are linear, so a drag stays responsive between round-trips to `POST /jobs/{id}/placement`. Treat any placement/bounds change as touching a contract shared across backend and frontend; see `tests/README.md` for how this is characterized.

### Concurrency model

Three background queues (`optimize_queue`, `plan_queue`, and the plot worker thread itself) share one budget via `workload.py` so they never fight the Pi's CPU for real plotting priority. Preview generation and vpype optimization each run in their own cancel-killable subprocess, behind their own single-worker queue.

## Testing conventions

Full detail in [tests/README.md](tests/README.md); the essentials:

- **Tests never touch the running plotter's live data.** `conftest.py` redirects `state.json` and `uploads/` to a temp dir for the whole session (`test_sandbox.py` asserts this took). This existed because an unsandboxed run once overwrote a real job queue — `app.state` persists to the repo's own `state.json`, and pytest never calls `state.init()`. Never read `UPLOAD_DIR` via a from-import; go through the module so the sandbox redirect is honored.
- **The placement golden suite** (`tests/golden/placement.json`, driven by `placement_cases.py`) characterizes current behavior, not correctness — a diff means placement moved, which is fine if you meant it (regenerate and read the diff) and a bug if you didn't. `test_placement_engine.py` is the other kind of test: it asserts the placement *rules* themselves (canvas is the composition, anchor at margin box's top-left, auto-rotate turns with the page, `meet` not stretch). When the two disagree, the specs win and the golden file gets regenerated.
- **`OPEN_FINDINGS` in `placement_cases.py`** documents known-bad rows in the golden file (currently: canvas crop only applies with Optimize SVG on; documents with nothing plottable are silently accepted) — not an assertion, a map of which fixtures demonstrate which open bug.
- **`test_manual_origin.py`** pins the carriage-position model: `origin_base` (where the page corner is declared), `manual_origin_offset` (the idle Move buttons) and `origin_nudge` (a mid-run pen-change correction). The carriage sits at the sum; each control owns exactly one value and must not disturb the others. Two rules keep the stored numbers honest — nothing is recorded unless the hardware actually moved, and no move longer than the bed is accepted, because the driver clips it while still reporting the full target.
- Fixtures in `tests/fixtures/` are hand-written, each isolating one property, deliberately non-square (square-only fixtures previously masked bugs by making every case incidentally hit the same code path) and never digit-labeled (vpype derives layer ids from the first digit group in `inkscape:label`, so `curves0`/`curves1` collide into one layer).
- `tests/real/` is gitignored — drop personal drawings there to exercise markup no fixture author thought to write; marked `real` and skipped by the everyday `-m "not real"` run.

## Behavioral guidelines

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

