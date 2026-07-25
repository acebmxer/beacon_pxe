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
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(users)"))}
        if "session_epoch" not in cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN session_epoch "
                "INTEGER NOT NULL DEFAULT 0"))
