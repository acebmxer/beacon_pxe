"""Drop-in Windows storage drivers: list, upload (zip or loose files), delete.

The folder is a host bind (./data/drivers -> /smb/drivers, see docker-compose.yml)
that the smb service re-exports read-only as install\\drivers. At boot WinPE maps
it as Z:, drvloads every .inf it finds, and hands the set to Setup via an answer
file — see services.images._driver_lines for the full story.

Nothing here touches an image. The batch script baked into boot.wim scans the
share at boot time, so drivers uploaded now apply to the next boot of *every*
Windows image, with no re-extraction and no container restart (the bind mount is
live in the smb container).
"""
import hashlib
import json
import re
import shutil
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from .. import config

# Control chars and the path separator are the only things we refuse outright in
# a file name. We deliberately do NOT mangle spaces or other punctuation: a .inf
# references its .sys and .cat files by exact name, so renaming a member is a
# silent way to break a driver package.
_UNSAFE_CHARS = re.compile(r"[\x00-\x1f/\\]")

# The pack folder name is ours to choose, so that one gets sanitised hard.
_SAFE_PACK = re.compile(r"[^A-Za-z0-9._-]+")

# Guard rails for zip extraction. A storage driver package is a few megabytes;
# these exist only so a corrupt or hostile archive can't fill the disk.
_MAX_TOTAL_BYTES = 2 * 1024 ** 3
_MAX_ENTRIES = 20_000

# Curated driver packs Beacon can fetch from the vendor on demand — see the
# provenance note inside the file. Beacon never redistributes these drivers
# (they are proprietary); it downloads them from Microsoft's Windows Update CDN
# when an admin asks, verifying a pinned sha256 first.
_CATALOG_PATH = Path(__file__).resolve().parent.parent / "drivers_catalog.json"

# A driver CAB is 1–2 MB; this cap only stops a hijacked URL from filling the
# disk before the hash check would have rejected the bytes anyway.
_MAX_DOWNLOAD_BYTES = 512 * 1024 ** 2


class DriverError(Exception):
    """Upload rejected — the message is shown to the admin verbatim."""


def safe_pack_name(name: str) -> str:
    return _SAFE_PACK.sub("-", name).strip("-._") or "drivers"


def _resolve_under(root: Path, rel_parts: list[str]) -> Path:
    """Join rel_parts under root, refusing any result that escapes it.

    This is the zip-slip guard, and it matters more here than in a normal upload
    directory: the drivers folder lives *inside* the SMB export, so an entry like
    ../../<image-id>/sources/install.wim would land in another image's install
    media — which every booting client executes. resolve() also collapses
    symlinks, so a planted link pointing outside the tree is caught too.
    """
    root_r = root.resolve()
    dest = root_r.joinpath(*rel_parts).resolve()
    if dest != root_r and root_r not in dest.parents:
        raise DriverError("Archive contains an entry that escapes the drivers "
                          "folder; refusing to extract it.")
    return dest


def _member_parts(name: str) -> list[str]:
    """Split an entry name into safe components (empty list = skip the entry).

    A ".." component is a hard rejection rather than something we quietly drop:
    no driver package legitimately contains one, so it means the archive is either
    hostile or corrupt, and silently extracting the remainder would hand the admin
    a mangled tree that doesn't match what they uploaded. This is what unzip(1)
    does too. A leading "/" or a stray "." is just packaging noise, so those are
    dropped and extraction continues.
    """
    # Zip spec says forward slashes, but plenty of Windows tools write backslashes.
    parts = []
    for component in name.replace("\\", "/").split("/"):
        cleaned = _UNSAFE_CHARS.sub("", component).strip()
        if cleaned in ("", "."):
            continue
        if cleaned == "..":
            raise DriverError("Refusing to unpack: this archive contains a path "
                              "that points outside the drivers folder.")
        parts.append(cleaned)
    return parts


