"""An in-memory stand-in for the parts of GitHub the update check and the change log use.

The releases page answers with a redirect to the latest release's tag, the API with that
release as JSON, the releases feed with every release (pre-releases too), and
raw.githubusercontent.com with the release's CHANGELOG.md. Release lookups, feed reads and
change log fetches are counted apart.
"""

import httpx

CHANGELOG = """# Changelog

## [Unreleased]

## [9.9.9] - 2026-10-20

### Added

- **Time travel**: photos arrive before they are taken.

### Fixed

- Nothing was broken.

## [0.0.1] - 2026-10-01

### Added

- The first release, as GitHub tells it.
"""


class FakeGitHub:
    def __init__(self, body: dict[str, object] | None = None, status: int = 200) -> None:
        self.body = body or {
            "tag_name": "v9.9.9",
            "html_url": "https://github.com/aistuartai/Googich_takeaway/releases/tag/v9.9.9",
            "prerelease": False,
            "draft": False,
        }
        self.status = status
        self.calls = 0
        """Release lookups: the releases page and the API."""
        self.page = True
        """Answer the releases page with a redirect, as GitHub does; False: only the API."""
        self.changelog: str | None = CHANGELOG
        """CHANGELOG.md as raw.githubusercontent.com serves it; None: not found."""
        self.changelog_calls = 0
        self.feed: list[str] | None = ["v9.9.9", "v0.0.1"]
        """The tags the releases feed lists, newest first; None: the feed is not found."""
        self.feed_calls = 0

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            assert "authorization" not in request.headers
            assert request.headers["user-agent"].startswith("googich-takeaway/")
            if request.url.host == "raw.githubusercontent.com":
                self.changelog_calls += 1
                if self.changelog is None:
                    return httpx.Response(404)
                return httpx.Response(200, text=self.changelog)
            if request.url.path.endswith("/releases.atom"):
                self.feed_calls += 1
                if self.feed is None:
                    return httpx.Response(404)
                return httpx.Response(200, text=feed(self.feed))
            self.calls += 1
            if request.url.host == "github.com" and not self.page:
                return httpx.Response(404)
            if request.url.host == "github.com":  # the releases page, as GitHub answers it
                tag = str(self.body.get("tag_name", ""))
                if self.status != 200 or self.body.get("prerelease") or self.body.get("draft"):
                    return httpx.Response(404 if self.status == 200 else self.status)
                location = f"https://github.com/aistuartai/Googich_takeaway/releases/tag/{tag}"
                return httpx.Response(302, headers={"location": location})
            return httpx.Response(self.status, json=self.body)

        return httpx.MockTransport(handle)


def feed(tags: list[str]) -> str:
    """The releases feed as GitHub writes it, cut down to what the app reads."""
    entries = "".join(
        f"""
  <entry>
    <id>tag:github.com,2008:Repository/1/{tag}</id>
    <link rel="alternate" type="text/html" href="https://github.com/aistuartai/Googich_takeaway/releases/tag/{tag}"/>
    <title>{tag}</title>
  </entry>"""
        for tag in tags
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="en-US">
  <link type="text/html" rel="alternate" href="https://github.com/aistuartai/Googich_takeaway/releases"/>{entries}
</feed>
"""
