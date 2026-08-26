"""Runtime configuration.

Loads from a JSON file on disk (writable by the service user) and falls back
to the PLOTTER_MODEL environment variable and hardcoded defaults. Edits from
the UI persist to the JSON file.

Settings are described once in the ``_SETTINGS`` table; load / snapshot /
update derive everything from it. Adding a new setting is a one-line change.
External callers keep accessing values as module-level uppercase attributes
(e.g. ``config.PLOTTER_MODEL``) — the table writes them via ``globals()``.
"""
import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
VERSION_PATH = BASE_DIR / "VERSION"
# User-maintained library of standalone calibration-test SVGs (pen-pressure
# grids, alignment marks, etc.), run from an awaiting_pen_change pause. Not
# tied to any particular job.
CALIBRATION_DIR = BASE_DIR / "calibration"
CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)

# Wall-clock cap on one expert-mode vpype run (app/svg_optimize.py
# run_custom_pipeline). Raw user-typed commands aren't otherwise bounded, and
# a stuck subprocess would hold the single heavy-work slot (app/workload.py)
# indefinitely. Not user-configurable — there's no UI need for it.
OPTIMIZE_EXPERT_TIMEOUT_S = 180


def _read_version() -> str:
    try:
        return VERSION_PATH.read_text().strip() or "unknown"
    except OSError:
        return "unknown"


APP_VERSION: str = _read_version()


@dataclass(frozen=True)
class _Setting:
    name: str                                # snake_case key in config.json
    type: type                               # int, bool, float, or str
    default: Any
    validate: Callable[[Any], bool] | None = None  # returns True if value is acceptable


