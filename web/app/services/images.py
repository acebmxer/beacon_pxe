"""Image handling: store ISO uploads and extract Linux kernel/initrd.

Extraction uses bsdtar (libarchive) to read files straight out of the ISO9660
image — no privileged loop mount, so it works in an unprivileged container.
"""
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..db import SessionLocal
from ..models import Image
from .. import config
from ..store import all_settings
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
    r".*/initrd.*",
    r".*/initramfs.*",
]


def iso_path(filename: str) -> Path:
    return config.IMAGE_DIR / filename


def _list_7z(path: Path) -> list[str]:
    """List members via 7z, which reads UDF (modern Windows ISOs use it)."""
    out = subprocess.run(
        ["7z", "-slt", "l", str(path)],
        capture_output=True, text=True, check=True,
    )
    entries = []
    for line in out.stdout.splitlines():
        # 7z -slt prints one "Path = <member>" per file. The first such line is
        # the archive's own path; skip it by ignoring the absolute ISO path.
        if line.startswith("Path = "):
            member = line[len("Path = "):].strip()
            if member and member != str(path):
                entries.append(member)
    return entries


def _list_iso(path: Path) -> list[str]:
    """List ISO members. bsdtar reads ISO9660 (Linux/XCP-NG ISOs); modern
    Windows ISOs are UDF, which bsdtar can't read — it returns only the ISO9660
    stub (a lone README). Fall back to 7z, which handles UDF, in that case.
    """
    out = subprocess.run(
        ["bsdtar", "-tf", str(path)],
        capture_output=True, text=True, check=True,
    )
    entries = [line.strip() for line in out.stdout.splitlines() if line.strip()]
    # A real bootable ISO has dozens+ of entries; a couple means bsdtar only saw
    # the ISO9660 stub of a UDF disc. Re-list with 7z to read the UDF volume.
    real = [e for e in entries if e.lstrip("./").rstrip("/")]
    if len(real) <= 3:
        try:
            return _list_7z(path)
        except subprocess.CalledProcessError:
            pass
    return entries


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


def _extract_one_7z(iso: Path, member: str, dest: Path) -> None:
    """Extract one member to dest via 7z (for UDF ISOs bsdtar can't read)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as fh:
        subprocess.run(
            ["7z", "e", "-so", str(iso), member],
            stdout=fh, stderr=subprocess.DEVNULL, check=True,
        )


def _extract_gunzip(iso: Path, member: str, dest: Path) -> None:
    """Extract a gzip member and write it decompressed (for xen.gz -> xen)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    extract = subprocess.Popen(
        ["bsdtar", "-xOf", str(iso), member], stdout=subprocess.PIPE)
    with open(dest, "wb") as fh:
        gunzip = subprocess.Popen(
            ["gzip", "-dc"], stdin=extract.stdout, stdout=fh)
        extract.stdout.close()  # let extract get SIGPIPE if gunzip dies
        gunzip.communicate()
    extract.wait()
    if extract.returncode or gunzip.returncode:
        raise subprocess.CalledProcessError(
            extract.returncode or gunzip.returncode, "bsdtar|gzip")


def _extract_tree(iso: Path, dest: Path) -> None:
    """Unpack the whole ISO into dest so its live filesystem can be NFS-exported.

    Re-extracts cleanly each time so a Retry can't leave a half-written tree.
    """
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(["bsdtar", "-xf", str(iso), "-C", str(dest)], check=True)


def _extract_tree_7z(iso: Path, dest: Path) -> None:
    """Unpack the whole ISO into dest via 7z (UDF-capable, for Windows ISOs).

    bsdtar can't read the UDF volume modern Windows ISOs use, so the SMB install
    media is unpacked with 7z. Re-extracts cleanly so a Retry can't leave a
    half-written tree.
    """
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["7z", "x", "-y", f"-o{dest}", str(iso)],
        check=True, capture_output=True, text=True,
    )


