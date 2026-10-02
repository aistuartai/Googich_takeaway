"""The web application.

Every page except the login, first-run setup, health check and static files needs a session.
Every state-changing request must come from this site (Origin check) and, once logged in, carry
the session's CSRF token. Responses carry a strict Content Security Policy: scripts and styles
load only from this server, and the pages contain no inline script.
"""

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from googich_takeaway import __version__, updates
from googich_takeaway.config import (
    Config,
    Look,
)
from googich_takeaway.credentials import SecretBox, load_master_key, master_key_path
from googich_takeaway.destinations.immich import ImmichClient
from googich_takeaway.locations import (
    SmbSettings,
    close_shared_smb,
)
from googich_takeaway.logs import LogBuffer, Logs
from googich_takeaway.pipeline import DriveFactory, ImmichFactory
from googich_takeaway.progress import format_duration, format_size
from googich_takeaway.sources.gdrive import GoogleDriveSource
from googich_takeaway.state import State
from googich_takeaway.summaries import Summaries, drive_labels
from googich_takeaway.web import auth
from googich_takeaway.web.common import (
    BODY_LIMIT,
    COOKIE,
    HERE,
    NOTICES,
    OPEN_BODY_LIMIT,
    RUN_NOTICES,
    UNSAFE_METHODS,
    WebSettings,
    _body_refused,
    _csrf_ok,
    _export_date,
    _is_open,
    _same_origin,
    _session_csrf,
    _with_headers,
    log,
)
from googich_takeaway.web.routes import auth as auth_routes
from googich_takeaway.web.routes import (
    cleanup_pages,
    dashboard,
    destinations,
    guide,
    sources,
    update_pages,
)
from googich_takeaway.web.routes import logs as log_routes
from googich_takeaway.web.routes import settings as settings_routes
from googich_takeaway.web.shared import Shared
from googich_takeaway.worker import PAUSED_RUN, Worker

__all__ = ["COOKIE", "WebSettings", "create_app"]


