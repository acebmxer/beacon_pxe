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


MAGEIA = {
    "name": "Mageia",
    "default_mirror": "https://dl.fedoraproject.org/pub/fedora/linux/",
    "kernel_url": "{mirror}/releases/{version}/Everything/x86_64/os/images/pxeboot/vmlinuz",
    "initrd_url": "{mirror}/releases/{version}/Everything/x86_64/os/images/pxeboot/initrd.img",
    "boot_args": "inst.repo={mirror}/releases/{version}/Everything/x86_64/os ip=dhcp",
    "releases": ["44", {"id": "43", "label": "Mageia 43 (older)"}],
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
    data = {"name": "Mageia", "default_mirror": MAGEIA["default_mirror"], "kind": "files",
            "kernel_url": MAGEIA["kernel_url"], "initrd_url": MAGEIA["initrd_url"],
            "boot_args": MAGEIA["boot_args"], "releases": "44\n43 | Mageia 43 (older)"}
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
        e = netboot.validate_entry(MAGEIA)
        assert e["id"] == "mageia"
        assert e["default_mirror"] == "https://dl.fedoraproject.org/pub/fedora/linux"
        assert e["releases"] == [{"id": "44", "label": "Mageia 44", "default": True},
                                 {"id": "43", "label": "Mageia 43 (older)"}]
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
            netboot.validate_entry(dict(MAGEIA, boot_args=args))

    def test_ipxe_variable_written_with_doubled_braces(self):
        e = netboot.validate_entry(dict(MAGEIA, boot_args="ks=${{boot-url}}/ks.cfg"))
        assert netboot.boot_args(e, e["default_mirror"], "44") == "ks=${boot-url}/ks.cfg"

    def test_url_template_takes_no_mirror_parts(self):
        with pytest.raises(netboot.NetbootError, match="Kernel URL has an unknown"):
            netboot.validate_entry(dict(MAGEIA, kernel_url="{mirror_host}/vmlinuz"))

    def test_several_releases_need_version_in_urls(self):
        with pytest.raises(netboot.NetbootError, match="no {version}"):
            netboot.validate_entry(dict(MAGEIA, kernel_url="{mirror}/vmlinuz"))
        netboot.validate_entry(dict(MAGEIA, kernel_url="{mirror}/vmlinuz", releases=["44"]))

    def test_url_must_come_out_as_http(self):
        with pytest.raises(netboot.NetbootError, match="does not give an http"):
            netboot.validate_entry(dict(MAGEIA, kernel_url="ftp://x/{version}/vmlinuz"))

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
        ({"id": "Mageia!"}, "ID may only contain"),
        ({"boot_args": "x" * 1025}, "longer than 1024"),
    ])
    def test_bad_entries_rejected(self, change, message):
        with pytest.raises(netboot.NetbootError, match=re.escape(message)):
            netboot.validate_entry(dict(MAGEIA, **change))

    @pytest.mark.parametrize("member", ["../etc/passwd", "/abs/linux", "a/../../b", "a b"])
    def test_archive_member_must_stay_inside(self, member):
        with pytest.raises(netboot.NetbootError, match="path inside the archive"):
            netboot.validate_entry(dict(ARCHIVE_ENTRY, kernel_member=member))

    def test_release_lines_round_trip(self):
        e = netboot.validate_entry(dict(MAGEIA, releases=[
            "43", {"id": "44", "default": True}, {"id": "42", "label": "Old one"}]))
        text = netboot.release_lines(e)
        assert text == "44\n43\n42 | Old one"
        assert netboot.parse_releases(text + "\n\n") == [
            {"id": "44"}, {"id": "43"}, {"id": "42", "label": "Old one"}]


class TestCustomEntryImport:
    def test_catalog_layout_list_and_single_entry(self):
        assert [e["id"] for e in netboot.parse_import(
            json.dumps({"distros": [MAGEIA, ARCHIVE_ENTRY]}))] == ["mageia", "my-debian"]
        assert len(netboot.parse_import(json.dumps([MAGEIA]))) == 1
        assert netboot.parse_import(json.dumps(MAGEIA))[0]["name"] == "Mageia"

    def test_one_bad_entry_imports_nothing_and_is_named(self):
        bad = dict(ARCHIVE_ENTRY, name="Broken", boot_args="x={nope}")
        with pytest.raises(netboot.NetbootError) as exc:
            netboot.parse_import(json.dumps({"distros": [MAGEIA, bad]}))
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
        with pytest.raises(netboot.NetbootError, match="ID mageia is used by more"):
            netboot.parse_import(json.dumps([MAGEIA, MAGEIA]))


