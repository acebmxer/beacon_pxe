"""OS image management: list, upload (ISO), edit, enable/disable, delete."""
import asyncio
import json
import re

from fastapi import (APIRouter, BackgroundTasks, Depends, Form, Request,
                     UploadFile, File)
from starlette.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db, SessionLocal
from ..deps import require_admin, require_user, render
from ..models import Image, User
from ..services import images as image_svc
from ..services import ipxe
from ..services import netboot as netboot_svc
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


def _page(request: Request, db: Session, **ctx):
    return render(request, db, "images.html", active="images",
                  images=_ordered_images(db),
                  catalog=netboot_svc.page_catalog(), **ctx)


@router.get("/images")
def images_page(request: Request, user: User = Depends(require_user),
                db: Session = Depends(get_db)):
    return _page(request, db)


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
        return _page(request, db,
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


@router.post("/images/netboot")
async def add_netboot(request: Request, background: BackgroundTasks,
                      user: User = Depends(require_admin),
                      distro: str = Form(...), variant: str = Form("installer"),
                      release: str = Form(""), live_release: str = Form(""),
                      flavour: str = Form(""),
                      mirror: str = Form(""), name: str = Form(""),
                      source: str = Form("mirror"), url: str = Form(""),
                      file: UploadFile | None = File(None),
                      db: Session = Depends(get_db)):
    """Add an image from a catalog distro without keeping an ISO.

    variant "installer": the distro's netboot archive (kernel + initrd).
    variant "live": a live desktop; Beacon fetches the live ISO, keeps its
    kernel, initrd and root filesystem, and deletes the ISO.
    """
    entry = netboot_svc.catalog_entry(distro)
    if entry is None:
        return _page(request, db, error="Unknown distro.")
    live = netboot_svc.live_entry(entry) if variant == "live" else None
    if variant == "live" and live is None:
        return _page(request, db, error=f"{entry['name']} has no live desktop entry.")
    try:
        if live:
            release = netboot_svc.validate_flavour(live, flavour)
            try:
                live_rel = netboot_svc.validate_live_release(live, live_release)
            except netboot_svc.NetbootError:
                live_rel = await run_in_threadpool(
                    netboot_svc.live_release_from_mirror, entry, live, live_release)
            mirror = netboot_svc.validate_mirror(mirror or live_rel["base_url"])
            if source == "url":
                source_url = netboot_svc.validate_source_url(url)
            else:
                if live_rel.get("unlisted"):
                    source_url = await run_in_threadpool(
                        netboot_svc.resolve_unlisted_live_url, live,
                        live_rel["unlisted"], release)
                else:
                    folder = await run_in_threadpool(
                        netboot_svc.resolve_release_folder, live_rel, mirror)
                    source_url = await run_in_threadpool(
                        netboot_svc.resolve_live_iso_url, live, folder, release)
                    netboot_svc.check_live_iso_matches(live, live_rel, source_url)
            label = next(f["label"] for f in live["flavours"] if f["id"] == release)
            version = f"{live_rel['name']} " if live_release.strip() else ""
            default_name = (f"{version or entry['name'] + ' '}Live {label}")
            filename = netboot_svc.new_archive_name(entry["id"], "live-" + release, ".iso")
        else:
            variant = "installer"
            release = netboot_svc.resolve_release(entry, release)
            mirror = netboot_svc.validate_mirror(mirror or entry["default_mirror"])
            number = next((r.get("number") for r in entry.get("releases", [])
                           if r["id"] == release), None)
            if release not in {r["id"] for r in entry.get("releases", [])}:
                found = await run_in_threadpool(
                    netboot_svc.lookup_release, entry, mirror, release)
                if found:
                    release, number = found["codename"], found["number"]
                elif re.fullmatch(r"(?i)(%s\s+)?[0-9]{1,3}" % re.escape(entry["name"]),
                                  release):
                    raise netboot_svc.NetbootError(
                        f"No release numbered {release} was found on {mirror}. "
                        "Type its name instead.")
            release = netboot_svc.validate_release(release)
            source_url = ""
            if source == "upload":
                if file is None or not file.filename:
                    raise netboot_svc.NetbootError("Choose an archive file to upload.")
            elif source == "url":
                source_url = netboot_svc.validate_source_url(url)
            else:
                source_url = netboot_svc.archive_url(entry, mirror, release)
            default_name = (f"{entry['name']} {number} {release} (netboot)" if number
                            else f"{entry['name']} {release} (netboot)")
            filename = netboot_svc.new_archive_name(entry["id"], release)
    except netboot_svc.NetbootError as e:
        return _page(request, db, error=str(e))

    size = 0
    if not live and source == "upload":
        with open(image_svc.iso_path(filename), "wb") as out:
            while chunk := await file.read(1024 * 1024):
                out.write(chunk)
                size += len(chunk)

    img = Image(
        # name is written verbatim into boot.ipxe; see upload().
        name=strip_control_chars(name.strip()) or default_name,
        filename=filename, status="pending", size_bytes=size,
        source="netboot", variant=variant, catalog_id=entry["id"],
        release=release, mirror=mirror, source_url=source_url,
    )
    db.add(img)
    db.commit()
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
