"""Update checking: compares deployed image digest against GHCR.

Which tag is watched depends on the configured update channel (config.BEACON_TAG,
set from BEACON_TAG in .env): "latest" follows the main branch, "stable" follows
tagged releases. docker-compose.yml interpolates the same variable, so the tag
checked here is always the tag `docker compose pull` installs.
"""
import json
import logging
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from ..db import SessionLocal
from ..store import get_setting, set_setting
from .. import config

log = logging.getLogger(__name__)

_OWNER = "acebmxer"
_IMAGE = "beacon-web"
_CHECK_INTERVAL = 86400  # 24 hours

# How long "Update applied successfully" stays on screen. It exists to confirm
# the restart finished, which the admin sees within a minute or two of the page
# coming back; past that it is stale reassurance about an update they have long
# since moved on from. Failures are not expired here — they describe a condition
# that is still true and still needs acting on, so they persist until the next
# attempt or an explicit dismissal.
_SUCCESS_TTL = 1800  # 30 minutes

# Name of the throwaway container that performs the recreation, and how long to
# wait for it to replace this container before declaring the update stalled.
# Generous: it has to pull nothing (images are already local) but may restart
# six services on slow storage.
_UPDATER_NAME = "beacon_updater"
_RECREATE_TIMEOUT = 300  # 5 minutes

# Stand-in for the deployed digest after a channel switch. Never equals a real
# digest, so the comparison below keeps reporting an update until one is applied
# and run_update() writes the true digest back.
_CHANNEL_SWITCHED = "channel-switched"


def _set_result(db, value: str) -> None:
    """Record the outcome of an update attempt, stamped with the time.

    The timestamp is what lets current_result() expire a success banner; without
    it a completed update reports "services are restarting" forever.
    """
    set_setting(db, "update_last_result", value)
    set_setting(db, "update_last_result_at", datetime.now(timezone.utc).isoformat())


def current_result(db) -> str:
    """The last update outcome worth showing, or "" if there is none.

    A success older than _SUCCESS_TTL is treated as absent. Results written
    before this timestamp existed have no recorded time; those are also treated
    as expired, since anything from a previous deployment is by definition old.
    """
    result = get_setting(db, "update_last_result", "")
    if result != "success":
        return result

    stamped = get_setting(db, "update_last_result_at", "")
    if not stamped:
        return ""
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamped)).total_seconds()
    except ValueError:
        return ""
    return result if age < _SUCCESS_TTL else ""


def clear_result(db) -> None:
    """Drop the recorded outcome so the UI stops showing it."""
    _set_result(db, "")


def image_ref() -> str:
    """Fully qualified image the update check watches and compose pulls."""
    return f"ghcr.io/{_OWNER}/{_IMAGE}:{config.BEACON_TAG}"


def version_label() -> str:
    """Human-readable description of the running build.

    BEACON_VERSION is whatever docker/metadata-action called the primary tag, so
    its shape tells us which kind of build this is: a semver release, a branch
    build off main, or an unpublished local build.
    """
    ver = config.BEACON_VERSION
    short = config.BEACON_COMMIT[:7]

    if ver == "dev":
        return f"dev build ({short})" if short else "dev build"
    if ver[0].isdigit():
        return f"v{ver}"
    # Branch build (e.g. "main"): the name alone doesn't identify it, so the
    # commit is what actually pins the version.
    return f"{ver} ({short})" if short else ver


def _ghcr_latest_digest() -> str | None:
    """Return the tracked tag's manifest digest from GHCR, or None on error.

    A missing tag (404) is indistinguishable here from a network failure — both
    return None and leave the recorded state alone. That is deliberate: pointing
    BEACON_TAG at a tag that does not exist yet must not clear a real pending
    update, and must not report "up to date" when nothing was actually checked.
    """
    try:
        token_url = (
            f"https://ghcr.io/token"
            f"?scope=repository:{_OWNER}/{_IMAGE}:pull&service=ghcr.io"
        )
        with urllib.request.urlopen(token_url, timeout=15) as r:
            token = json.loads(r.read())["token"]

        req = urllib.request.Request(
            f"https://ghcr.io/v2/{_OWNER}/{_IMAGE}/manifests/{config.BEACON_TAG}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": (
                    "application/vnd.oci.image.index.v1+json,"
                    "application/vnd.oci.image.manifest.v1+json,"
                    "application/vnd.docker.distribution.manifest.list.v2+json,"
                    "application/vnd.docker.distribution.manifest.v2+json"
                ),
            },
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.headers.get("Docker-Content-Digest")
    except Exception as exc:
        log.debug("GHCR digest check failed: %s", exc)
        return None


