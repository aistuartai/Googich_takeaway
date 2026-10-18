"""Runs rehearsal/proxmox.sh with stand-ins for Proxmox's qm and pvesh, ssh and scp, so its
refusals, its choice of VM ID and its clean-up are checked on every change."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "rehearsal" / "proxmox.sh"
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

INTERFACES = (
    '[{"name": "lo", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "127.0.0.1"}]},'
    ' {"name": "eth0", "ip-addresses": [{"ip-address-type": "ipv6", "ip-address": "fe80::1"},'
    ' {"ip-address-type": "ipv4", "ip-address": "203.0.113.7"}]}]'
)
STUBS = {
    "qm": """#!/bin/bash
echo "qm $*" >> "$STUB_LOG"
case "$1 $2" in
  "guest cmd") printf '%s' "$STUB_INTERFACES" ;;
esac
exit 0
""",
    "pvesh": """#!/bin/bash
echo "pvesh $*" >> "$STUB_LOG"
[ "$4" = "$STUB_TAKEN" ] && exit 1
exit 0
""",
    "ssh": """#!/bin/bash
echo "ssh ${*: -6}" >> "$STUB_LOG"
case "$*" in *guest.sh*) exit "$STUB_OUTCOME" ;; esac
exit 0
""",
    "scp": """#!/bin/bash
echo "scp ${*: -2}" >> "$STUB_LOG"
case "$*" in *results/.*) target="${*: -1}"; echo '{"passed": true}' > "$target/result.json" ;; esac
exit 0
""",
    "ssh-keygen": """#!/bin/bash
while [ $# -gt 0 ]; do case "$1" in -f) key="$2"; shift 2 ;; *) shift ;; esac; done
echo private > "$key"; echo public > "$key.pub"
""",
    "sleep": "#!/bin/bash\nexit 0\n",
}


def run(
    tmp_path: Path, *args: str, outcome: int = 0, conf_mode: int = 0o600, conf: str | None = None
) -> tuple[subprocess.CompletedProcess[str], str, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in STUBS.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    snippets = tmp_path / "snippets"
    snippets.mkdir(exist_ok=True)
    settings = tmp_path / "rehearsal.env"
    settings.write_text(
        conf
        if conf is not None
        else (
            "TEMPLATE=8000\nSTORAGE=test-store\nFIRST_VMID=500\nSNIPPETS_STORAGE=test-snip\n"
            f"SNIPPETS_DIR={snippets}\nRESULTS={tmp_path / 'results'}\n"
        )
    )
    settings.chmod(conf_mode)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "STUB_LOG": str(tmp_path / "log"),
        "STUB_TAKEN": "500",
        "STUB_INTERFACES": INTERFACES,
        "STUB_OUTCOME": str(outcome),
        "GOOGICH_REHEARSAL_CONF": str(settings),
        "GOOGICH_REHEARSAL_OWNER": str(os.getuid()),
        "TMPDIR": str(tmp_path),
    }
    result = subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT), *args],  # noqa: S607
        env=env,
        check=False,
        timeout=60,
        capture_output=True,
        text=True,
    )
    log = (tmp_path / "log").read_text() if (tmp_path / "log").exists() else ""
    return result, log, snippets


def test_rehearses_in_a_fresh_vm_and_destroys_it(tmp_path: Path) -> None:
    result, log, snippets = run(tmp_path, "--from", "0.6.0-rc1", "--to", "0.6.0-rc2")
    assert result.returncode == 0, result.stderr
    assert "qm clone 8000 501 --name googich-rehearsal-501 --full 1 --storage test-store" in log
    assert "--cicustom vendor=test-snip:snippets/googich-rehearsal-501.yaml" in log
    assert "qm resize 501 scsi0 +20G" in log
    assert "guest.sh --from 0.6.0-rc1 --to 0.6.0-rc2" in log
    assert "rehearsal@203.0.113.7" in log  # the VM's address, not loopback or IPv6
    assert "qm destroy 501 --purge 1" in log
    assert list(snippets.iterdir()) == []  # the cloud-init snippet removed too
    assert "PASSED: 0.6.0-rc1 to 0.6.0-rc2" in result.stdout
    kept = list((tmp_path / "results").iterdir())
    assert len(kept) == 1
    assert (kept[0] / "result.json").exists()


def test_a_failed_rehearsal_fails_and_still_cleans_up(tmp_path: Path) -> None:
    result, log, _ = run(tmp_path, "--to", "0.6.0-rc2", outcome=1)
    assert result.returncode == 1
    assert "guest.sh --from 0.6.0-rc2 --to 0.6.0-rc2" in log  # no --from: fresh install only
    assert "FAILED" in result.stdout
    assert "qm destroy 501" in log


def test_keep_leaves_the_vm_and_says_how_to_remove_it(tmp_path: Path) -> None:
    result, log, _ = run(tmp_path, "--to", "0.6.0", "--keep")
    assert result.returncode == 0, result.stderr
    assert "qm destroy" not in log
    assert "qm destroy 501 --purge 1" in result.stdout


@pytest.mark.parametrize(
    ("args", "conf", "mode", "message"),
    [
        (("--to", "latest"), None, 0o600, "--to should be a published version"),
        (("--to", "0.6.0", "--from", "0.6.0; reboot"), None, 0o600, "--from should be"),
        (("--to", "0.6.0"), None, 0o644, "must be owned by root with mode 600"),
        (("--to", "0.6.0"), "TEMPLATE=8000\n", 0o600, "does not set STORAGE"),
        (("--to", "0.6.0", "--force"), None, 0o600, "unknown argument --force"),
    ],
)
def test_refuses_before_touching_anything(
    tmp_path: Path, args: tuple[str, ...], conf: str | None, mode: int, message: str
) -> None:
    result, log, _ = run(tmp_path, *args, conf=conf, conf_mode=mode)
    assert result.returncode == 1
    assert message in result.stderr
    assert "qm" not in log
