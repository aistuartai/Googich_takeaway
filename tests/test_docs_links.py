"""Every link between guide pages, and to a section of one, leads somewhere."""

import re
from pathlib import Path

import markdown
import pytest

DOCS = Path(__file__).parent.parent / "src" / "googich_takeaway" / "docs"
ROOT = Path(__file__).parent.parent


def _anchors(path: Path) -> set[str]:
    converter = markdown.Markdown(extensions=["toc"])
    converter.convert(path.read_text())
    found: set[str] = set()

    def walk(tokens: list[dict[str, object]]) -> None:
        for token in tokens:
            found.add(str(token["id"]))
            children = token["children"]
            assert isinstance(children, list)
            walk(children)

    walk(converter.toc_tokens)  # type: ignore[attr-defined]
    return found


ANCHORS = {path.stem: _anchors(path) for path in DOCS.glob("*.md")}


@pytest.mark.parametrize("page", sorted(DOCS.glob("*.md")), ids=lambda p: p.stem)
def test_guide_links_lead_somewhere(page: Path) -> None:
    for target in re.findall(r"\]\(([^)\s]+)\)", page.read_text()):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        name, _, anchor = target.partition("#")
        stem = name.removesuffix(".md") if name else page.stem
        assert stem in ANCHORS, f"{page.name}: no guide page {target}"
        if anchor:
            assert anchor in ANCHORS[stem], f"{page.name}: no section {target}"


def test_readme_links_into_the_guide_lead_somewhere() -> None:
    readme = (ROOT / "README.md").read_text()
    for target in re.findall(r"\]\((src/googich_takeaway/docs/[^)\s]+)\)", readme):
        path, _, anchor = target.partition("#")
        assert (ROOT / path).exists(), target
        if anchor:
            assert anchor in ANCHORS[Path(path).stem], target
