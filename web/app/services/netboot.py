"""Netboot-archive images: a distro's own installer kernel + initrd.

The alternative to an ISO upload. The catalog (netboot_catalog.json) names a
distro's netboot archive and the kernel arguments that point its installer at a
package mirror. Beacon keeps only the kernel and initrd from the archive; the
client's installer pulls everything else from the mirror.

Admins can add their own entries in the same format (custom entries, stored in
the database); see the custom entries section below.
"""
import copy
import csv
import datetime
import http.client
import io
import json
import logging
import re
import secrets
import ssl
import string
import tarfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from ..models import CatalogEntry
from ..store import get_setting

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


class Cancelled(Exception):
    """The image being built was deleted, so its run stops where it is."""


# The stop flag of the image run on this thread (services.images sets it), so a
# multi-GB download notices a Delete within a second rather than at its end.
_run = threading.local()


def watch_for_stop(event: threading.Event | None) -> None:
    """Set (or clear, with None) the stop flag checked on this thread."""
    _run.stop = event


def check_stop() -> None:
    """Raise Cancelled if this thread's image run has been told to stop."""
    event = getattr(_run, "stop", None)
    if event is not None and event.is_set():
        raise Cancelled()


def load_catalog() -> list[dict]:
    """The built-in distros, or [] if the file is absent/broken.

    A broken catalog must not take the images page down with it; ISO upload
    still works without it.
    """
    try:
        distros = json.loads(_CATALOG_PATH.read_text()).get("distros", [])
    except (OSError, ValueError):
        return []
    distros = [d for d in distros if d.get("id") and (has_installer(d) or d.get("live"))]
    for d in distros:
        live = d.get("live")
        # A live desktop found by folder template uses the installer's releases
        # unless it lists its own.
        if live and live.get("folder") and "releases" not in live:
            live["releases"] = copy.deepcopy(d.get("releases", []))
        for rel in d.get("releases", []) + (live or {}).get("releases", []):
            rel["ended"] = _past(rel.get("eol"))
    return distros


def has_installer(entry: dict) -> bool:
    """True if the entry names installer boot files (some distros are live only)."""
    return bool(entry.get("archive_url") or entry.get("archive_listing")
                or entry.get("kernel_url"))


def for_release(entry: dict, release: str) -> dict:
    """The entry with a release's own boot arguments and extra files, where the
    catalog gives that release different ones (openSUSE Leap 15.6 installs with
    YaST, 16 with Agama). A pre-release the distro keeps somewhere else (Fedora's
    releases/test/, Rocky's staging mirror, Alpine's edge) gets its own boot file
    URLs, arguments and mirror from the entry's "prerelease" part."""
    rel = next((r for r in entry.get("releases", []) if r["id"] == release), None)
    over = {k: rel[k] for k in ("boot_args", "extra_files") if rel and k in rel}
    over.update(_prerelease_overrides(entry, release, entry.get("prerelease", {})))
    return dict(entry, **over) if over else entry


def _prerelease_match(entry: dict, release: str) -> re.Match | None:
    """The match if release names one of the distro's pre-releases (45_Beta,
    10.3-BETA, edge); the "number" group is its version, where it has one."""
    pattern = (entry.get("prerelease") or {}).get("match")
    return re.fullmatch(pattern, release) if pattern else None


def _prerelease_overrides(entry: dict, release: str, spec: dict) -> dict:
    """The templates spec["overrides"] and spec["default_mirror"] give a
    pre-release, with {number} filled in ({mirror} and {version} are left for
    the usual formatting). {} if release is not one of the distro's pre-releases."""
    m = _prerelease_match(entry, release)
    if not m:
        return {}
    number = m.groupdict().get("number") or release
    over = {}
    for key, value in spec.get("overrides", {}).items():
        if key == "iso_regex":
            over[key] = value.replace("{number}", re.escape(number))
        elif key == "extra_files":
            over[key] = [dict(f, url=f["url"].replace("{number}", number)) for f in value]
        else:
            over[key] = value.replace("{number}", number)
    if spec.get("default_mirror"):
        over["default_mirror"] = spec["default_mirror"]
    return over


def live_for_release(entry: dict, live: dict, release: str) -> dict:
    """The live settings for one release: a pre-release's ISOs can sit in another
    folder or mirror, or carry another file name (Fedora's releases/test/,
    Rocky's staging mirror, Linux Mint's testing/)."""
    spec = (entry.get("prerelease") or {}).get("live")
    if not isinstance(spec, dict):
        return live
    over = _prerelease_overrides(entry, release, {
        "overrides": spec, "default_mirror": entry["prerelease"].get("default_mirror")})
    return dict(live, **over) if over else live


def _past(day: str | None) -> bool:
    """True once an ISO date (YYYY-MM-DD) has passed; no or bad date is False."""
    try:
        return bool(day) and datetime.date.fromisoformat(day) < datetime.date.today()
    except ValueError:
        return False


def catalog_entry(distro_id: str, db=None) -> dict | None:
    """A built-in entry, or with a database session also a custom one."""
    for d in load_catalog():
        if d["id"] == distro_id:
            return d
    return custom_entry(db, distro_id) if db is not None else None


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
    if found["codename"] == found["number"]:
        return f"{entry['name']} {found['number']}"
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


def _fetch_text(url: str) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": "beacon-pxe"})
    try:
        with urllib.request.urlopen(req, timeout=_DISCOVERY_TIMEOUT) as resp:
            return resp.read(1024 * 1024).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def _iso_day(day) -> bool:
    try:
        datetime.date.fromisoformat(day)
        return True
    except (TypeError, ValueError):
        return False


def _eol_dates(entry: dict) -> dict[str, str]:
    """Codename or version -> the date support ends, from the distro's published
    release data: distro-info-data's CSV (Debian, Ubuntu) or a JSON list (Fedora's
    Bodhi, Alpine's releases.json). Long-term support counts as supported, so the
    LTS end date is used where there is one. {} if it cannot be read."""
    spec = entry.get("eol_json")
    url = spec["url"] if spec else entry.get("eol_dates_url")
    text = _fetch_text(url) if url else None
    if not text:
        return {}
    dates = {}
    if spec:
        try:
            items = json.loads(text).get(spec["list"], [])
        except (ValueError, AttributeError):
            return {}
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            m = re.search(spec["id_regex"], str(item.get(spec["id"], "")))
            if m and _iso_day(item.get(spec["date"])):
                dates[m.group(1).lower()] = item[spec["date"]]
        return dates
    for row in csv.DictReader(io.StringIO(text)):
        day = (row.get("eol-lts") or row.get("eol") or "").strip()
        if not _iso_day(day):
            continue
        dates[(row.get("series") or "").strip().lower()] = day
        # Ubuntu's releases are picked by number ("24.04 LTS" -> 24.04).
        number = (row.get("version") or "").split(" ")[0].strip()
        if number:
            dates.setdefault(number, day)
    return dates


