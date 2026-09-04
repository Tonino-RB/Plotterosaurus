"""The AxiDraw ⇆ physical-unit transform behind ``units_mode == "universal"``.

Universal mode never changes a stored value — it re-labels the six rate knobs
for display/entry — so the contract these tests pin is: the conversion is a
faithful round-trip within rounding, it clamps back into the AxiDraw range, and
``axidraw`` mode is the identity transform (one frontend code path).
"""
import pytest

from app import config, rate_units


LINEAR = ("speed_pendown", "speed_penup", "acceleration", "acceleration_penup")
RECIP = ("pen_rate_lower", "pen_rate_raise")
ALL = LINEAR + RECIP


def test_config_setting_present_and_validated():
    s = next(s for s in config._SETTINGS if s.name == "units_mode")
    assert s.default == "axidraw"
    assert s.validate("axidraw") and s.validate("universal")
    assert not s.validate("mm") and not s.validate("")


@pytest.mark.parametrize("key", ALL)
def test_stock_defaults_map_to_expected_universal(key):
    # The stored defaults are the pyaxidraw library defaults; these are the
    # numbers a user sees on the slider the first time they flip to universal.
    expected = {
        "speed_pendown": 50,        # 25 %  → 25 × 2.0084
        "speed_penup": 151,         # 75 %  → 75 × 2.0084
        "acceleration": 762,        # 75 %  → 75 × 10.16
        "acceleration_penup": 1143,  # 75 %  → 75 × 15.24
        "pen_rate_lower": 400,      # 50 %  → 20000 / 50
        "pen_rate_raise": 267,      # 75 %  → 20000 / 75
    }[key]
    stored_default = {
        "speed_pendown": 25, "speed_penup": 75, "acceleration": 75,
        "acceleration_penup": 75, "pen_rate_lower": 50, "pen_rate_raise": 75,
    }[key]
    assert rate_units.to_universal(key, stored_default) == expected


@pytest.mark.parametrize("key", ALL)
def test_round_trip_is_stable_across_the_range(key):
    lo, hi = rate_units.SPECS[key].ax_lo, rate_units.SPECS[key].ax_hi
    for stored in range(lo, hi + 1):
        back = rate_units.to_axidraw(key, rate_units.to_universal(key, stored))
        # ±1 is the most a single rounding hop can cost; most values are exact.
        assert abs(back - stored) <= 1


@pytest.mark.parametrize("key", ALL)
def test_to_axidraw_clamps_into_range(key):
    spec = rate_units.SPECS[key]
    assert rate_units.to_axidraw(key, 10_000_000) in (spec.ax_lo, spec.ax_hi)
    assert rate_units.to_axidraw(key, 0.0001) in (spec.ax_lo, spec.ax_hi)
    assert spec.ax_lo <= rate_units.to_axidraw(key, -5) <= spec.ax_hi


@pytest.mark.parametrize("key", LINEAR)
def test_linear_slider_bounds_hit_the_axidraw_endpoints(key):
    spec = rate_units.SPECS[key]
    assert rate_units.to_axidraw(key, spec.uni_lo) == spec.ax_lo
    assert rate_units.to_axidraw(key, spec.uni_hi) == spec.ax_hi


@pytest.mark.parametrize("key", RECIP)
def test_reciprocal_slider_bounds(key):
    spec = rate_units.SPECS[key]
    # Smaller ms = faster = higher %%. The fast end reaches 100 %%; the slow end
    # deliberately stops at 10 %% (a 2 s lift) rather than the 1 %% floor.
    assert rate_units.to_axidraw(key, spec.uni_lo) == spec.ax_hi
    assert rate_units.to_axidraw(key, spec.uni_hi) == 10
    assert spec.ax_lo <= rate_units.to_axidraw(key, spec.uni_hi) <= spec.ax_hi


def test_axidraw_mode_unit_specs_are_the_identity_transform():
    specs = rate_units.unit_specs("axidraw")
    assert set(specs) == set(ALL)
    for key, s in specs.items():
        assert s == {"unit": "%", "min": rate_units.SPECS[key].ax_lo,
                     "max": rate_units.SPECS[key].ax_hi, "step": 1,
                     "kind": "linear", "factor": 1}


def test_universal_mode_unit_specs_carry_conversion():
    specs = rate_units.unit_specs("universal")
    assert specs["speed_pendown"]["unit"] == "mm/s"
    assert specs["acceleration"]["unit"] == "mm/s²"
    assert specs["pen_rate_lower"] == {
        "unit": "ms", "min": 200, "max": 2000, "step": 1,
        "kind": "reciprocal", "factor": 20000.0,
    }
    # A frontend applying phys = stored × factor must reproduce to_universal.
    f = specs["speed_pendown"]["factor"]
    assert round(25 * f) == rate_units.to_universal("speed_pendown", 25)
