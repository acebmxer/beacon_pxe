"""Netboot-archive images: a distro's own installer kernel + initrd.

The alternative to an ISO upload. The catalog (netboot_catalog.json) names a
distro's netboot archive and the kernel arguments that point its installer at a
package mirror. Beacon keeps only the kernel and initrd from the archive; the
client's installer pulls everything else from the mirror.
"""
import copy
import csv
import datetime
import io
import json
import logging
import re
import secrets
import tarfile
import time
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
    distros = [d for d in distros if d.get("id") and d.get("archive_url")]
    for d in distros:
        for rel in d.get("releases", []) + (d.get("live") or {}).get("releases", []):
            rel["ended"] = _past(rel.get("eol"))
    return distros


def _past(day: str | None) -> bool:
    """True once an ISO date (YYYY-MM-DD) has passed; no or bad date is False."""
    try:
        return bool(day) and datetime.date.fromisoformat(day) < datetime.date.today()
    except ValueError:
        return False


def catalog_entry(distro_id: str) -> dict | None:
    for d in load_catalog():
        if d["id"] == distro_id:
            return d
    return None


def _match_release(releases: list[dict], text: str) -> dict | None:
    """The release a typed or picked text means: "12", "Debian 12", "bookworm"
    and the full list label all select bookworm."""
    full = text.strip().lower()
    for r in releases:
        names = {r["id"], str(r.get("number", "")), r["label"], r.get("name", "")}
        if full in {n.lower() for n in names if n}:
            return r
    return None


def resolve_release(entry: dict, text: str) -> str:
    """Turn what was typed or picked into a release name; anything that is not
    a listed release is passed on as a literal release name."""
    text = text.strip()
    low = re.sub(r"^" + re.escape(entry.get("name", "").lower()) + r"\s+", "", text.lower())
    match = _match_release(entry.get("releases", []), low) or _match_release(
        entry.get("releases", []), text)
    return match["id"] if match else text


