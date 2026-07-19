"""Password hashing and authentication helpers."""
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# A precomputed hash verified against when the requested user does not exist, so
# a missing username costs the same bcrypt work as a wrong password. Without it,
# login timing leaks whether an account exists (a real user runs bcrypt, a
# missing one returns immediately -- measured ~157 ms vs ~1 ms).
_DUMMY_HASH = pwd_context.hash("beacon-timing-equalizer")


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


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


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
