"""Regression tests for image processing: same-name uploads, Reprocess on an
image that is still processing, images left on "processing" by an
unexpected error, and Delete on an image whose run is still going."""
import threading

import pytest

import app.config as app_config
from app.models import Image
from app.services import images as image_svc
from app.services import netboot
from tests.test_routes import _login, _seed_image, _session


@pytest.fixture
def image_dir(tmp_path, monkeypatch):
    d = tmp_path / "images"
    d.mkdir()
    monkeypatch.setattr(app_config, "IMAGE_DIR", d)
    return d


@pytest.fixture
def no_processing(monkeypatch):
    """Record the background runs instead of extracting anything."""
    runs = []
    monkeypatch.setattr(image_svc, "process_image", runs.append)
    return runs


def _upload(client, filename, body):
    return client.post("/images/upload", follow_redirects=False,
                       files={"file": (filename, body, "application/octet-stream")})


def _image(image_id):
    db = _session()
    try:
        return db.get(Image, image_id)
    finally:
        db.close()


def _all_images():
    db = _session()
    try:
        return db.query(Image).order_by(Image.id).all()
    finally:
        db.close()


def test_same_name_upload_does_not_overwrite_the_first(client, image_dir, no_processing):
    _login(client)
    assert _upload(client, "a.iso", b"first").status_code == 303
    assert _upload(client, "a.iso", b"second").status_code == 303
    assert _upload(client, "a.iso", b"third").status_code == 303

    first, second, third = _all_images()
    assert (first.filename, second.filename, third.filename) == (
        "a.iso", "a-2.iso", "a-3.iso")
    assert (image_dir / "a.iso").read_bytes() == b"first"
    assert (image_dir / "a-2.iso").read_bytes() == b"second"
    assert (image_dir / "a-3.iso").read_bytes() == b"third"


def test_deleting_one_same_name_upload_keeps_the_others_iso(client, image_dir,
                                                             no_processing):
    _login(client)
    _upload(client, "a.iso", b"first")
    _upload(client, "a.iso", b"second")
    first, second = _all_images()

    client.post(f"/images/{second.id}/delete", follow_redirects=False)
    assert (image_dir / "a.iso").read_bytes() == b"first"
    assert _image(first.id) is not None


def test_name_owned_by_an_image_without_its_iso_is_not_reused(client, image_dir,
                                                               no_processing):
    # Live catalog images delete their ISO once unpacked; the name stays theirs.
    _login(client)
    _seed_image(name="Old", filename="a.iso")
    _upload(client, "a.iso", b"new")
    assert _all_images()[-1].filename == "a-2.iso"


def test_reprocess_leaves_an_image_that_is_still_processing_alone(client,
                                                                   no_processing):
    _login(client)
    img = _seed_image(status="processing")
    r = client.post(f"/images/{img.id}/retry", follow_redirects=False)
    assert r.status_code == 303
    assert no_processing == []
    assert _image(img.id).status == "processing"
    assert "still processing" in client.get("/images").text


def test_reprocess_clicked_twice_queues_one_run(client, no_processing):
    # The first click leaves the image "pending" until its background run
    # starts; a second click in that gap queued a second run.
    _login(client)
    img = _seed_image(status="ready")
    client.post(f"/images/{img.id}/retry", follow_redirects=False)
    client.post(f"/images/{img.id}/retry", follow_redirects=False)
    assert no_processing == [img.id]
    assert "still processing" in client.get("/images").text


def test_reprocess_selected_skips_a_queued_image(client, no_processing):
    _login(client)
    queued = _seed_image(name="Queued", status="pending")
    done = _seed_image(name="Done", status="ready")
    client.post("/images/bulk", follow_redirects=False,
                data={"action": "reprocess", "ids": [queued.id, done.id]})
    assert no_processing == [done.id]


def test_restart_clears_an_image_left_queued(client):
    from app.db import SessionLocal
    img = _seed_image(status="pending")
    db = SessionLocal()
    try:
        assert image_svc.reconcile_statuses(db) == 1
    finally:
        db.close()
    assert _image(img.id).status == image_svc.NEEDS_REPROCESS


def test_reprocess_still_runs_for_a_finished_image(client, no_processing):
    _login(client)
    img = _seed_image(status="error")
    client.post(f"/images/{img.id}/retry", follow_redirects=False)
    assert no_processing == [img.id]
    assert _image(img.id).status == "pending"


def test_unexpected_error_marks_the_image_failed(client, monkeypatch):
    # process_image opens its own session; point it at the test database.
    import app.db as app_db
    monkeypatch.setattr(image_svc, "SessionLocal", app_db.SessionLocal)
    img = _seed_image(status="pending", source="netboot", catalog_id="debian",
                      variant="installer")

    def truncated_archive(db, image):
        raise EOFError("Compressed file ended before the end-of-stream marker")

    monkeypatch.setattr(image_svc, "_process_netboot", truncated_archive)
    image_svc.process_image(img.id)

    after = _image(img.id)
    assert after.status == "error"
    assert "end-of-stream" in after.message


# --- Delete while an image is processing -----------------------------------