def _detect_family(entries: list[str]) -> str:
    """Windows install ISOs carry sources/*.wim and a root-level bootmgr.

    XCP-NG / XenServer ISOs carry the Xen hypervisor (boot/xen.gz) plus a
    root-level install.img — these boot via Xen multiboot, not a plain Linux
    kernel+initrd, so they need their own handling.

    Match precisely: a loose endswith("bootmgr") wrongly flags Ubuntu, whose
    pool contains the 'efibootmgr' package path.
    """
    norm = {e.lower().lstrip("./").rstrip("/") for e in entries}
    # XCP-NG: Xen multiboot installer.
    if any(p in norm for p in ("boot/xen.gz", "boot/xen.gz.")) and "install.img" in norm:
        return "xcpng"
    for el in norm:
        base = el.rsplit("/", 1)[-1]
        # Definitive Windows markers.
        if el in ("sources/boot.wim", "sources/install.wim"):
            return "windows"
        # bootmgr / bootmgr.efi at the ISO root (not nested in a package path).
        if base in ("bootmgr", "bootmgr.efi") and "/" not in el:
            return "windows"
    return "linux"


# XCP-NG multiboot files inside the ISO and their fixed extracted names. The
# renderer (services.ipxe) chains them as xen (multiboot kernel) -> vmlinuz
# (first module / dom0 kernel) -> install.img (second module / installer initrd).
#
# xen.gz MUST be gunzipped: iPXE's multiboot loader reads the multiboot header
# from the raw image and cannot decompress the gzip itself (see the iPXE
# XenServer appnote). vmlinuz ships as a raw bzImage and install.img as a module,
# both loaded as-is.
XCPNG_FILES = {
    "boot/vmlinuz": "vmlinuz",
    "install.img": "install.img",
}

# dom0 kernel command line for the netinstall, mirroring the ISO's isolinux.cfg
# "install" label (without the serial console). `netinstall` tells the installer
# to fetch its packages from a network repo it prompts for (or an answerfile).
XCPNG_DOM0_ARGS = "netinstall console=tty0"
XCPNG_XEN_ARGS = "dom0_max_vcpus=1-16 dom0_mem=max:8192M console=vga"

# GRUB modules baked into the standalone UEFI binary: efinet+http to fetch the
# multiboot files over HTTP, multiboot2 to boot Xen, plus the usual video/part
# helpers. net_default_server is set by GRUB to whatever it was chainloaded from.
_GRUB_MODULES = ("multiboot2 http efinet tftp net normal echo linux "
                 "part_gpt part_msdos gzio all_video test configfile")


def _build_xcpng_grub(img: Image, dest_dir: Path, dom0_args: str,
                      server_ip: str) -> None:
    """Build a self-contained UEFI GRUB that multiboots this XCP-NG image.

    iPXE cannot multiboot under UEFI (IMAGE_MULTIBOOT is BIOS-only), so for UEFI
    clients we chainload a grub.efi instead. GRUB *can* multiboot2 under UEFI. The
    cfg fetches xen/vmlinuz/install.img over HTTP from the server's literal IP —
    GRUB does not inherit iPXE's ${net_default_server}, and using a hostname would
    force a DNS lookup that times out ("no DNS reply received").
    """
    host = server_ip or "${net_default_server}"
    base = f"(http,{host})/{EXTRACT_SUBDIR}/{img.id}"
    # net_bootp brings the interface up via DHCP (IP + routing) before the HTTP
    # fetch — without it GRUB's network stack is never initialised.
    cfg = (
        "set timeout=3\n"
        'menuentry "XCP-NG" {\n'
        "    insmod efinet\n"
        "    insmod http\n"
        "    net_bootp\n"
        f"    multiboot2 {base}/xen {XCPNG_XEN_ARGS}\n"
        f"    module2 {base}/vmlinuz {dom0_args}\n"
        f"    module2 {base}/install.img\n"
        "}\n"
    )
    cfg_path = dest_dir / "grub.cfg"
    cfg_path.write_text(cfg)
    subprocess.run(
        ["grub-mkstandalone", "-O", "x86_64-efi",
         "-o", str(dest_dir / "bootx64.efi"),
         "--modules", _GRUB_MODULES,
         f"boot/grub/grub.cfg={cfg_path}"],
        check=True, capture_output=True, text=True,
    )


