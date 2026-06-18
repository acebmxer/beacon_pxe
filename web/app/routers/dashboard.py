"""Dashboard / home with a status overview."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_user, render
from ..models import Image, User
from ..store import all_settings

router = APIRouter()


@router.get("/")
def dashboard(request: Request, user: User = Depends(require_user),
              db: Session = Depends(get_db)):
    settings = all_settings(db)
    total = db.scalar(select(func.count(Image.id))) or 0
    ready = db.scalar(select(func.count(Image.id)).where(Image.status == "ready")) or 0
    users = db.scalar(select(func.count(User.id))) or 0
    return render(request, db, "dashboard.html",
                  active="dashboard",
                  settings=settings,
                  stats={"images": total, "ready": ready, "users": users})
