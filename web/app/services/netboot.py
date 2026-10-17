"""Netboot-archive images: a distro's own installer kernel + initrd.

The alternative to an ISO upload. The catalog (netboot_catalog.json) names a
distro's netboot archive and the kernel arguments that point its installer at a
package mirror. Beacon keeps only the kernel and initrd from the archive; the
client's installer pulls everything else from the mirror.
"""
import json
import logging
import re
import secrets
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("beacon.netboot")

_CATALOG_PATH = Path(__file__).resolve().parent.parent / "netboot_catalog.json"

# Size caps: they only stop a wrong URL from filling the disk. A netboot archive
# is tens of MB; a live ISO a few GB.
ARCHIVE_MAX_BYTES = 1024 * 1024 * 1024
LIVE_ISO_MAX_BYTES = 12 * 1024 * 1024 * 1024

# A release is spliced into a URL and a kernel command line, so it is limited to
# what distro release names actually use.
_RELEASE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class NetbootError(Exception):
    """A user-facing problem with a netboot image request."""


def load_catalog() -> list[dict]:
    """The built-in distros, or [] if the file is absent/broken.

    A broken catalog must not take the images page down with it; ISO upload
    still works without it.
    """
    try:
        distros = json.loads(_CATALOG_PATH.read_text()).get("distros", [])
    except (OSError, ValueError):
        return []
    return [d for d in distros if d.get("id") and d.get("archive_url")]


def catalog_entry(distro_id: str) -> dict | None:
    for d in load_catalog():
        if d["id"] == distro_id:
            return d
    return None


def validate_release(release: str) -> str:
    release = release.strip()
    if not _RELEASE_RE.fullmatch(release):
        raise NetbootError("Release may only contain letters, digits, '.', "
                           "'_' and '-' (for example: trixie).")
    return release


def validate_mirror(mirror: str) -> str:
    """Normalise a mirror base URL (no trailing slash); http or https only.

    The value ends up in a URL we fetch and in the client's kernel command line,
    so whitespace and anything but a plain host[:port]/path is rejected.
    """
    mirror = mirror.strip().rstrip("/")
    parsed = urlparse(mirror)
    if (parsed.scheme not in ("http", "https") or not parsed.netloc
            or re.search(r"[^A-Za-z0-9._~:/@%+-]", mirror[len(parsed.scheme) + 3:])
            or parsed.query or parsed.fragment or parsed.params):
        raise NetbootError("Mirror must be an http:// or https:// URL, for "
                           "example https://deb.debian.org/debian.")
    return mirror


def validate_source_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    if (parsed.scheme not in ("http", "https") or not parsed.netloc
            or re.search(r"\s", url)):
        raise NetbootError("Archive URL must be an http:// or https:// URL.")
    return url


def archive_url(entry: dict, mirror: str, release: str) -> str:
    return entry["archive_url"].format(mirror=mirror, version=release)


def boot_args(entry: dict, mirror: str, release: str) -> str:
    parsed = urlparse(mirror)
    return entry.get("boot_args", "").format(
        mirror=mirror, version=release, mirror_proto=parsed.scheme,
        mirror_host=parsed.netloc, mirror_path=parsed.path or "/")


def new_archive_name(distro_id: str, release: str, ext: str = ".tar.gz") -> str:
    """A unique file name, so two images of one release never share a download."""
    return f"netboot-{distro_id}-{release}-{secrets.token_hex(4)}{ext}"


def live_entry(entry: dict) -> dict | None:
    """The live-desktop part of a catalog entry, if the distro has one."""
    return entry.get("live") or None


def validate_flavour(live: dict, flavour: str) -> str:
    flavour = flavour.strip()
    if flavour not in {f["id"] for f in live.get("flavours", [])}:
        raise NetbootError("Choose one of the listed desktops.")
    return flavour


def resolve_live_iso_url(live: dict, base_url: str, flavour: str) -> str:
    """Find the current live ISO for a flavour in the mirror's directory listing.

    The file name carries the point release (debian-live-13.7.0-...), which
    changes, so it is read from the listing rather than pinned in the catalog.
    """
    base_url = base_url.rstrip("/")
    req = urllib.request.Request(base_url + "/", headers={"User-Agent": "beacon-pxe"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            listing = resp.read(4 * 1024 * 1024).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise NetbootError(f"Could not read {base_url}/ ({getattr(exc, 'reason', exc)}). "
                           "Check the mirror and the Beacon host's internet access.")
    rx = re.compile(live["iso_regex"].format(flavour=re.escape(flavour)))
    names = sorted({m.group(0) for m in rx.finditer(listing)})
    if not names:
        raise NetbootError(f"No live ISO for '{flavour}' was found at {base_url}/.")
    return f"{base_url}/{names[-1]}"


def live_boot_args(live: dict, image_id: int) -> str:
    return live["boot_args"].format(image_id=image_id)


def download(url: str, dest: Path, max_bytes: int = ARCHIVE_MAX_BYTES) -> None:
    """Stream url to dest (via a .part file) with a size cap and timeout.

    Separate from services.drivers._download on purpose: that one is https-only
    and pinned to a hash, while a netboot mirror is often plain http.
    """
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "beacon-pxe"})
    size = 0
    try:
        with urllib.request.urlopen(req, timeout=30) as resp, open(part, "wb") as out:
            while chunk := resp.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise NetbootError("Download exceeded the size limit; "
                                       "refusing to continue.")
                out.write(chunk)
        part.replace(dest)
    except urllib.error.HTTPError as exc:
        raise NetbootError(f"Download failed: the server answered {exc.code} "
                           f"for {url}. Check the release name and mirror.")
    except urllib.error.URLError as exc:
        raise NetbootError(f"Download failed ({getattr(exc, 'reason', exc)}). "
                           "The Beacon host needs internet access, or upload "
                           "the archive instead.")
    except TimeoutError:
        raise NetbootError("Download timed out. Check the Beacon host's "
                           "internet access and try again.")
    finally:
        part.unlink(missing_ok=True)


def extract_boot_files(archive: Path, entry: dict, dest_dir: Path) -> None:
    """Copy just the kernel and initrd out of the archive into dest_dir.

    Only the two named members are read (never extractall), so a hostile archive
    can't write anywhere else.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    wanted = {entry["kernel_member"]: "vmlinuz", entry["initrd_member"]: "initrd"}
    found = set()
    try:
        with tarfile.open(archive) as tar:
            for member in tar:
                name = member.name.removeprefix("./")
                if name in wanted and member.isreg():
                    src = tar.extractfile(member)
                    with open(dest_dir / wanted[name], "wb") as out:
                        while chunk := src.read(1024 * 1024):
                            out.write(chunk)
                    found.add(name)
    except (tarfile.TarError, OSError) as exc:
        raise NetbootError(f"Could not read the archive: {exc}")
    missing = set(wanted) - found
    if missing:
        raise NetbootError("The archive does not contain "
                           f"{', '.join(sorted(missing))}. Is it the right "
                           "distro and release?")
