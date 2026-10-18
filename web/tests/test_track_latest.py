"""Track latest: a newer build of a catalog image's release becomes a new image."""
import email.utils
import json
import re
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app import config
from app.models import Image
from app.services import images as image_svc
from app.services import netboot, track_latest
from app.store import get_setting, set_setting
import app.db as app_db

from .test_netboot import KERNEL, _archive, _netboot_image, _processed
from .test_routes import _login

DEBIAN_ARCHIVE = ("https://deb.debian.org/debian/dists/trixie/main/installer-amd64/"
                  "current/images/netboot/netboot.tar.gz")
UBUNTU_FOLDER = "https://cdimage.ubuntu.com/kubuntu/releases/24.04/release"


def _image(db, **fields):
    values = dict(name="Debian 13 trixie (netboot)", filename="x", source="netboot",
                  status="ready", variant="installer", catalog_id="debian",
                  release="trixie", mirror="https://deb.debian.org/debian",
                  source_url=DEBIAN_ARCHIVE, origin="mirror",
                  build=f"100 {DEBIAN_ARCHIVE}")
    values.update(fields)
    img = Image(**values)
    db.add(img)
    db.commit()
    return img


@pytest.fixture
def mirror(monkeypatch):
    """The mirror's current builds: url -> (size, date). Records what was checked."""
    builds, checked = {}, []

    def fake_remote_build(urls, timeout=30):
        checked.extend(urls)
        for url in urls:
            if url not in builds:
                raise netboot.NetbootError(f"could not reach {url} (offline)")
        return (netboot.build_stamp([(u, builds[u][0]) for u in urls]),
                max(builds[u][1] for u in urls))
    monkeypatch.setattr(netboot, "remote_build", fake_remote_build)
    return type("Mirror", (), {"builds": builds, "checked": checked})


def _images(db):
    return db.execute(select(Image).order_by(Image.id)).scalars().all()