_SETTINGS: list[_Setting] = [
    _Setting("plotter_model", int, int(os.environ.get("PLOTTER_MODEL", "2")),
             lambda v: 1 <= v <= 8),
    _Setting("pause_between_layers_default", bool, True),
    _Setting("delete_on_complete_default", bool, False),
    _Setting("speed_pendown_default", int, 25, lambda v: 1 <= v <= 110),
    _Setting("speed_penup_default", int, 75, lambda v: 1 <= v <= 110),
    _Setting("acceleration_default", int, 75, lambda v: 1 <= v <= 100),
    _Setting("pen_pos_up_default", int, 60, lambda v: 29 <= v <= 85),
    _Setting("pen_pos_down_default", int, 30, lambda v: 29 <= v <= 85),
    _Setting("optimize_svg_default", bool, True),
    _Setting("optimize_svg_tolerance_default_mm", float, 0.10,
             lambda v: 0.01 <= v <= 10.0),
    _Setting("optimize_svg_linemerge_default", bool, True),
    _Setting("optimize_svg_linesimplify_default", bool, True),
    _Setting("optimize_svg_linesort_default", bool, True),
    _Setting("optimize_svg_reloop_default", bool, True),
    # Expert-mode raw vpype command boxes: remembers the last text typed into
    # each box (any job), so a newly created job starts pre-filled with it.
    _Setting("optimize_expert_1_cmd_default", str, ""),
    _Setting("optimize_expert_2_cmd_default", str, ""),
    _Setting("optimize_expert_3_cmd_default", str, ""),
    _Setting("display_unit", str, None,
             lambda v: v in ("mm", "cm", "in")),
    # Last update the user chose to skip. The update banner stays hidden while
    # this equals the latest remote version; a newer release re-shows it.
    _Setting("skipped_version", str, None),
    # The machine itself lives in MACHINES/ACTIVE_MACHINE_ID below, not here —
    # its shape is a list of records rather than a scalar. plotter_model stays
    # only because pyaxidraw wants a model number; see _MACHINE_PRESETS.
    # Outgoing webhook fired on layer/job completion (see app/notify.py).
    _Setting("webhook_url", str, None),
    _Setting("webhook_on_layer_complete", bool, False),
    _Setting("webhook_on_job_complete", bool, False),
    # Plot recording via a Camera Module 3 + MediaMTX (see app/camera.py).
    # Opt-in: only present when install.sh was run with ENABLE_CAMERA=1 (mirrors
    # how plotter_model reads PLOTTER_MODEL), since most installs have no camera.
    _Setting("camera_enabled", bool, os.environ.get("ENABLE_CAMERA") == "1"),
    _Setting("camera_resolution_width", int, 1920, lambda v: v > 0),
    _Setting("camera_resolution_height", int, 1080, lambda v: v > 0),
    _Setting("camera_fps", int, 30, lambda v: 1 <= v <= 120),
    _Setting("camera_bitrate", int, 5_000_000, lambda v: v > 0),
    _Setting("camera_af_mode", str, "continuous",
             lambda v: v in ("auto", "manual", "continuous")),
    # Manual-focus lens position; only used while camera_af_mode == "manual".
    # MediaMTX's rpiCameraLensPosition: focus distance (m) = 1 / value, 0 = infinity.
    _Setting("camera_lens_position", float, 0.0, lambda v: 0.0 <= v <= 32.0),
    # How aggressively autofocus hunts for focus; "fast" reacts quicker at
    # the cost of more visible lens motion/settling.
    _Setting("camera_af_speed", str, "normal", lambda v: v in ("normal", "fast")),
    # Image tuning, passed straight through to MediaMTX's rpiCamera source
    # (same value ranges/semantics as the underlying libcamera controls).
    _Setting("camera_brightness", float, 0.0, lambda v: -1.0 <= v <= 1.0),
    _Setting("camera_contrast", float, 1.0, lambda v: 0.0 <= v <= 16.0),
    _Setting("camera_saturation", float, 1.0, lambda v: 0.0 <= v <= 16.0),
    _Setting("camera_sharpness", float, 1.0, lambda v: 0.0 <= v <= 16.0),
    # Exposure compensation, in EV stops.
    _Setting("camera_ev", float, 0.0, lambda v: -10.0 <= v <= 10.0),
    # Which AGC exposure profile to bias toward; "short" favors a faster
    # shutter (less motion blur) over a longer one, even while auto-exposing.
    _Setting("camera_exposure_mode", str, "normal",
             lambda v: v in ("normal", "short", "long", "custom")),
    # Fixed shutter speed in microseconds; 0 means auto (governed by
    # camera_exposure_mode above). A short fixed value freezes motion instead
    # of letting auto-exposure pick a slower shutter that smears/wobbles it.
    _Setting("camera_shutter_us", int, 0, lambda v: v >= 0),
    # Mains-flicker mitigation for AC-powered lighting, mapped to a raw
    # rpiCameraFlickerPeriod microsecond value in app/camera.py. "off"
    # preserves prior behavior (no correction).
    _Setting("camera_flicker_mode", str, "off",
             lambda v: v in ("off", "50hz", "60hz")),
    _Setting("camera_awb_mode", str, "auto",
             lambda v: v in ("auto", "incandescent", "tungsten", "fluorescent",
                             "indoor", "daylight", "cloudy")),
    # Fixed analogue gain (the "ISO" knob); 0 means auto.
    _Setting("camera_gain", float, 0.0, lambda v: v >= 0.0),
    _Setting("camera_denoise", str, "off",
             lambda v: v in ("off", "cdn_off", "cdn_fast", "cdn_hq")),
    _Setting("camera_hflip", bool, False),
    _Setting("camera_vflip", bool, False),
    _Setting("camera_output_folder", str, "recordings"),
    # rclone remote:path target for a post-recording `rclone copy`. Empty/None
    # disables cloud sync — Plotterosaurus never stores cloud credentials itself.
    _Setting("camera_rclone_target", str, None),
    # Delete the local recording once `rclone copy` above confirms success.
    # Ignored if camera_rclone_target is unset (nothing to confirm against).
    _Setting("camera_rclone_delete_local", bool, False),
    _Setting("camera_recording_mode_default", str, "realtime",
             lambda v: v in ("realtime", "timelapse", "sped_up")),
    _Setting("camera_timelapse_interval_s_default", float, 5.0, lambda v: v > 0),
    _Setting("camera_speed_multiplier_default", float, 4.0, lambda v: v > 1.0),
    _Setting("record_plot_default", bool, False),
    # Live "draw progress" page for an OBS Browser Source (see app/main.py's
    # /draw-stream routes). Opt-in the same way camera_enabled is: only present
    # when install.sh was run with ENABLE_DRAW_STREAM=1.
    _Setting("draw_stream_enabled", bool, os.environ.get("ENABLE_DRAW_STREAM") == "1"),
    # Fallback stroke width (px) for content with no resolvable SVG
    # stroke-width (e.g. fill-only shapes) — the normal case reads width and
    # color straight off the SVG (see static/draw-stream.js resolveLayerColor
    # / resolveLayerWidth), scaled to canvas px via the same mm-per-px factor
    # used for pen position, so line weight always matches the real plot.
    _Setting("draw_stream_stroke_width_px", int, 4, lambda v: 1 <= v <= 40),
    _Setting("draw_stream_background", str, "black", lambda v: v in ("black", "white")),
    # The canvas is always sized to the active job's own paper aspect ratio
    # (no separate ratio setting) — this just caps how many pixels its longer
    # edge renders at, for sharper lines on larger paper sizes.
    _Setting("draw_stream_max_resolution_px", int, 2560, lambda v: 480 <= v <= 4096),
]

