"""OS image management: list, upload (ISO), edit, enable/disable, delete, and
the custom netboot catalog entries."""
import asyncio
import json
import re

from fastapi import (APIRouter, BackgroundTasks, Depends, Form, Query, Request,
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


_ENTRY_FORM_FIELDS = ("name", "default_mirror", "kind", "archive_url", "kernel_member",
                      "initrd_member", "kernel_url", "initrd_url", "boot_args",
                      "releases")


def _page(request: Request, db: Session, **ctx):
    images = _ordered_images(db)
    custom = netboot_svc.custom_entries(db)
    used = {e["id"]: sum(1 for i in images if i.source == "netboot"
                         and i.catalog_id == e["id"]) for e in custom}
    ctx.setdefault("entry_form", {"kind": "archive"})
    return render(request, db, "images.html", active="images", images=images,
                  catalog=netboot_svc.page_catalog(db), custom_entries=custom,
                  entry_uses=used,
                  notice=request.session.pop("image_notice", None), **ctx)


def _entry_to_form(entry: dict) -> dict:
    form = {k: entry.get(k, "") for k in _ENTRY_FORM_FIELDS}
    form.update(original_id=entry["id"],
                kind="files" if entry.get("kernel_url") else "archive",
                releases=netboot_svc.release_lines(entry))
    return form


def _form_to_entry(form: dict, db: Session) -> dict:
    """The entry the custom-entry form describes, validated."""
    raw = {k: form[k] for k in ("name", "default_mirror", "boot_args")}
    keys = (("kernel_url", "initrd_url") if form["kind"] == "files"
            else ("archive_url", "kernel_member", "initrd_member"))
    raw.update({k: form[k] for k in keys})
    raw["releases"] = netboot_svc.parse_releases(form["releases"])
    if form["original_id"]:
        old = netboot_svc.custom_entry(db, form["original_id"])
        if old is None:
            raise netboot_svc.NetbootError("This entry no longer exists; add it again.")
        raw["id"] = old["id"]
        # The form has no box for a release's number or end-of-life date, so an
        # imported entry keeps them for the releases it still lists.
        kept = {r["id"]: r for r in old.get("releases", [])}
        for rel in raw["releases"]:
            rel.update({k: kept[rel["id"]][k] for k in ("number", "eol")
                        if k in kept.get(rel["id"], {})})
    return netboot_svc.validate_entry(raw)


@router.get("/images")
def images_page(request: Request, user: User = Depends(require_user),
                edit: str = Query(""), db: Session = Depends(get_db)):
    entry = netboot_svc.custom_entry(db, edit) if edit and user.is_admin else None
    return _page(request, db, **({"entry_form": _entry_to_form(entry)} if entry else {}))


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
    entry = netboot_svc.catalog_entry(distro, db)
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
                elif entry.get("number_lookup") and re.fullmatch(
                        r"(?i)(%s\s+)?[0-9]{1,3}" % re.escape(entry["name"]), release):
                    raise netboot_svc.NetbootError(
                        f"No release numbered {release} was found on {mirror}. "
                        "Type its name instead.")
            release = netboot_svc.validate_release(release)
            source_url = ""
            if entry.get("kernel_url"):
                # Its kernel and initrd are fetched separately when it is processed.
                if source != "mirror":
                    raise netboot_svc.NetbootError(
                        f"{entry['name']} downloads its kernel and initrd separately, "
                        "so it can only use Download from the mirror.")
            elif source == "upload":
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


@router.post("/images/catalog/entry")
async def save_entry(request: Request, user: User = Depends(require_admin),
                     action: str = Form("preview"), original_id: str = Form(""),
                     name: str = Form(""), default_mirror: str = Form(""),
                     kind: str = Form("archive"), archive_url: str = Form(""),
                     kernel_member: str = Form(""), initrd_member: str = Form(""),
                     kernel_url: str = Form(""), initrd_url: str = Form(""),
                     boot_args: str = Form(""), releases: str = Form(""),
                     db: Session = Depends(get_db)):
    """Preview, then save, a custom catalog entry from the form.

    Preview fills the templates for every release and checks the files on the
    mirror; the page offers Save only after a preview, so the admin has seen
    the kernel command line the entry produces.
    """
    form = dict(original_id=original_id, name=name, default_mirror=default_mirror,
                kind="files" if kind == "files" else "archive", archive_url=archive_url,
                kernel_member=kernel_member, initrd_member=initrd_member,
                kernel_url=kernel_url, initrd_url=initrd_url, boot_args=boot_args,
                releases=releases)
    try:
        entry = _form_to_entry(form, db)
        if action == "save" and not original_id:
            if len(netboot_svc.custom_entries(db)) >= netboot_svc.MAX_CUSTOM_ENTRIES:
                raise netboot_svc.NetbootError(
                    f"There are already {netboot_svc.MAX_CUSTOM_ENTRIES} custom entries.")
            entry["id"] = netboot_svc.free_entry_id(db, entry["id"])
    except netboot_svc.NetbootError as e:
        return _page(request, db, entry_form=form, entry_error=str(e))
    if action != "save":
        preview = await run_in_threadpool(netboot_svc.preview_entry, entry)
        return _page(request, db, entry_form=form, entry_preview=preview)
    netboot_svc.store_entry(db, entry)
    request.session["image_notice"] = (
        f"Saved the custom entry {entry['name']}. It is in the Distro list of "
        "Add from a netboot catalog.")
    return RedirectResponse("/images#custom-entries", status_code=303)


@router.post("/images/catalog/import")
async def import_entries(request: Request, user: User = Depends(require_admin),
                         action: str = Form("preview"), content: str = Form(""),
                         file: UploadFile | None = File(None),
                         db: Session = Depends(get_db)):
    """Import custom entries from a JSON file: preview every entry, then save
    them all on confirm. The file's text rides along in the preview page, so
    nothing is kept on the server in between."""
    try:
        if file is not None and file.filename:
            raw = await file.read(netboot_svc.IMPORT_MAX_BYTES + 1)
            if len(raw) > netboot_svc.IMPORT_MAX_BYTES:
                raise netboot_svc.NetbootError("The file is larger than 256 KB.")
            content = raw.decode("utf-8", "replace")
        if not content.strip():
            raise netboot_svc.NetbootError("Choose a JSON file to import.")
        entries = netboot_svc.parse_import(content)
        builtin = netboot_svc.builtin_ids()
        clash = [e["id"] for e in entries if e["id"] in builtin]
        if clash:
            raise netboot_svc.NetbootError(
                f"Nothing was imported. The ID {', '.join(clash)} belongs to a built-in "
                "entry; give the entry another \"id\".")
        existing = {e["id"] for e in netboot_svc.custom_entries(db)}
        added = [e for e in entries if e["id"] not in existing]
        if len(existing) + len(added) > netboot_svc.MAX_CUSTOM_ENTRIES:
            raise netboot_svc.NetbootError(
                f"Nothing was imported: it would make more than "
                f"{netboot_svc.MAX_CUSTOM_ENTRIES} custom entries.")
    except netboot_svc.NetbootError as e:
        return _page(request, db, import_error=str(e))
    if action != "save":
        previews = await run_in_threadpool(
            lambda: [netboot_svc.preview_entry(e) for e in entries])
        return _page(request, db, import_preview={
            "content": content,
            "entries": [{"entry": e, "rows": p, "replaces": e["id"] in existing}
                        for e, p in zip(entries, previews)]})
    for e in entries:
        netboot_svc.store_entry(db, e)
    replaced = len(entries) - len(added)
    request.session["image_notice"] = (
        f"Imported {len(entries)} custom entr{'y' if len(entries) == 1 else 'ies'}: "
        + ", ".join(e["name"] for e in entries)
        + (f" ({replaced} replaced an entry with the same ID)." if replaced else "."))
    return RedirectResponse("/images#custom-entries", status_code=303)


@router.post("/images/catalog/{entry_id}/delete")
def delete_entry(request: Request, entry_id: str, user: User = Depends(require_admin),
                 db: Session = Depends(get_db)):
    entry = netboot_svc.custom_entry(db, entry_id)
    if entry and netboot_svc.delete_entry(db, entry_id):
        request.session["image_notice"] = (
            f"Deleted the custom entry {entry['name']}. Images made from it keep "
            "booting; Reprocess needs the entry back.")
    return RedirectResponse("/images#custom-entries", status_code=303)
