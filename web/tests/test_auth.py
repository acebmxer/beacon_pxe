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

    # Made by the passlib CryptContext Beacon used before (passlib 1.7.4,
    # bcrypt 4.0.1). Existing accounts hold hashes like these and must keep
    # signing in after the switch to calling bcrypt directly.
    PASSLIB_HASH = "$2b$12$8uF2terSP4dxadkp4O1bbuwqzbAgkwWFIpMw4/FY8IVLVFg81M9pG"
    # passlib's hash of "x" * 72 + "tail": it used only the first 72 bytes.
    PASSLIB_LONG_HASH = "$2b$12$iOEqkitz3rBwS.dNgb8MZ.Cb0kGU1NtX94p2sQrCj9vt3uj5e8NJm"

    def test_passlib_hash_still_verifies(self):
        assert verify_password("OldPassword-from-passlib", self.PASSLIB_HASH)
        assert not verify_password("OldPassword-from-passlib!", self.PASSLIB_HASH)

    def test_passlib_long_password_matches_on_first_72_bytes(self):
        assert verify_password("x" * 72 + "tail", self.PASSLIB_LONG_HASH)
        assert verify_password("x" * 72 + "other", self.PASSLIB_LONG_HASH)
        assert not verify_password("x" * 71, self.PASSLIB_LONG_HASH)

    def test_new_hash_has_passlib_format(self):
        h = hash_password("correct-horse-battery")
        assert h.startswith("$2b$12$") and len(h) == 60

    def test_damaged_hash_is_a_failed_login_not_an_error(self):
        assert not verify_password("anything", "not-a-hash")
        assert not verify_password("anything", "")


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
