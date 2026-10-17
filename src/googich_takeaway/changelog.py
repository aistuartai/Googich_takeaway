"""The change log: what each release brought, shown under Help → Updates.

The app carries its own CHANGELOG.md, so the releases up to the one running can always be read,
with no network. When the update check finds a newer release, the app also fetches that
release's CHANGELOG.md from GitHub once, so what the update brings can be read before installing
it. The fetched text is treated as untrusted: it is used only for the releases newer than the one
running, raw HTML is shown as text, images are dropped, and only web links and links within the
page are kept. The request is anonymous, like the update check, and sends nothing but a
User-Agent naming the app version.
"""

import json
import re
import xml.etree.ElementTree as etree
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import cache, lru_cache
from importlib.resources import files
from pathlib import Path

import httpx
import markdown
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor
from markupsafe import Markup

from googich_takeaway import __version__, updates
from googich_takeaway.safe_markdown import forbid_raw_html, open_in_new_tab, split_at
from googich_takeaway.state import State

MAX_BYTES = 1_000_000
"""A change log longer than this is not read: ours is a small fraction of it."""
RETRY_AFTER = timedelta(hours=1)
"""How long a failed fetch waits before the daily check or a page asks again."""
KINDS = ("Added", "Changed", "Fixed", "Removed", "Deprecated", "Security")
_RELEASE = re.compile(
    r"^## \[(?P<version>[^\]]+)\](?:\s*-\s*(?P<date>\d{4}-\d{2}-\d{2}))?[ \t]*$", re.M
)
_KEPT = "updates.changelog"
_ATTEMPTED = "updates.changelog_attempted"


def remote_url(version: str) -> str:
    return f"https://raw.githubusercontent.com/{updates.REPOSITORY}/v{version}/CHANGELOG.md"


@dataclass(frozen=True)
class Release:
    version: str
    released: date | None
    body: str
    """The Markdown under the release's heading."""
    kinds: tuple[str, ...]
    """Which of Added, Changed, Fixed and the like it has, in order."""

    @property
    def anchor(self) -> str:
        return f"v{self.version}"

    @property
    def newer(self) -> bool:
        """Newer than the version running."""
        return updates.is_newer(self.version, __version__)

    @property
    def running(self) -> bool:
        return self.version == __version__

    def notes(self, level: int = 3) -> Markup:
        """The notes as HTML, their Added, Changed and Fixed headings at ``level``."""
        return _render(self.body, level)


class _UntrustedLinks(Treeprocessor):
    """Drops images, and every link but web links and links within the page."""

    def run(self, root: etree.Element) -> None:
        images = [
            (parent, child) for parent in root.iter() for child in parent if child.tag == "img"
        ]
        for parent, image in images:
            parent.remove(image)
        for link in root.iter("a"):
            href = link.get("href", "")
            if href.startswith("https://"):
                open_in_new_tab(link)
            elif not href.startswith("#"):
                link.attrib.pop("href", None)


class _KindHeadings(Treeprocessor):
    """Puts the ``### Added`` headings at the level the page needs, marked for styling."""

    def __init__(self, md: markdown.Markdown, level: int) -> None:
        super().__init__(md)
        self.level = level

    def run(self, root: etree.Element) -> None:
        for heading in root.iter():
            if heading.tag not in ("h1", "h2", "h3", "h4", "h5", "h6"):
                continue
            heading.tag = f"h{min(6, max(self.level, int(heading.tag[1]) - 3 + self.level))}"
            text = (heading.text or "").strip()
            if text in KINDS:
                heading.set("class", f"change-kind change-{text.lower()}")


class _ChangelogExtension(Extension):
    def __init__(self, level: int) -> None:
        super().__init__()
        self.level = level

    def extendMarkdown(self, md: markdown.Markdown) -> None:
        md.treeprocessors.register(_UntrustedLinks(md), "changelog-links", 2)
        md.treeprocessors.register(_KindHeadings(md, self.level), "changelog-headings", 1)
        forbid_raw_html(md)


