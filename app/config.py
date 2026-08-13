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
    _Setting("pause_after_job_default", bool, True),
    _Setting("delete_on_complete_default", bool, False),
    _Setting("speed_pendown_default", int, 25, lambda v: 1 <= v <= 110),
    _Setting("speed_penup_default", int, 75, lambda v: 1 <= v <= 110),
    _Setting("acceleration_default", int, 75, lambda v: 1 <= v <= 100),
    _Setting("pen_pos_up_default", int, 60, lambda v: 29 <= v <= 85),
    _Setting("pen_pos_down_default", int, 30, lambda v: 29 <= v <= 85),
    # Default XY origin offset for newly-created jobs, captured by jogging the
    # pen while idle and clicking "Set origin here" (see plot_worker.manual_jog
    # / set_manual_origin) — distinct from a job's own transform_offset_x/y_mm,
    # which this only seeds at job-creation time.
    _Setting("origin_offset_x_mm_default", float, 0.0, lambda v: -2000 <= v <= 2000),
    _Setting("origin_offset_y_mm_default", float, 0.0, lambda v: -2000 <= v <= 2000),
    _Setting("optimize_svg_default", bool, True),
    _Setting("optimize_svg_tolerance_default_mm", float, 0.10,
             lambda v: 0.01 <= v <= 10.0),
    _Setting("optimize_svg_linemerge_default", bool, True),
    _Setting("optimize_svg_linesimplify_default", bool, True),
    _Setting("optimize_svg_linesort_default", bool, True),
    _Setting("optimize_svg_reloop_default", bool, True),
    _Setting("optimize_svg_min_length_default", bool, False),
    _Setting("optimize_svg_min_length_mm_default", float, 1.0,
             lambda v: 0.01 <= v <= 100.0),
    _Setting("display_unit", str, None,
             lambda v: v in ("mm", "cm", "in")),
    # Last update the user chose to skip. The update banner stays hidden while
    # this equals the latest remote version; a newer release re-shows it.
    _Setting("skipped_version", str, None),
    # Custom machine bed-size profile, layered on top of plotter_model: the
    # driver still only knows models 1-8 for real travel-bounds/homing math,
    # these fields are UI/bounds-only (paper-fit + orientation auto-rotate).
    _Setting("machine_custom_enabled", bool, False),
    _Setting("machine_width_mm", float, 297.0, lambda v: v > 0),
    _Setting("machine_height_mm", float, 420.0, lambda v: v > 0),
    _Setting("machine_auto_rotate", str, "off",
             lambda v: v in ("off", "portrait", "landscape")),
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
    _Setting("camera_recording_mode_default", str, "realtime",
             lambda v: v in ("realtime", "timelapse", "sped_up")),
    _Setting("camera_timelapse_interval_s_default", float, 5.0, lambda v: v > 0),
    _Setting("camera_speed_multiplier_default", float, 4.0, lambda v: v > 1.0),
    _Setting("record_plot_default", bool, False),
]

# Static API key for /api/v1/* routes — kept outside the schema because it
# auto-generates when missing rather than falling back to a hardcoded default.
API_KEY: str = ""


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
    out: dict = {"api_key": API_KEY}
    for s in _SETTINGS:
        out[s.name] = globals()[s.name.upper()]
    return out


def update(**kwargs) -> None:
    for s in _SETTINGS:
        if s.name not in kwargs:
            continue
        v = _coerce(s, kwargs[s.name])
        if v is not None:
            _set(s, v)
    _save_to_disk()


_load_from_disk()
