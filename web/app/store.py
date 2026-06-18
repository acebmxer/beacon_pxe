"""Helpers for the Setting key/value table."""
from sqlalchemy.orm import Session

from .models import Setting
from . import config


def get_setting(db: Session, key: str, default: str | None = None) -> str:
    row = db.get(Setting, key)
    if row is not None:
        return row.value
    if default is not None:
        return default
    return config.DEFAULTS.get(key, "")


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()


def all_settings(db: Session) -> dict[str, str]:
    """Defaults overlaid with any stored values."""
    merged = dict(config.DEFAULTS)
    for row in db.query(Setting).all():
        merged[row.key] = row.value
    return merged
