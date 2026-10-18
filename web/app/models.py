"""Database models: User, Setting, Image, CatalogEntry, BootEvent, SeenClient,
LoginLockout."""
from datetime import datetime, timezone

from sqlalchemy import String, Integer, DateTime, Text, Float
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # "admin" or "user".
    role: Mapped[str] = mapped_column(String(16), default="user")
    # Bumped whenever the password changes, so existing signed sessions (which
    # carry the epoch they were minted with) stop authenticating — a password
    # reset then actually logs a stolen/old session out. See deps.current_user.
    session_epoch: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    # TOTP second factor. totp_secret is a base32 seed; totp_enabled gates the
    # login flow. A user with totp_enabled=0 has no second factor regardless of
    # whether a secret is stored.
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    totp_enabled: Mapped[int] = mapped_column(Integer, default=0)
    # Opaque Bearer token for API / scripted access. Generated on demand; NULL
    # means no token has been issued. Unique so the lookup is an index scan.
    api_token: Mapped[str | None] = mapped_column(String(128), nullable=True,
                                                   unique=True, default=None)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class LoginLockout(Base):
    """Persistent per-IP failed-login state (survives container restarts).

    Keyed by the proxy-aware client IP string. fail_count and last_fail are
    only used when there is no active lockout (locked_until in the future);
    once the lockout expires both are reset on the next successful login.
    All times are Unix epoch floats (wall clock, not monotonic).
    """
    __tablename__ = "login_lockouts"

    ip: Mapped[str] = mapped_column(String(64), primary_key=True)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    # Wall-clock Unix timestamp when the lockout expires (NULL = no active lockout).
    locked_until: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    last_fail: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)


class Setting(Base):
    """Simple key/value store for mutable server configuration."""
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class BootEvent(Base):
    """One record per time a client selected an image to boot.

    Written by the /track/{image_id} endpoint, which the boot menu pings (best
    effort) when a client picks an OS. Powers the dashboard's all-time "clients
    served" and "top deployed images" stats -- nginx serves the actual boot
    files and the web app never sees those requests, so this is how we count
    deployments.
    """
    __tablename__ = "boot_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    image_id: Mapped[int] = mapped_column(Integer, index=True)
    # Snapshot of the image name so stats survive the image being deleted.
    image_name: Mapped[str] = mapped_column(String(128), default="")
    mac: Mapped[str] = mapped_column(String(32), default="", index=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class SeenClient(Base):
    """The last time each client loaded the boot menu, one row per MAC.

    Written by /track/menu, which the menu pings once when it loads -- before
    anyone picks an image. With an external DHCP server dnsmasq's log never sees
    the client, so this is how a client that only reaches the menu still shows on
    the dashboard. Kept apart from BootEvent so it never counts as a deployment,
    and one row per MAC so it can't grow without bound.
    """
    __tablename__ = "seen_clients"

    mac: Mapped[str] = mapped_column(String(32), primary_key=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class Image(Base):
    """An uploaded OS image (ISO) and its extracted boot files."""
    __tablename__ = "images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    filename: Mapped[str] = mapped_column(String(255))          # ISO file on disk
    os_family: Mapped[str] = mapped_column(String(32), default="linux")  # linux|windows|xcpng
    # Extraction lifecycle: pending|processing|ready|error|needs_reprocess
    # (needs_reprocess is set at startup when a ready image's extracted files
    # are gone -- see services.images.reconcile_statuses)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    message: Mapped[str] = mapped_column(Text, default="")      # error/info detail
    kernel_path: Mapped[str] = mapped_column(String(255), default="")   # rel to bootroot
    initrd_path: Mapped[str] = mapped_column(String(255), default="")
    boot_args: Mapped[str] = mapped_column(Text, default="")    # extra kernel cmdline
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    # Display ordering in the boot menu (NULL = sort after explicit positions,
    # then alphabetically). Only one image should have is_default=1 at a time;
    # the first-run render and ipxe.render() both treat it as the default choice.
    display_order: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    is_default: Mapped[int] = mapped_column(Integer, default=0)
    # How the image was made: "iso" (uploaded ISO) or "netboot" (a catalog
    # distro's netboot archive; `filename` is then the small archive, and the
    # fields below say where it came from so Reprocess can rebuild it).
    source: Mapped[str] = mapped_column(String(16), default="iso")
    # "installer" (netboot archive) or "live" (live desktop: kernel, initrd and
    # root filesystem pulled from the distro's live ISO, which is then deleted).
    variant: Mapped[str] = mapped_column(String(16), default="installer")
    catalog_id: Mapped[str] = mapped_column(String(32), default="")
    release: Mapped[str] = mapped_column(String(64), default="")
    mirror: Mapped[str] = mapped_column(String(255), default="")
    # Archive download URL; empty when the admin uploaded the archive instead.
    source_url: Mapped[str] = mapped_column(String(512), default="")
    # Where the files came from: "mirror" (resolved from the catalog), "url"
    # (pasted) or "upload"; empty for images added before this was recorded.
    # Only "mirror" images are followed by Track latest (services.track_latest).
    origin: Mapped[str] = mapped_column(String(16), default="")
    # A live image's release (its `release` field holds the desktop), so the same
    # release can be looked up again for a newer build.
    live_release: Mapped[str] = mapped_column(String(64), default="")
    # The build the boot files were downloaded from (netboot.build_stamp).
    build: Mapped[str] = mapped_column(Text, default="")


class CatalogEntry(Base):
    """A netboot catalog entry added by an admin, beside the built-in ones.

    `data` is the entry as JSON, in the same format as netboot_catalog.json
    (validated by services.netboot.validate_entry). Kept in the database so a
    backup carries it.
    """
    __tablename__ = "catalog_entries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    data: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
