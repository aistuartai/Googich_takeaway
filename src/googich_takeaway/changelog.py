"""The change log: what each release brought, shown under Help → Updates.

The app carries its own CHANGELOG.md, so the releases up to the one running can always be read,
with no network. When the update check finds a newer release, the app also fetches that
release's CHANGELOG.md from GitHub once, so what the update brings can be read before installing
it. That text is treated as untrusted: raw HTML is shown as text, images are dropped, and only
web links and links within the page are kept.
"""

import json
import re
import xml.etree.ElementTree as etree
from dataclasses import dataclass
from datetime import date, datetime
from functools import cache, lru_cache
from importlib.resources import files
from pathlib import Path

import httpx
import markdown
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor
from markupsafe import Markup

from googich_takeaway import __version__, updates
from googich_takeaway.state import State

MAX_BYTES = 1_000_000
"""A change log longer than this is not read: ours is a small fraction of it."""
_HEADING = re.compile(
    r"^## \[(?P<version>[^\]]+)\](?:\s*-\s*(?P<date>\d{4}-\d{2}-\d{2}))?[ \t]*$", re.M
)
KINDS = ("Added", "Changed", "Fixed", "Removed", "Deprecated", "Security")


def remote_url(version: str) -> str:
    return f"https://raw.githubusercontent.com/{updates.REPOSITORY}/v{version}/CHANGELOG.md"


@dataclass(frozen=True)
class Release:
    version: str
    released: date | None
    html: Markup
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


class _Safe(Treeprocessor):
    """Keeps only web links and links within the page; marks the kind headings for styling."""

    def run(self, root: etree.Element) -> None:
        for parent in root.iter():
            for child in list(parent):
                if child.tag == "img":
                    parent.remove(child)
        for link in root.iter("a"):
            href = link.get("href", "")
            if href.startswith("https://"):
                link.set("target", "_blank")
                link.set("rel", "noopener noreferrer")
            elif not href.startswith("#"):
                link.attrib.pop("href", None)
        for heading in root.iter("h3"):
            text = (heading.text or "").strip()
            if text in KINDS:
                heading.set("class", f"change-kind change-{text.lower()}")


class _ChangelogExtension(Extension):
    def extendMarkdown(self, md: markdown.Markdown) -> None:
        md.treeprocessors.register(_Safe(md), "changelog-safe", 1)
        md.preprocessors.deregister("html_block")
        md.inlinePatterns.deregister("html")


def _render(text: str) -> Markup:
    converter = markdown.Markdown(extensions=["sane_lists", _ChangelogExtension()])
    return Markup(converter.convert(text))  # noqa: S704 - raw HTML removed, links checked


def parse(text: str) -> list[Release]:
    """Each release in a Keep a Changelog file, newest first; Unreleased is left out."""
    parts = _HEADING.split(text)
    releases = []
    for i in range(1, len(parts), 3):
        version, day, body = parts[i], parts[i + 1], parts[i + 2]
        if updates.parse_version(version) is None:
            continue
        try:
            released = date.fromisoformat(day) if day else None
        except ValueError:
            released = None
        kinds = tuple(k for k in KINDS if re.search(rf"^### {k}\s*$", body, re.M))
        releases.append(Release(version.lstrip("v"), released, _render(body), kinds))
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


def _cached_text(state: State, version: str) -> str | None:
    stored = state.get_setting("updates.changelog")
    if not stored:
        return None
    data = json.loads(stored)
    return str(data["text"]) if data.get("version") == version else None


@lru_cache(maxsize=2)
def _parse_cached(text: str) -> list[Release]:
    return parse(text)


def releases(state: State) -> list[Release]:
    """Every release known, newest first: this version's own change log, with the newest
    release's added when it was fetched (its wording wins where both list a release)."""
    own = bundled()
    known = updates.cached(state) if updates.enabled(state) else None
    text = _cached_text(state, known.latest) if known is not None and known.newer else None
    if text is None:
        return own
    merged = {r.version: r for r in own} | {r.version: r for r in _parse_cached(text)}
    return sorted(merged.values(), key=lambda r: updates.parse_version(r.version) or (0, 0, 0),
                  reverse=True)  # fmt: skip


def fetch(
    state: State,
    version: str,
    now: datetime,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 10.0,
) -> bool:
    """Fetch and keep the change log of ``version``, unless kept already. Never raises.

    Kept only if it lists that version, so a wrong or partial answer is never shown."""
    if updates.parse_version(version) is None:
        return False
    if _cached_text(state, version) is not None:
        return True
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            response = client.get(
                remote_url(version), headers={"User-Agent": f"googich-takeaway/{__version__}"}
            )
        response.raise_for_status()
        if len(response.content) > MAX_BYTES:
            return False
        text = response.content.decode("utf-8")
    except (httpx.HTTPError, UnicodeDecodeError):
        return False
    if not any(r.version == version for r in parse(text)):
        return False
    state.set_setting("updates.changelog", json.dumps({"version": version, "text": text}), now)
    return True