def _dir_stats(path: Path) -> tuple[int, int, int]:
    """(file count, .inf count, total bytes) for a driver folder."""
    files = infs = size = 0
    for p in path.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        files += 1
        if p.suffix.lower() == ".inf":
            infs += 1
        try:
            size += p.stat().st_size
        except OSError:
            pass
    return files, infs, size


def list_entries(root: Path | None = None) -> list[dict]:
    """Everything at the top level of a drivers folder (default: the SMB share).

    Folders are reported as packs; loose files are reported individually, because
    the documented layout allows both (README shows per-vendor subfolders, but a
    single unpacked driver dropped straight in works just as well — WinPE's sweep
    is recursive either way).
    """
    root = root or config.DRIVERS_DIR
    out: list[dict] = []
    for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
        if p.is_symlink():
            continue
        if p.is_dir():
            files, infs, size = _dir_stats(p)
            out.append({"name": p.name, "is_dir": True, "files": files,
                        "infs": infs, "size": size})
        elif p.is_file():
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            out.append({"name": p.name, "is_dir": False, "files": 1,
                        "infs": 1 if p.suffix.lower() == ".inf" else 0,
                        "size": size})
    return out


def total_infs(entries: list[dict]) -> int:
    return sum(e["infs"] for e in entries)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def extract_zip(src: Path, dest: Path) -> int:
    """Extract a driver zip into dest. Returns the number of files written.

    Only regular files are written: directory entries are implied by their
    members, and symlinks are skipped rather than recreated (a link is never part
    of a legitimate Windows driver package, and creating one inside an SMB export
    is a foothold we have no reason to offer).
    """
    written = 0
    with zipfile.ZipFile(src) as zf:
        members = [i for i in zf.infolist() if not i.is_dir()]
        if len(members) > _MAX_ENTRIES:
            raise DriverError(f"Archive holds more than {_MAX_ENTRIES} files; "
                              "that isn't a driver package.")
        if sum(i.file_size for i in members) > _MAX_TOTAL_BYTES:
            raise DriverError("Archive expands to more than 2 GB; "
                              "that isn't a driver package.")
        parsed = []
        for info in members:
            if stat.S_ISLNK(info.external_attr >> 16):
                continue
            parts = _member_parts(info.filename)
            if parts:
                parsed.append((tuple(parts), info))
        # Windows-built zips (Intel's Wired_driver zips among them) store
        # directory markers as ZERO-BYTE entries with no trailing slash, which
        # is_dir() doesn't catch. Written as a file, such a marker then blocks
        # every entry beneath it (NotADirectoryError -> the whole upload fails).
        # Any zero-byte entry whose path prefixes another member is really a
        # directory; its children imply it, so it is skipped, not written.
        dirs = set()
        for parts, _ in parsed:
            for i in range(1, len(parts)):
                dirs.add(parts[:i])
        for parts, info in parsed:
            if info.file_size == 0 and parts in dirs:
                continue
            out = _resolve_under(dest, list(parts))
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as fsrc, open(out, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst, 1024 * 1024)
            except (NotADirectoryError, IsADirectoryError, FileExistsError):
                # A non-empty file and a directory claiming the same name: the
                # archive contradicts itself; refuse it with a reason instead
                # of surfacing a 500.
                raise DriverError("Archive contains entries that conflict with "
                                  "each other; refusing to extract it.")
            written += 1
    if not written:
        raise DriverError("Archive contained no usable files.")
    return written


def member_path(dest: Path, filename: str) -> Path | None:
    """Safe destination for one uploaded file inside dest (None = skip it).

    Only the last component is kept: a browser sends a bare basename for a normal
    file input, and anything that looks like a path here is either a directory
    upload we're flattening or an attempt at traversal.
    """
    parts = _member_parts(filename)
    if not parts:
        return None
    return _resolve_under(dest, parts[-1:])


