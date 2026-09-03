# Plotterosaurus API

This document describes the public HTTP API exposed by Plotterosaurus for external clients (companion apps, CLI tools, scripts). All public endpoints live under the `/api/v1/` prefix and require an API key.

The web UI uses a separate, unauthenticated set of routes (e.g. `/jobs`, `/upload`, `/queue/*`). Those are an internal contract between the bundled HTML and the server — they may change without notice. **Build against `/api/v1/*` only.**

## Base URL

```
http://<your-pi-hostname>.local
```

Default port is 80. If port 80 is unavailable at install time, the installer falls back to 8080 (`http://<host>.local:8080`).

## Authentication

Every request to `/api/v1/*` must include the API key in an `X-API-Key` header.

```
X-API-Key: <your-key>
```

The key is generated automatically the first time the service starts and is persisted in `config.json`. To find it:

- **From the UI** — gear icon → Settings → "API Key" section (last item, collapsed by default). Use the *Copy* button.
- **From the host directly** — read the `api_key` field in `~/Plotterosaurus/config.json` on the Pi.

To rotate the key, edit `config.json` on the Pi (or delete the `api_key` line so a new key is generated on next start) and restart `plotterosaurus.service`.

### Errors

| Code | Meaning |
|---|---|
| `401 Unauthorized` | `X-API-Key` is missing or doesn't match. |
| `400 Bad Request` | Validation failure — invalid SVG, unknown paper preset, malformed metadata JSON. |
| `404 Not Found` | The job ID doesn't exist (for per-job endpoints). |
| `409 Conflict` | The action isn't valid in the current state (e.g. editing a plotting job — once those endpoints exist). |
| `503 Service Unavailable` | Server hasn't initialized the API key yet. |

Errors come back as `{"detail": "..."}`.

## Endpoints

### `POST /api/v1/jobs` — add a job

Adds a new job to the queue. Accepts `multipart/form-data` with two parts:

| Part | Type | Required | Notes |
|---|---|---|---|
| `file` | file | yes | The SVG. Must contain at least one Inkscape layer. Maximum 32 MB — a larger body is refused with `413` before anything is written to disk. |
| `metadata` | text (JSON) | no | Job metadata — see schema below. Omit entirely to use auto-detected defaults. |

#### Metadata schema

All fields are optional. Unspecified booleans, speeds, and `selected` flags fall back to server-side defaults (see `GET /api/v1/settings`).

