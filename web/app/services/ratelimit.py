"""Lightweight in-memory login throttle.

Beacon is a single-node admin tool, so failed-login state lives in a process
dict rather than a shared store. The management port is served directly by
uvicorn (not behind nginx), so the socket peer is the real client and a good
throttle key.

Two protections:
  * a sliding window of recent failures per key, and
  * a lockout once the window fills, during which every attempt is refused
    without even checking the password.

Successful logins clear the key. Entries expire on their own, so the dict can't
grow without bound from random source IPs.
"""
from __future__ import annotations

import threading
import time

# Tunables. Deliberately generous so a fat-fingered admin isn't locked out, but
# tight enough to make online brute force impractical against bcrypt.
_MAX_FAILURES = 5          # failures allowed within the window before lockout
_WINDOW_SECONDS = 300      # 5 min: failures older than this no longer count
_LOCKOUT_SECONDS = 900     # 15 min lockout once the window fills

_lock = threading.Lock()
# key -> list of monotonic timestamps of recent failures (most recent last).
_failures: dict[str, list[float]] = {}
# key -> monotonic time when an active lockout ends.
_locked_until: dict[str, float] = {}


def _prune(now: float) -> None:
    """Drop expired lockouts and empty failure lists. Caller holds the lock."""
    for key in [k for k, until in _locked_until.items() if until <= now]:
        _locked_until.pop(key, None)
    for key in list(_failures):
        recent = [t for t in _failures[key] if now - t < _WINDOW_SECONDS]
        if recent:
            _failures[key] = recent
        else:
            _failures.pop(key, None)


def retry_after(key: str) -> int:
    """Seconds the caller must wait, or 0 if a login attempt is allowed now."""
    now = time.monotonic()
    with _lock:
        _prune(now)
        until = _locked_until.get(key)
        if until and until > now:
            return int(until - now) + 1
        return 0


def record_failure(key: str) -> None:
    """Register a failed attempt; trip a lockout once the window is full."""
    now = time.monotonic()
    with _lock:
        _prune(now)
        hits = _failures.setdefault(key, [])
        hits.append(now)
        if len(hits) >= _MAX_FAILURES:
            _locked_until[key] = now + _LOCKOUT_SECONDS
            _failures.pop(key, None)


def reset(key: str) -> None:
    """Clear all failure/lockout state for a key (called on a successful login)."""
    with _lock:
        _failures.pop(key, None)
        _locked_until.pop(key, None)
