"""The web application.

Every page except the login, first-run setup, health check and static files needs a session.
Every state-changing request must come from this site (Origin check) and, once logged in, carry
the session's CSRF token. Responses carry a strict Content Security Policy: scripts and styles
load only from this server, and the pages contain no inline script.
"""

import io
import json
import logging
import time
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from googich_takeaway import __version__, cleanup, downloads, reminders, updates
from googich_takeaway.config import (
    BAR_STYLES,
    COLOUR_SCHEMES,
    DASHBOARD_ITEMS,
    DOWNLOAD_FOLDER_KIND,
    MAX_KEY_FILE_BYTES,
    MOTION,
    THEMES,
    Config,
    ConfigError,
    Look,
)
from googich_takeaway.credentials import SecretBox, load_master_key, master_key_path
from googich_takeaway.destinations.immich import ImmichClient, ImmichError
from googich_takeaway.locations import (
    Location,
    LocationError,
    SmbSettings,
    StoredFile,
    close_shared_smb,
)
from googich_takeaway.locations import archives as list_archives
from googich_takeaway.logs import LogBuffer, Logs
from googich_takeaway.pipeline import LATEST_EXPORT_SETTING, RunOptions
from googich_takeaway.progress import Stage, format_duration, format_size
from googich_takeaway.schedule import TAKEOUT_FREQUENCIES, WEEKDAYS, Schedule, upcoming_runs
from googich_takeaway.sources.base import SourceError
from googich_takeaway.sources.gdrive import GoogleDriveSource
from googich_takeaway.sources.local import LocalSource
from googich_takeaway.state import State
from googich_takeaway.web import auth, demo, help
from googich_takeaway.worker import PAUSED_RUN, Worker

log = logging.getLogger("googich.web")

HERE = Path(__file__).parent
COOKIE = "googich_session"
SESSION_LIFETIME = timedelta(days=7)
OPEN_PATHS = ("/login", "/setup", "/healthz", "/static/")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
NOTICES = {
    "started": "Run started. Progress shows below.",
    "busy": "A run is already going; it was left to finish.",
    "pausing": "Pausing at the next safe point. Press Resume to continue later.",
    "cancelling": "Cancelling at the next safe point.",
    "idle": "No run is going.",
    "resumed": "Resuming the paused run.",
    "discarded": "The paused run was set aside. The next run starts afresh, skipping what is done.",
    "schedule-paused": "Scheduled runs are paused. Run now still works.",
    "schedule-resumed": "Scheduled runs are on again.",
}
RUN_NOTICES = {"started", "pausing", "cancelling", "resumed"}
"""Notices about the run going now: not shown once it has ended (a reload, or the page coming
back after the run)."""
TIMEZONES = [
    *sorted(
        z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/"))
    ),
    "UTC",
]
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
REQUIRED_PERMISSIONS = ("asset.upload", "asset.read")
OPTIONAL_PERMISSIONS = ("stack.create",)

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # Not "no-referrer": browsers then send "Origin: null" on this site's own form posts, which
    # the same-origin check must reject. "same-origin" still sends nothing to other sites.
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


@dataclass(frozen=True)
class WebSettings:
    state_path: Path
    master_key_path: Path | None = None
    """Defaults to $GOOGICH_MASTER_KEY_FILE, or master.key next to the state database."""
    demo: bool = False
    """Offer a simulated run for previewing the progress views (GOOGICH_DEMO=1)."""
    trusted_proxies: tuple[str, ...] = ()
    """Reverse proxies (IP addresses or networks) whose X-Forwarded-Proto and X-Forwarded-For
    headers are believed (GOOGICH_TRUSTED_PROXIES). Without them, the app behind an HTTPS proxy
    sees plain http and refuses form posts as cross-site."""