```jsonc
{
  "name": "string",                   // Display name; replaces filename in the job card header.

  "paper_size": {
    "name": "string",                 // e.g. "A3", "Letter", or a custom label like "Square".
    "width": 200.0,                   // Numeric. Required if `height` is given (and vice versa).
    "height": 200.0,
    "unit": "mm" | "cm" | "in",       // Default "mm". Applies to width/height.
    "orientation": "portrait" | "landscape"  // Optional; swaps width/height if it disagrees.
  },

  "paper": {                          // Optional — the paper *stock*, not its size.
    "name": "FABRIANO Black Black 300g"  // Display only; shown under the preview, opposite the size.
  },

  // Job options — omit any field to inherit the corresponding server default.
  "layer_mode": "layer",             // "layer" | "group" | "pen". How the drawing is split into rows / plot stages: Inkscape layers, children of the root group, or one stage per stroke width + colour. "group"/"pen" re-partition the SVG into a standalone copy the job points at; layers[] overrides below are keyed against that copy's layers.
  "pause_between_layers": true,       // Pause for pen change between selected layers (multi-layer only).
  "skip_same_pen_pause": false,       // With pause_between_layers on: don't pause between consecutive layers whose stroke colour and width match (same pen — nothing to swap).
  "delete_on_complete":   false,      // Auto-remove the job and its uploaded SVG once complete.
  "disable_motors_on_complete": false, // Cut motor torque once the plot finishes back at the origin, so the steppers don't sit warm.

  // Request-only directive (not stored on the job record). When `true` AND
  // no other job is in a runnable or in-progress state (ready / paused /
  // plotting / planning / optimizing / homing / awaiting_pen_change), the
  // worker is started so this job plots immediately. Terminal-state leftovers
  // (`completed` / `failed` / `cancelled`) are inert and do *not* block
  // auto_plot. If a runnable/in-progress job exists, auto_plot is ignored —
  // you'd need to wait for it to finish or hit `/queue/plot` yourself.
  "auto_plot": false,                 // Default false.

  // Plotter speed — omit any field to inherit the server default.
  // Out-of-range values are silently clamped to the bounds.
  "speed_pendown": 30,                // 1–110
  "speed_penup":   80,                // 1–110
  "acceleration":  50,                // 1–100

  // Pen height (servo position) — omit any field to inherit the server
  // default. Out-of-range values are silently clamped to the bounds.
  "pen_pos_up":   60,                 // 0–100
  "pen_pos_down": 30,                 // 0–100

  // Plot recording via a Camera Module 3 (only meaningful when the server
  // was installed with ENABLE_CAMERA=1 — see "Camera / plot recording"
  // below). Omit any field to inherit the server default.
  "record_plot":                  false,   // Start/stop recording with this job.
  "record_mode": "realtime" | "timelapse" | "sped_up",
  "record_timelapse_interval_s":  5.0,     // Used only when record_mode is "timelapse".
  "record_speed_multiplier":      4.0,     // Used only when record_mode is "sped_up".

  // SVG optimization (vpype). Omit any field to inherit the server default.
  // The optimized SVG is cached per job and reused across re-plots; changing
  // any field below invalidates the cache and re-runs the pipeline.
  "optimize_svg":              true,  // Master toggle. When false the rest is ignored.
  "optimize_svg_tolerance_mm": 0.10,  // 0.01–10.0; used by linemerge + linesimplify.
  "optimize_svg_linemerge":    true,  // Stitch lines whose endpoints are within tolerance.
  "optimize_svg_linesimplify": true,  // Reduce vertex count (Douglas-Peucker).
  "optimize_svg_linesort":     true,  // Reorder lines to cut pen-up travel.
  "optimize_svg_reloop":       true,  // Randomize closed-path start (cosmetic).

  // "beginner" runs the toggles above automatically before each plot.
  // "expert" instead uses three raw vpype-command boxes below, run manually
  // from the UI ahead of time — plotting does not re-run vpype for an
  // expert-mode job. The boxes themselves are a UI-only workflow (see
  // internal `POST /jobs/{id}/optimize-expert/execute`, not part of this
  // API); these fields just carry their saved state.
  "optimize_mode":             "beginner",  // "beginner" | "expert"
  "optimize_expert_1_enabled": false,
  "optimize_expert_1_cmd":     "",           // Raw vpype command fragment, e.g. "linesimplify --tolerance 0.1mm"
  "optimize_expert_2_enabled": false,
  "optimize_expert_2_cmd":     "",
  "optimize_expert_3_enabled": false,
  "optimize_expert_3_cmd":     "",

  // "Grid" layout: tile the whole drawing to fill the sheet. Reversible (a
  // cached derivative selected by grid_enabled). Follows optimize_svg — with it
  // on the optimized geometry is what gets tiled, with it off the drawing as
  // uploaded. Beginner mode only. The columns x rows arrangement is derived
  // from grid_copies, the drawing's aspect ratio and the area inside the
  // margins; grid_copies is rounded up to a full grid (e.g. 5 -> 3x2 = 6).
  // Spacing that is too wide for the resulting cell is capped at a quarter of
  // that cell.
  "grid_enabled":              false,  // Master toggle.
  "grid_copies":               4,      // 2–64. Number of copies to fit on the sheet.
  "grid_fit":                  "page", // "page" fits the drawing's whole page to each cell (margins kept), "ink" fits the drawn geometry alone.
  "grid_spacing_x_mm":         0.0,    // 0–100. Per-side horizontal spacing (see below).
  "grid_spacing_y_mm":         0.0,    // 0–100. Per-side vertical spacing.
  "grid_spacing_linked":       true,   // UI-only: keeps the two spacings equal in the card.
  "grid_cut_marks":            false,  // Trim marks between copies. Adds a "Cut marks" row to layer_selections.

  "layers": [                         // Per-layer overrides keyed by SVG layer index.
    {
      "index": 0,                     // Required — the 0-based Inkscape layer index.
      "name": "string",               // Optional — overrides the embedded `inkscape:label`.
      "type": "pattern" | "text" | "svg" | "calibration" | "image",  // Optional — drives a small icon in the UI. Other values are accepted and fall back to a generic icon.
      "selected": false,              // Optional, default true. `false` excludes the layer from the plot.
      "speed_pendown": 25,            // Optional 1–110 — pen-down speed for this layer only.
      "speed_penup": 75,              // Optional 1–110 — pen-up speed for this layer only.
      "acceleration": 75,             // Optional 1–100 — acceleration for this layer only.
      "pen": {                        // Optional — the pen loaded for this layer.
        "name": "Uni Posca PC-5M White"  // Display only; shown after the layer name.
      }
    }
  ]
}
```

##### Paper size resolution

- **Metadata omitted, or `paper_size` omitted** — paper dimensions are taken straight from the SVG's `width`/`height` attributes (parsed via the SVG's units / `viewBox`). Orientation is implicit in those dimensions: portrait if `width ≤ height`, landscape otherwise. If the resulting size matches a known preset (A0–A6, B0–B5, Letter, Legal, Ledger, ANSI C–E), the web UI labels the job accordingly; otherwise it's shown as a custom size with the raw mm.
- **`paper_size.name` set, `width`/`height` omitted** — `name` must match a known preset (see list below); preset dimensions are used.
- **`paper_size.width` and `paper_size.height` set** — those values are used after unit conversion. `name` is preserved as a display label only.
- **`paper_size.orientation`** — if given, the resolved dimensions are swapped if needed so `width >= height` (landscape) or `width <= height` (portrait).

Known presets: `A0`–`A6`, `B0`–`B5`, `Letter`, `Legal`, `Ledger`, `ANSI-C`, `ANSI-D`, `ANSI-E`. Any other `name` without explicit dimensions returns `400`.

##### Paper stock and pens

`paper.name` and `layers[].pen.name` are free-form descriptive strings — they don't affect plotting, they just record what's physically loaded. Both are optional; omit them and the web UI shows nothing in their place. They are stored on the job record as `paper_name` and, per layer, `pen_name`.

In the web UI the paper stock appears under the page preview, right-aligned opposite the paper size. And the pen name trails its layer's name in the layer list:


##### Layer overrides

