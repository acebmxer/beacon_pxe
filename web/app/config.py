"""Environment-driven paths and defaults.

All mutable runtime settings (DHCP mode, theme, service toggles, ...) live in the
database (see models.Setting). This module only holds process-level config read
from the environment at startup.
"""
import os
import secrets
from pathlib import Path


def _path(env: str, default: str) -> Path:
    p = Path(os.environ.get(env, default))
    p.mkdir(parents=True, exist_ok=True)
    return p


# Mounted volumes (see docker-compose.yml).
DATA_DIR = _path("DATA_DIR", "./data")
BOOTROOT_DIR = _path("BOOTROOT_DIR", "./bootroot")
TFTP_DIR = _path("TFTP_DIR", "./tftp")
IMAGE_DIR = _path("IMAGE_DIR", "./data/images")
DNSMASQ_DIR = _path("DNSMASQ_DIR", "./dnsmasq")
# Live filesystems extracted from ISOs, exported read-only by the nfs service.
NFS_DIR = _path("NFS_DIR", "./data/nfs")

DB_PATH = DATA_DIR / "pxe.db"
DB_URL = f"sqlite:///{DB_PATH}"

# Initial admin (consumed once, on first start, by services.bootstrap).
ADMIN_USER = os.environ.get("ADMIN_USER", "admin").strip() or "admin"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()

# Session cookie signing key. Generated if not supplied (sessions then reset on
# restart, which is acceptable for a single-node admin tool).
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip() or secrets.token_hex(32)

WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))

# Defaults for first-run / settings, sourced from env so .env can seed them.
DEFAULTS = {
    "server_ip": os.environ.get("SERVER_IP", "").strip(),
    "boot_interface": os.environ.get("BOOT_INTERFACE", "eth0").strip(),
    "dhcp_mode": os.environ.get("DHCP_MODE", "proxy").strip(),
    "dhcp_range_start": os.environ.get("DHCP_RANGE_START", "192.168.1.100").strip(),
    "dhcp_range_end": os.environ.get("DHCP_RANGE_END", "192.168.1.200").strip(),
    "dhcp_subnet_mask": os.environ.get("DHCP_SUBNET_MASK", "255.255.255.0").strip(),
    "dhcp_gateway": os.environ.get("DHCP_GATEWAY", "192.168.1.1").strip(),
    "dhcp_dns": os.environ.get("DHCP_DNS", "192.168.1.1").strip(),
    # Service toggles.
    "svc_dhcp": "1",
    "svc_tftp": "1",
    "svc_http": "1",
    # UI.
    "theme": "dark",
    "menu_title": "Beacon",
    # First-run wizard completion flag.
    "setup_complete": "0",
}
