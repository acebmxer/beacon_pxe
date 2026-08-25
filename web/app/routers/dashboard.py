"""Dashboard / home with a status overview and live system stats."""
from datetime import datetime, timedelta, timezone

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
    # Fold BootEvent (the authoritative per-boot record: MAC + IP + timestamp)
    # into the log-derived rows. In proxyDHCP mode the dnsmasq log never carries a
    # client IP and a re-boot often shows up only as TFTP (no MAC), so the log
    # alone leaves the IP blank and "last seen" stuck at the last DHCP exchange --
    # the boot events refresh both. Bounded to the last week so the poll stays
    # cheap as the all-time BootEvent history grows; asc order => newest wins.
    # Naive UTC to match how created_at is stored (SQLite drops the tzinfo).
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
    boot_events = db.execute(
        select(BootEvent.mac, BootEvent.ip, BootEvent.created_at)
        .where(BootEvent.mac != "", BootEvent.created_at >= since)
        .order_by(BootEvent.created_at.asc())
    ).all()
    rows = clients.recent(boot_events=boot_events)

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


@router.get("/api/events")
def boot_events(
    image_id: int | None = None,
    since: str | None = None,
    limit: int = 100,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Programmatic access to boot events for external tooling.

    Query parameters:
      image_id  — filter to a specific image (optional)
      since     — ISO 8601 UTC timestamp, e.g. 2026-01-01T00:00:00 (optional)
      limit     — max rows to return (default 100, max 1000)
    """
    limit = max(1, min(limit, 1000))
    q = select(BootEvent).order_by(BootEvent.created_at.desc()).limit(limit)
    if image_id is not None:
        q = q.where(BootEvent.image_id == image_id)
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            since_naive = since_dt.replace(tzinfo=None)
            q = q.where(BootEvent.created_at >= since_naive)
        except ValueError:
            pass
    events = db.execute(q).scalars().all()
    return [
        {
            "id": e.id,
            "image_id": e.image_id,
            "image_name": e.image_name,
            "mac": e.mac,
            "ip": e.ip,
            "created_at": e.created_at.isoformat() + "Z",
        }
        for e in events
    ]


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
    # Fold BootEvent (the authoritative per-boot record: MAC + IP + timestamp)
    # into the log-derived rows. In proxyDHCP mode the dnsmasq log never carries a
    # client IP and a re-boot often shows up only as TFTP (no MAC), so the log
    # alone leaves the IP blank and "last seen" stuck at the last DHCP exchange --
    # the boot events refresh both. Bounded to the last week so the poll stays
    # cheap as the all-time BootEvent history grows; asc order => newest wins.
    # Naive UTC to match how created_at is stored (SQLite drops the tzinfo).
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
    boot_events = db.execute(
        select(BootEvent.mac, BootEvent.ip, BootEvent.created_at)
        .where(BootEvent.mac != "", BootEvent.created_at >= since)
        .order_by(BootEvent.created_at.asc())
    ).all()
    rows = clients.recent(boot_events=boot_events)

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
