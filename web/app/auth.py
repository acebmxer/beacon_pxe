"""Password hashing and authentication helpers."""
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


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
    if user and verify_password(password, user.password_hash):
        return user
    return None
