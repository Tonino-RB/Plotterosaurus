"""app.config's camera_flicker_mode validator (see app/camera.py's
_FLICKER_PERIOD_US for the raw-value mapping this setting feeds).
"""
from app import config


def _setting(name):
    return next(s for s in config._SETTINGS if s.name == name)


def test_flicker_mode_defaults_to_off():
    assert _setting("camera_flicker_mode").default == "off"


def test_flicker_mode_accepts_known_values():
    v = _setting("camera_flicker_mode").validate
    assert v("off") and v("50hz") and v("60hz")


def test_flicker_mode_rejects_garbage():
    v = _setting("camera_flicker_mode").validate
    assert not v("100hz") and not v("") and not v(None)
