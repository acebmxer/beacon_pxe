"""Tests for services.ipxe — boot menu script generation."""
import pytest
from unittest.mock import patch
from sqlalchemy.orm import Session

from app.models import Image, Setting
from app.services import ipxe


def _add_setting(db: Session, key: str, value: str):
    db.add(Setting(key=key, value=value))
    db.commit()


def _add_image(db: Session, **kwargs) -> Image:
    defaults = dict(
        name="Test Linux",
        filename="test.iso",
        os_family="linux",
        status="ready",
        enabled=1,
        kernel_path="os/1/vmlinuz",
        initrd_path="os/1/initrd.img",
        boot_args="",
    )
    defaults.update(kwargs)
    img = Image(**defaults)
    db.add(img)
    db.commit()
    db.refresh(img)
    return img


@pytest.fixture(autouse=True)
def no_file_write(tmp_path, monkeypatch):
    """Redirect the bootroot write so tests don't touch the filesystem."""
    monkeypatch.setattr(
        "app.config.BOOTROOT_DIR",
        tmp_path,
        raising=True,
    )


class TestRenderNoImages:
    def test_placeholder_when_empty(self, db):
        _add_setting(db, "server_ip", "192.168.1.10")
        _add_setting(db, "menu_title", "Beacon")
        text = ipxe.render(db)
        assert "#!ipxe" in text
        assert "no images uploaded yet" in text
        assert "exit_ipxe" in text

    def test_no_timeout_without_default(self, db):
        text = ipxe.render(db)
        assert "--timeout" not in text


class TestRenderWithImages:
    def test_image_appears_in_menu(self, db):
        img = _add_image(db, name="Ubuntu 24.04")
        text = ipxe.render(db)
        assert "Ubuntu 24.04" in text
        assert f"os_{img.id}" in text

    def test_disabled_image_not_in_menu(self, db):
        _add_image(db, name="Hidden Image", enabled=0)
        text = ipxe.render(db)
        assert "Hidden Image" not in text

    def test_not_ready_image_not_in_menu(self, db):
        _add_image(db, name="Processing", status="processing")
        text = ipxe.render(db)
        assert "Processing" not in text

    def test_linux_kernel_initrd_in_label(self, db):
        img = _add_image(db, kernel_path="os/7/vmlinuz", initrd_path="os/7/initrd")
        text = ipxe.render(db)
        assert "os/7/vmlinuz" in text
        assert "os/7/initrd" in text

    def test_windows_wimboot_in_label(self, db):
        img = _add_image(db, os_family="windows",
                         kernel_path="os/2/bootmgr",
                         initrd_path="os/2/boot.wim")
        text = ipxe.render(db)
        assert "wimboot" in text

    def test_xcpng_grub_chain(self, db):
        img = _add_image(db, os_family="xcpng",
                         kernel_path="os/3/vmlinuz",
                         initrd_path="os/3/install.img")
        text = ipxe.render(db)
        assert "chain" in text
        assert "bootx64.efi" in text


class TestDefaultAndTimeout:
    def test_default_image_sets_timeout(self, db):
        _add_setting(db, "boot_timeout", "45")
        img = _add_image(db, is_default=1)
        text = ipxe.render(db)
        assert "--timeout 45000" in text
        assert f"os_{img.id}" in text

    def test_no_timeout_when_no_default(self, db):
        _add_image(db)
        text = ipxe.render(db)
        assert "--timeout" not in text

    def test_first_image_highlighted_but_not_booted_when_no_default(self, db):
        # Nothing marked: the cursor starts on the first image so the menu opens
        # on an OS rather than on "Continue local boot" -- but there is no
        # countdown, so it never boots on its own.
        first = _add_image(db, name="Alpha Linux")
        _add_image(db, name="Beta Linux")
        text = ipxe.render(db)
        assert f"--default os_{first.id}" in text
        assert "--timeout" not in text

    def test_zero_timeout_means_no_timeout(self, db):
        _add_setting(db, "boot_timeout", "0")
        img = _add_image(db, is_default=1)
        text = ipxe.render(db)
        assert "--timeout" not in text

    def test_default_star_in_item(self, db):
        img = _add_image(db, is_default=1)
        text = ipxe.render(db)
        assert "★" in text


class TestDisplayOrder:
    def test_explicit_order_respected(self, db):
        a = _add_image(db, name="Zebra", display_order=0)
        b = _add_image(db, name="Alpha", display_order=1)
        text = ipxe.render(db)
        # Zebra should appear before Alpha
        assert text.index("Zebra") < text.index("Alpha")

    def test_null_order_sorts_last(self, db):
        a = _add_image(db, name="Ordered", display_order=0)
        b = _add_image(db, name="Unordered", display_order=None)
        text = ipxe.render(db)
        assert text.index("Ordered") < text.index("Unordered")


class TestTracking:
    def test_track_url_present(self, db):
        img = _add_image(db)
        text = ipxe.render(db)
        assert f"/track/{img.id}" in text
