"""User management (admin) and self-service profile (any user)."""
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..auth import hash_password, verify_password, get_user_by_name, password_error
from ..deps import require_admin, require_user, render
from ..models import User
from ..services import totp as totp_svc

router = APIRouter()


def _admin_count(db: Session) -> int:
    return db.scalar(select(func.count(User.id)).where(User.role == "admin")) or 0


# --------------------------------------------------------------------------- #
# Admin: manage all users
# --------------------------------------------------------------------------- #
@router.get("/users")
def users_page(request: Request, user: User = Depends(require_admin),
               db: Session = Depends(get_db), error: str = "", ok: str = ""):
    items = db.execute(select(User).order_by(User.username)).scalars().all()
    return render(request, db, "users.html", active="users",
                  users=items, error=error, ok=ok)


@router.post("/users/create")
def create_user(request: Request, username: str = Form(...),
                password: str = Form(...), role: str = Form("user"),
                user: User = Depends(require_admin), db: Session = Depends(get_db)):
    username = username.strip()
    role = "admin" if role == "admin" else "user"
    if not username or not password:
        return users_page(request, user, db, error="Username and password required.")
    pw_err = password_error(password)
    if pw_err:
        return users_page(request, user, db, error=pw_err)
    if get_user_by_name(db, username):
        return users_page(request, user, db, error="That username already exists.")
    db.add(User(username=username, password_hash=hash_password(password), role=role))
    db.commit()
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/reset")
def reset_password(user_id: int, request: Request, password: str = Form(...),
                   user: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if not target or not password:
        return RedirectResponse("/users", status_code=303)
    pw_err = password_error(password)
    if pw_err:
        return users_page(request, user, db, error=pw_err)
    target.password_hash = hash_password(password)
    # Invalidate the target's existing sessions (see deps.current_user). If the
    # admin reset their own password here, keep this session alive by adopting
    # the new epoch.
    target.session_epoch = (target.session_epoch or 0) + 1
    db.commit()
    if target.id == user.id:
        request.session["ep"] = target.session_epoch
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/role")
def change_role(user_id: int, request: Request, role: str = Form("user"),
                user: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if target:
        new_role = "admin" if role == "admin" else "user"
        # Don't allow demoting the last remaining admin.
        if target.role == "admin" and new_role == "user" and _admin_count(db) <= 1:
            return users_page(request, user, db,
                              error="Cannot demote the last remaining admin.")
        target.role = new_role
        db.commit()
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/delete")
def delete_user(user_id: int, request: Request,
                user: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if not target:
        return RedirectResponse("/users", status_code=303)
    if target.id == user.id:
        return users_page(request, user, db, error="You cannot delete your own account.")
    if target.role == "admin" and _admin_count(db) <= 1:
        return users_page(request, user, db, error="Cannot delete the last admin.")
    db.delete(target)
    db.commit()
    return RedirectResponse("/users", status_code=303)


# --------------------------------------------------------------------------- #
# Self-service: any logged-in user edits their own profile/password
# --------------------------------------------------------------------------- #
@router.get("/profile")
def profile_page(request: Request, user: User = Depends(require_user),
                 db: Session = Depends(get_db), error: str = "", ok: str = ""):
    return render(request, db, "profile.html", active="profile", error=error, ok=ok,
                  totp_uri=None, new_totp_secret=None)


@router.post("/profile/password")
def change_own_password(request: Request, current_password: str = Form(...),
                        new_password: str = Form(...),
                        user: User = Depends(require_user),
                        db: Session = Depends(get_db)):
    if not verify_password(current_password, user.password_hash):
        return render(request, db, "profile.html", active="profile",
                      error="Current password is incorrect.", ok="",
                      totp_uri=None, new_totp_secret=None)
    pw_err = password_error(new_password)
    if pw_err:
        return render(request, db, "profile.html", active="profile",
                      error=pw_err, ok="", totp_uri=None, new_totp_secret=None)
    user.password_hash = hash_password(new_password)
    # Log out this account's OTHER sessions (see deps.current_user), but keep the
    # one making the change by adopting the new epoch.
    user.session_epoch = (user.session_epoch or 0) + 1
    db.commit()
    request.session["ep"] = user.session_epoch
    return render(request, db, "profile.html", active="profile",
                  ok="Password updated.", error="", totp_uri=None, new_totp_secret=None)


# --------------------------------------------------------------------------- #
# TOTP second factor
# --------------------------------------------------------------------------- #
@router.post("/profile/totp/setup")
def totp_setup_start(request: Request, user: User = Depends(require_user),
                     db: Session = Depends(get_db)):
    """Generate a new TOTP secret and present the provisioning URI.

    The secret is not activated until the user confirms with a valid code via
    /profile/totp/confirm, so a half-started setup doesn't lock anyone out.
    """
    secret = totp_svc.generate_secret()
    uri = totp_svc.provisioning_uri(secret, user.username)
    # Store the pending secret in the session so /totp/confirm can find it
    # without committing it to the DB yet.
    request.session["totp_pending"] = secret
    return render(request, db, "profile.html", active="profile",
                  error="", ok="",
                  totp_uri=uri, new_totp_secret=secret)


@router.post("/profile/totp/confirm")
def totp_setup_confirm(request: Request, code: str = Form(...),
                       user: User = Depends(require_user),
                       db: Session = Depends(get_db)):
    """Activate TOTP after the user confirms a valid code from their app."""
    secret = request.session.get("totp_pending")
    if not secret:
        return render(request, db, "profile.html", active="profile",
                      error="Setup session expired. Please start again.", ok="",
                      totp_uri=None, new_totp_secret=None)
    if not totp_svc.verify(secret, code):
        uri = totp_svc.provisioning_uri(secret, user.username)
        return render(request, db, "profile.html", active="profile",
                      error="Code did not match. Try again.", ok="",
                      totp_uri=uri, new_totp_secret=secret)
    user.totp_secret = secret
    user.totp_enabled = 1
    db.commit()
    request.session.pop("totp_pending", None)
    return render(request, db, "profile.html", active="profile",
                  ok="Two-factor authentication enabled.", error="",
                  totp_uri=None, new_totp_secret=None)


@router.post("/profile/totp/disable")
def totp_disable(request: Request, current_password: str = Form(...),
                 user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Disable TOTP after confirming the account password."""
    if not verify_password(current_password, user.password_hash):
        return render(request, db, "profile.html", active="profile",
                      error="Current password is incorrect.", ok="",
                      totp_uri=None, new_totp_secret=None)
    user.totp_secret = None
    user.totp_enabled = 0
    db.commit()
    return render(request, db, "profile.html", active="profile",
                  ok="Two-factor authentication disabled.", error="",
                  totp_uri=None, new_totp_secret=None)


# --------------------------------------------------------------------------- #
# API token
# --------------------------------------------------------------------------- #
@router.post("/profile/token/generate")
def generate_token(request: Request, user: User = Depends(require_user),
                   db: Session = Depends(get_db)):
    """Generate (or replace) the user's Bearer API token."""
    user.api_token = secrets.token_hex(32)
    db.commit()
    return render(request, db, "profile.html", active="profile",
                  ok="New API token generated.", error="",
                  totp_uri=None, new_totp_secret=None)


@router.post("/profile/token/revoke")
def revoke_token(request: Request, user: User = Depends(require_user),
                 db: Session = Depends(get_db)):
    """Revoke the user's Bearer API token."""
    user.api_token = None
    db.commit()
    return render(request, db, "profile.html", active="profile",
                  ok="API token revoked.", error="",
                  totp_uri=None, new_totp_secret=None)
