"""Runs the real host update helper (deploy/updater/googich-updater.sh) with stand-in docker
and curl commands, so its refusals and rollback are checked on every change."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "deploy" / "updater" / "googich-updater.sh"

DOCKER = """#!/bin/bash
echo "docker $*" >> "$STUB_LOG"
if [ "$1 $2" = "compose up" ]; then
  grep -oE 'googich_takeaway:[^" ]+' "$GOOGICH_COMPOSE_DIR/compose.yaml" \\
    | cut -d: -f2 > "$STUB_RUNNING"
fi
"""
CURL = """#!/bin/bash
url="${@: -1}"
case "$url" in
  *releases/tags/v0.1.2)
    printf '{\\n "tag_name": "v0.1.2",\\n "draft": false,\\n "prerelease": false\\n}' ;;
  *releases/tags/v0.1.3) printf '{"tag_name": "v0.1.3", "draft": false, "prerelease": true}' ;;
  *healthz) running=$(cat "$STUB_RUNNING"); [ "$running" = "$STUB_BROKEN" ] && exit 7
            printf '{"status":"ok","version":"%s"}' "$running" ;;
  *) exit 22 ;;
esac
"""

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def run(tmp_path: Path, request: str, broken: str = "none") -> tuple[dict[str, str], str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("docker", DOCKER), ("curl", CURL), ("sleep", "#!/bin/bash\nexit 0\n")):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    compose_dir = tmp_path / "opt"
    updater = compose_dir / "data" / "updater"
    updater.mkdir(parents=True, exist_ok=True)
    (compose_dir / "compose.yaml").write_text(
        'services:\n  googich:\n    image: "ghcr.io/aistuartai/googich_takeaway:0.1.1"\n'
    )
    (tmp_path / "running").write_text("0.1.1\n")
    (updater / "request.json").write_text(request)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "STUB_LOG": str(tmp_path / "log"),
        "STUB_RUNNING": str(tmp_path / "running"),
        "STUB_BROKEN": broken,
        "GOOGICH_COMPOSE_DIR": str(compose_dir),
    }
    subprocess.run(["bash", str(SCRIPT)], env=env, check=False, timeout=60)  # noqa: S603, S607
    status = json.loads((updater / "status.json").read_text())
    return status, (compose_dir / "compose.yaml").read_text(), (tmp_path / "running").read_text()


def test_updates_to_a_published_release(tmp_path: Path) -> None:
    status, compose, running = run(tmp_path, '{"version": "0.1.2"}')
    assert status["state"] == "done"
    assert status["message"] == "Updated from 0.1.1 to 0.1.2."
    assert 'image: "ghcr.io/aistuartai/googich_takeaway:0.1.2"' in compose
    assert running.strip() == "0.1.2"
    assert not (tmp_path / "opt" / "data" / "updater" / "request.json").exists()


@pytest.mark.parametrize(
    ("request_body", "message"),
    [
        ('{"version": "9.9.9"}', "is not a published release"),
        ('{"version": "0.1.3"}', "is not a published release"),  # a pre-release
        ('{"version": "0.1.2; rm -rf /"}', "not a plain version number"),
        ('{"version": "$(reboot)"}', "not a plain version number"),
        ("not json", "not a plain version number"),
    ],
)
def test_refuses_anything_else(tmp_path: Path, request_body: str, message: str) -> None:
    status, compose, running = run(tmp_path, request_body)
    assert status["state"] == "failed"
    assert message in status["message"]
    assert "googich_takeaway:0.1.1" in compose
    assert running.strip() == "0.1.1"


def test_rolls_back_when_the_new_version_is_unhealthy(tmp_path: Path) -> None:
    status, compose, running = run(tmp_path, '{"version": "0.1.2"}', broken="0.1.2")
    assert status["state"] == "failed"
    assert "Rolled back to 0.1.1" in status["message"]
    assert "googich_takeaway:0.1.1" in compose
    assert running.strip() == "0.1.1"
