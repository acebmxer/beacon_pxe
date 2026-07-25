"""Drop-in Windows storage drivers: list, upload, delete.

Admin-only for writes. See services.drivers for what the folder is and why
uploading to it needs no image reprocessing.
"""
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..deps import require_admin, require_user, render
from ..models import User
from ..services import drivers as driver_svc
from ..services import images as images_svc

router = APIRouter()


def _rebake(request: Request, db: Session) -> None:
    """Re-patch every ready Windows boot.wim after a boot-driver change.

    Baked NIC drivers live inside each wim, so unlike the SMB share a change
    here isn't live until the wims are rewritten. Done inline (a few seconds
    per image) so that when the redirect lands, what the page shows is already
    true on disk.
    """
    n = images_svc.rebuild_windows_setup_all(db)
    request.session["driver_notice"] = (
        f"Boot images updated: {n} Windows image{'' if n == 1 else 's'} "
        "re-baked with the current NIC drivers."
        if n else
        "No ready Windows image to re-bake — the drivers will be baked in "
        "when a Windows image is processed.")


def _fail(request: Request, message: str) -> RedirectResponse:
    """Post/Redirect/Get with the message carried in the signed session cookie.

    Rendering the error straight from the POST would leave the browser on
    /drivers/upload, so a refresh re-submits; putting the text in the URL would
    make the message attacker-supplied. The session is signed, so neither applies.
    """
    request.session["driver_error"] = message
    return RedirectResponse("/drivers", status_code=303)


@router.get("/drivers")
def drivers_page(request: Request, user: User = Depends(require_user),
                 db: Session = Depends(get_db)):
    entries = driver_svc.list_entries()
    boot_entries = driver_svc.list_entries(config.NICDRIVERS_DIR)
    # Mark catalog packs that are already staged so the page offers Fetch only
    # for the missing ones (a re-fetch is delete + fetch, same as re-upload).
    staged = {e["name"] for e in entries}
    staged_boot = {e["name"] for e in boot_entries}
    catalog = [dict(p, staged=p["id"] in (
                   staged_boot if p.get("target") == "boot" else staged))
               for p in driver_svc.load_catalog()]
    return render(request, db, "drivers.html", active="drivers", entries=entries,
                  boot_entries=boot_entries,
                  total_infs=driver_svc.total_infs(entries),
                  human_size=driver_svc.human_size, catalog=catalog,
                  error=request.session.pop("driver_error", None),
                  notice=request.session.pop("driver_notice", None))


@router.post("/drivers/upload")
async def upload(request: Request, user: User = Depends(require_admin),
                 name: str = Form(""), target: str = Form("share"),
                 files: list[UploadFile] = File(...),
                 db: Session = Depends(get_db)):
    files = [f for f in files if f.filename]
    if not files:
        return _fail(request, "No files selected.")

    is_zip = len(files) == 1 and (files[0].filename or "").lower().endswith(".zip")

    # Default the folder name from what was uploaded: the zip's stem, or the
    # first file's stem for a loose selection.
    stem = Path(files[0].filename or "drivers").stem
    pack_name = driver_svc.safe_pack_name(name.strip() or stem)

    try:
        root = driver_svc.target_root(target)
        dest = driver_svc.pack_dir(pack_name, root)
    except driver_svc.DriverError as exc:
        return _fail(request, str(exc))
    if dest.exists():
        return _fail(request, f"“{pack_name}” already exists. Delete it first, or "
                              "upload under a different name.")

    dest.mkdir(parents=True)
    try:
        if is_zip:
            # Stage the archive next to the drivers folder rather than in /tmp: it
            # can be tens of megabytes and this path is known to be a real volume.
            with tempfile.TemporaryDirectory(dir=config.DATA_DIR) as tmp:
                staged = Path(tmp) / "upload.zip"
                with open(staged, "wb") as out:
                    while chunk := await files[0].read(1024 * 1024):
                        out.write(chunk)
                driver_svc.extract_zip(staged, dest)
        else:
            count = 0
            for f in files:
                out_path = driver_svc.member_path(dest, f.filename or "")
                if out_path is None:
                    continue
                with open(out_path, "wb") as out:
                    while chunk := await f.read(1024 * 1024):
                        out.write(chunk)
                count += 1
            if not count:
                raise driver_svc.DriverError("No usable files in the selection.")
    except driver_svc.DriverError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        return _fail(request, str(exc))
    except Exception:
        # Never leave a half-written pack behind: WinPE drvloads whatever landed,
        # and a truncated .sys is worse than no driver at all.
        shutil.rmtree(dest, ignore_errors=True)
        raise

    if target == "boot":
        _rebake(request, db)
    return RedirectResponse("/drivers", status_code=303)


@router.post("/drivers/fetch")
def fetch(request: Request, pack: str = Form(...),
          user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Download + verify + stage one curated pack from drivers_catalog.json.

    Sync on purpose: FastAPI runs it in the threadpool, and the payloads are
    a few MB, so the redirect lands in seconds. The heavy lifting and all
    failure modes (offline host, checksum mismatch, layout drift) live in
    services.drivers.fetch_pack and surface here as the page's error flash.
    Boot-target packs additionally re-bake every ready Windows boot.wim before
    the redirect, so the next PXE boot already carries them.
    """
    entry = driver_svc.catalog_pack(pack)
    if entry is None:
        return _fail(request, "Unknown driver pack.")
    try:
        driver_svc.fetch_pack(entry)
    except driver_svc.DriverError as exc:
        return _fail(request, str(exc))
    if entry.get("target") == "boot":
        _rebake(request, db)
    return RedirectResponse("/drivers", status_code=303)


@router.post("/drivers/delete")
def delete(request: Request, entry: str = Form(...),
           target: str = Form("share"), user: User = Depends(require_admin),
           db: Session = Depends(get_db)):
    try:
        driver_svc.delete_entry(entry, driver_svc.target_root(target))
    except driver_svc.DriverError as exc:
        return _fail(request, str(exc))
    if target == "boot":
        _rebake(request, db)
    return RedirectResponse("/drivers", status_code=303)