def _process_xcpng(db, img: Image, iso: Path) -> None:
    """Extract the XCP-NG Xen multiboot files and build the UEFI GRUB chainload."""
    dest_dir = config.BOOTROOT_DIR / EXTRACT_SUBDIR / str(img.id)
    dom0_args = img.boot_args or XCPNG_DOM0_ARGS
    server_ip = all_settings(db).get("server_ip", "")
    # Clean slate so a reprocess never leaves stale files behind.
    shutil.rmtree(dest_dir, ignore_errors=True)
    try:
        # xen.gz must be decompressed for the multiboot loader.
        _extract_gunzip(iso, "boot/xen.gz", dest_dir / "xen")
        for member, name in XCPNG_FILES.items():
            _extract_one(iso, member, dest_dir / name)
        _build_xcpng_grub(img, dest_dir, dom0_args, server_ip)
    except subprocess.CalledProcessError as e:
        img.status = "error"
        detail = getattr(e, "stderr", "") or e
        img.message = f"XCP-NG extraction failed: {detail}"
        db.commit()
        return

    # kernel_path = xen (decompressed hypervisor), initrd_path = vmlinuz. The
    # renderer chainloads the generated bootx64.efi for UEFI multiboot.
    img.kernel_path = f"{EXTRACT_SUBDIR}/{img.id}/xen"
    img.initrd_path = f"{EXTRACT_SUBDIR}/{img.id}/vmlinuz"
    img.boot_args = dom0_args
    img.status = "ready"
    img.message = "Extracted Xen multiboot + built UEFI GRUB chainloader"
    db.commit()


# Windows boot files extracted from the ISO for the wimboot chain, mapped to the
# fixed names the iPXE label references. wimboot loads bootmgr + BCD + boot.sdi +
# boot.wim into a ramdisk and starts WinPE. Paths are matched case-insensitively
# because Windows ISOs are inconsistent about case (boot/BCD vs boot/bcd).
_WINDOWS_FILES = {
    r"bootmgr": "bootmgr",
    r"boot/bcd": "bcd",
    r"boot/boot\.sdi": "boot.sdi",
    r"sources/boot\.wim": "boot.wim",
}

# The Windows Setup image inside boot.wim is index 2 (index 1 is the bare WinPE
# shell). We override index 2's shell with a winpeshl.ini that runs our own
# beacon-setup.cmd, which maps the SMB share and launches setup.exe from it.
# wiminfo confirms this layout on standard install media.
#
# Why not startnet.cmd? On Windows *Setup* media the boot image does not use
# startnet.cmd as its shell — Setup is auto-launched by winpeshl (or its
# built-in default when winpeshl.ini is absent), so a patched startnet.cmd never
# runs and Setup starts with no install-media drive ("a media driver is
# missing"). winpeshl.ini is the entry point WinPE Setup actually honors.
_WIM_SETUP_INDEX = 2

# Samba share name exported by the smb service (see smb/smb.conf).
_SMB_SHARE = "install"

# Set True to drop to a WinPE command prompt (after mounting the share) instead
# of launching Setup — useful for inspecting the mounted media live.
_WINPE_DIAGNOSTIC = False


# winpeshl runs the listed app(s) in order, replacing Setup's default
# auto-launch. We point it at our own beacon-setup.cmd.
_WINPESHL_INI = (
    "[LaunchApps]\r\n"
    "%SYSTEMDRIVE%\\Windows\\System32\\beacon-setup.cmd\r\n"
)


