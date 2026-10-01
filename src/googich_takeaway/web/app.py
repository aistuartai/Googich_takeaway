"""The web application.

Every page except the login, first-run setup, health check and static files needs a session.
Every state-changing request must come from this site (Origin check) and, once logged in, carry
the session's CSRF token. Responses carry a strict Content Security Policy: scripts and styles
load only from this server, and the pages contain no inline script.
"""

import logging
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from googich_takeaway import __version__
from googich_takeaway.state import State
from googich_takeaway.web import auth

log = logging.getLogger("googich.web")

HERE = Path(__file__).parent
COOKIE = "googich_session"
SESSION_LIFETIME = timedelta(days=7)
OPEN_PATHS = ("/login", "/setup", "/healthz", "/static/")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

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
    immich_public_url: str | None = None


def create_app(
    settings: WebSettings,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    throttle: auth.LoginThrottle | None = None,
) -> FastAPI:
    app = FastAPI(title="Googich Takeaway", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(version=__version__, immich_url=settings.immich_public_url)
    login_throttle = throttle or auth.LoginThrottle()

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

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
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
            if request.method in UNSAFE_METHODS and not await _csrf_ok(request, csrf):
                return _with_headers(Response("Missing or wrong CSRF token.", status_code=403))
        return _with_headers(await call_next(request))

    @app.get("/healthz", include_in_schema=False)
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

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> Response:
        return templates.TemplateResponse(request, "dashboard.html", {})

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    return app


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
    """Browsers send Origin on every cross-site POST; require it to be this site."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return False
    sent = urlsplit(origin)
    return f"{sent.scheme}://{sent.netloc}" == f"{request.url.scheme}://{request.url.netloc}"


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
