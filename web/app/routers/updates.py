"""Update check and apply endpoints (admin only)."""
import threading

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..deps import require_admin
from ..models import User
from ..store import get_setting
from ..services import updates as update_svc

router = APIRouter()


@router.get("/api/update/status")
def update_status(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    # An update whose recreation never happened leaves this container running
    # with the flag still set; catch it here rather than spinning forever.
    update_svc.reap_stalled_update(db)
    return {
        "available": get_setting(db, "update_available", "0") == "1",
        "in_progress": get_setting(db, "update_in_progress", "0") == "1",
        "last_checked": get_setting(db, "update_last_checked", ""),
        "last_result": update_svc.current_result(db),
        # Where updates come from and what's running now.
        "channel": config.BEACON_TAG,
        "image": update_svc.image_ref(),
        "version": update_svc.version_label(),
        # Locally built images are replaced by the published ones on Apply; the
        # UI warns rather than letting that happen unannounced.
        "dev_build": update_svc.is_dev_build(),
    }


@router.post("/api/update/check")
def trigger_check(user: User = Depends(require_admin)):
    """Force an immediate update check (runs synchronously, returns result)."""
    available = update_svc.check_for_updates()
    return {"available": available}


@router.post("/api/update/dismiss")
def dismiss_result(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Clear the last update outcome. Successes expire on their own; failures
    stay until the admin has dealt with them, so this is how they go away."""
    update_svc.clear_result(db)
    return {"cleared": True}


@router.post("/api/update/apply")
def apply_update(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Pull new images and recreate containers. Returns immediately; watch status."""
    if get_setting(db, "update_in_progress", "0") == "1":
        return JSONResponse({"error": "Update already in progress"}, status_code=409)
    threading.Thread(target=update_svc.run_update, daemon=True, name="update-apply").start()
    return {"started": True}
