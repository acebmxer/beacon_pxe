"""SQLAlchemy engine/session setup (SQLite)."""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from . import config


class Base(DeclarativeBase):
    pass


engine = create_engine(
    config.DB_URL,
    connect_args={"check_same_thread": False},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    """FastAPI dependency yielding a scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create tables. Imported models register themselves on Base.metadata."""
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _migrate()


def _migrate():
    """Idempotent, additive schema fixups for databases created by older builds.

    create_all() only creates missing tables, never adds columns to existing
    ones, so a column introduced after a table already exists has to be added by
    hand. Each step checks first, so this is safe to run on every startup.
    """
    with engine.begin() as conn:
        user_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(users)"))}
        if "session_epoch" not in user_cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN session_epoch "
                "INTEGER NOT NULL DEFAULT 0"))
        if "totp_secret" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN totp_secret TEXT"))
        if "totp_enabled" not in user_cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN totp_enabled INTEGER NOT NULL DEFAULT 0"))
        if "api_token" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN api_token TEXT"))

        img_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(images)"))}
        if "display_order" not in img_cols:
            conn.execute(text("ALTER TABLE images ADD COLUMN display_order INTEGER"))
        if "is_default" not in img_cols:
            conn.execute(text(
                "ALTER TABLE images ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0"))
