"""Netboot-archive images: catalog helpers, archive unpacking, add-image route."""
import io
import json
import re
import tarfile
import urllib.error

import pytest
from sqlalchemy import select

from app import config
from app.models import Image
from app.services import images as image_svc
from app.services import netboot
import app.db as app_db

from .test_routes import _login


def _debian():
    entry = netboot.catalog_entry("debian")
    assert entry is not None
    return entry


def _archive(path, members):
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


class TestCatalog:
    def test_debian_archive_url(self):
        url = netboot.archive_url(_debian(), "https://deb.debian.org/debian", "trixie")
        assert url == ("https://deb.debian.org/debian/dists/trixie/main/"
                       "installer-amd64/current/images/netboot/netboot.tar.gz")

    def test_boot_args_point_installer_at_mirror(self):
        args = netboot.boot_args(_debian(), "http://mirror.lan:3142/debian", "trixie")
        assert args == ("mirror/country=manual mirror/protocol=http "
                        "mirror/http/hostname=mirror.lan:3142 "
                        "mirror/http/directory=/debian")

    @pytest.mark.parametrize("mirror", [
        "ftp://x/debian", "deb.debian.org/debian", "https://x/de bian",
        "https://x/debian?a=b", "https://x/debian\nmirror/foo=1", ""])
    def test_bad_mirror_rejected(self, mirror):
        with pytest.raises(netboot.NetbootError):
            netboot.validate_mirror(mirror)

    def test_mirror_trailing_slash_dropped(self):
        assert netboot.validate_mirror(" https://x/debian/ ") == "https://x/debian"

    @pytest.mark.parametrize("release", ["", "a b", "../x", "trixie\nfoo=1"])
    def test_bad_release_rejected(self, release):
        with pytest.raises(netboot.NetbootError):
            netboot.validate_release(release)


class TestExtract:
    def test_keeps_only_kernel_and_initrd(self, tmp_path):
        archive = tmp_path / "n.tar.gz"
        _archive(archive, {
            "./debian-installer/amd64/linux": b"KERNEL",
            "./debian-installer/amd64/initrd.gz": b"INITRD",
            "./debian-installer/amd64/pxelinux.0": b"OTHER",
        })
        dest = tmp_path / "out"
        netboot.extract_boot_files(archive, _debian(), dest)
        assert sorted(p.name for p in dest.iterdir()) == ["initrd", "vmlinuz"]
        assert (dest / "vmlinuz").read_bytes() == b"KERNEL"
        assert (dest / "initrd").read_bytes() == b"INITRD"

    def test_wrong_archive_reports_missing(self, tmp_path):
        archive = tmp_path / "n.tar.gz"
        _archive(archive, {"./other/file": b"x"})
        with pytest.raises(netboot.NetbootError, match="does not contain"):
            netboot.extract_boot_files(archive, _debian(), tmp_path / "out")

    def test_archive_of_another_release_is_refused(self, tmp_path, monkeypatch):
        archive = tmp_path / "netboot.tar.gz"
        _archive(archive, {"./version.info": b"Debian version:  12 (bookworm)\n",
                           "./debian-installer/amd64/linux": b"K",
                           "./debian-installer/amd64/initrd.gz": b"I"})
        monkeypatch.setattr(netboot, "_suite_info",
                            lambda url, timeout=30: {"Codename": "forky"})
        netboot.extract_boot_files(archive, _debian(), tmp_path / "ok", "bookworm")
        with pytest.raises(netboot.NetbootError, match="bookworm installer, not trixie"):
            netboot.extract_boot_files(archive, _debian(), tmp_path / "bad", "trixie")

    def test_testing_archive_naming_the_last_release_is_accepted(self, tmp_path, monkeypatch):
        archive = tmp_path / "netboot.tar.gz"
        _archive(archive, {"./version.info": b"Debian version:  13 (trixie)\n",
                           "./debian-installer/amd64/linux": b"K",
                           "./debian-installer/amd64/initrd.gz": b"I"})
        monkeypatch.setattr(netboot, "_suite_info",
                            lambda url, timeout=30: {"Codename": "forky"})
        netboot.extract_boot_files(archive, _debian(), tmp_path / "a", "forky")
        netboot.extract_boot_files(archive, _debian(), tmp_path / "b", "sid")

    def test_iso_uploaded_as_archive_says_so(self, tmp_path):
        iso = tmp_path / "x.tar.gz"
        iso.write_bytes(b"\0" * 32769 + b"CD001" + b"\0" * 100)
        with pytest.raises(netboot.NetbootError, match="ISO image, not a netboot archive"):
            netboot.extract_boot_files(iso, _debian(), tmp_path / "out")

    def test_not_an_archive(self, tmp_path):
        bad = tmp_path / "n.tar.gz"
        bad.write_bytes(b"not a tarball")
        with pytest.raises(netboot.NetbootError, match="Could not read"):
            netboot.extract_boot_files(bad, _debian(), tmp_path / "out")


class TestProcess:
    def test_uploaded_archive_becomes_ready_image(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path / "images")
        (tmp_path / "images").mkdir()
        _archive(tmp_path / "images" / "a.tar.gz", {
            "./debian-installer/amd64/linux": b"K",
            "./debian-installer/amd64/initrd.gz": b"I"})
        db = app_db.SessionLocal()
        img = Image(name="d", filename="a.tar.gz", source="netboot",
                    catalog_id="debian", release="trixie",
                    mirror="https://deb.debian.org/debian", status="pending")
        db.add(img)
        db.commit()
        image_id = img.id
        db.close()
        # process_image opens its own session; point it at the test database.
        monkeypatch.setattr(image_svc, "SessionLocal", app_db.SessionLocal)
        image_svc.process_image(image_id)
        db = app_db.SessionLocal()
        img = db.get(Image, image_id)
        assert img.status == "ready", img.message
        assert img.kernel_path == f"os/{image_id}/vmlinuz"
        assert "mirror/https/hostname=deb.debian.org" in img.boot_args
        assert (config.BOOTROOT_DIR / img.kernel_path).read_bytes() == b"K"
        db.close()