@lru_cache(maxsize=256)
def _render(body: str, level: int) -> Markup:
    converter = markdown.Markdown(extensions=["sane_lists", _ChangelogExtension(level)])
    return Markup(converter.convert(body))  # noqa: S704 - raw HTML removed, links checked


def parse(text: str) -> list[Release]:
    """Each release in a Keep a Changelog file, newest first; Unreleased is left out."""
    releases = []
    for version, day, body in split_at(_RELEASE, text)[1]:
        if updates.parse_version(version) is None:
            continue
        try:
            released = date.fromisoformat(day) if day else None
        except ValueError:
            released = None
        kinds = tuple(k for k in KINDS if re.search(rf"^### {k}\s*$", body, re.M))
        releases.append(Release(version.lstrip("v"), released, body, kinds))
    return releases


def _bundled_text() -> str:
    shipped = files("googich_takeaway").joinpath("CHANGELOG.md")
    in_checkout = Path(__file__).parents[2] / "CHANGELOG.md"
    for read in (shipped.read_text, in_checkout.read_text):  # installed app, then a checkout
        try:
            return read(encoding="utf-8")
        except OSError:
            continue
    return ""


@cache
def bundled() -> list[Release]:
    """The releases this version's own change log lists."""
    return parse(_bundled_text())


@dataclass(frozen=True)
class _Kept:
    """The change log fetched for a newer release, as saved in the state database."""

    version: str
    text: str

    @classmethod
    def load(cls, state: State) -> "_Kept | None":
        stored = state.get_setting(_KEPT)
        if not stored:
            return None
        data = json.loads(stored)
        return cls(str(data["version"]), str(data["text"]))

    def save(self, state: State, now: datetime) -> None:
        state.set_setting(_KEPT, json.dumps({"version": self.version, "text": self.text}), now)


@lru_cache(maxsize=2)
def _parse_kept(text: str) -> list[Release]:
    return parse(text)


def releases(state: State) -> list[Release]:
    """Every release known, newest first: this version's own change log, and the releases newer
    than it from the newest release's change log, once that was fetched."""
    own = bundled()
    known = updates.cached(state)
    kept = _Kept.load(state)
    if known is None or not known.newer or kept is None or kept.version != known.latest:
        return own
    newer = [r for r in _parse_kept(kept.text) if r.newer]
    return newer + [r for r in own if r.version not in {n.version for n in newer}]


def has_notes_for(state: State, version: str) -> bool:
    kept = _Kept.load(state)
    return kept is not None and kept.version == version


def fetch_newer(
    state: State,
    now: datetime,
    transport: httpx.BaseTransport | None = None,
    retry_after: timedelta | None = RETRY_AFTER,
) -> bool:
    """Fetch the change log of the newer release the last check found, unless kept already.

    After a failure, waits ``retry_after`` before asking again (None: ask now, for Check now,
    which has its own limit). Never raises; True if the notes are kept."""
    known = updates.cached(state)
    if known is None or not known.newer:
        return False
    if has_notes_for(state, known.latest):
        return True
    attempted = state.get_setting(_ATTEMPTED)
    if retry_after and attempted and now - datetime.fromisoformat(attempted) < retry_after:
        return False
    state.set_setting(_ATTEMPTED, now.isoformat(), now)
    return fetch(state, known.latest, now, transport)


def fetch(
    state: State,
    version: str,
    now: datetime,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 10.0,
) -> bool:
    """Fetch and keep the change log of ``version``. Never raises; True if kept.

    Kept only if it lists that version, so a wrong or partial answer is never shown."""
    if updates.parse_version(version) is None:
        return False
    try:
        with (
            httpx.Client(timeout=timeout, transport=transport) as client,
            client.stream(
                "GET",
                remote_url(version),
                headers={"User-Agent": f"googich-takeaway/{__version__}"},
            ) as response,
        ):
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                content += chunk
                if len(content) > MAX_BYTES:
                    return False
        text = content.decode("utf-8")
    except (httpx.HTTPError, UnicodeDecodeError):
        return False
    if not any(r.version == version for r in parse(text)):
        return False
    _Kept(version, text).save(state, now)
    return True