def _discover_listing(entry: dict) -> list[dict] | None:
    """Releases read from a folder listing on the mirror (Fedora's releases/,
    Rocky's numbered folders), newest first; None if it cannot be read."""
    spec = entry["release_listing"]
    try:
        listing = _read_listing(spec["url"].format(mirror=entry["default_mirror"]).rstrip("/"),
                                _DISCOVERY_TIMEOUT)
    except NetbootError:
        return None
    ids = {m.group(1) for m in re.finditer(r'href="(?:\./)?' + spec["regex"] + '"', listing)}
    if spec.get("min"):
        ids = {i for i in ids if _version_key(i) >= _version_key(spec["min"])}
    newest = sorted(ids, key=_version_key, reverse=True)[:spec.get("count", 10)]
    if not newest:
        return None
    dates = _eol_dates(entry)
    ahead = _unreleased(entry)
    return [{"number": i, "codename": i, "testing": i.lower() in ahead,
             "eol": dates.get(i.lower())} for i in newest]


def _unreleased(entry: dict) -> set[str]:
    """Series and version numbers whose release day is still ahead, from
    distro-info-data's CSV: Ubuntu's folder for a release is there from its beta
    on. Empty if the entry has no CSV or it cannot be read."""
    url = entry.get("eol_dates_url")
    text = _fetch_text(url) if url else None
    if not text:
        return set()
    today = datetime.date.today().isoformat()
    ahead = set()
    for row in csv.DictReader(io.StringIO(text)):
        day = (row.get("release") or "").strip()
        if not _iso_day(day) or day <= today:
            continue
        ahead.add((row.get("series") or "").strip().lower())
        number = (row.get("version") or "").split(" ")[0].strip()
        if number:
            ahead.add(number)
    return ahead


def discover_releases(entry: dict) -> list[dict] | None:
    """The releases the default mirror carries now, newest first, each with the
    date its support ends (None if unknown). None if the mirror cannot be read."""
    if entry.get("release_listing"):
        return _discover_listing(entry)
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
    if found is None and (entry.get("number_lookup") or entry.get("release_listing")):
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
    # Ubuntu's default is the newest long-term release, not the newest one.
    prefer = (entry.get("release_listing") or {}).get("prefer")
    if prefer:
        stable = next((f["codename"] for f in found
                       if re.search(prefer, f["codename"]) and not f["testing"]), stable)

    def merged(listed: list[dict], make) -> list[dict]:
        by_id = {r["id"]: r for r in listed}
        out = []
        for f in found:
            rel = dict(by_id.get(f["codename"]) or make(f))
            if f["testing"]:
                rel["prerelease"] = True
                if "pre-release" not in rel["label"]:
                    rel["label"] += " — pre-release"
            if f.get("eol"):
                rel["eol"] = f["eol"]
            rel.pop("default", None)
            if f["codename"] == stable:
                rel["default"] = True
            rel["ended"] = _past(rel.get("eol"))
            out.append(rel)
        out.extend(r for r in listed if r["id"] not in seen)
        return out

    def plain(f):
        return {"id": f["codename"], "number": f["number"], "label": _release_label(entry, f)}

    if has_installer(entry):
        entry["releases"] = merged(entry.get("releases", []), plain)
    live = entry.get("live")
    if live:
        live["releases"] = merged(live.get("releases", []), plain if live.get("folder")
                                  else lambda f: _live_release_for(entry, live, f))
    return entry


_build_cache: dict[tuple[str, str], tuple[float, str | None]] = {}


def current_build(entry: dict, release: str) -> str | None:
    """The newest build the distro publishes now for a release, read from its
    mirror (Ubuntu 26.04 -> "26.04.1", Debian trixie -> "13.7", this month's
    Arch), so the Release list shows it without a Beacon update. None if the
    entry names no source, the mirror cannot be read, or nothing matches (a
    testing release has no point version)."""
    spec = entry.get("current_build")
    if not spec:
        return None
    # Filled with replace, not format: the regex has its own braces ({8}).
    url = spec["url"].replace("{mirror}", entry["default_mirror"]).replace(
        "{version}", release)
    text = _fetch_text(url)
    if text is None:
        return None
    rx = re.compile(spec["regex"].replace("{version}", re.escape(release)), re.M)
    found = sorted({m.group(1) for m in rx.finditer(text)}, key=_version_key)
    return found[-1] if found else None


def _cached_build(entry: dict, release: str) -> str | None:
    now = time.monotonic()
    key = (entry["id"], release)
    hit = _build_cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    found = current_build(entry, release)
    _build_cache[key] = (now + (_DISCOVERY_TTL if found else _DISCOVERY_RETRY), found)
    return found


def with_builds(entry: dict, builds: dict[str, str]) -> dict:
    """The entry with each release's newest build added to its label
    ("Ubuntu 26.04 LTS (Resolute Raccoon) — 26.04.1"). The build is kept as
    "build" too. A distro whose live desktops are built on their own schedule
    (Arch's EndeavourOS, CachyOS) shows it on the installer's releases only."""
    entry = copy.deepcopy(entry)
    show = entry["current_build"].get("show", "{build}")
    lists = [entry.get("releases", [])]
    if entry.get("live") and not entry["current_build"].get("installer_only"):
        lists.append(entry["live"].get("releases", []))
    for listed in lists:
        for rel in listed:
            build = builds.get(rel["id"])
            if build and build not in (rel["id"], str(rel.get("number", ""))):
                rel["build"] = build
                rel["label"] = f"{rel['label']} — {show.replace('{build}', build)}"
    return entry


def _release_ids(entry: dict) -> list[str]:
    ids = [r["id"] for r in entry.get("releases", [])]
    if entry.get("live") and not entry["current_build"].get("installer_only"):
        ids += [r["id"] for r in entry["live"].get("releases", []) if r["id"] not in ids]
    return ids


