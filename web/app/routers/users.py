"""User management (admin) and self-service profile (any user)."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..auth import hash_password, verify_password, get_user_by_name, password_error
from ..deps import require_admin, require_user, render
from ..models import User

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
    db.commit()
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
    return render(request, db, "profile.html", active="profile", error=error, ok=ok)


@router.post("/profile/password")
def change_own_password(request: Request, current_password: str = Form(...),
                        new_password: str = Form(...),
                        user: User = Depends(require_user),
                        db: Session = Depends(get_db)):
    if not verify_password(current_password, user.password_hash):
        return profile_page(request, user, db, error="Current password is incorrect.")
    pw_err = password_error(new_password)
    if pw_err:
        return profile_page(request, user, db, error=pw_err)
    user.password_hash = hash_password(new_password)
    db.commit()
    return profile_page(request, user, db, ok="Password updated.")
