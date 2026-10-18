"""The app installed as the README says: the example compose file in a folder of its own, with a
data folder and master key owned by the app's user, run by real Docker.

Only the image line is changed, so a test can run a release from ghcr.io or the image just built.
Everything else, from the read-only root to the dropped capabilities, is the file as shipped.
"""

import base64
import json
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "docker" / "compose.yaml"
IMAGE_LINE = re.compile(r"^(\s*image:\s*)ghcr\.io/aistuartai/googich_takeaway:\S+\s*$", re.M)
APP_USER = "10001:10001"
URL = "http://127.0.0.1:8080"


LATEST_PAGE = "https://github.com/aistuartai/Googich_takeaway/releases/latest"


def published_version() -> str:
    """The final release users install today, as GitHub's latest release says.

    Not the example compose file's version: on a release commit that names the release being
    made, whose image does not exist until the release workflow has built it."""

    class Stay(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: object, **kwargs: object) -> None:
            return None  # read where it points, without following it

    opener = urllib.request.build_opener(Stay)
    try:
        opener.open(LATEST_PAGE, timeout=30)
    except urllib.error.HTTPError as answer:
        found = re.search(r"/releases/tag/v(\d+\.\d+\.\d+)$", answer.headers.get("location", ""))
        if answer.code in (301, 302) and found:
            return found[1]
    raise AssertionError(f"{LATEST_PAGE} did not point at a release")


def released_image(version: str) -> str:
    return f"ghcr.io/aistuartai/googich_takeaway:{version}"


def _as_root(command: list[str]) -> list[str]:
    return command if os.geteuid() == 0 else ["sudo", "-n", *command]


@dataclass
class Install:
    folder: Path
    """Where compose.yaml, data/ and secrets/ live, as /opt/googich does on a real host."""

    def __post_init__(self) -> None:
        self.data = self.folder / "data"
        self.compose = self.folder / "compose.yaml"

    @property
    def installed(self) -> bool:
        return self.compose.exists()

    def prepare(self, image: str, example: str | None = None) -> None:
        """Step 1 of the example compose file: the data folder and the master key.

        ``example`` is the compose file to start from: this checkout's, unless given (a
        rehearsal starts from the one the earlier release shipped)."""
        (self.folder / "secrets").mkdir(parents=True)
        self.data.mkdir()
        key = self.folder / "secrets" / "master.key"
        key.write_text(base64.b64encode(secrets.token_bytes(32)).decode() + "\n")
        key.chmod(0o600)
        for folder in (self.data, self.folder / "secrets"):
            folder.chmod(0o700)
        subprocess.run(  # noqa: S603
            _as_root(["chown", "-R", APP_USER, str(self.data), str(self.folder / "secrets")]),
            check=True,
        )
        self.use(image, example)

    def use(self, image: str, example: str | None = None) -> None:
        """The example compose file, running ``image``: what changing the version line does."""
        source = example if example is not None else EXAMPLE.read_text()
        text, swapped = IMAGE_LINE.subn(lambda m: f"{m[1]}{image}", source)
        assert swapped == 1, "the example compose file should have one image line"
        self.compose.write_text(text)

    def compose_cmd(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """docker compose in this folder; a failure says what compose said."""
        done = subprocess.run(  # noqa: S603
            ["docker", "compose", "--project-directory", str(self.folder), *args],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if check and done.returncode != 0:
            raise AssertionError(
                f"docker compose {' '.join(args)} failed ({done.returncode}):\n"
                f"{done.stderr.strip() or done.stdout.strip()}"
            )
        return done

    def up(self) -> None:
        """Step 2, then wait until the app answers (its own health check runs only once a
        minute, too slowly to wait on)."""
        self.compose_cmd("up", "-d")
        deadline = time.monotonic() + 120
        while health() is None:
            if time.monotonic() > deadline:
                raise AssertionError(f"the app never answered:\n{self.logs()}")
            time.sleep(1)

    def down(self) -> None:
        self.compose_cmd("down", check=False)

    def remove(self) -> None:
        """Stop it and give the folders back, so the test's temporary folder can be removed."""
        self.down()
        subprocess.run(  # noqa: S603
            _as_root(["chown", "-R", f"{os.getuid()}:{os.getgid()}", str(self.folder)]),
            check=False,
        )

    def logs(self) -> str:
        return self.compose_cmd("logs", "--no-color", "googich", check=False).stdout

    def setup_token(self) -> str:
        """Step 3 of the example compose file: the one-time token from the logs."""
        for _ in range(60):
            found = re.findall(r"open /setup and enter this token: (\S+)", self.logs())
            if found:
                return str(found[-1])
            time.sleep(1)
        raise AssertionError("no setup token in the logs")

    def container_user(self) -> str:
        return self.compose_cmd("exec", "-T", "googich", "id", "-u").stdout.strip()


def image_version(compose: Path) -> str:
    """The version the image line of a compose file names."""
    found = re.search(r"googich_takeaway:(\S+)", compose.read_text())
    assert found
    return found[1]


def health(timeout: float = 3) -> dict[str, str] | None:
    try:
        with urllib.request.urlopen(f"{URL}/healthz", timeout=timeout) as answer:  # noqa: S310
            return dict(json.loads(answer.read()))
    except (OSError, ValueError, urllib.error.URLError):
        return None


def wait_for_version(version: str, seconds: float = 180) -> None:
    """Until /healthz reports ``version``, as the update helper waits."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        answer = health()
        if answer and answer.get("version") == version:
            return
        time.sleep(2)
    raise AssertionError(f"{version} never became healthy (last answer: {health()})")