def page_catalog(db=None) -> list[dict]:
    """The catalog as the Images page shows it: each distro's release list as
    its mirror carries it now, each release with the newest build published for
    it, or the catalog's own list if the mirror is out of reach, and the
    distro's pre-releases at the top. Custom entries follow the built-in ones.
    The mirrors are asked at the same time, so an unreachable one costs one
    timeout, not one per distro."""
    distros = load_catalog()
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = pool.map(_cached_prereleases, distros)
        founds = list(pool.map(_cached_discovery, distros))
        out = [with_discovered(d, found) if found else d for d, found in zip(distros, founds)]
        wanted = [(d, rid) for d in out if d.get("current_build") for rid in _release_ids(d)]
        found = pool.map(lambda job: _cached_build(*job), wanted)
        builds: dict[str, dict[str, str]] = {}
        for (d, rid), build in zip(wanted, found):
            if build:
                builds.setdefault(d["id"], {})[rid] = build
        pres = list(pending)
    out = [with_builds(d, builds[d["id"]]) if d["id"] in builds else d for d in out]
    out = [with_prereleases(d, p) if p else d for d, p in zip(out, pres)]
    out += custom_entries(db) if db is not None else []
    if db is not None and not show_eol(db):
        out = [_without(d, "ended") for d in out]
    if db is not None and not show_prerelease(db):
        out = [_without(d, "prerelease") for d in out]
    return out


def show_eol(db) -> bool:
    """Settings → Netboot catalog → Offer end-of-life releases."""
    return get_setting(db, "netboot_show_eol") == "1"


def show_prerelease(db) -> bool:
    """Settings → Netboot catalog → Offer pre-releases."""
    return get_setting(db, "netboot_show_prerelease") == "1"


def _without(entry: dict, flag: str) -> dict:
    """The entry without its releases marked flag ("ended", "prerelease")."""
    entry = copy.deepcopy(entry)
    if "releases" in entry:
        entry["releases"] = [r for r in entry["releases"] if not r.get(flag)]
    if entry.get("live"):
        entry["live"]["releases"] = [r for r in entry["live"].get("releases", [])
                                     if not r.get(flag)]
    return entry


# Pre-releases: betas, release candidates and development releases a distro
# publishes beside its releases (Fedora's releases/test/45_Beta, Rocky's and
# Alma's x.y-beta trees, openSUSE Leap's RC, Linux Mint's testing/ ISOs,
# Alpine's edge, Kali's kali-dev). The entry's "prerelease" part says how to find
# them: a folder listing, the distro's own release states (openSUSE), or a fixed
# list. Debian's testing suite is found with its releases (number_lookup), and a
# release whose day is still ahead by distro-info-data (Ubuntu's beta) is marked
# with its releases.

def discover_prereleases(entry: dict) -> list[dict] | None:
    """The pre-releases the distro publishes now, newest first, each as a release
    for the Release list. None if the mirror cannot be read."""
    spec = entry.get("prerelease") or {}
    mirror = spec.get("default_mirror") or entry["default_mirror"]
    found = [dict(item) for item in spec.get("ids", [])]
    listing = spec.get("listing")
    if listing:
        try:
            text = _read_listing(listing["url"].format(mirror=mirror).rstrip("/"),
                                 _DISCOVERY_TIMEOUT)
        except NetbootError:
            return None
        named = {}
        for m in re.finditer(r'href="(?:\./)?' + listing["regex"] + '"', text):
            named[listing["id"].format(**m.groupdict())] = m.groupdict()
        # Old betas stay in some folders (Alma's vault keeps every one since 8.4):
        # only the newest of each major release can still be ahead of it.
        majors = set()
        for rid in sorted(named, key=_version_key, reverse=True):
            major = _version_key(named[rid].get("number") or rid)[:1]
            if major and tuple(major) in majors:
                continue
            majors.add(tuple(major))
            found.append(dict(named[rid], id=rid, label=listing["label"]))
    states = spec.get("state_json")
    if states:
        text = _fetch_text(states["url"])
        try:
            items = json.loads(text).get(states["list"], []) if text else None
        except (ValueError, AttributeError):
            items = None
        if not isinstance(items, list):
            return None
        for item in items:
            if not isinstance(item, dict) or item.get(states["state"]) in states["released"]:
                continue
            number, stage = str(item.get(states["id"], "")), str(item.get(states["state"], ""))
            if re.fullmatch(r"[A-Za-z0-9 ._-]{1,20}", stage):
                found.append({"id": number, "number": number, "stage": stage,
                              "label": states["label"]})
    out = []
    for item in found:
        if not _RELEASE_RE.fullmatch(item["id"]):
            continue
        name = item["label"].format(name=entry["name"], number=item.get("number", ""),
                                    stage=item.get("stage", ""))
        rel = {"id": item["id"], "name": name, "label": f"{name} — pre-release",
               "prerelease": True}
        if item.get("number"):
            rel["number"] = item["number"]
        if spec.get("default_mirror"):
            rel["mirror"] = spec["default_mirror"]
        out.append(rel)
    return out


_prerelease_cache: dict[str, tuple[float, list[dict] | None]] = {}


def _cached_prereleases(entry: dict) -> list[dict] | None:
    if not (entry.get("prerelease") or {}).keys() & {"ids", "listing", "state_json"}:
        return None
    now = time.monotonic()
    hit = _prerelease_cache.get(entry["id"])
    if hit and hit[0] > now:
        return hit[1]
    found = discover_prereleases(entry)
    if found is None:
        log.info("Pre-release lookup for %s failed; none are listed", entry["id"])
    _prerelease_cache[entry["id"]] = (
        now + (_DISCOVERY_TTL if found is not None else _DISCOVERY_RETRY), found)
    return found


def _superseded(number: str | None, released: list[str]) -> bool:
    """True once the distro has released that version or a newer one of the
    same major release (Mint's 22.3 beta once 22.3 is out, Alma's 9.8 beta
    once 9.8 is out), so a leftover beta is not offered."""
    want = _version_key(number or "")
    if not want:
        return False
    for version in released:
        have = _version_key(version)
        if have and have[0] == want[0] and have[:len(want)] >= want:
            return True
    return False


