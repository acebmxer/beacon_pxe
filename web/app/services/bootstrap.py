"""First-start bootstrap: seed settings and create the default admin account."""
import logging
import secrets
import shutil
import string
from pathlib import Path

from sqlalchemy import select

from ..db import SessionLocal
from ..models import User, Setting
from ..auth import hash_password
from .. import config
from . import dnsmasq, images, ipxe

log = logging.getLogger("beacon.bootstrap")

# wimboot is baked into the web image (see web/Dockerfile) and copied into the
# bootroot so nginx serves it at /wimboot for the Windows boot chain.
_WIMBOOT_SRC = Path("/usr/local/share/wimboot")


def _stage_wimboot() -> None:
    """Copy wimboot into the bootroot if it isn't already there (or is stale)."""
    dest = config.BOOTROOT_DIR / "wimboot"
    if not _WIMBOOT_SRC.exists():
        log.warning("wimboot not bundled in image; Windows boot will not work")
        return
    if dest.exists() and dest.stat().st_size == _WIMBOOT_SRC.stat().st_size:
        return
    shutil.copy2(_WIMBOOT_SRC, dest)
    log.info("Staged wimboot into bootroot")


def _gen_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def run():
    """Idempotent. Safe to call on every startup."""
    db = SessionLocal()
    try:
        # Seed default settings that aren't present yet.
        existing = {s.key for s in db.query(Setting).all()}
        for key, value in config.DEFAULTS.items():
            if key not in existing:
                db.add(Setting(key=key, value=value))
        db.commit()

        # Create the default admin if there are no users at all.
        any_user = db.execute(select(User.id).limit(1)).first()
        if any_user is None:
            password = config.ADMIN_PASSWORD or _gen_password()
            generated = not config.ADMIN_PASSWORD
            admin = User(
                username=config.ADMIN_USER,
                password_hash=hash_password(password),
                role="admin",
            )
            db.add(admin)
            db.commit()

            banner = "=" * 60
            log.warning("\n%s\n Beacon — initial admin account created\n"
                        "   username: %s\n   admin password: %s\n%s",
                        banner, config.ADMIN_USER, password, banner)
            if not generated:
                log.warning("(password came from ADMIN_PASSWORD in .env)")

        # Make wimboot available for Windows images.
        _stage_wimboot()

        # Drop images whose extracted files no longer exist out of "ready", so
        # the menu rendered below can't offer a boot that is guaranteed to fail.
        images.reconcile_statuses(db)

        # Render initial boot configs so the stack is bootable immediately.
        dnsmasq.render(db)
        ipxe.render(db)
    finally:
        db.close()
