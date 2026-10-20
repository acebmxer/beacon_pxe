"""FastAPI application entrypoint."""
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import config, net
from .db import init_db, SessionLocal
from .deps import RedirectException, _user_by_token
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


# Cross-origin and anonymous state-changing requests.
#
# SameSite=Lax (below) stops other *sites* from sending the session cookie, but
# a page on another port of the same host -- nginx's boot files on :80, any
# other service on this box -- is the same site and its form posts carry the
# cookie. So a browser POST must come from this origin: browsers send Origin on
# every POST (Referer as the fallback). Scripts using a Bearer token send
# neither and are unaffected.
#
# Anonymous POSTs (other than signing in/out) are turned away here, before the
# route reads the body: FastAPI parses an upload form, spooling files to disk,
# before require_admin runs, so without this anyone could stream an endless
# upload at the console.
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_ANONYMOUS_POSTS = ("/login", "/logout")


def _request_hosts(request: Request) -> set[str]:
    hosts = {request.url.netloc}
    peer = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-host", "")
    if forwarded and peer and net.peer_is_trusted(peer):
        hosts.add(forwarded.split(",")[-1].strip())
    return hosts


@app.middleware("http")
async def csrf_and_anonymous_post_guard(request: Request, call_next):
    if request.method in _UNSAFE_METHODS:
        source = request.headers.get("origin") or request.headers.get("referer")
        if source is not None and urlparse(source).netloc not in _request_hosts(request):
            return PlainTextResponse("Cross-origin request refused.", status_code=403)
        if (not request.url.path.startswith(_ANONYMOUS_POSTS)
                and not request.session.get("uid")):
            db = SessionLocal()
            try:
                token_user = _user_by_token(db, request.headers.get("authorization"))
            finally:
                db.close()
            if token_user is None:
                return RedirectResponse("/login", status_code=303)
    return await call_next(request)


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
# same_site="lax" is one half of the CSRF defense: browsers won't send the
# session cookie on cross-site POSTs. It does nothing for other ports of the
# same host, which is what the Origin check in csrf_and_anonymous_post_guard
# (above) covers. "strict" would harden this slightly further
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
