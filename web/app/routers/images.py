"""OS image management: list, upload (ISO), edit, enable/disable, delete."""
import re

from fastapi import (APIRouter, BackgroundTasks, Depends, Form, Request,
                     UploadFile, File)
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_admin, require_user, render
from ..models import Image, User
from ..services import images as image_svc
from ..services import ipxe

router = APIRouter()

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str) -> str:
    return _SAFE.sub("_", name).strip("_") or "image.iso"


@router.get("/images")
def images_page(request: Request, user: User = Depends(require_user),
                db: Session = Depends(get_db)):
    items = db.execute(select(Image).order_by(Image.created_at.desc())).scalars().all()
    return render(request, db, "images.html", active="images", images=items)


@router.get("/images/status")
def images_status(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Lightweight JSON snapshot the images page polls while extraction runs."""
    items = db.execute(select(Image)).scalars().all()
    return [{"id": i.id, "status": i.status, "message": i.message} for i in items]


@router.post("/images/upload")
async def upload(request: Request, background: BackgroundTasks,
                 user: User = Depends(require_admin),
                 name: str = Form(""), file: UploadFile = File(...),
                 db: Session = Depends(get_db)):
    filename = _safe_filename(file.filename or "image.iso")
    if not filename.lower().endswith(".iso"):
        items = db.execute(select(Image).order_by(Image.created_at.desc())).scalars().all()
        return render(request, db, "images.html", active="images", images=items,
                      error="Only .iso files are supported. See the README for why.")

    dest = image_svc.iso_path(filename)
    # Stream to disk in chunks so large ISOs don't load into memory.
    size = 0
    with open(dest, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
            size += len(chunk)

    img = Image(
        name=name.strip() or filename.rsplit(".", 1)[0],
        filename=filename,
        status="pending",
        size_bytes=size,
    )
    db.add(img)
    db.commit()

    # Extraction can be slow; run it after the response is sent.
    background.add_task(image_svc.process_image, img.id)
    return RedirectResponse("/images", status_code=303)


@router.post("/images/{image_id}/toggle")
def toggle(image_id: int, user: User = Depends(require_admin),
           db: Session = Depends(get_db)):
    img = db.get(Image, image_id)
    if img:
        img.enabled = 0 if img.enabled else 1
        db.commit()
        ipxe.render(db)
    return RedirectResponse("/images", status_code=303)


@router.post("/images/{image_id}/args")
def update_args(image_id: int, boot_args: str = Form(""),
                user: User = Depends(require_admin), db: Session = Depends(get_db)):
    img = db.get(Image, image_id)
    if img:
        img.boot_args = boot_args.strip()
        db.commit()
        ipxe.render(db)
    return RedirectResponse("/images", status_code=303)


@router.post("/images/{image_id}/retry")
def retry(image_id: int, background: BackgroundTasks,
          user: User = Depends(require_admin), db: Session = Depends(get_db)):
    img = db.get(Image, image_id)
    if img:
        img.status = "pending"
        db.commit()
        background.add_task(image_svc.process_image, img.id)
    return RedirectResponse("/images", status_code=303)


@router.post("/images/{image_id}/delete")
def delete(image_id: int, user: User = Depends(require_admin),
           db: Session = Depends(get_db)):
    img = db.get(Image, image_id)
    if img:
        image_svc.delete_image(db, img)
    return RedirectResponse("/images", status_code=303)
