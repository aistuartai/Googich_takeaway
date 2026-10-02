import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from googich_takeaway.state import State
from googich_takeaway.web import help
from tests.web.test_settings import World, _ready


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


DOCS = help.DOCS


def test_every_topic_has_a_page_and_every_page_a_topic() -> None:
    files = {p.stem for p in DOCS.glob("*.md")} - {"README"}
    assert files == set(help.BY_SLUG)
    index = (DOCS / "README.md").read_text()
    for topic in help.TOPICS:
        assert f"]({topic.slug}.md)" in index  # the GitHub index lists it too


@pytest.mark.parametrize("topic", help.TOPICS, ids=lambda t: t.slug)
def test_links_between_pages_lead_somewhere(topic: help.Topic) -> None:
    rendered = help.page(topic.slug)
    assert rendered is not None
    for href in re.findall(r'href="(/help/[^"]+)"', rendered.html):
        slug, _, anchor = href.removeprefix("/help/").partition("#")
        target = help.page(slug)
        assert target is not None, href
        if anchor:
            assert f'id="{anchor}"' in target.html, href
    assert ".md" not in re.sub(r"<code>.*?</code>", "", rendered.html)


def test_raw_html_is_shown_as_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "how-it-works.md").write_text(
        "# Title\n\n<script>alert(1)</script>\n\nText <b onclick=x>bold</b> "
        "[out](https://example.com) [in](takeout.md#where-the-export-goes)\n"
    )
    monkeypatch.setattr(help, "DOCS", tmp_path)
    help.page.cache_clear()
    try:
        rendered = help.page("how-it-works")
        assert rendered is not None
        assert "<script>" not in rendered.html
        assert "&lt;script&gt;" in rendered.html
        assert "<b " not in rendered.html
        link = re.search(r'<a href="https://example.com"[^>]*>', rendered.html)
        assert link
        assert 'target="_blank"' in link[0]
        assert 'rel="noopener noreferrer"' in link[0]
        assert 'href="/help/takeout#where-the-export-goes"' in rendered.html
    finally:
        help.page.cache_clear()


def test_help_pages_in_the_app(world: World) -> None:
    index = world.client.get("/help")
    assert index.status_code == 200
    for topic in help.TOPICS:
        assert f'href="/help/{topic.slug}"' in index.text
    takeout = world.client.get("/help/takeout").text
    assert "every month or every 2 months" in takeout
    assert "On this page" in takeout
    assert world.client.get("/help/nothing-here").status_code == 404
    assert world.client.get("/help/..%2Fconfig").status_code == 404


def test_help_menu_holds_logs_and_about(world: World) -> None:
    page = world.client.get("/about").text
    assert "You are running version" in page
    nav = page[page.index('<nav aria-label="Main">') : page.index("</nav>")]
    menus = nav.split('<details class="nav-menu" name="nav-menus">')
    help_menu = next(m for m in menus if ">Help</summary>" in m)
    for name in ("Guide", "Updates", "Logs", "About"):
        assert f">{name}</a>" in help_menu
    assert '<summary class="current">Help</summary>' in nav


def test_google_photos_station(world: World) -> None:
    _ready(world)
    page = world.client.get("/").text
    assert "counted when the first export is imported" in page
    with State(world.tmp / "state.db") as state:
        state.set_setting(
            "photos.latest_export",
            json.dumps(
                {
                    "export_id": "20261001T010203Z",
                    "items": 48210,
                    "in_immich": 47980,
                    "at": datetime.now(UTC).isoformat(),
                }
            ),
            datetime.now(UTC),
        )
    page = world.client.get("/").text
    assert '<p class="station-figure">48,210</p>' in page
    assert '<a class="station-icon" href="https://photos.google.com/"' in page
    assert '<a href="/help/takeout">Takeout setup</a>' in page
    assert "items in the latest export, 01 Oct 2026" in page
    assert "47,980 of the latest export's 48,210" in page


def test_takeout_schedule_on_sources_page_and_reminder(world: World) -> None:
    from datetime import timedelta

    _ready(world)
    early = world.post("/schedule", data={"mode": "takeout", "at": "03:00"})
    assert early.status_code == 400
    assert "first note the day you set it up" in early.text
    assert world.post("/schedule/takeout", data={"started": "2025-10-15"}).status_code == 303
    assert world.post("/schedule", data={"mode": "takeout", "at": "03:00"}).status_code == 303
    assert "Following Takeout" in world.client.get("/").text
    schedule = world.client.get("/schedule").text
    assert "Coming up" in schedule
    assert 'muted">optional weekly run' not in schedule  # off unless chosen
    world.post("/schedule/follow", data={"fallback_weekly": "1", "fallback_weekday": "6"})
    schedule = world.client.get("/schedule").text
    assert schedule.count("<li>") >= 5
    assert 'muted">optional weekly run' in schedule
    page = world.client.get("/schedule").text
    assert 'value="2025-10-15"' in page
    assert "schedule ends around <strong>15 October 2026</strong>" in page
    assert 'id="follow"' in page
    for expected in ("Wed 15 Oct 2025", "Mon 15 Dec 2025", "Sat 15 Aug 2026"):
        assert f"<td>{expected}</td>" in page
    assert "15 Oct; if not there yet, every day to 28 Oct" in page
    assert "<td>Not seen</td>" in page  # nothing was downloaded in this test
    # Monthly Takeout: twelve exports. Waiting every 3 days, no weekly runs in between.
    world.post("/schedule/takeout", data={"started": "2025-10-15", "every_months": "1"})
    follow = {"retry_days": "3", "wait_days": "7", "fallback_weekday": "2"}
    assert world.post("/schedule/follow", data=follow).status_code == 303
    page = world.client.get("/schedule").text
    assert page.count("<td>Not seen</td>") == 12
    assert "<td>15 Nov; if not there yet, again on 18 Nov, 21 Nov</td>" in page
    assert 'name="fallback_weekly" value="1" >' in page
    assert 'muted">optional weekly run' not in page
    bad = world.post("/schedule/follow", data={**follow, "retry_days": "9"})
    assert bad.status_code == 400
    assert world.post("/schedule", data={"mode": "weekly", "at": "03:00"}).status_code == 303
    assert 'id="follow"' not in world.client.get("/schedule").text
    sources = world.client.get("/sources").text
    assert 'id="takeout"' not in sources
    assert 'href="https://drive.google.com/"' in sources
    assert 'href="/help/google-drive"' in sources
    assert world.post("/schedule/takeout", data={"started": "soon"}).status_code == 400
    nearly_a_year = (datetime.now(UTC) - timedelta(days=360)).date().isoformat()
    world.post("/schedule/takeout", data={"started": nearly_a_year})
    assert "Your Takeout schedule ends soon" in world.client.get("/").text