ImmichFactory = Callable[[str, str], ImmichClient]
DriveFactory = Callable[[str, dict[str, object]], GoogleDriveSource]


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
        if request.method not in UNSAFE_METHODS or request.url.path.startswith(OPEN_PATHS):
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

    def open_state() -> Iterator[State]:
        with State(settings.state_path) as state:
            yield state

    StateDep = Annotated[State, Depends(open_state)]

    def open_config(state: StateDep) -> Config:
        return Config(state, box, clock)

    ConfigDep = Annotated[Config, Depends(open_config)]

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

    @app.get("/activity", response_class=HTMLResponse)
    def activity_view(request: Request, state: StateDep) -> Response:
        """Polled by the menu bar on every page, so a run started elsewhere shows up."""
        return templates.TemplateResponse(request, "_activity.html", {"activity": activity(state)})

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
            if requested:
                finished = helper.version == requested and helper.state in ("done", "failed")
                if not finished:
                    pausing = worker.status_running()
                    return {"kind": "waiting", "version": requested, "pausing": pausing}
                state.set_setting("updates.requested", None, clock())
                if helper.state == "failed":
                    worker.resume_after_update()  # the app did not restart, so resume here
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
        if not path.startswith(OPEN_PATHS):
            csrf = _session_csrf(request, settings.state_path, clock())
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

    @app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    @app.get("/setup", response_class=HTMLResponse)
    def setup_form(request: Request, state: StateDep) -> Response:
        if state.password_hash() is not None:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request, "setup.html", {"error": None})

    @app.post("/setup", response_class=HTMLResponse)
    def setup(
        request: Request,
        state: StateDep,
        token: Annotated[str, Form()],
        password: Annotated[str, Form()],
        confirm: Annotated[str, Form()],
    ) -> Response:
        if state.password_hash() is not None or not app.state.setup_token:
            return RedirectResponse("/login", status_code=303)
        address = _client(request)
        wait = login_throttle.retry_after(address)
        if wait:
            return _throttled(request, templates, "setup.html", wait)
        if not auth.same(token.strip(), app.state.setup_token):
            login_throttle.failed(address)
            return templates.TemplateResponse(
                request, "setup.html", {"error": "That setup token is not right."}, status_code=400
            )
        problem = auth.password_problem(password, confirm)
        if problem:
            return templates.TemplateResponse(
                request, "setup.html", {"error": problem}, status_code=400
            )
        state.set_password_hash(auth.hash_password(password), clock())
        app.state.setup_token = None
        login_throttle.succeeded(address)
        log.warning("First-run setup complete; password set")
        return _start_session(request, state, clock())

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, state: StateDep) -> Response:
        if state.password_hash() is None:
            return RedirectResponse("/setup", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login", response_class=HTMLResponse)
    def login(request: Request, state: StateDep, password: Annotated[str, Form()]) -> Response:
        stored = state.password_hash()
        if stored is None:
            return RedirectResponse("/setup", status_code=303)
        address = _client(request)
        wait = login_throttle.retry_after(address)
        if wait:
            return _throttled(request, templates, "login.html", wait)
        if not auth.verify_password(stored, password):
            login_throttle.failed(address)
            log.warning("Failed login from %s", address)
            return templates.TemplateResponse(
                request, "login.html", {"error": "Wrong password."}, status_code=401
            )
        login_throttle.succeeded(address)
        old = request.cookies.get(COOKIE)
        if old:
            state.delete_session(auth.token_hash(old))  # never reuse a pre-login session
        return _start_session(request, state, clock())

    @app.post("/logout")
    def logout(request: Request, state: StateDep) -> Response:
        token = request.cookies.get(COOKIE)
        if token:
            state.delete_session(auth.token_hash(token))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE, path="/")
        return response

    listing_cache: dict[str, tuple[float, list[StoredFile] | None]] = {}

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
            if downloading_now != [name or ""]:
                # A download finished or started: read the folder again, so it is counted once.
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

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, config: ConfigDep, state: StateDep) -> Response:
        items = config.dashboard_items()
        return page(
            request,
            config,
            "dashboard.html",
            items=items,
            dashboard_items=DASHBOARD_ITEMS,
            journey=journey(config, state) if {"journey", "destinations"} & set(items) else None,
            source_cards=source_summaries(config, state) if "sources" in items else [],
            destinations=destination_summary(config, state) if "destinations" in items else None,
            cleanable=cleanup_summary(config, state) if "cleanup" in items else None,
            immich=config.immich(),
            general=config.general(),
            sources=config.sources(),
            schedule=config.schedule(),
            status=worker.status(),
            progress=worker.progress(),
            failures=config.scheduled_failures(),
            runs=state.recent_runs(15) if "runs" in items else [],
            last_run=next(iter(state.recent_runs(1)), None),
            notice=dashboard_notice(request.query_params.get("notice", "")),
            reminders=reminders.takeout_reminders(config, state, clock()),
            zone=_zone(config.general().timezone),
        )

    @app.get("/dashboard/configure", response_class=HTMLResponse)
    def dashboard_configure(request: Request, config: ConfigDep) -> Response:
        return page(
            request,
            config,
            "dashboard_configure.html",
            items=config.dashboard_items(),
            dashboard_items=DASHBOARD_ITEMS,
            saved=request.query_params.get("saved"),
        )

    @app.post("/dashboard/items")
    def save_dashboard(
        config: ConfigDep,
        items: Annotated[list[str] | None, Form()] = None,
        back: Annotated[str, Form()] = "/",
    ) -> Response:
        try:
            config.save_dashboard_items(items or [])
        except ConfigError:
            return Response("Unknown dashboard item.", status_code=400)
        if back == "/dashboard/configure":  # only ever these two places
            return RedirectResponse("/dashboard/configure?saved=1", status_code=303)
        return RedirectResponse("/", status_code=303)

    @app.post("/demo/run")
    def demo_run() -> Response:
        if not settings.demo:
            return Response(status_code=404)
        demo.play(worker)
        return RedirectResponse("/", status_code=303)

    @app.get("/status", response_class=HTMLResponse)
    def status_card(request: Request, config: ConfigDep, state: StateDep) -> Response:
        """Polled by the dashboard while a run is going; reloads the page when it ends."""
        current = worker.status()
        if not current.running:
            # Back to the plain dashboard: a "Pausing…" or "Started" notice in the address
            # would otherwise come back with the reload.
            return Response(status_code=200, headers={"HX-Redirect": "/"})
        return templates.TemplateResponse(
            request,
            "_status.html",
            {
                "progress": worker.progress(),
                "items": config.dashboard_items(),
                "notice": None,
                "immich_link": config.immich().link,
                "journey": journey(config, state),
                "look": config.look(),
                "status": current,
                "schedule": config.schedule(),
                "general": config.general(),
                "sources": config.sources(),
                "zone": _zone(config.general().timezone),
            },
        )

    @app.get("/logs", response_class=HTMLResponse)
    def logs_page(
        request: Request,
        config: ConfigDep,
        state: StateDep,
        level: str = "INFO",
        q: str = "",
        run: int | None = None,
        follow: int = 1,
    ) -> Response:
        since = until = None
        selected = None
        if run is not None:
            selected = next((r for r in state.recent_runs(200) if r.id == run), None)
            if selected:
                since, until = selected.started_at, selected.finished_at
        entries = log_store.buffer.query(level=level, text=q[:200], since=since, until=until)
        return page(
            request,
            config,
            "logs.html",
            entries=entries,
            retention_days=config.log_retention_days(),
            log_usage=log_usage(config.state),
            saved=request.query_params.get("saved"),
            level=level.upper() if level.upper() in LOG_LEVELS else "INFO",
            levels=LOG_LEVELS,
            q=q[:200],
            run=selected,
            follow=bool(follow) and selected is None,
            last_seq=entries[-1].seq if entries else 0,
            zone=_zone(config.general().timezone),
        )

    @app.get("/logs/tail", response_class=HTMLResponse)
    def logs_tail(
        request: Request, config: ConfigDep, after: int = 0, level: str = "INFO", q: str = ""
    ) -> Response:
        entries = log_store.buffer.query(level=level, text=q[:200], after=after)
        context = {
            "entries": entries,
            "zone": _zone(config.general().timezone),
            "last_seq": entries[-1].seq if entries else after,
            "follow": True,
            "oob": True,
            "level": level,
            "q": q[:200],
        }
        rows = templates.get_template("_log_rows.html").render(context)
        tail = templates.get_template("_log_tail.html").render(context)
        return HTMLResponse(rows + tail)

    def log_usage(state: State) -> dict[str, int]:
        """How much space the log files take now."""
        sizes = []
        for path in log_store.files():
            try:
                sizes.append(path.stat().st_size)
            except OSError:
                continue
        return {"files": len(sizes), "bytes": sum(sizes)}

    @app.get("/logs/download")
    def logs_download() -> Response:
        """Every log file kept, oldest first, in one zip.

        Each file is read whole before anything is sent: the current file grows while the app
        runs, and streaming it from disk sent more bytes than the Content-Length announced,
        so browsers gave up on the download."""
        files = log_store.files()
        if not files:
            return Response("No log file yet.", status_code=404, media_type="text/plain")
        packed = io.BytesIO()
        with zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in reversed(files):
                try:
                    data = path.read_bytes()
                    written = datetime.fromtimestamp(path.stat().st_mtime)
                except OSError:
                    continue  # rotated away meanwhile
                entry = zipfile.ZipInfo(path.name, written.timetuple()[:6])
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, data)
        stamp = f"{datetime.now(UTC):%Y%m%d-%H%M%S}"
        return Response(
            packed.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="googich-logs-{stamp}.zip"'},
        )

    # --- help --------------------------------------------------------------------------------

    @app.get("/help", response_class=HTMLResponse)
    def help_index(request: Request, config: ConfigDep, q: str = "") -> Response:
        query = " ".join(q.split())[:100]
        return page(
            request,
            config,
            "help.html",
            topics=help.TOPICS,
            q=query,
            hits=help.search(query) if query else [],
        )

    @app.get("/help/{slug}", response_class=HTMLResponse)
    def help_page(request: Request, config: ConfigDep, slug: str) -> Response:
        found = help.page(slug)
        if found is None:
            return page(request, config, "help.html", topics=help.TOPICS, status_code=404)
        return page(request, config, "help_page.html", doc=found, topics=help.TOPICS)

    def latest_release(config: Config) -> tuple[updates.UpdateInfo | None, str | None]:
        """The latest release, checked as the page opens, and the version to show for it: never
        older than the version running, which a reading saved before an update could be."""
        known = updates.check_on_view(config.state, clock, github_transport)
        if known is None:
            return None, None
        return known, __version__ if updates.is_newer(__version__, known.latest) else known.latest

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

    # --- cleanup -------------------------------------------------------------------------------

    def drive_labels(config: Config) -> dict[str, str]:
        return {f"gdrive:{s.location}": s.name for s in config.sources() if s.kind == "gdrive"}

    @app.get("/cleanup", response_class=HTMLResponse)
    def cleanup_page(
        request: Request,
        config: ConfigDep,
        state: StateDep,
        message: str | None = None,
        error: str | None = None,
    ) -> Response:
        staging = config.staging_location()
        try:
            staged = cleanup.staged_exports(staging, state)
            partials = cleanup.partial_downloads(staging)
        except LocationError as problem:
            staged, partials = [], []
            error = error or f"Cannot read the download folder: {problem}"
        return page(
            request,
            config,
            "cleanup.html",
            staged=staged,
            partials=partials,
            drive=cleanup.drive_exports(state, drive_labels(config)),
            running=worker.status().running,
            message=message,
            error=error,
            zone=_zone(config.general().timezone),
        )

    @app.get("/cleanup/staged/{export_id}/confirm", response_class=HTMLResponse)
    def confirm_delete_staged(
        request: Request, config: ConfigDep, state: StateDep, export_id: str
    ) -> Response:
        """Before deleting: list exactly which files go, and what stays."""
        try:
            staged = cleanup.staged_exports(config.staging_location(), state)
        except LocationError as problem:
            return RedirectResponse(f"/cleanup?error={quote(str(problem))}", status_code=303)
        copy = next((c for c in staged if c.export_id == export_id and c.ready), None)
        if copy is None:
            note = "That export is not ready to delete."
            return RedirectResponse(f"/cleanup?error={quote(note)}", status_code=303)
        return page(
            request,
            config,
            "cleanup_confirm.html",
            copy=copy,
            folder=config.general().describe(),
            running=worker.status().running,
        )

    @app.get("/cleanup/partial/{name}/confirm", response_class=HTMLResponse)
    def confirm_delete_partial(request: Request, config: ConfigDep, name: str) -> Response:
        try:
            partials = cleanup.partial_downloads(config.staging_location())
        except LocationError as problem:
            return RedirectResponse(f"/cleanup?error={quote(str(problem))}", status_code=303)
        found = next((p for p in partials if p.name == name), None)
        if found is None:
            return RedirectResponse("/cleanup?error=No+such+partial+download.", status_code=303)
        return page(
            request,
            config,
            "cleanup_confirm.html",
            partial=found,
            folder=config.general().describe(),
            running=worker.status().running,
        )

    @app.post("/cleanup/staged/{export_id}")
    def delete_staged(
        config: ConfigDep,
        state: StateDep,
        export_id: str,
        confirm: Annotated[str, Form()] = "",
    ) -> Response:
        staging = config.staging_location()
        if staging is None:
            return RedirectResponse("/cleanup?error=No+download+folder+is+set.", status_code=303)
        if worker.status().running:
            return RedirectResponse(
                "/cleanup?error=Wait+for+the+current+run+to+finish.", status_code=303
            )
        try:
            freed = cleanup.delete_staged_export(
                staging, state, export_id, bool(confirm), log=log.warning
            )
        except cleanup.CleanupError as error:
            return RedirectResponse(f"/cleanup?error={quote(str(error))}", status_code=303)
        listing_cache.clear()
        note = f"Deleted export {export_id} from the download folder, freeing {format_size(freed)}."
        return RedirectResponse(f"/cleanup?message={quote(note)}", status_code=303)

    @app.post("/cleanup/partial/{name}")
    def delete_partial(config: ConfigDep, name: str) -> Response:
        staging = config.staging_location()
        if staging is None or worker.status().running:
            return RedirectResponse(
                "/cleanup?error=Not+possible+while+a+run+is+going.", status_code=303
            )
        try:
            freed = cleanup.delete_partial(staging, name)
        except cleanup.CleanupError as error:
            return RedirectResponse(f"/cleanup?error={quote(str(error))}", status_code=303)
        log.warning("Deleted partial download %s", name)
        note = f"Deleted the partial download of {name}, freeing {format_size(freed)}."
        return RedirectResponse(f"/cleanup?message={quote(note)}", status_code=303)

    @app.post("/cleanup/drive/{export_id}/recheck", response_class=HTMLResponse)
    def recheck_drive(
        request: Request, config: ConfigDep, state: StateDep, export_id: str
    ) -> Response:
        immich = config.immich()
        key = config.immich_key()
        if not immich.url or not key:
            return result(request, False, "Set up Immich first.")
        try:
            with immich_factory(immich.url, key) as client:
                found = cleanup.recheck(state, client, "immich", export_id)
        except ImmichError as error:
            return result(request, False, str(error))
        if found.checked == 0:
            return result(request, False, "No uploads are recorded for this export.")
        if found.ok:
            return result(
                request, True, f"All {found.checked} files are in Immich now. Safe to remove."
            )
        problems = []
        if found.missing:
            problems.append(f"{found.missing} are no longer in Immich")
        if found.trashed:
            problems.append(f"{found.trashed} are in Immich's trash")
        return result(
            request,
            False,
            f"Of {found.checked} files, {' and '.join(problems)}. Keep this archive in Drive, "
            "and run a Re-import (or restore them in Immich) first.",
        )

    @app.post("/runs")
    def run_now(
        reimport: Annotated[str, Form()] = "",
        download_again: Annotated[str, Form()] = "",
    ) -> Response:
        options = RunOptions(reimport=bool(reimport), download_again=bool(download_again))
        if not worker.request_run(options):
            return RedirectResponse("/?notice=busy", status_code=303)
        worker.discard_paused()  # a new run replaces a paused one
        return RedirectResponse("/?notice=started", status_code=303)

    @app.get("/runs/stop", response_class=HTMLResponse)
    def confirm_stop(request: Request, config: ConfigDep, kind: str = "pause") -> Response:
        """Before Pause or Cancel: say exactly what happens to the file being worked on."""
        if kind not in ("pause", "cancel"):
            kind = "pause"
        if not worker.status().running:
            return RedirectResponse("/?notice=idle", status_code=303)
        active = worker.tracker.active()
        return page(
            request,
            config,
            "stop_confirm.html",
            kind=kind,
            stage=active[0].value if active else None,
            item=active[1] if active else None,
        )

    @app.post("/runs/pause")
    def pause_run() -> Response:
        stopping = worker.request_stop("pause")
        return RedirectResponse(f"/?notice={'pausing' if stopping else 'idle'}", status_code=303)

    @app.post("/runs/cancel")
    def cancel_run() -> Response:
        stopping = worker.request_stop("cancel")
        return RedirectResponse(f"/?notice={'cancelling' if stopping else 'idle'}", status_code=303)

    @app.post("/runs/resume")
    def resume_run() -> Response:
        resumed = worker.resume_paused()
        return RedirectResponse(f"/?notice={'resumed' if resumed else 'busy'}", status_code=303)

    @app.post("/runs/discard")
    def discard_run() -> Response:
        worker.discard_paused()
        return RedirectResponse("/?notice=discarded", status_code=303)

    @app.post("/schedule/pause")
    def pause_schedule(config: ConfigDep) -> Response:
        config.set_schedule_paused(True, by_user=True)
        return RedirectResponse("/?notice=schedule-paused", status_code=303)

    @app.post("/schedule/resume")
    def resume(config: ConfigDep) -> Response:
        config.set_schedule_paused(False)
        return RedirectResponse("/?notice=schedule-resumed", status_code=303)

    # --- settings, schedule, notifications, updates ------------------------------------------
    #
    # A form that fails to save shows its page again with an error. ``draft`` holds what was typed,
    # so it is shown again; passwords, keys and notification addresses never are.

    def settings_page(request: Request, config: Config, error: str | None = None) -> Response:
        """Time zone, look and feel, and how long logs are kept."""
        return page(
            request,
            config,
            "settings.html",
            general=config.general(),
            retention_days=config.log_retention_days(),
            log_usage=log_usage(config.state),
            timezones=TIMEZONES,
            themes=THEMES,
            colour_schemes=COLOUR_SCHEMES,
            bar_styles=BAR_STYLES,
            motions=MOTION,
            saved=request.query_params.get("saved"),
            error=error,
            status_code=400 if error else 200,
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_view(request: Request, config: ConfigDep) -> Response:
        return settings_page(request, config)

    @app.post("/settings/timezone")
    def save_timezone(
        request: Request, config: ConfigDep, timezone: Annotated[str, Form()]
    ) -> Response:
        try:
            config.save_timezone(timezone)
        except ConfigError as error:
            return settings_page(request, config, error=str(error))
        return RedirectResponse("/settings?saved=timezone#timezone", status_code=303)

    @app.post("/settings/look")
    def save_look(
        request: Request,
        config: ConfigDep,
        theme: Annotated[str, Form()] = "auto",
        colours: Annotated[str, Form()] = "spectrum",
        bars: Annotated[str, Form()] = "striped",
        motion: Annotated[str, Form()] = "auto",
    ) -> Response:
        try:
            config.save_look(theme, colours, bars, motion)
        except ConfigError as error:
            return settings_page(request, config, error=str(error))
        return RedirectResponse("/settings?saved=look#look", status_code=303)

    @app.post("/settings/logs")
    def save_log_retention(
        request: Request,
        config: ConfigDep,
        days: Annotated[str, Form()] = "",
        back: Annotated[str, Form()] = "",
    ) -> Response:
        try:
            config.save_log_retention(days)
        except ConfigError as error:
            if back == "logs":
                return RedirectResponse("/logs?saved=retention-invalid", status_code=303)
            return settings_page(request, config, error=str(error))
        worker.prune_logs()  # a shorter period applies straight away
        if back == "logs":  # only ever these two places
            return RedirectResponse("/logs?saved=retention", status_code=303)
        return RedirectResponse("/settings?saved=logs#logs", status_code=303)

    def schedule_page(
        request: Request,
        config: Config,
        error: str | None = None,
        draft: dict[str, str] | None = None,
    ) -> Response:
        schedule = config.schedule()
        zone = _zone(config.general().timezone)
        today = clock().astimezone(zone).date()
        last = next(
            (r.started_at for r in config.state.recent_runs(50) if r.trigger == "schedule"), None
        )
        exports = schedule.exports()
        arrivals = [t.astimezone(zone).date() for t in config.state.download_times()]
        return page(
            request,
            config,
            "schedule.html",
            schedule=schedule,
            upcoming=upcoming_runs(schedule, clock(), zone, last),
            takeout_status=schedule.takeout_status(today),
            paused=config.schedule_paused(),
            zone=zone,
            today=today,
            takeout_frequencies=TAKEOUT_FREQUENCIES,
            takeout_ends=reminders.schedule_end(schedule.takeout_started),
            checks=[
                (
                    day,
                    schedule.check_days(day),
                    _export_state(day, exports, arrivals, today, schedule),
                )
                for day in exports
            ],
            next_export=next((day for day in exports if day >= today), None),
            weekdays=WEEKDAYS,
            general=config.general(),
            saved=request.query_params.get("saved"),
            error=error,
            draft=draft or {},
            status_code=400 if error else 200,
        )

    @app.get("/schedule", response_class=HTMLResponse)
    def schedule_view(request: Request, config: ConfigDep) -> Response:
        return schedule_page(request, config)

    @app.post("/schedule/takeout")
    def save_takeout_schedule(
        request: Request,
        config: ConfigDep,
        started: Annotated[str, Form()] = "",
        every_months: Annotated[str, Form()] = "2",
    ) -> Response:
        try:
            config.save_takeout_schedule(started, every_months)
        except ConfigError as error:
            return schedule_page(request, config, error=str(error))
        return RedirectResponse("/schedule?saved=takeout#takeout", status_code=303)

    @app.post("/schedule")
    def save_schedule(
        request: Request,
        config: ConfigDep,
        mode: Annotated[str, Form()],
        at: Annotated[str, Form()] = "03:00",
        weekday: Annotated[str, Form()] = "6",
        every_hours: Annotated[str, Form()] = "24",
        pause_after: Annotated[str, Form()] = "3",
    ) -> Response:
        try:
            config.save_schedule(mode, at, weekday, every_hours, pause_after)
        except ConfigError as error:
            draft = {
                "mode": mode,
                "at": at,
                "weekday": weekday,
                "every_hours": every_hours,
                "pause_after": pause_after,
            }
            return schedule_page(request, config, str(error), draft=draft)
        return RedirectResponse("/schedule?saved=schedule#schedule", status_code=303)

    @app.post("/schedule/follow")
    def save_follow_options(
        request: Request,
        config: ConfigDep,
        retry_days: Annotated[str, Form()] = "1",
        wait_days: Annotated[str, Form()] = "14",
        fallback_weekly: Annotated[str, Form()] = "",
        fallback_weekday: Annotated[str, Form()] = "6",
    ) -> Response:
        try:
            config.save_follow_options(
                retry_days, wait_days, bool(fallback_weekly), fallback_weekday
            )
        except ConfigError as error:
            return schedule_page(request, config, str(error))
        return RedirectResponse("/schedule?saved=follow#follow", status_code=303)

    def notifications_page(
        request: Request, config: Config, error: str | None = None, draft_name: str = ""
    ) -> Response:
        return page(
            request,
            config,
            "notifications.html",
            outcomes=config.notification_outcomes(),
            targets=config.notification_targets(),
            saved=request.query_params.get("saved"),
            error=error,
            draft_name=draft_name,
            status_code=400 if error else 200,
        )

    @app.get("/notifications", response_class=HTMLResponse)
    def notifications_view(request: Request, config: ConfigDep) -> Response:
        return notifications_page(request, config)

    @app.post("/notifications")
    def save_notifications(
        request: Request,
        config: ConfigDep,
        outcomes: Annotated[list[str] | None, Form()] = None,
    ) -> Response:
        try:
            config.save_notifications(None, outcomes or [])
        except ConfigError as error:
            return notifications_page(request, config, error=str(error))
        return RedirectResponse("/notifications?saved=outcomes#outcomes", status_code=303)

    @app.post("/notifications/add")
    def add_notification(
        request: Request,
        config: ConfigDep,
        name: Annotated[str, Form()] = "",
        url: Annotated[str, Form()] = "",
    ) -> Response:
        try:
            config.add_notification_target(name, url)
        except ConfigError as error:  # the URL is not put back into the page
            return notifications_page(request, config, error=str(error), draft_name=name)
        log.info("Notification %r added", name.strip())
        return RedirectResponse("/notifications?saved=added", status_code=303)

    @app.post("/notifications/{target_id}/remove")
    def remove_notification(config: ConfigDep, target_id: str) -> Response:
        config.remove_notification_target(target_id)
        return RedirectResponse("/notifications?saved=removed", status_code=303)

    @app.post("/notifications/test", response_class=HTMLResponse)
    def test_notifications(request: Request, config: ConfigDep) -> Response:
        if not config.has_notification_urls():
            return result(request, False, "Add a notification first.")
        ok, reasons, _ = config.test_notification()
        return test_result(request, ok, reasons)

    @app.post("/notifications/{target_id}/test", response_class=HTMLResponse)
    def test_notification(request: Request, config: ConfigDep, target_id: str) -> Response:
        found = next((t for t in config.notification_targets() if t.id == target_id), None)
        if found is None:
            return result(request, False, "No such notification.")
        ok, reasons, note = config.test_notification(target_id)
        log.info("Test notification to %r: %s", found.name, "sent" if ok else "failed")
        return test_result(request, ok, reasons, note)

    def test_result(request: Request, ok: bool, reasons: list[str], note: str = "") -> Response:
        if ok:
            return result(request, True, f"Test notification sent.{note}")
        why = " ".join(reasons) if reasons else "The service did not accept it."
        return result(request, False, f"Not delivered: {why}")

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
        return RedirectResponse(path if path.startswith("/") else "/", status_code=303)

    @app.post("/updates/check")
    def check_updates(state: StateDep) -> Response:
        found = updates.check_now(state, clock, github_transport)
        return RedirectResponse(f"/updates?saved=check-{found.value}", status_code=303)

    @app.post("/updates/daily")
    def save_updates(state: StateDep, check: Annotated[str, Form()] = "") -> Response:
        updates.set_enabled(state, bool(check), clock())
        return RedirectResponse("/updates?saved=updates", status_code=303)

    # --- destinations --------------------------------------------------------------------------

    def destinations_page(
        request: Request,
        config: Config,
        error: str | None = None,
        saved: str | None = None,
        draft: dict[str, str] | None = None,
        failed: str | None = None,
    ) -> Response:
        """Where photos go (Immich) and where archives wait (the download folder).

        ``draft`` holds what was typed into a form that failed to save, so it is shown again.
        Passwords and keys are never put back into the page."""
        return page(
            request,
            config,
            "destinations.html",
            immich=config.immich(),
            general=config.general(),
            has_smb_password=config.has_smb_password(),
            error=error,
            saved=saved,
            draft=draft or {},
            failed=failed,
            status_code=400 if error else 200,
        )

    @app.get("/destinations", response_class=HTMLResponse)
    def destinations_view(
        request: Request, config: ConfigDep, saved: str | None = None
    ) -> Response:
        return destinations_page(request, config, saved=saved)

    @app.post("/destinations/immich")
    def save_immich(
        request: Request,
        config: ConfigDep,
        url: Annotated[str, Form()],
        public_url: Annotated[str, Form()] = "",
        api_key: Annotated[str, Form()] = "",
    ) -> Response:
        try:
            config.save_immich(url, public_url, api_key or None)
        except ConfigError as error:
            draft = {"url": url, "public_url": public_url}
            return destinations_page(request, config, str(error), draft=draft, failed="immich")
        return RedirectResponse("/destinations?saved=immich#immich", status_code=303)

    @app.post("/destinations/immich/test", response_class=HTMLResponse)
    def test_immich(request: Request, config: ConfigDep) -> Response:
        immich = config.immich()
        key = config.immich_key()
        if not immich.url or not key:
            return result(request, False, "Save the Immich address and API key first.")
        try:
            with immich_factory(immich.url, key) as client:
                version = client.server_version()
                granted = set(client.key_permissions())
        except ImmichError as error:
            return result(request, False, str(error))
        missing = [p for p in REQUIRED_PERMISSIONS if p not in granted and "all" not in granted]
        optional = [p for p in OPTIONAL_PERMISSIONS if p not in granted and "all" not in granted]
        if missing:
            return result(
                request,
                False,
                f"Connected to Immich {version}, but the API key lacks: {', '.join(missing)}.",
            )
        note = f" Optional, for stacking edited copies: {', '.join(optional)}." if optional else ""
        return result(request, True, f"Connected to Immich {version}. The API key is fine.{note}")

    @app.post("/destinations/downloads")
    def save_general(
        request: Request,
        config: ConfigDep,
        storage: Annotated[str, Form()] = "local",
        staging: Annotated[str, Form()] = "",
        smb_server: Annotated[str, Form()] = "",
        smb_share: Annotated[str, Form()] = "",
        smb_folder: Annotated[str, Form()] = "",
        smb_username: Annotated[str, Form()] = "",
        smb_password: Annotated[str, Form()] = "",
        smb_domain: Annotated[str, Form()] = "",
        smb_port: Annotated[str, Form()] = "445",
    ) -> Response:
        try:
            if storage == "smb":
                config.save_smb(
                    smb_server,
                    smb_share,
                    smb_folder,
                    smb_username,
                    smb_password or None,
                    port=smb_port,
                    domain=smb_domain,
                    test=smb_test,
                )
            else:
                config.save_general(staging)
        except ConfigError as error:
            draft = {
                "storage": storage,
                "staging": staging,
                "smb_server": smb_server,
                "smb_share": smb_share,
                "smb_folder": smb_folder,
                "smb_username": smb_username,
                "smb_domain": smb_domain,
                "smb_port": smb_port,
            }
            return destinations_page(request, config, str(error), draft=draft, failed="downloads")
        return RedirectResponse("/destinations?saved=downloads#downloads", status_code=303)

    @app.post("/destinations/downloads/test", response_class=HTMLResponse)
    def test_storage(request: Request, config: ConfigDep) -> Response:
        location = config.staging_location()
        if location is None:
            return result(request, False, "Save a download folder first.")
        try:
            location.prepare()
            free = location.free_space()
        except LocationError as error:
            return result(request, False, str(error))
        except Exception as error:
            log.exception("Download folder test failed")
            return result(request, False, f"Unexpected error ({type(error).__name__}); see Logs.")
        space = f" {format_size(free)} free." if free is not None else ""
        return result(request, True, f"Can write to {location.describe()}.{space}")

    # --- sources -------------------------------------------------------------------------------

    def sources_page(request: Request, config: Config, error: str | None = None) -> Response:
        drives = [s for s in config.sources() if s.kind == "gdrive"]
        accounts = {s.id: config.drive_account(s.id) for s in drives}
        folder_names = {s.id: config.drive_folder_name(s.id) for s in drives}
        return page(
            request,
            config,
            "sources.html",
            sources=config.sources(),
            accounts=accounts,
            folder_names=folder_names,
            staging=config.general().describe() if config.general().configured else None,
            has_download_source=any(s.kind == DOWNLOAD_FOLDER_KIND for s in config.sources()),
            error=error,
            status_code=400 if error else 200,
        )

    @app.get("/sources", response_class=HTMLResponse)
    def sources_view(request: Request, config: ConfigDep) -> Response:
        return sources_page(request, config)

    @app.post("/sources/drive")
    async def add_drive(
        request: Request,
        config: ConfigDep,
        name: Annotated[str, Form()],
        folder_id: Annotated[str, Form()],
        key_file: Annotated[UploadFile, File()],
    ) -> Response:
        data = await key_file.read(MAX_KEY_FILE_BYTES + 1)
        try:
            source_id = config.add_drive_source(name, folder_id, data)
        except ConfigError as error:
            return sources_page(request, config, error=str(error))
        try:  # best effort: the folder may not be shared with the account yet
            with drive_factory(folder_id.strip(), config.drive_key(source_id)) as drive:
                config.set_drive_folder_name(source_id, drive.check_folder())
        except (SourceError, ConfigError):
            pass
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/download-folder")
    def add_download_folder(
        request: Request, config: ConfigDep, name: Annotated[str, Form()] = ""
    ) -> Response:
        if not config.general().configured:
            return sources_page(request, config, error="Choose the download folder first.")
        try:
            config.add_download_folder_source(name)
        except ConfigError as error:
            return sources_page(request, config, error=str(error))
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/local")
    def add_local(
        request: Request,
        config: ConfigDep,
        name: Annotated[str, Form()],
        path: Annotated[str, Form()],
    ) -> Response:
        try:
            config.add_local_source(name, path)
        except ConfigError as error:
            return sources_page(request, config, error=str(error))
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{source_id}/key")
    async def replace_key(
        request: Request,
        config: ConfigDep,
        source_id: int,
        key_file: Annotated[UploadFile, File()],
    ) -> Response:
        if not any(s.id == source_id and s.kind == "gdrive" for s in config.sources()):
            return Response(status_code=404)
        data = await key_file.read(MAX_KEY_FILE_BYTES + 1)
        try:
            config.replace_drive_key(source_id, data)
        except ConfigError as error:
            return sources_page(request, config, error=str(error))
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{source_id}/delete")
    def delete_source(config: ConfigDep, source_id: int) -> Response:
        config.delete_source(source_id)
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{source_id}/test", response_class=HTMLResponse)
    def test_source(request: Request, config: ConfigDep, source_id: int) -> Response:
        source = next((s for s in config.sources() if s.id == source_id), None)
        if source is None:
            return result(request, False, "No such source.")
        try:
            if source.kind == "local":
                files = LocalSource(Path(source.location)).list_archives()
            elif source.kind == DOWNLOAD_FOLDER_KIND:
                location = config.staging_location()
                if location is None:
                    return result(request, False, "No download folder is set.")
                try:
                    found = list_archives(location)
                except LocationError as error:
                    return result(request, False, f"Cannot read the download folder: {error}")
                count, size = len(found), sum(a.size for a in found) / 1000**3
                if not count:
                    return result(request, True, "Connected. No Takeout archives saved there yet.")
                return result(
                    request,
                    True,
                    f"Connected. {count} archives in the download folder, {size:.1f} GB.",
                )
            else:
                with drive_factory(source.location, config.drive_key(source_id)) as drive:
                    files = drive.list_archives()
                    if drive.folder_name:
                        config.set_drive_folder_name(source_id, drive.folder_name)
        except (SourceError, ConfigError) as error:
            return result(request, False, str(error))
        name = config.drive_folder_name(source_id) if source.kind == "gdrive" else None
        connected = f"Connected to the folder “{name}”." if name else "Connected."
        if not files:
            return result(request, True, f"{connected} No Takeout archives in the folder yet.")
        total = sum(f.size for f in files) / 1000**3
        return result(
            request, True, f"{connected} {len(files)} archives in the folder, {total:.1f} GB."
        )

    def result(request: Request, ok: bool, message: str) -> Response:
        return templates.TemplateResponse(request, "_result.html", {"ok": ok, "message": message})

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    if settings.trusted_proxies:
        # Added last, so it is the outermost layer: the origin check, Secure cookies and login
        # throttling all see the scheme and client address the proxy reports. Only these
        # proxies' forwarded headers are believed; anyone else's are ignored.
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=list(settings.trusted_proxies))
    return app