class TestCheck:
    def test_newer_build_is_added_beside_the_old_image(self, db, mirror):
        old = _image(db)
        mirror.builds[DEBIAN_ARCHIVE] = (200, "2026-10-15")
        added = track_latest.run_check(db)
        imgs = _images(db)
        assert added == [imgs[1].id] and len(imgs) == 2
        new = imgs[1]
        assert new.name == "Debian 13 trixie (netboot) (build 2026-10-15)"
        assert (new.status, new.source, new.variant, new.catalog_id, new.release,
                new.mirror, new.source_url, new.origin) == (
            "pending", "netboot", "installer", "debian", "trixie",
            "https://deb.debian.org/debian", DEBIAN_ARCHIVE, "mirror")
        assert new.filename != old.filename and new.filename.startswith("netboot-debian-")
        # The old image is left as it is.
        assert (old.status, old.build, old.name) == (
            "ready", f"100 {DEBIAN_ARCHIVE}", "Debian 13 trixie (netboot)")
        assert "Added 1 newer build: Debian 13 trixie (netboot) (build 2026-10-15)." == \
            get_setting(db, track_latest.RESULT_KEY)
        assert get_setting(db, track_latest.CHECKED_KEY)

    def test_same_build_adds_nothing(self, db, mirror):
        _image(db)
        mirror.builds[DEBIAN_ARCHIVE] = (100, "2026-09-01")
        assert track_latest.run_check(db) == []
        assert len(_images(db)) == 1
        assert get_setting(db, track_latest.RESULT_KEY) == "No newer builds (1 release checked)."

    def test_only_the_newest_image_of_a_release_is_compared(self, db, mirror):
        _image(db, build=f"100 {DEBIAN_ARCHIVE}")
        _image(db, name="Debian 13 trixie (netboot) (build 2026-10-15)",
               build=f"200 {DEBIAN_ARCHIVE}")
        mirror.builds[DEBIAN_ARCHIVE] = (200, "2026-10-15")
        assert track_latest.run_check(db) == []
        assert mirror.checked == [DEBIAN_ARCHIVE]

    def test_a_deleted_new_build_is_not_added_again(self, db, mirror):
        _image(db)
        mirror.builds[DEBIAN_ARCHIVE] = (200, "2026-10-15")
        [new_id] = track_latest.run_check(db)
        db.delete(db.get(Image, new_id))
        db.commit()
        assert track_latest.run_check(db) == []
        # A build newer than the deleted one is still added.
        mirror.builds[DEBIAN_ARCHIVE] = (300, "2026-11-15")
        assert len(track_latest.run_check(db)) == 1

    def test_the_new_name_replaces_an_earlier_build_date(self, db, mirror):
        _image(db, name="Debian 13 (build 2026-10-15)")
        mirror.builds[DEBIAN_ARCHIVE] = (300, "2026-11-15")
        track_latest.run_check(db)
        assert _images(db)[1].name == "Debian 13 (build 2026-11-15)"

    @pytest.mark.parametrize("origin", ["url", "upload", ""])
    def test_images_not_built_from_the_mirror_are_not_followed(self, db, mirror, origin):
        _image(db, origin=origin)
        mirror.builds[DEBIAN_ARCHIVE] = (200, "2026-10-15")
        assert track_latest.run_check(db) == []
        assert mirror.checked == []
        assert "No catalog images to follow" in get_setting(db, track_latest.RESULT_KEY)

    def test_iso_images_are_not_followed(self, db, mirror):
        _image(db, source="iso")
        assert track_latest.run_check(db) == [] and mirror.checked == []

    def test_a_release_whose_newest_image_failed_is_skipped_and_reported(self, db, mirror):
        _image(db, status="error")
        mirror.builds[DEBIAN_ARCHIVE] = (200, "2026-10-15")
        assert track_latest.run_check(db) == []
        assert mirror.checked == []
        assert "not ready" in get_setting(db, track_latest.RESULT_KEY)

    def test_a_release_still_being_built_is_skipped_quietly(self, db, mirror):
        _image(db, status="processing")
        assert track_latest.run_check(db) == [] and mirror.checked == []
        assert "Could not check" not in get_setting(db, track_latest.RESULT_KEY)

    def test_mirror_out_of_reach_is_reported(self, db, mirror):
        _image(db)
        assert track_latest.run_check(db) == []
        assert len(_images(db)) == 1
        result = get_setting(db, track_latest.RESULT_KEY)
        assert "Could not check: Debian 13 trixie (netboot): could not reach" in result

    def test_an_image_with_no_recorded_build_takes_the_current_one(self, db, mirror):
        img = _image(db, build="")
        mirror.builds[DEBIAN_ARCHIVE] = (200, "2026-10-15")
        assert track_latest.run_check(db) == []
        assert img.build == f"200 {DEBIAN_ARCHIVE}"
        mirror.builds[DEBIAN_ARCHIVE] = (300, "2026-11-15")
        assert len(track_latest.run_check(db)) == 1

    def test_kernel_url_entry_compares_kernel_and_initrd(self, db, mirror):
        base = "https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Everything/x86_64/os"
        kernel, initrd = f"{base}/images/pxeboot/vmlinuz", f"{base}/images/pxeboot/initrd.img"
        _image(db, name="Fedora 44 (netboot)", catalog_id="fedora", release="44",
               mirror="https://dl.fedoraproject.org/pub/fedora/linux", source_url="",
               build=f"10 {kernel}\n20 {initrd}")
        mirror.builds.update({kernel: (10, "2026-04-01"), initrd: (20, "2026-04-01")})
        assert track_latest.run_check(db) == []
        mirror.builds[initrd] = (21, "2026-05-01")
        assert len(track_latest.run_check(db)) == 1
        assert sorted(mirror.checked) == sorted([kernel, initrd] * 2)

    def test_point_release_of_a_live_desktop_is_found_in_the_listing(
            self, db, mirror, monkeypatch):
        old_url = f"{UBUNTU_FOLDER}/kubuntu-24.04.2-desktop-amd64.iso"
        new_url = f"{UBUNTU_FOLDER}/kubuntu-24.04.3-desktop-amd64.iso"
        monkeypatch.setattr(netboot, "_read_listing", lambda base, timeout=30: (
            '<a href="kubuntu-24.04.2-desktop-amd64.iso">x</a>'
            '<a href="kubuntu-24.04.3-desktop-amd64.iso">x</a>'))
        _image(db, name="Ubuntu 24.04 LTS Live Kubuntu", catalog_id="ubuntu",
               variant="live", release="kubuntu", live_release="24.04",
               mirror="https://cdimage.ubuntu.com", source_url=old_url,
               build=f"5000 {old_url}")
        mirror.builds[new_url] = (5100, "2026-08-07")
        [new_id] = track_latest.run_check(db)
        new = db.get(Image, new_id)
        assert (new.variant, new.release, new.live_release, new.source_url) == (
            "live", "kubuntu", "24.04", new_url)
        assert new.name == "Ubuntu 24.04 LTS Live Kubuntu (build 2026-08-07)"
        assert new.filename.endswith(".iso")

    def test_debian_live_release_is_looked_up_again(self, db, mirror, monkeypatch):
        folder = "https://cdimage.debian.org/debian-cd/current-live/amd64/iso-hybrid"
        monkeypatch.setattr(netboot, "_read_listing", lambda base, timeout=30: (
            '<a href="debian-live-13.2.0-amd64-kde.iso">x</a>'))
        _image(db, name="Debian 13 Live KDE Plasma", variant="live", release="kde",
               live_release="trixie", mirror=folder,
               source_url=f"{folder}/debian-live-13.1.0-amd64-kde.iso",
               build=f"3000 {folder}/debian-live-13.1.0-amd64-kde.iso")
        mirror.builds[f"{folder}/debian-live-13.2.0-amd64-kde.iso"] = (3100, "2026-11-08")
        [new_id] = track_latest.run_check(db)
        assert db.get(Image, new_id).source_url.endswith("debian-live-13.2.0-amd64-kde.iso")

    def test_unknown_catalog_entry_is_reported(self, db, mirror):
        _image(db, catalog_id="gone")
        assert track_latest.run_check(db) == []
        assert "no longer exists" in get_setting(db, track_latest.RESULT_KEY)

    def test_seen_builds_of_groups_with_no_image_are_forgotten(self, db, mirror):
        set_setting(db, "netboot_track_seen", json.dumps({"old|group": "1 x"}))
        _image(db)
        mirror.builds[DEBIAN_ARCHIVE] = (100, "2026-09-01")
        track_latest.run_check(db)
        assert json.loads(get_setting(db, "netboot_track_seen")) == {}


