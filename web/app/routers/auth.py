"""Login / logout."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..auth import authenticate
from ..deps import current_user, render
from ..net import client_ip
from ..services import ratelimit

router = APIRouter()


def _client_key(request: Request) -> str:
    """Throttle key for a login attempt: the connecting client's IP.

    Uses the trusted-proxy-aware client IP (see net.client_ip) — behind a reverse
    proxy the socket peer is the proxy, so keying on it would let a handful of
    failures from anyone lock out every admin.
    """
    return client_ip(request)


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_db)):
    # Check for a genuinely valid session, not just a uid in the cookie: a stale
    # session (deleted user, or one invalidated by a password change) must fall
    # through to the login form, not bounce to "/" and back in a redirect loop.
    if current_user(request, db) is not None:
        return RedirectResponse("/", status_code=303)
    return render(request, db, "login.html", error=None)


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    key = _client_key(request)
    wait = ratelimit.retry_after(key)
    if wait:
        # Refuse without checking the password, so a lockout can't be probed.
        mins = (wait + 59) // 60
        return render(request, db, "login.html",
                      error=f"Too many failed attempts. Try again in {mins} "
                            f"minute{'s' if mins != 1 else ''}.")

    user = authenticate(db, username, password)
    if user is None:
        ratelimit.record_failure(key)
        return render(request, db, "login.html",
                      error="Invalid username or password.")
    ratelimit.reset(key)
    request.session["uid"] = user.id
    # Pin the session to the user's current password epoch; a later password
    # change bumps the epoch and invalidates this session (see deps.current_user).
    request.session["ep"] = user.session_epoch
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    # POST-only: a state-changing GET could be triggered cross-site (e.g.
    # <img src=".../logout">) since SameSite=Lax still sends the cookie on
    # top-level GETs. The nav "Sign out" control posts this form.
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
