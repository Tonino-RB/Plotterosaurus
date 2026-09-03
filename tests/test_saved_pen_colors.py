"""The ``saved_pen_colors`` setting behind the layer-colour popover's palette.

Stored in config.json as a JSON-array string (config._SETTINGS is scalar-only);
main normalises a client array on the way in and hands back a real array on the
way out. CONFIG_PATH is monkeypatched here — conftest does not sandbox
config.json, and config.update() writes it.
"""
import json

import pytest

from app import config, main

TestClient = pytest.importorskip(
    "starlette.testclient", reason="httpx not installed"
).TestClient


@pytest.fixture
def sandboxed_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config, "SAVED_PEN_COLORS", "[]")
    yield


@pytest.fixture
def client(sandboxed_config):
    return TestClient(main.app)


def test_validator_accepts_a_short_hex_list():
    assert config._valid_saved_pen_colors('["#ff0000", "#00ff00"]')
    assert config._valid_saved_pen_colors("[]")


@pytest.mark.parametrize("bad", [
    '["red"]',                       # not #rrggbb
    '["#fff"]',                      # short hex
    "[1, 2]",                        # not strings
    "not json",
    '{"a": 1}',                      # not a list
    json.dumps(["#000000"] * 25),    # over the cap
])
def test_validator_rejects(bad):
    assert not config._valid_saved_pen_colors(bad)


def test_patch_normalises_and_get_returns_an_array(client):
    r = client.patch("/settings", json={
        "saved_pen_colors": ["#FF0000", "#ff0000", "nope", "#00FF00"],
    })
    assert r.status_code == 200
    assert r.json()["saved_pen_colors"] == ["#ff0000", "#00ff00"]

    got = client.get("/settings").json()["saved_pen_colors"]
    assert got == ["#ff0000", "#00ff00"]
    # Persisted as text.
    assert isinstance(config.SAVED_PEN_COLORS, str)
    assert json.loads(config.SAVED_PEN_COLORS) == ["#ff0000", "#00ff00"]


def test_patch_caps_at_24(client):
    r = client.patch("/settings", json={
        "saved_pen_colors": [f"#{i:02x}0000" for i in range(40)],
    })
    assert len(r.json()["saved_pen_colors"]) == 24
