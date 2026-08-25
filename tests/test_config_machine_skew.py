"""config._normalize_machine's handling of skew_deg: the one new field a
machine profile carries for the skew-angle setting. Exercises the pure
normalizer directly rather than config.update(), since config.CONFIG_PATH is
the repo's real config.json and is never sandboxed for tests (unlike
state.json — see conftest.py).
"""
from app import config


def _machine(**overrides):
    raw = {"name": "m", "width_mm": 100.0, "height_mm": 100.0}
    raw.update(overrides)
    return config._normalize_machine(raw, set())


def test_skew_defaults_to_zero_when_absent():
    assert _machine()["skew_deg"] == 0.0


def test_skew_passes_through_within_range():
    assert _machine(skew_deg=1.5)["skew_deg"] == 1.5
    assert _machine(skew_deg=-1.5)["skew_deg"] == -1.5


def test_skew_clamps_to_the_max():
    assert _machine(skew_deg=90)["skew_deg"] == config.MACHINE_SKEW_DEG_MAX
    assert _machine(skew_deg=-90)["skew_deg"] == -config.MACHINE_SKEW_DEG_MAX


def test_skew_ignores_garbage_rather_than_discarding_the_profile():
    assert _machine(skew_deg="not a number")["skew_deg"] == 0.0
    assert _machine(skew_deg=None)["skew_deg"] == 0.0
