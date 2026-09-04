"""AxiDraw ⇆ physical-unit conversion for the six rate knobs.

Plotterosaurus stores every speed / acceleration / pen-rate knob in the
pyaxidraw factor the driver wants: a 1–110 %% speed, a 1–100 %% acceleration
factor, a 1–100 %% servo-sweep rate. ``config.UNITS_MODE == "universal"`` is a
pure display/entry transform — the browser and the ``/settings`` payload show
these same stored values in mm/s, mm/s² and ms and convert back before
anything downstream sees them. ``plot_worker`` and the driver never change.

The anchor constants come straight from ``axidrawinternal.axidraw_conf`` and
are model-independent (they are ``params``, not the per-model ``x_travel_*``),
so a universal-mode value drives any EBB/AxiDraw board identically.

Two knobs per kind, but only three conversions:

    speed_pendown / speed_penup       1–110 %%  →  mm/s   (linear, ×2.008)
    acceleration                      1–100 %%  →  mm/s²  (linear, ×10.16)
    acceleration_penup                1–100 %%  →  mm/s²  (linear, ×15.24)
    pen_rate_lower / pen_rate_raise    1–100 %%  →  ms     (reciprocal, 20000/x)

``cornering`` has no physical unit, and ``pen_pos_*`` need pen-arm geometry we
don't have, so those stay as-is; ``pen_delay_*`` are already ms. None of them
appear here.
"""
from __future__ import annotations

from dataclasses import dataclass

_MM_PER_IN = 25.4
# High-resolution XY speed cap, in/s (axidraw_conf.speed_lim_xy_hr).
# Plotterosaurus never sets ad.options.resolution, so the driver stays in its
# default high-res mode and this is the only cap in play. speed_pendown /
# speed_penup are a percent of it, scaled /110 (axidraw.py).
_SPEED_LIM_XY_HR = 8.6979
# Pen-down / pen-up acceleration bases, in/s² (axidraw_conf.accel_rate /
# accel_rate_pu). Effective rate is base × accel_percent / 100 (motion.py).
_ACCEL_RATE = 40.0
_ACCEL_RATE_PU = 60.0
# ms for the servo control signal to sweep its full range at rate 100 %%
# (axidraw_conf.servo_sweep_time); at rate r the full lift takes 20000 / r ms.
_SERVO_SWEEP_MS = 200.0

_SPEED_FACTOR = _SPEED_LIM_XY_HR * _MM_PER_IN / 110.0     # ≈ 2.008424
_ACCEL_FACTOR = _ACCEL_RATE * _MM_PER_IN / 100.0          # 10.16
_ACCEL_PU_FACTOR = _ACCEL_RATE_PU * _MM_PER_IN / 100.0    # 15.24
_PEN_RATE_C = _SERVO_SWEEP_MS * 100.0                     # 20000.0

MODES = ("axidraw", "universal")


@dataclass(frozen=True)
class _Spec:
    ax_lo: int
    ax_hi: int
    unit: str          # universal-mode unit label
    kind: str          # "linear" (phys = stored × factor) or "reciprocal" (phys = factor / stored)
    factor: float
    uni_lo: int        # universal-mode slider range, derived from the AxiDraw endpoints
    uni_hi: int
    uni_step: int

    def to_universal(self, stored: float) -> int:
        v = self.factor / stored if self.kind == "reciprocal" else stored * self.factor
        return round(v)

    def to_axidraw(self, universal: float) -> int:
        v = self.factor / universal if self.kind == "reciprocal" else universal / self.factor
        return max(self.ax_lo, min(self.ax_hi, round(v)))


# Universal ranges are the AxiDraw endpoints run through the conversion and
# rounded to something a slider can land on:
#   speed  1–110 %%  → 2.0 – 220.9 mm/s
#   accel  1–100 %%  → 10.2 – 1016 mm/s²  (pen-down) / 15.2 – 1524 (pen-up)
#   rate 100–10 %%   → 200 – 2000 ms  (rates below 10 %% — a 2 s+ lift — are
#                                       unreachable in universal mode)
SPECS: dict[str, _Spec] = {
    "speed_pendown":      _Spec(1, 110, "mm/s",  "linear",     _SPEED_FACTOR,    2,  221, 1),
    "speed_penup":        _Spec(1, 110, "mm/s",  "linear",     _SPEED_FACTOR,    2,  221, 1),
    "acceleration":       _Spec(1, 100, "mm/s²", "linear",     _ACCEL_FACTOR,   10, 1016, 2),
    "acceleration_penup": _Spec(1, 100, "mm/s²", "linear",     _ACCEL_PU_FACTOR, 15, 1524, 2),
    "pen_rate_lower":     _Spec(1, 100, "ms",    "reciprocal", _PEN_RATE_C,    200, 2000, 1),
    "pen_rate_raise":     _Spec(1, 100, "ms",    "reciprocal", _PEN_RATE_C,    200, 2000, 1),
}


def to_axidraw(key: str, value: float) -> int:
    """A universal-unit value → the stored 1–110 / 1–100 AxiDraw factor, clamped."""
    return SPECS[key].to_axidraw(value)


def to_universal(key: str, value: float) -> int:
    """A stored AxiDraw factor → its universal-unit display value."""
    return SPECS[key].to_universal(value)


def unit_specs(mode: str) -> dict:
    """Per-knob ``{unit, min, max, step, kind, factor}`` for the ``/settings``
    payload. The browser converts generically off ``kind``/``factor``; in
    ``axidraw`` mode every knob is the identity transform (unit ``"%"``,
    factor ``1``) so the frontend runs one code path in both modes."""
    out: dict = {}
    for key, s in SPECS.items():
        if mode == "universal":
            out[key] = {"unit": s.unit, "min": s.uni_lo, "max": s.uni_hi,
                        "step": s.uni_step, "kind": s.kind, "factor": s.factor}
        else:
            out[key] = {"unit": "%", "min": s.ax_lo, "max": s.ax_hi,
                        "step": 1, "kind": "linear", "factor": 1}
    return out
