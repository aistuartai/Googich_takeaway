"""The web application.

Every page except the login, first-run setup, health check and static files needs a session.
Every state-changing request must come from this site (Origin check) and, once logged in, carry
the session's CSRF token. Responses carry a strict Content Security Policy: scripts and styles
load only from this server, and the pages contain no inline script.
"""

import json
import logging
import threading
import time
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

from googich_takeaway import __version__, cleanup, downloads, updates
from googich_takeaway.config import (
    Config,
    Look,
)
from googich_takeaway.credentials import SecretBox, load_master_key, master_key_path
from googich_takeaway.destinations.immich import ImmichClient
from googich_takeaway.locations import (
    Location,
    LocationError,
    SmbSettings,
    StoredFile,
    close_shared_smb,
)
from googich_takeaway.locations import archives as list_archives
from googich_takeaway.logs import LogBuffer, Logs
from googich_takeaway.pipeline import LATEST_EXPORT_SETTING
from googich_takeaway.progress import Stage, format_duration, format_size
from googich_takeaway.sources.gdrive import GoogleDriveSource
from googich_takeaway.state import State
from googich_takeaway.web import auth
from googich_takeaway.web.common import (
    BODY_LIMIT,
    COOKIE,
    HERE,
    NOTICES,
    OPEN_BODY_LIMIT,
    RUN_NOTICES,
    UNSAFE_METHODS,
    DriveFactory,
    ImmichFactory,
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
    templates.env.globals.update(version=__version__, default_look=Look(), demo=settings.demo)
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

    # Shared by requests running in parallel threads. Single dict reads and writes are atomic;
    # the worst a race can do is read a folder once more than needed. The lock covers the one
    # check-then-change below.
    listing_cache: dict[str, tuple[float, list[StoredFile] | None]] = {}
    cache_lock = threading.Lock()

    def folder_listing(location: Location) -> list[StoredFile] | None:
        """The download folder's archives, read at most every 10 seconds (it may be on SMB).

        None if the folder cannot be read right now."""
        key = location.describe()
        hit = listing_cache.get(key)
        if hit and time.monotonic() - hit[0] < 10:
            return hit[1]
        try:
            found: list[StoredFile] | None = list_archives(location)
        except LocationError:
            found = None
        listing_cache[key] = (time.monotonic(), found)
        return found

    def latest_export(state: State) -> dict[str, object] | None:
        stored = state.get_setting(LATEST_EXPORT_SETTING)
        if not stored:
            return None
        data = json.loads(stored)
        data["at"] = datetime.fromisoformat(data["at"])
        return data if isinstance(data, dict) else None

    downloading_now: list[str] = []

    def journey(config: Config, state: State) -> dict[str, object]:
        drive_count, drive_bytes = state.download_totals()
        location = config.staging_location()
        folder_count: int | None = 0
        folder_bytes = 0
        arriving = False
        if location is not None:
            active = worker.tracker.active() if worker.status_running() else None
            name = active[1].name if active and active[0] is Stage.DOWNLOAD else None
            with cache_lock:
                if downloading_now != [name or ""]:
                    # A download finished or started: read the folder again, counted once.
                    listing_cache.pop(location.describe(), None)
                    downloading_now[:] = [name or ""]
            found = folder_listing(location)
            if found is None:
                folder_count = None
            else:
                folder_count, folder_bytes = len(found), sum(a.size for a in found)
                if active and name and all(a.name != name for a in found):
                    folder_bytes += min(active[1].done, active[1].size)  # arriving now
                    arriving = True
        drive_names = {f"gdrive:{s.location}" for s in config.sources() if s.kind == "gdrive"}
        listed = [v for k, v in downloads.listings(state).items() if k in drive_names]
        photos = latest_export(state)
        return {
            "uses_drive": bool(drive_names),
            "has_sources": bool(config.sources()),
            "drive_count": drive_count,
            "drive_bytes": drive_bytes,
            "drive_listed": sum(int(str(v["count"])) for v in listed) if listed else None,
            "drive_listed_bytes": sum(int(str(v["bytes"])) for v in listed),
            "drive_listed_at": max(datetime.fromisoformat(str(v["at"])) for v in listed)
            if listed
            else None,
            "folder_count": folder_count,
            "folder_bytes": folder_bytes,
            "folder_arriving": arriving,
            "immich_count": state.upload_count("immich"),
            "photos": photos,
            "photos_seen": state.seen_item_count(),
        }

    def cached_call(key: str, seconds: float, read: Callable[[], object]) -> object:
        """Remember a slow reading (an SMB listing, free space) for a few seconds."""
        hit = slow_cache.get(key)
        if hit and time.monotonic() - hit[0] < seconds:
            return hit[1]
        value = read()
        slow_cache[key] = (time.monotonic(), value)
        return value

    slow_cache: dict[str, tuple[float, object]] = {}

    def source_summaries(config: Config, state: State) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        for source in config.sources():
            latest: dict[str, tuple[int, datetime]] = {}
            for record in state.downloads(f"{source.kind}:{source.location}"):
                latest[record.file_id] = (record.size, record.downloaded_at)
            found.append(
                {
                    "source": source,
                    "folder_name": config.drive_folder_name(source.id),
                    "archives": len(latest),
                    "bytes": sum(size for size, _ in latest.values()),
                    "last": max((at for _, at in latest.values()), default=None),
                }
            )
        return found

    def destination_summary(config: Config, state: State) -> dict[str, object]:
        counts = state.verification_counts("immich")
        location = config.staging_location()
        free: int | None = None
        if location is not None:

            def read_free() -> int | None:
                try:
                    return location.free_space()
                except LocationError:
                    return None

            value = cached_call(f"free:{location.describe()}", 60, read_free)
            free = value if isinstance(value, int) else None
        return {
            "immich": config.immich(),
            "uploaded": sum(counts.values()),
            "awaiting": counts.get("uploaded", 0),
            "general": config.general(),
            "free": free,
        }

    def cleanup_summary(config: Config, state: State) -> dict[str, object]:
        location = config.staging_location()
        staged: list[cleanup.ExportCopy] | None = []
        if location is not None:
            found = folder_listing(location)
            staged = None if found is None else cleanup.staged_exports(location, state, found)
        folder_ready = [c for c in staged or [] if c.ready]
        drive_ready = [
            c
            for c in cleanup.drive_exports(state, drive_labels(config))
            if c.ready and any(p.removed_at is None for p in c.parts)
        ]
        folder_bytes = sum(c.size for c in folder_ready)
        drive_bytes = sum(p.size for c in drive_ready for p in c.parts if p.removed_at is None)
        return {
            "total": folder_bytes + drive_bytes,
            "folder_exports": len(folder_ready),
            "folder_bytes": folder_bytes,
            "folder_unreadable": staged is None,
            "drive_exports": len(drive_ready),
            "drive_bytes": drive_bytes,
        }

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

    def drive_labels(config: Config) -> dict[str, str]:
        return {f"gdrive:{s.location}": s.name for s in config.sources() if s.kind == "gdrive"}

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
        folder_listing=folder_listing,
        latest_export=latest_export,
        drive_labels=drive_labels,
        dashboard_notice=dashboard_notice,
        cached_call=cached_call,
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
