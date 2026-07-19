"""Best-effort boot tracking pinged by the iPXE menu.

nginx serves the actual boot files, so the web app never sees a deployment. The
boot menu fetches /track/{image_id} (with the client's MAC/IP) just before it
boots an image; we log a BootEvent here so the dashboard can show all-time
clients-served and per-image deploy counts.

This is intentionally public and forgiving: iPXE can't authenticate, and the
menu pings us with `|| goto ...` so a failure never blocks a client from booting.
We always return a trivially-valid iPXE script and HTTP 200.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import BootEvent, Image

log = logging.getLogger("beacon.track")

router = APIRouter()

_OK = PlainTextResponse("#!ipxe\nexit 0\n", media_type="text/plain")


@router.get("/track/{image_id}")
def track(image_id: int, request: Request):
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
        # This endpoint is unauthenticated, so bound every stored field to its
        # column size. SQLite does not enforce String(n) limits, so without this
        # a client could persist arbitrarily large mac/ip values (disk-fill).
        db.add(BootEvent(
            image_id=image_id,
            image_name=(img.name if img else f"#{image_id}")[:128],
            mac=mac[:32],
            ip=ip[:64],
        ))
        db.commit()
    except Exception:                       # never let tracking break a boot
        log.exception("failed to record boot event for image %s", image_id)
        db.rollback()
    finally:
        db.close()
    return _OK