def target_root(target: str) -> Path:
    """Folder a driver target maps to: 'share' -> SMB share, 'boot' -> baked.

    'share' drivers are served over SMB and swept at boot; 'boot' drivers are
    baked into each Windows boot.wim (NIC drivers — they must be present before
    the network is). Anything else is rejected, since the value can come from a
    form field.
    """
    if target == "boot":
        return config.NICDRIVERS_DIR
    if target == "share":
        return config.DRIVERS_DIR
    raise DriverError("Unknown driver target.")


def pack_dir(name: str, root: Path | None = None) -> Path:
    """Path of a driver pack, guarded against traversal in the supplied name."""
    parts = _member_parts(name)
    if len(parts) != 1:
        raise DriverError("Invalid driver folder name.")
    return _resolve_under(root or config.DRIVERS_DIR, parts)


def delete_entry(name: str, root: Path | None = None) -> None:
    """Remove one pack folder or loose file from a drivers folder."""
    target = pack_dir(name, root)
    if target.is_symlink():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()


def load_catalog() -> list[dict]:
    """The curated fetch-on-demand packs, or [] if the file is absent/broken.

    A broken catalog must not take the drivers page down with it — uploads and
    the staged list still work without it — so parse errors degrade to "no
    recommendations" rather than a 500.
    """
    try:
        packs = json.loads(_CATALOG_PATH.read_text()).get("packs", [])
    except (OSError, ValueError):
        return []
    return [p for p in packs if p.get("id") and p.get("files")]


def catalog_pack(pack_id: str) -> dict | None:
    for p in load_catalog():
        if p["id"] == pack_id:
            return p
    return None


