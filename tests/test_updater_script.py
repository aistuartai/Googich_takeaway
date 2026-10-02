"""Runs the real host update helper (deploy/updater/googich-updater.sh) with stand-in docker
and curl commands, so its refusals and rollback are checked on every change."""

import json
import os
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
# Stand-in for curl: understands -o FILE and -w FORMAT as the helper uses them.
out=""; fmt=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -w) fmt="$2"; shift 2 ;;
    -H|--max-time) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
reply() {  # code body
  if [ -n "$out" ]; then printf '%s' "$2" > "$out"; else printf '%s' "$2"; fi
  [ -n "$fmt" ] && printf '%s' "$1"
  return 0
}
case "$url" in
  *github.com/*/releases/latest)
    if [ -n "$STUB_RATE_LIMITED" ]; then printf '000'; exit 0; fi
    case "$fmt" in
      *redirect_url*) printf 'https://github.com/aistuartai/Googich_takeaway/releases/tag/v0.1.2' ;;
      *) printf '302' ;;
    esac ;;
  *releases/download/v0.1.2/SHA256SUMS) printf '302' ;;
  *releases/download/*) printf '404' ;;
  *healthz) running=$(cat "$STUB_RUNNING"); [ "$running" = "$STUB_BROKEN" ] && exit 7
            printf '{"status":"ok","version":"%s"}' "$running" ;;
  *) exit 6 ;;
esac
"""

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def run(
    tmp_path: Path,
    request: str,
    broken: str = "none",
    rate_limited: bool = False,
    link_request_to: Path | None = None,
) -> tuple[dict[str, str], str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("docker", DOCKER), ("curl", CURL), ("sleep", "#!/bin/bash\nexit 0\n")):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    compose_dir = tmp_path / "opt"
    updater = compose_dir / "data" / "updater"
    updater.mkdir(parents=True, exist_ok=True)
    updater.chmod(0o1770)  # as installed: owned by root (here, the test user) and sticky
    (compose_dir / "compose.yaml").write_text(
        'services:\n  googich:\n    image: "ghcr.io/aistuartai/googich_takeaway:0.1.1"\n'
    )
    (tmp_path / "running").write_text("0.1.1\n")
    if link_request_to is not None:
        (updater / "request.json").symlink_to(link_request_to)
    else:
        (updater / "request.json").write_text(request)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "STUB_LOG": str(tmp_path / "log"),
        "STUB_RUNNING": str(tmp_path / "running"),
        "STUB_BROKEN": broken,
        "GOOGICH_COMPOSE_DIR": str(compose_dir),
        "STUB_RATE_LIMITED": "1" if rate_limited else "",
        "GOOGICH_UPDATER_OWNER": str(os.getuid()),
        "GOOGICH_RUNTIME_DIR": str(tmp_path / "run"),
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
        ('{"version": "9.9.9"}', "newer than the newest published release (0.1.2)"),
        ('{"version": "0.1.3"}', "newer than the newest"),  # a pre-release: never the newest
        ('{"version": "0.1.0"}', "is not a published release"),  # no release files
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


def test_rate_limited_github_is_not_mistaken_for_an_unpublished_release(tmp_path: Path) -> None:
    status, compose, running = run(tmp_path, '{"version": "0.1.2"}', rate_limited=True)
    assert status["state"] == "failed"
    assert "Could not ask GitHub to confirm 0.1.2" in status["message"]
    assert "not a published release" not in status["message"]
    assert "googich_takeaway:0.1.1" in compose
    assert running.strip() == "0.1.1"


def test_planted_links_are_not_followed(tmp_path: Path) -> None:
    """The container owns <data>: links it plants must never make root write elsewhere."""
    victim = tmp_path / "victim"
    victim.write_text("precious\n")
    updater = tmp_path / "opt" / "data" / "updater"
    updater.mkdir(parents=True)
    for name in (".lock", ".release.json", "lock", "release.json"):  # old and new scratch names
        (updater / name).symlink_to(victim)
    status, _, _ = run(tmp_path, '{"version": "0.1.2"}')
    assert status["state"] == "done"
    assert victim.read_text() == "precious\n"


def test_request_through_a_link_is_refused(tmp_path: Path) -> None:
    """request.json is the container's file: a link to a root-only file is not read."""
    secret = tmp_path / "secret"
    secret.write_text('{"version": "0.1.2"}')
    status, _, running = run(tmp_path, "", link_request_to=secret)
    assert status["state"] == "failed"
    assert "not a plain version number" in status["message"]
    assert running.strip() == "0.1.1"


def test_refuses_an_updater_folder_the_container_could_tamper_with(tmp_path: Path) -> None:
    updater = tmp_path / "opt" / "data" / "updater"
    updater.mkdir(parents=True)
    (updater / "status.json").write_text('{"helper": "1", "state": "idle"}')
    # Not sticky: the container could rename or replace root's files.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "GOOGICH_COMPOSE_DIR": str(tmp_path / "opt"),
        "GOOGICH_UPDATER_OWNER": str(os.getuid()),
        "GOOGICH_RUNTIME_DIR": str(tmp_path / "run"),
    }
    (updater / "request.json").write_text('{"version": "0.1.2"}')
    updater.chmod(0o770)
    result = subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT)],  # noqa: S607
        env=env,
        check=False,
        timeout=60,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "must be owned by root with mode 1770" in result.stderr
    assert (updater / "request.json").exists()  # untouched


INSTALL = SCRIPT.parent / "install-updater.sh"
HELPER_FILES = ("googich-updater.sh", "googich-updater.path", "googich-updater.service")


def _release(tmp_path: Path, tamper: str = "") -> Path:
    """The release's helper files and SHA256SUMS, as the release workflow publishes them."""
    import hashlib

    release = tmp_path / "release"
    release.mkdir()
    sums = []
    for name in HELPER_FILES:
        data = (SCRIPT.parent / name).read_bytes()
        sums.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
        (release / name).write_bytes(data + (b"# changed\n" if name == tamper else b""))
    (release / "SHA256SUMS").write_text("".join(sums))
    return release


def test_installer_sets_everything_up_from_the_running_release(tmp_path: Path) -> None:
    """install-updater.sh, with stand-ins for the commands that need root or the network."""
    result, site, calls = _install(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "releases/download/v0.3.4/googich-updater.sh" in calls
    assert "releases/download/v0.3.4/SHA256SUMS" in calls
    assert "systemctl enable --now googich-updater.path" in calls
    assert (tmp_path / "lib" / "googich-updater.sh").stat().st_mode & 0o777 == 0o755
    path_unit = (tmp_path / "units" / "googich-updater.path").read_text()
    assert f"PathChanged={site}/data/updater/request.json" in path_unit
    service = (tmp_path / "units" / "googich-updater.service").read_text()
    assert f"GOOGICH_COMPOSE_DIR={site}" in service
    assert (site / "data" / "updater").stat().st_mode & 0o7777 == 0o1770
    assert not (site / "data" / "updater" / ".lock").exists()


def test_installer_refuses_files_that_do_not_match_their_checksums(tmp_path: Path) -> None:
    result, _, _ = _install(tmp_path, tamper="googich-updater.sh")
    assert result.returncode == 1
    assert "do not match the release's checksums" in result.stderr
    assert not (tmp_path / "lib" / "googich-updater.sh").exists()  # nothing installed


def _install(
    tmp_path: Path, tamper: str = ""
) -> tuple[subprocess.CompletedProcess[str], Path, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "log"
    release = _release(tmp_path, tamper)
    stubs = {
        "id": "#!/bin/bash\necho 0\n",
        "curl": f"""#!/bin/bash
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift 2 ;; -*) shift ;; *) url="$1"; shift ;; esac
done
echo "curl $url" >> "{log}"
cp "{release}/$(basename "$url")" "$out"
""",
        "systemctl": f'#!/bin/bash\necho "systemctl $*" >> "{log}"\n',
        "chown": f'#!/bin/bash\necho "chown $*" >> "{log}"\n',
        "install": """#!/bin/bash
args=()
while [ $# -gt 0 ]; do case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac; done
exec /usr/bin/install "${args[@]}"
""",
    }
    for name, body in stubs.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    site = tmp_path / "srv" / "googich"
    (site / "data" / "updater").mkdir(parents=True)
    (site / "data" / "updater" / ".lock").write_text("")  # left by helper version 1
    # A trailing comment must not change the version read (it once became 0.3.46).
    (site / "compose.yaml").write_text(
        "    image: ghcr.io/aistuartai/googich_takeaway:0.3.4  # pinned 6 Oct\n"
    )
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "GOOGICH_LIB_DIR": str(tmp_path / "lib"),
        "GOOGICH_UNIT_DIR": str(tmp_path / "units"),
    }
    (tmp_path / "units").mkdir()
    result = subprocess.run(  # noqa: S603
        ["bash", str(INSTALL), str(site)],  # noqa: S607
        env=env,
        check=False,
        timeout=60,
        capture_output=True,
        text=True,
    )
    return result, site, log.read_text() if log.exists() else ""


def test_installer_needs_a_fixed_version(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "id").write_text("#!/bin/bash\necho 0\n")
    (bin_dir / "id").chmod(0o755)
    site = tmp_path / "site"
    (site / "data").mkdir(parents=True)
    (site / "compose.yaml").write_text("    image: ghcr.io/aistuartai/googich_takeaway:latest\n")
    result = subprocess.run(  # noqa: S603
        ["bash", str(INSTALL), str(site)],  # noqa: S607
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        check=False,
        timeout=60,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "should name a fixed version" in result.stderr


def test_an_updater_folder_swapped_for_a_link_is_refused(tmp_path: Path) -> None:
    """A link to another root-owned sticky folder (such as /dev/shm) must not pass."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    elsewhere.chmod(0o1770)
    (elsewhere / "request.json").write_text('{"version": "0.1.2"}')
    data = tmp_path / "opt" / "data"
    data.mkdir(parents=True)
    (data / "updater").symlink_to(elsewhere)
    env = {
        "PATH": "/usr/bin:/bin",
        "GOOGICH_COMPOSE_DIR": str(tmp_path / "opt"),
        "GOOGICH_UPDATER_OWNER": str(os.getuid()),
        "GOOGICH_RUNTIME_DIR": str(tmp_path / "run"),
    }
    result = subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT)],  # noqa: S607
        env=env,
        check=False,
        timeout=60,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "is a link" in result.stderr
    assert (elsewhere / "request.json").exists()  # untouched
    assert not (elsewhere / "status.json").exists()
