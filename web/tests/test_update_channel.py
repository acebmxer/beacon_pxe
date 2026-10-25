"""Beta update switch: which tag updates target, and what compose is told."""
import subprocess

import pytest

import app.config as app_config
from app.store import get_setting


# app.services.updates binds SessionLocal when first imported, and the route
# fixtures swap SessionLocal in before importing the app. Importing it at module
# level here would bind the real one first and break every route test, so it is
# imported per test instead.
@pytest.fixture(autouse=True)
def updates(monkeypatch):
    monkeypatch.setattr(app_config, "BEACON_TAG", "stable")
    from app.services import updates
    return updates


def test_target_is_env_channel_by_default(db, updates):
    assert updates.target_channel(db) == "stable"
    assert updates.image_ref("stable").endswith("/beacon-web:stable")


def test_beta_switch_overrides_env_channel(db, updates):
    updates.set_beta(db, True)
    assert updates.beta_enabled(db)
    assert updates.target_channel(db) == "beta"
    updates.set_beta(db, False)
    assert updates.target_channel(db) == "stable"


def test_run_update_passes_channel_to_compose(db, updates, monkeypatch):
    updates.set_beta(db, True)
    monkeypatch.setattr(app_config, "COMPOSE_PROJECT_DIR", "/srv/beacon")
    monkeypatch.setattr(updates, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(updates, "_docker", lambda args: "same")
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(updates.subprocess, "run", fake_run)
    monkeypatch.setattr(updates, "_UPDATER_NAME", "beacon_updater")
    updates.run_update()

    pull = next(c for c in calls if c[0][-1] == "pull")
    assert pull[1]["env"]["BEACON_TAG"] == "beta"
    launch = next(c for c in calls if c[0][:3] == ["docker", "run", "--detach"])
    assert "BEACON_TAG=beta" in launch[0]
    assert launch[0][launch[0].index("-e") + 2].endswith("/beacon-web:beta")


def test_beta_endpoint_saves_and_status_reports_it(client, updates, monkeypatch):
    from tests.test_routes import _login

    monkeypatch.setattr(updates, "check_for_updates", lambda: False)
    monkeypatch.setattr(updates, "_running_channel", "stable")
    anon = client.post("/api/update/beta", json={"enabled": True}, follow_redirects=False)
    assert anon.status_code in (302, 303, 307) and anon.headers["location"] == "/login"
    _login(client)
    assert client.get("/api/update/status").json()["beta"] is False
    r = client.post("/api/update/beta", json={"enabled": True})
    assert r.status_code == 200 and r.json()["beta"] is True
    status = client.get("/api/update/status").json()
    assert status["beta"] is True
    assert status["target_channel"] == "beta"
    assert status["channel"] == "stable"


# Which build is newer comes from the GitHub releases, ranked the way
# linux_util_desktop's updater ranks them: a release above every beta of its
# version. The digests are the real GHCR ones of v0.8.0 (:stable) and
# v0.8.0-beta.6 (:beta), the pair that offered stable users the older beta.
_STABLE_080 = "sha256:581c5fb3b4b8a73a5f9f1ef38cbc334e327c0c8d5c252ca118f64f24811d0ede"
_BETA_080_6 = "sha256:cc5d32078be42b6331eb190db451dbdab13bb9c1379e01bb924f3899f1a5900a"


def _release(tag, draft=False):
    return {"tag_name": tag, "draft": draft, "prerelease": "-" in tag}


@pytest.fixture
def github(db, updates, monkeypatch):
    """Serve fake GitHub releases (newest first, as GitHub lists them) and the
    two GHCR tags; returns a setter for the running build."""
    import io
    import json

    monkeypatch.setattr(updates, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    state: dict = {"releases": [_release("v0.8.0"), _release("v0.8.0-beta.6"),
                                _release("v0.8.0-beta.5")]}

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        assert url.startswith("https://api.github.com/repos/acebmxer/beacon_pxe/releases")
        stable = [r for r in state["releases"] if not r["prerelease"] and not r["draft"]]
        body = stable[0] if url.endswith("/latest") else state["releases"]
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(updates, "_ghcr_latest_digest",
                        lambda ch: {"stable": _STABLE_080, "beta": _BETA_080_6}.get(ch))

    def running(version, digest):
        monkeypatch.setattr(app_config, "BEACON_VERSION", version)
        monkeypatch.setattr(updates, "_deployed_digests", lambda: {digest})

    state["running"] = running
    return state


def test_releases_rank_stable_above_their_betas(updates):
    r = updates._parse_release
    assert r("v0.8.0") > r("v0.8.0-beta.6") > r("v0.8.0-beta.5") > r("v0.7.2")
    assert r("v0.8.1-beta.1") > r("v0.8.0")
    assert r("0.8.0-beta.12") > r("0.8.0-beta.9")
    assert r("v0.8.0-rc1") < r("v0.8.0-beta.1")
    assert r("main") is None and r("dev") is None and r("v1.2") is None


def test_stable_with_betas_on_is_not_offered_the_older_beta(db, updates, github):
    github["running"]("0.8.0", _STABLE_080)
    updates.set_beta(db, True)
    assert updates.check_for_updates() is False
    assert updates.target_channel(db) == "stable"


def test_beta_is_offered_the_newer_release_and_moves_to_stable(db, updates, github):
    github["running"]("0.8.0-beta.6", _BETA_080_6)
    updates.set_beta(db, True)
    assert updates.check_for_updates() is True
    assert get_setting(db, "update_available_version") == "0.8.0"
    assert updates.target_channel(db) == "stable"


def test_newer_beta_is_offered_over_the_release(db, updates, github):
    github["releases"].insert(0, _release("v0.8.1-beta.1"))
    github["releases"].insert(0, _release("v0.8.1-beta.2", draft=True))
    github["running"]("0.8.0", _STABLE_080)
    updates.set_beta(db, True)
    assert updates.check_for_updates() is True
    assert get_setting(db, "update_available_version") == "0.8.1-beta.1"
    assert updates.target_channel(db) == "beta"


def test_betas_off_on_a_newer_beta_is_not_offered_the_older_release(db, updates, github):
    github["running"]("0.8.1-beta.1", _BETA_080_6)
    assert updates.check_for_updates() is False
    assert updates.target_channel(db) == "stable"


def test_stable_is_offered_a_newer_release(db, updates, github):
    github["running"]("0.7.2", "sha256:old")
    assert updates.check_for_updates() is True
    assert get_setting(db, "update_available_version") == "0.8.0"


def test_stale_offer_retracted_once_that_version_runs(db, updates, github):
    github["running"]("0.8.0-beta.6", _BETA_080_6)
    updates.set_beta(db, True)
    assert updates.check_for_updates() is True
    # Updated by hand on the host: this process now runs v0.8.0.
    github["running"]("0.8.0", _STABLE_080)
    updates.refresh_available(db)
    assert get_setting(db, "update_available") == "0"


def test_beta_build_is_labelled_with_its_number_and_not_a_local_build(updates, monkeypatch):
    monkeypatch.setattr(app_config, "BEACON_VERSION", "0.7.2-beta.1")
    monkeypatch.setattr(app_config, "BEACON_COMMIT", "abc1234def")
    assert updates.version_label() == "v0.7.2-beta.1"
    assert updates.is_dev_build() is False
