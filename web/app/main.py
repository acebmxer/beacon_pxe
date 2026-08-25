"""FastAPI application entrypoint."""
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import config
from .db import init_db, SessionLocal, get_db
from .deps import RedirectException
from .store import get_setting
from .services import bootstrap
from .routers import (auth, dashboard, settings, images, drivers, users, setup,
                      track, updates)
from .services import updates as update_svc


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


_setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    bootstrap.run()
    # Reaching startup with an update in flight means this process is the
    # container the update just created, which is the confirmation the update
    # actually landed. Runs before the checker so the state is settled first.
    update_svc.finish_pending_update()
    update_svc.start_background_checker()
    yield


app = FastAPI(title="Beacon", docs_url=None, redoc_url=None, lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")),
          name="static")


# Bounce unauthenticated users (raised from require_user) to the login page.
@app.exception_handler(RedirectException)
async def _redirect_handler(request: Request, exc: RedirectException):
    return RedirectResponse(exc.location, status_code=303)


# ----- Health check (unauthenticated, for monitoring) ---------------------- #
@app.get("/healthz", include_in_schema=False)
def healthz():
    """Liveness probe: verifies the process is up and the DB is reachable."""
    from sqlalchemy import text as _text
    from .db import engine as _engine
    try:
        with _engine.connect() as conn:
            conn.execute(_text("SELECT 1"))
        return JSONResponse({"status": "ok"})
    except Exception as exc:  # pragma: no cover
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=503)


# Security headers applied to every response. Scripts are allowed ONLY via a
# per-request nonce (no 'unsafe-inline'), so injected inline <script> markup will
# not execute even if it slips past output escaping — the real XSS backstop. The
# nonce is minted per request below and stamped onto every inline <script> the
# templates emit (see deps.render -> csp_nonce). Styles still allow 'unsafe-inline'
# on purpose: the templates use inline style="" attributes throughout, which a
# nonce cannot cover (nonces apply to <style>/<script> elements, not attributes),
# and injected CSS is a far weaker vector than injected script.
def _csp(nonce: str) -> str:
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # Mint a per-request nonce before the route runs so the template can stamp it
    # on its inline scripts; the same value goes into the CSP header below.
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce
    response = await call_next(request)
    # Exempt /healthz and /static from CSP (not user-facing HTML pages).
    if not request.url.path.startswith(("/healthz", "/static")):
        response.headers.setdefault("Content-Security-Policy", _csp(nonce))
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


# Force the first-run wizard until it's completed.
_EXEMPT_PREFIXES = ("/login", "/logout", "/setup", "/static", "/theme", "/track",
                    "/healthz")


@app.middleware("http")
async def first_run_redirect(request: Request, call_next):
    path = request.url.path
    if not any(path.startswith(p) for p in _EXEMPT_PREFIXES):
        if request.session.get("uid"):
            db = SessionLocal()
            try:
                if get_setting(db, "setup_complete", "0") != "1":
                    return RedirectResponse("/setup", status_code=303)
            finally:
                db.close()
    return await call_next(request)


# SessionMiddleware is added LAST so it sits OUTERMOST in the stack and has
# populated request.session before first_run_redirect (above) runs.
#
# same_site="lax" is also the CSRF defense: browsers won't send the session
# cookie on cross-site POSTs, so a third-party page can't drive a state-changing
# request as the logged-in admin. "strict" would harden this slightly further
# but drops the cookie on ordinary top-level navigations into the app (e.g. a
# bookmarked deep link would appear logged out), which isn't worth it here.
# https_only adds the Secure flag so the browser only returns the cookie over
# HTTPS. Off by default (plain-HTTP trusted LAN); enabled via SESSION_SECURE when
# Beacon is behind a TLS-terminating proxy.
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY,
                   max_age=60 * 60 * 12, same_site="lax",
                   https_only=config.SESSION_SECURE)


app.include_router(auth.router)
app.include_router(setup.router)
app.include_router(dashboard.router)
app.include_router(settings.router)
app.include_router(images.router)
app.include_router(drivers.router)
app.include_router(users.router)
app.include_router(track.router)
app.include_router(updates.router)