def _beacon_setup_cmd(server_ip: str, image_id: int) -> str:
    """Build the script winpeshl runs: mount the SMB share and launch Setup.

    iPXE can't present the ISO to WinPE as a drive under UEFI (sanhook is BIOS
    INT 13h only), so instead WinPE pulls the install media from Samba. wpeinit
    brings up networking + DHCP; then we map the read-only guest share and launch
    setup.exe from it. The server IP is baked in: WinPE has no DNS for our host.

    This is invoked via winpeshl.ini (not startnet.cmd): on Windows Setup media
    the boot image auto-launches Setup and ignores startnet.cmd, so overriding
    winpeshl is the only way to inject our own pre-Setup steps.

    CRLF line endings — this runs as a Windows batch file.
    """
    host = server_ip or "%SERVER_IP%"
    share = rf"\\{host}\{_SMB_SHARE}\{image_id}"
    lines = [
        "@echo off",
        "wpeinit",
        rf"echo Connecting to Beacon install share {share} ...",
        # Bounded retry (DHCP may not be ready on the first try). Show the real
        # `net use` error on each attempt instead of silently looping forever —
        # a silent infinite loop just makes the firmware reboot with no clue why.
        "set /a tries=0",
        ":retry",
        "set /a tries+=1",
        rf'net use Y: {share} /user:guest ""',
        "if exist Y:\\setup.exe goto run",
        "if %tries% geq 10 goto failed",
        "echo   mount attempt %tries% failed; retrying in 3s ...",
        "ping -n 4 127.0.0.1 >nul",
        "goto retry",
        ":failed",
        "echo.",
        "echo *** Could not mount the install share after %tries% tries. ***",
        rf"echo Server: {host}   Share: {share}",
        "echo --- ipconfig ---",
        "ipconfig",
        rf"echo --- ping {host} ---",
        rf"ping -n 3 {host}",
        "echo Dropping to a prompt so you can diagnose (try the net use by hand).",
        "cmd",
        "goto end",
        ":run",
    ]
    if _WINPE_DIAGNOSTIC:
        # Don't launch Setup; show what WinPE can see on the share, then drop to
        # a prompt so we can inspect the mounted media live.
        lines += [
            "echo === Beacon diagnostics ===",
            "net use",
            "dir Y:\\",
            "dir Y:\\sources\\install.* Y:\\sources\\setup*.*",
            "type Y:\\sources\\install.wim >nul 2>&1 && echo READ_OK || echo READ_FAIL",
            "echo === end diagnostics; dropping to a prompt ===",
            "cmd",
        ]
    else:
        lines += [
            "echo Starting Windows Setup ...",
            "Y:\\setup.exe",
        ]
    lines.append(":end")
    return "\r\n".join(lines) + "\r\n"