def create_app(
    settings: WebSettings,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    throttle: auth.LoginThrottle | None = None,
    immich_factory: ImmichFactory = ImmichClient,
    drive_factory: DriveFactory = GoogleDriveSource,
    start_worker: bool = True,
    smb_test: Callable[[SmbSettings], None] | None = None,
    logs: Logs | None = None,
    github_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    async def csrf_guard(request: Request) -> None:
        # A dependency, not middleware: it shares FastAPI's parsed form with the route. Reading
        # the body in middleware would consume it before the route sees its form fields.
        if request.method not in UNSAFE_METHODS or _is_open(request.url.path):
            return
        expected = getattr(request.state, "csrf", None)
        if expected is None or not await _csrf_ok(request, expected):
            raise HTTPException(status_code=403, detail="Missing or wrong CSRF token.")

    box = SecretBox(
        load_master_key(settings.master_key_path or master_key_path(settings.state_path))
    )
    worker = Worker(
        settings.state_path,
        box,
        clock=clock,
        immich_factory=immich_factory,
        drive_factory=drive_factory,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if start_worker:
            worker.start()
        yield
        if start_worker:
            worker.stop()
        close_shared_smb()

    app = FastAPI(
        title="Googich Takeaway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(csrf_guard)],
        lifespan=lifespan,
    )
    app.state.worker = worker
    log_store = logs or Logs(buffer=LogBuffer(), directory=settings.state_path.parent / "logs")
    if logs is None:
        logging.getLogger().addHandler(log_store.buffer)  # tests and embedded use
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        version=__version__,
        default_look=Look(),
        demo=settings.demo,
        installer_sha256=updates.installer_sha256(),
    )
    templates.env.filters.update(
        duration=format_duration, size=format_size, export_date=_export_date
    )
    login_throttle = throttle or auth.LoginThrottle()
    data_dir = settings.state_path.parent

    with State(settings.state_path) as state:
        needs_setup = state.password_hash() is None
    app.state.setup_token = auth.new_token() if needs_setup else None
    if app.state.setup_token:
        # The only place this token is ever shown. It stops anyone else on the network from
        # claiming a fresh install before its owner does.
        log.warning("First-run setup: open /setup and enter this token: %s", app.state.setup_token)

    def page(
        request: Request,
        config: Config,
        name: str,
        status_code: int = 200,
        **context: object,
    ) -> Response:
        context.setdefault("immich_link", config.immich().link)
        context.setdefault("look", config.look())
        known = updates.cached(config.state) if updates.enabled(config.state) else None
        context.setdefault("update", known if known and known.newer else None)
        context.setdefault("helper", updates.helper_status(data_dir))
        context.setdefault("banner", update_banner(config))
        context.setdefault("activity", activity(config.state))
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    def activity(state: State) -> dict[str, object] | None:
        """What the menu bar says about runs: the stage working now and how far it is, or how
        far a paused run got. None when nothing is going on, so the menu bar shows nothing."""
        if worker.status_running():
            stopping = worker.tracker.stopping
            snapshot = worker.progress()
            view = next((v for v in snapshot.stages if v.stage == snapshot.stage), None)
            if view is None and snapshot.stages:
                view = snapshot.stages[-1]
            if stopping:
                label = "Pausing" if stopping == "pause" else "Cancelling"
            else:
                label = view.label if view is not None else "Starting"
            return {
                "kind": "stopping" if stopping else "running",
                "label": label,
                "percent": int(view.fraction * 100) if view is not None and view.total else None,
            }
        stored = state.get_setting(PAUSED_RUN)
        if stored is not None:
            # The furthest stage the paused run reached, as on the dashboard.
            stages = [v for v in json.loads(stored).get("progress", []) if v.get("total")]
            last = stages[-1] if stages else None
            return {
                "kind": "paused",
                "label": f"Paused: {str(last['label']).lower()}" if last else "Paused",
                "percent": int(last["done"] / last["total"] * 100) if last else None,
            }
        return None

    def update_banner(config: Config) -> dict[str, object] | None:
        """What the banner at the top of every page says about updates, if anything.

        While an update installs it polls /updates/banner, so it keeps checking while the app
        restarts and says when the update is complete, or why it failed, until dismissed."""
        state = config.state
        helper = updates.helper_status(data_dir)
        requested = state.get_setting("updates.requested")
        if helper is not None:
            if helper.state == "updating":
                return {"kind": "updating", "helper": helper}
            finished = helper.version == requested and helper.state in ("done", "failed")
            if requested and not finished:  # the worker clears the request once it is done
                pausing = worker.status_running()
                return {"kind": "waiting", "version": requested, "pausing": pausing}
            recent = helper.at is not None and clock() - helper.at < timedelta(days=7)
            acknowledged = state.get_setting("updates.helper_ack")
            if (
                helper.state in ("done", "failed")
                and recent
                and helper.at is not None
                and acknowledged != helper.at.isoformat()
            ):
                return {"kind": helper.state, "helper": helper}
        known = updates.cached(state) if updates.enabled(state) else None
        if known and known.newer:
            return {"kind": "available", "update": known, "helper": helper}
        return None

    proxy_hint_logged: list[bool] = []

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not proxy_hint_logged and "x-forwarded-proto" in request.headers:
            client = request.client.host if request.client else ""
            if client not in settings.trusted_proxies:
                proxy_hint_logged.append(True)
                log.info(
                    "Requests arrive through a reverse proxy at %s. It works as it is; set "
                    "GOOGICH_TRUSTED_PROXIES=%s so session cookies are marked secure and logins "
                    "are throttled per visitor rather than per proxy.",
                    client,
                    client,
                )
        if request.method in UNSAFE_METHODS and not _same_origin(request):
            return _with_headers(Response("Cross-site request refused.", status_code=403))
        path = request.url.path
        if request.method in UNSAFE_METHODS:
            refused = _body_refused(request, OPEN_BODY_LIMIT if _is_open(path) else BODY_LIMIT)
            if refused is not None:
                return _with_headers(refused)
        if not _is_open(path):
            csrf = await run_in_threadpool(_session_csrf, request, settings.state_path, clock())
            if csrf is None:
                if request.headers.get("HX-Request"):
                    response: Response = Response(
                        status_code=401, headers={"HX-Redirect": "/login"}
                    )
                else:
                    response = RedirectResponse("/login", status_code=303)
                return _with_headers(response)
            request.state.csrf = csrf
        return _with_headers(await call_next(request))

    summaries = Summaries(worker)
    listing_cache = summaries.listing_cache
    journey = summaries.journey
    source_summaries = summaries.source_summaries
    destination_summary = summaries.destination_summary
    cleanup_summary = summaries.cleanup_summary

    def dashboard_notice(key: str) -> str | None:
        if key in RUN_NOTICES and not worker.run_pending_or_going():
            return None
        return NOTICES.get(key)

    def latest_release(config: Config) -> tuple[updates.UpdateInfo | None, str | None]:
        """The latest release, checked as the page opens, and the version to show for it: never
        older than the version running, which a reading saved before an update could be."""
        known = updates.check_on_view(config.state, clock, github_transport)
        if known is None:
            return None, None
        return known, __version__ if updates.is_newer(__version__, known.latest) else known.latest

    def result(request: Request, ok: bool, message: str) -> Response:
        return templates.TemplateResponse(request, "_result.html", {"ok": ok, "message": message})

    def log_usage() -> dict[str, int]:
        """How much space the log files take now."""
        sizes = []
        for path in log_store.files():
            try:
                sizes.append(path.stat().st_size)
            except OSError:
                continue
        return {"files": len(sizes), "bytes": sum(sizes)}

    web = Shared(
        settings=settings,
        worker=worker,
        templates=templates,
        clock=clock,
        box=box,
        data_dir=data_dir,
        log_store=log_store,
        login_throttle=login_throttle,
        immich_factory=immich_factory,
        drive_factory=drive_factory,
        smb_test=smb_test,
        github_transport=github_transport,
        page=page,
        result=result,
        activity=activity,
        update_banner=update_banner,
        latest_release=latest_release,
        journey=journey,
        source_summaries=source_summaries,
        destination_summary=destination_summary,
        cleanup_summary=cleanup_summary,
        drive_labels=drive_labels,
        dashboard_notice=dashboard_notice,
        log_usage=log_usage,
        listing_cache=listing_cache,
    )
    app.state.web = web
    auth_routes.register(app, web)
    dashboard.register(app, web)
    log_routes.register(app, web)
    guide.register(app, web)
    cleanup_pages.register(app, web)
    settings_routes.register(app, web)
    update_pages.register(app, web)
    destinations.register(app, web)
    sources.register(app, web)

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    if settings.trusted_proxies:
        # Added last, so it is the outermost layer: the origin check, Secure cookies and login
        # throttling all see the scheme and client address the proxy reports. Only these
        # proxies' forwarded headers are believed; anyone else's are ignored.
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=list(settings.trusted_proxies))
    return app
