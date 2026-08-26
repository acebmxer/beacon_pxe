"""Restore the database from a backup downloaded via GET /api/backup.

That backup is the SQLite file itself, so restoring is fundamentally a file
swap. Three things make it more than `cp`:

1. **Validation.** The uploaded file becomes the authentication database. A
   truncated download, an unrelated .db, or a backup whose users table holds no
   admin would each leave Beacon unable to log anyone in and no way back through
   the UI. Nothing is touched until the file passes every check.

2. **Host identity.** Most settings describe how the admin wants Beacon to
   behave and should come back verbatim. Two describe the *machine*:
   `server_ip` and `boot_interface`. bootstrap regenerates dnsmasq.conf from
   them on the next start, so a backup taken on other hardware means dnsmasq
   binds an interface that doesn't exist, or hands clients a next-server that
   isn't this box -- PXE breaks with nothing pointing at the cause. Both are
   checked against the host (services.hostinfo) and left out of the restore when
   they don't describe it.

3. **Restart.** SQLAlchemy's pool holds open handles to the live database file.
   Swapping the file underneath them leaves in-flight requests writing to an
   unlinked inode, where the writes silently vanish. Restarting the container
   makes startup the only thing that opens the restored file -- and startup
   already does the right work: _migrate() adds columns an older backup lacks,
   reconcile_statuses() flags images whose extracted files are gone, and
   dnsmasq/ipxe render fresh config from the restored rows.

The flow is upload -> stage -> preview -> apply, so the admin sees what would
change and what would be rejected before committing. The staged file sits beside
the live database in DATA_DIR so the final swap is an atomic os.replace within a
single filesystem.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..store import all_settings
from . import hostinfo

log = logging.getLogger(__name__)

# Uploaded file awaiting confirmation, and the safety copy of the database it
# replaces. Both live in DATA_DIR: same filesystem as the live DB (so the swap
# is atomic) and a host bind mount (so the admin can recover with `cp` if the
# restored database turns out to be one they can't log into).
STAGED_PATH = config.DATA_DIR / "restore-staged.db"
PRE_RESTORE_PATH = config.DATA_DIR / "pxe.db.pre-restore"

# Every SQLite file starts with this.
_MAGIC = b"SQLite format 3\x00"

# Tables a file must have to plausibly be a Beacon backup.
_REQUIRED_TABLES = {"users", "settings", "images"}

# Refuse anything larger than this. The database is settings and metadata --
# even a heavily used instance is a few MB -- so a file this size is a mistake,
# and streaming it to disk unbounded would be a way to fill the data volume.
MAX_UPLOAD_BYTES = 256 * 1024 * 1024

# Settings that describe the machine rather than the admin's preferences, with
# the host lookup that decides whether a backup's value belongs to this host.
# The lookups are wrapped rather than referenced directly so they resolve
# through the module at call time — otherwise this dict would capture the
# function objects at import and ignore any later patching.
HOST_KEYS = {
    "server_ip": (
        lambda: hostinfo.local_ipv4s(),
        "an address configured on this host",
    ),
    "boot_interface": (
        lambda: hostinfo.interfaces(),
        "a network interface on this host",
    ),
}

# The rest of the network configuration. These aren't checkable against the host
# on their own, but they only make sense relative to server_ip -- so when that is
# rejected, the whole block is flagged for review rather than silently applied.
NETWORK_KEYS = (
    "dhcp_mode", "dhcp_range_start", "dhcp_range_end",
    "dhcp_subnet_mask", "dhcp_gateway", "dhcp_dns",
)

# Update bookkeeping describes the deployment doing the restoring, not the one
# that was backed up: which digest is deployed, whether a check found something,
# whether an update is mid-flight. Restoring `update_in_progress=1` from a backup
# taken during an update would wedge the UI on "Update in progress" forever, and
# a stale digest would advertise an update that is already installed. Dropped
# from the staged file so the live values stand and the daily check re-derives.
_DEPLOYMENT_KEY_PREFIX = "update_"

# Where apply() leaves its outcome for the settings page to show after restart.
_NOTICE_KEY = "restore_notice"
_NOTICE_LEVEL_KEY = "restore_notice_level"
_NOTICE_AT_KEY = "restore_notice_at"

_WEB_CONTAINER = "beacon_web"


class RestoreError(Exception):
    """The uploaded file can't be restored; the message is shown to the admin."""


