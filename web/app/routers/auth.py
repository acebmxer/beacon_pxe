"""Login / logout."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..auth import authenticate
from ..deps import render
from ..services import ratelimit

router = APIRouter()


def _client_key(request: Request) -> str:
    """Throttle key for a login attempt: the connecting client's IP."""
    return request.client.host if request.client else "unknown"


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_db)):
    if request.session.get("uid"):
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
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