def _download(url: str, dest: Path, referer: str = "") -> str:
    """Stream url to dest, returning the sha256 of what was written.

    The caller compares the digest against the catalog pin, so this only has to
    get the bytes down safely: https-only (a catalog edit can't downgrade to
    plaintext) and size-capped (a hijacked URL can't fill the disk with a
    stream the hash check would reject anyway). Some vendor CDNs
    (downloadmirror.intel.com) refuse requests without a Referer from their own
    site, so the catalog can specify one per file.
    """
    if not url.lower().startswith("https://"):
        raise DriverError("Driver pack URLs must be https.")
    headers = {"User-Agent": "beacon-pxe"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as out:
            while chunk := resp.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_DOWNLOAD_BYTES:
                    raise DriverError("Download exceeded the size limit; "
                                      "refusing to continue.")
                digest.update(chunk)
                out.write(chunk)
    except urllib.error.URLError as exc:
        raise DriverError(
            f"Download failed ({getattr(exc, 'reason', exc)}). The Beacon host "
            "needs internet access to fetch drivers — on an offline network, "
            "download the package elsewhere and upload it here instead.")
    except TimeoutError:
        raise DriverError("Download timed out. Check the Beacon host's "
                          "internet access and try again.")
    return digest.hexdigest()


def _extract_archive(archive: Path, dest: Path) -> None:
    """Unpack a downloaded driver CAB with 7z.

    CABs are what Windows Update serves and zipfile can't open them. Integrity
    is already settled by the sha256 pin, so this only converts verified bytes
    into files. (Zips go through _extract_zip_subset instead: 7z on Linux writes
    a Windows-built zip's backslash member names as literal file names.)
    """
    dest.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["7z", "x", "-y", f"-o{dest}", str(archive)],
                       check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise DriverError(f"Could not unpack the downloaded driver package: "
                          f"{exc.stderr or exc}")


def _extract_zip_subset(archive: Path, dest: Path, prefixes: list[str]) -> int:
    """Extract only the members under the given path prefixes from a zip.

    Vendor bundles (Intel's Wired_driver zip) carry every product family and
    OS tree; a catalog entry names just the folders worth staging so a 38 MB
    download doesn't become 120 MB baked into every boot.wim. Prefixes match
    case-insensitively on the normalised (backslash-fixed, sanitised) path; an
    empty list keeps everything. Same hygiene as extract_zip otherwise,
    including the zero-byte directory-marker handling.
    """
    want = [tuple(q.lower() for q in _member_parts(p)) for p in prefixes]
    written = 0
    with zipfile.ZipFile(archive) as zf:
        parsed = []
        for info in zf.infolist():
            if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
                continue
            parts = _member_parts(info.filename)
            if not parts:
                continue
            lowered = tuple(p.lower() for p in parts)
            if want and not any(lowered[:len(w)] == w for w in want):
                continue
            parsed.append((tuple(parts), info))
        if len(parsed) > _MAX_ENTRIES:
            raise DriverError("Archive holds too many files; refusing it.")
        if sum(i.file_size for _, i in parsed) > _MAX_TOTAL_BYTES:
            raise DriverError("Archive expands too large; refusing it.")
        dirs = set()
        for parts, _ in parsed:
            for i in range(1, len(parts)):
                dirs.add(parts[:i])
        for parts, info in parsed:
            if info.file_size == 0 and parts in dirs:
                continue
            out = _resolve_under(dest, list(parts))
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as fsrc, open(out, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst, 1024 * 1024)
            written += 1
    return written


def _install_tree(src: Path, dest: Path) -> int:
    """Copy the staged files into the pack folder. Returns files copied.

    Same hygiene as zip extraction — regular files only, every path component
    sanitised and kept under dest — even though the source here is a tree 7z
    already wrote: the guard is cheap and keeps one rule for everything that
    lands in the SMB-exported folder.
    """
    written = 0
    for p in sorted(src.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        parts = _member_parts(p.relative_to(src).as_posix())
        if not parts:
            continue
        out = _resolve_under(dest, parts)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, out)
        written += 1
    return written


def fetch_pack(pack: dict) -> int:
    """Download, verify, and stage one catalog pack. Returns files staged.

    Every part is downloaded and hash-checked into a temp dir first; the pack
    folder appears only after all of them pass, so a failed or tampered
    download can never leave a half-staged pack for WinPE to drvload. Multi-file
    packs (AMD's rcbottom/rcraid/rccfg) keep one subfolder per part — the WinPE
    sweep is recursive, and it stops two parts from clobbering same-named files.

    Packs with target "boot" stage into NICDRIVERS_DIR instead of the SMB
    share; the caller is responsible for re-patching the Windows boot.wims
    afterwards (services.images.rebuild_windows_setup_all).
    """
    root = target_root(pack.get("target") or "share")
    dest = pack_dir(pack["id"], root)
    if dest.exists():
        raise DriverError(f"“{pack['id']}” is already staged. Delete it first "
                          "to fetch a fresh copy.")
    with tempfile.TemporaryDirectory(dir=config.DATA_DIR) as tmp:
        staging = Path(tmp) / "stage"
        for i, part in enumerate(pack["files"]):
            archive = Path(tmp) / f"part{i}"
            got = _download(part["url"], archive, part.get("referer") or "")
            want = (part.get("sha256") or "").lower()
            if got != want:
                raise DriverError(
                    f"Checksum mismatch for {pack['title']} — the file at the "
                    "vendor URL is not the one this Beacon release verified. "
                    "Refusing to stage it. An update may have moved the "
                    "package; try again after updating Beacon, or download it "
                    "manually and upload it here.")
            part_dir = staging / safe_pack_name(part.get("name") or f"part{i}")
            if part["url"].lower().endswith(".zip"):
                _extract_zip_subset(archive, part_dir, part.get("include") or [])
            else:
                _extract_archive(archive, part_dir)
        count = sum(1 for f in staging.rglob("*.inf") if f.is_file())
        if not count:
            raise DriverError(f"The downloaded {pack['title']} package "
                              "contained no .inf — upstream layout changed; "
                              "refusing to stage it.")
        try:
            written = _install_tree(staging, dest)
        except Exception:
            # Never leave a half-written pack: WinPE drvloads whatever is here.
            shutil.rmtree(dest, ignore_errors=True)
            raise
    return written