# ---------------------------------------------------------------------------
# Reading a candidate backup
# ---------------------------------------------------------------------------
def _connect_ro(path: Path) -> sqlite3.Connection:
    """Open a database file read-only, so inspecting can't modify it."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def inspect_backup(path: Path) -> dict:
    """Validate a candidate backup and read what's in it.

    Raises RestoreError with an admin-facing message for anything that would
    make the file unusable as Beacon's database.
    """
    try:
        size = path.stat().st_size
    except OSError:
        raise RestoreError("The uploaded file could not be read.")

    if size == 0:
        raise RestoreError("The uploaded file is empty.")

    with open(path, "rb") as fh:
        if fh.read(len(_MAGIC)) != _MAGIC:
            raise RestoreError(
                "That is not a SQLite database. Upload the beacon-backup.db "
                "file downloaded from this page, not an archive or an export.")

    try:
        conn = _connect_ro(path)
    except sqlite3.Error:
        raise RestoreError("The file is not a readable SQLite database.")

    try:
        # A truncated or partially written download opens fine and only fails
        # later, on the first query that touches a missing page.
        check = conn.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0] != "ok":
            raise RestoreError(
                "The database failed SQLite's integrity check — the file is "
                "corrupt or was truncated in transfer. Download the backup "
                "again.")

        present = _tables(conn)
        missing = _REQUIRED_TABLES - present
        if missing:
            raise RestoreError(
                "This does not look like a Beacon backup: it has no "
                + ", ".join(sorted(missing)) + " table.")

        user_cols = _columns(conn, "users")
        if "username" not in user_cols or "role" not in user_cols:
            raise RestoreError(
                "The users table is not in Beacon's format — this backup came "
                "from a different application.")

        admins = [r[0] for r in conn.execute(
            "SELECT username FROM users WHERE role='admin' ORDER BY username")]
        if not admins:
            raise RestoreError(
                "This backup contains no admin account, so restoring it would "
                "lock you out of Beacon permanently.")

        usernames = [r[0] for r in conn.execute(
            "SELECT username FROM users ORDER BY username")]
        settings = {k: v for k, v in conn.execute(
            "SELECT key, value FROM settings")}
        images = [
            {"name": r[0], "filename": r[1], "status": r[2]}
            for r in conn.execute(
                "SELECT name, filename, status FROM images ORDER BY name")
        ]
        events = 0
        if "boot_events" in present:
            events = conn.execute("SELECT COUNT(*) FROM boot_events").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise RestoreError(f"The database could not be read: {exc}")
    finally:
        conn.close()

    return {
        "size_bytes": size,
        "usernames": usernames,
        "admins": admins,
        "settings": settings,
        "images": images,
        "boot_events": events,
    }


def token_for(path: Path) -> str:
    """Content hash of a staged file, so apply() can confirm it's what was
    previewed rather than a second upload that landed in between."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Deciding what a restore would do
# ---------------------------------------------------------------------------
def _host_decision(key: str, current: str, incoming: str) -> dict:
    """Whether a host-identity setting from the backup applies to this host."""
    lookup, description = HOST_KEYS[key]
    known = lookup()

    row = {"key": key, "current": current, "incoming": incoming}

    if incoming == current:
        return {**row, "action": "restore", "reason": "unchanged"}

    # A backup that never had the value set would otherwise wipe a working one.
    if not incoming:
        return {**row, "action": "keep",
                "reason": "the backup has no value for this setting"}

    if not known:
        # No host /proc mounted (or unreadable). Trusting the backup preserves
        # the pre-validation behaviour; the warning says why it wasn't checked.
        return {**row, "action": "restore", "reason": "could not be verified",
                "unverified": True}

    if incoming in known:
        return {**row, "action": "restore",
                "reason": f"confirmed as {description}"}

    return {**row, "action": "keep",
            "reason": f"“{incoming}” is not {description}"}


def build_preview(db, path: Path) -> dict:
    """What restoring `path` over the live database would do.

    Pure inspection: nothing is written. The same decisions are recomputed at
    apply() time against the database as it stands then.
    """
    backup = inspect_backup(path)
    current = all_settings(db)
    incoming = backup["settings"]

    warnings: list[str] = []
    host_rows: list[dict] = []
    rejected_host = False

    for key in HOST_KEYS:
        row = _host_decision(key, current.get(key, ""), incoming.get(key, ""))
        host_rows.append(row)
        if row["action"] == "keep":
            rejected_host = True
            warnings.append(
                f"{_label(key)} will keep this host's value "
                f"“{row['current']}” — {row['reason']}.")
        elif row.get("unverified"):
            warnings.append(
                f"{_label(key)} could not be checked against this host "
                "(the host's /proc isn't mounted), so the backup's value "
                f"“{row['incoming']}” will be restored as-is.")

    # Ordinary settings: everything that isn't host identity or deployment
    # bookkeeping, shown only where the value actually changes.
    setting_rows = []
    for key in sorted(set(current) | set(incoming)):
        if key in HOST_KEYS or key.startswith(_DEPLOYMENT_KEY_PREFIX):
            continue
        if key.startswith("restore_notice"):
            continue
        before, after = current.get(key, ""), incoming.get(key, "")
        if before != after:
            setting_rows.append({"key": key, "current": before,
                                 "incoming": after, "action": "restore"})

    # A rejected host setting means this backup was taken somewhere else, so the
    # rest of the network block describes that other network too. It can't be
    # checked the way an IP or an interface name can, so say so rather than
    # either applying it silently or discarding values that may well be right.
    if rejected_host and any(incoming.get(k) for k in NETWORK_KEYS):
        warnings.append(
            "The backup's DHCP range, gateway and DNS will be restored, but "
            "they describe the network the backup was taken on. Review the "
            "network settings after the restore — dnsmasq is regenerated from "
            "them as soon as Beacon restarts.")

    if backup["images"]:
        warnings.append(
            f"{len(backup['images'])} image(s) will be listed again. Any whose "
            "extracted files are missing are marked “needs reprocess” at "
            "startup and stay out of the boot menu until reprocessed.")

    current_users = set(_current_usernames(db))
    incoming_users = set(backup["usernames"])

    return {
        "size_bytes": backup["size_bytes"],
        "summary": {
            "users": len(backup["usernames"]),
            "admins": len(backup["admins"]),
            "images": len(backup["images"]),
            "settings": len(incoming),
            "boot_events": backup["boot_events"],
        },
        "host": host_rows,
        "settings": setting_rows,
        "users": {
            "added": sorted(incoming_users - current_users),
            "removed": sorted(current_users - incoming_users),
            "kept": sorted(incoming_users & current_users),
            "admins": backup["admins"],
        },
        "images": backup["images"],
        "warnings": warnings,
    }


