"""Best-effort boot tracking pinged by the iPXE menu.

nginx serves the actual boot files, so the web app never sees a deployment. The
boot menu fetches /track/{image_id} (with the client's MAC/IP) just before it
boots an image; we log a BootEvent here so the dashboard can show all-time
clients-served and per-image deploy counts.

This is intentionally public and forgiving: iPXE can't authenticate, and the
menu pings us with `|| goto ...` so a failure never blocks a client from booting.
We always return a trivially-valid iPXE script and HTTP 200 — even when we skip
recording, so throttling or a bad id never stops a boot.
"""
import logging
import random

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import BootEvent, Image
from ..net import client_ip
from ..services import ratelimit

log = logging.getLogger("beacon.track")

router = APIRouter()

_OK = PlainTextResponse("#!ipxe\nexit 0\n", media_type="text/plain")

# Abuse controls for this unauthenticated, side-effecting endpoint. The rate
# limit keys on the (proxy-aware) client IP so a real client's occasional reboot
# is never throttled, while a flood is capped. The row cap bounds total on-disk
# growth regardless of source; it is generous enough that ordinary all-time
# stats are untouched and only an extreme volume trims the oldest events.
_RATE_LIMIT = 120          # recorded events per client IP per minute
_RATE_WINDOW = 60.0
_MAX_ROWS = 200_000
_PRUNE_PROBABILITY = 0.02  # amortise the cap check across inserts


def _prune(db: Session) -> None:
    """Trim boot_events back to the newest _MAX_ROWS rows (oldest ids go first)."""
    keep = select(BootEvent.id).order_by(BootEvent.id.desc()).limit(_MAX_ROWS)
    db.query(BootEvent).filter(~BootEvent.id.in_(keep)).delete(
        synchronize_session=False)
    db.commit()


@router.get("/track/{image_id}")
def track(image_id: int, request: Request):
    # Cap per-client recording rate. On exceed we still return a valid script so
    # the boot proceeds; only the stats write is skipped.
    if not ratelimit.allow(f"track:{client_ip(request)}", _RATE_LIMIT, _RATE_WINDOW):
        return _OK

    mac = (request.query_params.get("mac") or "").lower().strip()
    # Prefer the IP iPXE put in the query; fall back to what nginx forwards
    # (X-Real-IP), then the socket peer. The peer is nginx itself once proxied,
    # so it's the last resort.
    ip = (request.query_params.get("ip") or "").strip()
    if ip in ("", "0.0.0.0"):
        ip = (request.headers.get("x-real-ip")
              or (request.client.host if request.client else "")).strip()
    db: Session = SessionLocal()
    try:
        img = db.get(Image, image_id)
        # Only record boots of a real, enabled image. An unknown or hidden id is
        # never in the menu, so a hit on one is noise or abuse — recording it
        # would let anyone grow the table with phantom rows.
        if img is None or not img.enabled:
            return _OK
        # This endpoint is unauthenticated, so bound every stored field to its
        # column size. SQLite does not enforce String(n) limits, so without this
        # a client could persist arbitrarily large mac/ip values (disk-fill).
        db.add(BootEvent(
            image_id=image_id,
            image_name=img.name[:128],
            mac=mac[:32],
            ip=ip[:64],
        ))
        db.commit()
        if random.random() < _PRUNE_PROBABILITY:
            if (db.scalar(select(func.count(BootEvent.id))) or 0) > _MAX_ROWS:
                _prune(db)
    except Exception:                       # never let tracking break a boot
        log.exception("failed to record boot event for image %s", image_id)
        db.rollback()
    finally:
        db.close()
    return _OK
