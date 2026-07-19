"""FastAPI application entrypoint."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import config
from .db import init_db, SessionLocal
from .deps import RedirectException
from .store import get_setting
from .services import bootstrap
from .routers import auth, dashboard, settings, images, users, setup, track, updates
from .services import updates as update_svc

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


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


# Security headers applied to every response. The CSP still permits inline
# script/style because the templates rely on them; it locks down framing
# (clickjacking), plugins, and the base URI, and is a meaningful backstop
# against injected markup. Tightening to a nonce-based CSP (dropping
# 'unsafe-inline') is a possible follow-up but needs a template refactor.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


# Force the first-run wizard until it's completed.
_EXEMPT_PREFIXES = ("/login", "/logout", "/setup", "/static", "/theme", "/track")


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
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY,
                   max_age=60 * 60 * 12, same_site="lax")


app.include_router(auth.router)
app.include_router(setup.router)
app.include_router(dashboard.router)
app.include_router(settings.router)
app.include_router(images.router)
app.include_router(users.router)
app.include_router(track.router)
app.include_router(updates.router)
