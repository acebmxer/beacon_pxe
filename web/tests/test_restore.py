"""Tests for restoring the database from a backup.

The unit tests build throwaway SQLite files that look like backups (and like
things that aren't backups) and check what services.restore makes of them. The
route tests drive the upload -> preview -> apply flow through the app, with the
container restart stubbed out — the restart is the one step that can't run
inside a test.
"""
import sqlite3

import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from app.auth import hash_password
from app.models import Setting, User
from app.services import hostinfo, restore
import app.config as app_config
import app.db as app_db


# ---------------------------------------------------------------------------
# Building candidate backup files
# ---------------------------------------------------------------------------
def _make_backup(path, settings=None, users=None, images=None):
    """Write a minimal but structurally valid Beacon backup."""
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, "
                     "username TEXT, password_hash TEXT, role TEXT)")
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE images (id INTEGER PRIMARY KEY, name TEXT, "
                     "filename TEXT, status TEXT)")
        conn.execute("CREATE TABLE boot_events (id INTEGER PRIMARY KEY)")
        for name, role in (users or [("admin", "admin")]):
            conn.execute("INSERT INTO users (username, password_hash, role) "
                         "VALUES (?, ?, ?)", (name, "x", role))
        for key, value in (settings or {}).items():
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)",
                         (key, value))
        for name, filename, status in (images or []):
            conn.execute("INSERT INTO images (name, filename, status) "
                         "VALUES (?, ?, ?)", (name, filename, status))
    conn.close()
    return path


@pytest.fixture
def fake_host(monkeypatch):
    """Pin what the host looks like so decisions are deterministic."""
    monkeypatch.setattr(hostinfo, "local_ipv4s", lambda: {"10.0.0.5", "127.0.0.1"})
    monkeypatch.setattr(hostinfo, "interfaces", lambda: {"eno1", "docker0"})


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def test_rejects_non_sqlite_file(tmp_path):
    path = tmp_path / "not-a-db"
    path.write_bytes(b"PK\x03\x04 this is a zip file")
    with pytest.raises(restore.RestoreError, match="not a SQLite database"):
        restore.inspect_backup(path)


def test_rejects_empty_file(tmp_path):
    path = tmp_path / "empty.db"
    path.write_bytes(b"")
    with pytest.raises(restore.RestoreError, match="empty"):
        restore.inspect_backup(path)


def test_rejects_database_without_beacon_tables(tmp_path):
    path = tmp_path / "other.db"
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY)")
    conn.close()
    with pytest.raises(restore.RestoreError, match="does not look like a Beacon backup"):
        restore.inspect_backup(path)


def test_rejects_backup_with_no_admin(tmp_path):
    """The check that stops an admin locking themselves out permanently."""
    path = _make_backup(tmp_path / "b.db", users=[("bob", "user")])
    with pytest.raises(restore.RestoreError, match="no admin account"):
        restore.inspect_backup(path)


