"""Auth dependencies and shared template context."""
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from pathlib import Path

from .db import get_db
from .models import User
from .store import get_setting

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_STATIC_DIR = Path(__file__).parent / "static"


def _asset_version() -> str:
    """Cache-busting token for static assets: newest mtime under static/.

    Bumps automatically whenever a CSS/JS file changes, so browsers re-fetch the
    stylesheet instead of serving a stale cached copy.
    """
    try:
        return str(int(max(p.stat().st_mtime for p in _STATIC_DIR.glob("*"))))
    except ValueError:
        return "0"


class RedirectException(Exception):
    """Raised to bounce unauthenticated users to the login page."""

    def __init__(self, location: str):
        self.location = location


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    return db.get(User, uid)


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = current_user(request, db)
    if user is None:
        raise RedirectException("/login")
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    user = require_user(request, db)
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Admin privileges required")
    return user


def render(request: Request, db: Session, template: str, **ctx):
    """Render a template with common context (current user, theme, active nav)."""
    user = current_user(request, db)
    base = {
        "request": request,
        "user": user,
        "theme": get_setting(db, "theme"),
        "menu_title": get_setting(db, "menu_title"),
        "asset_version": _asset_version(),
    }
    base.update(ctx)
    return templates.TemplateResponse(template, base)
