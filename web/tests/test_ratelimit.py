"""Tests for the DB-backed login rate limiter."""
import time
import pytest

from app.models import LoginLockout
from app.services import ratelimit


class TestRetryAfter:
    def test_no_entry_allows(self, db):
        assert ratelimit.retry_after("1.2.3.4", db) == 0

    def test_after_lockout_blocked(self, db):
        future = time.time() + 1000
        db.add(LoginLockout(ip="1.2.3.4", locked_until=future, fail_count=0))
        db.commit()
        result = ratelimit.retry_after("1.2.3.4", db)
        assert result > 0

    def test_expired_lockout_allows(self, db):
        past = time.time() - 1000
        db.add(LoginLockout(ip="1.2.3.4", locked_until=past, fail_count=0))
        db.commit()
        assert ratelimit.retry_after("1.2.3.4", db) == 0


class TestRecordFailure:
    def test_single_failure_creates_row(self, db):
        ratelimit.record_failure("10.0.0.1", db)
        row = db.get(LoginLockout, "10.0.0.1")
        assert row is not None
        assert row.fail_count == 1

    def test_accumulates_failures(self, db):
        for _ in range(3):
            ratelimit.record_failure("10.0.0.1", db)
        row = db.get(LoginLockout, "10.0.0.1")
        assert row.fail_count == 3

    def test_lockout_after_max_failures(self, db):
        for _ in range(ratelimit._MAX_FAILURES):
            ratelimit.record_failure("10.0.0.2", db)
        assert ratelimit.retry_after("10.0.0.2", db) > 0

    def test_stale_failures_reset_count(self, db):
        old_time = time.time() - (ratelimit._WINDOW_SECONDS + 60)
        db.add(LoginLockout(ip="10.0.0.3", fail_count=4, last_fail=old_time))
        db.commit()
        ratelimit.record_failure("10.0.0.3", db)
        row = db.get(LoginLockout, "10.0.0.3")
        # Old failures expired, so counter starts fresh from 1
        assert row.fail_count == 1


class TestReset:
    def test_reset_clears_entry(self, db):
        ratelimit.record_failure("5.5.5.5", db)
        ratelimit.reset("5.5.5.5", db)
        assert db.get(LoginLockout, "5.5.5.5") is None

    def test_reset_no_entry_is_noop(self, db):
        ratelimit.reset("0.0.0.0", db)  # should not raise


class TestAllowInMemory:
    def test_within_limit_returns_true(self):
        for _ in range(5):
            assert ratelimit.allow("test_key", limit=10, window=60)

    def test_exceed_limit_returns_false(self):
        key = "over_limit_key"
        for _ in range(10):
            ratelimit.allow(key, limit=10, window=60)
        assert ratelimit.allow(key, limit=10, window=60) is False