def _export_state(
    expected: date, exports: list[date], arrivals: list[date], today: date, schedule: Schedule
) -> str:
    """For the Schedule page: what happened to one expected Takeout export."""
    later = [day for day in exports if day > expected]
    until = later[0] if later else expected + timedelta(days=62)
    if any(expected <= day < until for day in arrivals):
        return "Arrived"
    if today < expected:
        return "Planned"
    if today < expected + timedelta(days=schedule.wait_days):
        return "Waiting"
    return "Not seen"


def _export_date(value: str) -> str:
    """``20261001`` (the start of a Takeout export ID) as ``01 Oct 2026``."""
    try:
        return datetime.strptime(value[:8], "%Y%m%d").strftime("%d %b %Y")
    except ValueError:
        return value


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _start_session(request: Request, state: State, now: datetime) -> Response:
    token = auth.new_token()
    state.create_session(auth.token_hash(token), auth.new_token(), now, now + SESSION_LIFETIME)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE,
        token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path="/",
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
    )
    return response


def _session_csrf(request: Request, state_path: Path, now: datetime) -> str | None:
    token = request.cookies.get(COOKIE)
    if not token or len(token) > 256:
        return None
    with State(state_path) as state:
        return state.get_session(auth.token_hash(token), now)


async def _csrf_ok(request: Request, expected: str) -> bool:
    sent = request.headers.get("X-CSRF-Token")
    if sent is None and request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        form = await request.form()
        value = form.get("csrf_token")
        sent = value if isinstance(value, str) else None
    return sent is not None and auth.same(sent, expected)