def _label(key: str) -> str:
    return {"server_ip": "Server IP",
            "boot_interface": "Boot interface"}.get(key, key)


def _current_usernames(db) -> list[str]:
    from ..models import User
    return [u.username for u in db.query(User).all()]


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------
def _rewrite_staged(path: Path, overrides: dict[str, str], notice: str,
                    level: str) -> None:
    """Fix up the staged database in place, before it becomes the live one.

    Doing this here rather than after the swap means the database is already
    correct the moment the restarted process opens it — bootstrap renders
    dnsmasq from the corrected values on that first start, so there is never a
    moment where the wrong interface or IP is live.
    """
    conn = sqlite3.connect(path)
    try:
        with conn:
            for key, value in overrides.items():
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value))
            conn.execute("DELETE FROM settings WHERE key LIKE ?",
                         (_DEPLOYMENT_KEY_PREFIX + "%",))
            for key, value in (
                (_NOTICE_KEY, notice),
                (_NOTICE_LEVEL_KEY, level),
                (_NOTICE_AT_KEY, datetime.now(timezone.utc).isoformat()),
            ):
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value))
    finally:
        conn.close()


def docker_available() -> bool:
    """Whether the daemon is reachable, checked before anything is swapped.

    Without it the swap still works but nothing can restart the container, and
    this process would keep serving from the replaced file's unlinked inode.
    """
    try:
        proc = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def restart_self() -> None:
    """Ask the daemon to restart this container.

    `docker restart` on the container it is called from works: the daemon owns
    the operation, so the CLI process dying along with the container doesn't
    cancel it. (`docker compose up -d` does *not* survive that, which is why
    services.updates hands recreation to a throwaway container. A restart needs
    none of that.)

    Spawned without waiting, and called from a background task so the response
    has already reached the browser when the container goes down.
    """
    try:
        subprocess.Popen(["docker", "restart", _WEB_CONTAINER],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log.error("Restore: could not restart %s: %s", _WEB_CONTAINER, exc)


def apply(db, path: Path) -> dict:
    """Swap the staged backup in as the live database.

    Returns a summary for the response. The caller is responsible for triggering
    restart_self() after the response is sent.
    """
    preview = build_preview(db, path)

    overrides = {row["key"]: row["current"]
                 for row in preview["host"] if row["action"] == "keep"}

    kept = [f"{_label(k)} “{v}”" for k, v in overrides.items()]
    if kept:
        notice = ("Database restored from backup. This host's "
                  + " and ".join(kept)
                  + " were kept because the backup's values do not describe "
                    "this machine. Check the network settings below.")
        level = "warn"
    else:
        notice = "Database restored from backup."
        level = "ok"

    _rewrite_staged(path, overrides, notice, level)

    # Safety copy of what is being replaced. Overwrites the previous one: it
    # exists to undo *this* restore, and keeping a chain of them on the data
    # volume would be a slow leak nobody prunes.
    shutil.copy2(config.DB_PATH, PRE_RESTORE_PATH)

    # Atomic within DATA_DIR: either the old file or the new one is at DB_PATH,
    # never a partial copy, even if the machine loses power here.
    os.replace(path, config.DB_PATH)

    log.warning("Database restored from backup (kept host settings: %s)",
                ", ".join(overrides) or "none")
    return {"notice": notice, "level": level, "kept": overrides,
            "pre_restore": str(PRE_RESTORE_PATH)}


def clear_staged() -> None:
    """Drop an uploaded file that was previewed but not applied."""
    try:
        STAGED_PATH.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Post-restart notice
# ---------------------------------------------------------------------------
def current_notice(db) -> dict:
    """The outcome of the last restore, for the settings page to show."""
    from ..store import get_setting
    text = get_setting(db, _NOTICE_KEY, "")
    if not text:
        return {}
    return {
        "text": text,
        "level": get_setting(db, _NOTICE_LEVEL_KEY, "ok"),
        "at": get_setting(db, _NOTICE_AT_KEY, ""),
    }


def clear_notice(db) -> None:
    from ..store import set_setting
    for key in (_NOTICE_KEY, _NOTICE_LEVEL_KEY, _NOTICE_AT_KEY):
        set_setting(db, key, "")