def with_prereleases(entry: dict, found: list[dict]) -> dict:
    """The entry with the distro's pre-releases at the top of its Release lists
    (the installer's, and the live desktop's unless the pre-release has none).
    One the distro has since released is left out."""
    entry = copy.deepcopy(entry)
    spec = entry.get("prerelease") or {}
    lists = []
    if has_installer(entry):
        lists.append(entry.setdefault("releases", []))
    if entry.get("live") and spec.get("live") is not False:
        lists.append(entry["live"].setdefault("releases", []))
    for listed in lists:
        released = [v for r in listed if not r.get("prerelease")
                    for v in (r["id"], r.get("number"), r.get("build")) if v]
        ids = {r["id"] for r in listed}
        listed[:0] = [dict(p) for p in found
                      if p["id"] not in ids and not _superseded(p.get("number"), released)]
    return entry


def prerelease_release(entry: dict, release: str, live: bool = False) -> dict | None:
    """The release, if it is a pre-release, by the same lists the Images page
    shows; a typed name the distro marks as a pre-release (45_Beta, edge,
    Debian's sid) counts too."""
    if not entry.get("custom"):
        found = _cached_discovery(entry)
        if found:
            entry = with_discovered(entry, found)
        pres = _cached_prereleases(entry)
        if pres:
            entry = with_prereleases(entry, pres)
    listed = (entry.get("live") or {}).get("releases", []) if live else entry.get("releases", [])
    rel = next((r for r in listed if r["id"] == release), None)
    if rel is not None:
        return rel if rel.get("prerelease") else None
    if _prerelease_match(entry, release) or (
            entry.get("number_lookup") and release.lower() in ("sid", "unstable", "testing")):
        return {"id": release, "label": f"{entry['name']} {release}"}
    return None


def ended_release(entry: dict, release: str, live: bool = False) -> dict | None:
    """The listed release, if its end of life has passed, by the same dates the
    Images page shows (the mirror's where it was read, else the catalog's)."""
    if not entry.get("custom"):
        found = _cached_discovery(entry)
        if found:
            entry = with_discovered(entry, found)
    listed = (entry.get("live") or {}).get("releases", []) if live else entry.get("releases", [])
    rel = next((r for r in listed if r["id"] == release), None)
    return rel if rel and rel.get("ended") else None


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


def resolve_archive_url(entry: dict, mirror: str, release: str) -> str:
    """The archive to download. Ubuntu names its archive for the point release
    (ubuntu-24.04.5-netboot-amd64.tar.gz), so the newest one is read from the
    mirror's listing."""
    spec = entry.get("archive_listing")
    if not spec:
        return archive_url(entry, mirror, release)
    folder = spec["folder"].format(mirror=mirror, version=release)
    listing = _read_listing(folder)
    names = _listed_names(listing, spec["regex"], release)
    beta = (entry.get("prerelease") or {}).get("archive_regex")
    if not names and beta:
        # Before its release day the folder holds only the beta's archive
        # (ubuntu-26.10-beta-netboot-amd64.tar.gz).
        names = _listed_names(listing, beta, release)
    if not names:
        raise NetbootError(f"No {entry['name']} {release} netboot archive was found at "
                           f"{folder}/. Check the release and the mirror.")
    return f"{folder}/{names[-1]}"


def _listed_names(listing: str, regex: str, release: str) -> list[str]:
    rx = re.compile(r"(?<![\w.-])" + regex.format(version=re.escape(release)))
    return sorted({m.group(0) for m in rx.finditer(listing)}, key=_version_key)


def boot_args(entry: dict, mirror: str, release: str, image_id: int = 0,
              values: dict | None = None) -> str:
    """The kernel command line. image_id places files kept on Beacon
    (${boot-url}/os/<id>/...); values are read from the archive (Ubuntu's ISO URL)."""
    parsed = urlparse(mirror)
    return for_release(entry, release).get("boot_args", "").format(
        mirror=mirror, version=release, mirror_proto=parsed.scheme,
        mirror_host=parsed.netloc, mirror_path=parsed.path or "/", image_id=image_id,
        **(values or {}))


def archive_values(entry: dict, mirror: str, values: dict) -> dict:
    """Values read from an archive, on the image's mirror: Ubuntu's archive names
    its ISO on releases.ubuntu.com, which is swapped for a mirror the admin set."""
    default = entry.get("default_mirror", "")
    out = {}
    for key, value in values.items():
        if mirror != default and value.startswith(default + "/"):
            value = mirror + value[len(default):]
        out[key] = value
    return out


def new_archive_name(distro_id: str, release: str, ext: str = ".tar.gz") -> str:
    """A unique file name, so two images of one release never share a download."""
    return f"netboot-{distro_id}-{release}-{secrets.token_hex(4)}{ext}"


def live_entry(entry: dict) -> dict | None:
    """The live-desktop part of a catalog entry, if the distro has one."""
    return entry.get("live") or None


def validate_flavour(live: dict, flavour: str, release: str | None = None) -> str:
    flavour = flavour.strip()
    found = next((f for f in live.get("flavours", []) if f["id"] == flavour), None)
    if found is None:
        raise NetbootError("Choose one of the listed desktops.")
    # A pre-release counts as its major release (Rocky's 9.9-BETA as 9).
    major = re.match(r"[0-9]+", release or "")
    if (release is not None and found.get("only") and release not in found["only"]
            and not (major and major.group(0) in found["only"])):
        raise NetbootError(f"{found['label']} is only published for release "
                           f"{', '.join(found['only'])}.")
    return flavour


def live_for_flavour(live: dict, flavour: str) -> dict:
    """The live settings for one desktop: a flavour can have its own folder, file
    pattern or mirror (Ubuntu's own desktop is on releases.ubuntu.com, the
    flavours on cdimage.ubuntu.com)."""
    found = next((f for f in live.get("flavours", []) if f["id"] == flavour), {})
    over = {k: found[k] for k in ("folder", "iso_regex", "default_mirror", "dir_regex",
                                  "kernel_member", "initrd_member") if k in found}
    return dict(live, **over) if over else live


def live_mirror(entry: dict, live: dict, flavour: str) -> str:
    return live_for_flavour(live, flavour).get("default_mirror") or entry["default_mirror"]


def _flavour_fields(live: dict, flavour: str) -> dict:
    """A flavour's text fields (dir, file, ...) for the folder and file templates."""
    found = next((f for f in live.get("flavours", []) if f["id"] == flavour), {})
    return {k: v for k, v in found.items()
            if isinstance(v, str) and k not in ("flavour", "version", "mirror", "dir_regex")}