# Static API key for /api/v1/* routes — kept outside the schema because it
# auto-generates when missing rather than falling back to a hardcoded default.
API_KEY: str = ""


# Machine profiles ---------------------------------------------------------
#
# A machine is a name and a bed, and that really is all of it. pyaxidraw's
# `model` option selects nothing but travel bounds (axidraw.py
# update_options) — step resolution, servo timing and speeds are all
# model-independent — and plot_worker._apply_bed_size already overwrites
# those bounds with the profile's own size. So the bed fully describes the
# machine here, and a custom build is expressible exactly as precisely as a
# stock AxiDraw: no model has to be picked to stand in for it.
#
# Seeds for a fresh install, taken from the stock AxiDraw travel figures in
# axidrawinternal.axidraw_conf (x/y_travel_*, inches -> mm), in model-number
# order so a pre-profiles config.json can map its plotter_model onto one.
# They're a starting point only — every entry is renameable, resizable and
# removable from Settings.
_MACHINE_PRESETS: list[dict] = [
    {"name": "AxiDraw V2 / V3 / SE A4", "width_mm": 300.0, "height_mm": 217.9},
    {"name": "AxiDraw SE A3 / iDraw H SE A3", "width_mm": 430.0, "height_mm": 296.9},
    {"name": "AxiDraw V3 XLX", "width_mm": 594.9, "height_mm": 217.9},
    {"name": "AxiDraw MiniKit", "width_mm": 160.0, "height_mm": 101.6},
    {"name": "AxiDraw SE A1", "width_mm": 864.1, "height_mm": 594.1},
    {"name": "AxiDraw SE A2", "width_mm": 594.1, "height_mm": 432.1},
    {"name": "AxiDraw V3 B6", "width_mm": 190.0, "height_mm": 140.0},
    {"name": "AxiDraw V3 Wide", "width_mm": 300.0, "height_mm": 217.9},
]

_AUTO_ROTATE_VALUES = ("off", "portrait", "landscape")
_MACHINE_NAME_MAX = 60

MACHINES: list[dict] = []
ACTIVE_MACHINE_ID: str = ""

