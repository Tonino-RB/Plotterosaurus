"""app.camera's mapping from the friendly camera_flicker_mode setting to
MediaMTX's raw rpiCameraFlickerPeriod microsecond value.
"""
from app import camera


def test_flicker_period_mapping():
    assert camera._FLICKER_PERIOD_US == {"off": 0, "50hz": 10_000, "60hz": 8_333}
