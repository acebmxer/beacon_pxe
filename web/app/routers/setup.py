"""First-run setup wizard (admin-only, shown until completed)."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_admin, render
from ..models import User
from ..store import all_settings, set_setting
from ..services import dnsmasq, ipxe

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
            set_setting(db, key, str(form[key]).strip())
    set_setting(db, "setup_complete", "1")
    dnsmasq.render(db)
    ipxe.render(db)
    return RedirectResponse("/", status_code=303)
