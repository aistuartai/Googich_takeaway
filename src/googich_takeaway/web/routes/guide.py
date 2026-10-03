"""The built-in guide and the About page."""

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from googich_takeaway import updates
from googich_takeaway.web import help
from googich_takeaway.web.common import (
    _zone,
)
from googich_takeaway.web.shared import ConfigDep, Shared


def register(app: FastAPI, web: Shared) -> None:
    page = web.page
    latest_release = web.latest_release

    # --- help --------------------------------------------------------------------------------

    @app.get("/help", response_class=HTMLResponse)
    def help_index(request: Request, config: ConfigDep, q: str = "") -> Response:
        query = " ".join(q.split())[:100]
        return page(
            request,
            config,
            "help.html",
            topics=help.TOPICS,
            groups=help.GROUPS,
            q=query,
            hits=help.search(query) if query else [],
        )

    @app.get("/help/{slug}", response_class=HTMLResponse)
    def help_page(request: Request, config: ConfigDep, slug: str) -> Response:
        found = help.page(slug)
        if found is None:
            return page(
                request,
                config,
                "help.html",
                topics=help.TOPICS,
                groups=help.GROUPS,
                status_code=404,
            )
        return page(
            request, config, "help_page.html", doc=found, topics=help.TOPICS, groups=help.GROUPS
        )

    @app.get("/about", response_class=HTMLResponse)
    def about(request: Request, config: ConfigDep) -> Response:
        known, latest = latest_release(config)
        return page(
            request,
            config,
            "about.html",
            update_known=known,
            latest_shown=latest,
            update_available=known if known and known.newer else None,
            repository=updates.REPOSITORY,
            zone=_zone(config.general().timezone),
        )