def check_for_updates() -> bool:
    """Query GHCR and record result in DB. Returns True if an update is available."""
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc).isoformat()
        set_setting(db, "update_last_checked", now)

        digest = _ghcr_latest_digest()

        if digest is None:
            # Network unreachable, or the tag does not exist yet — keep whatever
            # state was last recorded rather than guessing.
            return get_setting(db, "update_available", "0") == "1"

        # The recorded digest belongs to whichever channel was tracked when it
        # was written, so a channel switch invalidates it. Replace it with a
        # sentinel rather than the new digest: the containers are still running
        # the *previous* channel's images, so an update genuinely is pending —
        # `docker compose pull` has to run to move onto this channel. (Switching
        # main -> stable is therefore reported as an available update even though
        # it installs an older, released build. The action needed is the same.)
        #
        # An empty previous channel means a fresh install, or one upgrading from
        # a build predating channels. Neither is a switch, so just record it and
        # let the normal comparison below decide.
        prev_channel = get_setting(db, "update_channel", "")
        if prev_channel != config.BEACON_TAG:
            set_setting(db, "update_channel", config.BEACON_TAG)
            if prev_channel:
                log.info(
                    "Update channel changed %s -> %s; update pending",
                    prev_channel, config.BEACON_TAG,
                )
                set_setting(db, "update_known_digest", _CHANNEL_SWITCHED)

        known = get_setting(db, "update_known_digest", "")
        if not known:
            # First run: record current GHCR digest as the deployed baseline.
            # The image was just pulled, so GHCR digest == what's running.
            set_setting(db, "update_known_digest", digest)
            set_setting(db, "update_available", "0")
            return False

        available = digest != known
        set_setting(db, "update_available", "1" if available else "0")
        return available
    finally:
        db.close()