def _suite_info(url: str, timeout: int = 30) -> dict:
    """Codename and Version from a mirror's Release file ({} if unreadable)."""
    req = urllib.request.Request(url, headers={"User-Agent": "beacon-pxe"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read(256 * 1024).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError):
        return {}
    return dict(m.groups() for m in re.finditer(r"^(Codename|Version): *(\S+)", text, re.M))


def lookup_suites(entry: dict, mirror: str, timeout: int = 30) -> list[dict]:
    """Every release the mirror carries by suite name, with version and codename.

    stable, oldstable, ... carry their version and codename; the testing suite
    has no version, so it is numbered as the one after the newest stable.
    """
    lookup = entry.get("number_lookup")
    if not lookup:
        return []
    found = []
    for suite in lookup["suites"]:
        info = _suite_info(lookup["release_url"].format(mirror=mirror, suite=suite), timeout)
        major = re.match(r"[0-9]+", info.get("Version", ""))
        if major and "Codename" in info:
            found.append({"number": major.group(0), "codename": info["Codename"],
                          "testing": False})
    if found and lookup.get("testing"):
        info = _suite_info(lookup["release_url"].format(mirror=mirror, suite=lookup["testing"]),
                           timeout)
        if "Codename" in info:
            found.append({"number": str(max(int(f["number"]) for f in found) + 1),
                          "codename": info["Codename"], "testing": True})
    return found


def lookup_release(entry: dict, mirror: str, text: str) -> dict | None:
    """The mirror's release a typed number or name means ("15", "Debian 15",
    "duke"), or None if the mirror has no such release."""
    wanted = re.sub(r"^" + re.escape(entry.get("name", "").lower()) + r"\s+", "",
                    text.strip().lower())
    for r in lookup_suites(entry, mirror):
        if wanted in (r["number"], r["codename"].lower()):
            return r
    return None


def lookup_codename(entry: dict, mirror: str, text: str) -> str | None:
    """The release name for a version number the catalog does not list."""
    if not re.fullmatch(r"[0-9]{1,3}", re.sub(
            r"^" + re.escape(entry.get("name", "").lower()) + r"\s+", "", text.strip().lower())):
        return None
    found = lookup_release(entry, mirror, text)
    return found["codename"] if found else None


def live_release_from_mirror(entry: dict, live: dict, text: str) -> dict:
    """A live release that is not in the catalog, found on the mirror by number
    or codename. Released versions are looked up in Debian's current and archive
    ISO folders; the testing release uses the weekly build folder."""
    found = lookup_release(entry, entry["default_mirror"], text)
    if not found:
        raise NetbootError(f"No Debian release '{text.strip()}' was found. Type its "
                           "number or codename, or choose 'Download from a URL' to "
                           "use any ISO.")
    return _live_release_for(entry, live, found)


def _release_label(entry: dict, found: dict) -> str:
    if found["testing"]:
        return (f"{entry['name']} {found['number']} "
                f"({found['codename']}, testing \u2014 pre-release)")
    return f"{entry['name']} {found['number']} ({found['codename']})"


def _live_release_for(entry: dict, live: dict, found: dict) -> dict:
    """A live release for one the mirror carries but the catalog does not list."""
    rel = {"id": found["codename"], "name": f"{entry['name']} {found['number']}",
           "number": found["number"], "label": _release_label(entry, found)}
    if found["testing"]:
        return dict(rel, base_url=live["testing_folder"])
    return dict(rel, base_url=live["unlisted_folders"][0]["base_url"],
                unlisted=found["number"])


# Release discovery for the Images page. The page lists what the mirror carries
# now, so a new release shows up without a Beacon update. Results are kept for a
# while so the page is not slowed by the mirror on every load; a failed lookup is
# retried sooner, and the catalog's own list is shown meanwhile.
_DISCOVERY_TIMEOUT = 5
_DISCOVERY_TTL = 6 * 3600
_DISCOVERY_RETRY = 600
_discovery_cache: dict[str, tuple[float, list[dict] | None]] = {}


def _eol_dates(entry: dict) -> dict[str, str]:
    """Codename -> the date support ends, from the distro's published release
    data (Debian's distro-info-data). Long-term support counts as supported, so
    the LTS end date is used where there is one. {} if it cannot be read."""
    url = entry.get("eol_dates_url")
    if not url:
        return {}
    req = urllib.request.Request(url, headers={"User-Agent": "beacon-pxe"})
    try:
        with urllib.request.urlopen(req, timeout=_DISCOVERY_TIMEOUT) as resp:
            text = resp.read(256 * 1024).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError):
        return {}
    dates = {}
    for row in csv.DictReader(io.StringIO(text)):
        day = (row.get("eol-lts") or row.get("eol") or "").strip()
        try:
            datetime.date.fromisoformat(day)
        except ValueError:
            continue
        dates[(row.get("series") or "").strip().lower()] = day
    return dates


def discover_releases(entry: dict) -> list[dict] | None:
    """The releases the default mirror carries now, newest first, each with the
    date its support ends (None if unknown). None if the mirror cannot be read."""
    lookup = entry.get("number_lookup")
    if not lookup:
        return None
    # One quick probe first, so a Beacon host with no internet waits seconds,
    # not one timeout per suite.
    if not _suite_info(lookup["release_url"].format(
            mirror=entry["default_mirror"], suite=lookup["suites"][0]), _DISCOVERY_TIMEOUT):
        return None
    found = lookup_suites(entry, entry["default_mirror"], _DISCOVERY_TIMEOUT)
    if not found:
        return None
    dates = _eol_dates(entry)
    for r in found:
        r["eol"] = dates.get(r["codename"].lower())
    return sorted(found, key=lambda r: int(r["number"]), reverse=True)


def _cached_discovery(entry: dict) -> list[dict] | None:
    now = time.monotonic()
    hit = _discovery_cache.get(entry["id"])
    if hit and hit[0] > now:
        return hit[1]
    found = discover_releases(entry)
    if found is None:
        log.info("Release lookup for %s failed; showing the catalog's list", entry["id"])
    _discovery_cache[entry["id"]] = (now + (_DISCOVERY_TTL if found else _DISCOVERY_RETRY),
                                     found)
    return found


