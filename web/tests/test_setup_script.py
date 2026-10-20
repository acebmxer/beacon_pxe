"""setup.sh must finish a fresh install and write a usable, private .env.

Runs the real script in a scratch copy of the repo root with `docker` and `ip`
replaced by stubs, so nothing is pulled or started. Skipped where the script
isn't present (the production image copies only web/).
"""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "setup.sh"
EXAMPLE = REPO / ".env.example"

pytestmark = pytest.mark.skipif(
    not (SCRIPT.is_file() and EXAMPLE.is_file() and shutil.which("bash")),
    reason="setup.sh / .env.example / bash not available",
)


def _env_values(path: Path) -> dict:
    values = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


@pytest.fixture
def scratch(tmp_path):
    root = tmp_path / "beacon"
    root.mkdir()
    shutil.copy(SCRIPT, root / "setup.sh")
    shutil.copy(EXAMPLE, root / ".env.example")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("docker", "ip"):
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return root, env


def test_fresh_install_completes_with_generated_secrets(scratch):
    root, env = scratch
    # The old script died with SIGPIPE (exit 141) on most runs, not all;
    # several runs make a regression show up reliably.
    for _ in range(5):
        (root / ".env").unlink(missing_ok=True)
        result = subprocess.run(
            ["bash", str(root / "setup.sh")],
            cwd=root, env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        values = _env_values(root / ".env")
        assert len(values["ADMIN_PASSWORD"]) == 20
        assert values["ADMIN_PASSWORD"].isalnum()
        assert len(values["SECRET_KEY"]) == 48
        assert values["SECRET_KEY"].isalnum()


def test_env_is_readable_by_owner_only(scratch):
    root, env = scratch
    subprocess.run(
        ["bash", str(root / "setup.sh")],
        cwd=root, env=env, capture_output=True, text=True, timeout=60,
    )
    mode = stat.S_IMODE((root / ".env").stat().st_mode)
    assert mode == 0o600
