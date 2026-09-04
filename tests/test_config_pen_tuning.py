"""The corner-speed / pen-timing settings added to fight ink blobs at sharp
corners (cornering) and starting dots (pen delay / pen rate), plus a pen-up
acceleration split out from the shared `acceleration` factor.

Table assertions only — see the note in test_config_atomic_save.py about why a
config test never calls config.update().
"""
from app import config


def _setting(name):
    return next(s for s in config._SETTINGS if s.name == name)


def test_defaults_match_stock_pyaxidraw():
    # A fresh install must behave exactly as before: these defaults are the
    # pyaxidraw library defaults (axidraw_conf.py), and acceleration_penup
    # mirrors acceleration_default so the pen-up ramp is unchanged.
    assert _setting("cornering_default").default == 10
    assert _setting("pen_rate_lower_default").default == 50
    assert _setting("pen_rate_raise_default").default == 75
    assert _setting("pen_delay_down_default").default == 0
    assert _setting("pen_delay_up_default").default == 0
    assert (_setting("acceleration_penup_default").default
            == _setting("acceleration_default").default)


def test_ranges():
    for name in ("cornering_default", "pen_rate_lower_default",
                 "pen_rate_raise_default", "acceleration_penup_default"):
        v = _setting(name).validate
        assert v(1) and v(100)
        assert not v(0) and not v(101)
    for name in ("pen_delay_down_default", "pen_delay_up_default"):
        v = _setting(name).validate
        assert v(-500) and v(0) and v(500)
        assert not v(-501) and not v(501)


def test_all_declared_int():
    for name in ("cornering_default", "acceleration_penup_default",
                 "pen_rate_lower_default", "pen_rate_raise_default",
                 "pen_delay_down_default", "pen_delay_up_default"):
        assert _setting(name).type is int


def test_pen_pos_bounds_defaults_preserve_the_old_hardcoded_range():
    # 29–85 was hard-coded in six places before it became a setting; the
    # defaults keep a fresh install identical.
    assert _setting("pen_pos_min").default == 29
    assert _setting("pen_pos_max").default == 85
    assert _setting("pen_pos_min").type is int
    assert _setting("pen_pos_max").type is int


def test_pen_pos_bounds_validators_span_the_servo_range():
    for name in ("pen_pos_min", "pen_pos_max"):
        v = _setting(name).validate
        assert v(0) and v(100)
        assert not v(-1) and not v(101)


def test_pen_pos_default_validators_are_widened_to_the_servo_range():
    # The effective clamp is now config.PEN_POS_MIN/MAX (enforced in main), so
    # the per-setting validator only guards the raw servo range.
    for name in ("pen_pos_up_default", "pen_pos_down_default"):
        v = _setting(name).validate
        assert v(0) and v(100)
        assert not v(-1) and not v(101)