def _spawn_recreator(image: str, project_dir: str) -> str | None:
    """Start a throwaway container to run `docker compose up -d`.

    The recreation cannot run in this process. `docker compose up -d` detaches
    the *containers* it starts, not itself — it stays in the foreground doing
    the recreation, and one of the containers it must replace is the one this
    code is running in. Stopping it kills the compose process mid-run, which is
    why the update appeared to succeed while nothing was replaced.

    So hand the job to a container outside the compose project, which nothing in
    the stack can take down. It runs the web image (just pulled, so guaranteed
    present, and it already carries the docker CLI + compose plugin for exactly
    this reason) with the project directory mounted at its own path, so compose
    derives the same project name and resolves relative volume paths the way the
    original `up` did.

    Returns None once the container is launched, or an error string.
    """
    # A leftover from a previous run would make `docker run --name` fail.
    subprocess.run(["docker", "rm", "-f", _UPDATER_NAME],
                   capture_output=True, text=True, timeout=30)

    launch = subprocess.run(
        [
            "docker", "run", "--detach", "--rm",
            "--name", _UPDATER_NAME,
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "-v", f"{project_dir}:{project_dir}",
            "-w", project_dir,
            image,
            # --remove-orphans only touches containers labelled with this
            # compose project; this container carries no such label, so it
            # cannot delete itself out from under the recreation.
            "docker", "compose", "up", "-d", "--remove-orphans",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if launch.returncode != 0:
        return (launch.stderr or launch.stdout or "unknown error").strip()[:300]
    return None


def finish_pending_update() -> None:
    """Resolve an in-flight update at startup.

    Called from the app's startup hook. Reaching this point with an update
    in progress means this process is the *replacement* container: the previous
    one was stopped by the recreation, and the new image is now running. That
    is the only trustworthy confirmation available, since the process that
    started the update does not survive to see it finish.
    """
    db = SessionLocal()
    try:
        if get_setting(db, "update_in_progress", "0") != "1":
            return

        # Digest recorded before the recreation, so no network call on startup.
        pending = get_setting(db, "update_pending_digest", "")
        if pending:
            set_setting(db, "update_known_digest", pending)
        set_setting(db, "update_pending_digest", "")
        set_setting(db, "update_available", "0")
        set_setting(db, "update_in_progress", "0")
        _set_result(db, "success")
        log.info("Update completed; now running the recreated container")
    finally:
        db.close()


def reap_stalled_update(db) -> None:
    """Fail an update that started but never replaced this container.

    If the recreation dies, this process keeps running with the update still
    marked in progress, and the UI would spin on "pulling images" forever. The
    replacement path clears the flag on startup, so anything still set here well
    past that point means the recreation never happened.
    """
    if get_setting(db, "update_in_progress", "0") != "1":
        return

    started = get_setting(db, "update_started_at", "")
    if not started:
        return
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds()
    except ValueError:
        return
    if age < _RECREATE_TIMEOUT:
        return

    set_setting(db, "update_in_progress", "0")
    _set_result(
        db,
        "recreate_failed: images were pulled but the containers were not "
        "recreated. Run `docker compose up -d` on the host to finish.",
    )
    log.warning("Update stalled: containers were not recreated after %.0fs", age)


def run_update() -> None:
    """
    Pull new images, then hand container recreation to a throwaway container.

    Deliberately records no success here. This process is about to be killed by
    the recreation, so it cannot observe the outcome; writing "success" before
    starting the work is what previously reported updates that never landed.
    The replacement container confirms it instead, in finish_pending_update().
    """
    db = SessionLocal()
    try:
        set_setting(db, "update_in_progress", "1")
        set_setting(db, "update_started_at", datetime.now(timezone.utc).isoformat())
        _set_result(db, "")

        compose_file = str(config.COMPOSE_FILE)
        project_dir = config.COMPOSE_PROJECT_DIR
        env_file = config.COMPOSE_ENV_FILE

        base_cmd = ["docker", "compose", "-f", compose_file]
        if project_dir:
            base_cmd += ["--project-directory", project_dir]
        if env_file and env_file.exists():
            base_cmd += ["--env-file", str(env_file)]

        # Pull new images (blocking; does not touch running containers).
        pull = subprocess.run(
            base_cmd + ["pull"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if pull.returncode != 0:
            err = (pull.stderr or pull.stdout or "unknown error")[:400]
            _set_result(db, f"pull_failed: {err}")
            set_setting(db, "update_in_progress", "0")
            return

        # Stash the digest now, while the network call is cheap and this process
        # is still alive; finish_pending_update() promotes it after the restart.
        new_digest = _ghcr_latest_digest()
        set_setting(db, "update_pending_digest", new_digest or "")

        # The recreation needs a project directory on the host to run from.
        # Without it compose would resolve relative volume paths and the project
        # name against the wrong directory and build a parallel stack.
        if not project_dir:
            _set_result(
                db,
                "recreate_failed: PROJECT_DIR is not set in .env, so the update "
                "cannot recreate containers. Add it (see .env.example) and run "
                "`docker compose up -d` on the host.",
            )
            set_setting(db, "update_in_progress", "0")
            return

        error = _spawn_recreator(image_ref(), project_dir)
        if error:
            _set_result(db, f"recreate_failed: {error}")
            set_setting(db, "update_in_progress", "0")
            return

        # This container is now living on borrowed time — the recreation will
        # stop it shortly. Its replacement writes the success state.

    except subprocess.TimeoutExpired:
        _set_result(db, "timeout: pull took too long")
        set_setting(db, "update_in_progress", "0")
    except Exception as exc:
        _set_result(db, f"error: {str(exc)[:200]}")
        set_setting(db, "update_in_progress", "0")
    finally:
        db.close()


def _check_loop() -> None:
    time.sleep(300)  # let the app settle before the first network call
    while True:
        try:
            check_for_updates()
        except Exception:
            pass
        time.sleep(_CHECK_INTERVAL)


def start_background_checker() -> None:
    threading.Thread(target=_check_loop, daemon=True, name="update-checker").start()
