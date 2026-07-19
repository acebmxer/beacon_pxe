"""Database models: User, Setting, Image."""
from datetime import datetime, timezone

from sqlalchemy import String, Integer, DateTime, Text
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


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
