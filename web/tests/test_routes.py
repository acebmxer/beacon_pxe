"""Integration tests for key API routes.

All tests use the `client` fixture from conftest, which wires up a temp SQLite
database and stubs out file-writing services. The `client` fixture seeds
setup_complete=1 via bootstrap; additional data is seeded per test.
"""
import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from app.auth import hash_password
from app.models import Image, User, BootEvent, Setting
import app.db as app_db


def _session():
    """Open a session against whatever engine the app is currently using."""
    return app_db.SessionLocal()


def _seed_admin():
    db = _session()
    try:
        existing = db.execute(
            select(User).where(User.username == "testadmin")
        ).scalar_one_or_none()
        if existing:
            return existing
        admin = User(username="testadmin",
                     password_hash=hash_password("AdminPass123!"),
                     role="admin", session_epoch=0)
        db.add(admin)
        db.merge(Setting(key="setup_complete", value="1"))
        db.commit()
        db.refresh(admin)
        return admin
    finally:
        db.close()


def _seed_image(**kwargs) -> Image:
    db = _session()
    try:
        defaults = dict(name="Test", filename="t.iso", status="ready",
                        enabled=1, os_family="linux",
                        kernel_path="os/1/vmlinuz", initrd_path="os/1/initrd")
        defaults.update(kwargs)
        img = Image(**defaults)
        db.add(img)
        db.commit()
        db.refresh(img)
        return img
    finally:
        db.close()


def _login(client: TestClient):
    _seed_admin()
    client.post("/login", data={"username": "testadmin",
                                "password": "AdminPass123!"})


class TestHealthz:
    def test_healthz_ok(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestImagesStatus:
    def test_empty_when_no_images(self, client):
        _login(client)
        r = client.get("/images/status")
        assert r.status_code == 200
        assert r.json() == []

    def test_returns_image_status(self, client):
        _login(client)
        _seed_image()
        r = client.get("/images/status")
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 1
        assert items[0]["status"] == "ready"


class TestImageToggle:
    def test_toggle_changes_enabled(self, client):
        _login(client)
        img = _seed_image(enabled=1)
        r = client.post(f"/images/{img.id}/toggle")
        assert r.status_code in (200, 303)
        db = _session()
        try:
            updated = db.get(Image, img.id)
            assert updated.enabled == 0
        finally:
            db.close()


class TestImageDefault:
    def test_set_default(self, client):
        _login(client)
        img = _seed_image()
        client.post(f"/images/{img.id}/default")
        db = _session()
        try:
            assert db.get(Image, img.id).is_default == 1
        finally:
            db.close()

    def test_clear_default_by_toggling(self, client):
        _login(client)
        img = _seed_image()
        client.post(f"/images/{img.id}/default")  # set
        client.post(f"/images/{img.id}/default")  # clear
        db = _session()
        try:
            assert db.get(Image, img.id).is_default == 0
        finally:
            db.close()


class TestImageOrder:
    def test_order_endpoint(self, client):
        _login(client)
        a = _seed_image(name="A")
        b = _seed_image(name="B")
        r = client.post("/images/order", data={"order": f"{b.id},{a.id}"})
        assert r.status_code == 200
        db = _session()
        try:
            assert db.get(Image, b.id).display_order == 0
            assert db.get(Image, a.id).display_order == 1
        finally:
            db.close()


class TestBootEventsApi:
    def test_events_empty(self, client):
        _login(client)
        r = client.get("/api/events")
        assert r.status_code == 200
        assert r.json() == []

    def test_events_returns_records(self, client):
        _login(client)
        db = _session()
        try:
            db.add(BootEvent(image_id=1, image_name="Test",
                             mac="aa:bb:cc", ip="1.2.3.4"))
            db.commit()
        finally:
            db.close()
        r = client.get("/api/events")
        assert r.status_code == 200
        events = r.json()
        assert len(events) == 1
        assert events[0]["mac"] == "aa:bb:cc"

    def test_events_filter_by_image(self, client):
        _login(client)
        db = _session()
        try:
            db.add(BootEvent(image_id=1, image_name="Img1",
                             mac="aa:bb:cc", ip="1.1.1.1"))
            db.add(BootEvent(image_id=2, image_name="Img2",
                             mac="dd:ee:ff", ip="2.2.2.2"))
            db.commit()
        finally:
            db.close()
        r = client.get("/api/events?image_id=1")
        events = r.json()
        assert len(events) == 1
        assert events[0]["image_id"] == 1


class TestApiTokenAuth:
    def test_token_grants_access(self, client):
        _seed_admin()
        db = _session()
        try:
            admin = db.execute(
                select(User).where(User.username == "testadmin")
            ).scalar_one()
            admin.api_token = "test-token-abc123"
            db.commit()
        finally:
            db.close()
        r = client.get("/images/status",
                       headers={"Authorization": "Bearer test-token-abc123"})
        assert r.status_code == 200
