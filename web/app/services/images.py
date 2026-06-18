"""Image handling: store ISO uploads and extract Linux kernel/initrd.

Extraction uses bsdtar (libarchive) to read files straight out of the ISO9660
image — no privileged loop mount, so it works in an unprivileged container.
"""
import logging
import re
import shutil
import subprocess
from pathlib import Path

from ..db import SessionLocal
from ..models import Image
from .. import config
from . import ipxe

log = logging.getLogger("beacon.images")

# Where extracted kernels/initrds live, relative to BOOTROOT_DIR.
EXTRACT_SUBDIR = "os"

# Candidate (kernel, initrd) path patterns by distro family, checked in order.
# Patterns are matched case-insensitively against the ISO's file listing.
KERNEL_PATTERNS = [
    r"casper/vmlinuz",
    r"live/vmlinuz.*",
    r"images/pxeboot/vmlinuz",
    r"isolinux/vmlinuz.*",
    r"arch/boot/x86_64/vmlinuz.*",
    r"boot/vmlinuz.*",
    r"kernel/vmlinuz",
    r".*/vmlinuz.*",
    r".*/bzimage",
]
INITRD_PATTERNS = [
    r"casper/initrd.*",
    r"live/initrd.*",
    r"images/pxeboot/initrd.*",
    r"isolinux/initrd.*",
    r"arch/boot/x86_64/initramfs.*",
    r"boot/initramfs.*",
    r"boot/initrd.*",
    r"install\.img(;[0-9]+)?",  # XCP-NG / XenServer — root-level, ISO9660 ;1 suffix optional
    r".*/initrd.*",
    r".*/initramfs.*",
]


def iso_path(filename: str) -> Path:
    return config.IMAGE_DIR / filename


def _list_iso(path: Path) -> list[str]:
    out = subprocess.run(
        ["bsdtar", "-tf", str(path)],
        capture_output=True, text=True, check=True,
    )
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _match(entries: list[str], patterns: list[str]) -> str | None:
    for pat in patterns:
        rx = re.compile(pat + r"$", re.IGNORECASE)
        for entry in entries:
            if rx.fullmatch(entry.lstrip("./")):
                return entry
    return None


def _extract_one(iso: Path, member: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as fh:
        subprocess.run(
            ["bsdtar", "-xOf", str(iso), member],
            stdout=fh, check=True,
        )


def _extract_tree(iso: Path, dest: Path) -> None:
    """Unpack the whole ISO into dest so its live filesystem can be NFS-exported.

    Re-extracts cleanly each time so a Retry can't leave a half-written tree.
    """
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(["bsdtar", "-xf", str(iso), "-C", str(dest)], check=True)


def _detect_family(entries: list[str]) -> str:
    """Windows install ISOs carry sources/*.wim and a root-level bootmgr.

    Match precisely: a loose endswith("bootmgr") wrongly flags Ubuntu, whose
    pool contains the 'efibootmgr' package path.
    """
    for e in entries:
        el = e.lower().lstrip("./").rstrip("/")
        base = el.rsplit("/", 1)[-1]
        # Definitive Windows markers.
        if el in ("sources/boot.wim", "sources/install.wim"):
            return "windows"
        # bootmgr / bootmgr.efi at the ISO root (not nested in a package path).
        if base in ("bootmgr", "bootmgr.efi") and "/" not in el:
            return "windows"
    return "linux"


def _netboot_plan(entries: list[str], filename: str, image_id: int) -> tuple[bool, str]:
    """Decide how a live ISO should netboot and return (needs_nfs, kernel cmdline).

    casper (Ubuntu) and live (Debian) images mount their squashfs over NFS so the
    whole ISO never has to fit in client RAM — the old `url=`/`fetch=` methods
    copied the full image into a tmpfs and fell over on anything but huge clients.
    nfsroot points at this image's exported tree; ${server-ip} is set in boot.ipxe.
    Fedora/RHEL already stream their repo over HTTP, so they stay on HTTP.
    """
    nfsroot = f"${{server-ip}}:/nfs/{image_id}"
    iso_url = f"${{boot-url}}/images/{filename}"
    joined = " ".join(e.lower() for e in entries)
    if "casper/" in joined:  # Ubuntu / casper live
        return True, f"boot=casper netboot=nfs nfsroot={nfsroot} ip=dhcp"
    if "live/" in joined:    # Debian live
        return True, f"boot=live netboot=nfs nfsroot={nfsroot} ip=dhcp"
    if "images/pxeboot/" in joined:  # Fedora/RHEL family
        return False, f"inst.repo={iso_url} ip=dhcp"
    if "install.img" in joined:  # XCP-NG / XenServer — extract ISO tree so installer finds PACKAGES/ over NFS
        return True, f"install nfs:{nfsroot}"
    return False, "ip=dhcp"


def process_image(image_id: int) -> None:
    """Run extraction for one image. Intended to run as a background task."""
    db = SessionLocal()
    try:
        img = db.get(Image, image_id)
        if img is None:
            return
        iso = iso_path(img.filename)
        try:
            entries = _list_iso(iso)
        except subprocess.CalledProcessError as e:
            img.status = "error"
            img.message = f"Could not read ISO: {e.stderr or e}"
            db.commit()
            return

        family = _detect_family(entries)
        img.os_family = family
        if family == "windows":
            img.status = "unsupported"
            img.message = ("Windows image stored. Direct PXE boot of Windows ISOs "
                           "is not supported yet (needs wimboot/WinPE). See README.")
            db.commit()
            return

        kernel = _match(entries, KERNEL_PATTERNS)
        initrd = _match(entries, INITRD_PATTERNS)
        if not kernel or not initrd:
            img.status = "error"
            img.message = ("Could not locate kernel/initrd in ISO. You may set "
                           "paths manually after checking the ISO layout.")
            db.commit()
            return

        dest_dir = config.BOOTROOT_DIR / EXTRACT_SUBDIR / str(img.id)
        try:
            _extract_one(iso, kernel, dest_dir / "vmlinuz")
            _extract_one(iso, initrd, dest_dir / "initrd")
        except subprocess.CalledProcessError as e:
            img.status = "error"
            img.message = f"Extraction failed: {e.stderr or e}"
            db.commit()
            return

        needs_nfs, guessed_args = _netboot_plan(entries, img.filename, img.id)
        if needs_nfs:
            # Unpack the live filesystem so the nfs service can export it.
            try:
                _extract_tree(iso, config.NFS_DIR / str(img.id))
            except subprocess.CalledProcessError as e:
                img.status = "error"
                img.message = f"Live filesystem extraction failed: {e.stderr or e}"
                db.commit()
                return

        img.kernel_path = f"{EXTRACT_SUBDIR}/{img.id}/vmlinuz"
        img.initrd_path = f"{EXTRACT_SUBDIR}/{img.id}/initrd"
        img.boot_args = img.boot_args or guessed_args
        img.status = "ready"
        img.message = f"Extracted {Path(kernel).name} + {Path(initrd).name}"
        db.commit()

        ipxe.render(db)
        log.info("Image %s ready (%s)", img.name, family)
    finally:
        db.close()


def delete_image(db, img: Image) -> None:
    """Remove the ISO, extracted files, DB row, and regenerate the menu."""
    iso = iso_path(img.filename)
    iso.unlink(missing_ok=True)
    extracted = config.BOOTROOT_DIR / EXTRACT_SUBDIR / str(img.id)
    shutil.rmtree(extracted, ignore_errors=True)
    shutil.rmtree(config.NFS_DIR / str(img.id), ignore_errors=True)
    db.delete(img)
    db.commit()
    ipxe.render(db)
