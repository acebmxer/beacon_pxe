"""Server settings: DHCP mode, services, boot menu, theme, backup/restore."""
import os
import shutil
import tempfile
from urllib.parse import urlparse

from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form, Request,
                     UploadFile)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from .. import config
from ..deps import require_admin, require_user, render
from ..models import User
from ..store import all_settings, set_setting, strip_control_chars
from ..services import dnsmasq, ipxe
from ..services import images as image_svc
from ..services import restore as restore_svc

router = APIRouter()

# Settings the form is allowed to write, with simple coercion.
BOOL_KEYS = {"svc_dhcp", "svc_tftp", "svc_http"}
TEXT_KEYS = {
    "server_ip", "boot_interface", "dhcp_mode", "dhcp_range_start",
    "dhcp_range_end", "dhcp_subnet_mask", "dhcp_gateway", "dhcp_dns",
    "menu_title", "theme", "boot_timeout",
}


@router.get("/settings")
def settings_page(request: Request, user: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    return render(request, db, "settings.html",
                  active="settings", settings=all_settings(db), saved=False,
                  restore_notice=restore_svc.current_notice(db))


@router.post("/settings")
async def settings_save(request: Request, user: User = Depends(require_admin),
                        db: Session = Depends(get_db)):
    form = await request.form()
    for key in TEXT_KEYS:
        if key in form:
            raw = strip_control_chars(str(form[key]).strip())
            # boot_timeout must be a non-negative integer.
            if key == "boot_timeout":
                try:
                    raw = str(max(0, int(raw)))
                except ValueError:
                    raw = "30"
            set_setting(db, key, raw)
    # Checkboxes only appear in the form when checked.
    for key in BOOL_KEYS:
        set_setting(db, key, "1" if key in form else "0")

    # Regenerate boot configs; the reload sidecar restarts dnsmasq.
    dnsmasq.render(db)
    ipxe.render(db)
    # XCP-NG GRUB chainloaders bake in the server IP, so rebuild them in case it
    # changed (cheap: no re-extraction).
    image_svc.rebuild_xcpng_grub_all(db)
    # Windows WinPE setup script also bakes in the server IP for its SMB mount.
    image_svc.rebuild_windows_setup_all(db)
    return render(request, db, "settings.html",
                  active="settings", settings=all_settings(db), saved=True,
                  restore_notice=restore_svc.current_notice(db))


@router.post("/theme")
def toggle_theme(request: Request, user: User = Depends(require_user),
                 db: Session = Depends(get_db)):
    """Quick global light/dark toggle from the navbar."""
    current = all_settings(db).get("theme", "dark")
    set_setting(db, "theme", "light" if current == "dark" else "dark")
    # Return to the page the toggle was clicked from, but only if the Referer is
    # same-origin -- otherwise a crafted Referer would make this an open redirect.
    dest = "/"
    referer = request.headers.get("referer", "")
    if referer:
        parsed = urlparse(referer)
        if not parsed.netloc or parsed.netloc == request.url.netloc:
            dest = parsed.path or "/"
            if parsed.query:
                dest += "?" + parsed.query
    return RedirectResponse(dest, status_code=303)


@router.get("/api/backup")
def backup(background: BackgroundTasks, user: User = Depends(require_admin)):
    """Download the SQLite database as beacon-backup.db.

    Creates a temporary copy of the live DB file so the download is consistent
    even if writes land during the transfer. The temp file is deleted after the
    response body is sent.
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    shutil.copy2(config.DB_PATH, tmp.name)
    background.add_task(os.unlink, tmp.name)
    return FileResponse(
        tmp.name,
        filename="beacon-backup.db",
        media_type="application/octet-stream",
    )


@router.post("/api/restore/preview")
async def restore_preview(file: UploadFile = File(...),
                          user: User = Depends(require_admin),
                          db: Session = Depends(get_db)):
    """Stage an uploaded backup and report what restoring it would do.

    Writes nothing but the staged file: the live database is untouched until
    /api/restore/apply is called with the token returned here.
    """
    size = 0
    try:
        with open(restore_svc.STAGED_PATH, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > restore_svc.MAX_UPLOAD_BYTES:
                    raise restore_svc.RestoreError(
                        "That file is far larger than any Beacon database. "
                        "Upload beacon-backup.db, not an image or an archive.")
                out.write(chunk)

        preview = restore_svc.build_preview(db, restore_svc.STAGED_PATH)
        preview["token"] = restore_svc.token_for(restore_svc.STAGED_PATH)
        preview["filename"] = file.filename or "beacon-backup.db"
        # Surfaced now rather than after the swap: without the daemon nothing
        # can restart this container, and the admin should know that before
        # committing rather than be left on a half-applied restore.
        preview["can_restart"] = restore_svc.docker_available()
    except restore_svc.RestoreError as exc:
        restore_svc.clear_staged()
        return JSONResponse({"error": str(exc)}, status_code=400)
    except OSError as exc:
        restore_svc.clear_staged()
        return JSONResponse({"error": f"Could not stage the upload: {exc}"},
                            status_code=500)

    return JSONResponse(preview)


@router.post("/api/restore/apply")
def restore_apply(background: BackgroundTasks, token: str = Form(...),
                  user: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    """Replace the live database with the staged backup, then restart.

    The token ties this to the file the admin actually previewed, so a second
    upload landing in between can't be applied unseen.
    """
    if not restore_svc.STAGED_PATH.exists():
        return JSONResponse(
            {"error": "No backup is staged. Choose a file and preview it first."},
            status_code=400)

    if token != restore_svc.token_for(restore_svc.STAGED_PATH):
        restore_svc.clear_staged()
        return JSONResponse(
            {"error": "The staged file changed since it was previewed. "
                      "Upload it again and re-check the preview."},
            status_code=409)

    try:
        result = restore_svc.apply(db, restore_svc.STAGED_PATH)
    except restore_svc.RestoreError as exc:
        restore_svc.clear_staged()
        return JSONResponse({"error": str(exc)}, status_code=400)

    # After the response is sent — the browser needs to receive this before the
    # container goes down, or the admin sees a network error instead of the
    # "reconnecting" state.
    background.add_task(restore_svc.restart_self)
    return JSONResponse({"ok": True, **result})


@router.post("/api/restore/cancel")
def restore_cancel(user: User = Depends(require_admin)):
    """Discard a staged upload the admin decided not to apply."""
    restore_svc.clear_staged()
    return JSONResponse({"ok": True})


@router.post("/api/restore/dismiss")
def restore_dismiss(user: User = Depends(require_admin),
                    db: Session = Depends(get_db)):
    """Clear the post-restart restore notice."""
    restore_svc.clear_notice(db)
    return JSONResponse({"ok": True})
