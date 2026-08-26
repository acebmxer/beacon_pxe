"""OS image management: list, upload (ISO), edit, enable/disable, delete."""
import asyncio
import json
import re

from fastapi import (APIRouter, BackgroundTasks, Depends, Form, Request,
                     UploadFile, File)
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db, SessionLocal
from ..deps import require_admin, require_user, render
from ..models import Image, User
from ..services import images as image_svc
from ..services import ipxe
from ..store import strip_control_chars

router = APIRouter()

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str) -> str:
    return _SAFE.sub("_", name).strip("_") or "image.iso"


def _ordered_images(db: Session) -> list[Image]:
    """All images sorted by display_order (NULLs last), then name."""
    return db.execute(
        select(Image).order_by(Image.display_order.is_(None), Image.display_order,
                               func.lower(Image.name))
    ).scalars().all()


@router.get("/images")
def images_page(request: Request, user: User = Depends(require_user),
                db: Session = Depends(get_db)):
    return render(request, db, "images.html", active="images",
                  images=_ordered_images(db))


@router.get("/images/status")
def images_status(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Lightweight JSON snapshot the images page polls while extraction runs."""
    items = db.execute(select(Image)).scalars().all()
    return [{"id": i.id, "status": i.status, "message": i.message} for i in items]


@router.get("/images/events")
async def images_sse(request: Request, user: User = Depends(require_user)):
    """SSE stream that pushes status updates while images are processing.

    The client connects when it lands on the images page with pending/processing
    rows. The stream polls the DB every second and emits an event whenever any
    image status changes. The connection is automatically closed (and the
    generator exits) when the client disconnects.
    """
    async def generate():
        seen: dict[int, str] = {}
        while True:
            if await request.is_disconnected():
                break
            db = SessionLocal()
            try:
                items = db.execute(select(Image)).scalars().all()
                updates = []
                for img in items:
                    if img.id in seen and seen[img.id] != img.status:
                        updates.append({
                            "id": img.id,
                            "status": img.status,
                            "message": img.message,
                        })
                    seen[img.id] = img.status
                if updates:
                    yield f"data: {json.dumps(updates)}\n\n"
                else:
                    yield ": heartbeat\n\n"
            finally:
                db.close()
            await asyncio.sleep(1)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/images/upload")
async def upload(request: Request, background: BackgroundTasks,
                 user: User = Depends(require_admin),
                 name: str = Form(""), file: UploadFile = File(...),
                 db: Session = Depends(get_db)):
    filename = _safe_filename(file.filename or "image.iso")
    if not filename.lower().endswith(".iso"):
        return render(request, db, "images.html", active="images",
                      images=_ordered_images(db),
                      error="Only .iso files are supported. See the README for why.")

    dest = image_svc.iso_path(filename)
    # Stream to disk in chunks so large ISOs don't load into memory.
    size = 0
    with open(dest, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
            size += len(chunk)

    img = Image(
        # name is written verbatim into boot.ipxe; strip control chars so a
        # newline can't inject additional iPXE commands into the menu.
        name=strip_control_chars(name.strip()) or filename.rsplit(".", 1)[0],
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


@router.post("/images/{image_id}/default")
def set_default(image_id: int, user: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    """Toggle the default boot image. Only one image can be default at a time.

    If the target is already the default, clicking again clears the default
    (no default image set). Clearing means the menu has no timeout and no
    pre-selected choice.
    """
    target = db.get(Image, image_id)
    if target:
        new_default = not target.is_default
        # Clear all other images' default flag first.
        for img in db.execute(select(Image)).scalars().all():
            img.is_default = 0
        if new_default:
            target.is_default = 1
        db.commit()
        ipxe.render(db)
    return RedirectResponse("/images", status_code=303)


@router.post("/images/order")
def reorder(order: str = Form(...), user: User = Depends(require_admin),
            db: Session = Depends(get_db)):
    """Set the display order for all images from a comma-separated ID list."""
    ids = [int(x) for x in order.split(",") if x.strip().isdigit()]
    for pos, img_id in enumerate(ids):
        img = db.get(Image, img_id)
        if img:
            img.display_order = pos
    db.commit()
    ipxe.render(db)
    return {"ok": True}


@router.post("/images/{image_id}/args")
def update_args(image_id: int, boot_args: str = Form(""),
                user: User = Depends(require_admin), db: Session = Depends(get_db)):
    img = db.get(Image, image_id)
    if img:
        # boot_args is written verbatim into boot.ipxe; strip control chars so a
        # newline can't inject additional iPXE commands into the menu.
        img.boot_args = strip_control_chars(boot_args.strip())
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
