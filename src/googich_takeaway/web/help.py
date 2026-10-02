"""The guide: Markdown files in ``googich_takeaway/docs``, shown in the web interface.

The same files read correctly on GitHub. Links between them (``takeout.md#anchor``) become links
to ``/help/<topic>``, and links to other sites open in a new tab. Raw HTML in the Markdown is
shown as text, never passed through, so a page can never carry markup or script of its own.

Search looks through every page, section by section, for all the words asked for.
"""

import re
import xml.etree.ElementTree as etree
from dataclasses import dataclass
from functools import cache
from html import unescape
from pathlib import Path

import markdown
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor
from markupsafe import Markup

DOCS = Path(__file__).parent.parent / "docs"


@dataclass(frozen=True)
class Topic:
    slug: str
    title: str
    summary: str


TOPICS = (
    Topic("how-it-works", "How it works", "The journey from Google Photos to Immich."),
    Topic(
        "what-is-immich",
        "What is Immich?",
        "Your own photo library, and why it pairs with this app.",
    ),
    Topic("immich-setup", "Setting up Immich", "Installing Immich and connecting it to this app."),
    Topic("first-setup", "First-time setup", "Everything to set up, in order."),
    Topic(
        "android-backup",
        "Backing up an Android phone",
        "The Immich app: new photos straight from your phone.",
    ),
    Topic("takeout", "Setting up Google Takeout", "A scheduled export of Google Photos."),
    Topic("google-drive", "Connecting Google Drive", "A read-only service account."),
    Topic(
        "manual-downloads",
        "Downloading exports yourself",
        "No Google Cloud: save Takeout's archives into a folder.",
    ),
    Topic("destinations", "Immich and the download folder", "Where photos go, and wait."),
    Topic("schedule", "Schedule and notifications", "When runs happen, and messages."),
    Topic("dates", "Dates and time zones", "How each photo's date is worked out."),
    Topic("cleanup", "Cleanup", "Freeing space safely, here and in Google Drive."),
    Topic("updates", "Updates", "New releases, and one-click updates."),
    Topic("security", "Security and backups", "What is protected, and what to back up."),
    Topic("troubleshooting", "Troubleshooting", "Common problems and what to do."),
    Topic("command-line", "Command-line tools", "Scanning and importing from a terminal."),
)
BY_SLUG = {topic.slug: topic for topic in TOPICS}


@dataclass(frozen=True)
class Page:
    topic: Topic
    html: Markup
    contents: list[tuple[str, str]]
    """Second-level headings: (anchor, title)."""


class _Links(Treeprocessor):
    def run(self, root: etree.Element) -> None:
        for link in root.iter("a"):
            href = link.get("href", "")
            if href.startswith(("http://", "https://")):
                link.set("target", "_blank")
                link.set("rel", "noopener noreferrer")
                continue
            page, _, anchor = href.partition("#")
            if page.endswith(".md") and "/" not in page:
                slug = page.removesuffix(".md")
                link.set("href", f"/help/{slug}" + (f"#{anchor}" if anchor else ""))


class _GuideExtension(Extension):
    def extendMarkdown(self, md: markdown.Markdown) -> None:
        md.treeprocessors.register(_Links(md), "guide-links", 1)
        # No raw HTML: show it as text.
        md.preprocessors.deregister("html_block")
        md.inlinePatterns.deregister("html")


@cache
def page(slug: str) -> Page | None:
    topic = BY_SLUG.get(slug)
    if topic is None:
        return None
    converter = markdown.Markdown(
        extensions=["tables", "fenced_code", "sane_lists", "toc", _GuideExtension()],
        extension_configs={"toc": {"toc_depth": "2-2"}},
    )
    text = (DOCS / f"{slug}.md").read_text(encoding="utf-8")
    html = converter.convert(text)
    contents = [(t["id"], t["name"]) for t in _flatten(converter.toc_tokens)]  # type: ignore[attr-defined]
    return Page(topic, Markup(html), contents)  # noqa: S704 - our own files, raw HTML removed


def _flatten(tokens: list[dict[str, object]]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for token in tokens:
        if token.get("level") == 2:
            found.append({"id": str(token["id"]), "name": str(token["name"])})
        children = token.get("children")
        if isinstance(children, list):
            found.extend(_flatten(children))
    return found


@dataclass(frozen=True)
class Hit:
    topic: Topic
    anchor: str
    """The section's heading anchor; empty for the text before the first heading."""
    section: str
    snippet: Markup
    """A few words around the first match, with the matches marked."""
    score: int


_HEADING = re.compile(r'<h2 id="([^"]+)">(.*?)</h2>', re.S)
_INLINE_TAGS = re.compile(r"</?(?:a|b|code|em|i|strong)\b[^>]*>")
_TAGS = re.compile(r"<[^>]+>")
SNIPPET_CHARS = 90


@cache
def _sections(slug: str) -> list[tuple[str, str, str]]:
    """(anchor, heading, plain text) for each part of a page, split at its second-level headings."""
    found = page(slug)
    if found is None:
        return []
    html = str(found.html)
    parts = _HEADING.split(html)
    sections = [("", found.topic.title, _plain(parts[0]))]
    for i in range(1, len(parts), 3):
        sections.append((parts[i], _plain(parts[i + 1]), _plain(parts[i + 2])))
    return sections


def _plain(html: str) -> str:
    return " ".join(unescape(_TAGS.sub(" ", _INLINE_TAGS.sub("", html))).split())


def search(query: str, limit: int = 30) -> list[Hit]:
    """Sections of the guide that hold every word of ``query``, best matches first."""
    words = [w for w in query.casefold().split() if w][:8]
    if not words:
        return []
    hits = []
    for topic in TOPICS:
        for anchor, heading, text in _sections(topic.slug):
            haystack = f"{heading} {text}".casefold()
            if not all(w in haystack for w in words):
                continue
            in_heading = sum(w in heading.casefold() or w in topic.title.casefold() for w in words)
            score = in_heading * 100 + sum(haystack.count(w) for w in words)
            hits.append(Hit(topic, anchor, heading, _snippet(text or heading, words), score))
    hits.sort(key=lambda h: -h.score)
    return hits[:limit]


def _snippet(text: str, words: list[str]) -> Markup:
    folded = text.casefold()
    first = min((folded.find(w) for w in words if w in folded), default=0)
    start = max(0, first - SNIPPET_CHARS // 3)
    end = min(len(text), start + SNIPPET_CHARS * 2)
    if start:
        start = text.rfind(" ", 0, start) + 1  # whole words
    piece = text[start:end]
    if end < len(text):
        piece = piece[: piece.rfind(" ")] if " " in piece else piece
    pattern = re.compile("|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)), re.I)
    out = Markup("…") if start else Markup("")
    position = 0
    for match in pattern.finditer(piece):
        out += piece[position : match.start()]
        out += Markup("<mark>%s</mark>") % match.group(0)
        position = match.end()
    out += piece[position:]
    return out + (Markup("…") if end < len(text) else Markup(""))