class TestRemoteBuild:
    def _urlopen(self, monkeypatch, headers):
        seen = []

        class Resp:
            def __init__(self, req):
                seen.append(req)
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        monkeypatch.setattr(netboot._HEAD_OPENER, "open", lambda req, timeout=30: Resp(req))
        return seen

    def test_size_and_date_from_a_head_request(self, monkeypatch):
        day = email.utils.format_datetime(datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc),
                                          usegmt=True)
        seen = self._urlopen(monkeypatch, {"Content-Length": "1234", "Last-Modified": day})
        assert netboot.remote_build(["https://m/a", "https://m/b"]) == (
            "1234 https://m/a\n1234 https://m/b", "2026-10-15")
        assert [r.get_method() for r in seen] == ["HEAD", "HEAD"]

    def test_no_date_still_gives_a_build(self, monkeypatch):
        self._urlopen(monkeypatch, {"Content-Length": "1"})
        assert netboot.remote_build(["https://m/a"]) == ("1 https://m/a", "")

    def test_no_size_cannot_be_compared(self, monkeypatch):
        self._urlopen(monkeypatch, {"Last-Modified": "Thu, 15 Oct 2026 08:00:00 GMT"})
        with pytest.raises(netboot.NetbootError, match="does not give the size"):
            netboot.remote_build(["https://m/a"])

    def test_not_found(self, monkeypatch):
        def fail(req, timeout=30):
            raise netboot.urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        monkeypatch.setattr(netboot._HEAD_OPENER, "open", fail)
        with pytest.raises(netboot.NetbootError, match="answered 404"):
            netboot.remote_build(["https://m/a"])


    def test_a_redirect_stays_a_head_request(self):
        req = netboot.urllib.request.Request("https://download.example/a", method="HEAD")
        new = netboot._KeepHeadRedirect().redirect_request(
            req, None, 302, "Found", {}, "https://mirror.example/a")
        assert new.get_method() == "HEAD" and new.full_url == "https://mirror.example/a"