class TestCustomEntryStore:
    def test_stored_entry_is_found_with_the_built_ins(self, db):
        netboot.store_entry(db, netboot.validate_entry(MAGEIA))
        assert netboot.catalog_entry("mageia") is None
        e = netboot.catalog_entry("mageia", db)
        assert e["custom"] is True and e["kernel_url"] == MAGEIA["kernel_url"]
        assert [d["id"] for d in netboot.page_catalog(db)] == [
            d["id"] for d in netboot.load_catalog()] + ["mageia"]

    def test_free_id_skips_built_in_and_custom_ids(self, db):
        assert netboot.free_entry_id(db, "debian") == "debian-2"
        netboot.store_entry(db, netboot.validate_entry(dict(MAGEIA, id="debian-2")))
        assert netboot.free_entry_id(db, "debian") == "debian-3"

    def test_preview_lists_every_release(self, no_url_checks):
        rows = netboot.preview_entry(netboot.validate_entry(MAGEIA))
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
        assert netboot.custom_entry(db, "mageia")["releases"][1]["label"] == "Mageia 43 (older)"
        db.close()
        page = client.get("/images").text
        assert "Saved the custom entry Mageia" in page
        assert '<option value="mageia">Mageia (custom)</option>' in page

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
        netboot.store_entry(db, netboot.validate_entry(dict(MAGEIA, releases=[
            {"id": "44", "number": "44", "eol": "2027-05-01"}, "43"])))
        _login(client)
        page = client.get("/images?edit=mageia").text
        assert "Edit Mageia" in page and 'name="original_id" value="mageia"' in page
        client.post("/images/catalog/entry", data=_form(
            action="save", original_id="mageia", name="Mageia Linux", releases="44\n45"))
        e = netboot.custom_entry(db, "mageia")
        assert e["name"] == "Mageia Linux"
        assert [{k: v for k, v in r.items() if k != "ended"} for r in e["releases"]] == [
            {"id": "44", "label": "Mageia Linux 44", "number": "44", "eol": "2027-05-01",
             "default": True},
            {"id": "45", "label": "Mageia Linux 45"}]
        assert len(netboot.custom_entries(db)) == 1
        db.close()

    def test_import_previews_then_saves_all(self, client, no_url_checks):
        _login(client)
        text = json.dumps({"distros": [MAGEIA, ARCHIVE_ENTRY]})
        r = client.post("/images/catalog/import", data={"action": "preview"},
                        files={"file": ("mine.json", text.encode(), "application/json")})
        assert "Import 2 entries" in r.text and "mirror/http/hostname=mirror.lan" in r.text
        db = app_db.SessionLocal()
        assert netboot.custom_entries(db) == []
        r = client.post("/images/catalog/import", data={"action": "save", "content": text},
                        follow_redirects=False)
        assert r.status_code == 303
        assert [e["id"] for e in netboot.custom_entries(db)] == ["mageia", "my-debian"]
        db.close()
        assert "Imported 2 custom entries: Mageia, My Debian." in client.get("/images").text

    def test_import_reimport_replaces_same_id(self, client, no_url_checks):
        db = app_db.SessionLocal()
        netboot.store_entry(db, netboot.validate_entry(MAGEIA))
        _login(client)
        text = json.dumps(dict(MAGEIA, releases=["45"]))
        r = client.post("/images/catalog/import", data={"action": "preview", "content": text})
        assert "replaces the existing entry" in r.text
        client.post("/images/catalog/import", data={"action": "save", "content": text},
                    follow_redirects=False)
        assert [r["id"] for r in netboot.custom_entry(db, "mageia")["releases"]] == ["45"]
        db.close()
        assert "1 replaced an entry with the same ID" in client.get("/images").text

    def test_import_refuses_built_in_id(self, client):
        _login(client)
        r = client.post("/images/catalog/import", data={
            "action": "save", "content": json.dumps(dict(MAGEIA, id="debian"))})
        assert "belongs to a built-in entry" in r.text
        db = app_db.SessionLocal()
        assert netboot.custom_entries(db) == []
        db.close()

    def test_delete(self, client):
        db = app_db.SessionLocal()
        netboot.store_entry(db, netboot.validate_entry(MAGEIA))
        _login(client)
        r = client.post("/images/catalog/mageia/delete", follow_redirects=False)
        assert r.status_code == 303
        assert netboot.custom_entry(db, "mageia") is None
        db.close()
        assert "Deleted the custom entry Mageia" in client.get("/images").text

    def test_unlisted_number_release_passes_through(self, client, tmp_path, monkeypatch):
        """Mageia's releases are numbers; an unlisted one is not looked up on
        Debian-style suites."""
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path)
        monkeypatch.setattr(image_svc, "process_image", lambda i: None)
        db = app_db.SessionLocal()
        netboot.store_entry(db, netboot.validate_entry(MAGEIA))
        _login(client)
        r = client.post("/images/netboot", data={
            "distro": "mageia", "release": "46", "mirror": "", "source": "mirror"},
            follow_redirects=False)
        assert r.status_code == 303
        img = db.execute(select(Image)).scalar_one()
        assert (img.catalog_id, img.release, img.source_url) == ("mageia", "46", "")
        assert img.name == "Mageia 46 (netboot)"
        db.close()

    def test_separate_files_entry_only_downloads_from_mirror(self, client):
        db = app_db.SessionLocal()
        netboot.store_entry(db, netboot.validate_entry(MAGEIA))
        db.close()
        _login(client)
        r = client.post("/images/netboot", data={
            "distro": "mageia", "release": "44", "source": "url",
            "url": "https://x/netboot.tar.gz"})
        assert "can only use Download from the mirror" in r.text

    def _files_image(self, monkeypatch, files):
        db = app_db.SessionLocal()
        netboot.store_entry(db, netboot.validate_entry(MAGEIA))
        img = Image(name="f", filename="netboot-mageia-44-x.tar.gz", source="netboot",
                    catalog_id="mageia", release="44",
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


# --- Built-in distros beyond Debian ------------------------------------------

def _entry(distro_id):
    entry = netboot.catalog_entry(distro_id)
    assert entry is not None, distro_id
    return entry


def _fill(template, **fields):
    return template.format(mirror="https://m.example/x", version="1.0", image_id=7,
                           flavour="f", iso_url="https://m.example/x.iso", **fields)


class TestBuiltInDistros:
    IDS = ["debian", "ubuntu", "fedora", "rocky", "almalinux", "centos-stream",
           "opensuse-leap", "opensuse-tumbleweed", "linuxmint", "kali", "arch", "alpine",
           "xcp-ng"]

    def test_catalog_lists_every_distro(self):
        assert [d["id"] for d in netboot.load_catalog()] == self.IDS

    @pytest.mark.parametrize("distro_id", IDS)
    def test_every_template_fills_to_an_http_url(self, distro_id):
        """A stray brace or unknown placeholder would only show up when an admin
        adds the image, so every built-in template is filled here."""
        e = _entry(distro_id)
        urls = [e[k] for k in ("archive_url", "kernel_url", "initrd_url") if k in e]
        if e.get("archive_listing"):
            urls.append(e["archive_listing"]["folder"])
        for rel in e.get("releases", []):
            for extra in netboot.for_release(e, rel["id"]).get("extra_files", []):
                urls.append(extra["url"])
                assert ".." not in extra["dest"].split("/")
            args = netboot.boot_args(e, "https://m.example/x", rel["id"], 7,
                                     {"iso_url": "https://m.example/x.iso"})
            assert "{" not in args.replace("${", "") and "\n" not in args
        live = e.get("live")
        for f in (live or {}).get("flavours", []):
            lv = netboot.live_for_flavour(live, f["id"])
            if lv.get("folder"):
                urls.append(_fill(lv["folder"], **netboot._flavour_fields(live, f["id"])))
                re.compile(lv["iso_regex"].format(
                    **netboot._flavour_fields(live, f["id"]), flavour="f", version="1"))
        for url in urls:
            netboot.validate_source_url(_fill(url))
        if live:
            assert netboot.live_boot_args(live, 7).count("/7") == 1

    def test_live_only_distro_has_no_installer(self):
        assert not netboot.has_installer(_entry("linuxmint"))
        assert netboot.has_installer(_entry("fedora"))

    def test_live_folder_entries_use_the_installer_releases(self):
        fedora = _entry("fedora")
        assert [r["id"] for r in fedora["live"]["releases"]] == [
            r["id"] for r in fedora["releases"]]

    def test_leap_15_installs_with_yast_and_16_with_agama(self):
        leap = _entry("opensuse-leap")
        old = netboot.boot_args(leap, "https://download.opensuse.org", "15.6", 3)
        new = netboot.boot_args(leap, "https://download.opensuse.org", "16.0", 3)
        assert old == "install=https://download.opensuse.org/distribution/leap/15.6/repo/oss/"
        assert new.startswith("root=live:${boot-url}/os/3/LiveOS/squashfs.img rd.live.image")
        assert netboot.for_release(leap, "15.6")["extra_files"] == []
        assert len(netboot.for_release(leap, "16.0")["extra_files"]) == 1

    def test_el_installer_keeps_its_stage2(self):
        rocky = _entry("rocky")
        args = netboot.boot_args(rocky, "https://dl.rockylinux.org/pub/rocky", "9", 4)
        assert args == ("inst.stage2=${boot-url}/os/4/stage2 inst.repo=https://"
                        "dl.rockylinux.org/pub/rocky/9/BaseOS/x86_64/os ip=dhcp")

    def test_arch_boot_args_carry_the_ipxe_mac(self):
        args = netboot.boot_args(_entry("arch"), "https://geo.mirror.pkgbuild.com", "latest", 5)
        assert args == ("archiso_http_srv=${boot-url}/os/5/ archisobasedir=arch "
                        "BOOTIF=01-${net0/mac:hexhyp} ip=dhcp")


def _listing(*names):
    return "".join(f'<a href="{n}">{n}</a>' for n in names).encode()


class TestListingDiscovery:
    def _serve(self, monkeypatch, pages):
        def fake(req, **k):
            for key, body in pages.items():
                if key in req.full_url:
                    if body is None:
                        raise netboot.urllib.error.URLError("down")
                    return _Resp(body)
            raise netboot.urllib.error.URLError("not served")
        monkeypatch.setattr(netboot.urllib.request, "urlopen", fake)

    def test_fedora_newest_two_with_bodhi_dates(self, monkeypatch):
        bodhi = json.dumps({"releases": [
            {"name": "F45", "eol": "2027-11-24"}, {"name": "F44", "eol": "2027-06-02"},
            {"name": "EPEL-9", "eol": None}]}).encode()
        # Bodhi's URL also has /releases/ in it, so it is matched first.
        self._serve(monkeypatch, {"bodhi": bodhi,
                                  "/releases/": _listing("7/", "42/", "44/", "45/", "test/")})
        found = netboot.discover_releases(_entry("fedora"))
        assert [(f["codename"], f["eol"]) for f in found] == [
            ("45", "2027-11-24"), ("44", "2027-06-02")]
        entry = netboot.with_discovered(_entry("fedora"), found)
        rel = {r["id"]: r for r in entry["releases"]}
        assert rel["45"]["label"] == "Fedora 45" and rel["45"]["default"]
        # A catalog release the listing no longer shows stays, after the others.
        assert [r["id"] for r in entry["releases"]] == ["45", "44", "43"]
        assert [r["id"] for r in entry["live"]["releases"]] == ["45", "44", "43"]

    def test_listing_with_dot_slash_links_and_minimum(self, monkeypatch):
        self._serve(monkeypatch, {"/": _listing("./8/", "./8.10/", "./9/", "./10/", "./7/")})
        found = netboot.discover_releases(_entry("rocky"))
        assert [f["codename"] for f in found] == ["10", "9", "8"]

    def test_ubuntu_default_is_newest_lts_and_dates_by_version(self, monkeypatch):
        csv_text = (b"version,codename,series,created,release,eol,eol-server\n"
                    b"26.04 LTS,Resolute Raccoon,resolute,2025-10-09,2026-04-23,2031-05-29\n"
                    b"26.10,Stonking Stingray,stonking,2026-04-24,2026-10-15,2027-07-15\n"
                    b"27.04,Tenacious T-Rex,tenacious,2026-10-16,2027-04-22,2028-01-20\n")
        self._serve(monkeypatch, {"ubuntu.csv": csv_text, "releases.ubuntu.com/": _listing(
            "22.04/", "24.04/", "26.04/", "26.10/", "27.04/", "noble/")})
        found = netboot.discover_releases(_entry("ubuntu"))
        assert [f["codename"] for f in found] == ["27.04", "26.10", "26.04", "24.04"]
        assert found[0]["eol"] == "2028-01-20"
        entry = netboot.with_discovered(_entry("ubuntu"), found)
        assert [r["id"] for r in entry["releases"] if r.get("default")] == ["26.04"]
        # 22.04 has no netboot archive, but its desktop is still listed.
        assert "22.04" not in [r["id"] for r in entry["releases"]]
        assert "22.04" in [r["id"] for r in entry["live"]["releases"]]

    def test_alpine_dates_from_releases_json(self, monkeypatch):
        rel = json.dumps({"release_branches": [{"rel_branch": "edge"},
                                               {"rel_branch": "v3.25", "eol_date": "2028-11-01"},
                                               {"rel_branch": "v3.24", "eol_date": "2028-06-01"}]})
        self._serve(monkeypatch, {"releases.json": rel.encode(), "/alpine/": _listing(
            "v3.9/", "v3.22/", "v3.23/", "v3.24/", "v3.25/", "edge/", "latest-stable/")})
        found = netboot.discover_releases(_entry("alpine"))
        assert [(f["codename"], f["eol"]) for f in found] == [
            ("3.25", "2028-11-01"), ("3.24", "2028-06-01"), ("3.23", None), ("3.22", None)]

    def test_live_only_distro_keeps_no_installer_list(self, monkeypatch):
        self._serve(monkeypatch, {"/stable/": _listing("20.3/", "21/", "22.3/", "23/")})
        entry = netboot.with_discovered(_entry("linuxmint"),
                                        netboot.discover_releases(_entry("linuxmint")))
        assert "releases" not in entry
        assert entry["live"]["releases"][0]["id"] == "23"

    def test_unreadable_listing_gives_none(self, monkeypatch):
        self._serve(monkeypatch, {"/releases/": None})
        assert netboot.discover_releases(_entry("fedora")) is None


class TestUbuntuInstaller:
    def test_archive_is_newest_point_release_in_the_folder(self, monkeypatch):
        monkeypatch.setattr(netboot.urllib.request, "urlopen", lambda *a, **k: _Resp(_listing(
            "ubuntu-24.04-netboot-amd64.tar.gz",
            "ubuntu-24.04.4-netboot-amd64.tar.gz", "ubuntu-24.04.5-netboot-amd64.tar.gz",
            "ubuntu-24.04.5-netboot-amd64.tar.gz.zsync", "ubuntu-24.04.10-wsl-amd64.wsl")))
        assert netboot.resolve_archive_url(
            _entry("ubuntu"), "https://releases.ubuntu.com", "24.04") == (
            "https://releases.ubuntu.com/24.04/ubuntu-24.04.5-netboot-amd64.tar.gz")

    def test_no_archive_for_release(self, monkeypatch):
        monkeypatch.setattr(netboot.urllib.request, "urlopen",
                            lambda *a, **k: _Resp(_listing("ubuntu-22.04.5-desktop-amd64.iso")))
        with pytest.raises(netboot.NetbootError, match="No Ubuntu 22.04 netboot archive"):
            netboot.resolve_archive_url(_entry("ubuntu"), "https://r", "22.04")

    GRUB = (b'menuentry "Install Ubuntu Server" {\n  linux linux iso-url=https://releases.'
            b'ubuntu.com/26.04.1/ubuntu-26.04.1-live-server-amd64.iso ip=dhcp ---\n}\n')

    def test_iso_url_is_read_from_the_archive_and_moved_to_the_mirror(self, tmp_path):
        archive = tmp_path / "u.tar.gz"
        _archive(archive, {"./amd64/linux": b"K", "./amd64/initrd": b"I",
                           "./amd64/grub/grub.cfg": self.GRUB})
        e = _entry("ubuntu")
        values = netboot.extract_boot_files(archive, e, tmp_path / "out", "26.04")
        assert values == {"iso_url": "https://releases.ubuntu.com/26.04.1/"
                                     "ubuntu-26.04.1-live-server-amd64.iso"}
        moved = netboot.archive_values(e, "http://mirror.lan/ubuntu-releases", values)
        assert netboot.boot_args(e, "http://mirror.lan/ubuntu-releases", "26.04", 1, moved) == (
            "iso-url=http://mirror.lan/ubuntu-releases/26.04.1/"
            "ubuntu-26.04.1-live-server-amd64.iso ip=dhcp")

    def test_archive_without_its_boot_menu_is_refused(self, tmp_path):
        archive = tmp_path / "u.tar.gz"
        _archive(archive, {"./amd64/linux": b"K", "./amd64/initrd": b"I"})
        with pytest.raises(netboot.NetbootError, match="amd64/grub/grub.cfg does not give"):
            netboot.extract_boot_files(archive, _entry("ubuntu"), tmp_path / "out", "26.04")

    def test_route_names_server_installer(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path)
        monkeypatch.setattr(image_svc, "process_image", lambda i: None)
        monkeypatch.setattr(netboot.urllib.request, "urlopen", lambda *a, **k: _Resp(
            _listing("ubuntu-26.04.1-netboot-amd64.tar.gz")))
        _login(client)
        r = client.post("/images/netboot", data={
            "distro": "ubuntu", "release": "26.04", "source": "mirror"}, follow_redirects=False)
        assert r.status_code == 303
        db = app_db.SessionLocal()
        img = db.execute(select(Image)).scalar_one()
        assert img.name == "Ubuntu 26.04 (netboot)"
        assert img.source_url.endswith("/26.04/ubuntu-26.04.1-netboot-amd64.tar.gz")
        db.close()


def _netboot_image(**fields):
    db = app_db.SessionLocal()
    img = Image(name="n", filename="x", source="netboot", status="pending", **fields)
    db.add(img)
    db.commit()
    image_id = img.id
    db.close()
    return image_id


def _processed(monkeypatch, image_id):
    monkeypatch.setattr(image_svc, "SessionLocal", app_db.SessionLocal)
    image_svc.process_image(image_id)
    db = app_db.SessionLocal()
    img = db.get(Image, image_id)
    db.expunge(img)
    db.close()
    return img


class TestExtraFiles:
    def test_arch_keeps_kernel_initrd_and_root_filesystem(self, client, monkeypatch):
        fetched = []

        def fake_download(url, dest, max_bytes=netboot.ARCHIVE_MAX_BYTES):
            fetched.append((url, max_bytes))
            dest.write_bytes(KERNEL if url.endswith("vmlinuz-linux") else b"data")
        monkeypatch.setattr(netboot, "download", fake_download)
        image_id = _netboot_image(catalog_id="arch", release="latest",
                                  mirror="https://geo.mirror.pkgbuild.com")
        img = _processed(monkeypatch, image_id)
        assert img.status == "ready", img.message
        d = config.BOOTROOT_DIR / "os" / str(image_id)
        assert (d / "arch/x86_64/airootfs.sfs").read_bytes() == b"data"
        assert (d / "arch/x86_64/airootfs.sha512").exists()
        assert [u.rsplit("/", 1)[-1] for u, _ in fetched] == [
            "vmlinuz-linux", "initramfs-linux.img", "airootfs.sfs", "airootfs.sha512"]
        # The root filesystem is far larger than a netboot archive.
        assert fetched[2][1] == netboot.LIVE_ISO_MAX_BYTES
        assert img.boot_args.startswith(f"archiso_http_srv=${{boot-url}}/os/{image_id}/ ")
        assert img.size_bytes == len(KERNEL) + 12

        (d / "arch/x86_64/airootfs.sfs").unlink()
        db = app_db.SessionLocal()
        assert image_svc.reconcile_statuses(db) == 1
        assert db.get(Image, image_id).status == image_svc.NEEDS_REPROCESS
        db.close()

    def test_installer_pointing_at_the_mirror_is_not_reported_missing(self, client, monkeypatch):
        """A netboot installer's inst.repo= is the mirror, not an unpacked tree
        on Beacon, so startup must not ask for it to be reprocessed."""
        monkeypatch.setattr(netboot, "download",
                            lambda url, dest, max_bytes=0: dest.write_bytes(KERNEL))
        image_id = _netboot_image(catalog_id="fedora", release="44",
                                  mirror="https://dl.fedoraproject.org/pub/fedora/linux")
        img = _processed(monkeypatch, image_id)
        assert img.status == "ready" and "inst.repo=https://dl.fedoraproject.org" in img.boot_args
        db = app_db.SessionLocal()
        assert image_svc.reconcile_statuses(db) == 0
        db.close()


FEDORA_SPINS = ('<a href="Fedora-Xfce-Live-43-1.6.x86_64.iso">x</a>'
                '<a href="Fedora-Sway-Live-x86_64-43-1.6.iso">x</a>'
                '<a href="Fedora-Spins-43-1.6-x86_64-CHECKSUM">x</a>')


class TestTemplateLive:
    def _fake(self, monkeypatch, pages):
        seen = []

        def fake(req, **k):
            seen.append(req.full_url)
            return _Resp(next(v for key, v in pages.items() if req.full_url.endswith(key)).encode())
        monkeypatch.setattr(netboot.urllib.request, "urlopen", fake)
        return seen

    @pytest.mark.parametrize("flavour, name", [
        ("xfce", "Fedora-Xfce-Live-43-1.6.x86_64.iso"),
        ("sway", "Fedora-Sway-Live-x86_64-43-1.6.iso")])
    def test_fedora_spin_both_file_name_styles(self, monkeypatch, flavour, name):
        seen = self._fake(monkeypatch, {"/43/Spins/x86_64/iso/": FEDORA_SPINS})
        live = _entry("fedora")["live"]
        url = netboot.resolve_flavour_iso_url(live, flavour, "https://m", "43")
        assert url == f"https://m/releases/43/Spins/x86_64/iso/{name}"
        assert seen == ["https://m/releases/43/Spins/x86_64/iso/"]

    def test_ubuntu_desktop_never_picks_a_flavour_iso(self, monkeypatch):
        self._fake(monkeypatch, {"/24.04/": '<a href="kubuntu-24.04.5-desktop-amd64.iso">x</a>'
                                            '<a href="ubuntu-24.04.5.1-desktop-amd64.iso">x</a>'
                                            '<a href="ubuntu-24.04-desktop-amd64.iso">x</a>'
                                            '<a href="ubuntu-24.04.4-desktop-amd64.iso">x</a>'})
        live = _entry("ubuntu")["live"]
        assert netboot.live_mirror(_entry("ubuntu"), live, "ubuntu") == "https://releases.ubuntu.com"
        assert netboot.live_mirror(_entry("ubuntu"), live, "kubuntu") == "https://cdimage.ubuntu.com"
        assert netboot.resolve_flavour_iso_url(
            live, "ubuntu", "https://releases.ubuntu.com", "24.04") == (
            "https://releases.ubuntu.com/24.04/ubuntu-24.04.5.1-desktop-amd64.iso")

    def test_flavour_only_offered_for_its_releases(self):
        live = _entry("rocky")["live"]
        assert netboot.validate_flavour(live, "cinnamon", "9") == "cinnamon"
        with pytest.raises(netboot.NetbootError, match="only published for release 9"):
            netboot.validate_flavour(live, "cinnamon", "10")

    def test_route_adds_fedora_spin(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path)
        monkeypatch.setattr(image_svc, "process_image", lambda i: None)
        self._fake(monkeypatch, {"/43/Spins/x86_64/iso/": FEDORA_SPINS})
        _login(client)
        r = client.post("/images/netboot", data={
            "distro": "fedora", "variant": "live", "flavour": "xfce", "live_release": "43",
            "mirror": "", "source": "mirror"}, follow_redirects=False)
        assert r.status_code == 303
        db = app_db.SessionLocal()
        img = db.execute(select(Image)).scalar_one()
        assert img.name == "Fedora 43 Live Xfce"
        assert (img.variant, img.release) == ("live", "xfce")
        assert img.mirror == "https://dl.fedoraproject.org/pub/fedora/linux"
        assert img.source_url.endswith("/43/Spins/x86_64/iso/Fedora-Xfce-Live-43-1.6.x86_64.iso")
        db.close()

    def test_route_refuses_installer_for_live_only_distro(self, client):
        _login(client)
        r = client.post("/images/netboot", data={"distro": "linuxmint", "release": "22.3"})
        assert "Linux Mint has no installer entry" in r.text

    def test_fedora_live_takes_kernel_from_the_iso(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path / "images")
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "f.iso").write_bytes(b"iso")
        monkeypatch.setattr(image_svc, "_list_iso", lambda p: [
            "boot/x86_64/loader/linux", "boot/x86_64/loader/initrd", "LiveOS/squashfs.img"])

        def fake_extract(iso_path, member, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(member.encode())
        monkeypatch.setattr(image_svc, "_extract_one", fake_extract)
        monkeypatch.setattr(netboot, "download", lambda *a, **k: pytest.fail("no download"))
        image_id = _netboot_image(catalog_id="fedora", variant="live", release="kde")
        db = app_db.SessionLocal()
        db.get(Image, image_id).filename = "f.iso"
        db.commit()
        db.close()
        img = _processed(monkeypatch, image_id)
        assert img.status == "ready", img.message
        d = config.BOOTROOT_DIR / "os" / str(image_id)
        assert (d / "vmlinuz").read_bytes() == b"boot/x86_64/loader/linux"
        assert (d / "filesystem.squashfs").read_bytes() == b"LiveOS/squashfs.img"
        assert img.boot_args == (f"root=live:${{boot-url}}/os/{image_id}/filesystem.squashfs "
                                 "rd.live.image ip=dhcp")

    def test_casper_live_is_unpacked_for_nfs(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path / "images")
        monkeypatch.setattr(config, "NFS_DIR", tmp_path / "nfs")
        (tmp_path / "images").mkdir()
        iso = tmp_path / "images" / "m.iso"
        iso.write_bytes(b"iso")
        monkeypatch.setattr(image_svc, "_list_iso", lambda p: [
            "casper/vmlinuz", "casper/initrd.lz", "casper/filesystem.squashfs"])

        def fake_extract(iso_path, member, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(member.encode())

        def fake_tree(iso_path, dest):
            (dest / "casper").mkdir(parents=True)
            (dest / "casper" / "filesystem.squashfs").write_bytes(b"root")
        monkeypatch.setattr(image_svc, "_extract_one", fake_extract)
        monkeypatch.setattr(image_svc, "_extract_tree", fake_tree)
        image_id = _netboot_image(catalog_id="linuxmint", variant="live", release="xfce")
        db = app_db.SessionLocal()
        db.get(Image, image_id).filename = "m.iso"
        db.commit()
        db.close()
        img = _processed(monkeypatch, image_id)
        assert img.status == "ready", img.message
        assert img.boot_args == (f"boot=casper netboot=nfs nfsroot=${{server-ip}}:/nfs/{image_id} "
                                 "ip=dhcp")
        assert (config.BOOTROOT_DIR / "os" / str(image_id) / "initrd").read_bytes() == (
            b"casper/initrd.lz")
        assert not (config.BOOTROOT_DIR / "os" / str(image_id) / "filesystem.squashfs").exists()
        assert not iso.exists() and img.size_bytes == 4
        assert "served over NFS" in img.message
        db = app_db.SessionLocal()
        assert image_svc.reconcile_statuses(db) == 0
        db.close()


def test_newest_centos_live_build_by_date():
    """The x86_64 in CentOS Stream's live ISO names is not a version number."""
    names = ["CentOS-Stream-Image-KDE-Live.x86_64-10-202608080808.iso",
             "CentOS-Stream-Image-KDE-Live.x86_64-10-202604020202.iso"]
    assert max(names, key=netboot._version_key) == names[0]
    assert sorted(["ubuntu-26.04.1-x", "ubuntu-26.04-netboot-amd64"],
                  key=netboot._version_key)[-1] == "ubuntu-26.04.1-x"


class TestEndOfLifeSetting:
    def _set(self, value):
        from app.store import set_setting
        db = app_db.SessionLocal()
        set_setting(db, "netboot_show_eol", value)
        db.close()

    def _page_releases(self, client, distro_id):
        page = client.get("/images").text
        catalog = json.loads(re.search(r"var catalog = (\[.*?\]);\n", page, re.S).group(1))
        d = next(x for x in catalog if x["id"] == distro_id)
        return ([r["id"] for r in d["releases"]],
                [r["id"] for r in (d.get("live") or {}).get("releases", [])])

    def test_shown_by_default(self, client):
        _login(client)
        installer, live = self._page_releases(client, "debian")
        assert "bullseye" in installer and "bullseye" in live

    def test_hidden_when_off_and_cannot_be_added(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path)
        monkeypatch.setattr(image_svc, "process_image", lambda i: None)
        _login(client)
        self._set("0")
        installer, live = self._page_releases(client, "debian")
        assert "bullseye" not in installer and "bullseye" not in live
        assert "trixie" in installer
        r = client.post("/images/netboot", data={
            "distro": "debian", "release": "bullseye", "source": "mirror"})
        assert "Debian 11 (bullseye) has reached its end of life" in r.text
        r = client.post("/images/netboot", data={
            "distro": "opensuse-leap", "release": "15.6", "source": "mirror"})
        assert "openSUSE Leap 15.6 has reached its end of life" in r.text
        db = app_db.SessionLocal()
        assert db.execute(select(Image)).first() is None
        db.close()
        r = client.post("/images/netboot", data={
            "distro": "debian", "release": "trixie", "source": "mirror"},
            follow_redirects=False)
        assert r.status_code == 303

    def test_existing_images_are_kept(self, client):
        db = app_db.SessionLocal()
        db.add(Image(name="Debian 11 bullseye (netboot)", filename="x", source="netboot",
                     catalog_id="debian", release="bullseye", status="ready"))
        db.commit()
        db.close()
        _login(client)
        self._set("0")
        assert "Debian 11 bullseye (netboot)" in client.get("/images").text

    def test_settings_checkbox_saves(self, client):
        _login(client)
        assert 'id="netboot_show_eol" name="netboot_show_eol" checked' in client.get(
            "/settings").text
        client.post("/settings", data={"server_ip": "10.0.0.5"})
        db = app_db.SessionLocal()
        from app.store import get_setting
        assert get_setting(db, "netboot_show_eol") == "0"
        db.close()


class TestArchLive:
    def test_endeavouros_newest_dated_iso(self, monkeypatch):
        monkeypatch.setattr(netboot.urllib.request, "urlopen", lambda *a, **k: _Resp(_listing(
            "EndeavourOS_Cassini_Nova-03-2023.iso", "EndeavourOS_Titan-Neo-2026.04.27.iso",
            "EndeavourOS_Titan-Nova-2026.08.15.iso", "EndeavourOS_Titan-Nova-2026.08.15.iso.sig",
            "EndeavourOS_Ganymede-2025.11.24.iso")))
        live = _entry("arch")["live"]
        assert netboot.resolve_flavour_iso_url(
            live, "endeavouros", "https://mirror.alpix.eu/endeavouros", "latest") == (
            "https://mirror.alpix.eu/endeavouros/iso/EndeavourOS_Titan-Nova-2026.08.15.iso")

    def test_cachyos_newest_build_folder(self, monkeypatch):
        pages = {"/ISO/desktop/": _listing("260426/", "260628/", "260809/"),
                 "/ISO/desktop/260809/": _listing("cachyos-desktop-linux-260809.iso",
                                                  "cachyos-desktop-linux-260809.iso.sig")}
        monkeypatch.setattr(netboot.urllib.request, "urlopen", lambda req, **k: _Resp(
            next(v for key, v in sorted(pages.items(), key=lambda kv: -len(kv[0]))
                 if req.full_url.endswith(key))))
        live = _entry("arch")["live"]
        assert netboot.resolve_flavour_iso_url(
            live, "cachyos", "https://mirror.cachyos.org", "latest") == (
            "https://mirror.cachyos.org/ISO/desktop/260809/cachyos-desktop-linux-260809.iso")

    def test_route_names_the_derivative(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path)
        monkeypatch.setattr(image_svc, "process_image", lambda i: None)
        monkeypatch.setattr(netboot, "resolve_flavour_iso_url",
                            lambda live, flavour, mirror, version: f"{mirror}/iso/e.iso")
        _login(client)
        client.post("/images/netboot", data={
            "distro": "arch", "variant": "live", "flavour": "endeavouros",
            "live_release": "latest", "mirror": "", "source": "mirror"})
        db = app_db.SessionLocal()
        img = db.execute(select(Image)).scalar_one()
        assert img.name == "EndeavourOS Live"
        assert img.mirror == "https://mirror.alpix.eu/endeavouros"
        db.close()

    def test_cachyos_keeps_archiso_layout_with_lts_kernel(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path / "images")
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "c.iso").write_bytes(b"iso")
        monkeypatch.setattr(image_svc, "_list_iso", lambda p: [
            "arch/boot/x86_64/vmlinuz-linux-cachyos", "arch/boot/x86_64/vmlinuz-linux-cachyos-lts",
            "arch/boot/x86_64/initramfs-linux-cachyos.img",
            "arch/boot/x86_64/initramfs-linux-cachyos-lts.img",
            "arch/x86_64/airootfs.sfs", "arch/x86_64/airootfs.sha512"])

        def fake_extract(iso_path, member, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(member.encode())
        monkeypatch.setattr(image_svc, "_extract_one", fake_extract)
        image_id = _netboot_image(catalog_id="arch", variant="live", release="cachyos")
        db = app_db.SessionLocal()
        db.get(Image, image_id).filename = "c.iso"
        db.commit()
        db.close()
        img = _processed(monkeypatch, image_id)
        assert img.status == "ready", img.message
        d = config.BOOTROOT_DIR / "os" / str(image_id)
        assert (d / "vmlinuz").read_bytes() == b"arch/boot/x86_64/vmlinuz-linux-cachyos-lts"
        assert (d / "arch/x86_64/airootfs.sfs").exists()
        assert (d / "arch/x86_64/airootfs.sha512").exists()
        assert not (d / "filesystem.squashfs").exists()
        assert img.boot_args.startswith(f"archiso_http_srv=${{boot-url}}/os/{image_id}/ ")
        db = app_db.SessionLocal()
        assert image_svc.reconcile_statuses(db) == 0
        db.close()


class _Stream(io.BytesIO):
    """A response that serves data, then stalls (raises) after `cut` bytes."""
    def __init__(self, data, status=200, headers=None, cut=None):
        super().__init__(data)
        self.status, self.headers, self.cut = status, headers or {}, cut

    def read(self, n=-1):
        if self.cut is not None and self.tell() >= self.cut:
            raise TimeoutError("The read operation timed out")
        if self.cut is not None:
            n = min(n, self.cut - self.tell())
        return super().read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestDownloadResume:
    DATA = bytes(range(256)) * 40

    def _server(self, monkeypatch, plan):
        """plan: one function per request, given the Range offset, returning a _Stream."""
        calls = []

        def fake(req, timeout):
            rng = req.get_header("Range")
            offset = int(rng[6:-1]) if rng else 0
            calls.append(offset)
            return plan[len(calls) - 1](offset)
        monkeypatch.setattr(netboot.urllib.request, "urlopen", fake)
        monkeypatch.setattr(netboot, "_RETRY_PAUSE", 0)
        return calls

    def test_stall_resumes_where_it_stopped(self, tmp_path, monkeypatch):
        d, n = self.DATA, len(self.DATA)
        calls = self._server(monkeypatch, [
            lambda o: _Stream(d, headers={"Content-Length": str(n)}, cut=3000),
            lambda o: _Stream(d[o:], 206, {"Content-Range": f"bytes {o}-{n - 1}/{n}"})])
        netboot.download("https://m/x.iso", tmp_path / "x.iso")
        assert calls == [0, 3000]
        assert (tmp_path / "x.iso").read_bytes() == d
        assert not (tmp_path / "x.iso.part").exists()

    def test_server_ignoring_range_starts_over(self, tmp_path, monkeypatch):
        d = self.DATA
        self._server(monkeypatch, [lambda o: _Stream(d, cut=3000), lambda o: _Stream(d)])
        netboot.download("https://m/x.iso", tmp_path / "x.iso")
        assert (tmp_path / "x.iso").read_bytes() == d

    def test_connection_closed_early_is_resumed(self, tmp_path, monkeypatch):
        d, n = self.DATA, len(self.DATA)
        calls = self._server(monkeypatch, [
            lambda o: _Stream(d[:4000], headers={"Content-Length": str(n)}),
            lambda o: _Stream(d[o:], 206, {"Content-Range": f"bytes {o}-{n - 1}/{n}"})])
        netboot.download("https://m/x.iso", tmp_path / "x.iso")
        assert calls == [0, 4000] and (tmp_path / "x.iso").read_bytes() == d

    def test_gives_up_after_the_last_try(self, tmp_path, monkeypatch):
        d = self.DATA
        calls = self._server(monkeypatch, [lambda o: _Stream(d, cut=0)] * 4)
        with pytest.raises(netboot.NetbootError, match=r"timed out \(4 tries\)"):
            netboot.download("https://m/x.iso", tmp_path / "x.iso")
        assert len(calls) == netboot.DOWNLOAD_ATTEMPTS
        assert not (tmp_path / "x.iso").exists() and not (tmp_path / "x.iso.part").exists()

    def test_http_error_is_not_retried(self, tmp_path, monkeypatch):
        calls = []

        def fake(req, timeout):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        monkeypatch.setattr(netboot.urllib.request, "urlopen", fake)
        with pytest.raises(netboot.NetbootError, match="answered 404"):
            netboot.download("https://m/x.iso", tmp_path / "x.iso")
        assert calls == [1]


class TestXcpng:
    def test_netinstall_files_laid_out_like_an_iso_image(self, client, monkeypatch):
        import gzip as _gzip
        files = {"vmlinuz": KERNEL, "install.img": b"INSTALL", "xen.gz": _gzip.compress(b"XEN")}
        fetched = []

        def fake_download(url, dest, max_bytes=netboot.ARCHIVE_MAX_BYTES):
            fetched.append(url)
            dest.write_bytes(files[url.rsplit("/", 1)[-1]])
        monkeypatch.setattr(netboot, "download", fake_download)
        built = []
        monkeypatch.setattr(image_svc, "_build_xcpng_grub",
                            lambda img, d, args, ip: built.append(args) or
                            (d / "bootx64.efi").write_bytes(b"EFI"))
        image_id = _netboot_image(catalog_id="xcp-ng", release="8.3",
                                  mirror="https://updates.xcp-ng.org")
        img = _processed(monkeypatch, image_id)
        assert img.status == "ready", img.message
        assert fetched == ["https://updates.xcp-ng.org/netinstall/8.3/boot/vmlinuz",
                           "https://updates.xcp-ng.org/netinstall/8.3/install.img",
                           "https://updates.xcp-ng.org/netinstall/8.3/boot/xen.gz"]
        d = config.BOOTROOT_DIR / "os" / str(image_id)
        assert sorted(p.name for p in d.iterdir()) == [
            "bootx64.efi", "install.img", "vmlinuz", "xen"]
        assert (d / "xen").read_bytes() == b"XEN"
        assert (img.os_family, img.kernel_path, img.initrd_path) == (
            "xcpng", f"os/{image_id}/xen", f"os/{image_id}/vmlinuz")
        assert img.boot_args == image_svc.XCPNG_DOM0_ARGS == built[0]
        assert img.name == "n"
        db = app_db.SessionLocal()
        assert image_svc.reconcile_statuses(db) == 0
        db.close()

    def test_default_name(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path)
        monkeypatch.setattr(image_svc, "process_image", lambda i: None)
        _login(client)
        client.post("/images/netboot", data={"distro": "xcp-ng", "release": "8.3",
                                             "source": "mirror"})
        db = app_db.SessionLocal()
        img = db.execute(select(Image)).scalar_one()
        assert img.name == "XCP-ng 8.3 (netboot)" and img.source_url == ""
        db.close()


# Taken before conftest's fixture stubs it for every test.
_REAL_CACHED_BUILD = netboot._cached_build


class TestCurrentBuild:
    """The newest build of each release, read from what each mirror serves
    (texts copied from the real mirrors)."""

    PAGES = {
        "https://deb.debian.org/debian/dists/trixie/Release":
            "Origin: Debian\nVersion: 13.7\nCodename: trixie\n",
        "https://deb.debian.org/debian/dists/forky/Release":
            "Origin: Debian\nSuite: testing\nCodename: forky\n",
        "https://releases.ubuntu.com/26.04/":
            '<a href="ubuntu-26.04-desktop-amd64.iso">x</a>'
            '<a href="ubuntu-26.04.1-desktop-amd64.iso">x</a>'
            '<a href="ubuntu-26.04.1-netboot-amd64.tar.gz">x</a>',
        "https://dl.rockylinux.org/pub/rocky/":
            '<a href="10.0/">10.0/</a><a href="10.1/">10.1/</a><a href="10.2/">10.2/</a>'
            '<a href="9.6/">9.6/</a><a href="10/">10/</a>',
        "https://mirror.stream.centos.org/10-stream/COMPOSE_ID":
            "CentOS-Stream-10-20261013.0\n",
        "https://download.opensuse.org/tumbleweed/repo/oss/media.1/products":
            "/ openSUSE 20261016-1\n",
        "https://http.kali.org/kali/dists/kali-rolling/Release":
            "Origin: Kali\nCodename: kali-rolling\nDate: Fri, 16 Oct 2026 00:06:16 UTC\n",
        "https://geo.mirror.pkgbuild.com/iso/latest/":
            '<a href="archlinux-2026.10.01-x86_64.iso">x</a>',
        "https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/x86_64/":
            '<a href="alpine-netboot-3.22.5-x86_64.tar.gz">x</a>'
            '<a href="alpine-netboot-3.22.6-x86_64.tar.gz">x</a>',
        "https://updates.xcp-ng.org/netinstall/8.3/.treeinfo":
            "[platform]\nname = XCP\nversion = 3.4.0\n\n[branding]\nname = XCP-ng\n"
            "version = 8.3.0\n",
    }

    @pytest.fixture(autouse=True)
    def fake_mirror(self, monkeypatch):
        monkeypatch.setattr(netboot, "_fetch_text", lambda url: self.PAGES.get(url))

    @pytest.mark.parametrize("distro,release,build", [
        ("debian", "trixie", "13.7"),
        ("ubuntu", "26.04", "26.04.1"),
        ("rocky", "10", "10.2"),
        ("centos-stream", "10", "20261013.0"),
        ("opensuse-tumbleweed", "tumbleweed", "20261016"),
        ("kali", "kali-rolling", "16 Oct 2026"),
        ("arch", "latest", "2026.10.01"),
        ("alpine", "3.22", "3.22.6"),
        ("xcp-ng", "8.3", "8.3.0"),
    ])
    def test_read_from_the_mirror(self, distro, release, build):
        assert netboot.current_build(netboot.catalog_entry(distro), release) == build

    def test_none_when_there_is_no_point_version_or_no_mirror(self):
        assert netboot.current_build(netboot.catalog_entry("debian"), "forky") is None
        assert netboot.current_build(netboot.catalog_entry("debian"), "bookworm") is None
        assert netboot.current_build(netboot.catalog_entry("fedora"), "44") is None

    def test_release_list_shows_the_newest_build(self, monkeypatch):
        monkeypatch.setattr(netboot, "_cached_build", netboot.current_build)
        catalog = {d["id"]: d for d in netboot.page_catalog()}
        ubuntu = catalog["ubuntu"]
        rel = next(r for r in ubuntu["releases"] if r["id"] == "26.04")
        assert rel["label"].endswith(" — 26.04.1") and rel["build"] == "26.04.1"
        live = next(r for r in ubuntu["live"]["releases"] if r["id"] == "26.04")
        assert live["label"].endswith(" — 26.04.1")
        debian = next(r for r in catalog["debian"]["releases"] if r["id"] == "trixie")
        assert debian["label"] == "Debian 13 (trixie) — 13.7"
        forky = next(r for r in catalog["debian"]["releases"] if r["id"] == "forky")
        assert "build" not in forky
        tw = catalog["opensuse-tumbleweed"]["releases"][0]
        assert tw["label"].endswith(" — snapshot 20261016")
        # Arch's live desktops (EndeavourOS, CachyOS) have their own builds.
        assert "build" not in catalog["arch"]["live"]["releases"][0]
        assert catalog["arch"]["releases"][0]["label"].endswith(" — 2026.10.01")
        # The catalog file itself is left as it is.
        assert netboot.catalog_entry("ubuntu")["releases"][1]["label"].endswith(")")

    def test_a_build_equal_to_the_release_is_not_repeated(self):
        entry = netboot.with_builds(netboot.catalog_entry("xcp-ng"), {"8.3": "8.3"})
        assert "build" not in entry["releases"][0]

    def test_mirror_out_of_reach_shows_the_plain_list(self, monkeypatch):
        monkeypatch.setattr(netboot, "_fetch_text", lambda url: None)
        monkeypatch.setattr(netboot, "_cached_build", netboot.current_build)
        ubuntu = {d["id"]: d for d in netboot.page_catalog()}["ubuntu"]
        assert all("build" not in r for r in ubuntu["releases"])

    def test_results_are_kept_for_a_while(self, monkeypatch):
        calls = []
        monkeypatch.setattr(netboot, "current_build",
                            lambda entry, release: calls.append(release) or "13.7")
        monkeypatch.setattr(netboot, "_build_cache", {})
        entry = netboot.catalog_entry("debian")
        assert _REAL_CACHED_BUILD(entry, "trixie") == "13.7"
        assert _REAL_CACHED_BUILD(entry, "trixie") == "13.7"
        assert calls == ["trixie"]


# Taken before conftest's fixture stubs it for every test.
_REAL_CACHED_PRERELEASES = netboot._cached_prereleases


class TestPreReleases:
    """Betas, release candidates and development releases, read from fake
    mirrors laid out as the real ones are (checked 2026-10-17)."""

    def _serve(self, monkeypatch, pages):
        TestListingDiscovery._serve(self, monkeypatch, pages)

    def _set(self, value):
        from app.store import set_setting
        db = app_db.SessionLocal()
        set_setting(db, "netboot_show_prerelease", value)
        db.close()

    def _page(self, client, distro_id):
        page = client.get("/images").text
        catalog = json.loads(re.search(r"var catalog = (\[.*?\]);\n", page, re.S).group(1))
        return next(x for x in catalog if x["id"] == distro_id)

    def test_fedora_beta_from_releases_test(self, monkeypatch):
        self._serve(monkeypatch, {"/releases/test/": _listing("44_Beta/", "45_Beta/")})
        found = netboot.discover_prereleases(_entry("fedora"))
        assert [(p["id"], p["label"]) for p in found] == [
            ("45_Beta", "Fedora 45 Beta — pre-release"), ("44_Beta", "Fedora 44 Beta "
                                                                "— pre-release")]
        entry = netboot.with_prereleases(_entry("fedora"), found)
        # 44 is out, so its leftover beta is not offered; the release stays the default.
        assert [r["id"] for r in entry["releases"]] == ["45_Beta", "44", "43"]
        assert [r["id"] for r in entry["live"]["releases"]] == ["45_Beta", "44", "43"]
        assert [r["id"] for r in entry["releases"] if r.get("default")] == ["44"]

    def test_fedora_beta_boots_from_releases_test(self):
        fedora, mirror = _entry("fedora"), "https://dl.fedoraproject.org/pub/fedora/linux"
        urls = dict(netboot.boot_file_urls(fedora, mirror, "45_Beta"))
        assert urls["Kernel"] == (f"{mirror}/releases/test/45_Beta/Everything/x86_64/os/"
                                  "images/pxeboot/vmlinuz")
        assert netboot.boot_args(fedora, mirror, "45_Beta") == (
            f"inst.repo={mirror}/releases/test/45_Beta/Everything/x86_64/os ip=dhcp")
        # A release is left as it was.
        assert dict(netboot.boot_file_urls(fedora, mirror, "44"))["Kernel"].startswith(
            f"{mirror}/releases/44/")

    def test_fedora_beta_live_iso_from_releases_test(self, monkeypatch):
        self._serve(monkeypatch, {"/releases/test/45_Beta/KDE/x86_64/iso": _listing(
            "Fedora-KDE-Desktop-Live-45_Beta-1.3.x86_64.iso",
            "Fedora-KDE-iso-45_Beta-1.3-x86_64-CHECKSUM")})
        e = _entry("fedora")
        assert netboot.resolve_release_iso_url(
            e, e["live"], "kde", e["default_mirror"], "45_Beta") == (
            "https://dl.fedoraproject.org/pub/fedora/linux/releases/test/45_Beta/KDE/x86_64/"
            "iso/Fedora-KDE-Desktop-Live-45_Beta-1.3.x86_64.iso")

    def test_rocky_betas_on_the_staging_mirror_newest_per_major(self, monkeypatch):
        self._serve(monkeypatch, {"stg/rocky/": _listing(
            "10/", "10-BETA/", "10.2-BETA/", "10.2-RC2/", "10.3-BETA/", "9.9-BETA/", "9.9/")})
        found = netboot.discover_prereleases(_entry("rocky"))
        assert [p["id"] for p in found] == ["10.3-BETA", "9.9-BETA"]
        assert found[0]["mirror"] == "https://dl.rockylinux.org/stg/rocky"
        rocky = netboot.for_release(_entry("rocky"), "10.3-BETA")
        assert rocky["default_mirror"] == "https://dl.rockylinux.org/stg/rocky"
        assert netboot.for_release(_entry("rocky"), "10")["default_mirror"] == (
            "https://dl.rockylinux.org/pub/rocky")

    def test_rocky_beta_live_iso_and_desktops_of_its_major(self, monkeypatch):
        e = _entry("rocky")
        self._serve(monkeypatch, {"stg/rocky/9.9-BETA/live/x86_64": _listing(
            "Rocky-9-XFCE-x86_64-latest.iso", "Rocky-9.9-XFCE-x86_64-20261002.0.iso")})
        lv = netboot.live_for_release(e, e["live"], "9.9-BETA")
        assert lv["default_mirror"] == "https://dl.rockylinux.org/stg/rocky"
        assert netboot.resolve_release_iso_url(
            e, e["live"], "xfce", lv["default_mirror"], "9.9-BETA").endswith(
            "/stg/rocky/9.9-BETA/live/x86_64/Rocky-9.9-XFCE-x86_64-20261002.0.iso")
        # Xfce is published for 9 and 8, so for 9.9's beta too, not for 10.3's.
        assert netboot.validate_flavour(e["live"], "xfce", "9.9-BETA") == "xfce"
        with pytest.raises(netboot.NetbootError, match="only published"):
            netboot.validate_flavour(e["live"], "xfce", "10.3-BETA")

    def test_released_point_release_hides_its_beta(self, monkeypatch):
        self._serve(monkeypatch, {"vault.almalinux.org/": _listing(
            "8.10-beta/", "9.8-beta/", "9.9-beta/", "10.2-beta/")})
        found = netboot.discover_prereleases(_entry("almalinux"))
        assert [p["id"] for p in found] == ["10.2-beta", "9.9-beta", "8.10-beta"]
        alma = netboot.with_builds(_entry("almalinux"), {"10": "10.2", "9": "9.8", "8": "8.10"})
        entry = netboot.with_prereleases(alma, found)
        assert [r["id"] for r in entry["releases"]][:2] == ["9.9-beta", "10"]
        # Alma publishes no live desktops for its betas.
        assert not any(r.get("prerelease") for r in entry["live"]["releases"])

    def test_alpine_edge_and_kali_dev(self):
        alpine, mirror = _entry("alpine"), "https://dl-cdn.alpinelinux.org/alpine"
        assert [p["id"] for p in netboot.discover_prereleases(alpine)] == ["edge"]
        urls = dict(netboot.boot_file_urls(alpine, mirror, "edge"))
        assert urls["Initrd"] == f"{mirror}/edge/releases/x86_64/netboot/initramfs-lts"
        assert netboot.for_release(alpine, "edge")["extra_files"][0]["url"] == (
            "{mirror}/edge/releases/x86_64/netboot/modloop-lts")
        assert f"alpine_repo={mirror}/edge/main " in netboot.boot_args(alpine, mirror, "edge", 3)
        assert "/v3.24/" in dict(netboot.boot_file_urls(alpine, mirror, "3.24"))["Kernel"]
        kali = netboot.discover_prereleases(_entry("kali"))
        assert [(p["id"], p["label"]) for p in kali] == [
            ("kali-dev", "Kali Linux kali-dev (development) — pre-release")]

    def test_kali_dev_installer_naming_kali_rolling_is_accepted(self, tmp_path):
        archive = tmp_path / "nb.tar.gz"
        _archive(archive, {"./version.info": b"Debian version:  kali-rolling\n",
                           "./debian-installer/amd64/linux": b"K",
                           "./debian-installer/amd64/initrd.gz": b"I"})
        netboot.extract_boot_files(archive, _entry("kali"), tmp_path / "a", "kali-dev")
        assert (tmp_path / "a" / "vmlinuz").read_bytes() == b"K"

    def test_leap_release_candidate_from_opensuse_states(self, monkeypatch):
        states = json.dumps({"Leap": [
            {"version": "16.1", "state": "RC"}, {"version": "16.0", "state": "Stable"},
            {"version": "15.5", "state": "EOL"}, {"version": "x y", "state": "Beta"}]})
        self._serve(monkeypatch, {"get.opensuse.org": states.encode()})
        found = netboot.discover_prereleases(_entry("opensuse-leap"))
        assert [(p["id"], p["label"]) for p in found] == [
            ("16.1", "openSUSE Leap 16.1 RC — pre-release")]
        entry = netboot.with_prereleases(_entry("opensuse-leap"), found)
        assert [r["id"] for r in entry["releases"]] == ["16.1", "16.0", "15.6"]

    def test_mint_beta_live_iso_from_testing(self, monkeypatch):
        self._serve(monkeypatch, {"/linuxmint/testing": _listing(
            "linuxmint-23-cinnamon-64bit-beta.iso", "linuxmint-23-xfce-64bit-beta.iso",
            "lmde-7-cinnamon-64bit-beta.iso")})
        e = _entry("linuxmint")
        found = netboot.discover_prereleases(e)
        assert [p["id"] for p in found] == ["23-beta"]
        entry = netboot.with_prereleases(e, found)
        assert entry["live"]["releases"][0]["label"] == "Linux Mint 23 Beta — pre-release"
        assert netboot.resolve_release_iso_url(
            e, e["live"], "xfce", e["default_mirror"], "23-beta") == (
            "https://mirrors.kernel.org/linuxmint/testing/linuxmint-23-xfce-64bit-beta.iso")

    def test_ubuntu_release_before_its_day_is_a_pre_release(self, monkeypatch):
        csv_text = (b"version,codename,series,created,release,eol,eol-server\n"
                    b"26.04 LTS,Resolute Raccoon,resolute,2025-10-09,2026-04-23,2031-05-29\n"
                    b"28.04 LTS,Xyz,xyz,2027-10-16,2099-04-22,2099-05-29\n")
        self._serve(monkeypatch, {"ubuntu.csv": csv_text, "releases.ubuntu.com/": _listing(
            "24.04/", "26.04/", "28.04/")})
        entry = netboot.with_discovered(_entry("ubuntu"),
                                        netboot.discover_releases(_entry("ubuntu")))
        rel = {r["id"]: r for r in entry["releases"]}
        assert rel["28.04"]["prerelease"] and rel["28.04"]["label"] == (
            "Ubuntu 28.04 — pre-release")
        # The newest LTS is the default only once it is out.
        assert [r["id"] for r in entry["releases"] if r.get("default")] == ["26.04"]
        assert not rel["26.04"].get("prerelease")

    def test_ubuntu_beta_files_used_only_when_there_are_no_final_ones(self, monkeypatch):
        ubuntu = _entry("ubuntu")
        both = _listing("ubuntu-26.10-beta-netboot-amd64.tar.gz",
                        "ubuntu-26.10-netboot-amd64.tar.gz")
        self._serve(monkeypatch, {"/26.10": both})
        assert netboot.resolve_archive_url(ubuntu, "https://releases.ubuntu.com", "26.10") == (
            "https://releases.ubuntu.com/26.10/ubuntu-26.10-netboot-amd64.tar.gz")
        self._serve(monkeypatch, {"/27.04": _listing("ubuntu-27.04-beta-netboot-amd64.tar.gz")})
        assert netboot.resolve_archive_url(ubuntu, "https://releases.ubuntu.com", "27.04") == (
            "https://releases.ubuntu.com/27.04/ubuntu-27.04-beta-netboot-amd64.tar.gz")
        # Kubuntu's beta is in releases/27.04/beta/; release/ does not exist yet.
        self._serve(monkeypatch, {"/27.04/release": None, "/27.04/beta": _listing(
            "kubuntu-27.04-beta-desktop-amd64.iso")})
        assert netboot.resolve_release_iso_url(
            ubuntu, ubuntu["live"], "kubuntu", "https://cdimage.ubuntu.com", "27.04") == (
            "https://cdimage.ubuntu.com/kubuntu/releases/27.04/beta/"
            "kubuntu-27.04-beta-desktop-amd64.iso")
        # A release with neither still says what is missing where it looked first.
        self._serve(monkeypatch, {"/26.04/release": _listing("x.iso"), "/26.04/beta": None})
        with pytest.raises(netboot.NetbootError, match="26.04/release/"):
            netboot.resolve_release_iso_url(
                ubuntu, ubuntu["live"], "kubuntu", "https://cdimage.ubuntu.com", "26.04")

    @pytest.mark.parametrize("distro_id", TestBuiltInDistros.IDS)
    def test_every_pre_release_template_fills(self, distro_id):
        e = _entry(distro_id)
        spec = e.get("prerelease") or {}
        for key, value in spec.get("overrides", {}).items():
            if key == "extra_files":
                for f in value:
                    netboot.validate_source_url(_fill(f["url"]))
            elif key == "boot_args":
                args = _fill(value.replace("{number}", "1"))
                assert "{" not in args.replace("${", "")
            else:
                netboot.validate_source_url(_fill(value.replace("{number}", "1")))
        live = spec.get("live")
        if isinstance(live, dict):
            for f in e["live"]["flavours"]:
                lv = netboot.live_for_flavour(netboot.live_for_release(
                    e, e["live"], {"fedora": "45_Beta", "rocky": "10.3-BETA",
                                   "linuxmint": "23-beta"}[distro_id]), f["id"])
                fields = netboot._flavour_fields(e["live"], f["id"])
                netboot.validate_source_url(_fill(lv["folder"], **fields))
                re.compile(lv["iso_regex"].format(**fields, flavour="f", version="1"))
        for p in spec.get("ids", []):
            assert netboot._prerelease_match(e, p["id"])

    def test_lookups_are_kept_for_a_while(self, monkeypatch):
        calls = []
        monkeypatch.setattr(netboot, "discover_prereleases",
                            lambda entry: calls.append(entry["id"]) or [])
        monkeypatch.setattr(netboot, "_prerelease_cache", {})
        assert _REAL_CACHED_PRERELEASES(_entry("fedora")) == []
        assert _REAL_CACHED_PRERELEASES(_entry("fedora")) == []
        assert _REAL_CACHED_PRERELEASES(_entry("centos-stream")) is None
        assert calls == ["fedora"]

    def test_shown_by_default(self, client):
        _login(client)
        debian = self._page(client, "debian")
        assert "forky" in [r["id"] for r in debian["releases"]]
        assert "forky" in [r["id"] for r in debian["live"]["releases"]]

    def test_hidden_when_off_and_cannot_be_added(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path)
        monkeypatch.setattr(image_svc, "process_image", lambda i: None)
        # No mirror answers: the fixed ones (Alpine's edge) are listed all the same,
        # and a typed name the distro marks as a pre-release is still known.
        self._serve(monkeypatch, {})
        monkeypatch.setattr(netboot, "_cached_prereleases", netboot.discover_prereleases)
        _login(client)
        self._set("0")
        debian = self._page(client, "debian")
        assert "forky" not in [r["id"] for r in debian["releases"]]
        assert "forky" not in [r["id"] for r in debian["live"]["releases"]]
        assert "trixie" in [r["id"] for r in debian["releases"]]
        assert "edge" not in [r["id"] for r in self._page(client, "alpine")["releases"]]
        for distro, release, name in [("debian", "forky", "Debian 14 (forky, testing"),
                                      ("debian", "sid", "Debian sid"),
                                      ("fedora", "45_Beta", "Fedora 45_Beta"),
                                      ("alpine", "edge", "Alpine Linux edge (development)")]:
            r = client.post("/images/netboot", data={
                "distro": distro, "release": release, "source": "mirror"})
            assert f"{name}" in r.text and "is a pre-release. Turn on Offer pre-releases" in r.text
        db = app_db.SessionLocal()
        assert db.execute(select(Image)).first() is None
        db.close()
        r = client.post("/images/netboot", data={
            "distro": "fedora", "release": "44", "source": "mirror"}, follow_redirects=False)
        assert r.status_code == 303

    def test_beta_added_on_its_own_mirror_with_its_name(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path)
        monkeypatch.setattr(image_svc, "process_image", lambda i: None)
        self._serve(monkeypatch, {"stg/rocky/": _listing("10.3-BETA/")})
        monkeypatch.setattr(netboot, "_cached_prereleases", netboot.discover_prereleases)
        _login(client)
        rocky = self._page(client, "rocky")
        assert rocky["releases"][0]["mirror"] == "https://dl.rockylinux.org/stg/rocky"
        # The page sends the box as it stands; the usual mirror is swapped for the beta's.
        client.post("/images/netboot", data={
            "distro": "rocky", "release": "10.3-BETA", "source": "mirror",
            "mirror": "https://dl.rockylinux.org/pub/rocky"})
        db = app_db.SessionLocal()
        img = db.execute(select(Image)).scalar_one()
        assert img.mirror == "https://dl.rockylinux.org/stg/rocky"
        assert img.name == "Rocky Linux 10.3 Beta (netboot)"
        db.close()

    def test_custom_entry_pre_release(self, db):
        entry = netboot.validate_entry({
            "name": "Thing", "default_mirror": "https://m.example/thing",
            "kernel_url": "{mirror}/{version}/vmlinuz", "initrd_url": "{mirror}/{version}/initrd",
            "releases": [{"id": "2"}, {"id": "3-rc1", "prerelease": True}]})
        assert entry["releases"][1]["prerelease"] is True
        assert netboot._without(entry, "prerelease")["releases"] == [entry["releases"][0]]
        with pytest.raises(netboot.NetbootError, match="prerelease must be true or false"):
            netboot.validate_entry(dict(entry, releases=[{"id": "2", "prerelease": "yes"}]))

    def test_settings_checkbox_saves(self, client):
        _login(client)
        assert ('id="netboot_show_prerelease" name="netboot_show_prerelease" checked'
                in client.get("/settings").text)
        client.post("/settings", data={"server_ip": "10.0.0.5"})
        db = app_db.SessionLocal()
        from app.store import get_setting
        assert get_setting(db, "netboot_show_prerelease") == "0"
        db.close()
