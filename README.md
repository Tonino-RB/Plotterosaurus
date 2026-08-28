# Plotterosaurus

> **Status: beta (v0.3.0-beta).** Plotterosaurus is a personal fork of [Plotter Hub](https://github.com/Synendo/PlotterHub) — version numbering restarted from `0.1.0` to reflect that this is a separate, actively-changing line rather than a continuation of upstream's `1.x` releases. Expect breaking changes between betas.

A self-hosted plot server for the iDraw H SE A3 and AxiDraw-class pen plotters. Submit SVGs over the network and the Pi drives the plotter locally via the official AxiDraw Python API, so your workstation doesn't need to stay connected for the duration of the plot.

Open `http://plotterosaurus.local` (or whatever your Pi's hostname is) and you get a drag-and-drop UI with layer-by-layer plotting, pen-change pauses, paper-size presets, and a live pen-position cursor.

## Background

I didn't like that my iDraw H SE A3 plotter had to stay connected to my laptop to run a plot. Luckily it's compatible with the great [AxiDraw software](https://axidraw.com/), which can be installed on a Raspberry Pi — this repo is just a UI around [AxiDraw's Python library](https://axidraw.com/doc/py_api/).

I also had a look at [saxi](https://github.com/nornagon/saxi), but it didn't support the physical pause button on my iDraw. AxiDraw does recognize button presses, so Plotterosaurus supports it: press the button once to pause, press it a second time to resume the plot. The same button also continues to the next layer when the plot is paused for a pen change.

**Disclaimer:** this code was completely created by [Claude Code](https://claude.com/claude-code) (Claude Opus 4.7-4.8, 1M-context).

## Features

**Plotting**

- Drag-and-drop SVG upload; Inkscape layers parsed and selectable, each shown with an icon inferred from its type (pattern, text, svg, calibration, image, map, 3D model)
- SVGs with content sitting outside any Inkscape layer are auto-repaired into one on upload (a vpype read/write round-trip) instead of showing up as "no layers"
- Staged plotting: optional pause between layers for pen changes
- No queue to manage: a job is `ready` from the moment it is created, Plot runs the topmost ready one, and the run ends with that job — so the paper can be changed with the machine genuinely idle
- Paper presets (A0–A5, B0–B5, Letter, Legal, Ledger, ANSI C–E, Custom) + orientation
- 4-sided margins, fit-content-to-page, and a per-job scale / rotation / X-Y offset transform
- Configurable pen-down / pen-up speed, acceleration and pen height, with optional per-layer overrides
- Optional [vpype](https://vpype.readthedocs.io/) optimization (linemerge / linesimplify / linesort / reloop, plus a minimum-segment-length filter) before plotting; cached per job and reused across re-plots
- Background pre-optimization and pre-planning queues: uploads are vpype-optimized and time/distance-estimated ahead of time (on upload and on job create/edit), so clicking Plot is usually an instant cache hit rather than a 20–30s wait
- "Save As" on the job card: download the processed drawing as SVG, PNG (white or transparent), PDF, G-code, or HPGL — either "as optimized" (the drawing's own coordinates) or "as plotted" (selected layers only, placed on the page with the job's size/margins/transform; G-code and HPGL additionally carry the active machine's axis-skew correction, SVG/PNG/PDF stay square), so the export is a ready-to-run toolpath for another plotter / GRBL machine

**During the plot**

- Pre-plot estimate: time, pen-down distance, total distance, pen lifts
- Progress bar with remaining-time based on the estimate
- Live pen cursor on the preview (blue while drawing, grey while traveling)
- UI Pause / Resume / Cancel — cancel returns to origin via `res_home`
- "Pause at Pen Lift" — a deferred pause that waits for the next pen-up so pump-action pens don't leave a dot mid-stroke
- Live speed / acceleration / pen-height adjustment while a stage is actively plotting, applied at the next motion checkpoint; a speed/acceleration change also recalibrates the remaining-time estimate in the background so the progress bar stays accurate
- Physical pause button toggles: press to pause, press again to resume

**Calibration & alignment**

- Per-job calibration layers (layer type `calibration`) plottable on demand from a pen-change pause without advancing the job — for pressure/alignment tests on the actual document
- A standalone calibration-file library (the gitignored `calibration/` folder) of reusable test SVGs, runnable the same way independent of any job
- Fine origin nudge (X/Y, in mm) during a pen-change pause to correct for paper drift between layers — also physically jogs the carriage so you see/feel the correction
- Manual jog d-pad + "Set Origin Here" to walk the carriage over the paper while idle and capture that position as the default offset for new jobs
- Manual pen up/down and motor enable/disable, usable any time the plotter isn't actively driving a plot (e.g. to move the carriage by hand)
- Bounds protection: a nudge, jog, or leftover un-homed jog that would push the artwork's actual ink (not just its canvas) off the page or the carriage past the machine bed edge is rejected up front, with a precise "nudge back by (x, y) mm" correction — surfaced as a one-click button on the job card if it blocks a plot from starting
- Live preview overlay (red dot + outline) showing exactly where the current jog/nudge places the artwork's origin and footprint on the page, before it's committed

**Plot recording (optional — camera)**

- Records a plot via a Raspberry Pi Camera Module 3, driven through [MediaMTX](https://mediamtx.org) as a separate systemd service
- Three modes: realtime, timelapse (periodic JPEG grabs, assembled into video), and sped-up (ffmpeg `setpts` after the fact)
- Live preview with autofocus/exposure/white-balance/denoise/brightness/contrast/saturation/sharpness controls and resolution/bitrate settings, all in Settings → Camera
- A job can request recording itself (`record_plot`); it pauses/resumes automatically with the job. Recording can also be started/stopped manually, independent of any job
- Live RTSP / HLS / WebRTC stream URLs for viewing in VLC, OBS, or Home Assistant while a plot runs
- Finished recordings are saved locally under `camera_output_folder` and optionally pushed to cloud storage via `rclone copy` (bring your own installed + authenticated `rclone`)
- Opt-in at install time (`ENABLE_CAMERA=1`) — everything above is skipped entirely on installs without a camera. See [Install options](#install-options) and [API.md](API.md#camera--plot-recording)

**Operational**

- Runs as a systemd service under the user who invoked `install.sh`
- In-app self-update (opt-in, see [Updating](#updating)): checks GitHub for new releases and updates with one click (Settings → About & Updates), guarded so it never runs mid-plot or over local changes
- Outgoing webhook notifications on layer/job completion — point it at ntfy, Home Assistant, or a Slack/Discord incoming webhook for a push/text/email
- Multi-language UI (English, German, Spanish, French, Italian, Japanese, Korean, Dutch, Portuguese, Simplified Chinese), auto-detected from the browser, switchable in Settings
- Configurable display unit (mm / cm / in) for the whole UI
- Custom plotter bed size with optional paper auto-rotate, layered on top of the selected AxiDraw model — clips real travel to the custom size, never beyond the hardware's own limits
- Plot worker runs in a thread; preview and vpype optimization run in cancel-killable subprocesses, each behind their own single-worker queue so they never fight each other for CPU on the Pi
- In-memory preview cache — same SVG + same params skips the ~20–30s planning pass
- Graceful shutdown on service stop: pauses any in-flight plot so the pen is raised and the resume SVG is flushed

**API**

- HTTP API for companion apps and scripts under `/api/v1/*`, secured with an auto-generated `X-API-Key`, plus a live-state WebSocket (`/api/v1/ws/state`)
- See [API.md](API.md) for the endpoint reference and `multipart/form-data` schema

## Requirements

- Raspberry Pi Zero 2 W, 3B+, or newer running Raspberry Pi OS Trixie (Debian 13) or Bookworm (Debian 12)
- An iDraw H SE A3, AxiDraw, or compatible EBB-based plotter on USB
- A Raspberry Pi Camera Module 3 — optional, only needed for [plot recording](#plot-recording-optional--camera) (`ENABLE_CAMERA=1`)

Tested on a Raspberry Pi 3 Model B and a Raspberry Pi Zero 2 W, both running Raspberry Pi OS Lite (64-bit) — a port of Debian Trixie with no desktop environment (released 2026-04-21).

**Hardware notes:** both the Zero 2 W and the 3B+ are capable hosts — which one fits best depends on how you plot. The **Zero 2 W** draws the least power and is well suited to an always-on box; it boots and optimizes/plans a job roughly 40% slower than a **3B+**, but that overhead is negligible next to the plotting time itself. If you plot a lot and want snappier setup and preview times, the 3B+ is the more comfortable choice. If you plot **large or curve-heavy files**, go further up the range: measured on a Pi 4, a 2.4MB drawing of bezier curves needs ~380MB of RAM and ~23s just to measure its bounds, and three such files uploaded together will keep all four cores busy for minutes. A Zero 2 W has 512MB and will struggle badly with that class of work.

`install.sh` checks these prerequisites and aborts with a hint if any are missing:

- Python ≥ 3.11 (default on Bookworm and newer)
- Service user is a member of the `dialout` group (for `/dev/ttyACM0`)
- Service user is a member of the `video` group — only checked when installing with `ENABLE_CAMERA=1` (needed for camera access)
- `avahi-daemon` is running (warning only — needed for `.local` hostname)

### Dependencies installed by the script

**apt packages** (idempotent — apt skips anything already present):

- [`python3`](https://www.python.org/)
- [`python3-venv`](https://docs.python.org/3/library/venv.html)
- [`python3-pip`](https://pip.pypa.io/)

**Python packages**, pip-installed into a project-local `venv/`:

- [`fastapi`](https://fastapi.tiangolo.com/)
- [`uvicorn[standard]`](https://www.uvicorn.org/)
- [`python-multipart`](https://github.com/Kludex/python-multipart)
- [`pyaxidraw`](https://axidraw.com/doc/py_api/) (from the Evil Mad Scientist [AxiDraw API zip](https://cdn.evilmadscientist.com/dl/ad/public/AxiDraw_API.zip))
- [`vpype`](https://vpype.readthedocs.io/) — invoked as a subprocess for optional pre-plot optimization

**Camera dependencies** (opt-in, only installed with `ENABLE_CAMERA=1`):

- `rpicam-apps`, `libfreetype6`, `ffmpeg` (apt)
- [MediaMTX](https://mediamtx.org) — downloaded as a prebuilt arm64 binary to `/opt/mediamtx` on first install and run as its own systemd service, reading the camera directly via its native `rpiCamera` source and serving the RTSP/HLS/WebRTC stream plus on-disk recording segments; `app/camera.py` only drives its local Control API and post-processes with ffmpeg
- `rclone` is **not** installed by the script — install and authenticate it yourself if you want finished recordings pushed to cloud storage; Plotterosaurus only shells out to `rclone copy` and never stores cloud credentials

**System files** (written / overwritten on every run):

- `/etc/systemd/system/plotterosaurus.service` — templated from `systemd/plotterosaurus.service` with the invoking user and the repo path
- `/etc/sudoers.d/plotterosaurus-shutdown` — grants the service user NOPASSWD on `/sbin/shutdown` so the UI's shutdown button works
- `/etc/systemd/system/mediamtx.service` and `/opt/mediamtx/mediamtx.yml` — installed and enabled only with `ENABLE_CAMERA=1`; the config is templated from MediaMTX's own defaults with the Control API bound to loopback (127.0.0.1:9997) and a single `cam` path added for the Camera Module 3. The unit uses `Restart=always`, so MediaMTX is brought back up a few seconds after any exit — clean or crashed — keeping the camera available without manual intervention
- `/usr/local/sbin/plotterosaurus-update` and `/etc/sudoers.d/plotterosaurus-update` — root-owned self-update helper and its NOPASSWD sudo rule, installed only with `ENABLE_SELF_UPDATE=1` (off by default — see [Updating](#updating))

### Assumed already present on Raspberry Pi OS

The script relies on these but does not install them: `sudo`, `apt`, `systemctl`, `ss` (from `iproute2`), `install`, `visudo`, plus `git`, `runuser`, and `systemd-run` (used by the self-update path). They ship with any stock Raspberry Pi OS install.

## Install

On a clean Raspberry Pi, as whichever user you want the service to run as. From your workstation, ssh in (replace the hostname/username with your Pi's):

```bash
ssh plotter@plotterosaurus.local
```

Raspberry Pi OS Lite doesn't ship with git, so install it first if needed, then clone and run the installer:

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/Tonino-RB/Plotterosaurus.git ~/Plotterosaurus
cd ~/Plotterosaurus
./install.sh
```

The script is idempotent — re-run after `git pull` to update dependencies and restart the service. Concretely:

- apt install is a no-op when packages are already current
- The `venv/` directory is only created if it doesn't exist; otherwise it's reused
- `pip install -r requirements.txt` skips packages whose spec is already satisfied
- The systemd unit and sudoers rule are re-templated and re-written every time
- `systemctl daemon-reload` / `enable` / `restart` are safe to repeat

If a previous install is already running, the script stops it first so the port probe doesn't see its own listener as a conflict, then binds port 80 if free, else port 8080.

The systemd unit runs the server as the user who invoked `install.sh`, from the directory where the repo was cloned — no specific username is required, and the clone path isn't constrained.

When the script finishes it prints the URL to open in your browser.

### Install options

```bash
# Unattended install (pipes sudo password):
SUDO_PW='your-password' ./install.sh

# Set a different plotter model at install (default is 2, AxiDraw SE/A3):
PLOTTER_MODEL=1 ./install.sh

# Enable plot recording via a Pi Camera Module 3 + MediaMTX (off by default):
ENABLE_CAMERA=1 ./install.sh

# Enable the /draw-stream live "draw progress" page, for an OBS Browser
# Source (off by default — pure app code, no extra packages/services):
ENABLE_DRAW_STREAM=1 ./install.sh

# Enable the in-app "Update now" self-update path (off by default — see Updating below):
ENABLE_SELF_UPDATE=1 ./install.sh

# Options can be combined:
SUDO_PW='your-password' PLOTTER_MODEL=2 ENABLE_CAMERA=1 ./install.sh
```

After install, the plotter model can also be changed from the UI (gear icon → Settings) and is persisted to `config.json`.

### Network and access

Plotterosaurus has no built-in login — anyone who can reach its web port can upload, plot, change settings, update, or shut down the Pi. That's intentional for a trusted home LAN (like a network printer's web page), but it means you should **keep it on your local network and not port-forward it to the internet**. For remote access, put it behind a VPN such as [Tailscale](https://tailscale.com/) or WireGuard rather than exposing it directly. The `X-API-Key` only guards the `/api/v1/*` endpoints and is itself readable on the LAN — it's a scripting convenience, not a security boundary.

## Updating

Plotterosaurus can update itself from the web UI, or you can update manually over ssh. The UI path is the convenient one — no terminal needed, but it's opt-in: it does a `git reset --hard`, which would silently discard any local changes on a modified checkout, so `install.sh` only sets it up when run with `ENABLE_SELF_UPDATE=1` (see [Install options](#install-options)). Without that flag, the update banner and "Update now" stay inert and the manual path below is the only way to update.

### From the UI (recommended)

When a newer version is published on `main`, a banner appears at the top of the page — **Update available: `<current>` → `<latest>`** — with **Update now** and **Skip**. The same controls, plus the current version, an availability badge, and a **Check now** button, live under **Settings → About & Updates**.

- **Update now** pulls the latest version, re-runs `install.sh`, and restarts the service. The installer log streams live in a dialog and the page reconnects on its own once the new version is up (don't close the tab — it takes a minute or two).
- **Skip** hides the banner for that version; it comes back only when a *newer* version is released. You can still start the update later from Settings (the skip just suppresses the banner).
- The check queries the public GitHub repo over HTTPS (no credentials needed) and is cached for an hour, so a freshly published release may not show immediately — **Check now** forces a fresh check.

Updates are **refused while a plot is running** (wait until the queue is idle), and a second update can't start while one is already in progress. If the app folder has **local changes**, the update asks you to confirm before overwriting them — your settings, job queue, and uploads are always kept (they're gitignored, so `git reset` never touches them).

Under the hood: `install.sh` installs a small root-owned helper at `/usr/local/sbin/plotterosaurus-update` with a scoped NOPASSWD sudoers rule. When triggered it re-launches itself in a transient systemd unit so it survives the service restart, runs `git reset --hard` to the latest `main`, then re-runs `install.sh`. All output is written to `update.log`.

If an update doesn't come back up, the cause is usually in `update.log` (or `journalctl -u plotterosaurus -n 50`); ssh in and re-run `./install.sh` to recover.

### Manually over ssh

ssh to the Pi, pull the latest version of the repository and re-run the installer:

```bash
cd ~/Plotterosaurus
git pull
./install.sh
```

`install.sh` is idempotent, so re-running it is the upgrade path — `apt` skips satisfied packages, `pip` only installs requirements that changed, and the systemd unit is re-templated and restarted. Your `config.json`, `state.json`, and everything under `uploads/` is gitignored and preserved across upgrades; the job queue rehydrates on service start. Uploaded SVGs accumulate in `uploads/` over time — enable *Delete on complete* in Settings, or clear old files periodically, so a small SD card doesn't fill up.

Before upgrading (either way), it's cleanest to wait until the queue is idle (or the active job is `paused` / `awaiting_pen_change`). If you do upgrade mid-plot via the manual path, the graceful-shutdown handler pauses the active job and queue persistence restores it as a resumable paused job on the next start.

## Architecture

| Layer | What it is |
|---|---|
| Backend | Python 3.13, FastAPI, Uvicorn (uvloop + httptools) |
| Plotter control | `pyaxidraw` Python API (not the `axicli` CLI) |
| Optimization | `vpype` CLI invoked as a subprocess (cancel-killable) for optional pre-plot path optimization; run ahead of time by a background queue and cached per job, reused across re-plots |
| Camera (optional) | [MediaMTX](https://mediamtx.org), its own systemd service, reads a Pi Camera Module 3 directly and serves RTSP/HLS/WebRTC + recording segments; `app/camera.py` drives its local Control API and post-processes segments with ffmpeg (optional `rclone copy` afterward) |
| Placement | `app/placement.py` — one pure function decides where artwork lands on paper; the SVG writer, the bounds check and the browser preview all consume it |
| Measurement | `app/ink_cache.py` — every layer's ink rectangle, measured once per file on a background thread. Requests read it from memory or are told it is not ready; they never wait on vpype |
| Load budget | `app/workload.py` — one heavy job at a time across the three background queues, all of them below the plot worker's scheduling priority |
| Frontend | Vanilla HTML + CSS + JavaScript, no build step; `static/i18n.js` + `static/i18n/*.json` for the 10-language UI. The browser derives no placement of its own — it asks `POST /jobs/{id}/placement` and renders the reply, extrapolating only along the axes the engine guarantees are linear so a drag stays live between answers (see `effectivePlacement`) |
| Transport | HTTP + WebSocket |
| State | In-memory, broadcast via `asyncio.Queue` |
| Process mgmt | systemd (`plotterosaurus.service`, plus `mediamtx.service` when the camera is enabled) |
| Persistence | Uploaded SVGs + resume SVGs on disk; `config.json` for settings; `state.json` for the job queue (so a paused plot survives a service restart); finished recordings under `recordings/` |

Key module layout:

```
app/
  main.py           # FastAPI routes: /upload, /jobs, /queue/*, /pen/*, /motors/*,
                    # /camera/*, /webhook/*, /settings, /update/*, /ws/state,
                    # plus the auth-gated /api/v1/* public API
  plot_worker.py    # plot + resume + homing worker thread, staged-loop /
                    # pen-change-pause / calibration logic, manual jog & pen
                    # control, button-poll and position-poll threads, preview cache
  preview_runner.py # subprocess entry point for pyaxidraw preview mode
  optimize_queue.py # single-worker FIFO queue that runs vpype ahead of time
                    # (on upload and on job create/edit), shared with plot_worker
  plan_queue.py     # single-worker FIFO queue that pre-computes each ready
                    # job's time/distance estimate in the background
  svg_optimize.py   # vpype subprocess wrapper for optional pre-plot optimization
  placement.py      # THE placement engine: where ink lands on paper. Pure
                    # (floats in, floats out); every other module and the web
                    # UI consume its answer rather than deriving their own
  svg_utils.py      # Inkscape-layer parsing, filtering, and rendering a
                    # placement into an SVG (see placement.py)
  ink_cache.py      # per-layer ink rectangles, measured once per file on a
                    # background thread; a selection's rectangle is the union
                    # of its layers', so no request ever waits on vpype
  workload.py       # the shared budget: one heavy background job at a time,
                    # and all of them below the plot worker's priority
  camera.py         # plot recording via a Camera Module 3 + MediaMTX (opt-in)
  notify.py         # outgoing webhook delivery on layer/job completion
  state.py          # in-memory state + WebSocket broadcast
  config.py         # plotter / camera / webhook / display settings, persisted to config.json
  updates.py        # self-update: remote version check + guarded apply (opt-in)
static/             # index.html, app.js, style.css, i18n.js + i18n/ (10 languages)
tests/              # placement corpus, engine specs, scale and curve budgets,
                    # layer order, pause behaviour — see tests/README.md
systemd/            # plotterosaurus.service + mediamtx.service (templates)
scripts/            # plotterosaurus-update.in (self-update helper template)
install.sh          # idempotent installer
uploads/            # gitignored; uploaded SVGs and per-stage filtered / resume files
calibration/        # gitignored; user-maintained library of standalone calibration SVGs
recordings/         # gitignored; finished plot recordings (created when the camera is enabled)
```

## Development

The local source of truth is on your workstation; deploy to the Pi via rsync:

```bash
# Replace <user>@<host> with your Pi's ssh target, and ~/Plotterosaurus with
# the path where you cloned the repo.
rsync -avz --exclude=.git --exclude=venv --exclude='uploads/*' \
  -e ssh ./ <user>@<host>.local:~/Plotterosaurus/
ssh <user>@<host>.local '~/Plotterosaurus/install.sh'
```

`install.sh` detects that dependencies are already installed and just restarts the service.

Never restart the service mid-plot — Python can't kill a thread, so a SIGTERM during `plot_run` would strand the pen. On modern installs the graceful-shutdown handler mitigates this by pausing first, but it's still better to wait until `status` is `idle`, `completed`, `failed`, or `cancelled`.

## Known limitations

- No live progress while `plot_run` is in its ~18s pre-motion setup phase (EBB version query, servo init, path planning) — pyaxidraw doesn't expose progress events until motion starts.
- Remote update checks are hardcoded off in `app/updates.py` (`_UPDATES_DISABLED = True`) on this checkout, since it's an actively-changing personal fork — the update banner never appears regardless of `ENABLE_SELF_UPDATE`. `git pull && ./install.sh` always works as the manual update path.
- **Content outside the SVG canvas is only dropped when "Optimize SVG" is on.** The canvas is treated as the composition, so anything outside it is meant to be excluded — but that rule is currently enforced by vpype's page crop, which only runs as part of optimization. With optimization off, out-of-canvas geometry is plotted wherever it lands on the sheet.
- **A document with nothing plottable is accepted and plots nothing.** Live text and raster images are dropped on the way to the plotter (they aren't strokes), but the upload succeeds and the job runs to "completed" without drawing. Convert text to paths before uploading.
- The machine profile isn't snapshotted onto a job, so switching the active machine changes how jobs already sitting at `ready` are placed.
- **Artwork larger than the machine bed is estimated in full, then clipped when plotted.** The driver drops pen-down moves outside its travel bounds, so an oversized drawing used to report `0 m / 0 s` — the clip, not the drawing. The estimate now measures the artwork, and the card's machine-bounds warning is what tells you it will not all fit. Identical to before whenever the artwork does fit.
- **The origin is not remembered across a restart.** Deliberate: the motors disengage when the plotter is switched off, so the carriage can be moved by hand, and a remembered position would be a confident lie. Re-aim before the first plot of a session.

## Testing

```bash
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest -m "not real" -q     # the everyday one, ~90s
venv/bin/python -m pytest -q                   # adds tests/real/, minutes
```

Test dependencies are kept out of `requirements.txt` so `install.sh` never puts a test runner on a plotter host.

Two things worth knowing before you run it. The suite redirects `state.json` and `uploads/` to a temp directory for the whole session — it will not touch the running plotter's queue, and `tests/test_sandbox.py` asserts that. And `tests/real/` is gitignored: drop your own drawings in there and the whole contract suite runs against them, which is the only way to cover markup no fixture author thinks to write.

See [tests/README.md](tests/README.md) for what the suite covers and how to regenerate the placement corpus.

## License

Released under the MIT License — see [LICENSE](LICENSE). Built around the AxiDraw Python API from Evil Mad Scientist (GPL-2.0), which is installed as a runtime dependency rather than bundled; the assembled system is therefore subject to GPL-2.0 terms. Optional path optimization uses [vpype](https://vpype.readthedocs.io/) (MIT, © Antoine Beyeler & Contributors), invoked as a separate subprocess and likewise installed as a runtime dependency.

Plotterosaurus is an independent project and is not affiliated with, endorsed by, or supported by Evil Mad Scientist Laboratories. AxiDraw is a trademark of Evil Mad Scientist Laboratories.