def with_discovered(entry: dict, found: list[dict]) -> dict:
    """The entry with its release lists rebuilt from what the mirror carries.

    A release the catalog lists keeps its catalog fields (label, live folder);
    the published support-end date replaces the catalog's. The newest released
    one is the default. Catalog releases the mirror no longer carries stay at the
    end, so a release past its support is still shown, marked end of life.
    """
    entry = copy.deepcopy(entry)
    seen = {f["codename"] for f in found}
    stable = next((f["codename"] for f in found if not f["testing"]), None)

    def merged(listed: list[dict], make) -> list[dict]:
        by_id = {r["id"]: r for r in listed}
        out = []
        for f in found:
            rel = dict(by_id.get(f["codename"]) or make(f))
            if f.get("eol"):
                rel["eol"] = f["eol"]
            rel.pop("default", None)
            if f["codename"] == stable:
                rel["default"] = True
            rel["ended"] = _past(rel.get("eol"))
            out.append(rel)
        out.extend(r for r in listed if r["id"] not in seen)
        return out

    entry["releases"] = merged(entry.get("releases", []), lambda f: {
        "id": f["codename"], "number": f["number"], "label": _release_label(entry, f)})
    live = entry.get("live")
    if live:
        live["releases"] = merged(live.get("releases", []),
                                  lambda f: _live_release_for(entry, live, f))
    return entry


def page_catalog() -> list[dict]:
    """The catalog as the Images page shows it: each distro's release list as
    its mirror carries it now, or the catalog's own list if the mirror is out
    of reach."""
    out = []
    for d in load_catalog():
        found = _cached_discovery(d)
        out.append(with_discovered(d, found) if found else d)
    return out


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


def validate_live_release(live: dict, release: str) -> dict:
    """The live release named by the form; the first (current) one if blank."""
    releases = live.get("releases", [])
    if not release.strip() and releases:
        return next((r for r in releases if r.get("default")), releases[0])
    match = _match_release(releases, release)
    if match:
        return match
    raise NetbootError("Unknown live release.")


def resolve_unlisted_live_url(live: dict, number: str, flavour: str) -> str:
    """Find a live ISO for a version that is not in the catalog.

    Debian keeps the newest stable release's ISOs in one folder and moves older
    ones to an archive folder named for the point release, so both are tried.
    The file's own version must match, so asking for 14 never returns 13's ISO.
    """
    for folder in live["unlisted_folders"]:
        rel = dict(folder, label=f"Debian {number}")
        if rel.get("dir_regex"):
            rel["dir_regex"] = rel["dir_regex"].format(number=re.escape(number))
        try:
            base = resolve_release_folder(rel, folder["base_url"])
            url = resolve_live_iso_url(live, base, flavour)
        except NetbootError:
            continue
        if re.search(rf"debian-live-{re.escape(number)}\.", url.rsplit("/", 1)[-1]):
            return url
    raise NetbootError(f"No live ISO for Debian {number} was found in Debian's current "
                       "or archive folders. If it is a pre-release, choose "
                       "'Download from a URL' and paste the ISO address.")


def check_live_iso_matches(live: dict, rel: dict, url: str) -> None:
    """Refuse an ISO whose own version is not the release that was picked.

    The Mirror box is editable and is sent as it stands, so a folder left over
    from another release (13's current-live folder with 14 picked) would
    otherwise give a "Debian 14" image that boots Debian 13.
    """
    number = rel.get("number")
    if not number:
        return
    name = url.rsplit("/", 1)[-1]
    m = re.match(r"debian-live-([0-9.]+|testing)-", name)
    if not m:
        return
    testing = rel.get("base_url") == live.get("testing_folder")
    if (m.group(1) == "testing") if testing else m.group(1).startswith(f"{number}."):
        return
    found = "the testing build" if m.group(1) == "testing" else f"Debian {m.group(1)}"
    raise NetbootError(f"The Mirror folder holds {found} ({name}), not {rel['label']}. "
                       "Pick the release again so the Mirror box is refilled, or "
                       "correct the folder.")


