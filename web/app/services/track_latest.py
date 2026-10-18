"""Track latest: add a newer build of a catalog image's release as a new image.

When a distro publishes a newer build of a release an image was added from
(Ubuntu 24.04.2 -> 24.04.3, a respun live ISO, this month's Arch), Beacon
downloads it into a new image beside the old one. Images already built never
change, and a new release (26.04) is never picked up here: it is added as its
own image.

One switch under Settings -> Netboot catalog covers every catalog image (off by
default); when on, the check runs once a day. Check now runs it at any time.

Only images whose files came from the mirror are followed: a pasted URL or an
uploaded archive names one fixed file, with nothing newer to look for. Images of
one distro, desktop, release and mirror are one group, and only the newest image
of a group is compared with the mirror.
"""
import json
import logging
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Image
from ..store import get_setting, set_setting
from . import images as image_svc
from . import netboot

log = logging.getLogger("beacon.track_latest")

ENABLED_KEY = "netboot_track_latest"
CHECKED_KEY = "netboot_track_checked"   # when the last check finished (ISO, UTC)
RESULT_KEY = "netboot_track_result"     # what it found, shown in Settings
# The last build added for each group, so a new image the admin deletes is not
# added again at the next check; only a build newer than it is.
_SEEN_KEY = "netboot_track_seen"

_CHECK_INTERVAL = 86400  # 24 hours
_FIRST_CHECK_DELAY = 600  # let the app settle (and the update checker go first)
_RESULT_MAX = 2000

# One check at a time: the daily check and Check now must not both add the
# same build.
_running = threading.Lock()


def enabled(db) -> bool:
    return get_setting(db, ENABLED_KEY, "0") == "1"


def is_running() -> bool:
    return _running.locked()


def status(db) -> dict:
    """For Settings: whether a check is running, when the last one finished
    (as "YYYY-MM-DD HH:MM UTC") and what it found."""
    checked = get_setting(db, CHECKED_KEY, "")
    try:
        checked = datetime.fromisoformat(checked).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        checked = ""
    return {"running": is_running(), "checked": checked,
            "result": get_setting(db, RESULT_KEY, "")}


def _group(img: Image) -> str:
    return "|".join((img.catalog_id, img.variant, img.release, img.live_release, img.mirror))


def _newest_per_group(db) -> list[Image]:
    rows = db.execute(
        select(Image).where(Image.source == "netboot", Image.origin == "mirror")
        .order_by(Image.id)).scalars().all()
    newest = {}
    for img in rows:
        newest[_group(img)] = img
    return list(newest.values())


def _build_name(name: str, day: str) -> str:
    """The old image's name with the new build's date: "Ubuntu 24.04 Live
    Kubuntu (build 2026-10-15)". A date from an earlier build is replaced."""
    base = name
    if base.endswith(")") and " (build " in base:
        base = base[:base.rindex(" (build ")]
    suffix = f" (build {day})"
    return base[:128 - len(suffix)] + suffix


def _check_one(db, img: Image, seen: dict) -> tuple[Image | None, str | None]:
    """(new image, problem): the newer build added for img's group, if any, or
    why its group could not be checked."""
    if img.status != "ready":
        # A build being made or one that failed is left to the admin; checking
        # past it would add another image at every check.
        if img.status in ("error", "needs_reprocess"):
            return None, (f"{img.name}: not ready, so its release was skipped; "
                          "Reprocess or delete it on the Images page")
        return None, None
    entry = netboot.catalog_entry(img.catalog_id, db)
    if entry is None:
        return None, f"{img.name}: its catalog entry no longer exists"
    try:
        plan = netboot.plan_image(entry, img.variant, img.release, img.live_release,
                                  img.release if img.variant == "live" else "", img.mirror)
        stamp, day = netboot.remote_build(netboot.stamp_urls(
            entry, plan["mirror"], plan["release"], plan["source_url"]))
    except netboot.NetbootError as e:
        return None, f"{img.name}: {e}"
    group = _group(img)
    if not img.build:
        # No build was recorded when it was made, so what it holds is not known:
        # the mirror's build now is taken as its own and only a later one is
        # added, rather than a copy at every check.
        img.build = stamp
        db.commit()
        return None, None
    if stamp in (img.build, seen.get(group)):
        return None, None
    new = Image(
        # name is written verbatim into boot.ipxe; it comes from an existing
        # image's name and a date, so carries no control characters.
        name=_build_name(img.name, day or datetime.now(timezone.utc).date().isoformat()),
        filename=plan["filename"], status="pending",
        source="netboot", variant=plan["variant"], catalog_id=img.catalog_id,
        release=plan["release"], live_release=plan["live_release"],
        mirror=plan["mirror"], source_url=plan["source_url"], origin="mirror",
    )
    db.add(new)
    db.commit()
    seen[group] = stamp
    log.info("Newer build of %s found; added image %d (%s)", img.name, new.id, new.name)
    return new, None


def run_check(db) -> list[int]:
    """Check every followed image's group once; returns the ids of the images
    added (queued, not yet built). Writes the outcome for Settings."""
    try:
        seen = json.loads(get_setting(db, _SEEN_KEY, "{}") or "{}")
    except ValueError:
        seen = {}
    added, problems = [], []
    groups = _newest_per_group(db)
    for img in groups:
        new, problem = _check_one(db, img, seen)
        if new is not None:
            added.append(new)
        if problem:
            problems.append(problem)
    # Forget groups that no longer have an image, so the list cannot grow.
    live_groups = {_group(img) for img in groups} | {_group(img) for img in added}
    set_setting(db, _SEEN_KEY, json.dumps({k: v for k, v in seen.items() if k in live_groups}))

    if not groups:
        result = ("No catalog images to follow: only images added from the mirror "
                  "are followed, and not ones added before Beacon 0.8.0.")
    elif added:
        result = (f"Added {len(added)} newer build{'s' if len(added) != 1 else ''}: "
                  + ", ".join(img.name for img in added) + ".")
    else:
        result = f"No newer builds ({len(groups)} release{'s' if len(groups) != 1 else ''} checked)."
    if problems:
        result += " Could not check: " + "; ".join(problems) + "."
    set_setting(db, RESULT_KEY, result[:_RESULT_MAX])
    set_setting(db, CHECKED_KEY, datetime.now(timezone.utc).isoformat())
    return [img.id for img in added]


def _check_and_build() -> None:
    """One check, then build what it added, one image at a time (each can be a
    multi-GB download). The caller holds _running; it is released once the
    check is done, as a group whose newest image is still being built is not
    checked again."""
    added = []
    try:
        db = SessionLocal()
        try:
            added = run_check(db)
        finally:
            db.close()
    except Exception:
        log.exception("Track latest check failed")
    finally:
        _running.release()
    for image_id in added:
        try:
            image_svc.process_image(image_id)
        except Exception:
            log.exception("Building image %d failed", image_id)


def start_check() -> bool:
    """Check now, from Settings: runs in the background. False if a check is
    already running. The lock is taken here, so the page shown next already
    says a check is running."""
    if not _running.acquire(blocking=False):
        return False
    threading.Thread(target=_check_and_build, daemon=True, name="track-latest-now").start()
    return True


def _check_loop() -> None:
    time.sleep(_FIRST_CHECK_DELAY)
    while True:
        db = SessionLocal()
        try:
            on = enabled(db)
        finally:
            db.close()
        if on and _running.acquire(blocking=False):
            _check_and_build()
        time.sleep(_CHECK_INTERVAL)


def start_background_checker() -> None:
    threading.Thread(target=_check_loop, daemon=True, name="track-latest").start()
