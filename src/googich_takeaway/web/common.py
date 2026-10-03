"""Constants and helpers shared by the web application's modules."""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from fastapi import Request, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from googich_takeaway.schedule import Schedule
from googich_takeaway.state import State
from googich_takeaway.web import auth

log = logging.getLogger("googich.web")

HERE = Path(__file__).parent
COOKIE = "googich_session"
SESSION_LIFETIME = timedelta(days=7)
OPEN_PATHS = frozenset({"/login", "/setup", "/healthz"})
"""Reachable without a session: these exact paths, and files under /static/."""
OPEN_BODY_LIMIT = 16 * 1024
"""Largest request body accepted before signing in (the login and setup forms are tiny)."""
BODY_LIMIT = 1024 * 1024
"""Largest request body accepted at all (key files are at most 64 KB)."""


def _is_open(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith("/static/")


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
NOTICES = {
    "started": "Run started. Progress shows below.",
    "busy": "A run is already going; it was left to finish.",
    "pausing": "Pausing at the next safe point. Press Resume to continue later.",
    "cancelling": "Cancelling at the next safe point.",
    "idle": "No run is going.",
    "resumed": "Resuming the paused run.",
    "discarded": "The paused run was set aside. The next run starts afresh, skipping what is done.",
    "ignored": "Failed files ignored: that export now counts as done, and Cleanup can offer it.",
    "schedule-paused": "Scheduled runs are paused. Run now still works.",
    "schedule-resumed": "Scheduled runs are on again.",
    "refreshed": "Figures read again just now.",
    "refresh-partly": "Some figures could not be read again; Logs says which and why.",
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
OPTIONAL_PERMISSIONS = {
    "asset.statistics": "the dashboard's count of everything in Immich",
    "stack.create": "stacking edited copies, in a later release",
}

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


def _body_refused(request: Request, limit: int) -> Response | None:
    """A response refusing the request body, if it is too large or of unknown length."""
    length = request.headers.get("content-length")
    if length is None:
        return Response("A request body of known length is required.", status_code=411)
    if not length.isdigit() or int(length) > limit:
        return Response("Request too large.", status_code=413)
    return None


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