def resolve_flavour_iso_url(live: dict, flavour: str, mirror: str, version: str,
                            over: dict | None = None) -> str:
    """The newest live ISO of a flavour and release, for a distro whose ISO folder
    follows a template ({mirror}/releases/{version}/Spins/x86_64/iso). over
    replaces the folder or file pattern, even a flavour's own."""
    lv = dict(live_for_flavour(live, flavour), **(over or {}))
    folder = lv["folder"].format(mirror=mirror, version=version, flavour=flavour,
                                 **_flavour_fields(live, flavour))
    if lv.get("dir_regex"):
        # One folder per build (CachyOS's ISO/desktop/260809/): take the newest.
        folder = resolve_release_folder({"dir_regex": lv["dir_regex"], "label": flavour},
                                        folder)
    return resolve_live_iso_url(lv, folder, flavour, version)


def resolve_release_iso_url(entry: dict, live: dict, flavour: str, mirror: str,
                            release: str) -> str:
    """The live ISO for a flavour and release of a distro whose ISO folder follows
    a template. A pre-release is looked for where the distro keeps it. Before its
    release day a release has only beta ISOs (Ubuntu's ubuntu-26.10-beta-...,
    kubuntu's in releases/26.10/beta/), so they are looked for when no final ISO
    is found: in the same folder, then in the beta folder."""
    lv = live_for_release(entry, live, release)
    beta = (entry.get("prerelease") or {}).get("live_fallback")
    try:
        return resolve_flavour_iso_url(lv, flavour, mirror, release)
    except NetbootError as first:
        if not beta:
            raise
        error = first
    for over in ({"iso_regex": beta["iso_regex"]}, beta):
        try:
            return resolve_flavour_iso_url(lv, flavour, mirror, release, over)
        except NetbootError:
            continue
    raise error


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
    """The numbers in a file or folder name, for finding the newest. Architecture
    names are not versions: in ubuntu-26.04-netboot-amd64 the 64 would otherwise
    sort the release above its point release 26.04.1."""
    name = re.sub(r"x86_64|amd64|arm64|aarch64|ppc64le|s390x|i[3-6]86", "", name)
    return [int(n) for n in re.findall(r"\d+", name)]


