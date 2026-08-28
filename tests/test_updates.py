"""Version comparison for the self-update check.

The reason this file exists: `_parse` used to be `int()` over `"."`-split
parts, which raised on any pre-release suffix and returned None. An
unparsable *local* version sorts below everything (semver_gt's second guard),
so while VERSION said `0.3.0-beta` every remote release — including older
ones — announced itself as an available update. The banner would have offered
a downgrade, and /update/apply would have run a `git reset --hard` onto it.

These are ordering rules, not a characterization: they should hold for any
version string this project ever ships.
"""
import pytest

from app import updates


@pytest.mark.parametrize("newer,older", [
    # Plain releases.
    ("1.0.0", "0.9.9"),
    ("1.1.0", "1.0.9"),
    ("0.3.0", "0.2.9"),
    # Numeric, not lexical: "10" > "4" even though "1" < "4" as text.
    ("1.1.10", "1.1.4"),
    ("1.10.0", "1.9.0"),
    # A release is newer than any of its own pre-releases.
    ("1.0.0", "1.0.0-rc1"),
    ("1.0.0", "1.0.0-beta"),
    ("0.3.0", "0.3.0-beta"),
    # Pre-releases of the same release, against each other.
    ("1.0.0-rc2", "1.0.0-rc1"),
    # A pre-release of a later release still beats an earlier full release.
    ("1.1.0-rc1", "1.0.0"),
    # The specific pairing that was broken: a real release against the
    # pre-release VERSION this project shipped while the bug was live.
    ("1.0.0", "0.3.0-beta"),
])
def test_strictly_newer(newer, older):
    assert updates.semver_gt(newer, older) is True
    assert updates.semver_gt(older, newer) is False


@pytest.mark.parametrize("version", ["1.0.0", "0.3.0-beta", "2.4.11", "1.0.0-rc1"])
def test_a_version_is_not_newer_than_itself(version):
    assert updates.semver_gt(version, version) is False


def test_trailing_zeros_are_the_same_release():
    """"1.0" and "1.0.0" name one release; neither is an update to the other."""
    assert updates.semver_gt("1.0", "1.0.0") is False
    assert updates.semver_gt("1.0.0", "1.0") is False
    assert updates.semver_gt("1.0.1", "1.0") is True


@pytest.mark.parametrize("junk", [None, "", "   ", "banana", "v1.0.0", "1.0.0.beta"])
def test_unreadable_remote_never_offers_an_update(junk):
    """We can't claim a version we can't read is newer than what's installed."""
    assert updates.semver_gt(junk, "1.0.0") is False


@pytest.mark.parametrize("junk", [None, "", "banana"])
def test_unreadable_local_does_offer_an_update(junk):
    """The other direction is deliberate: a local VERSION file that can't be
    read is damaged, and the update that overwrites it is the repair."""
    assert updates.semver_gt("1.0.0", junk) is True


def test_the_hard_disable_gate_is_gone():
    """A public release ships with update checking live, rather than compiled
    out. What gates it now is whether the install can actually apply one —
    see is_enabled."""
    assert not hasattr(updates, "_UPDATES_DISABLED")


# Whether an install can update itself ------------------------------------
#
# The two halves fail apart: checking for a new version is a network call any
# install can make, applying one needs the root-owned wrapper that install.sh
# only puts there for ENABLE_SELF_UPDATE=1. Reporting an update on an install
# that has no wrapper lights the banner over a button that cannot work — sudo
# exits non-zero into DEVNULL, so nothing surfaces and the progress dialog
# waits forever on a log that is never written.

@pytest.fixture
def wrapper_installed(tmp_path, monkeypatch):
    wrapper = tmp_path / "plotterosaurus-update"
    wrapper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(updates, "WRAPPER_PATH", str(wrapper))
    return wrapper


@pytest.fixture
def wrapper_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "WRAPPER_PATH", str(tmp_path / "not-installed"))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Never let these reach GitHub; get_status would otherwise git-fetch."""
    monkeypatch.setattr(updates, "fetch_remote_version", lambda *a, **kw: "99.0.0")
    monkeypatch.setattr(updates, "_cache_at", 0.0)
    monkeypatch.setattr(updates, "_cache_latest", None)
    monkeypatch.setattr(updates, "_cache_error", False)


def test_enabled_when_the_wrapper_is_installed(wrapper_installed):
    assert updates.is_enabled() is True


def test_disabled_when_the_wrapper_is_absent(wrapper_absent):
    assert updates.is_enabled() is False


def test_status_offers_the_update_when_it_can_be_applied(wrapper_installed):
    status = updates.get_status(force=True)
    assert status["enabled"] is True
    assert status["latest"] == "99.0.0"
    assert status["update_available"] is True


def test_status_offers_nothing_when_it_cannot_be_applied(wrapper_absent):
    status = updates.get_status(force=True)
    assert status["enabled"] is False
    assert status["update_available"] is False
    assert status["latest"] is None
    # Not an error: a feature nobody opted into has not failed, and the UI
    # would otherwise show a "check failed" indicator for it.
    assert status["error"] is False
    assert status["current"] == updates.config.APP_VERSION


def test_a_disabled_install_does_not_call_github(wrapper_absent, monkeypatch):
    calls = []
    monkeypatch.setattr(updates, "fetch_remote_version",
                        lambda *a, **kw: calls.append(1) or "99.0.0")
    updates.get_status(force=True)
    assert calls == []
