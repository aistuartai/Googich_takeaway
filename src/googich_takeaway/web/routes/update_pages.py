"""Updates: checking for releases and one-click updates."""

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from googich_takeaway import updates
from googich_takeaway.state import State
from googich_takeaway.web.common import (
    _zone,
    log,
)
from googich_takeaway.web.shared import ConfigDep, Shared, StateDep


def register(app: FastAPI, web: Shared) -> None:
    settings = web.settings
    worker = web.worker
    templates = web.templates
    clock = web.clock
    data_dir = web.data_dir
    github_transport = web.github_transport
    page = web.page
    update_banner = web.update_banner
    latest_release = web.latest_release

    @app.get("/updates", response_class=HTMLResponse)
    def updates_view(request: Request, config: ConfigDep) -> Response:
        known, latest = latest_release(config)
        return page(
            request,
            config,
            "updates.html",
            latest_shown=latest,
            updates_enabled=updates.enabled(config.state),
            update_known=known,
            update_available=known if known and known.newer else None,
            saved=request.query_params.get("saved"),
            zone=_zone(config.general().timezone),
        )

    @app.post("/updates/apply")
    def apply_update(config: ConfigDep) -> Response:
        """Ask the host helper to install the newest release the app itself found.

        A run going now is paused first, so the restart cuts nothing off; it resumes by itself
        once the new version starts."""
        known = updates.cached(config.state)
        if known is None or not known.newer:
            return RedirectResponse("/updates?saved=no-update", status_code=303)
        if updates.helper_status(data_dir) is None:
            return RedirectResponse("/updates?saved=no-helper", status_code=303)
        version = known.latest  # never a version from the browser
        config.state.set_setting("updates.requested", version, clock())

        def send() -> None:
            try:
                updates.request_update(data_dir, version)
            except (ValueError, OSError) as error:
                log.warning("Update request refused: %s", error)
                with State(settings.state_path) as state:
                    state.set_setting("updates.requested", None, clock())
                return
            log.warning("Update to %s requested from the web interface", version)

        if worker.pause_then(send):
            return RedirectResponse("/updates?saved=update-after-pause", status_code=303)
        return RedirectResponse("/updates?saved=update-requested", status_code=303)

    @app.get("/updates/banner", response_class=HTMLResponse)
    def updates_banner(request: Request, config: ConfigDep) -> Response:
        """Polled by the banner while an update installs."""
        return templates.TemplateResponse(
            request, "_update_banner.html", {"banner": update_banner(config)}
        )

    @app.post("/updates/dismiss")
    def dismiss_update_banner(request: Request, config: ConfigDep) -> Response:
        helper = updates.helper_status(data_dir)
        if helper is not None and helper.at is not None:
            config.state.set_setting("updates.helper_ack", helper.at.isoformat(), clock())
        back = request.headers.get("referer", "/")
        path = urlsplit(back).path or "/"
        local = path.startswith("/") and not path.startswith(("//", "/\\"))  # never off-site
        return RedirectResponse(path if local else "/", status_code=303)

    @app.post("/updates/check")
    def check_updates(state: StateDep) -> Response:
        found = updates.check_now(state, clock, github_transport)
        return RedirectResponse(f"/updates?saved=check-{found.value}", status_code=303)

    @app.post("/updates/daily")
    def save_updates(state: StateDep, check: Annotated[str, Form()] = "") -> Response:
        updates.set_enabled(state, bool(check), clock())
        return RedirectResponse("/updates?saved=updates", status_code=303)