def test_rejects_truncated_download(tmp_path):
    """A half-transferred file opens fine and only fails on a real read."""
    path = _make_backup(tmp_path / "b.db", settings={"theme": "light"})
    data = path.read_bytes()
    path.write_bytes(data[:len(data) // 2])
    with pytest.raises(restore.RestoreError):
        restore.inspect_backup(path)


def test_reads_a_valid_backup(tmp_path):
    path = _make_backup(
        tmp_path / "b.db",
        settings={"theme": "light", "menu_title": "Lab PXE"},
        users=[("admin", "admin"), ("bob", "user")],
        images=[("Ubuntu", "ubuntu.iso", "ready")],
    )
    info = restore.inspect_backup(path)
    assert info["admins"] == ["admin"]
    assert info["usernames"] == ["admin", "bob"]
    assert info["settings"]["menu_title"] == "Lab PXE"
    assert info["images"] == [{"name": "Ubuntu", "filename": "ubuntu.iso",
                               "status": "ready"}]


# ---------------------------------------------------------------------------
# Host-identity decisions
# ---------------------------------------------------------------------------
def test_restores_host_settings_that_match_this_host(fake_host):
    row = restore._host_decision("server_ip", "10.0.0.9", "10.0.0.5")
    assert row["action"] == "restore"

    row = restore._host_decision("boot_interface", "eth0", "eno1")
    assert row["action"] == "restore"


def test_keeps_current_value_when_backup_names_another_machine(fake_host):
    """The Fedora -> Ubuntu NIC rename case: enp1s0 isn't on this host."""
    row = restore._host_decision("boot_interface", "eno1", "enp1s0")
    assert row["action"] == "keep"
    assert "enp1s0" in row["reason"]

    row = restore._host_decision("server_ip", "10.0.0.5", "192.168.1.20")
    assert row["action"] == "keep"


def test_keeps_current_value_when_backup_has_none(fake_host):
    """An unset value in the backup must not wipe a working one."""
    row = restore._host_decision("server_ip", "10.0.0.5", "")
    assert row["action"] == "keep"
    assert "no value" in row["reason"]


def test_unchanged_host_setting_is_not_flagged(fake_host):
    row = restore._host_decision("server_ip", "10.0.0.5", "10.0.0.5")
    assert row["action"] == "restore"
    assert row["reason"] == "unchanged"


def test_unverifiable_host_falls_back_to_trusting_the_backup(monkeypatch):
    """No host /proc means "can't check", which must not reject everything."""
    monkeypatch.setattr(hostinfo, "interfaces", lambda: set())
    row = restore._host_decision("boot_interface", "eth0", "enp1s0")
    assert row["action"] == "restore"
    assert row["unverified"] is True


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------
def test_preview_reports_differences_without_touching_anything(db, tmp_path, fake_host):
    db.add(Setting(key="menu_title", value="Old title"))
    db.add(Setting(key="server_ip", value="10.0.0.5"))
    db.add(Setting(key="boot_interface", value="eno1"))
    db.add(User(username="admin", password_hash="x", role="admin"))
    db.commit()

    path = _make_backup(
        tmp_path / "b.db",
        settings={"menu_title": "New title", "server_ip": "10.0.0.5",
                  "boot_interface": "enp1s0", "dhcp_range_start": "10.9.9.10"},
        users=[("admin", "admin"), ("newuser", "user")],
    )
    before = path.read_bytes()
    preview = restore.build_preview(db, path)

    assert path.read_bytes() == before, "preview must not modify the upload"

    titles = [r for r in preview["settings"] if r["key"] == "menu_title"]
    assert titles == [{"key": "menu_title", "current": "Old title",
                       "incoming": "New title", "action": "restore"}]

    host = {r["key"]: r for r in preview["host"]}
    assert host["server_ip"]["action"] == "restore"
    assert host["boot_interface"]["action"] == "keep"

    assert preview["users"]["added"] == ["newuser"]
    # A rejected host setting means the rest of the network block describes
    # somewhere else too, so it has to be called out.
    assert any("network settings" in w for w in preview["warnings"])


def test_preview_omits_deployment_bookkeeping(db, tmp_path, fake_host):
    """update_* describes the deployment restoring, not the one backed up."""
    db.add(User(username="admin", password_hash="x", role="admin"))
    db.commit()
    path = _make_backup(tmp_path / "b.db",
                        settings={"update_in_progress": "1",
                                  "update_known_digest": "sha256:old"})
    preview = restore.build_preview(db, path)
    assert not [r for r in preview["settings"] if r["key"].startswith("update_")]


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
def test_apply_swaps_the_file_and_keeps_rejected_host_settings(
        db, tmp_path, monkeypatch, fake_host):
    live = tmp_path / "pxe.db"
    _make_backup(live, settings={"boot_interface": "eno1", "theme": "dark"})
    monkeypatch.setattr(app_config, "DB_PATH", live)
    monkeypatch.setattr(restore, "PRE_RESTORE_PATH", tmp_path / "pxe.db.pre-restore")

    db.add(Setting(key="boot_interface", value="eno1"))
    db.add(User(username="admin", password_hash="x", role="admin"))
    db.commit()

    staged = _make_backup(
        tmp_path / "staged.db",
        settings={"boot_interface": "enp1s0", "theme": "light",
                  "update_in_progress": "1"},
    )
    result = restore.apply(db, staged)

    assert not staged.exists(), "staged file is moved into place, not copied"
    assert (tmp_path / "pxe.db.pre-restore").exists()
    assert result["kept"] == {"boot_interface": "eno1"}

    conn = sqlite3.connect(live)
    restored = dict(conn.execute("SELECT key, value FROM settings"))
    conn.close()

    # The backup's own preference survives; the value describing the other
    # machine does not; the update bookkeeping is gone entirely.
    assert restored["theme"] == "light"
    assert restored["boot_interface"] == "eno1"
    assert "update_in_progress" not in restored
    assert "kept because" in restored["restore_notice"]
    assert restored["restore_notice_level"] == "warn"


def test_apply_leaves_no_notice_warning_when_host_settings_match(
        db, tmp_path, monkeypatch, fake_host):
    live = tmp_path / "pxe.db"
    _make_backup(live)
    monkeypatch.setattr(app_config, "DB_PATH", live)
    monkeypatch.setattr(restore, "PRE_RESTORE_PATH", tmp_path / "pre.db")

    db.add(User(username="admin", password_hash="x", role="admin"))
    db.commit()

    staged = _make_backup(tmp_path / "staged.db",
                          settings={"boot_interface": "eno1",
                                    "server_ip": "10.0.0.5"})
    result = restore.apply(db, staged)
    assert result["kept"] == {}
    assert result["level"] == "ok"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def _login(client: TestClient):
    session = app_db.SessionLocal()
    try:
        existing = session.execute(
            select(User).where(User.username == "testadmin")
        ).scalar_one_or_none()
        if not existing:
            session.add(User(username="testadmin",
                             password_hash=hash_password("AdminPass123!"),
                             role="admin", session_epoch=0))
            session.merge(Setting(key="setup_complete", value="1"))
            session.commit()
    finally:
        session.close()
    client.post("/login", data={"username": "testadmin",
                                "password": "AdminPass123!"})


@pytest.fixture
def staged_in_tmp(tmp_path, monkeypatch):
    """Keep staged/pre-restore files out of the real data directory."""
    monkeypatch.setattr(restore, "STAGED_PATH", tmp_path / "restore-staged.db")
    monkeypatch.setattr(restore, "PRE_RESTORE_PATH", tmp_path / "pre-restore.db")
    return tmp_path


def test_preview_route_rejects_a_file_that_is_not_a_backup(client, staged_in_tmp):
    _login(client)
    r = client.post("/api/restore/preview",
                    files={"file": ("notes.txt", b"hello there", "text/plain")})
    assert r.status_code == 400
    assert "not a SQLite database" in r.json()["error"]
    # A rejected upload must not be left staged for a later apply to pick up.
    assert not restore.STAGED_PATH.exists()


def test_preview_route_returns_a_token_and_apply_requires_it(
        client, staged_in_tmp, tmp_path, monkeypatch, fake_host):
    _login(client)
    monkeypatch.setattr(restore, "docker_available", lambda: True)

    backup = _make_backup(tmp_path / "upload.db",
                          settings={"menu_title": "Restored"})
    r = client.post("/api/restore/preview",
                    files={"file": ("beacon-backup.db", backup.read_bytes(),
                                    "application/octet-stream")})
    assert r.status_code == 200
    preview = r.json()
    assert preview["token"]
    assert preview["summary"]["admins"] == 1

    # Wrong token: the staged file isn't the one that was previewed.
    r = client.post("/api/restore/apply", data={"token": "0" * 64})
    assert r.status_code == 409
    assert not restore.STAGED_PATH.exists()


def test_apply_route_restores_and_asks_for_a_restart(
        client, staged_in_tmp, tmp_path, monkeypatch, fake_host):
    _login(client)
    monkeypatch.setattr(restore, "docker_available", lambda: True)
    restarted = []
    monkeypatch.setattr(restore, "restart_self", lambda: restarted.append(True))

    backup = _make_backup(tmp_path / "upload.db",
                          settings={"menu_title": "Restored"})
    preview = client.post(
        "/api/restore/preview",
        files={"file": ("beacon-backup.db", backup.read_bytes(),
                        "application/octet-stream")}).json()

    r = client.post("/api/restore/apply", data={"token": preview["token"]})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert restarted == [True], "the restart is what makes the swap take effect"

    conn = sqlite3.connect(app_config.DB_PATH)
    restored = dict(conn.execute("SELECT key, value FROM settings"))
    conn.close()
    assert restored["menu_title"] == "Restored"


def test_apply_route_without_a_staged_file(client, staged_in_tmp):
    _login(client)
    r = client.post("/api/restore/apply", data={"token": "abc"})
    assert r.status_code == 400
    assert "No backup is staged" in r.json()["error"]


def test_restore_routes_require_admin(client, staged_in_tmp):
    """Unauthenticated callers are bounced, not served a restore."""
    r = client.post("/api/restore/apply", data={"token": "abc"},
                    follow_redirects=False)
    assert r.status_code in (302, 303, 307)