# Mirrors of the active profile. Everything that asks about the machine —
# plot_worker's bounds and auto-rotate, the settings payload the UI reads —
# went on reading these plain scalars when profiles arrived, so introducing
# them changed no call sites. MACHINE_CUSTOM_ENABLED is now always true: a
# profile always states its own bed, so there is no longer a "no custom bed
# configured" case for the auto-rotate gate to fall through to.
MACHINE_CUSTOM_ENABLED: bool = True
MACHINE_WIDTH_MM: float = _MACHINE_PRESETS[1]["width_mm"]
MACHINE_HEIGHT_MM: float = _MACHINE_PRESETS[1]["height_mm"]
MACHINE_AUTO_ROTATE: str = "off"


def _normalize_machine(raw: Any, used_ids: set[str]) -> dict | None:
    """Coerce one incoming machine record into storable shape, or None if it
    can't be salvaged. Ids are made unique here rather than trusted, since
    they come from the client and a collision would make two profiles
    indistinguishable to every lookup."""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    try:
        width = float(raw.get("width_mm"))
        height = float(raw.get("height_mm"))
    except (TypeError, ValueError):
        return None
    if not (width > 0 and height > 0):
        return None
    auto_rotate = raw.get("auto_rotate")
    if auto_rotate not in _AUTO_ROTATE_VALUES:
        auto_rotate = "off"
    machine_id = str(raw.get("id") or "").strip()
    while not machine_id or machine_id in used_ids:
        machine_id = secrets.token_hex(4)
    used_ids.add(machine_id)
    return {"id": machine_id, "name": name[:_MACHINE_NAME_MAX],
            "width_mm": width, "height_mm": height, "auto_rotate": auto_rotate}


def _seed_machines(data: dict) -> tuple[list[dict], str]:
    """Build the profile list for a config.json written before profiles
    existed: the stock presets, plus the user's own bed as a real profile if
    they had the custom-bed checkbox on. Their selected plotter_model picks
    which preset starts out active, so an install that never touched the
    custom bed keeps exactly the machine it had."""
    machines = [{"id": f"m{i + 1}", "auto_rotate": "off", **preset}
                for i, preset in enumerate(_MACHINE_PRESETS)]
    try:
        index = int(data.get("plotter_model")) - 1
    except (TypeError, ValueError):
        index = 1
    if not 0 <= index < len(machines):
        index = 1
    active_id = machines[index]["id"]

    if data.get("machine_custom_enabled"):
        # Auto-rotate only ever applied while the custom bed was on, so an
        # install with the checkbox off carries no orientation policy over.
        custom = _normalize_machine({
            "id": "m0",
            "name": "My machine",
            "width_mm": data.get("machine_width_mm"),
            "height_mm": data.get("machine_height_mm"),
            "auto_rotate": data.get("machine_auto_rotate"),
        }, {m["id"] for m in machines})
        if custom is not None:
            machines.insert(0, custom)
            active_id = custom["id"]
    return machines, active_id


def active_machine() -> dict:
    """The profile every bounds question resolves against. MACHINES is never
    empty (see _load_machines), so this always answers."""
    for machine in MACHINES:
        if machine["id"] == ACTIVE_MACHINE_ID:
            return machine
    return MACHINES[0]


def _sync_active_machine() -> None:
    global ACTIVE_MACHINE_ID, MACHINE_WIDTH_MM, MACHINE_HEIGHT_MM, MACHINE_AUTO_ROTATE
    machine = active_machine()
    ACTIVE_MACHINE_ID = machine["id"]
    MACHINE_WIDTH_MM = machine["width_mm"]
    MACHINE_HEIGHT_MM = machine["height_mm"]
    MACHINE_AUTO_ROTATE = machine["auto_rotate"]


