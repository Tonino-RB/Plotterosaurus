"""User-defined pen-height envelope (``pen_pos_min`` / ``pen_pos_max``).

The 29–85 range used to be hard-coded in the config validators, the job-field
clamp, and the ``/queue/pen-height`` request models. It is now one setting that
every pen-height path clamps against. These tests pin:

  - a settings PATCH with ``min >= max`` is refused, not silently corrected;
  - the pen-up/down *defaults* are pulled inside the envelope on save;
  - ``_clamp_job_fields`` clamps a job's ``pen_pos_up`` / ``pen_pos_down`` to the
    configured envelope (and still drops an emptied box rather than nulling it);
  - the ``/queue/pen-height`` route clamps before it reaches the worker;
  - the 29/85 defaults reproduce the old behaviour exactly.

``config.CONFIG_PATH`` is monkeypatched because conftest does not sandbox
config.json and ``patch_settings`` calls ``config.update()`` (see the note in
test_saved_pen_colors.py).
"""
import pytest

from app import config, main

TestClient = pytest.importorskip(
    "starlette.testclient", reason="httpx not installed"
).TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config, "PEN_POS_MIN", 29)
    monkeypatch.setattr(config, "PEN_POS_MAX", 85)
    monkeypatch.setattr(config, "PEN_POS_UP_DEFAULT", 60)
    monkeypatch.setattr(config, "PEN_POS_DOWN_DEFAULT", 30)
    return TestClient(main.app)


# --- settings PATCH -----------------------------------------------------------

def test_min_not_below_max_is_rejected(client):
    r = client.patch("/settings", json={"pen_pos_min": 80, "pen_pos_max": 70})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "pen_pos_bounds_invalid"


def test_equal_bounds_are_rejected(client):
    r = client.patch("/settings", json={"pen_pos_min": 50, "pen_pos_max": 50})
    assert r.status_code == 400


def test_a_valid_pair_persists(client):
    r = client.patch("/settings", json={"pen_pos_min": 35, "pen_pos_max": 70})
    assert r.status_code == 200
    snap = client.get("/settings").json()
    assert (snap["pen_pos_min"], snap["pen_pos_max"]) == (35, 70)


def test_defaults_are_pulled_inside_a_narrowed_envelope(client):
    # pen_pos_up_default is 60; narrowing the ceiling to 50 must drag it down
    # rather than leave a stored default the sliders can't reach.
    r = client.patch("/settings", json={"pen_pos_max": 50})
    assert r.status_code == 200
    snap = r.json()
    assert snap["pen_pos_max"] == 50
    assert snap["pen_pos_up_default"] == 50
    assert snap["pen_pos_down_default"] == 30      # already inside, untouched


def test_an_incoming_default_is_clamped_to_the_incoming_envelope(client):
    r = client.patch("/settings", json={
        "pen_pos_min": 40, "pen_pos_max": 70, "pen_pos_up_default": 90,
    })
    assert r.status_code == 200
    assert r.json()["pen_pos_up_default"] == 70


def test_a_patch_that_touches_neither_bound_is_left_alone(client):
    # Regression guard: _apply_pen_pos_bounds must no-op unless a pen-pos key is
    # in the payload, or an unrelated settings save would start rewriting the
    # pen defaults.
    r = client.patch("/settings", json={"speed_pendown_default": 40})
    assert r.status_code == 200
    assert r.json()["pen_pos_up_default"] == 60


# --- job-field clamp --------------------------------------------------------

def test_clamp_job_fields_uses_the_configured_envelope(monkeypatch):
    monkeypatch.setattr(config, "PEN_POS_MIN", 40)
    monkeypatch.setattr(config, "PEN_POS_MAX", 70)
    d = {"pen_pos_up": 90, "pen_pos_down": 20}
    main._clamp_job_fields(d)
    assert d == {"pen_pos_up": 70, "pen_pos_down": 40}


def test_clamp_job_fields_still_drops_an_emptied_box(monkeypatch):
    monkeypatch.setattr(config, "PEN_POS_MIN", 40)
    monkeypatch.setattr(config, "PEN_POS_MAX", 70)
    d = {"pen_pos_up": None, "pen_pos_down": 55}
    main._clamp_job_fields(d)
    assert d == {"pen_pos_down": 55}


def test_default_envelope_matches_the_old_hardcoded_clamp(monkeypatch):
    monkeypatch.setattr(config, "PEN_POS_MIN", 29)
    monkeypatch.setattr(config, "PEN_POS_MAX", 85)
    d = {"pen_pos_up": 200, "pen_pos_down": 0}
    main._clamp_job_fields(d)
    assert d == {"pen_pos_up": 85, "pen_pos_down": 29}


# --- live pen-height route ---------------------------------------------------

def test_queue_pen_height_clamps_before_the_worker(monkeypatch):
    seen = {}
    monkeypatch.setattr(config, "PEN_POS_MIN", 40)
    monkeypatch.setattr(config, "PEN_POS_MAX", 70)
    monkeypatch.setattr(main.plot_worker, "set_live_pen_heights",
                        lambda up, down, test: seen.update(up=up, down=down, test=test))
    client = TestClient(main.app)
    r = client.post("/queue/pen-height",
                    json={"pen_pos_up": 95, "pen_pos_down": 10, "test": "up"})
    assert r.status_code == 200
    assert seen == {"up": 70, "down": 40, "test": "up"}


def test_queue_live_settings_clamps_pen_heights(monkeypatch):
    seen = {}
    monkeypatch.setattr(config, "PEN_POS_MIN", 40)
    monkeypatch.setattr(config, "PEN_POS_MAX", 70)
    monkeypatch.setattr(main.plot_worker, "set_live_plot_settings",
                        lambda **kw: seen.update(kw))
    client = TestClient(main.app)
    r = client.post("/queue/live-settings", json={"pen_pos_up": 95, "speed_pendown": 30})
    assert r.status_code == 200
    assert seen["pen_pos_up"] == 70
    assert seen["pen_pos_down"] is None
    assert seen["speed_pendown"] == 30
