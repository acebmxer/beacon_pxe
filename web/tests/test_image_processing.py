"""Regression tests for image processing: same-name uploads, Reprocess on an
image that is still processing, and images left on "processing" by an
unexpected error."""
import pytest

import app.config as app_config
from app.models import Image
from app.services import images as image_svc
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