def _load_machines(data: dict) -> None:
    global MACHINES, ACTIVE_MACHINE_ID
    raw = data.get("machines")
    machines: list[dict] = []
    if isinstance(raw, list):
        used_ids: set[str] = set()
        for item in raw:
            machine = _normalize_machine(item, used_ids)
            if machine is not None:
                machines.append(machine)
    if machines:
        MACHINES = machines
        ACTIVE_MACHINE_ID = str(data.get("active_machine_id") or "")
    else:
        # No usable list — either a config.json from before profiles, or one
        # whose every entry was corrupt. Either way, rebuild rather than leave
        # the app with no machine to measure against.
        MACHINES, ACTIVE_MACHINE_ID = _seed_machines(data)
    _sync_active_machine()


def _update_machines(machines: Any, active_id: Any) -> None:
    """Apply a settings edit. A list that normalizes to nothing is ignored
    outright: an empty machine list has no meaningful bed, and silently
    accepting one would leave every bounds check with nothing to answer."""
    global MACHINES, ACTIVE_MACHINE_ID
    if machines is not None:
        normalized: list[dict] = []
        used_ids: set[str] = set()
        for item in machines if isinstance(machines, list) else []:
            machine = _normalize_machine(item, used_ids)
            if machine is not None:
                normalized.append(machine)
        if not normalized:
            log.warning("config: ignoring a machine list with no usable entries")
        else:
            MACHINES = normalized
    if active_id is not None:
        ACTIVE_MACHINE_ID = str(active_id)
    _sync_active_machine()


def _coerce(s: _Setting, raw: Any) -> Any | None:
    """Cast raw to the setting's declared type and run its validator. Returns
    None if the value is missing or invalid (caller decides what to do)."""
    if raw is None:
        return None
    try:
        if s.type is bool:
            v: Any = bool(raw)
        elif s.type is int:
            v = int(raw)
        elif s.type is float:
            v = float(raw)
        else:
            v = raw  # str
    except (TypeError, ValueError):
        log.warning("config: invalid %s in %s", s.name, CONFIG_PATH)
        return None
    if s.validate is not None and not s.validate(v):
        return None
    return v


def _set(s: _Setting, value: Any) -> None:
    globals()[s.name.upper()] = value


# Initialize module-level attributes from defaults so static imports see
# valid values before _load_from_disk runs.
for _s in _SETTINGS:
    _set(_s, _s.default)


def _load_from_disk() -> None:
    global API_KEY
    data: dict = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
        except Exception:
            log.exception("config: could not parse %s; using defaults", CONFIG_PATH)
            data = {}

    for s in _SETTINGS:
        if s.name not in data:
            continue
        raw = data[s.name]
        if raw is None:
            # Honour explicit null only for settings whose default is None
            # (currently just display_unit).
            if s.default is None:
                _set(s, None)
            continue
        v = _coerce(s, raw)
        if v is not None:
            _set(s, v)

    _load_machines(data)

    api = data.get("api_key")
    if isinstance(api, str) and api.strip():
        API_KEY = api.strip()
    else:
        API_KEY = secrets.token_urlsafe(24)
        _save_to_disk()


def _save_to_disk() -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(snapshot(), indent=2) + "\n")
    except Exception:
        log.exception("config: failed to save %s", CONFIG_PATH)


def snapshot() -> dict:
    """What gets persisted to config.json. The MACHINE_* mirrors are left out
    on purpose — the active profile is where that lives, and writing both
    would make config.json disagree with itself the moment one was edited by
    hand. main._settings_payload adds them back for the UI."""
    out: dict = {"api_key": API_KEY}
    for s in _SETTINGS:
        out[s.name] = globals()[s.name.upper()]
    out["machines"] = [dict(m) for m in MACHINES]
    out["active_machine_id"] = ACTIVE_MACHINE_ID
    return out


def update(**kwargs) -> None:
    for s in _SETTINGS:
        if s.name not in kwargs:
            continue
        v = _coerce(s, kwargs[s.name])
        if v is not None:
            _set(s, v)
    if "machines" in kwargs or "active_machine_id" in kwargs:
        _update_machines(kwargs.get("machines"), kwargs.get("active_machine_id"))
    _save_to_disk()


_load_from_disk()