def _same_origin(request: Request) -> bool:
    """Browsers send Origin on every cross-site POST; require it to name this site's host.

    Only the host and port are compared, not the scheme. A forged request from another site
    always carries that site's host, which pages cannot change, so the scheme adds no protection
    here. Ignoring it lets the app work behind an HTTPS reverse proxy that forwards plain http,
    without any configuration.
    """
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin or origin == "null":
        return False
    sent = urlsplit(origin)
    if sent.scheme not in ("http", "https") or not sent.hostname:
        return False
    return _same_host(sent.netloc, request.url.netloc)


def _same_host(sent: str, served: str) -> bool:
    """Same host name, and the same port. A missing port matches only the standard ports
    (80 and 443), because a proxy may forward ``Host`` without the port the browser used."""
    sent_host, sent_port = _split_netloc(sent)
    served_host, served_port = _split_netloc(served)
    if sent_host != served_host:
        return False
    if sent_port and served_port:
        return sent_port == served_port
    return (sent_port or served_port) in (None, "80", "443")


def _split_netloc(netloc: str) -> tuple[str, str | None]:
    host = netloc.rsplit("@", 1)[-1].lower()
    if host.startswith("["):  # IPv6 literal, e.g. [::1]:8080
        end = host.find("]")
        name, rest = host[: end + 1], host[end + 1 :]
        return name, rest[1:] if rest.startswith(":") else None
    name, _, port = host.partition(":")
    return name, port or None


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _throttled(request: Request, templates: Jinja2Templates, page: str, wait: float) -> Response:
    message = f"Too many attempts. Try again in {int(wait) + 1} seconds."
    return templates.TemplateResponse(
        request,
        page,
        {"error": message},
        status_code=429,
        headers={"Retry-After": str(int(wait) + 1)},
    )


def _with_headers(response: Response) -> Response:
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    response.headers.setdefault("Cache-Control", "no-store")
    return response