class TestRoute:
    def test_add_from_mirror_queues_download(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path)
        queued = []
        monkeypatch.setattr(image_svc, "process_image", queued.append)
        _login(client)
        r = client.post("/images/netboot", data={
            "distro": "debian", "release": "trixie",
            "mirror": "https://deb.debian.org/debian/", "source": "mirror"},
            follow_redirects=False)
        assert r.status_code == 303
        db = app_db.SessionLocal()
        img = db.execute(select(Image)).scalar_one()
        assert queued == [img.id]
        assert img.source == "netboot" and img.status == "pending"
        assert img.mirror == "https://deb.debian.org/debian"
        assert img.source_url.endswith("/trixie/main/installer-amd64/current/"
                                       "images/netboot/netboot.tar.gz")
        db.close()

    @pytest.mark.parametrize("text", ["12", "Debian 12", "bookworm", "BOOKWORM",
                                      "Debian 12 (bookworm)"])
    def test_release_accepts_number_name_or_label(self, text):
        assert netboot.resolve_release(_debian(), text) == "bookworm"

    def test_release_marked_ended_after_its_eol_date(self):
        debian = netboot.catalog_entry("debian")
        ended = {r["id"]: r["ended"] for r in debian["releases"]}
        assert ended["bullseye"] is True
        assert ended["bookworm"] is False
        assert ended["trixie"] is False
        assert "ended" in debian["live"]["releases"][0]

    def test_eol_date_missing_or_bad_is_not_ended(self):
        assert netboot._past(None) is False
        assert netboot._past("not-a-date") is False
        assert netboot._past("2000-01-01") is True

    def test_release_unknown_name_passes_through(self):
        assert netboot.resolve_release(_debian(), " sid ") == "sid"

    def test_route_maps_number_to_release(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path)
        monkeypatch.setattr(image_svc, "process_image", lambda image_id: None)
        _login(client)
        client.post("/images/netboot", data={
            "distro": "debian", "release": "14", "source": "mirror"})
        db = app_db.SessionLocal()
        img = db.execute(select(Image)).scalar_one()
        assert img.release == "forky" and "dists/forky/" in img.source_url
        db.close()

    def test_default_name_carries_the_version_number(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path)
        monkeypatch.setattr(image_svc, "process_image", lambda image_id: None)
        _login(client)
        client.post("/images/netboot", data={
            "distro": "debian", "release": "bullseye", "source": "mirror"})
        db = app_db.SessionLocal()
        img = db.execute(select(Image)).scalar_one()
        assert img.name == "Debian 11 bullseye (netboot)"
        db.close()

    def test_unlisted_number_is_read_from_the_mirror(self, monkeypatch):
        suites = {"stable": b"Version: 15.2\nCodename: duke\n", "oldstable": b"Version: 14.9\nCodename: forky\n",
                  "oldoldstable": b"Version: 13.7\nCodename: trixie\n",
                  "testing": b"Codename: sid2\n"}
        monkeypatch.setattr(netboot.urllib.request, "urlopen", lambda req, **k: _Resp(
            next(v for key, v in suites.items() if f"/{key}/" in req.full_url)))
        e = _debian()
        assert netboot.lookup_codename(e, "https://m", "15") == "duke"
        assert netboot.lookup_codename(e, "https://m", "Debian 13") == "trixie"
        assert netboot.lookup_codename(e, "https://m", "16") == "sid2"
        assert netboot.lookup_codename(e, "https://m", "20") is None
        assert netboot.lookup_codename(e, "https://m", "sid") is None
        assert netboot.lookup_release(e, "https://m", "Debian duke")["number"] == "15"

    def test_route_unknown_number_shows_error(self, client, monkeypatch):
        monkeypatch.setattr(netboot, "lookup_codename", lambda *a: None)
        _login(client)
        r = client.post("/images/netboot", data={
            "distro": "debian", "release": "99", "source": "mirror"})
        assert "No release numbered 99" in r.text

    def test_bad_release_shows_error_and_adds_nothing(self, client):
        _login(client)
        r = client.post("/images/netboot", data={
            "distro": "debian", "release": "a b", "source": "mirror"})
        assert r.status_code == 200
        assert "Release may only contain" in r.text
        db = app_db.SessionLocal()
        assert db.execute(select(Image)).first() is None
        db.close()

    def test_upload_requires_file(self, client):
        _login(client)
        r = client.post("/images/netboot", data={
            "distro": "debian", "release": "trixie", "source": "upload"})
        assert "Choose an archive file" in r.text

    def test_images_page_shows_form(self, client):
        _login(client)
        r = client.get("/images")
        assert "Add from a netboot catalog" in r.text


