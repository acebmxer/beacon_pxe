"""First-run setup wizard (admin-only, shown until completed)."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_admin, render
from ..models import User
from ..store import all_settings, set_setting, strip_control_chars
from ..services import dnsmasq, ipxe
from ..services import images as image_svc

router = APIRouter()

WIZARD_KEYS = {
    "server_ip", "boot_interface", "dhcp_mode", "dhcp_range_start",
    "dhcp_range_end", "dhcp_subnet_mask", "dhcp_gateway", "dhcp_dns",
    "menu_title",
}


@router.get("/setup")
def setup_page(request: Request, user: User = Depends(require_admin),
               db: Session = Depends(get_db)):
    return render(request, db, "setup.html", settings=all_settings(db),
                  hide_chrome=True)


@router.post("/setup")
async def setup_save(request: Request, user: User = Depends(require_admin),
                     db: Session = Depends(get_db)):
    form = await request.form()
    for key in WIZARD_KEYS:
        if key in form:
            set_setting(db, key, strip_control_chars(str(form[key]).strip()))
    set_setting(db, "setup_complete", "1")
    dnsmasq.render(db)
    ipxe.render(db)
    # Rebuild XCP-NG GRUB chainloaders with the (possibly new) server IP.
    image_svc.rebuild_xcpng_grub_all(db)
    # Re-patch Windows WinPE setup script (SMB mount bakes in the server IP).
    image_svc.rebuild_windows_setup_all(db)
    return RedirectResponse("/", status_code=303)
