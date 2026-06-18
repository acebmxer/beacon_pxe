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


class Image(Base):
    """An uploaded OS image (ISO) and its extracted boot files."""
    __tablename__ = "images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    filename: Mapped[str] = mapped_column(String(255))          # ISO file on disk
    os_family: Mapped[str] = mapped_column(String(32), default="linux")  # linux|windows
    # Extraction lifecycle: pending|ready|error|unsupported
    status: Mapped[str] = mapped_column(String(16), default="pending")
    message: Mapped[str] = mapped_column(Text, default="")      # error/info detail
    kernel_path: Mapped[str] = mapped_column(String(255), default="")   # rel to bootroot
    initrd_path: Mapped[str] = mapped_column(String(255), default="")
    boot_args: Mapped[str] = mapped_column(Text, default="")    # extra kernel cmdline
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
