"""Dashboard / home with a status overview and live system stats."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_admin, require_user, render
from ..models import BootEvent, Image, User
from ..services import clients, metrics
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
                  stats_reset=request.query_params.get("reset") == "1",
                  stats={"images": total, "ready": ready, "users": users})


@router.get("/api/stats")
def stats(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Live host performance + recent PXE clients, polled by the dashboard.

    Auth-gated (require_user) so the metrics aren't exposed unauthenticated.
    """
    rows = clients.recent()

    # In proxyDHCP mode dnsmasq never sees a client IP (the existing DHCP server
    # assigns it), so the log can't fill the IP column. The /track ping, however,
    # carries the client's IP -- backfill it by MAC from the latest boot event.
    macs = [r["mac"] for r in rows if not r.get("ip") and r.get("mac")]
    if macs:
        latest_ip: dict[str, str] = {}
        for mac, ip in db.execute(
            select(BootEvent.mac, BootEvent.ip)
            .where(BootEvent.mac.in_(macs), BootEvent.ip != "")
            .order_by(BootEvent.created_at.asc())
        ).all():
            latest_ip[mac] = ip            # asc order => last write wins = newest
        for r in rows:
            if not r.get("ip"):
                r["ip"] = latest_ip.get(r["mac"], "")

    total_deploys = db.scalar(select(func.count(BootEvent.id))) or 0
    # Distinct clients ever served (by MAC; ignore events that recorded no MAC).
    clients_served = db.scalar(
        select(func.count(func.distinct(BootEvent.mac)))
        .where(BootEvent.mac != "")
    ) or 0
    top_images = [
        {"name": name, "count": count}
        for name, count in db.execute(
            select(BootEvent.image_name, func.count(BootEvent.id))
            .group_by(BootEvent.image_name)
            .order_by(func.count(BootEvent.id).desc())
            .limit(5)
        ).all()
    ]

    return {
        "perf": metrics.sample(),
        "clients": rows,
        "clients_active": clients.count_active(rows),
        "clients_served": clients_served,
        "total_deploys": total_deploys,
        "top_images": top_images,
    }


@router.post("/stats/reset")
def reset_stats(request: Request, user: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    """Clear all-time deployment stats (clients served, total deploys, top images).

    These derive entirely from BootEvent rows, so dropping them resets the
    counters. We also truncate the dnsmasq log so the "recent clients" table
    clears -- otherwise it would keep showing clients from before the reset until
    the log naturally rolls over.
    """
    db.execute(delete(BootEvent))
    db.commit()
    clients.clear_log()
    return RedirectResponse("/?reset=1", status_code=303)
