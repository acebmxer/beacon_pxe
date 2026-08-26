"""Login / logout, with optional TOTP second factor."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..auth import authenticate
from ..deps import current_user, render
from ..models import User
from ..net import client_ip
from ..services import ratelimit, totp as totp_svc

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
    wait = ratelimit.retry_after(key, db)
    if wait:
        # Refuse without checking the password, so a lockout can't be probed.
        mins = (wait + 59) // 60
        return render(request, db, "login.html",
                      error=f"Too many failed attempts. Try again in {mins} "
                            f"minute{'s' if mins != 1 else ''}.")

    user = authenticate(db, username, password)
    if user is None:
        ratelimit.record_failure(key, db)
        return render(request, db, "login.html",
                      error="Invalid username or password.")
    ratelimit.reset(key, db)

    # If the user has TOTP enabled, hold the authentication in "pending" state
    # and redirect to the TOTP challenge page. We use a separate session key
    # ("totp_uid") that does NOT satisfy current_user() so the user has no
    # access to protected pages until the second factor is confirmed.
    if user.totp_enabled:
        request.session["totp_uid"] = user.id
        return RedirectResponse("/login/totp", status_code=303)

    request.session["uid"] = user.id
    request.session["ep"] = user.session_epoch
    return RedirectResponse("/", status_code=303)


@router.get("/login/totp")
def totp_form(request: Request, db: Session = Depends(get_db)):
    if not request.session.get("totp_uid"):
        return RedirectResponse("/login", status_code=303)
    return render(request, db, "login_totp.html", error=None, hide_chrome=True)


@router.post("/login/totp")
def totp_submit(
    request: Request,
    code: str = Form(...),
    db: Session = Depends(get_db),
):
    uid = request.session.get("totp_uid")
    if not uid:
        return RedirectResponse("/login", status_code=303)

    user = db.get(User, uid)
    if user is None or not user.totp_enabled:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    if not totp_svc.verify(user.totp_secret or "", code):
        return render(request, db, "login_totp.html",
                      error="Invalid code. Please try again.", hide_chrome=True)

    # Second factor confirmed — promote to a full session.
    request.session.pop("totp_uid", None)
    request.session["uid"] = user.id
    request.session["ep"] = user.session_epoch
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    # POST-only: a state-changing GET could be triggered cross-site (e.g.
    # <img src=".../logout">) since SameSite=Lax still sends the cookie on
    # top-level GETs. The nav "Sign out" control posts this form.
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

