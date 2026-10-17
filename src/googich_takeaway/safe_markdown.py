"""What the guide and the change log share when turning Markdown into HTML."""

import re
import xml.etree.ElementTree as etree

import markdown


def forbid_raw_html(md: markdown.Markdown) -> None:
    """Show raw HTML in the Markdown as text, so a page can never carry markup or script."""
    md.preprocessors.deregister("html_block")
    md.inlinePatterns.deregister("html")


def open_in_new_tab(link: etree.Element) -> None:
    link.set("target", "_blank")
    link.set("rel", "noopener noreferrer")


def split_at(heading: re.Pattern[str], text: str) -> tuple[str, list[tuple[str, ...]]]:
    """The text before the first heading, then each heading's groups with the text under it."""
    parts = heading.split(text)
    stride = heading.groups + 1
    return parts[0], [tuple(parts[i : i + stride]) for i in range(1, len(parts), stride)]