def resolve_release_folder(rel: dict, base_url: str) -> str:
    """The folder holding a live release's ISOs.

    Current releases are served from one fixed folder. Older ones move into an
    archive folder named for the point release (12.15.0-live/), so the newest
    matching folder is read from the listing.
    """
    base_url = base_url.rstrip("/")
    if not rel.get("dir_regex"):
        return base_url
    listing = _read_listing(base_url)
    rx = re.compile(rel["dir_regex"])
    dirs = sorted({m.group(0) for m in rx.finditer(listing)}, key=_version_key)
    if not dirs:
        raise NetbootError(f"No {rel['label']} live folder was found at {base_url}/.")
    return "/".join(p for p in (base_url, dirs[-1].rstrip("/"), rel.get("subpath", "")) if p)


def _version_key(name: str) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", name)]


def _read_listing(base_url: str) -> str:
    req = urllib.request.Request(base_url + "/", headers={"User-Agent": "beacon-pxe"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read(4 * 1024 * 1024).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise NetbootError(f"Could not read {base_url}/ ({getattr(exc, 'reason', exc)}). "
                           "Check the mirror and the Beacon host's internet access.")


def resolve_live_iso_url(live: dict, base_url: str, flavour: str) -> str:
    """Find the current live ISO for a flavour in the mirror's directory listing.

    The file name carries the point release (debian-live-13.7.0-...), which
    changes, so it is read from the listing rather than pinned in the catalog.
    """
    base_url = base_url.rstrip("/")
    listing = _read_listing(base_url)
    rx = re.compile(live["iso_regex"].format(flavour=re.escape(flavour)))
    names = sorted({m.group(0) for m in rx.finditer(listing)}, key=_version_key)
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


def _testing_or_unknown(entry: dict, release: str) -> bool:
    """True if release is the distro's testing or unstable release, or if that
    cannot be told (mirror out of reach)."""
    if release.lower() in ("sid", "unstable", "testing"):
        return True
    lookup = entry.get("number_lookup")
    if not lookup or not lookup.get("testing"):
        return False
    info = _suite_info(lookup["release_url"].format(
        mirror=entry["default_mirror"], suite=lookup["testing"]), _DISCOVERY_TIMEOUT)
    return not info or info.get("Codename", "").lower() == release.lower()


def _check_archive_release(entry: dict, text: bytes, release: str) -> None:
    m = re.search(entry.get("version_regex", r"(?!x)x"), text.decode("utf-8", "replace"))
    # Debian's testing and unstable installers still name the last release they
    # branched from (forky's says "13 (trixie)"), so the check only holds for
    # released versions.
    if (m and m.group(1).lower() != release.lower()
            and not _testing_or_unknown(entry, release)):
        raise NetbootError(f"This archive is the {m.group(1)} installer, not {release}. "
                           f"Use {release}'s netboot archive, or add the image again "
                           f"with {m.group(1)} as the release.")


def _is_iso(path: Path) -> bool:
    """True for an ISO 9660 image (its volume descriptor sits at byte 32769)."""
    try:
        with open(path, "rb") as f:
            f.seek(32769)
            return f.read(5) == b"CD001"
    except OSError:
        return False


def extract_boot_files(archive: Path, entry: dict, dest_dir: Path,
                       release: str | None = None) -> None:
    """Copy just the kernel and initrd out of the archive into dest_dir.

    Only the two named members are read (never extractall), so a hostile archive
    can't write anywhere else. With a release, an archive whose own version file
    names a different release is refused: its kernel would not match the
    packages the installer then fetches from the mirror.
    """
    if _is_iso(archive):
        raise NetbootError("This file is an ISO image, not a netboot archive. Add an "
                           "ISO with Upload ISO at the top of the page, or upload the "
                           "distro's netboot.tar.gz here.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    wanted = {entry["kernel_member"]: "vmlinuz", entry["initrd_member"]: "initrd"}
    found = set()
    try:
        with tarfile.open(archive) as tar:
            for member in tar:
                name = member.name.removeprefix("./")
                if release and name == entry.get("version_member") and member.isreg():
                    _check_archive_release(entry, tar.extractfile(member).read(4096),
                                           release)
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
