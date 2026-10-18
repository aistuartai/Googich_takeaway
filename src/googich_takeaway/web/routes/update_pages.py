"""Updates: checking for releases, what they bring, and one-click updates."""

import threading
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from googich_takeaway import changelog, updates
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

    def fetch_notes_in_background() -> None:
        """Fetch what a newer release brings without holding up the page: it shows the next
        time the page opens. A failure waits an hour before the next try."""

        def fetch() -> None:
            try:
                with State(settings.state_path) as state:
                    changelog.fetch_newer(state, clock(), github_transport)
            except Exception:  # never let a background fetch go unnoticed
                log.exception("Fetching the change log failed")

        threading.Thread(target=fetch, name="changelog-fetch", daemon=True).start()

    @app.get("/updates", response_class=HTMLResponse)
    def updates_view(request: Request, config: ConfigDep) -> Response:
        known, latest = latest_release(config)
        newer = known if known and known.newer else None
        if newer and not changelog.has_notes_for(config.state, newer.latest):
            fetch_notes_in_background()
        return page(
            request,
            config,
            "updates.html",
            latest_shown=latest,
            updates_enabled=updates.enabled(config.state),
            candidates=updates.candidates(config.state),
            update_known=known,
            update_available=newer,
            whats_new=[r for r in changelog.releases(config.state) if r.newer],
            repository=updates.REPOSITORY,
            saved=request.query_params.get("saved"),
            check_error=updates.last_error(config.state),
            zone=_zone(config.general().timezone),
        )

    @app.get("/updates/changelog", response_class=HTMLResponse)
    def changelog_view(request: Request, config: ConfigDep) -> Response:
        """Every release, newest first: those newer than this one, this one and the three
        before it in full, the rest folded away."""
        releases = changelog.releases(config.state)
        running = next((i for i, r in enumerate(releases) if r.running), None)
        shown = (running + 4) if running is not None else 4
        return page(
            request,
            config,
            "changelog.html",
            recent=releases[:shown],
            earlier=releases[shown:],
            newer=[r for r in releases if r.newer],
            repository=updates.REPOSITORY,
        )

    @app.post("/updates/apply")
    def apply_update(config: ConfigDep) -> Response:
        """Ask the host helper to install the newest release the app itself found.

        A run going now is paused first, so the restart cuts nothing off; it resumes by itself
        once the new version starts."""
        known = updates.cached(config.state)
        if known is None or not known.newer:
            return RedirectResponse("/updates?saved=no-update", status_code=303)
        helper = updates.helper_status(data_dir)
        if helper is None:
            return RedirectResponse("/updates?saved=no-helper", status_code=303)
        if known.candidate and not helper.installs_candidates:
            return RedirectResponse("/updates?saved=helper-too-old", status_code=303)
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
        if found is updates.CheckResult.NEWER:  # what it brings, before the page shows it
            changelog.fetch_newer(state, clock(), github_transport, retry_after=None)
        return RedirectResponse(f"/updates?saved=check-{found.value}", status_code=303)

    @app.post("/updates/candidates")
    def save_candidates(state: StateDep, on: Annotated[str, Form()] = "") -> Response:
        """Switch release candidates on or off, then check straight away for the new choice."""
        updates.set_candidates(state, bool(on), clock())
        if updates.check_now(state, clock, github_transport) is updates.CheckResult.NEWER:
            changelog.fetch_newer(state, clock(), github_transport, retry_after=None)
        return RedirectResponse(
            f"/updates?saved=candidates-{'on' if on else 'off'}", status_code=303
        )

    @app.post("/updates/daily")
    def save_updates(state: StateDep, check: Annotated[str, Form()] = "") -> Response:
        updates.set_enabled(state, bool(check), clock())
        return RedirectResponse("/updates?saved=updates", status_code=303)
