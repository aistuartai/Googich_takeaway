"""The guide: Markdown files in ``googich_takeaway/docs``, shown in the web interface.

The same files read correctly on GitHub. Links between them (``takeout.md#anchor``) become links
to ``/help/<topic>``, and links to other sites open in a new tab. Raw HTML in the Markdown is
shown as text, never passed through, so a page can never carry markup or script of its own.
"""

import xml.etree.ElementTree as etree
from dataclasses import dataclass
from functools import cache
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
    Topic("first-setup", "First-time setup", "Everything to set up, in order."),
    Topic("takeout", "Setting up Google Takeout", "A scheduled export of Google Photos."),
    Topic("google-drive", "Connecting Google Drive", "A read-only service account."),
    Topic("destinations", "Immich and the download folder", "Where photos go, and wait."),
    Topic("schedule", "Schedule and notifications", "When runs happen, and messages."),
    Topic("dates", "Dates and time zones", "How each photo's date is worked out."),
    Topic("cleanup", "Cleanup", "Freeing space safely, here and in Google Drive."),
    Topic("updates", "Updates", "New releases, and one-click updates."),
    Topic("security", "Security and backups", "What is protected, and what to back up."),
    Topic("troubleshooting", "Troubleshooting", "Common problems and what to do."),
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