class TestRecordedOnAdd:
    def test_mirror_installer(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path)
        monkeypatch.setattr(image_svc, "process_image", lambda image_id: None)
        _login(client)
        client.post("/images/netboot", data={"distro": "debian", "release": "trixie",
                                             "source": "mirror"})
        db = app_db.SessionLocal()
        img = db.execute(select(Image)).scalar_one()
        assert (img.origin, img.live_release, img.build) == ("mirror", "", "")
        db.close()

    def test_pasted_url(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path)
        monkeypatch.setattr(image_svc, "process_image", lambda image_id: None)
        _login(client)
        client.post("/images/netboot", data={"distro": "debian", "release": "trixie",
                                             "source": "url", "url": "https://x/n.tar.gz"})
        db = app_db.SessionLocal()
        assert db.execute(select(Image)).scalar_one().origin == "url"
        db.close()

    def test_live_desktop_keeps_its_release(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path)
        monkeypatch.setattr(image_svc, "process_image", lambda image_id: None)
        monkeypatch.setattr(netboot, "_read_listing", lambda base, timeout=30: (
            '<a href="debian-live-13.1.0-amd64-kde.iso">x</a>'))
        _login(client)
        r = client.post("/images/netboot", data={
            "distro": "debian", "variant": "live", "live_release": "trixie",
            "flavour": "kde", "source": "mirror"}, follow_redirects=False)
        assert r.status_code == 303
        db = app_db.SessionLocal()
        img = db.execute(select(Image)).scalar_one()
        assert (img.origin, img.release, img.live_release) == ("mirror", "kde", "trixie")
        db.close()


class TestRecordedOnBuild:
    def test_archive_size_and_url(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "IMAGE_DIR", tmp_path)

        def fake_download(url, dest, max_bytes=netboot.ARCHIVE_MAX_BYTES):
            _archive(dest, {"debian-installer/amd64/linux": KERNEL,
                            "debian-installer/amd64/initrd.gz": b"INITRD"})
        monkeypatch.setattr(netboot, "download", fake_download)
        image_id = _netboot_image(catalog_id="debian", release="trixie",
                                  mirror="https://deb.debian.org/debian",
                                  source_url=DEBIAN_ARCHIVE)
        img = _processed(monkeypatch, image_id)
        assert img.status == "ready", img.message
        size = image_svc.iso_path("x").stat().st_size
        assert img.build == f"{size} {DEBIAN_ARCHIVE}"

    def test_kernel_and_initrd_sizes_and_urls(self, client, monkeypatch):
        files = {"vmlinuz": KERNEL, "initrd.img": b"INITRD"}

        def fake_download(url, dest, max_bytes=netboot.ARCHIVE_MAX_BYTES):
            dest.write_bytes(files[url.rsplit("/", 1)[-1]])
        monkeypatch.setattr(netboot, "download", fake_download)
        image_id = _netboot_image(catalog_id="fedora", release="44",
                                  mirror="https://dl.fedoraproject.org/pub/fedora/linux")
        img = _processed(monkeypatch, image_id)
        assert img.status == "ready", img.message
        base = ("https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Everything/"
                "x86_64/os/images/pxeboot")
        assert img.build == f"{len(KERNEL)} {base}/vmlinuz\n6 {base}/initrd.img"


class TestSettings:
    def test_switch_is_off_by_default_and_saved(self, client):
        _login(client)
        page = client.get("/settings").text
        box = re.search(r'<input[^>]*id="netboot_track_latest"[^>]*>', page).group(0)
        assert "checked" not in box
        assert "Not checked yet." in page
        client.post("/settings", data={"netboot_track_latest": "on"})
        db = app_db.SessionLocal()
        assert track_latest.enabled(db)
        db.close()

    def test_check_now_starts_a_check(self, client, monkeypatch):
        started = []
        monkeypatch.setattr(track_latest, "start_check", lambda: started.append(1) or True)
        _login(client)
        r = client.post("/settings/track-latest/check", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/settings#netboot-catalog"
        assert started == [1]

    def test_last_result_is_shown(self, client):
        _login(client)
        db = app_db.SessionLocal()
        set_setting(db, track_latest.CHECKED_KEY, "2026-10-17T22:00:00+00:00")
        set_setting(db, track_latest.RESULT_KEY, "No newer builds (2 releases checked).")
        db.close()
        page = client.get("/settings").text
        assert "Last checked 2026-10-17 22:00 UTC: No newer builds (2 releases checked)." in page

    def test_check_now_needs_an_admin(self, client):
        r = client.post("/settings/track-latest/check", follow_redirects=False)
        assert r.status_code == 303 and "/login" in r.headers["location"]


class TestLock:
    def test_only_one_check_at_a_time(self, monkeypatch):
        monkeypatch.setattr(track_latest.threading, "Thread",
                            lambda **kw: type("T", (), {"start": lambda self: None})())
        assert track_latest.start_check() is True
        assert track_latest.is_running()
        assert track_latest.start_check() is False
        track_latest._running.release()