class _SlowRun:
    """Stands in for the netboot build: writes a boot file, then waits until
    the test lets it go, as a long download or unpack would."""

    def __init__(self, monkeypatch, image_id, check_stop=True):
        import app.db as app_db
        monkeypatch.setattr(image_svc, "SessionLocal", app_db.SessionLocal)
        monkeypatch.setattr(image_svc, "_process_netboot", self._step)
        self.out = app_config.BOOTROOT_DIR / "os" / str(image_id)
        self.check_stop = check_stop
        self.started, self.release = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=image_svc.process_image,
                                       args=(image_id,))

    def _step(self, db, image):
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "vmlinuz").write_bytes(b"K")
        self.started.set()
        self.release.wait(5)
        (self.out / "initrd").write_bytes(b"I")  # written after the Delete
        if self.check_stop:
            netboot.check_stop()
        image.status = "ready"
        db.commit()

    def __enter__(self):
        self.thread.start()
        assert self.started.wait(5)
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.thread.join(5)
        assert not self.thread.is_alive()


def _netboot_seed(**fields):
    return _seed_image(status="pending", source="netboot", catalog_id="debian",
                       variant="installer", filename="n.tar.gz", **fields)


def test_delete_while_processing_waits_for_the_run_then_removes_it(client,
                                                                   monkeypatch):
    # Removing the files under a running job left it writing new ones (and a
    # re-downloaded ISO) that no image owned.
    _login(client)
    img = _netboot_seed()
    with _SlowRun(monkeypatch, img.id) as run:
        client.post(f"/images/{img.id}/delete", follow_redirects=False)
        assert _image(img.id).status == image_svc.DELETING
        assert "deleted as soon as the current step stops" in client.get("/images").text
    assert _image(img.id) is None
    assert not run.out.exists()


def test_delete_still_wins_when_the_run_finishes_its_last_step(client, monkeypatch):
    # The run can mark the image ready after the Delete; it is removed anyway.
    _login(client)
    img = _netboot_seed()
    with _SlowRun(monkeypatch, img.id, check_stop=False) as run:
        client.post(f"/images/{img.id}/delete", follow_redirects=False)
    assert _image(img.id) is None
    assert not run.out.exists()


def test_second_delete_removes_a_stuck_image_at_once(client, monkeypatch):
    _login(client)
    img = _netboot_seed()
    with _SlowRun(monkeypatch, img.id) as run:
        client.post(f"/images/{img.id}/delete", follow_redirects=False)
        client.post(f"/images/{img.id}/delete", follow_redirects=False)
        assert _image(img.id) is None
    # What the run wrote after that is cleared when it stops.
    assert not run.out.exists()


def test_reprocess_leaves_an_image_being_deleted_alone(client, no_processing):
    _login(client)
    img = _seed_image(status=image_svc.DELETING)
    client.post(f"/images/{img.id}/retry", follow_redirects=False)
    client.post("/images/bulk", follow_redirects=False,
                data={"action": "reprocess", "ids": [img.id]})
    assert no_processing == []
    assert _image(img.id).status == image_svc.DELETING


def test_delete_selected_waits_for_a_running_image(client, monkeypatch):
    _login(client)
    running = _netboot_seed(name="Running")
    done = _seed_image(name="Done", filename="d.iso")
    with _SlowRun(monkeypatch, running.id):
        client.post("/images/bulk", follow_redirects=False,
                    data={"action": "delete", "ids": [running.id, done.id]})
        assert _image(done.id) is None
        assert _image(running.id).status == image_svc.DELETING
        assert ("Deleted 1 image; 1 still processing is deleted as soon as the "
                "current step stops.") in client.get("/images").text
    assert _image(running.id) is None


def test_queued_run_of_a_deleted_or_finished_image_does_nothing(client,
                                                               monkeypatch):
    import app.db as app_db
    monkeypatch.setattr(image_svc, "SessionLocal", app_db.SessionLocal)
    runs = []
    monkeypatch.setattr(image_svc, "_process_netboot", lambda db, i: runs.append(i.id))
    img = _netboot_seed()
    image_svc.process_image(img.id + 100)  # deleted while it waited its turn
    db = _session()
    db.get(Image, img.id).status = "ready"  # built by an earlier queued run
    db.commit()
    db.close()
    image_svc.process_image(img.id)
    assert runs == []
    assert _image(img.id).status == "ready"


def test_restart_finishes_a_delete_the_run_never_completed(client):
    from app.db import SessionLocal
    img = _seed_image(status=image_svc.DELETING)
    out = app_config.BOOTROOT_DIR / "os" / str(img.id)
    out.mkdir(parents=True)
    (out / "vmlinuz").write_bytes(b"K")
    db = SessionLocal()
    try:
        assert image_svc.reconcile_statuses(db) == 1
    finally:
        db.close()
    assert _image(img.id) is None
    assert not out.exists()


class _Download:
    status = 200
    headers = {}

    def __init__(self):
        self.chunks = [b"x" * 10] * 3

    def read(self, n):
        return self.chunks.pop() if self.chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_download_stops_when_its_image_is_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(netboot.urllib.request, "urlopen", lambda *a, **k: _Download())
    dest = tmp_path / "live.iso"
    stop = threading.Event()
    netboot.watch_for_stop(stop)
    try:
        netboot.download("https://mirror/live.iso", dest)  # not stopped: completes
        assert dest.read_bytes() == b"x" * 30
        dest.unlink()
        stop.set()
        with pytest.raises(netboot.Cancelled):
            netboot.download("https://mirror/live.iso", dest)
    finally:
        netboot.watch_for_stop(None)
    assert not dest.exists()
    assert not dest.with_name("live.iso.part").exists()