`layers[]` is keyed by `index` (matching the SVG's Inkscape layer order, 0-based). Layers not listed keep the SVG's embedded `inkscape:label`, have no `type`, and are **selected** by default. Listed layers can override `name`, `type`, and `selected` independently — supplying only `type` keeps the embedded label, and supplying only `selected: false` excludes the layer from the plot. If every layer is deselected the request returns `400`.

`pen.name` is a display-only note of the pen loaded for that layer — see [Paper stock and pens](#paper-stock-and-pens).

`speed_pendown`, `speed_penup`, and `acceleration` are optional per-layer speed overrides: when set, they take precedence over the job's (document) and the system's speed settings for that layer only. Each axis falls back independently, so you can override just one. Out-of-range values are clamped, not rejected.

Because a layer can only carry its own speed when it's plotted as a separate stage, **any** layer speed override forces per-layer staging — the layers plot as back-to-back stages even when `pause_between_layers` is `false` (the pen returns to the home corner between them). Note that the plot-time estimate is computed once at the job's base speed, so it is approximate when per-layer overrides are in play.

Layer types are decorative — the icon is shown in the layer list:

| Type | Meaning | Icon (web UI) |
|---|---|---|
| `pattern` | Generative / decorative pattern | waveform |
| `text` | Text rendered as paths | text bars |
| `svg` | A vector glyph or composed shape | triangle/circle/square |
| `calibration` | Registration / alignment marks | scope (crosshair-in-circle) |
| `image` | A raster / photo-derived layer | photo (mountains & sun) |

#### Response

`200 OK`, JSON, the full job record:

```jsonc
{
  "job_id": "abc12345",               // Job ID — use this for future per-job actions.
  "status": "ready",
  "created_at": 1777212168.88,
  "svg_id": "1ebd8a27",
  "filename": "APITest.svg",
  "name": "API Test (via GD Studio)",
  "paper_size_name": "A3",
  "paper_name": "FABRIANO Black Black 300g",  // null when `paper` was omitted.
  "layer_selections": [
    { "index": 0, "label": "Guilloché", "type": "pattern", "pen_name": "Uni Posca PC-5M White" },
    { "index": 1, "label": "Text",      "type": "text" },   // `pen_name` absent when `pen` was omitted.
    { "index": 2, "label": "Logo",      "type": "svg" }
  ],
  "paper_width_mm": 420.0,             // Always millimetres, regardless of input unit.
  "paper_height_mm": 297.0,
  "pause_between_layers": true,       // From server-side defaults (Settings).
  "skip_same_pen_pause": false,       // From server-side defaults (Settings).
  "delete_on_complete": false,
  "disable_motors_on_complete": false,
  "speed_pendown": 25,
  "speed_penup": 75,
  "acceleration": 75,
  "pen_pos_up": 60,
  "pen_pos_down": 30,
  "record_plot": false,
  "record_mode": "realtime"
  // ... margins, transforms, timing fields, etc.
}
```

#### Example

```bash
curl -X POST http://plotterosaurus.local/api/v1/jobs \
  -H "X-API-Key: $PLOTTEROSAURUS_API_KEY" \
  -F "file=@/path/to/drawing.svg" \
  -F 'metadata={"name":"Nightly run","paper_size":{"name":"A3","orientation":"landscape"},"paper":{"name":"FABRIANO Black Black 300g"},"layers":[{"index":0,"name":"Outline","type":"pattern","pen":{"name":"Uni Posca PC-5M White"}},{"index":1,"name":"Title","type":"text"}]}'
```

If your shell mangles the inline JSON (extra spaces, broken backslash continuations), put the JSON in a file and reference it:

```bash
curl -X POST http://plotterosaurus.local/api/v1/jobs \
  -H "X-API-Key: $PLOTTEROSAURUS_API_KEY" \
  -F "file=@/path/to/drawing.svg" \
  -F "metadata=<./metadata.json"
```

### Queue control

All endpoints take no body, return `{"ok": true}` on success, and respond `409 Conflict` (with a `detail` message) when the action isn't valid in the current state.

| Method | Path | What it does | 409 conditions |
|---|---|---|---|
| `POST` | `/api/v1/queue/plot` | Plot the topmost `ready` job. The run ends with that job — nothing advances to the next one on its own. | No `ready` job; a job is already plotting. |
| `POST` | `/api/v1/queue/pause` | Pause the active plot. Pen is raised; resumable. | No actively-plotting job. |
| `POST` | `/api/v1/queue/pause-at-pen-up` | Soft pause: defer until the next pen lift, so the pen doesn't stop mid-stroke (useful for pump-action pens). Pauses immediately if the pen is already up. While pending, the snapshot field `pause_at_pen_up_pending` is `true`. | No actively-plotting job. |
| `POST` | `/api/v1/queue/resume` | Resume a paused plot. | No paused job; missing resume data. |
| `POST` | `/api/v1/queue/redraw` | From a `paused` plot, rewind the resume point by `{"distance_mm": 50}` of pen-down travel (1–2000) and carry on — so the last stretch of drawing is traced again (a skipped line, a stretch a dry pen missed) and the plot still finishes. Clamped to the start of the layer currently plotting; can't reach into an earlier layer. | Active job is not `paused`; `distance_mm` out of range. |
| `POST` | `/api/v1/queue/continue` | Advance past a pen-change pause, on to the job's next stage. | Nothing waiting on a continue. |
| `POST` | `/api/v1/queue/calibrate` | At a pen-change pause, plot every layer with `type: "calibration"` (regardless of `selected`) as a one-shot side plot, then return to `awaiting_pen_change`. Lets the user verify pen alignment between layers without advancing the main plot. | Active job is not in `awaiting_pen_change`; job has no calibration-typed layers. |
| `POST` | `/api/v1/queue/nudge-origin` | At a pen-change pause, shift the origin of the remaining (not-yet-plotted) stages by `{"dx_mm": 0.1, "dy_mm": 0.0}` (either field optional, default `0.0`) — for compensating small paper drift between layers. Session-only: not written back to the job's `transform_offset_*_mm`, and resets when the run ends. | Active job is not in `awaiting_pen_change`. |
| `POST` | `/api/v1/queue/cancel` | Cancel the active job. The plotter homes if it can. | No active job. |
| `POST` | `/api/v1/queue/calibrate-file` | At a pen-change pause, plot a standalone SVG from the server's `calibration/` library as a side plot, transformed onto the job's current paper/margins. Body: `{"filename": "grid.svg"}`. | Active job is not in `awaiting_pen_change`; unknown or invalid filename. |
| `POST` | `/api/v1/queue/pen-height` | Live-adjust pen height at a pen-change pause and move the pen so the change is visible. Body: `{"pen_pos_up": 60, "pen_pos_down": 30, "test": "up" \| "down"}` (heights optional, 29–85; `test` required). Persisted onto the active job. | Active job is not in `awaiting_pen_change`. |
| `POST` | `/api/v1/queue/live-settings` | Change speed / acceleration / pen height of the plot in progress, applied at the next segment checkpoint. Body: any of `{"speed_pendown", "speed_penup", "acceleration", "pen_pos_up", "pen_pos_down"}`. | No active job. |

Note: a pen-change pause (`awaiting_pen_change`) only resumes via `/api/v1/queue/continue` — the plotter's physical pause button does **not** auto-continue it (unlike a plain `paused` state, which the button does resume). This is intentional, so there's always a chance to calibrate / jog the pen / nudge the origin first.

### Manual pen control

| Method | Path | What it does | 409 conditions |
|---|---|---|---|
| `POST` | `/api/v1/pen/up` | Raise the pen outside of a plot (no SVG involved), using the active job's pen height if one is loaded, otherwise the system default. | The plotter is actively driving a real plot (`plotting` / `homing` / `plotting_calibration`); connection failure. |
| `POST` | `/api/v1/pen/down` | Lower the pen outside of a plot, same height rules as above. | Same as above. |
| `POST` | `/api/v1/motors/enable` | Energize the stepper motors (holds position). | Same as above. |
| `POST` | `/api/v1/motors/disable` | Release the steppers so the carriage can be pushed by hand. Note that moving it by hand shifts the physical origin, which the server cannot see. | Same as above. |

### Carriage position

Idle-only. Three separate values describe where the carriage is relative to the paper, and each endpoint owns exactly one of them: the *declared origin* (where the page's top-left corner is), the *manual offset* accumulated by jogging away from it, and — during a run only — the origin nudge (see `/api/v1/queue/nudge-origin`). The carriage sits at the sum of all three, and an AxiDraw has no home switches, so wherever it stands when a plot starts becomes that plot's zero.

Both offsets are reported in the state snapshot as `manual_origin_offset_x_mm` / `_y_mm` and `origin_nudge_x_mm` / `_y_mm`.

| Method | Path | What it does | 409 conditions |
|---|---|---|---|
| `POST` | `/api/v1/pen/jog` | Move the carriage pen-up by `{"dx_mm": 5.0, "dy_mm": 0.0}` (either field optional, default `0.0`) and add it to the manual offset. A move that lands above/left of the declared origin is refused with code `jog_below_origin` unless you also send `"confirm_below_origin": true`. A move that would run the carriage past the bed's far edge is refused outright. | Not idle; past the bed's far edge; a move longer than the bed itself; connection failure. |
| `POST` | `/api/v1/pen/jog-home` | Walk the carriage back to the declared origin, clearing the manual offset. No-op when the offset is already zero. | Not idle; connection failure. |
| `POST` | `/api/v1/pen/jog-paper-origin` | Walk the carriage to the active machine profile's paper origin — `paper_origin_x_mm` / `_y_mm` on that machine, measured from the declared origin, so a second call moves nothing. A plain move: it never declares an origin (use `/pen/set-origin` for that). No body. | Not idle; the paper origin lies past the bed's far edge (e.g. after switching to a smaller machine profile); connection failure. |
| `POST` | `/api/v1/pen/set-origin` | Declare wherever the carriage currently sits to be the page's top-left corner: the manual offset folds into the origin and resets to zero. Touches no hardware. | Not idle. |

### Calibration library

| Method | Path | What it does |
|---|---|---|
| `GET` | `/api/v1/calibration/files` | List the standalone calibration SVGs available to `/api/v1/queue/calibrate-file`. |

### Camera / plot recording

Only available when the server was installed with `ENABLE_CAMERA=1` (`camera_enabled: true` in
`GET /api/v1/settings`) — a Pi Camera Module 3 plus a separate [MediaMTX](https://mediamtx.org)
service the installer sets up. Plotterosaurus drives MediaMTX locally; there's no camera-specific
credential to manage. See `camera_*` / `record_plot*` fields under Settings above for the
configurable defaults (resolution, FPS, bitrate, autofocus, output folder, rclone target,
recording mode).

A recording can be started automatically by a job (`record_plot: true` in its metadata — see
`POST /api/v1/jobs`) or manually via the endpoints below, independent of any job. Automatic
recordings pause whenever the job pauses (a pen-change pause or a plain mid-stroke pause) and
resume when it does; the live stream itself is never interrupted by this, only the on-disk
recording. Only one recording (manual or job-driven) can be active at a time.

| Method | Path | What it does | 409 conditions |
|---|---|---|---|
| `POST` | `/api/v1/camera/recording/start` | Start a manual recording (not tied to any job). Body: `{"mode": "realtime"\|"timelapse"\|"sped_up", "timelapse_interval_s": 5.0, "speed_multiplier": 4.0}` — all fields optional, falling back to the configured defaults. | Camera not enabled; a recording is already in progress; MediaMTX unreachable; not enough free disk space. |
| `POST` | `/api/v1/camera/recording/pause` | Pause the active recording (no-op if not recording). | — |
| `POST` | `/api/v1/camera/recording/resume` | Resume a paused recording (no-op if not paused). | — |
| `POST` | `/api/v1/camera/recording/stop` | Stop and finalize the active recording (no-op if idle). Finalization (segment concatenation, speed post-processing, optional `rclone copy`) runs in the background after this returns. | — |
| `POST` | `/api/v1/camera/focus` | Live autofocus control. Body: `{"af_mode": "auto"\|"manual"\|"continuous", "lens_position": 0.0}` (`lens_position` only applies in `"manual"` mode; distance (m) = 1/value, 0 = infinity). Applied immediately and persisted to settings. | Camera not enabled; MediaMTX unreachable; invalid `af_mode`. |
| `GET` | `/api/v1/camera/status` | Returns `{"camera_enabled", "recording_status": "idle"\|"recording"\|"paused", "recording_job_id", "rtsp_url", "hls_url", "webrtc_view_url"}`. The stream URLs are always derived from the request's own host, so they're correct for whatever hostname/IP you reached the server on. | — |

Starting a recording is refused when the volume holding the recordings is nearly full — capture
is only part of the cost, since assembling the finished file writes a second copy of it (a third
in `sped_up` mode). If assembly fails anyway, the raw segments are kept under
`recordings/_failed_segments/` instead of being deleted, and `GET /camera/recordings` reports them
as `failed_finalizes`.

Finished recordings land in `camera_output_folder` as `<job_id>.mp4` (or `manual-<timestamp>.mp4`
for a manual recording), and are additionally copied via `rclone copy <file>
<camera_rclone_target>` in the background if a target is configured — `rclone` itself must
already be installed and authenticated on the Pi; Plotterosaurus never stores cloud credentials.

#### Lifecycle cheat sheet

```
ready ──plot──► [optimizing] ──► planning ──► plotting ──pause──► paused ──resume──► plotting
                                                  │                                       │
                                                  └──► awaiting_pen_change ──continue──► (next stage)
                                                              │  ▲                       │
                                                              │  └── calibrate ◄──┐      │
                                                              ▼                   │      │
                                                        plotting_calibration ─────┘      │
                                                                                         │
                                                                                ──cancel──► homing ──► cancelled
```

`plotting_calibration` is entered from `awaiting_pen_change` via `/queue/calibrate`. It's a self-contained side plot of the calibration-typed layers; on completion the worker returns to `awaiting_pen_change` and the user can calibrate again, continue, or cancel.

`optimizing` is only entered when the job has `optimize: true` AND its cached
optimized SVG either doesn't exist or was produced with different parameters.
On subsequent re-plots of the same job the cache is reused and the worker
goes straight to `planning`.

#### Example

```bash
curl -X POST http://plotterosaurus.local/api/v1/queue/plot \
  -H "X-API-Key: $PLOTTEROSAURUS_API_KEY"
```

### Per-job CRUD

All routes require `X-API-Key`. Job IDs are 8-hex-char strings returned from `POST /api/v1/jobs`. A `404 Not Found` is returned if the job ID doesn't exist.

#### `GET /api/v1/jobs` — list

Returns the full queue snapshot, mirroring what the WebSocket broadcasts:

```jsonc
{
  "queue":   [ /* array of job records, in list order */ ],
  "active_id": "abc12345",          // null if no active job
  "status": "plotting"              // top-level worker status
}
```

#### `GET /api/v1/jobs/{job_id}` — get one

Returns the full job record (same shape as the `POST /api/v1/jobs` response).

#### `PATCH /api/v1/jobs/{job_id}` — edit

Body is JSON. All fields optional; only the fields you send are applied. To clear a nullable field (e.g. `paper_size_name`), send it explicitly as `null` — *omitted* fields are ignored, *null* fields are cleared.

Numeric fields with a documented range below (margins, transforms, plotter speeds, optimize tolerance) are silently clamped to the nearest bound rather than returning `400`. Margins are floored at `0`; `transform_offset_x_mm` / `transform_offset_y_mm` are clamped to `±paper_width_mm` / `±paper_height_mm`.

Editable fields:

| Field | Type | Notes |
|---|---|---|
| `name` | string \| null | Display name override. Renaming a job also renames its source drawing in the library. |
| `layer_mode` | `"layer"` \| `"group"` \| `"pen"` | How the drawing is split into rows / plot stages. Switching it re-points the job at a re-partitioned copy and rebuilds `layer_selections` — any custom row labels are lost. |
| `paper_size_name` | string \| null | Display label for the paper size. |
| `paper_name` | string \| null | Display label for the paper stock (e.g. `"FABRIANO Black Black 300g"`). |
| `paper_width_mm`, `paper_height_mm` | number | Paper dimensions; always in mm. |
| `margin_top_mm`, `margin_right_mm`, `margin_bottom_mm`, `margin_left_mm` | number | |
| `fit_content` | bool | Scale SVG to fit the printable area. |
| `transform_scale` | number | 0.01–5.0 |
| `transform_rotation_deg` | number | 0–360 |
| `transform_offset_x_mm`, `transform_offset_y_mm` | number | |
| `speed_pendown`, `speed_penup` | int | 1–110 |
| `acceleration` | int | 1–100 |
| `pen_pos_up`, `pen_pos_down` | int | 0–100 |
| `record_plot` | bool | Record this job via the camera — see "Camera / plot recording" below. Only meaningful when the server has `camera_enabled`. |
| `record_mode` | `"realtime"` \| `"timelapse"` \| `"sped_up"` | |
| `record_timelapse_interval_s` | number | 0.5–3600 |
| `record_speed_multiplier` | number | 1.1–60 |
| `pause_between_layers`, `delete_on_complete` | bool | `pause_between_layers` pauses only resume via `/queue/continue`, never the physical button — see the note under Queue control. |
| `skip_same_pen_pause` | bool | With `pause_between_layers` on, suppresses the pause between consecutive layers whose stroke colour and width both match (same pen, so nothing to swap). Colour/width are read from the un-optimized source; a layer whose pen can't be resolved keeps its pause. |
| `disable_motors_on_complete` | bool | After the plot finishes and returns to the origin, cut torque to the XY steppers so they don't sit warm. Only on natural completion — not on cancel or failure. |
| `optimize_svg` | bool | Run the vpype optimization pipeline before planning. |
| `optimize_svg_tolerance_mm` | number | 0.01–10.0 |
| `optimize_svg_linemerge`, `optimize_svg_linesimplify`, `optimize_svg_linesort`, `optimize_svg_reloop` | bool | Per-step toggles for the vpype pipeline. |
| `optimize_mode` | `"beginner"` \| `"expert"` | See the metadata schema above. |
| `optimize_expert_1_enabled`, `optimize_expert_2_enabled`, `optimize_expert_3_enabled` | bool | |
| `optimize_expert_1_cmd`, `optimize_expert_2_cmd`, `optimize_expert_3_cmd` | string | Raw vpype command fragment for that box. |
| `grid_enabled` | bool | "Grid" layout: tile the whole drawing to fill the sheet before placement. Reversible; follows `optimize_svg` (the optimized geometry when it is on, the upload as-is when it is off); beginner mode only. |
| `grid_copies` | int | 2–64. Copies to fit on the sheet; rounded up to a full columns×rows grid, arranged to fill the area inside the margins. |
| `grid_fit` | `"page"` \| `"ink"` | What "fill the sheet" scales into each cell. `"page"` (default) fits the drawing's whole page — its own width/height, whitespace kept — the same rule "fit to page" uses. `"ink"` fits the drawn geometry's bounding box, blowing each copy up to the cell edges. |
| `grid_spacing_x_mm`, `grid_spacing_y_mm` | number | 0–100. Per-side spacing: pads every edge of every copy, so neighbours end up `2×` the value apart and the outer copies are inset that value from the sheet edge (a margin on top of the page margins). Each axis is capped at a quarter of its spacing-free cell. |
| `grid_spacing_linked` | bool | UI convenience only — the card keeps the two spacings equal while set. Ignored by the tiler. |
| `grid_cut_marks` | bool | Add cutting marks to the tiled sheet: a short tick where a cut between two copies reaches the sheet edge, a small cross where two cuts meet between four; interior joins only. Setting this synthesises a leading `layer_selections` entry (`label: "Cut marks"`, `cut_marks: true`, `index` = artwork layer count) — its own row in the layer list: reorder it, or deselect it to skip the marks for a run without regenerating the sheet. Clearing `grid_cut_marks` (or `grid_enabled`) removes the row. |
| `layer_selections` | array | `[{index, label, type?, selected?, pen_name?, cut_marks?}]` — drives which layers plot, in list order. Entries with `selected: false` are kept in the list (so name/type metadata survives a toggle in the UI) but skipped when planning. `cut_marks: true` marks the server-managed grid cut-marks row (see `grid_cut_marks`). |

Returns the full updated job record. **`409 Conflict`** if the job is currently active (`plotting`, `planning`, `paused`, `awaiting_pen_change`, `homing`).

A side-effect to be aware of: editing a job that's in a terminal state (`completed`, `failed`, `cancelled`) automatically transitions it back to `ready` so a re-plot doesn't need a separate `/requeue` call.

#### `POST /api/v1/jobs/{job_id}/move` — reorder

Body: `{"new_index": <0-based int>}`. Returns `{"ok": true}`. **`409 Conflict`** if the job is active.

#### `POST /api/v1/jobs/{job_id}/requeue` — reset to `ready`

No body. Puts a finished or cancelled job back to `ready`, clearing its stages, estimate and error. Returns the updated job record. Idempotent on jobs that are already `ready` (returns the existing record). **`409 Conflict`** if the job is active.

#### `POST /api/v1/jobs/{job_id}/placement` — preview a placement

Body: any of `{"transform_scale", "transform_rotation_deg", "transform_offset_x_mm", "transform_offset_y_mm", "fit_content"}`. Returns where the artwork would land on the page for those values, without storing them — the same answer the plot itself uses (see `app/placement.py`). **`409 Conflict`** if the job is active.

#### `DELETE /api/v1/jobs/{job_id}` — remove

No body. Returns `{"ok": true}`. Removes the job from the queue **and deletes the uploaded SVG** plus all on-disk derivatives (preview / filtered / staged / resume). **`409 Conflict`** if the job is active.

### Live state stream

#### `WS /api/v1/ws/state`

Streams the same JSON messages the web UI consumes — every queue mutation, status change, and live pen-position tick.

**Authentication.** Either:

- `X-API-Key: <key>` header on the upgrade request (preferred), or
- `?api_key=<key>` query parameter (for clients like the browser `WebSocket` API that can't set custom headers on a handshake).

If the key is missing or wrong, the server **rejects the WebSocket upgrade with HTTP 403** — the connection is refused before any frames are exchanged.

#### Message shape

The first message after `accept()` is always a full `state` snapshot:

```jsonc
{
  "type": "state",
  "queue": [ /* job records */ ],
  "active_id": "abc12345",
  "status": "plotting",
  "error": null,

  // Carriage position relative to the declared origin, in mm — see
  // "Carriage position" above.
  "manual_origin_offset_x_mm": 0.0,
  "manual_origin_offset_y_mm": 0.0,
  "origin_nudge_x_mm": 0.0,
  "origin_nudge_y_mm": 0.0
}
```

Subsequent messages are either further `state` updates (whenever the queue or any job changes) or pen-position ticks:

```jsonc
{ "type": "position", "x_mm": 123.4, "y_mm": 56.7, "pen_down": true }
```

Clients should switch on `type` and treat unknown types as forward-compat noise.

#### Example (CLI)

`~/Desktop/Examples/plotterosaurus-api-test-ws.sh` — pure-stdlib Python wrapped in a shell launcher; streams every frame to stdout, pretty-printed. Honors `PLOTTEROSAURUS_HOST` / `PLOTTEROSAURUS_API_KEY` env overrides.

### Settings

Server-wide defaults that new jobs inherit (the same set the web UI exposes in its Settings modal).

#### `GET /api/v1/settings`

Returns the current snapshot. The `api_key` is never echoed back — clients already have it (they used it to authenticate this request).

```jsonc
{
  "plotter_model": 2,                           // 1–8 (see install.sh / Settings UI for the table)
  "pause_between_layers_default": true,
  "skip_same_pen_pause_default": false,         // new jobs' "don't pause between same-pen layers" checkbox
  "layer_mode_default": "layer",                // "layer" | "group" | "pen" — new jobs' layer grouping
  "delete_on_complete_default": false,
  "disable_motors_on_complete_default": false,  // Default for a new job's disable-motors-on-complete checkbox
  "speed_pendown_default": 25,                  // 1–110
  "speed_penup_default": 75,                    // 1–110
  "acceleration_default": 75,                   // 1–100
  "pen_pos_up_default": 60,                     // 0–100
  "pen_pos_down_default": 30,                   // 0–100
  "optimize_svg_default": true,                 // Run vpype before plotting on new jobs
  "optimize_svg_tolerance_default_mm": 0.10,    // 0.01–10.0
  "optimize_svg_linemerge_default": true,
  "optimize_svg_linesimplify_default": true,
  "optimize_svg_linesort_default": true,
  "optimize_svg_reloop_default": true,
  "saved_pen_colors": [],                       // curated palette for the layer-colour popover; array of "#rrggbb", max 24
  "display_unit": null,                         // null | "mm" | "cm" | "in" — UI labels only
  "machine_custom_enabled": false,              // Custom bed-size profile layered on plotter_model (UI/bounds only)
  "machine_width_mm": 297.0,
  "machine_height_mm": 420.0,
  "machine_auto_rotate": "off",                 // "off" | "portrait" | "landscape"
  "webhook_url": null,                          // POSTed a JSON payload on layer/job completion — see below
  "webhook_on_layer_complete": false,
  "webhook_on_job_complete": false,
  "camera_enabled": false,                      // true only if the server was installed with ENABLE_CAMERA=1
  "camera_resolution_width": 1920,
  "camera_resolution_height": 1080,
  "camera_fps": 30,
  "camera_bitrate": 5000000,
  "camera_af_mode": "continuous",               // "auto" | "manual" | "continuous"
  "camera_lens_position": 0.0,                  // used only when camera_af_mode is "manual"; distance (m) = 1/value, 0 = infinity
  "camera_output_folder": "recordings",         // local path on the Pi, relative to the install dir unless absolute
  "camera_rclone_target": null,                 // e.g. "gdrive:Plotterosaurus/Recordings" — see "Camera / plot recording" below
  "camera_retention_gb": 10.0,                  // cap on the local recordings folder; oldest deleted first, 0 keeps everything
  "camera_recording_mode_default": "realtime",  // "realtime" | "timelapse" | "sped_up"
  "camera_timelapse_interval_s_default": 5.0,
  "camera_speed_multiplier_default": 4.0,
  "record_plot_default": false,
  "draw_stream_enabled": false,                 // true only if the server was installed with ENABLE_DRAW_STREAM=1
  "draw_stream_stroke_width_px": 4,             // fallback line width (px) for content with no resolvable SVG stroke-width
  "draw_stream_background": "black",            // "black" | "white"
  "draw_stream_max_resolution_px": 2560         // cap on the live-page canvas's longer edge, in px
}
```

`display_unit` only affects how the web UI renders paper-size and SVG-dimension labels. Internal storage and inputs always stay in mm. When the field is `null` (no preference saved yet), the browser picks an initial value from `navigator.language` (en-US → in, otherwise mm); once the user saves a choice it overrides the locale fallback on every subsequent load.

`machine_width_mm`/`machine_height_mm`/`machine_auto_rotate` only take effect when `machine_custom_enabled` is `true`. They don't change `pyaxidraw`'s real travel bounds (still governed entirely by `plotter_model`) — they're a software bed-size profile the web UI uses for paper-fit bounds warnings and for auto-rotating a job's paper to match the bed's preferred orientation.

#### `PATCH /api/v1/settings`

Body is sparse JSON — only the fields you send are applied. Returns the new snapshot.

| Field | Range / Type |
|---|---|
| `plotter_model` | int 1–8 |
| `pen_pos_up_default`, `pen_pos_down_default` | int 0–100 |
| `machine_custom_enabled` | bool |
| `machine_width_mm`, `machine_height_mm` | number > 0 |
| `machine_auto_rotate` | `"off"` \| `"portrait"` \| `"landscape"` |
| `webhook_url` | string. PATCH cannot clear it back to `null` — same limitation as `display_unit`. |
| `webhook_on_layer_complete`, `webhook_on_job_complete` | bool |
| `camera_enabled` | bool. Set at install time via `ENABLE_CAMERA=1`; can be toggled here too, but the MediaMTX service itself is only installed when the installer flag was used. |
| `camera_resolution_width`, `camera_resolution_height` | int > 0 |
| `camera_fps` | int 1–120 |
| `camera_bitrate` | int > 0 (bits/sec) |
| `camera_af_mode` | `"auto"` \| `"manual"` \| `"continuous"` |
| `camera_lens_position` | number 0–32 |
| `camera_output_folder` | string (local path) |
| `camera_rclone_target` | string, e.g. `"gdrive:Plotterosaurus/Recordings"`. Empty disables cloud sync. |
| `camera_retention_gb` | number ≥ 0 (GB). Total size cap on the local recordings folder, enforced after each finished recording, oldest first; `0` keeps everything. A recording whose upload hasn't landed yet is never deleted. |
| `camera_recording_mode_default` | `"realtime"` \| `"timelapse"` \| `"sped_up"` |
| `camera_timelapse_interval_s_default` | number > 0 |
| `camera_speed_multiplier_default` | number > 1.0 |
| `record_plot_default` | bool |
| `draw_stream_enabled` | bool. Set at install time via `ENABLE_DRAW_STREAM=1`; can be toggled here too — see the `/draw-stream` live page in README.md. |
| `draw_stream_stroke_width_px` | int 1–40 |
| `draw_stream_background` | `"black"` \| `"white"` |
| `draw_stream_max_resolution_px` | int 480–4096 |
| `pause_between_layers_default` | bool |
| `skip_same_pen_pause_default` | bool |
| `layer_mode_default` | `"layer"` \| `"group"` \| `"pen"` |
| `delete_on_complete_default` | bool |
| `disable_motors_on_complete_default` | bool |
| `speed_pendown_default` | int 1–110 |
| `speed_penup_default` | int 1–110 |
| `acceleration_default` | int 1–100 |
| `optimize_svg_default` | bool |
| `optimize_svg_tolerance_default_mm` | float 0.01–10.0 |
| `optimize_svg_linemerge_default`, `optimize_svg_linesimplify_default`, `optimize_svg_linesort_default`, `optimize_svg_reloop_default` | bool |
| `saved_pen_colors` | array of `"#rrggbb"` strings. Normalised on save: lower-cased, non-hex dropped, de-duplicated, capped at 24. |
| `display_unit` | `"mm"` \| `"cm"` \| `"in"` — UI display only. PATCH cannot clear it back to `null`; that state only exists before any value has been saved. |

Out-of-range values return `400`. The API key cannot be set through this endpoint — to rotate it, edit `config.json` on the Pi and restart the service.

```bash
curl -X PATCH http://plotterosaurus.local/api/v1/settings \
  -H "X-API-Key: $PLOTTEROSAURUS_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"speed_pendown_default": 30, "delete_on_complete_default": true}'
```

### Outgoing webhooks

When `webhook_url` is set, Plotterosaurus POSTs a JSON payload to it — fire-and-forget from a background thread, with a 5s timeout; delivery failures are logged locally and never affect the plot. Independently toggled per event via `webhook_on_layer_complete` / `webhook_on_job_complete`:

```jsonc
// event: "layer_complete" — one selected layer (a stage) just finished plotting.
{
  "event": "layer_complete",
  "timestamp": 1777212168.88,
  "job_id": "abc12345",
  "job_name": "Nightly run",
  "stage_label": "Outline",
  "stage_index": 0,
  "stage_count": 3
}
```

```jsonc
// event: "job_complete" — every stage of the job finished.
{
  "event": "job_complete",
  "timestamp": 1777212500.12,
  "job_id": "abc12345",
  "job_name": "Nightly run"
}
```

There's no dedicated `/api/v1/*` endpoint for this — it's an outbound integration, not something a client calls. Point it at something that can turn a POST into a push/text/email (ntfy, Home Assistant, a Slack/Discord incoming webhook, etc.) if that's the end goal.

### System

#### `GET /api/v1/version`

Returns the running Plotterosaurus version (read from the `VERSION` file at install time):

```json
{ "version": "1.0.2" }
```

Useful for an "About" surface in your client and for compatibility checks against future API revisions.

#### `POST /api/v1/system/shutdown`

Powers off the Raspberry Pi. The HTTP response is flushed first, then the system halts roughly 1.5 seconds later (the service unit is also stopped along with the OS). No body; returns `{"ok": true}` immediately on dispatch.

**Be careful** — there's no abort once the request is accepted. The web UI guards this behind a confirmation modal; an external client should do the same. Don't issue a shutdown while a plot is running: the plotter is left wherever the pen happens to be, and on next boot the queue rehydrates with a paused job whose pen is no longer in a known position.

```bash
curl -X POST http://plotterosaurus.local/api/v1/system/shutdown \
  -H "X-API-Key: $PLOTTEROSAURUS_API_KEY"
```
