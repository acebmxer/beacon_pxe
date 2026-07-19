"""Server settings: DHCP mode, services, boot menu, theme."""
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_admin, require_user, render
from ..models import User
from ..store import all_settings, set_setting, strip_control_chars
from ..services import dnsmasq, ipxe
from ..services import images as image_svc

router = APIRouter()

# Settings the form is allowed to write, with simple coercion.
BOOL_KEYS = {"svc_dhcp", "svc_tftp", "svc_http"}
TEXT_KEYS = {
    "server_ip", "boot_interface", "dhcp_mode", "dhcp_range_start",
    "dhcp_range_end", "dhcp_subnet_mask", "dhcp_gateway", "dhcp_dns",
    "menu_title", "theme",
}


@router.get("/settings")
def settings_page(request: Request, user: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    return render(request, db, "settings.html",
                  active="settings", settings=all_settings(db), saved=False)


@router.post("/settings")
async def settings_save(request: Request, user: User = Depends(require_admin),
                        db: Session = Depends(get_db)):
    form = await request.form()
    for key in TEXT_KEYS:
        if key in form:
            set_setting(db, key, strip_control_chars(str(form[key]).strip()))
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
                  active="settings", settings=all_settings(db), saved=True)


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
