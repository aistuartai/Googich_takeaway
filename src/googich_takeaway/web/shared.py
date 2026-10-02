"""What the route modules share: the app's objects and helpers, and the request dependencies.

``create_app`` builds one ``Shared`` and passes it to each module's ``register``. The
dependencies find it on ``request.app.state.web``.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, Request, Response
from fastapi.templating import Jinja2Templates

from googich_takeaway.config import Config
from googich_takeaway.credentials import SecretBox
from googich_takeaway.locations import Location, SmbSettings, StoredFile
from googich_takeaway.logs import Logs
from googich_takeaway.state import State
from googich_takeaway.updates import UpdateInfo
from googich_takeaway.web import auth
from googich_takeaway.web.common import DriveFactory, ImmichFactory, WebSettings
from googich_takeaway.worker import Worker


@dataclass(frozen=True)
class Shared:
    settings: WebSettings
    worker: Worker
    templates: Jinja2Templates
    clock: Callable[[], datetime]
    box: SecretBox
    data_dir: Path
    log_store: Logs
    login_throttle: auth.LoginThrottle
    immich_factory: ImmichFactory
    drive_factory: DriveFactory
    smb_test: Callable[[SmbSettings], None] | None
    github_transport: httpx.BaseTransport | None
    page: Callable[..., Response]
    """Render a full page with the menu bar, update banner and run activity filled in."""
    result: Callable[[Request, bool, str], Response]
    """A short success or failure message, for htmx buttons such as Test."""
    activity: Callable[[State], dict[str, object] | None]
    update_banner: Callable[[Config], dict[str, object] | None]
    latest_release: Callable[[Config], tuple[UpdateInfo | None, str | None]]
    journey: Callable[[Config, State], dict[str, object]]
    source_summaries: Callable[[Config, State], list[dict[str, object]]]
    destination_summary: Callable[[Config, State], dict[str, object]]
    cleanup_summary: Callable[[Config, State], dict[str, object]]
    folder_listing: Callable[[Location], list[StoredFile] | None]
    latest_export: Callable[[State], dict[str, object] | None]
    drive_labels: Callable[[Config], dict[str, str]]
    dashboard_notice: Callable[[str], str | None]
    cached_call: Callable[[str, float, Callable[[], object]], object]
    log_usage: Callable[[], dict[str, int]]
    listing_cache: dict[str, tuple[float, list[StoredFile] | None]]
    """Download folder listings, read at most every 10 seconds; cleared after a delete."""


def _shared(request: Request) -> Shared:
    web: Shared = request.app.state.web
    return web


def open_state(request: Request) -> Iterator[State]:
    with State(_shared(request).settings.state_path) as state:
        yield state


StateDep = Annotated[State, Depends(open_state)]


def open_config(request: Request, state: StateDep) -> Config:
    web = _shared(request)
    return Config(state, web.box, web.clock)


ConfigDep = Annotated[Config, Depends(open_config)]