LISTING = ('<a href="debian-live-13.6.0-amd64-kde.iso">x</a>'
           '<a href="debian-live-13.7.0-amd64-kde.iso">x</a>'
           '<a href="debian-live-13.7.0-amd64-kde.iso.sha256">x</a>'
           '<a href="debian-live-13.7.0-amd64-xfce.iso">x</a>')


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestLive:
    def _live(self):
        return netboot.live_entry(_debian())

    def test_resolves_newest_iso_for_flavour(self, monkeypatch):
        monkeypatch.setattr(netboot.urllib.request, "urlopen",
                            lambda *a, **k: _Resp(LISTING.encode()))
        url = netboot.resolve_live_iso_url(
            self._live(), "https://cdimage.debian.org/debian-cd/x/", "kde")
        assert url == "https://cdimage.debian.org/debian-cd/x/debian-live-13.7.0-amd64-kde.iso"

    def test_unknown_flavour_in_listing(self, monkeypatch):
        monkeypatch.setattr(netboot.urllib.request, "urlopen",
                            lambda *a, **k: _Resp(LISTING.encode()))
        with pytest.raises(netboot.NetbootError, match="No live ISO"):
            netboot.resolve_live_iso_url(self._live(), "https://x", "gnome")

    def test_current_release_is_default_and_unknown_rejected(self):
        assert netboot.validate_live_release(self._live(), "")["id"] == "trixie"
        assert netboot.validate_live_release(self._live(), "bookworm")["name"] == "Debian 12"
        with pytest.raises(netboot.NetbootError):
            netboot.validate_live_release(self._live(), "sid")

    def test_testing_release_listed_and_matches_weekly_iso_name(self, monkeypatch):
        ids = [r["id"] for r in self._live()["releases"]]
        assert ids == ["forky", "trixie", "bookworm", "bullseye"]
        monkeypatch.setattr(netboot.urllib.request, "urlopen", lambda *a, **k: _Resp(
            b'<a href="debian-live-testing-amd64-kde.iso">x</a>'))
        assert netboot.resolve_live_iso_url(self._live(), "https://m", "kde").endswith(
            "/debian-live-testing-amd64-kde.iso")

    @pytest.mark.parametrize("text", ["12", "Debian 12", "bookworm", "Debian 12 (bookworm)"])
    def test_live_release_accepts_number_or_name(self, text):
        assert netboot.validate_live_release(self._live(), text)["id"] == "bookworm"

    SUITES = {"stable": b"Version: 15.2\nCodename: duke\n", "oldstable": b"Version: 14.9\nCodename: forky\n",
              "oldoldstable": b"Version: 13.7\nCodename: trixie\n", "testing": b"Codename: sid2\n"}

    def _suites(self, monkeypatch):
        monkeypatch.setattr(netboot.urllib.request, "urlopen", lambda req, **k: _Resp(
            next(v for key, v in self.SUITES.items() if f"/{key}/" in req.full_url)))

    @pytest.mark.parametrize("text, number", [("15", "15"), ("Debian 15", "15"),
                                              ("duke", "15"), ("Debian duke", "15")])
    def test_unlisted_live_release_by_number_or_codename(self, monkeypatch, text, number):
        self._suites(monkeypatch)
        rel = netboot.live_release_from_mirror(_debian(), self._live(), text)
        assert rel["unlisted"] == number and rel["name"] == f"Debian {number}"

    def test_unlisted_testing_live_release_uses_weekly_folder(self, monkeypatch):
        self._suites(monkeypatch)
        rel = netboot.live_release_from_mirror(_debian(), self._live(), "16")
        assert "weekly-live-builds" in rel["base_url"] and "unlisted" not in rel

    def test_unlisted_live_release_not_on_mirror(self, monkeypatch):
        self._suites(monkeypatch)
        with pytest.raises(netboot.NetbootError, match="No Debian release 'sid'"):
            netboot.live_release_from_mirror(_debian(), self._live(), "sid")

    def test_unlisted_version_must_match_the_iso(self, monkeypatch):
        listing = {"current-live/amd64/iso-hybrid/": '<a href="debian-live-13.7.0-amd64-kde.iso">x</a>',
                   "archive/": '<a href="15.1.0-live/">x</a>',
                   "archive/15.1.0-live/amd64/iso-hybrid/": '<a href="debian-live-15.1.0-amd64-kde.iso">x</a>'}

        def fake(req, **k):
            return _Resp(next(v for key, v in listing.items() if req.full_url.endswith(key)).encode())
        monkeypatch.setattr(netboot.urllib.request, "urlopen", fake)
        url = netboot.resolve_unlisted_live_url(self._live(), "15", "kde")
        assert url.endswith("archive/15.1.0-live/amd64/iso-hybrid/debian-live-15.1.0-amd64-kde.iso")
        with pytest.raises(netboot.NetbootError, match="No live ISO for Debian 99"):
            netboot.resolve_unlisted_live_url(self._live(), "99", "kde")

    def test_iso_must_be_the_picked_release(self):
        live = self._live()
        forky = netboot.validate_live_release(live, "forky")
        trixie = netboot.validate_live_release(live, "trixie")
        current = "https://m/current-live/debian-live-13.7.0-amd64-kde.iso"
        weekly = "https://m/weekly/debian-live-testing-amd64-kde.iso"
        netboot.check_live_iso_matches(live, trixie, current)
        netboot.check_live_iso_matches(live, forky, weekly)
        with pytest.raises(netboot.NetbootError, match="holds Debian 13.7.0"):
            netboot.check_live_iso_matches(live, forky, current)
        with pytest.raises(netboot.NetbootError, match="holds the testing build"):
            netboot.check_live_iso_matches(live, trixie, weekly)

    def test_current_release_folder_is_the_mirror(self):
        rel = netboot.validate_live_release(self._live(), "trixie")
        assert netboot.resolve_release_folder(rel, "https://m/x/") == "https://m/x"

    def test_archived_release_folder_is_newest_point_release(self, monkeypatch):
        listing = ('<a href="12.9.0-live/">x</a><a href="12.15.0-live/">x</a>'
                   '<a href="13.7.0-live/">x</a><a href="12.15.0/">x</a>')
        monkeypatch.setattr(netboot.urllib.request, "urlopen",
                            lambda *a, **k: _Resp(listing.encode()))
        rel = netboot.validate_live_release(self._live(), "bookworm")
        assert netboot.resolve_release_folder(rel, "https://m/archive") == (
            "https://m/archive/12.15.0-live/amd64/iso-hybrid")

    def test_flavour_must_be_listed(self):
        with pytest.raises(netboot.NetbootError):
            netboot.validate_flavour(self._live(), "nope")

    def test_boot_args_fetch_squashfs_over_http(self):
        assert netboot.live_boot_args(self._live(), 7) == (
            "boot=live fetch=${boot-url}/os/7/filesystem.squashfs ip=dhcp")

    def test_process_keeps_three_files_and_deletes_iso(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path / "images")
        (tmp_path / "images").mkdir()
        iso = tmp_path / "images" / "l.iso"
        iso.write_bytes(b"iso")
        monkeypatch.setattr(image_svc, "_list_iso", lambda p: [
            "live/vmlinuz", "live/initrd.img", "live/filesystem.squashfs",
            "live/vmlinuz-6.12", "live/filesystem.packages"])

        def fake_extract(iso_path, member, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(member.encode())
        monkeypatch.setattr(image_svc, "_extract_one", fake_extract)
        db = app_db.SessionLocal()
        img = Image(name="l", filename="l.iso", source="netboot", variant="live",
                    catalog_id="debian", release="kde", status="pending")
        db.add(img)
        db.commit()
        image_id = img.id
        db.close()
        monkeypatch.setattr(image_svc, "SessionLocal", app_db.SessionLocal)
        image_svc.process_image(image_id)
        db = app_db.SessionLocal()
        img = db.get(Image, image_id)
        assert img.status == "ready", img.message
        assert img.boot_args == (
            f"boot=live fetch=${{boot-url}}/os/{image_id}/filesystem.squashfs ip=dhcp")
        d = config.BOOTROOT_DIR / "os" / str(image_id)
        assert sorted(p.name for p in d.iterdir()) == [
            "filesystem.squashfs", "initrd", "vmlinuz"]
        assert not iso.exists()
        db.close()

    def test_route_adds_live_image(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path)
        queued = []
        monkeypatch.setattr(image_svc, "process_image", queued.append)
        monkeypatch.setattr(netboot, "resolve_live_iso_url",
                            lambda live, base, flavour: f"{base}/debian-live-13.7.0-amd64-{flavour}.iso")
        _login(client)
        r = client.post("/images/netboot", data={
            "distro": "debian", "variant": "live", "flavour": "kde",
            "mirror": "", "source": "mirror"}, follow_redirects=False)
        assert r.status_code == 303
        db = app_db.SessionLocal()
        img = db.execute(select(Image)).scalar_one()
        assert queued == [img.id]
        assert img.variant == "live" and img.name == "Debian Live KDE Plasma"
        assert img.filename.endswith(".iso")
        assert img.source_url.endswith("debian-live-13.7.0-amd64-kde.iso")
        db.close()

    def test_route_names_live_image_after_its_release(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path)
        monkeypatch.setattr(image_svc, "process_image", lambda image_id: None)
        monkeypatch.setattr(netboot, "resolve_release_folder",
                            lambda rel, base: base + "/12.15.0-live/amd64/iso-hybrid")
        monkeypatch.setattr(netboot, "resolve_live_iso_url",
                            lambda live, base, flavour: f"{base}/debian-live-12.15.0-amd64-{flavour}.iso")
        _login(client)
        r = client.post("/images/netboot", data={
            "distro": "debian", "variant": "live", "flavour": "kde",
            "live_release": "bookworm", "mirror": "", "source": "mirror"},
            follow_redirects=False)
        assert r.status_code == 303
        db = app_db.SessionLocal()
        img = db.execute(select(Image)).scalar_one()
        assert img.name == "Debian 12 Live KDE Plasma"
        assert "12.15.0-live" in img.source_url
        db.close()

    def test_route_rejects_unknown_flavour(self, client):
        _login(client)
        r = client.post("/images/netboot", data={
            "distro": "debian", "variant": "live", "flavour": "nope"})
        assert "Choose one of the listed desktops" in r.text


    def test_hardlinked_kernel_uses_entry_with_data(self, client, tmp_path, monkeypatch):
        """vmlinuz can be an empty hard link to vmlinuz-<version>; take the data."""
        monkeypatch.setattr(image_svc, "_list_iso", lambda p: [
            "live/vmlinuz-6.12", "live/vmlinuz", "live/initrd.img",
            "live/filesystem.squashfs"])

        def fake_extract(iso_path, member, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"" if member == "live/vmlinuz" else b"data")
        monkeypatch.setattr(image_svc, "_extract_one", fake_extract)
        dest = tmp_path / "out"
        image_svc._extract_live_files(tmp_path / "x.iso", netboot.live_entry(_debian()), dest)
        assert (dest / "vmlinuz").read_bytes() == b"data"


class TestReleaseDiscovery:
    RELEASE = {"stable": b"Version: 15.1\nCodename: duke\n",
               "oldstable": b"Version: 14.4\nCodename: forky\n",
               "oldoldstable": b"Version: 13.9\nCodename: trixie\n",
               "testing": b"Codename: sid2\n"}
    CSV = (b"version,codename,series,created,release,eol,eol-lts,eol-elts\n"
           b"13,Trixie,trixie,2023-06-10,2025-08-09,2028-08-09,2000-06-30,2035-06-30\n"
           b"14,Forky,forky,2025-08-09,2027-08-01,2030-08-01,2099-06-30\n"
           b"15,Duke,duke,2027-08-01\n")

    def _mirror(self, monkeypatch, csv=CSV):
        def fake(req, **k):
            if req.full_url.endswith(".csv"):
                if csv is None:
                    raise netboot.urllib.error.URLError("down")
                return _Resp(csv)
            return _Resp(next(v for key, v in self.RELEASE.items()
                              if f"/{key}/" in req.full_url))
        monkeypatch.setattr(netboot.urllib.request, "urlopen", fake)

    def test_lists_what_the_mirror_carries_newest_first(self, monkeypatch):
        self._mirror(monkeypatch)
        found = netboot.discover_releases(_debian())
        assert [(f["number"], f["codename"]) for f in found] == [
            ("16", "sid2"), ("15", "duke"), ("14", "forky"), ("13", "trixie")]
        # The LTS end date is the end of support.
        assert {f["codename"]: f["eol"] for f in found} == {
            "sid2": None, "duke": None, "forky": "2099-06-30", "trixie": "2000-06-30"}

    def test_unreachable_mirror_gives_none(self, monkeypatch):
        def down(req, **k):
            raise netboot.urllib.error.URLError("down")
        monkeypatch.setattr(netboot.urllib.request, "urlopen", down)
        assert netboot.discover_releases(_debian()) is None

    def test_new_release_appears_and_ended_one_is_marked(self, monkeypatch):
        self._mirror(monkeypatch)
        entry = netboot.with_discovered(_debian(), netboot.discover_releases(_debian()))
        rel = {r["id"]: r for r in entry["releases"]}
        ids = [r["id"] for r in entry["releases"]]
        # Mirror order first, then catalog releases the mirror no longer carries.
        assert ids[:4] == ["sid2", "duke", "forky", "trixie"]
        assert "bookworm" in ids[4:] and "bullseye" in ids[4:]
        assert rel["duke"]["label"] == "Debian 15 (duke)" and rel["duke"]["default"]
        assert "default" not in rel["trixie"]
        assert rel["sid2"]["label"] == "Debian 16 (sid2, testing \u2014 pre-release)"
        assert rel["trixie"]["ended"] is True and rel["forky"]["ended"] is False
        assert rel["bullseye"]["ended"] is True

    def test_new_live_release_gets_an_iso_folder(self, monkeypatch):
        self._mirror(monkeypatch)
        entry = netboot.with_discovered(_debian(), netboot.discover_releases(_debian()))
        rel = {r["id"]: r for r in entry["live"]["releases"]}
        assert rel["duke"]["unlisted"] == "15" and rel["duke"]["base_url"]
        assert "weekly-live-builds" in rel["sid2"]["base_url"]
        # A listed release keeps its own folder.
        assert rel["trixie"]["base_url"].endswith("current-live/amd64/iso-hybrid")

    def test_catalog_dates_kept_when_dates_unreadable(self, monkeypatch):
        self._mirror(monkeypatch, csv=None)
        entry = netboot.with_discovered(_debian(), netboot.discover_releases(_debian()))
        rel = {r["id"]: r for r in entry["releases"]}
        assert rel["trixie"]["eol"] == "2030-06-30" and rel["trixie"]["ended"] is False
        assert rel["duke"].get("eol") is None and rel["duke"]["ended"] is False

    def test_page_lists_discovered_releases(self, client, monkeypatch):
        self._mirror(monkeypatch)
        found = netboot.discover_releases(_debian())
        monkeypatch.setattr(netboot, "_cached_discovery", lambda entry: found)
        _login(client)
        r = client.get("/images")
        assert "Debian 15 (duke)" in r.text

    def test_failed_lookup_is_cached_briefly(self, monkeypatch):
        calls = []
        monkeypatch.undo()
        monkeypatch.setattr(netboot, "discover_releases", lambda e: calls.append(1))
        monkeypatch.setattr(netboot, "_discovery_cache", {})
        netboot._cached_discovery(_debian())
        netboot._cached_discovery(_debian())
        assert len(calls) == 1


FEDORA = {
    "name": "Fedora",
    "default_mirror": "https://dl.fedoraproject.org/pub/fedora/linux/",
    "kernel_url": "{mirror}/releases/{version}/Everything/x86_64/os/images/pxeboot/vmlinuz",
    "initrd_url": "{mirror}/releases/{version}/Everything/x86_64/os/images/pxeboot/initrd.img",
    "boot_args": "inst.repo={mirror}/releases/{version}/Everything/x86_64/os ip=dhcp",
    "releases": ["44", {"id": "43", "label": "Fedora 43 (older)"}],
}

ARCHIVE_ENTRY = {
    "name": "My Debian",
    "default_mirror": "http://mirror.lan/debian",
    "archive_url": "{mirror}/dists/{version}/main/installer-amd64/current/images/netboot/netboot.tar.gz",
    "kernel_member": "./debian-installer/amd64/linux",
    "initrd_member": "debian-installer/amd64/initrd.gz",
    "boot_args": "mirror/country=manual mirror/http/hostname={mirror_host}",
    "releases": [{"id": "trixie", "number": "13"}],
}

# The bzImage magic the kernel check looks for.
KERNEL = b"\0" * 0x202 + b"HdrS" + b"\0" * 64


def _form(**over):
    data = {"name": "Fedora", "default_mirror": FEDORA["default_mirror"], "kind": "files",
            "kernel_url": FEDORA["kernel_url"], "initrd_url": FEDORA["initrd_url"],
            "boot_args": FEDORA["boot_args"], "releases": "44\n43 | Fedora 43 (older)"}
    data.update(over)
    return data


@pytest.fixture
def no_url_checks(monkeypatch):
    checked = []

    def check(url):
        checked.append(url)
        return {"ok": True, "text": "found (1.0 MB)"}
    monkeypatch.setattr(netboot, "check_url", check)
    return checked


class TestCustomEntryValidation:
    def test_files_entry_is_normalised(self):
        e = netboot.validate_entry(FEDORA)
        assert e["id"] == "fedora"
        assert e["default_mirror"] == "https://dl.fedoraproject.org/pub/fedora/linux"
        assert e["releases"] == [{"id": "44", "label": "Fedora 44", "default": True},
                                 {"id": "43", "label": "Fedora 43 (older)"}]
        assert netboot.boot_args(e, e["default_mirror"], "44") == (
            "inst.repo=https://dl.fedoraproject.org/pub/fedora/linux/releases/44/"
            "Everything/x86_64/os ip=dhcp")

    def test_archive_entry_keeps_members(self):
        e = netboot.validate_entry(ARCHIVE_ENTRY)
        assert e["id"] == "my-debian"
        assert e["kernel_member"] == "debian-installer/amd64/linux"
        assert e["releases"][0]["number"] == "13"

    @pytest.mark.parametrize("args, message", [
        ("ip={boot-url}", "unknown placeholder {boot-url}"),
        ("x={mirror.__class__}", "unknown placeholder"),
        ("x={mirror!r}", "unknown placeholder {mirror!r}"),
        ("x={mirror:>40}", "unknown placeholder"),
        ("x={}", "unknown placeholder {}"),
        ("x={mirror", "unmatched"),
        ("x=}", "unmatched"),
    ])
    def test_bad_boot_args_template_rejected(self, args, message):
        with pytest.raises(netboot.NetbootError, match=re.escape(message)):
            netboot.validate_entry(dict(FEDORA, boot_args=args))

    def test_ipxe_variable_written_with_doubled_braces(self):
        e = netboot.validate_entry(dict(FEDORA, boot_args="ks=${{boot-url}}/ks.cfg"))
        assert netboot.boot_args(e, e["default_mirror"], "44") == "ks=${boot-url}/ks.cfg"

    def test_url_template_takes_no_mirror_parts(self):
        with pytest.raises(netboot.NetbootError, match="Kernel URL has an unknown"):
            netboot.validate_entry(dict(FEDORA, kernel_url="{mirror_host}/vmlinuz"))

    def test_several_releases_need_version_in_urls(self):
        with pytest.raises(netboot.NetbootError, match="no {version}"):
            netboot.validate_entry(dict(FEDORA, kernel_url="{mirror}/vmlinuz"))
        netboot.validate_entry(dict(FEDORA, kernel_url="{mirror}/vmlinuz", releases=["44"]))

    def test_url_must_come_out_as_http(self):
        with pytest.raises(netboot.NetbootError, match="does not give an http"):
            netboot.validate_entry(dict(FEDORA, kernel_url="ftp://x/{version}/vmlinuz"))

    @pytest.mark.parametrize("change, message", [
        ({"archive_url": ARCHIVE_ENTRY["archive_url"]}, "not both"),
        ({"kernel_url": ""}, "both a kernel URL and an initrd URL"),
        ({"live": {}}, "Unsupported field live"),
        ({"name": ""}, "Name is required"),
        ({"name": "Fe\ndora"}, "Name must be on one line"),
        ({"default_mirror": "mirror.lan"}, "Default mirror:"),
        ({"releases": []}, "at least one release"),
        ({"releases": ["44", "44"]}, "listed twice"),
        ({"releases": ["4 4"]}, "Release '4 4'"),
        ({"releases": [{"id": "44", "default": True}, {"id": "43", "default": True}]},
         "only one release"),
        ({"releases": [{"id": "44", "eol": "soon"}]}, "eol must be a date"),
        ({"releases": [{"id": "44", "codename": "x"}]}, "unsupported field codename"),
        ({"id": "Fedora!"}, "ID may only contain"),
        ({"boot_args": "x" * 1025}, "longer than 1024"),
    ])
    def test_bad_entries_rejected(self, change, message):
        with pytest.raises(netboot.NetbootError, match=re.escape(message)):
            netboot.validate_entry(dict(FEDORA, **change))

    @pytest.mark.parametrize("member", ["../etc/passwd", "/abs/linux", "a/../../b", "a b"])
    def test_archive_member_must_stay_inside(self, member):
        with pytest.raises(netboot.NetbootError, match="path inside the archive"):
            netboot.validate_entry(dict(ARCHIVE_ENTRY, kernel_member=member))

    def test_release_lines_round_trip(self):
        e = netboot.validate_entry(dict(FEDORA, releases=[
            "43", {"id": "44", "default": True}, {"id": "42", "label": "Old one"}]))
        text = netboot.release_lines(e)
        assert text == "44\n43\n42 | Old one"
        assert netboot.parse_releases(text + "\n\n") == [
            {"id": "44"}, {"id": "43"}, {"id": "42", "label": "Old one"}]


class TestCustomEntryImport:
    def test_catalog_layout_list_and_single_entry(self):
        assert [e["id"] for e in netboot.parse_import(
            json.dumps({"distros": [FEDORA, ARCHIVE_ENTRY]}))] == ["fedora", "my-debian"]
        assert len(netboot.parse_import(json.dumps([FEDORA]))) == 1
        assert netboot.parse_import(json.dumps(FEDORA))[0]["name"] == "Fedora"

    def test_one_bad_entry_imports_nothing_and_is_named(self):
        bad = dict(ARCHIVE_ENTRY, name="Broken", boot_args="x={nope}")
        with pytest.raises(netboot.NetbootError) as exc:
            netboot.parse_import(json.dumps({"distros": [FEDORA, bad]}))
        assert str(exc.value).startswith("Nothing was imported. Broken: ")
        assert "{nope}" in str(exc.value)

    @pytest.mark.parametrize("text, message", [
        ("{not json", "not valid JSON"), ("[]", "holds no entries"),
        ('{"distros": {}}', "holds no entries"), ('"x"', "holds no entries"),
    ])
    def test_unusable_file(self, text, message):
        with pytest.raises(netboot.NetbootError, match=message):
            netboot.parse_import(text)

    def test_same_id_twice_in_file(self):
        with pytest.raises(netboot.NetbootError, match="ID fedora is used by more"):
            netboot.parse_import(json.dumps([FEDORA, FEDORA]))


class TestCustomEntryStore:
    def test_stored_entry_is_found_with_the_built_ins(self, db):
        netboot.store_entry(db, netboot.validate_entry(FEDORA))
        assert netboot.catalog_entry("fedora") is None
        e = netboot.catalog_entry("fedora", db)
        assert e["custom"] is True and e["kernel_url"] == FEDORA["kernel_url"]
        assert [d["id"] for d in netboot.page_catalog(db)] == ["debian", "fedora"]

    def test_free_id_skips_built_in_and_custom_ids(self, db):
        assert netboot.free_entry_id(db, "debian") == "debian-2"
        netboot.store_entry(db, netboot.validate_entry(dict(FEDORA, id="debian-2")))
        assert netboot.free_entry_id(db, "debian") == "debian-3"

    def test_preview_lists_every_release(self, no_url_checks):
        rows = netboot.preview_entry(netboot.validate_entry(FEDORA))
        assert [r["release"]["id"] for r in rows] == ["44", "43"]
        assert [f["label"] for f in rows[0]["files"]] == ["Kernel", "Initrd"]
        assert rows[1]["files"][0]["url"].endswith("/releases/43/Everything/x86_64/os/"
                                                   "images/pxeboot/vmlinuz")
        assert rows[1]["boot_args"].startswith("inst.repo=https://dl.fedoraproject.org")
        assert len(no_url_checks) == 4

    def test_check_url_reports_missing_file(self, monkeypatch):
        def fail(req, timeout):
            assert req.get_method() == "HEAD"
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        monkeypatch.setattr(netboot.urllib.request, "urlopen", fail)
        assert netboot.check_url("https://x/vmlinuz") == {
            "ok": False, "text": "not found (the server answered 404)"}

    def test_check_url_offline_is_not_a_failure(self, monkeypatch):
        def offline(req, timeout):
            raise urllib.error.URLError("no route")
        monkeypatch.setattr(netboot.urllib.request, "urlopen", offline)
        assert netboot.check_url("https://x/vmlinuz")["ok"] is None


class TestCustomEntryRoutes:
    def test_preview_shows_command_line_then_save_stores(self, client, no_url_checks):
        _login(client)
        r = client.post("/images/catalog/entry", data=_form(action="preview"))
        assert r.status_code == 200
        assert ("inst.repo=https://dl.fedoraproject.org/pub/fedora/linux/releases/43/"
                "Everything/x86_64/os ip=dhcp") in r.text
        assert 'id="entry-save"' in r.text
        db = app_db.SessionLocal()
        assert netboot.custom_entries(db) == []
        r = client.post("/images/catalog/entry", data=_form(action="save"),
                        follow_redirects=False)
        assert r.status_code == 303
        assert netboot.custom_entry(db, "fedora")["releases"][1]["label"] == "Fedora 43 (older)"
        db.close()
        page = client.get("/images").text
        assert "Saved the custom entry Fedora" in page
        assert '<option value="fedora">Fedora (custom)</option>' in page

    def test_bad_template_shows_error_and_no_save(self, client, no_url_checks):
        _login(client)
        r = client.post("/images/catalog/entry",
                        data=_form(action="save", boot_args="ip={ip}"))
        assert "unknown placeholder {ip}" in r.text
        assert 'id="entry-save"' not in r.text
        db = app_db.SessionLocal()
        assert netboot.custom_entries(db) == []
        db.close()

    def test_new_entry_named_like_a_built_in_gets_its_own_id(self, client):
        _login(client)
        client.post("/images/catalog/entry", data=_form(action="save", name="Debian"))
        db = app_db.SessionLocal()
        assert [e["id"] for e in netboot.custom_entries(db)] == ["debian-2"]
        assert netboot.catalog_entry("debian", db).get("custom") is None
        db.close()

    def test_edit_keeps_id_and_imported_release_fields(self, client):
        db = app_db.SessionLocal()
        netboot.store_entry(db, netboot.validate_entry(dict(FEDORA, releases=[
            {"id": "44", "number": "44", "eol": "2027-05-01"}, "43"])))
        _login(client)
        page = client.get("/images?edit=fedora").text
        assert "Edit Fedora" in page and 'name="original_id" value="fedora"' in page
        client.post("/images/catalog/entry", data=_form(
            action="save", original_id="fedora", name="Fedora Linux", releases="44\n45"))
        e = netboot.custom_entry(db, "fedora")
        assert e["name"] == "Fedora Linux"
        assert [{k: v for k, v in r.items() if k != "ended"} for r in e["releases"]] == [
            {"id": "44", "label": "Fedora Linux 44", "number": "44", "eol": "2027-05-01",
             "default": True},
            {"id": "45", "label": "Fedora Linux 45"}]
        assert len(netboot.custom_entries(db)) == 1
        db.close()

    def test_import_previews_then_saves_all(self, client, no_url_checks):
        _login(client)
        text = json.dumps({"distros": [FEDORA, ARCHIVE_ENTRY]})
        r = client.post("/images/catalog/import", data={"action": "preview"},
                        files={"file": ("mine.json", text.encode(), "application/json")})
        assert "Import 2 entries" in r.text and "mirror/http/hostname=mirror.lan" in r.text
        db = app_db.SessionLocal()
        assert netboot.custom_entries(db) == []
        r = client.post("/images/catalog/import", data={"action": "save", "content": text},
                        follow_redirects=False)
        assert r.status_code == 303
        assert [e["id"] for e in netboot.custom_entries(db)] == ["fedora", "my-debian"]
        db.close()
        assert "Imported 2 custom entries: Fedora, My Debian." in client.get("/images").text

    def test_import_reimport_replaces_same_id(self, client, no_url_checks):
        db = app_db.SessionLocal()
        netboot.store_entry(db, netboot.validate_entry(FEDORA))
        _login(client)
        text = json.dumps(dict(FEDORA, releases=["45"]))
        r = client.post("/images/catalog/import", data={"action": "preview", "content": text})
        assert "replaces the existing entry" in r.text
        client.post("/images/catalog/import", data={"action": "save", "content": text},
                    follow_redirects=False)
        assert [r["id"] for r in netboot.custom_entry(db, "fedora")["releases"]] == ["45"]
        db.close()
        assert "1 replaced an entry with the same ID" in client.get("/images").text

    def test_import_refuses_built_in_id(self, client):
        _login(client)
        r = client.post("/images/catalog/import", data={
            "action": "save", "content": json.dumps(dict(FEDORA, id="debian"))})
        assert "belongs to a built-in entry" in r.text
        db = app_db.SessionLocal()
        assert netboot.custom_entries(db) == []
        db.close()

    def test_delete(self, client):
        db = app_db.SessionLocal()
        netboot.store_entry(db, netboot.validate_entry(FEDORA))
        _login(client)
        r = client.post("/images/catalog/fedora/delete", follow_redirects=False)
        assert r.status_code == 303
        assert netboot.custom_entry(db, "fedora") is None
        db.close()
        assert "Deleted the custom entry Fedora" in client.get("/images").text

    def test_unlisted_number_release_passes_through(self, client, tmp_path, monkeypatch):
        """Fedora's releases are numbers; an unlisted one is not looked up on
        Debian-style suites."""
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path)
        monkeypatch.setattr(image_svc, "process_image", lambda i: None)
        db = app_db.SessionLocal()
        netboot.store_entry(db, netboot.validate_entry(FEDORA))
        _login(client)
        r = client.post("/images/netboot", data={
            "distro": "fedora", "release": "46", "mirror": "", "source": "mirror"},
            follow_redirects=False)
        assert r.status_code == 303
        img = db.execute(select(Image)).scalar_one()
        assert (img.catalog_id, img.release, img.source_url) == ("fedora", "46", "")
        assert img.name == "Fedora 46 (netboot)"
        db.close()

    def test_separate_files_entry_only_downloads_from_mirror(self, client):
        db = app_db.SessionLocal()
        netboot.store_entry(db, netboot.validate_entry(FEDORA))
        db.close()
        _login(client)
        r = client.post("/images/netboot", data={
            "distro": "fedora", "release": "44", "source": "url",
            "url": "https://x/netboot.tar.gz"})
        assert "can only use Download from the mirror" in r.text

    def _files_image(self, monkeypatch, files):
        db = app_db.SessionLocal()
        netboot.store_entry(db, netboot.validate_entry(FEDORA))
        img = Image(name="f", filename="netboot-fedora-44-x.tar.gz", source="netboot",
                    catalog_id="fedora", release="44",
                    mirror="http://mirror.lan/fedora", status="pending")
        db.add(img)
        db.commit()
        image_id = img.id
        db.close()
        fetched = []

        def fake_download(url, dest, max_bytes=netboot.ARCHIVE_MAX_BYTES):
            fetched.append(url)
            dest.write_bytes(files[url.rsplit("/", 1)[-1]])
        monkeypatch.setattr(netboot, "download", fake_download)
        monkeypatch.setattr(image_svc, "SessionLocal", app_db.SessionLocal)
        image_svc.process_image(image_id)
        db = app_db.SessionLocal()
        img = db.get(Image, image_id)
        db.expunge(img)
        db.close()
        return img, fetched

    def test_process_downloads_kernel_and_initrd(self, client, monkeypatch):
        img, fetched = self._files_image(
            monkeypatch, {"vmlinuz": KERNEL, "initrd.img": b"INITRD"})
        assert img.status == "ready", img.message
        assert fetched == [
            "http://mirror.lan/fedora/releases/44/Everything/x86_64/os/images/pxeboot/vmlinuz",
            "http://mirror.lan/fedora/releases/44/Everything/x86_64/os/images/pxeboot/initrd.img"]
        assert img.boot_args == ("inst.repo=http://mirror.lan/fedora/releases/44/"
                                 "Everything/x86_64/os ip=dhcp")
        assert img.size_bytes == len(KERNEL) + 6
        assert (config.BOOTROOT_DIR / img.initrd_path).read_bytes() == b"INITRD"

    def test_process_refuses_a_page_that_is_not_a_kernel(self, client, monkeypatch):
        img, _ = self._files_image(
            monkeypatch, {"vmlinuz": b"<html>moved</html>", "initrd.img": b"I"})
        assert img.status == "error"
        assert "is not a Linux kernel" in img.message

    def test_missing_files_message_says_reprocess_downloads(self, client, monkeypatch):
        img, _ = self._files_image(monkeypatch, {"vmlinuz": KERNEL, "initrd.img": b"I"})
        (config.BOOTROOT_DIR / img.kernel_path).unlink()
        db = app_db.SessionLocal()
        assert image_svc.reconcile_statuses(db) == 1
        assert "Reprocess downloads them again" in db.get(Image, img.id).message
        db.close()