def _read_listing(base_url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(base_url + "/", headers={"User-Agent": "beacon-pxe"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(4 * 1024 * 1024).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise NetbootError(f"Could not read {base_url}/ ({getattr(exc, 'reason', exc)}). "
                           "Check the mirror and the Beacon host's internet access.")


def resolve_live_iso_url(live: dict, base_url: str, flavour: str, version: str = "") -> str:
    """Find the current live ISO for a flavour in the mirror's directory listing.

    The file name carries the point release (debian-live-13.7.0-...), which
    changes, so it is read from the listing rather than pinned in the catalog.
    """
    base_url = base_url.rstrip("/")
    listing = _read_listing(base_url)
    fields = {k: re.escape(v) for k, v in _flavour_fields(live, flavour).items()}
    # Anchored at the start of a file name, so "ubuntu-..." never matches inside
    # "kubuntu-...".
    rx = re.compile(r"(?<![\w.-])" + live["iso_regex"].format(
        **fields, flavour=re.escape(flavour), version=re.escape(version)))
    names = sorted({m.group(0) for m in rx.finditer(listing)}, key=_version_key)
    if not names:
        raise NetbootError(f"No live ISO for '{flavour}' was found at {base_url}/.")
    return f"{base_url}/{names[-1]}"


def live_boot_args(live: dict, image_id: int) -> str:
    return live["boot_args"].format(image_id=image_id)


DOWNLOAD_ATTEMPTS = 4
_RETRY_PAUSE = 5


def download(url: str, dest: Path, max_bytes: int = ARCHIVE_MAX_BYTES) -> None:
    """Stream url to dest (via a .part file) with a size cap and timeout.

    A connection that stalls or drops part-way is resumed where it stopped
    (an HTTP Range request), up to DOWNLOAD_ATTEMPTS tries in all, so one hiccup
    does not throw away a multi-GB live ISO. A server that ignores the range is
    read again from the start, and one that resumes at the wrong offset is
    asked again for the whole file.

    Separate from services.drivers._download on purpose: that one is https-only
    and pinned to a hash, while a netboot mirror is often plain http.
    """
    part = dest.with_name(dest.name + ".part")
    part.unlink(missing_ok=True)
    size = 0
    try:
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            headers = {"User-Agent": "beacon-pxe"}
            if size:
                headers["Range"] = f"bytes={size}-"
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    start = _range_start(resp) if resp.status == 206 else 0
                    if size and start != size:
                        # Appending would corrupt the file. A reply from byte 0
                        # (or a 200) is the whole file: keep it. Any other
                        # offset is useless: drop what we have and try again.
                        size = 0
                        if start != 0:
                            raise ConnectionError("the server resumed at the wrong offset")
                    expected = _expected_size(resp, size)
                    with open(part, "ab" if size else "wb") as out:
                        while chunk := resp.read(1024 * 1024):
                            check_stop()
                            size += len(chunk)
                            if size > max_bytes:
                                raise NetbootError("Download exceeded the size limit; "
                                                   "refusing to continue.")
                            out.write(chunk)
                if expected and size < expected:
                    raise http.client.IncompleteRead(b"", expected - size)
                part.replace(dest)
                return
            except urllib.error.HTTPError as exc:
                raise NetbootError(f"Download failed: the server answered {exc.code} "
                                   f"for {url}. Check the release name and mirror.")
            except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError,
                    http.client.HTTPException) as exc:
                if attempt == DOWNLOAD_ATTEMPTS:
                    if isinstance(exc, TimeoutError) or isinstance(
                            getattr(exc, "reason", None), TimeoutError):
                        raise NetbootError(
                            f"Download timed out ({DOWNLOAD_ATTEMPTS} tries). Check the "
                            "Beacon host's internet access and try again.")
                    if isinstance(exc, urllib.error.URLError):
                        raise NetbootError(f"Download failed ({getattr(exc, 'reason', exc)}). "
                                           "The Beacon host needs internet access, or upload "
                                           "the archive instead.")
                    raise NetbootError(f"Download failed: the connection to {url} kept "
                                       f"dropping ({DOWNLOAD_ATTEMPTS} tries).")
                log.info("Download of %s interrupted at %d bytes (%s); trying again",
                         url, size, exc)
                time.sleep(_RETRY_PAUSE)
    finally:
        part.unlink(missing_ok=True)


def _range_start(resp) -> int | None:
    """The first byte a 206 reply carries (Content-Range: bytes <start>-...)."""
    m = re.match(r"\s*bytes\s+([0-9]+)-", resp.headers.get("Content-Range", ""))
    return int(m.group(1)) if m else None


def _expected_size(resp, offset: int) -> int | None:
    """The full file size the server announced, if any (Content-Length, or the
    total in Content-Range for a resumed request)."""
    if resp.status == 206:
        m = re.search(r"/([0-9]+)\s*$", resp.headers.get("Content-Range", ""))
        return int(m.group(1)) if m else None
    length = resp.headers.get("Content-Length", "")
    return int(length) if length.isdigit() else None


def _testing_or_unknown(entry: dict, release: str) -> bool:
    """True if release is the distro's testing or unstable release, or if that
    cannot be told (mirror out of reach)."""
    if release.lower() in ("sid", "unstable", "testing") or _prerelease_match(entry, release):
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
                       release: str | None = None) -> dict:
    """Copy just the kernel and initrd out of the archive into dest_dir.

    Only the two named members are read (never extractall), so a hostile archive
    can't write anywhere else. With a release, an archive whose own version file
    names a different release is refused: its kernel would not match the
    packages the installer then fetches from the mirror.

    Returns the values the entry reads from the archive for its kernel
    arguments (Ubuntu's iso-url, from the archive's own boot menu).
    """
    if _is_iso(archive):
        raise NetbootError("This file is an ISO image, not a netboot archive. Add an "
                           "ISO with Upload ISO at the top of the page, or upload the "
                           "distro's netboot.tar.gz here.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    wanted = {entry["kernel_member"]: "vmlinuz", entry["initrd_member"]: "initrd"}
    reads = {v["member"]: v for v in entry.get("archive_values", [])}
    found, values = set(), {}
    try:
        with tarfile.open(archive) as tar:
            for member in tar:
                name = member.name.removeprefix("./")
                if release and name == entry.get("version_member") and member.isreg():
                    _check_archive_release(entry, tar.extractfile(member).read(4096),
                                           release)
                if name in reads and member.isreg():
                    text = tar.extractfile(member).read(64 * 1024).decode("utf-8", "replace")
                    m = re.search(reads[name]["regex"], text)
                    if m and validate_source_url(m.group(1)):
                        values[reads[name]["name"]] = m.group(1)
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
    unread = sorted(m for m, v in reads.items() if v["name"] not in values)
    if unread:
        raise NetbootError(f"The archive's {', '.join(unread)} does not give the "
                           "address its installer needs. Is it the right distro?")
    return values


# --- Custom entries ---------------------------------------------------------
# Entries an admin adds, in the same format as netboot_catalog.json. They are
# installers only (no live desktop, no release discovery), and name their boot
# files either as an archive holding the kernel and initrd (Debian's
# netboot.tar.gz) or as a kernel URL and an initrd URL (Fedora's
# images/pxeboot/, Alpine's netboot/). The templates are the part that is easy
# to get wrong, so every one is checked here and the Images page shows what
# they produce before an entry is saved.

_URL_FIELDS = ("mirror", "version")
_ARGS_FIELDS = ("mirror", "version", "mirror_proto", "mirror_host", "mirror_path")
_ENTRY_FIELDS = {
    "id": "ID", "name": "Name", "default_mirror": "Default mirror",
    "archive_url": "Archive URL", "kernel_member": "Kernel in the archive",
    "initrd_member": "Initrd in the archive", "kernel_url": "Kernel URL",
    "initrd_url": "Initrd URL", "boot_args": "Kernel arguments", "releases": "Releases",
}
_RELEASE_FIELDS = {"id", "label", "number", "default", "eol", "prerelease"}
_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_MEMBER_RE = re.compile(r"[A-Za-z0-9._+-]+(/[A-Za-z0-9._+-]+)*")
_NUMBER_RE = re.compile(r"[0-9][0-9.]{0,15}")
MAX_CUSTOM_ENTRIES = 100
MAX_RELEASES = 50
IMPORT_MAX_BYTES = 256 * 1024


def entry_id_for(name: str) -> str:
    """The ID an entry gets from its name when none is given ("Fedora" -> fedora)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32].strip("-") or "custom"


def _template_fields(value: str, label: str, allowed: tuple[str, ...]) -> set[str]:
    """The placeholders a template uses; anything not in allowed is refused.

    The template is filled with str.format, so a field such as {mirror.__class__}
    or a format spec must never reach it.
    """
    braces = "Write a literal { or } as {{ or }} (an iPXE variable is ${{name}})."
    try:
        parsed = list(string.Formatter().parse(value))
    except ValueError:
        raise NetbootError(f"{label} has an unmatched {{ or }}. {braces}")
    used = set()
    for _, field, spec, conversion in parsed:
        if field is None:
            continue
        if field not in allowed or spec or conversion:
            raw = "{" + field + (f"!{conversion}" if conversion else "") + (
                f":{spec}" if spec else "") + "}"
            names = ", ".join("{" + f + "}" for f in allowed)
            raise NetbootError(f"{label} has an unknown placeholder {raw}. "
                               f"Placeholders: {names}. {braces}")
        used.add(field)
    return used


def _entry_text(raw: dict, key: str, limit: int, required: bool = True) -> str:
    label = _ENTRY_FIELDS[key]
    value = raw.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise NetbootError(f"{label} must be text.")
    value = value.strip()
    if re.search(r"[\x00-\x1f\x7f]", value):
        raise NetbootError(f"{label} must be on one line.")
    if len(value) > limit:
        raise NetbootError(f"{label} is longer than {limit} characters.")
    if required and not value:
        raise NetbootError(f"{label} is required.")
    return value


def _entry_releases(value, name: str) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise NetbootError("Releases: list at least one release.")
    if len(value) > MAX_RELEASES:
        raise NetbootError(f"Releases: at most {MAX_RELEASES} per entry.")
    out, seen = [], set()
    for item in value:
        if isinstance(item, str):
            item = {"id": item}
        if not isinstance(item, dict):
            raise NetbootError("Releases: each release is a name or an object with an \"id\".")
        unknown = sorted(set(item) - _RELEASE_FIELDS)
        if unknown:
            raise NetbootError(f"Releases: unsupported field {', '.join(unknown)} "
                               f"(a release takes {', '.join(sorted(_RELEASE_FIELDS))}).")
        try:
            rid = validate_release(str(item.get("id", "")))
        except NetbootError as e:
            raise NetbootError(f"Release '{item.get('id', '')}': {e}")
        if rid.lower() in seen:
            raise NetbootError(f"Release '{rid}' is listed twice.")
        seen.add(rid.lower())
        label = item.get("label") or f"{name} {rid}"
        if (not isinstance(label, str) or len(label) > 128
                or re.search(r"[\x00-\x1f\x7f]", label)):
            raise NetbootError(f"Release '{rid}': the label must be one line of text, "
                               "at most 128 characters.")
        rel = {"id": rid, "label": label.strip()}
        number = item.get("number")
        if number not in (None, ""):
            if not _NUMBER_RE.fullmatch(str(number)):
                raise NetbootError(f"Release '{rid}': number must be a version number "
                                   "such as 44 or 3.21.")
            rel["number"] = str(number)
        eol = item.get("eol")
        if eol not in (None, ""):
            try:
                datetime.date.fromisoformat(str(eol))
            except ValueError:
                raise NetbootError(f"Release '{rid}': eol must be a date such as 2030-06-30.")
            rel["eol"] = str(eol)
        if item.get("default") not in (None, False, True):
            raise NetbootError(f"Release '{rid}': default must be true or false.")
        if item.get("default"):
            rel["default"] = True
        if item.get("prerelease") not in (None, False, True):
            raise NetbootError(f"Release '{rid}': prerelease must be true or false.")
        if item.get("prerelease"):
            rel["prerelease"] = True
        out.append(rel)
    defaults = [r for r in out if r.get("default")]
    if len(defaults) > 1:
        raise NetbootError("Releases: only one release can be the default.")
    if not defaults:
        out[0]["default"] = True
    return out


def validate_entry(raw) -> dict:
    """Check a custom entry and return it normalised, or raise NetbootError.

    Every template is filled for every release with the default mirror, and the
    result has to be a usable URL and a one-line kernel command line.
    """
    if not isinstance(raw, dict):
        raise NetbootError("An entry must be a JSON object.")
    unknown = sorted(set(raw) - set(_ENTRY_FIELDS))
    if unknown:
        raise NetbootError(f"Unsupported field {', '.join(unknown)}. Custom entries are "
                           "installers and take: " + ", ".join(_ENTRY_FIELDS) + ".")
    name = _entry_text(raw, "name", 64)
    entry_id = _entry_text(raw, "id", 32, required=False).lower() or entry_id_for(name)
    if not _ID_RE.fullmatch(entry_id):
        raise NetbootError("ID may only contain lowercase letters, digits and '-' "
                           "(for example: fedora).")
    try:
        mirror = validate_mirror(_entry_text(raw, "default_mirror", 255))
    except NetbootError as e:
        raise NetbootError(f"Default mirror: {e}")
    entry = {"id": entry_id, "name": name, "default_mirror": mirror}
    archive = _entry_text(raw, "archive_url", 512, required=False)
    kernel_url = _entry_text(raw, "kernel_url", 512, required=False)
    initrd_url = _entry_text(raw, "initrd_url", 512, required=False)
    if archive and (kernel_url or initrd_url):
        raise NetbootError("Give either an archive URL, or a kernel URL and an initrd "
                           "URL, not both.")
    if archive:
        entry["archive_url"] = archive
        for key in ("kernel_member", "initrd_member"):
            member = _entry_text(raw, key, 255).removeprefix("./")
            if (not _MEMBER_RE.fullmatch(member)
                    or ".." in member.split("/")):
                raise NetbootError(f"{_ENTRY_FIELDS[key]} must be a path inside the "
                                   "archive, such as debian-installer/amd64/linux.")
            entry[key] = member
        url_keys = ["archive_url"]
    elif kernel_url and initrd_url:
        entry["kernel_url"], entry["initrd_url"] = kernel_url, initrd_url
        url_keys = ["kernel_url", "initrd_url"]
    else:
        raise NetbootError("Give an archive URL, or both a kernel URL and an initrd URL.")
    entry["boot_args"] = _entry_text(raw, "boot_args", 1024, required=False)
    entry["releases"] = _entry_releases(raw.get("releases"), name)

    _template_fields(entry["boot_args"], _ENTRY_FIELDS["boot_args"], _ARGS_FIELDS)
    for key in url_keys:
        used = _template_fields(entry[key], _ENTRY_FIELDS[key], _URL_FIELDS)
        if len(entry["releases"]) > 1 and "version" not in used:
            raise NetbootError(f"{_ENTRY_FIELDS[key]} has no {{version}}, so every "
                               "release would boot the same files. Add {version} "
                               "where the release goes, or list one release.")
    for rel in entry["releases"]:
        for key in url_keys:
            try:
                validate_source_url(entry[key].format(mirror=mirror, version=rel["id"]))
            except NetbootError:
                raise NetbootError(f"{_ENTRY_FIELDS[key]} does not give an http:// or "
                                   f"https:// URL for release {rel['id']}.")
        if len(boot_args(entry, mirror, rel["id"])) > 2048:
            raise NetbootError("Kernel arguments are longer than 2048 characters once "
                               "filled in.")
    return entry


def parse_releases(text: str) -> list[dict]:
    """Releases typed one per line, the first being the default: "44" or
    "44 | Fedora 44"."""
    out = []
    for line in text.splitlines():
        rid, _, label = line.partition("|")
        if rid.strip():
            out.append({"id": rid.strip(), "label": label.strip()} if label.strip()
                       else {"id": rid.strip()})
    return out


def release_lines(entry: dict) -> str:
    """An entry's releases as parse_releases reads them, default first."""
    releases = sorted(entry.get("releases", []), key=lambda r: not r.get("default"))
    lines = []
    for r in releases:
        auto = r["label"] == f"{entry['name']} {r['id']}"
        lines.append(r["id"] if auto else f"{r['id']} | {r['label']}")
    return "\n".join(lines)


def _custom_rows(db):
    return db.query(CatalogEntry).order_by(CatalogEntry.id).all()


def _custom_from_row(row) -> dict | None:
    try:
        entry = json.loads(row.data)
    except ValueError:
        log.warning("Custom catalog entry %s is not valid JSON; skipped", row.id)
        return None
    if not isinstance(entry, dict):
        return None
    entry.update(id=row.id, custom=True)
    for rel in entry.get("releases", []):
        rel["ended"] = _past(rel.get("eol"))
    return entry


def custom_entries(db) -> list[dict]:
    """The custom entries, sorted by name."""
    entries = [e for e in map(_custom_from_row, _custom_rows(db)) if e]
    return sorted(entries, key=lambda e: e.get("name", "").lower())


def custom_entry(db, entry_id: str) -> dict | None:
    row = db.get(CatalogEntry, entry_id)
    return _custom_from_row(row) if row else None


def builtin_ids() -> set[str]:
    return {d["id"] for d in load_catalog()}


def free_entry_id(db, base: str) -> str:
    """base, or base-2, base-3, ... if a built-in or custom entry has it."""
    taken = builtin_ids()
    candidate, n = base, 1
    while candidate in taken or db.get(CatalogEntry, candidate) is not None:
        n += 1
        suffix = f"-{n}"
        candidate = base[:32 - len(suffix)] + suffix
    return candidate


def store_entry(db, entry: dict) -> None:
    """Save a validated entry under its ID, replacing one with the same ID."""
    data = json.dumps({k: v for k, v in entry.items() if k not in ("id", "custom")})
    row = db.get(CatalogEntry, entry["id"])
    if row is None:
        db.add(CatalogEntry(id=entry["id"], data=data))
    else:
        row.data = data
    db.commit()


def delete_entry(db, entry_id: str) -> bool:
    row = db.get(CatalogEntry, entry_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def parse_import(text: str) -> list[dict]:
    """The entries in an imported file, all validated, or NetbootError naming
    every entry that is wrong (then nothing is imported).

    Takes netboot_catalog.json's own layout ({"distros": [...]}), a list of
    entries, or a single entry.
    """
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as e:
        # Very deeply nested JSON ([[[[...) exhausts the parser's recursion
        # limit instead of raising ValueError.
        if isinstance(e, RecursionError):
            raise NetbootError("The file is not valid JSON (it is nested too deeply).")
        raise NetbootError(f"The file is not valid JSON ({e}).")
    if isinstance(data, dict) and "distros" in data:
        items = data["distros"]
    elif isinstance(data, dict):
        items = [data]
    else:
        items = data
    if not isinstance(items, list) or not items:
        raise NetbootError("The file holds no entries. It should be an entry, a list of "
                           "entries, or {\"distros\": [...]} as in the built-in catalog.")
    if len(items) > MAX_CUSTOM_ENTRIES:
        raise NetbootError(f"The file holds more than {MAX_CUSTOM_ENTRIES} entries.")
    entries, problems = [], []
    for n, raw in enumerate(items, 1):
        named = raw.get("name") if isinstance(raw, dict) else None
        label = named.strip() if isinstance(named, str) and named.strip() else f"Entry {n}"
        try:
            entries.append(validate_entry(raw))
        except NetbootError as e:
            problems.append(f"{label}: {e}")
    ids = [e["id"] for e in entries]
    problems += [f"ID {i} is used by more than one entry in the file."
                 for i in sorted({i for i in ids if ids.count(i) > 1})]
    if problems:
        raise NetbootError("Nothing was imported. " + " ".join(problems))
    return entries


def boot_file_urls(entry: dict, mirror: str, release: str) -> list[tuple[str, str]]:
    """(label, URL) for each file the entry downloads for a release."""
    entry = for_release(entry, release)
    if entry.get("kernel_url"):
        return [("Kernel", entry["kernel_url"].format(mirror=mirror, version=release)),
                ("Initrd", entry["initrd_url"].format(mirror=mirror, version=release))]
    return [("Archive", archive_url(entry, mirror, release))]


def check_url(url: str) -> dict:
    """Whether a URL answers, without downloading it: {"ok": True/False/None,
    "text": ...}. None means it could not be checked (no internet, say)."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "beacon-pxe"})
    try:
        with urllib.request.urlopen(req, timeout=_DISCOVERY_TIMEOUT) as resp:
            size = resp.headers.get("Content-Length", "")
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return {"ok": False, "text": f"not found (the server answered {exc.code})"}
        return {"ok": None, "text": f"could not check (the server answered {exc.code})"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": None, "text": f"could not check ({getattr(exc, 'reason', exc)})"}
    if size.isdigit():
        return {"ok": True, "text": f"found ({int(size) / 1024 / 1024:.1f} MB)"}
    return {"ok": True, "text": "found"}


def preview_entry(entry: dict) -> list[dict]:
    """What the entry gives for each release with its default mirror: the files
    it downloads (each checked on the mirror) and the kernel command line the
    boot menu will carry."""
    mirror = entry["default_mirror"]
    rows = [{"release": r, "files": boot_file_urls(entry, mirror, r["id"]),
             "boot_args": boot_args(entry, mirror, r["id"])}
            for r in entry["releases"]]
    urls = [url for row in rows for _, url in row["files"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        checks = dict(zip(urls, pool.map(check_url, urls)))
    for row in rows:
        row["files"] = [{"label": label, "url": url, "check": checks[url]}
                        for label, url in row["files"]]
    return rows


def _is_kernel(path: Path) -> bool:
    """True for an x86 Linux kernel (bzImage): "HdrS" at offset 0x202."""
    try:
        with open(path, "rb") as f:
            f.seek(0x202)
            return f.read(4) == b"HdrS"
    except OSError:
        return False


def fetch_boot_files(entry: dict, mirror: str, release: str, dest_dir: Path) -> int:
    """Download an entry's kernel and initrd into dest_dir; returns their size.

    A built-in entry can also name extra files kept beside them: the root
    filesystem of a live installer (Arch, openSUSE Leap 16), Alpine's modloop, or
    an EL installer's own runtime image.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    (_, kernel), (_, initrd) = boot_file_urls(entry, mirror, release)
    files = [(kernel, dest_dir / "vmlinuz", ARCHIVE_MAX_BYTES),
             (initrd, dest_dir / "initrd", ARCHIVE_MAX_BYTES)]
    for extra in for_release(entry, release).get("extra_files", []):
        files.append((extra["url"].format(mirror=mirror, version=release),
                      dest_dir / extra["dest"], LIVE_ISO_MAX_BYTES))
    total = 0
    for url, dest, cap in files:
        dest.parent.mkdir(parents=True, exist_ok=True)
        download(url, dest, cap)
        size = dest.stat().st_size
        if not size:
            raise NetbootError(f"{url} is empty.")
        total += size
        if dest.name == "vmlinuz" and not _is_kernel(dest):
            raise NetbootError(f"{kernel} is not a Linux kernel. Check the entry's "
                               "kernel URL.")
    return total
