"""FastAPI application entrypoint."""
import logging
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

app = FastAPI(title="Beacon", docs_url=None, redoc_url=None)

app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")),
          name="static")


@app.on_event("startup")
def on_startup():
    init_db()
    bootstrap.run()
    # Reaching startup with an update in flight means this process is the
    # container the update just created, which is the confirmation the update
    # actually landed. Runs before the checker so the state is settled first.
    update_svc.finish_pending_update()
    update_svc.start_background_checker()


# Bounce unauthenticated users (raised from require_user) to the login page.
@app.exception_handler(RedirectException)
async def _redirect_handler(request: Request, exc: RedirectException):
    return RedirectResponse(exc.location, status_code=303)


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
