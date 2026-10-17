"""Netboot-archive images: catalog helpers, archive unpacking, add-image route."""
import io
import tarfile

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
        assert ids == ["forky", "trixie", "bookworm"]
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