def _patch_boot_wim(wim: Path, server_ip: str, image_id: int) -> None:
    """Override the Setup image (index 2) shell with our winpeshl + script.

    Adds winpeshl.ini and beacon-setup.cmd to \\Windows\\System32 via wimlib's
    `wimupdate` add command (overwrites in place, no mount, so it works
    unprivileged in the container). winpeshl.ini makes WinPE run beacon-setup.cmd
    instead of auto-launching Setup, and that script mounts the SMB share and
    starts setup.exe from it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        cmd = Path(tmp) / "beacon-setup.cmd"
        cmd.write_text(_beacon_setup_cmd(server_ip, image_id), newline="")
        ini = Path(tmp) / "winpeshl.ini"
        ini.write_text(_WINPESHL_INI, newline="")
        # `add <source> <dest-in-wim>`; overwrites existing files. wimupdate
        # reads its command list from stdin.
        commands = (
            f"add '{cmd}' '/Windows/System32/beacon-setup.cmd'\n"
            f"add '{ini}' '/Windows/System32/winpeshl.ini'\n"
        )
        subprocess.run(
            ["wimupdate", str(wim), str(_WIM_SETUP_INDEX)],
            input=commands, check=True, capture_output=True, text=True,
        )


def _process_windows(db, img: Image, iso: Path, entries: list[str]) -> None:
    """Prepare a Windows ISO for PXE install via wimboot + SMB.

    1. Extract the WinPE boot files (bootmgr/BCD/boot.sdi/boot.wim) that wimboot
       loads into a ramdisk to start WinPE.
    2. Unpack the whole ISO into SMB_DIR/<id> so the smb service can serve the
       install media (sources/install.wim et al.) to WinPE.
    3. Inject winpeshl.ini + beacon-setup.cmd into boot.wim so WinPE maps that
       share and launches setup.exe from it.
    """
    dest_dir = config.BOOTROOT_DIR / EXTRACT_SUBDIR / str(img.id)
    shutil.rmtree(dest_dir, ignore_errors=True)
    server_ip = all_settings(db).get("server_ip", "")
    try:
        for pattern, name in _WINDOWS_FILES.items():
            member = _match(entries, [pattern])
            if member is None:
                img.status = "error"
                img.message = f"Windows ISO is missing {name} (looked for {pattern})"
                db.commit()
                return
            # Windows ISOs are UDF; extract with 7z (bsdtar can't read UDF).
            _extract_one_7z(iso, member, dest_dir / name)

        # Unpack the full ISO for the SMB share (Setup reads install.wim there).
        _extract_tree_7z(iso, config.SMB_DIR / str(img.id))

        # Override WinPE's shell so it mounts the share and runs Setup.
        _patch_boot_wim(dest_dir / "boot.wim", server_ip, img.id)
    except subprocess.CalledProcessError as e:
        img.status = "error"
        detail = getattr(e, "stderr", "") or e
        img.message = f"Windows extraction failed: {detail}"
        db.commit()
        return

    # kernel_path points at wimboot's WinPE image; the renderer wraps the wimboot
    # chain (no sanhook — WinPE reaches the media over SMB instead).
    img.kernel_path = f"{EXTRACT_SUBDIR}/{img.id}/boot.wim"
    img.initrd_path = f"{EXTRACT_SUBDIR}/{img.id}/bootmgr"
    img.status = "ready"
    img.message = "Extracted WinPE + unpacked SMB media; boots via wimboot"
    db.commit()


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
    return False, "ip=dhcp"


def _clean_derived_data(image_id: int) -> None:
    """Remove every derived artifact for an image (all families) but keep the ISO.

    Called at the start of each (re)process so a run is always a clean slate: an
    image whose family or netboot method changed can't leave a stale boot.wim,
    Xen kernel, or a multi-GB NFS/SMB tree from a previous run behind. The ISO
    itself lives in IMAGE_DIR and is preserved.
    """
    sid = str(image_id)
    shutil.rmtree(config.BOOTROOT_DIR / EXTRACT_SUBDIR / sid, ignore_errors=True)
    shutil.rmtree(config.NFS_DIR / sid, ignore_errors=True)
    shutil.rmtree(config.SMB_DIR / sid, ignore_errors=True)


def process_image(image_id: int) -> None:
    """Run extraction for one image. Intended to run as a background task."""
    db = SessionLocal()
    try:
        img = db.get(Image, image_id)
        if img is None:
            return
        # Mark active extraction so the UI can distinguish "queued" from "working".
        img.status = "processing"
        db.commit()
        # Wipe any artifacts from a previous run before re-extracting, so a
        # changed family/netboot method can't leave stale (possibly huge) data.
        _clean_derived_data(image_id)
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
            _process_windows(db, img, iso, entries)
            ipxe.render(db)
            log.info("Image %s ready (%s)", img.name, family)
            return

        if family == "xcpng":
            # Xen multiboot installer; handled separately from Linux kernel+initrd.
            _process_xcpng(db, img, iso)
            ipxe.render(db)
            log.info("Image %s ready (%s)", img.name, family)
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
    shutil.rmtree(config.SMB_DIR / str(img.id), ignore_errors=True)
    db.delete(img)
    db.commit()
    ipxe.render(db)


def rebuild_xcpng_grub_all(db) -> None:
    """Rebuild the GRUB chainloader for every ready XCP-NG image.

    The server IP is baked into each image's grub.efi, so when Server IP changes
    the chainloaders must be regenerated. This only rewrites grub.cfg + grub.efi
    from the already-extracted xen/vmlinuz/install.img (no slow re-extraction), so
    it is cheap to call whenever settings are saved.
    """
    server_ip = all_settings(db).get("server_ip", "")
    for img in db.query(Image).filter(
            Image.os_family == "xcpng", Image.status == "ready").all():
        dest_dir = config.BOOTROOT_DIR / EXTRACT_SUBDIR / str(img.id)
        if not (dest_dir / "xen").exists():
            continue  # extracted files gone; a full reprocess is needed instead
        try:
            _build_xcpng_grub(img, dest_dir, img.boot_args or XCPNG_DOM0_ARGS,
                              server_ip)
        except subprocess.CalledProcessError as e:
            log.warning("Rebuilding XCP-NG GRUB for %s failed: %s", img.name, e)


def rebuild_windows_setup_all(db) -> None:
    """Re-patch the WinPE setup script for every ready Windows image.

    The server IP is baked into each boot.wim's beacon-setup.cmd (WinPE has no
    DNS for our host), so a Server IP change must rewrite it. This only re-patches
    the already-extracted boot.wim — no slow re-unpacking of the SMB tree.
    """
    server_ip = all_settings(db).get("server_ip", "")
    for img in db.query(Image).filter(
            Image.os_family == "windows", Image.status == "ready").all():
        wim = config.BOOTROOT_DIR / EXTRACT_SUBDIR / str(img.id) / "boot.wim"
        if not wim.exists():
            continue  # extracted files gone; a full reprocess is needed instead
        try:
            _patch_boot_wim(wim, server_ip, img.id)
        except subprocess.CalledProcessError as e:
            log.warning("Re-patching Windows setup for %s failed: %s",
                        img.name, e)
