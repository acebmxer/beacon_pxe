"""Login throttle with SQLite-backed lockout state.

Two protections:
  * a sliding window of recent failures per key that trips a lockout once full,
  * a lockout (locked_until timestamp) that refuses every attempt until it expires.

Successful logins clear the key. Lockout state persists across container restarts
because it lives in the database, so a restart can no longer be used to bypass it.

A separate in-memory sliding-window throttle (allow()) is also provided for
unauthenticated endpoints like /track — those don't need persistence because
their only effect is skipping a stat write, not blocking access.
"""
from __future__ import annotations

import threading
import time

from sqlalchemy.orm import Session

from ..models import LoginLockout

# Deliberately generous so a fat-fingered admin isn't locked out, but tight
# enough to make online brute force impractical against bcrypt.
_MAX_FAILURES = 5          # failures within the window before lockout
_WINDOW_SECONDS = 300      # 5 min: failures older than this no longer count
_LOCKOUT_SECONDS = 900     # 15 min lockout once the window fills


def retry_after(key: str, db: Session) -> int:
    """Seconds the caller must wait, or 0 if a login attempt is allowed now."""
    now = time.time()
    row = db.get(LoginLockout, key)
    if row is None:
        return 0
    if row.locked_until and row.locked_until > now:
        return int(row.locked_until - now) + 1
    # Window expired — prune stale entry.
    if row.last_fail and (now - row.last_fail) >= _WINDOW_SECONDS:
        db.delete(row)
        db.commit()
    return 0


def record_failure(key: str, db: Session) -> None:
    """Register a failed attempt; trip a lockout once the window is full."""
    now = time.time()
    row = db.get(LoginLockout, key)
    if row is None:
        row = LoginLockout(ip=key, fail_count=0, locked_until=None, last_fail=None)
        db.add(row)
    # Reset count if the previous failure is outside the window.
    if row.last_fail is not None and (now - row.last_fail) >= _WINDOW_SECONDS:
        row.fail_count = 0
    row.fail_count += 1
    row.last_fail = now
    if row.fail_count >= _MAX_FAILURES:
        row.locked_until = now + _LOCKOUT_SECONDS
        row.fail_count = 0
    db.commit()


def reset(key: str, db: Session) -> None:
    """Clear all failure/lockout state for a key (called on a successful login)."""
    row = db.get(LoginLockout, key)
    if row is not None:
        db.delete(row)
        db.commit()


# --- Generic in-memory sliding-window throttle ----------------------------- #
# Separate from the login lockout above: only caps event recording rate with
# no lockout. Used to bound unauthenticated endpoints (e.g. /track) — callers
# treat False as "skip the side effect", not "reject the request".
_events_lock = threading.Lock()
_events: dict[str, list[float]] = {}


def allow(key: str, limit: int, window: float = 60.0) -> bool:
    """True if an event for `key` is within `limit` per `window`, and record it."""
    now = time.monotonic()
    with _events_lock:
        recent = [t for t in _events.get(key, ()) if now - t < window]
        if len(recent) >= limit:
            _events[key] = recent
            return False
        recent.append(now)
        _events[key] = recent
        # Opportunistic cleanup so one-off keys can't grow the dict without bound.
        if len(_events) > 4096:
            for k in [k for k, v in _events.items()
                      if not any(now - t < window for t in v)]:
                _events.pop(k, None)
        return True
