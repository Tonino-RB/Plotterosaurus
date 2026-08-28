"""config.json survives an interrupted write.

The failure this guards against is quiet, not loud. A plotter is a machine
people switch off at the wall, and a config.json truncated mid-write doesn't
raise on the next boot — `_load_from_disk` catches the parse error, logs it,
and falls back to defaults. Every setting is gone, and API_KEY is regenerated,
so any client configured with the old key stops working too. The queue in
state.json has been written atomically for this reason all along; settings
were not.

CONFIG_PATH is monkeypatched in every test here. It normally points at the
repo's own config.json — the running plotter's real settings — and conftest's
sandbox covers state.json and uploads/ but not this one.
"""
import json

import pytest

from app import config


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    """Redirect config.json into a temp dir for one test."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    return path


def test_save_writes_readable_json(config_path):
    config._save_to_disk()
    data = json.loads(config_path.read_text())
    assert data["api_key"] == config.API_KEY
    assert "plotter_model" in data


def test_save_leaves_no_temp_file_behind(config_path, tmp_path):
    config._save_to_disk()
    assert list(tmp_path.iterdir()) == [config_path]


def test_a_failed_write_does_not_destroy_the_previous_settings(config_path):
    """The point of the rename: a write that dies partway leaves the file that
    was already there untouched, rather than a truncated one in its place.

    The failure is injected through the filesystem — a directory sitting where
    the tmp file wants to be — rather than by patching pathlib, which is
    global: background queue workers are alive during a full-suite run and
    would inherit a broken write_text for the length of the test.
    """
    config._save_to_disk()
    good = json.loads(config_path.read_text())
    settled = good["plotter_model"]

    config_path.with_suffix(".json.tmp").mkdir()
    # Change something first, so a write that *did* get through would be
    # visible. Without this the file is byte-identical either way and the
    # assertion below passes whether or not the failure was ever injected.
    config.PLOTTER_MODEL = settled + 1
    try:
        config._save_to_disk()  # swallowed and logged, not raised
        assert json.loads(config_path.read_text())["plotter_model"] == settled
    finally:
        config.PLOTTER_MODEL = settled


def test_a_failed_rename_cleans_up_its_temp_file(tmp_path, monkeypatch):
    """Same injection at the other end: the tmp file is written, the rename
    onto it fails, and the scratch file must not be left behind."""
    # A directory at CONFIG_PATH: writing the tmp file succeeds, os.replace
    # onto a directory does not.
    target = tmp_path / "config.json"
    target.mkdir()
    monkeypatch.setattr(config, "CONFIG_PATH", target)

    config._save_to_disk()

    # Still a directory: proof the rename really did fail rather than the test
    # quietly exercising a successful save.
    assert target.is_dir()
    assert not (tmp_path / "config.json.tmp").exists()


def test_concurrent_saves_all_land(config_path):
    """Two request threads saving at once must not leave one of them renaming
    a tmp file the other already moved away."""
    import threading

    errors: list[BaseException] = []

    def save():
        try:
            for _ in range(40):
                config._save_to_disk()
        except BaseException as e:  # pragma: no cover - only on a regression
            errors.append(e)

    threads = [threading.Thread(target=save) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert json.loads(config_path.read_text())
