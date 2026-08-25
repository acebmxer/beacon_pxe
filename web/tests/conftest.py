"""Test fixtures and shared test database setup.

Unit tests (test_ipxe, test_ratelimit, test_auth) use the `db` fixture directly
and never touch the file system or the running app.

Route tests (test_routes) use the `client` fixture which spins up the app with a
temporary SQLite database and stubs out the services that write boot config files
— those are covered by test_ipxe separately.
"""
import os
import pytest
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient


# ---------------------------------------------------------------------------
# Shared in-memory engine used by the `db` fixture for unit tests.
# ---------------------------------------------------------------------------
_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
)
_TestingSession = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture(autouse=True)
def fresh_tables():
    """Recreate all tables before each test so tests are isolated."""
    from app import models  # noqa: F401 — registers models on Base.metadata
    from app.db import Base
    Base.metadata.create_all(bind=_engine)
    yield
    Base.metadata.drop_all(bind=_engine)


@pytest.fixture
def db():
    """A clean SQLAlchemy session backed by the in-memory test engine."""
    session = _TestingSession()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Route / integration tests: full TestClient with file-system stubs.
# ---------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path):
    """TestClient with a temp-file SQLite DB and stubbed file-write services."""
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"

    from sqlalchemy import create_engine as _ce
    from sqlalchemy.orm import sessionmaker as _sm

    test_engine = _ce(db_url, connect_args={"check_same_thread": False})
    TestSession = _sm(bind=test_engine, autoflush=False, expire_on_commit=False)

    # Re-wire the app's engine and SessionLocal before importing main so that
    # bootstrap.run() and route handlers all land on the same temp DB.
    import app.db as app_db
    import app.config as app_config

    orig_engine = app_db.engine
    orig_session = app_db.SessionLocal
    orig_db_url = app_config.DB_URL
    orig_db_path = app_config.DB_PATH

    app_db.engine = test_engine
    app_db.SessionLocal = TestSession
    app_config.DB_URL = db_url
    app_config.DB_PATH = db_path

    # main.py and bootstrap.py bind SessionLocal at import time via
    # `from .db import SessionLocal`, creating a local name that won't be
    # affected by patching app_db.SessionLocal. Patch each module directly so
    # the middleware and bootstrap both write to the same temp DB.
    import app.main as app_main
    import app.services.bootstrap as bootstrap_mod
    orig_main_sl = app_main.SessionLocal
    orig_bootstrap_sl = bootstrap_mod.SessionLocal
    app_main.SessionLocal = TestSession
    bootstrap_mod.SessionLocal = TestSession

    # Point all path-based config to temp dirs so service code doesn't explode.
    orig_bootroot = app_config.BOOTROOT_DIR
    orig_dnsmasq = app_config.DNSMASQ_DIR
    orig_tftp = app_config.TFTP_DIR
    app_config.BOOTROOT_DIR = tmp_path / "bootroot"
    app_config.DNSMASQ_DIR = tmp_path / "dnsmasq"
    app_config.TFTP_DIR = tmp_path / "tftp"
    for d in (app_config.BOOTROOT_DIR, app_config.DNSMASQ_DIR, app_config.TFTP_DIR):
        d.mkdir(parents=True, exist_ok=True)

    from app.db import get_db

    def override_get_db():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

    from app.main import app
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c

    # Restore everything.
    app.dependency_overrides.pop(get_db, None)
    app_main.SessionLocal = orig_main_sl
    bootstrap_mod.SessionLocal = orig_bootstrap_sl
    app_db.engine = orig_engine
    app_db.SessionLocal = orig_session
    app_config.DB_URL = orig_db_url
    app_config.DB_PATH = orig_db_path
    app_config.BOOTROOT_DIR = orig_bootroot
    app_config.DNSMASQ_DIR = orig_dnsmasq
    app_config.TFTP_DIR = orig_tftp
    test_engine.dispose()
