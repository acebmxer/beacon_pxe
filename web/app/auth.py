"""Password hashing and authentication helpers."""
import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import User

# bcrypt reads at most 72 bytes of a secret. passlib (used before) and bcrypt
# 4.0 silently dropped the rest; bcrypt 5 raises instead. Cutting it here keeps
# hashes made by passlib verifying exactly as before on any bcrypt version.
# (New passwords can't be longer anyway: see password_error.)
_BCRYPT_MAX_BYTES = 72


def _secret(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    # gensalt()'s defaults ($2b$, cost 12) are what passlib produced, so new and
    # existing hashes look the same.
    return bcrypt.hashpw(_secret(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_secret(password), password_hash.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        # Not a bcrypt hash (a damaged row, say): no password matches it.
        return False


# A precomputed hash verified against when the requested user does not exist, so
# a missing username costs the same bcrypt work as a wrong password. Without it,
# login timing leaks whether an account exists (a real user runs bcrypt, a
# missing one returns immediately -- measured ~157 ms vs ~1 ms).
_DUMMY_HASH = hash_password("beacon-timing-equalizer")


MIN_PASSWORD_LENGTH = 12
# bcrypt silently truncates the input beyond 72 bytes, so anything past that
# would not actually be part of the checked secret. Reject it rather than let a
# user believe their long passphrase is fully in effect.
_MAX_PASSWORD_BYTES = 72


def password_error(password: str) -> str | None:
    """Return a human-readable reason the password is unacceptable, else None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password.encode("utf-8")) > _MAX_PASSWORD_BYTES:
        return "Password is too long (max 72 bytes)."
    return None


def get_user_by_name(db: Session, username: str) -> User | None:
    return db.execute(
        select(User).where(User.username == username)
    ).scalar_one_or_none()


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = get_user_by_name(db, username)
    # Always run a verify -- against the real hash if the user exists, otherwise
    # against a dummy -- so the response time doesn't reveal whether the account
    # exists.
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if verify_password(password, user.password_hash):
        return user
    return None
