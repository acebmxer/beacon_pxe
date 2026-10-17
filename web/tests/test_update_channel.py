"""Beta update switch: which tag updates target, and what compose is told."""
import subprocess

import pytest

import app.config as app_config


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


def test_beta_build_is_labelled_with_its_number_and_not_a_local_build(updates, monkeypatch):
    monkeypatch.setattr(app_config, "BEACON_VERSION", "0.7.2-beta.1")
    monkeypatch.setattr(app_config, "BEACON_COMMIT", "abc1234def")
    assert updates.version_label() == "v0.7.2-beta.1"
    assert updates.is_dev_build() is False
