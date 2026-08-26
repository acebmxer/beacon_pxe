"""Tests for auth helpers and store utilities."""
import pytest

from app.auth import authenticate, hash_password, verify_password, password_error
from app.models import User, Setting
from app.store import get_setting, set_setting, all_settings
from app import config


class TestPasswordValidation:
    def test_too_short(self):
        assert password_error("short") is not None

    def test_exactly_min_length(self):
        assert password_error("a" * 12) is None

    def test_above_max_bytes(self):
        # 73 single-byte chars exceeds 72-byte bcrypt limit
        assert password_error("a" * 73) is not None

    def test_valid_password(self):
        assert password_error("validpassword123") is None


class TestHashAndVerify:
    def test_hash_verifies(self):
        h = hash_password("correct-horse-battery")
        assert verify_password("correct-horse-battery", h)

    def test_wrong_password_fails(self):
        h = hash_password("correct-horse-battery")
        assert not verify_password("wrong-password", h)


class TestAuthenticate:
    def test_valid_credentials(self, db):
        db.add(User(username="alice", password_hash=hash_password("ValidPass123"),
                    role="user"))
        db.commit()
        user = authenticate(db, "alice", "ValidPass123")
        assert user is not None
        assert user.username == "alice"

    def test_wrong_password(self, db):
        db.add(User(username="bob", password_hash=hash_password("RealPass456"),
                    role="user"))
        db.commit()
        assert authenticate(db, "bob", "WrongPass") is None

    def test_nonexistent_user(self, db):
        assert authenticate(db, "nobody", "anything") is None


class TestStore:
    def test_get_default_from_config(self, db):
        val = get_setting(db, "theme")
        assert val == config.DEFAULTS.get("theme", "dark")

    def test_set_and_get(self, db):
        set_setting(db, "menu_title", "My Beacon")
        assert get_setting(db, "menu_title") == "My Beacon"

    def test_all_settings_includes_defaults(self, db):
        s = all_settings(db)
        assert "theme" in s
        assert "server_ip" in s

    def test_all_settings_override(self, db):
        set_setting(db, "theme", "light")
        s = all_settings(db)
        assert s["theme"] == "light"
